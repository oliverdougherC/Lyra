"""Durable run state for draft passes and reviews.

The artifact row remains the UI mirror, but that mirror is too small to survive a
restart: queue payload, cancellation intent, restart warnings, and safe-boundary
checkpoints all need a dedicated row. This module keeps that row intentionally narrow
and JSON-backed so the workers can evolve without schema churn.
"""

import json
import sqlite3
from collections.abc import Sequence

from backend.core.errors import ConflictError

PASS = "pass"  # noqa: S105 - a job kind, not a credential.
REVIEW = "review"
KINDS: tuple[str, ...] = (PASS, REVIEW)

QUEUED = "queued"
RUNNING = "running"
CANCEL_REQUESTED = "cancel_requested"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
STATUSES: tuple[str, ...] = (QUEUED, RUNNING, CANCEL_REQUESTED, COMPLETED, FAILED, CANCELLED)
ACTIVE_STATUSES: tuple[str, ...] = (QUEUED, RUNNING, CANCEL_REQUESTED)

RESTART_WARNING = "resumed_after_restart"
CHECKPOINT_MISMATCH_WARNING = "checkpoint_mismatch"
WEB_RESEARCH_DEGRADED_WARNING = "web_research_degraded"

ALREADY_RUNNING_MESSAGE = "This draft already has a run in flight."


def create_run(
    conn: sqlite3.Connection,
    artifact_id: int,
    job_kind: str,
    depth: str,
    *,
    request: dict[str, object] | None = None,
    started_at: str,
) -> dict[str, object]:
    """Create one queued run, failing with the route's conflict message on duplicates."""
    payload = json.dumps(request or {}, ensure_ascii=False)
    try:
        run_id = int(
            conn.execute(
                "insert into writer_runs (artifact_id, job_kind, depth, status, request_json, "
                "started_at) values (?, ?, ?, ?, ?, ?)",
                (artifact_id, job_kind, depth, QUEUED, payload, started_at),
            ).lastrowid
            or 0
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(ALREADY_RUNNING_MESSAGE) from exc
    return get_run(conn, run_id)


def get_run(conn: sqlite3.Connection, run_id: int) -> dict[str, object]:
    row = conn.execute("select * from writer_runs where id = ?", (run_id,)).fetchone()
    if row is None:
        raise LookupError(f"Writer run {run_id} does not exist.")
    return _row_dict(row)


def latest_run(conn: sqlite3.Connection, artifact_id: int) -> dict[str, object] | None:
    row = conn.execute(
        "select * from writer_runs where artifact_id = ? order by id desc limit 1",
        (artifact_id,),
    ).fetchone()
    return _row_dict(row) if row is not None else None


def active_run(conn: sqlite3.Connection, artifact_id: int) -> dict[str, object] | None:
    row = conn.execute(
        "select * from writer_runs where artifact_id = ? and status in (?, ?, ?) "
        "order by id desc limit 1",
        (artifact_id, *ACTIVE_STATUSES),
    ).fetchone()
    return _row_dict(row) if row is not None else None


def recoverable_runs(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        "select * from writer_runs where status in (?, ?, ?) order by id",
        ACTIVE_STATUSES,
    ).fetchall()
    return [_row_dict(row) for row in rows]


def request_cancel(conn: sqlite3.Connection, artifact_id: int) -> dict[str, object] | None:
    run = active_run(conn, artifact_id)
    if run is None:
        return None
    if run["status"] == CANCEL_REQUESTED:
        return run
    conn.execute(
        "update writer_runs set status = ?, cancel_requested_at = datetime('now'), "
        "updated_at = datetime('now') where id = ? and status in (?, ?)",
        (CANCEL_REQUESTED, int(run["id"]), QUEUED, RUNNING),
    )
    conn.commit()
    return get_run(conn, int(run["id"]))


def mark_running(conn: sqlite3.Connection, run_id: int) -> dict[str, object]:
    return _set_status(conn, run_id, RUNNING)


def checkpoint(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    stage: str,
    index: int = 0,
    targets: Sequence[str] = (),
    data: dict[str, object] | None = None,
) -> dict[str, object]:
    current = get_run(conn, run_id).get("checkpoint")
    payload = dict(current) if isinstance(current, dict) else {}
    payload.update({"stage": stage, "index": max(0, index), "targets": list(targets)})
    if data is not None:
        payload["data"] = data
    conn.execute(
        "update writer_runs set checkpoint_json = ?, updated_at = datetime('now') where id = ?",
        (json.dumps(payload, ensure_ascii=False), run_id),
    )
    conn.commit()
    return payload


def add_warning(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    code: str,
    message: str,
    replace: bool = False,
) -> list[dict[str, str]]:
    run = get_run(conn, run_id)
    warnings = list(run["warnings"])
    if replace:
        warnings = [warning for warning in warnings if warning.get("code") != code]
    if not any(
        warning.get("code") == code and warning.get("message") == message for warning in warnings
    ):
        warnings.append({"code": code, "message": message})
    _write_warnings(conn, run_id, warnings)
    return warnings


def clear_warning(conn: sqlite3.Connection, run_id: int, code: str) -> list[dict[str, str]]:
    run = get_run(conn, run_id)
    warnings = [warning for warning in run["warnings"] if warning.get("code") != code]
    _write_warnings(conn, run_id, warnings)
    return warnings


def mark_completed(conn: sqlite3.Connection, run_id: int) -> None:
    _finish(conn, run_id, COMPLETED)


def mark_failed(conn: sqlite3.Connection, run_id: int, message: str) -> None:
    _finish(conn, run_id, FAILED, message=message)


def settle_failure(conn: sqlite3.Connection, run_id: int, detail: str, message: str) -> bool:
    """Fail the owning run and its mirrors in one transaction.

    Releasing the active-run slot separately from the artifact update lets a late
    callback overwrite the next run. Terminal/superseded callbacks are no-ops.
    """
    try:
        cursor = conn.execute(
            "update writer_runs set status = ?, error_message = ?, "
            "finished_at = datetime('now'), updated_at = datetime('now') "
            "where id = ? and status in (?, ?) and not exists "
            "(select 1 from writer_runs successor where "
            "successor.artifact_id = writer_runs.artifact_id and successor.id > writer_runs.id)",
            (FAILED, message, run_id, QUEUED, RUNNING),
        )
        if not cursor.rowcount:
            conn.rollback()
            return False
        run = get_run(conn, run_id)
        conn.execute(
            "update artifacts set state = ?, stage_detail = ?, error_message = ?, "
            "writer_job_completed_at = datetime('now'), updated_at = datetime('now') "
            "where id = ?",
            (FAILED, detail, message, run["artifact_id"]),
        )
        conn.execute(
            "update live_draft_suggestions set status = ?, detail = ?, "
            "version = version + 1, updated_at = datetime('now') where run_id = ?",
            (FAILED, message, run_id),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def mark_cancelled(conn: sqlite3.Connection, run_id: int) -> None:
    _finish(conn, run_id, CANCELLED)


def queue_for_restart(conn: sqlite3.Connection, run_id: int, message: str) -> dict[str, object]:
    run = get_run(conn, run_id)
    add_warning(conn, run_id, code=RESTART_WARNING, message=message, replace=True)
    next_status = CANCEL_REQUESTED if run["status"] == CANCEL_REQUESTED else QUEUED
    return _set_status(conn, run_id, next_status)


def cancel_requested(conn: sqlite3.Connection, run_id: int | None) -> bool:
    if run_id is None:
        return False
    return get_run(conn, run_id)["status"] in (CANCEL_REQUESTED, CANCELLED)


def settle_cancellation(conn: sqlite3.Connection, run_id: int | None, detail: str) -> bool:
    """Settle and mirror one cancelled run atomically without touching a newer run.

    The first conditional write acquires SQLite's writer lock before a terminal run
    frees the active-run slot. A new run can queue only after every mirror is settled.
    Repeated callbacks may repair this run's own mirror, never a successor's artifact.
    """
    if run_id is None or not cancel_requested(conn, run_id):
        return False
    try:
        conn.execute(
            "update writer_runs set status = ?, error_message = null, "
            "finished_at = datetime('now'), updated_at = datetime('now') "
            "where id = ? and status = ?",
            (CANCELLED, run_id, CANCEL_REQUESTED),
        )
        run = get_run(conn, run_id)
        cancelled = run["status"] == CANCELLED
        if cancelled:
            conn.execute(
                "update artifacts set state = ?, stage_detail = ?, "
                "writer_job_completed_at = datetime('now'), updated_at = datetime('now') "
                "where id = ? and not exists (select 1 from writer_runs "
                "where artifact_id = ? and id > ?) "
                "and (state != ? or stage_detail is not ? or writer_job_completed_at is null)",
                (
                    CANCELLED,
                    detail,
                    run["artifact_id"],
                    run["artifact_id"],
                    run_id,
                    CANCELLED,
                    detail,
                ),
            )
            conn.execute(
                "update live_draft_suggestions set status = ?, detail = ?, "
                "version = version + 1, updated_at = datetime('now') "
                "where run_id = ? and status != ?",
                (CANCELLED, detail, run_id, CANCELLED),
            )
        conn.commit()
        return cancelled
    except Exception:
        conn.rollback()
        raise


def compatible_index(
    run: dict[str, object] | None,
    *,
    stage: str,
    targets: Sequence[str],
) -> int:
    if run is None:
        return 0
    checkpoint_payload = run.get("checkpoint")
    if not isinstance(checkpoint_payload, dict):
        return 0
    if checkpoint_payload.get("stage") != stage:
        return 0
    saved_targets = checkpoint_payload.get("targets")
    if not isinstance(saved_targets, list):
        return 0
    current = list(targets)
    if saved_targets[: len(current)] != current[: len(saved_targets)] and saved_targets != current:
        return -1
    try:
        return max(0, min(int(checkpoint_payload.get("index") or 0), len(current)))
    except (TypeError, ValueError):
        return 0


def build_job(run: dict[str, object]) -> object:
    """Recreate one queued job from its durable payload."""
    from backend.core import review_pipeline, writer_pipeline, writer_tools

    payload = dict(run["request"])
    artifact_id = int(run["artifact_id"])
    if run["job_kind"] == PASS:
        return writer_tools._compatible_job(
            writer_pipeline.PassJob,
            artifact_id,
            run_id=int(run["id"]),
            instruction=payload.get("instruction"),
            section_refs=tuple(payload.get("section_refs") or ()),
            depth=str(run["depth"]),
            pause_at_plan=bool(payload.get("pause_at_plan", False)),
            address_comment_id=payload.get("address_comment_id"),
        )
    if run["job_kind"] == REVIEW:
        return writer_tools._compatible_job(
            review_pipeline.ReviewJob,
            artifact_id,
            run_id=int(run["id"]),
            depth=str(run["depth"]),
        )
    raise ValueError(f"Unknown writer run kind: {run['job_kind']!r}")


def _finish(
    conn: sqlite3.Connection, run_id: int, status: str, *, message: str | None = None
) -> None:
    conn.execute(
        "update writer_runs set status = case when status = ? then ? else ? end, "
        "error_message = case when status = ? then null else ? end, "
        "finished_at = datetime('now'), updated_at = datetime('now') "
        "where id = ? and status in (?, ?, ?)",
        (CANCEL_REQUESTED, CANCELLED, status, CANCEL_REQUESTED, message, run_id, *ACTIVE_STATUSES),
    )
    conn.commit()


def _set_status(conn: sqlite3.Connection, run_id: int, status: str) -> dict[str, object]:
    conn.execute(
        "update writer_runs set status = ?, updated_at = datetime('now') "
        "where id = ? and status in (?, ?)",
        (status, run_id, QUEUED, RUNNING),
    )
    conn.commit()
    return get_run(conn, run_id)


def _write_warnings(conn: sqlite3.Connection, run_id: int, warnings: list[dict[str, str]]) -> None:
    conn.execute(
        "update writer_runs set warnings_json = ?, updated_at = datetime('now') where id = ?",
        (json.dumps(warnings, ensure_ascii=False), run_id),
    )
    conn.commit()


def _row_dict(row: sqlite3.Row) -> dict[str, object]:
    data = dict(row)
    data["request"] = _load_json(data.pop("request_json"), default={})
    data["checkpoint"] = _load_json(data.pop("checkpoint_json"), default=None)
    data["warnings"] = _load_json(data.pop("warnings_json"), default=[])
    return data


def _load_json(value: object, *, default: object) -> object:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default
