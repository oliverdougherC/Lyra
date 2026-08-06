"""Suggestion-mode pending edits: whole-document AI revisions, reviewed hunk by hunk.

One pending edit per draft body part (migration 017). The base and proposed contents are
stored as full blobs; the review hunks are derived at read time and never stored. Every
accept writes the draft body through `artifacts.set_part_content`, so the existing
revision history and restore UI are the undo story.

The hunk math is `difflib` with a two-line context and no fuzz anywhere: a hunk applies
exactly where its context matches or not at all. The hash on each hunk guards
accept/reject races: the client echoes `{index, hash}`, and a mismatch means the hunk set
moved under it.

Coordinates are 0-based line offsets into the base (old side) or proposed (new side)
content. They are an implementation detail of the server; the client renders `lines` and
echoes `{index, hash}`.
"""

import difflib
import hashlib
import sqlite3
from dataclasses import dataclass

from backend.core import artifacts
from backend.core.errors import ConflictError, NotFoundError

NO_SUCH_EDIT_MESSAGE = "That suggestion does not exist."
STALE_ACCEPT_MESSAGE = (
    "The suggestion is stale: review it side by side, or accept with force to replace the document."
)
STALE_REJECT_MESSAGE = (
    "The suggestion is stale and its hunks no longer anchor. Reject it, or review side by side."
)
HUNK_RACE_MESSAGE = "That hunk changed since it was fetched. Re-fetch the suggestion."

# Two lines of context, matching the reference implementation: enough to anchor a hunk
# uniquely in a draft, little enough that nearby edits stay separate hunks.
_CONTEXT_LINES = 2


@dataclass(frozen=True)
class Hunk:
    """One review unit of a base/proposed diff.

    Attributes:
        index: Position in the hunk list, which is what the client echoes back.
        old_start: First line of the hunk in the base content, 0-based.
        old_lines: How many base lines the hunk spans.
        new_start: First line of the hunk in the proposed content, 0-based.
        new_lines: How many proposed lines the hunk spans.
        lines: Unified-diff style, each prefixed ' ', '-', or '+', line endings kept.
        hash: sha256 over the display lines (prefix plus stripped text). A hash mismatch
            on accept or reject means the hunk set moved under the client.
    """

    index: int
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    lines: list[str]
    hash: str

    def to_dict(self) -> dict[str, object]:
        """The REST shape: display lines carry no line endings."""
        return {
            "index": self.index,
            "old_start": self.old_start,
            "old_lines": self.old_lines,
            "new_start": self.new_start,
            "new_lines": self.new_lines,
            "lines": [_display(line) for line in self.lines],
            "hash": self.hash,
        }


def _display(line: str) -> str:
    """A hunk line without its line ending, for the interface."""
    return line.removesuffix("\n").removesuffix("\r")


def _hash(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(_display(line) for line in lines).encode()).hexdigest()


def compute_hunks(base: str, proposed: str) -> list[Hunk]:
    """Derive the review hunks of a base/proposed pair, context two lines.

    Autojunk is off: a draft repeats lines (blank lines, headings) far past difflib's
    popularity heuristic, and skipping them would cut hunks differently than the hashes
    the client already holds.
    """
    base_lines = base.splitlines(keepends=True)
    proposed_lines = proposed.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, base_lines, proposed_lines, autojunk=False)

    hunks: list[Hunk] = []
    for index, group in enumerate(matcher.get_grouped_opcodes(_CONTEXT_LINES)):
        old_start = group[0][1]
        old_end = group[-1][2]
        new_start = group[0][3]
        new_end = group[-1][4]
        lines: list[str] = []
        for tag, a1, a2, b1, b2 in group:
            if tag == "equal":
                lines.extend(" " + line for line in base_lines[a1:a2])
            if tag in ("delete", "replace"):
                lines.extend("-" + line for line in base_lines[a1:a2])
            if tag in ("insert", "replace"):
                lines.extend("+" + line for line in proposed_lines[b1:b2])
        hunks.append(
            Hunk(
                index=index,
                old_start=old_start,
                old_lines=old_end - old_start,
                new_start=new_start,
                new_lines=new_end - new_start,
                lines=lines,
                hash=_hash(lines),
            )
        )
    return hunks


def apply_hunk(content: str, hunk: Hunk) -> str | None:
    """Apply ONE hunk to base-side content at its old-side coordinates, exact context.

    Returns the patched content, or None when the context does not match - no fuzz.
    """
    expected = [line[1:] for line in hunk.lines if line[:1] in (" ", "-")]
    replacement = [line[1:] for line in hunk.lines if line[:1] in (" ", "+")]
    lines = content.splitlines(keepends=True)
    if lines[hunk.old_start : hunk.old_start + hunk.old_lines] != expected:
        return None
    return "".join(lines[: hunk.old_start] + replacement + lines[hunk.old_start + hunk.old_lines :])


def unapply_hunk(content: str, hunk: Hunk) -> str | None:
    """Remove ONE hunk from proposed-side content at its new-side coordinates.

    The per-hunk reject primitive. Returns the content, or None when it does not apply.
    """
    expected = [line[1:] for line in hunk.lines if line[:1] in (" ", "+")]
    replacement = [line[1:] for line in hunk.lines if line[:1] in (" ", "-")]
    lines = content.splitlines(keepends=True)
    if lines[hunk.new_start : hunk.new_start + hunk.new_lines] != expected:
        return None
    return "".join(lines[: hunk.new_start] + replacement + lines[hunk.new_start + hunk.new_lines :])


def apply_patch(content: str, base: str, proposed: str) -> str | None:
    """Apply the whole diff(base, proposed) to `content`, exact context, hunk by hunk.

    Used only by staleness refresh: rebasing a pending edit onto a document the user
    edited while it waited. Coordinates shift by each applied hunk's line delta, so the
    hunks apply in order against the content as it becomes.
    """
    offset = 0
    for hunk in compute_hunks(base, proposed):
        shifted = Hunk(
            index=hunk.index,
            old_start=hunk.old_start + offset,
            old_lines=hunk.old_lines,
            new_start=hunk.new_start,
            new_lines=hunk.new_lines,
            lines=hunk.lines,
            hash=hunk.hash,
        )
        patched = apply_hunk(content, shifted)
        if patched is None:
            return None
        content = patched
        offset += hunk.new_lines - hunk.old_lines
    return content


@dataclass(frozen=True)
class PendingEdit:
    """A pending_edits row as a value."""

    id: int
    part_id: int
    base_content: str
    base_hash: str
    proposed_content: str
    stale: bool
    note: str | None

    def to_dict(self) -> dict[str, object]:
        """The REST shape. Stale edits carry the base too, for the side-by-side view."""
        hunks = compute_hunks(self.base_content, self.proposed_content)
        edit: dict[str, object] = {
            "id": self.id,
            "stale": self.stale,
            "note": self.note,
            "hunks": [hunk.to_dict() for hunk in hunks],
            "proposed_content": self.proposed_content,
        }
        if self.stale:
            edit["base_content"] = self.base_content
        return edit


def _row_to_edit(row: sqlite3.Row) -> PendingEdit:
    return PendingEdit(
        id=int(row["id"]),
        part_id=int(row["part_id"]),
        base_content=str(row["base_content"]),
        base_hash=str(row["base_hash"]),
        proposed_content=str(row["proposed_content"]),
        stale=bool(row["stale"]),
        note=row["note"],
    )


def _get(conn: sqlite3.Connection, edit_id: int) -> PendingEdit:
    row = conn.execute("select * from pending_edits where id = ?", (edit_id,)).fetchone()
    if row is None:
        raise NotFoundError(NO_SUCH_EDIT_MESSAGE)
    return _row_to_edit(row)


def _get_by_part(conn: sqlite3.Connection, part_id: int) -> PendingEdit | None:
    row = conn.execute("select * from pending_edits where part_id = ?", (part_id,)).fetchone()
    return _row_to_edit(row) if row is not None else None


def _store(conn: sqlite3.Connection, edit: PendingEdit) -> None:
    conn.execute(
        "update pending_edits set base_content = ?, base_hash = ?, proposed_content = ?, "
        "stale = ?, note = ?, updated_at = datetime('now') where id = ?",
        (
            edit.base_content,
            edit.base_hash,
            edit.proposed_content,
            int(edit.stale),
            edit.note,
            edit.id,
        ),
    )
    conn.commit()


def _delete(conn: sqlite3.Connection, edit_id: int) -> None:
    conn.execute("delete from pending_edits where id = ?", (edit_id,))
    conn.commit()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def refresh(
    conn: sqlite3.Connection, edit: PendingEdit, current_content: str
) -> PendingEdit | None:
    """Reconcile a stored edit with what the draft body says right now.

    Run on every read and before every accept or reject:

    - The body is untouched (its hash matches base): the edit is fresh, unchanged.
    - The body already reads as the proposal: the user made the proposed change
      themselves, so the edit has resolved itself and is deleted.
    - Otherwise try to rebase the diff onto the edited body. Clean: the base becomes the
      current content and the proposal follows it. The diff no longer applies: the edit
      is stale, its blobs untouched.
    """
    if _sha256(current_content) == edit.base_hash:
        return edit
    if current_content == edit.proposed_content:
        _delete(conn, edit.id)
        return None
    patched = apply_patch(current_content, edit.base_content, edit.proposed_content)
    if patched is None:
        if not edit.stale:
            edit = PendingEdit(**{**vars(edit), "stale": True})
            _store(conn, edit)
        return edit
    if patched == current_content:
        _delete(conn, edit.id)
        return None
    rebased = PendingEdit(
        **{
            **vars(edit),
            "base_content": current_content,
            "base_hash": _sha256(current_content),
            "proposed_content": patched,
            "stale": False,
        }
    )
    _store(conn, rebased)
    return rebased


def pending_for_part(conn: sqlite3.Connection, part_id: int) -> dict[str, object] | None:
    """The pending edit for one draft body in REST shape, refreshed against the body.

    None when nothing is pending - including when the refresh found the edit resolved
    itself and deleted it.
    """
    part = artifacts.get_part(conn, part_id)
    edit = _get_by_part(conn, part_id)
    if edit is None:
        return None
    refreshed = refresh(conn, edit, str(part["content"]))
    return refreshed.to_dict() if refreshed is not None else None


def propose(
    conn: sqlite3.Connection, part_id: int, proposed_content: str, note: str | None
) -> PendingEdit | None:
    """Create or coalesce the pending edit for one draft body.

    The base is the part's content at FIRST proposal; a later proposal replaces the
    proposed content and the instruction only - the stored base survives, which is what
    makes sequential AI passes coherent. An empty diff creates nothing: proposing the
    current content is not a suggestion.
    """
    part = artifacts.get_part(conn, part_id)
    current = str(part["content"])
    existing = _get_by_part(conn, part_id)

    if existing is None:
        if proposed_content == current:
            return None
        conn.execute(
            "insert into pending_edits "
            "(part_id, base_content, base_hash, proposed_content, note) "
            "values (?, ?, ?, ?, ?)",
            (part_id, current, _sha256(current), proposed_content, note),
        )
        conn.commit()
        return _get_by_part(conn, part_id)

    if proposed_content == existing.base_content:
        # The new proposal reads exactly as the stored base: nothing left to suggest.
        _delete(conn, existing.id)
        return None
    coalesced = PendingEdit(
        **{**vars(existing), "proposed_content": proposed_content, "note": note}
    )
    _store(conn, coalesced)
    return coalesced


def _revision_note(note: str | None) -> str:
    """The instruction the student gave, on the revision their accept writes."""
    return f"Accepted suggestion: {note}" if note else "Accepted suggestion"


def accept(
    conn: sqlite3.Connection,
    edit_id: int,
    hunk: dict[str, object] | None = None,
    force: bool = False,
) -> dict[str, object]:
    """Accept a pending edit: all of it, one hunk, or force-replace on a stale row.

    Every accept writes the draft body through `set_part_content`, so each one is a
    normal revision with the instruction as its note.

    Raises:
        ConflictError: on a stale edit without force, and on hunk races.
    """
    edit = _get(conn, edit_id)
    part = artifacts.get_part(conn, edit.part_id)
    refreshed = refresh(conn, edit, str(part["content"]))
    if refreshed is None:
        return {"remaining": 0}

    if refreshed.stale and not force:
        raise ConflictError(STALE_ACCEPT_MESSAGE)

    if hunk is not None and not refreshed.stale:
        hunks = compute_hunks(refreshed.base_content, refreshed.proposed_content)
        index = int(hunk.get("index", -1))
        target = hunks[index] if 0 <= index < len(hunks) else None
        if target is None or target.hash != hunk.get("hash"):
            raise ConflictError(HUNK_RACE_MESSAGE)
        new_base = apply_hunk(refreshed.base_content, target)
        if new_base is None:
            raise ConflictError(HUNK_RACE_MESSAGE)
        artifacts.set_part_content(
            conn,
            refreshed.part_id,
            new_base,
            origin=artifacts.GENERATED,
            note=_revision_note(refreshed.note),
        )
        remaining = compute_hunks(new_base, refreshed.proposed_content)
        if not remaining:
            _delete(conn, refreshed.id)
            return {"remaining": 0}
        kept = PendingEdit(
            **{
                **vars(refreshed),
                "base_content": new_base,
                "base_hash": _sha256(new_base),
            }
        )
        _store(conn, kept)
        return {"remaining": len(remaining), "edit": kept.to_dict()}

    # Accept all, including force on a stale row: the proposal becomes the document.
    artifacts.set_part_content(
        conn,
        refreshed.part_id,
        refreshed.proposed_content,
        origin=artifacts.GENERATED,
        note=_revision_note(refreshed.note),
    )
    _delete(conn, refreshed.id)
    return {"remaining": 0}


def reject(
    conn: sqlite3.Connection, edit_id: int, hunk: dict[str, object] | None = None
) -> dict[str, object]:
    """Reject a pending edit: all of it, or one hunk out of the proposal.

    Never writes the draft body: a reject says the document stays as it is.

    Raises:
        ConflictError: on a hunk reject against a stale edit, and on hunk races.
    """
    edit = _get(conn, edit_id)
    part = artifacts.get_part(conn, edit.part_id)
    refreshed = refresh(conn, edit, str(part["content"]))
    if refreshed is None:
        return {"remaining": 0}

    if hunk is None:
        _delete(conn, refreshed.id)
        return {"remaining": 0}

    if refreshed.stale:
        raise ConflictError(STALE_REJECT_MESSAGE)
    hunks = compute_hunks(refreshed.base_content, refreshed.proposed_content)
    index = int(hunk.get("index", -1))
    target = hunks[index] if 0 <= index < len(hunks) else None
    if target is None or target.hash != hunk.get("hash"):
        raise ConflictError(HUNK_RACE_MESSAGE)
    new_proposed = unapply_hunk(refreshed.proposed_content, target)
    if new_proposed is None:
        raise ConflictError(HUNK_RACE_MESSAGE)
    remaining = compute_hunks(refreshed.base_content, new_proposed)
    if not remaining:
        _delete(conn, refreshed.id)
        return {"remaining": 0}
    kept = PendingEdit(**{**vars(refreshed), "proposed_content": new_proposed})
    _store(conn, kept)
    return {"remaining": len(remaining), "edit": kept.to_dict()}
