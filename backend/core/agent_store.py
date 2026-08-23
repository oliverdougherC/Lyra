"""SQLite persistence for Phase 4 workspace attachment, proposals, and commands.

This module is intentionally isolated from routes, execution, and migrations. It exposes
the table contract as SQL plus narrow stateful helpers so the later integration layer can
wire the approved Phase 4 surfaces without guessing the persistence rules.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from backend.core import agent_attempts, classes, commands, sessions, workspace_paths
from backend.core.errors import ConflictError, NotFoundError

READ = "read"
CHANGE_PROPOSALS = "change_proposals"
COMMANDS = "commands"

WORKSPACE_CHANGE_PENDING = "pending"
WORKSPACE_CHANGE_PARTIALLY_APPLIED = "partially_applied"
WORKSPACE_CHANGE_APPLIED = "applied"
WORKSPACE_CHANGE_REJECTED = "rejected"
WORKSPACE_CHANGE_STALE = "stale"
WORKSPACE_CHANGE_FAILED = "failed"

WORKSPACE_CHANGE_STATES: tuple[str, ...] = (
    WORKSPACE_CHANGE_PENDING,
    WORKSPACE_CHANGE_PARTIALLY_APPLIED,
    WORKSPACE_CHANGE_APPLIED,
    WORKSPACE_CHANGE_REJECTED,
    WORKSPACE_CHANGE_STALE,
    WORKSPACE_CHANGE_FAILED,
)

COMMAND_PENDING = "pending"
COMMAND_RUNNING = "running"
COMMAND_COMPLETED = "completed"
COMMAND_FAILED = "failed"
COMMAND_TIMED_OUT = "timed_out"
COMMAND_REJECTED = "rejected"
COMMAND_ABANDONED = "abandoned"

COMMAND_STATES: tuple[str, ...] = (
    COMMAND_PENDING,
    COMMAND_RUNNING,
    COMMAND_COMPLETED,
    COMMAND_FAILED,
    COMMAND_TIMED_OUT,
    COMMAND_REJECTED,
    COMMAND_ABANDONED,
)

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600
MAX_OUTPUT_BYTES = commands.DEFAULT_MAX_OUTPUT_BYTES
MAX_REASON_CHARS = 2_000
MAX_EXPECTED_SIGNAL_CHARS = 1_000
_UNSET = object()

TABLE_SQL = """
create table if not exists class_workspaces (
  id integer primary key,
  class_id integer not null unique references classes(id) on delete cascade,
  root_path text not null,
  display_name text not null,
  root_device integer not null,
  root_inode integer not null,
  read_enabled integer not null default 0 check (read_enabled in (0, 1)),
  change_proposals_enabled integer not null default 0
    check (change_proposals_enabled in (0, 1)),
  commands_enabled integer not null default 0 check (commands_enabled in (0, 1)),
  created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

create table if not exists workspace_changes (
  id integer primary key,
  workspace_id integer not null references class_workspaces(id) on delete cascade,
  session_id integer not null references chat_sessions(id) on delete cascade,
  relative_path text not null,
  base_hash text not null,
  base_content text not null,
  proposed_content text not null,
  file_device integer not null,
  file_inode integer not null,
  file_mode integer not null,
  newline text check (newline is null or newline in (char(10), char(13), char(13) || char(10))),
  rationale text,
  state text not null default 'pending' check (state in
    ('pending','partially_applied','applied','rejected','stale','failed')),
  accepted_hunks_json text not null default '[]',
  rejected_hunks_json text not null default '[]',
  before_hash text not null,
  after_hash text,
  state_reason text,
  created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

create table if not exists command_requests (
  id integer primary key,
  workspace_id integer not null references class_workspaces(id) on delete cascade,
  session_id integer not null references chat_sessions(id) on delete cascade,
  argv_json text not null,
  relative_cwd text not null,
  reason text not null,
  expected_signal text,
  timeout_seconds integer not null check (timeout_seconds between 1 and 600),
  state text not null default 'pending' check (state in
    ('pending','running','completed','failed','timed_out','rejected','abandoned')),
  confirmed_at text,
  started_at text,
  finished_at text,
  exit_code integer,
  stdout_text text,
  stderr_text text,
  state_reason text,
  created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

create index if not exists workspace_changes_workspace_state_idx
  on workspace_changes (workspace_id, state);
create index if not exists workspace_changes_session_idx on workspace_changes (session_id);
create index if not exists command_requests_workspace_state_idx
  on command_requests (workspace_id, state);
create index if not exists command_requests_session_idx on command_requests (session_id);
create unique index if not exists command_requests_one_active_per_workspace
  on command_requests (workspace_id) where state = 'running';
"""

_WORKSPACE_COLUMNS = (
    "id, class_id, root_path, display_name, root_device, root_inode, read_enabled, "
    "change_proposals_enabled, commands_enabled, created_at, updated_at"
)
_CHANGE_COLUMNS = (
    "id, workspace_id, session_id, relative_path, base_hash, base_content, proposed_content, "
    "file_device, file_inode, file_mode, newline, rationale, state, accepted_hunks_json, "
    "rejected_hunks_json, before_hash, after_hash, state_reason, created_at, updated_at"
)
_COMMAND_COLUMNS = (
    "id, workspace_id, session_id, argv_json, relative_cwd, reason, expected_signal, "
    "timeout_seconds, state, confirmed_at, started_at, finished_at, exit_code, stdout_text, "
    "stderr_text, state_reason, created_at, updated_at"
)

_WORKSPACE_CHANGE_TRANSITIONS: dict[str, set[str]] = {
    WORKSPACE_CHANGE_PENDING: {
        WORKSPACE_CHANGE_PARTIALLY_APPLIED,
        WORKSPACE_CHANGE_APPLIED,
        WORKSPACE_CHANGE_REJECTED,
        WORKSPACE_CHANGE_STALE,
        WORKSPACE_CHANGE_FAILED,
    },
    WORKSPACE_CHANGE_PARTIALLY_APPLIED: {
        WORKSPACE_CHANGE_PARTIALLY_APPLIED,
        WORKSPACE_CHANGE_APPLIED,
        WORKSPACE_CHANGE_REJECTED,
        WORKSPACE_CHANGE_STALE,
        WORKSPACE_CHANGE_FAILED,
    },
}
_COMMAND_TRANSITIONS: dict[str, set[str]] = {
    COMMAND_PENDING: {COMMAND_RUNNING, COMMAND_REJECTED, COMMAND_ABANDONED},
    COMMAND_RUNNING: {
        COMMAND_COMPLETED,
        COMMAND_FAILED,
        COMMAND_TIMED_OUT,
        COMMAND_ABANDONED,
    },
}


def _qualified(columns: str, alias: str) -> str:
    return ", ".join(f"{alias}.{column.strip()}" for column in columns.split(","))


def _require_text(value: str, field: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field} cannot be blank")
    return clean


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode_workspace(row: sqlite3.Row) -> dict[str, object]:
    workspace = dict(row)
    workspace["read_enabled"] = bool(workspace["read_enabled"])
    workspace["change_proposals_enabled"] = bool(workspace["change_proposals_enabled"])
    workspace["commands_enabled"] = bool(workspace["commands_enabled"])
    return workspace


def _decode_change(row: sqlite3.Row) -> dict[str, object]:
    change = dict(row)
    change["accepted_hunks"] = json.loads(str(change.pop("accepted_hunks_json")))
    change["rejected_hunks"] = json.loads(str(change.pop("rejected_hunks_json")))
    return change


def _decode_command(row: sqlite3.Row) -> dict[str, object]:
    command = dict(row)
    command["argv"] = json.loads(str(command.pop("argv_json")))
    return command


def _canonical_root(root_path: str) -> tuple[str, int, int]:
    target = workspace_paths.canonical_workspace_root(_require_text(root_path, "root_path"))
    stats = target.lstat()
    return str(target), int(stats.st_dev), int(stats.st_ino)


def _require_session_scope(
    conn: sqlite3.Connection, session_id: int, class_id: int
) -> dict[str, object]:
    session = sessions.get_session(conn, session_id)
    if int(session["class_id"]) != class_id:
        raise NotFoundError("That conversation does not exist in this class.")
    return session


def _bounded_optional_text(value: str | None, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    clean = value.strip()
    if len(clean) > maximum:
        raise ValueError(f"{field} cannot exceed {maximum} characters")
    return clean or None


def _truncate_utf8(value: str | None, maximum: int = MAX_OUTPUT_BYTES) -> str | None:
    if value is None:
        return None
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    return encoded[:maximum].decode("utf-8", errors="ignore")


def _truncate_command_output(
    stdout_text: str | None, stderr_text: str | None
) -> tuple[str | None, str | None]:
    stdout = _truncate_utf8(stdout_text)
    used = len((stdout or "").encode("utf-8"))
    stderr = _truncate_utf8(stderr_text, max(0, MAX_OUTPUT_BYTES - used))
    return stdout, stderr


def get_workspace_for_class(conn: sqlite3.Connection, class_id: int) -> dict[str, object] | None:
    classes.get_class(conn, class_id)
    row = conn.execute(
        f"select {_WORKSPACE_COLUMNS} from class_workspaces where class_id = ?",  # noqa: S608
        (class_id,),
    ).fetchone()
    return None if row is None else _decode_workspace(row)


def get_workspace(
    conn: sqlite3.Connection,
    workspace_id: int,
    *,
    class_id: int | None = None,
) -> dict[str, object]:
    values: tuple[object, ...] = (workspace_id,)
    sql = f"select {_WORKSPACE_COLUMNS} from class_workspaces where id = ?"  # noqa: S608
    if class_id is not None:
        classes.get_class(conn, class_id)
        sql += " and class_id = ?"
        values = (workspace_id, class_id)
    row = conn.execute(sql, values).fetchone()
    if row is None:
        raise NotFoundError("That workspace is not attached to this class.")
    return _decode_workspace(row)


def _invalidate_pending_changes(
    conn: sqlite3.Connection,
    workspace_id: int,
    *,
    reason: str,
) -> None:
    conn.execute(
        "update workspace_changes set state = ?, state_reason = ?, updated_at = "
        "strftime('%Y-%m-%dT%H:%M:%fZ', 'now') where workspace_id = ? and state in (?, ?)",
        (
            WORKSPACE_CHANGE_REJECTED,
            reason,
            workspace_id,
            WORKSPACE_CHANGE_PENDING,
            WORKSPACE_CHANGE_PARTIALLY_APPLIED,
        ),
    )


def _invalidate_pending_commands(
    conn: sqlite3.Connection,
    workspace_id: int,
    *,
    reason: str,
) -> None:
    conn.execute(
        "update command_requests set state = ?, state_reason = ?, finished_at = "
        "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), updated_at = "
        "strftime('%Y-%m-%dT%H:%M:%fZ', 'now') where workspace_id = ? and state = ?",
        (COMMAND_REJECTED, reason, workspace_id, COMMAND_PENDING),
    )


def attach_workspace(
    conn: sqlite3.Connection,
    class_id: int,
    *,
    root_path: str,
    display_name: str | None = None,
) -> dict[str, object]:
    """Attach or replace the one workspace root for a class."""
    classes.get_class(conn, class_id)
    canonical_root, device, inode = _canonical_root(root_path)
    label = (display_name or Path(canonical_root).name or canonical_root).strip()
    if not label:
        raise ValueError("display_name cannot be blank")
    existing = get_workspace_for_class(conn, class_id)
    try:
        conn.execute("begin immediate")
        if existing is None:
            cursor = conn.execute(
                "insert into class_workspaces (class_id, root_path, display_name, root_device, "
                "root_inode) values (?, ?, ?, ?, ?)",
                (class_id, canonical_root, label, device, inode),
            )
            workspace_id = int(cursor.lastrowid or 0)
        else:
            workspace_id = int(existing["id"])
            if (
                existing["root_path"] != canonical_root
                or int(existing["root_device"]) != device
                or int(existing["root_inode"]) != inode
            ):
                _invalidate_pending_changes(conn, workspace_id, reason="workspace_replaced")
                _invalidate_pending_commands(conn, workspace_id, reason="workspace_replaced")
                conn.execute(
                    "update class_workspaces set root_path = ?, display_name = ?, root_device = ?, "
                    "root_inode = ?, read_enabled = 0, change_proposals_enabled = 0, "
                    "commands_enabled = 0, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "where id = ?",
                    (canonical_root, label, device, inode, workspace_id),
                )
            else:
                conn.execute(
                    "update class_workspaces set display_name = ?, updated_at = "
                    "strftime('%Y-%m-%dT%H:%M:%fZ', 'now') where id = ?",
                    (label, workspace_id),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_workspace(conn, workspace_id, class_id=class_id)


def update_workspace_grants(
    conn: sqlite3.Connection,
    class_id: int,
    *,
    read_enabled: bool | None = None,
    change_proposals_enabled: bool | None = None,
    commands_enabled: bool | None = None,
) -> dict[str, object]:
    """Enable or revoke explicit workspace grants for one class."""
    workspace = get_workspace_for_class(conn, class_id)
    if workspace is None:
        raise NotFoundError("That class has no attached workspace.")
    next_read = workspace["read_enabled"] if read_enabled is None else bool(read_enabled)
    next_change = (
        workspace["change_proposals_enabled"]
        if change_proposals_enabled is None
        else bool(change_proposals_enabled)
    )
    next_commands = (
        workspace["commands_enabled"] if commands_enabled is None else bool(commands_enabled)
    )
    read_revoked = bool(workspace["read_enabled"]) and not next_read
    if read_revoked:
        next_change = False
    if next_change and not next_read:
        raise ValueError("Change proposals require workspace read.")
    change_revoked = bool(workspace["change_proposals_enabled"]) and not next_change
    commands_revoked = bool(workspace["commands_enabled"]) and not next_commands
    try:
        conn.execute("begin immediate")
        conn.execute(
            "update class_workspaces set read_enabled = ?, change_proposals_enabled = ?, "
            "commands_enabled = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "where id = ?",
            (int(next_read), int(next_change), int(next_commands), workspace["id"]),
        )
        if change_revoked:
            _invalidate_pending_changes(conn, int(workspace["id"]), reason="change_grant_revoked")
        if commands_revoked:
            _invalidate_pending_commands(
                conn, int(workspace["id"]), reason="commands_grant_revoked"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_workspace(conn, int(workspace["id"]), class_id=class_id)


def detach_workspace(conn: sqlite3.Connection, class_id: int) -> None:
    """Detach a class workspace and invalidate pending rows tied to that root."""
    workspace = get_workspace_for_class(conn, class_id)
    if workspace is None:
        raise NotFoundError("That class has no attached workspace.")
    workspace_id = int(workspace["id"])
    try:
        conn.execute("begin immediate")
        _invalidate_pending_changes(conn, workspace_id, reason="workspace_detached")
        _invalidate_pending_commands(conn, workspace_id, reason="workspace_detached")
        conn.execute("delete from class_workspaces where id = ?", (workspace_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def create_workspace_change(
    conn: sqlite3.Connection,
    class_id: int,
    *,
    workspace_id: int,
    session_id: int,
    relative_path: str,
    base_hash: str,
    base_content: str,
    proposed_content: str,
    file_device: int,
    file_inode: int,
    file_mode: int,
    newline: str | None,
    rationale: str | None,
    attempt_id: int | None = None,
) -> dict[str, object]:
    """Persist one inert file-change proposal for later user review."""
    workspace = get_workspace(conn, workspace_id, class_id=class_id)
    _require_session_scope(conn, session_id, class_id)
    if not workspace["change_proposals_enabled"]:
        raise ConflictError("Change proposals are not enabled for this workspace.")
    try:
        conn.execute("begin immediate")
        cursor = conn.execute(
            "insert into workspace_changes (workspace_id, session_id, relative_path, base_hash, "
            "base_content, proposed_content, file_device, file_inode, file_mode, newline, "
            "rationale, before_hash) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                session_id,
                _require_text(relative_path, "relative_path"),
                _require_text(base_hash, "base_hash"),
                base_content,
                proposed_content,
                file_device,
                file_inode,
                file_mode,
                newline,
                _bounded_optional_text(rationale, "rationale", MAX_REASON_CHARS),
                base_hash,
            ),
        )
        change_id = int(cursor.lastrowid or 0)
        agent_attempts.link_target(
            conn,
            attempt_id,
            target_kind="workspace_change",
            target_id=change_id,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_workspace_change(
        conn,
        change_id,
        class_id=class_id,
        workspace_id=workspace_id,
        session_id=session_id,
    )


def get_workspace_change(
    conn: sqlite3.Connection,
    change_id: int,
    *,
    class_id: int | None = None,
    workspace_id: int | None = None,
    session_id: int | None = None,
) -> dict[str, object]:
    sql = (
        f"select {_qualified(_CHANGE_COLUMNS, 'c')}, w.class_id from workspace_changes c "  # noqa: S608
        "join class_workspaces w on w.id = c.workspace_id where c.id = ?"
    )
    values: list[object] = [change_id]
    if class_id is not None:
        classes.get_class(conn, class_id)
        sql += " and w.class_id = ?"
        values.append(class_id)
    if workspace_id is not None:
        sql += " and c.workspace_id = ?"
        values.append(workspace_id)
    if session_id is not None:
        sql += " and c.session_id = ?"
        values.append(session_id)
    row = conn.execute(sql, tuple(values)).fetchone()
    if row is None:
        raise NotFoundError("That workspace change does not exist in this scope.")
    decoded = _decode_change(row)
    decoded.pop("class_id", None)
    return decoded


def list_workspace_changes(
    conn: sqlite3.Connection,
    *,
    class_id: int,
    workspace_id: int,
    session_id: int,
    limit: int = 100,
) -> list[dict[str, object]]:
    """List recent proposals in one exact class/workspace/session scope."""
    get_workspace(conn, workspace_id, class_id=class_id)
    _require_session_scope(conn, session_id, class_id)
    if limit < 1 or limit > 200:
        raise ValueError("Workspace change limit must be between 1 and 200")
    rows = conn.execute(
        f"select {_CHANGE_COLUMNS} from workspace_changes "  # noqa: S608
        "where workspace_id = ? and session_id = ? order by id desc limit ?",
        (workspace_id, session_id, limit),
    ).fetchall()
    return [_decode_change(row) for row in rows]


def transition_workspace_change(
    conn: sqlite3.Connection,
    change_id: int,
    *,
    class_id: int,
    workspace_id: int,
    session_id: int,
    state: str,
    accepted_hunks: list[int] | None = None,
    rejected_hunks: list[int] | None = None,
    after_hash: str | None = None,
    state_reason: str | None = None,
    base_hash: str | object = _UNSET,
    base_content: str | object = _UNSET,
    proposed_content: str | object = _UNSET,
    file_device: int | object = _UNSET,
    file_inode: int | object = _UNSET,
    file_mode: int | object = _UNSET,
    newline: str | None | object = _UNSET,
) -> dict[str, object]:
    """Advance one change proposal through its review lifecycle."""
    next_state = _require_text(state, "state")
    if next_state not in WORKSPACE_CHANGE_STATES:
        raise ValueError(f"Unknown workspace change state: {next_state}")
    current = get_workspace_change(
        conn,
        change_id,
        class_id=class_id,
        workspace_id=workspace_id,
        session_id=session_id,
    )
    allowed = _WORKSPACE_CHANGE_TRANSITIONS.get(str(current["state"]), set())
    if next_state not in allowed:
        raise ConflictError("That workspace change cannot move to the requested state.")
    refreshed = {
        "base_hash": base_hash,
        "base_content": base_content,
        "proposed_content": proposed_content,
        "file_device": file_device,
        "file_inode": file_inode,
        "file_mode": file_mode,
        "newline": newline,
    }
    supplied = {name: value for name, value in refreshed.items() if value is not _UNSET}
    if supplied and (next_state != WORKSPACE_CHANGE_PARTIALLY_APPLIED or len(supplied) != 7):
        raise ValueError("A partial apply must refresh the complete proposal snapshot.")
    assignments = [
        "state = ?",
        "accepted_hunks_json = ?",
        "rejected_hunks_json = ?",
        "after_hash = ?",
        "state_reason = ?",
    ]
    values: list[object] = [
        next_state,
        _json(accepted_hunks or []),
        _json(rejected_hunks or []),
        after_hash,
        state_reason,
    ]
    for column, value in supplied.items():
        assignments.append(f"{column} = ?")
        values.append(value)
    assignments.append("updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')")
    values.append(change_id)
    conn.execute(
        f"update workspace_changes set {', '.join(assignments)} where id = ?",  # noqa: S608
        tuple(values),
    )
    conn.commit()
    return get_workspace_change(
        conn,
        change_id,
        class_id=class_id,
        workspace_id=workspace_id,
        session_id=session_id,
    )


def create_command_request(
    conn: sqlite3.Connection,
    class_id: int,
    *,
    workspace_id: int,
    session_id: int,
    argv: list[str],
    relative_cwd: str,
    reason: str,
    expected_signal: str | None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    attempt_id: int | None = None,
) -> dict[str, object]:
    """Persist one exact, user-confirmable command request."""
    workspace = get_workspace(conn, workspace_id, class_id=class_id)
    _require_session_scope(conn, session_id, class_id)
    if not workspace["commands_enabled"]:
        raise ConflictError("Commands are not enabled for this workspace.")
    exact_argv = commands.validate_argv(argv)
    commands.validate_command_cwd(Path(str(workspace["root_path"])), relative_cwd)
    if timeout_seconds <= 0 or timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be between 1 and {MAX_TIMEOUT_SECONDS}")
    try:
        conn.execute("begin immediate")
        cursor = conn.execute(
            "insert into command_requests (workspace_id, session_id, argv_json, relative_cwd, "
            "reason, expected_signal, timeout_seconds) values (?, ?, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                session_id,
                _json(exact_argv),
                _require_text(relative_cwd, "relative_cwd"),
                _require_text(reason, "reason")[:MAX_REASON_CHARS],
                _bounded_optional_text(
                    expected_signal, "expected_signal", MAX_EXPECTED_SIGNAL_CHARS
                ),
                timeout_seconds,
            ),
        )
        request_id = int(cursor.lastrowid or 0)
        agent_attempts.link_target(
            conn,
            attempt_id,
            target_kind="command_request",
            target_id=request_id,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_command_request(
        conn,
        request_id,
        class_id=class_id,
        workspace_id=workspace_id,
        session_id=session_id,
    )


def get_command_request(
    conn: sqlite3.Connection,
    request_id: int,
    *,
    class_id: int | None = None,
    workspace_id: int | None = None,
    session_id: int | None = None,
) -> dict[str, object]:
    sql = (
        f"select {_qualified(_COMMAND_COLUMNS, 'r')}, w.class_id from command_requests r "  # noqa: S608
        "join class_workspaces w on w.id = r.workspace_id where r.id = ?"
    )
    values: list[object] = [request_id]
    if class_id is not None:
        classes.get_class(conn, class_id)
        sql += " and w.class_id = ?"
        values.append(class_id)
    if workspace_id is not None:
        sql += " and r.workspace_id = ?"
        values.append(workspace_id)
    if session_id is not None:
        sql += " and r.session_id = ?"
        values.append(session_id)
    row = conn.execute(sql, tuple(values)).fetchone()
    if row is None:
        raise NotFoundError("That command request does not exist in this scope.")
    decoded = _decode_command(row)
    decoded.pop("class_id", None)
    return decoded


def list_command_requests(
    conn: sqlite3.Connection,
    *,
    class_id: int,
    workspace_id: int,
    session_id: int,
    limit: int = 100,
) -> list[dict[str, object]]:
    """List recent command requests in one exact class/workspace/session scope."""
    get_workspace(conn, workspace_id, class_id=class_id)
    _require_session_scope(conn, session_id, class_id)
    if limit < 1 or limit > 200:
        raise ValueError("Command request limit must be between 1 and 200")
    rows = conn.execute(
        f"select {_COMMAND_COLUMNS} from command_requests "  # noqa: S608
        "where workspace_id = ? and session_id = ? order by id desc limit ?",
        (workspace_id, session_id, limit),
    ).fetchall()
    return [_decode_command(row) for row in rows]


def transition_command_request(
    conn: sqlite3.Connection,
    request_id: int,
    *,
    class_id: int,
    workspace_id: int,
    session_id: int,
    state: str,
    exit_code: int | None = None,
    stdout_text: str | None = None,
    stderr_text: str | None = None,
    state_reason: str | None = None,
) -> dict[str, object]:
    """Advance one command request through its confirmation/execution lifecycle."""
    next_state = _require_text(state, "state")
    if next_state not in COMMAND_STATES:
        raise ValueError(f"Unknown command state: {next_state}")
    current = get_command_request(
        conn,
        request_id,
        class_id=class_id,
        workspace_id=workspace_id,
        session_id=session_id,
    )
    allowed = _COMMAND_TRANSITIONS.get(str(current["state"]), set())
    if next_state not in allowed:
        raise ConflictError("That command request cannot move to the requested state.")
    confirmed_at = current["confirmed_at"]
    started_at = current["started_at"]
    finished_at = current["finished_at"]
    if next_state == COMMAND_RUNNING:
        confirmed_at = "now"
        started_at = "now"
        finished_at = None
    elif next_state in {COMMAND_COMPLETED, COMMAND_FAILED, COMMAND_TIMED_OUT, COMMAND_ABANDONED}:
        finished_at = "now"
    stdout_text, stderr_text = _truncate_command_output(stdout_text, stderr_text)
    try:
        conn.execute(
            "update command_requests set state = ?, confirmed_at = "
            "case when ? = 'now' then strftime('%Y-%m-%dT%H:%M:%fZ', 'now') else ? end, "
            "started_at = case when ? = 'now' then "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now') else ? end, "
            "finished_at = case when ? = 'now' then strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "else ? end, exit_code = ?, stdout_text = ?, stderr_text = ?, state_reason = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') where id = ?",
            (
                next_state,
                confirmed_at,
                confirmed_at,
                started_at,
                started_at,
                finished_at,
                finished_at,
                exit_code,
                stdout_text,
                stderr_text,
                state_reason,
                request_id,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ConflictError("That workspace already has an active command request.") from exc
    return get_command_request(
        conn,
        request_id,
        class_id=class_id,
        workspace_id=workspace_id,
        session_id=session_id,
    )


def reconcile_running_commands(
    conn: sqlite3.Connection, *, reason: str = "startup_reconcile"
) -> int:
    """Mark commands interrupted by a backend restart as abandoned."""
    cursor = conn.execute(
        "update command_requests set state = ?, state_reason = ?, finished_at = "
        "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), updated_at = "
        "strftime('%Y-%m-%dT%H:%M:%fZ', 'now') where state = ?",
        (COMMAND_ABANDONED, _require_text(reason, "reason"), COMMAND_RUNNING),
    )
    conn.commit()
    return int(cursor.rowcount)
