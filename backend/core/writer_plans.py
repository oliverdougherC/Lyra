"""Versioned plans: the writer's durable brief, thesis, map, and section intent.

The active plan is editable while a pass is working. Replanning creates a new version in
one transaction and deactivates the old row; inactive versions are never mutated.
"""

import json
import sqlite3
from collections.abc import Mapping, Sequence

from backend.core import artifacts
from backend.core.errors import NotFoundError

_PLAN_COLUMNS = (
    "id, artifact_id, version, active, brief_analysis, thesis, argument_map, created_at, updated_at"
)
_SECTION_COLUMNS = (
    "id, plan_id, section_ref, ordinal, title, job, claim, evidence, source_ids, "
    "word_budget, research_notes, created_at, updated_at"
)
_SECTION_FIELDS = frozenset(
    {
        "ordinal",
        "title",
        "job",
        "claim",
        "evidence",
        "source_ids",
        "word_budget",
        "research_notes",
    }
)


def _require_draft(conn: sqlite3.Connection, artifact_id: int) -> None:
    artifact = artifacts.get_artifact(conn, artifact_id)
    if artifact["kind"] != artifacts.KIND_DRAFT:
        raise NotFoundError("That draft does not exist.")


def _structured(value: object, field: str, expected: type) -> object:
    """Validate a JSON structure supplied either decoded or as JSON text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} must be valid JSON") from exc
    if not isinstance(value, expected):
        raise ValueError(f"{field} must be a JSON {expected.__name__}")
    return value


def _json(value: object, field: str, expected: type) -> str:
    return json.dumps(
        _structured(value, field, expected), ensure_ascii=False, separators=(",", ":")
    )


def _section_payload(section: Mapping[str, object], fallback_ordinal: int) -> tuple[object, ...]:
    section_ref = str(section.get("section_ref", "")).strip()
    if not section_ref:
        raise ValueError("Every plan section needs a section_ref")
    ordinal = section.get("ordinal", fallback_ordinal)
    word_budget = section.get("word_budget", 0)
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise ValueError("section ordinal must be a non-negative integer")
    if isinstance(word_budget, bool) or not isinstance(word_budget, int) or word_budget < 0:
        raise ValueError("section word_budget must be a non-negative integer")
    evidence = _json(section.get("evidence", []), "section evidence", list)
    source_ids_value = _structured(section.get("source_ids", []), "section source_ids", list)
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in source_ids_value
    ):
        raise ValueError("section source_ids must contain positive integers")
    source_ids = json.dumps(source_ids_value, separators=(",", ":"))
    return (
        section_ref,
        ordinal,
        str(section.get("title", "")).strip(),
        str(section.get("job", "")).strip(),
        str(section.get("claim", "")).strip(),
        evidence,
        source_ids,
        word_budget,
        str(section.get("research_notes", "")).strip(),
    )


def _decode_section(row: sqlite3.Row) -> dict[str, object]:
    section = dict(row)
    section["evidence"] = json.loads(str(section["evidence"]))
    section["source_ids"] = json.loads(str(section["source_ids"]))
    return section


def _decode_plan(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, object]:
    plan = dict(row)
    plan["active"] = bool(plan["active"])
    plan["argument_map"] = json.loads(str(plan["argument_map"]))
    section_rows = conn.execute(
        f"select {_SECTION_COLUMNS} from draft_plan_sections "  # noqa: S608
        "where plan_id = ? order by ordinal, id",
        (plan["id"],),
    )
    plan["sections"] = [_decode_section(section) for section in section_rows]
    return plan


def get_plan(conn: sqlite3.Connection, plan_id: int) -> dict[str, object]:
    """Read one plan with decoded argument map and ordered section rows."""
    row = conn.execute(
        f"select {_PLAN_COLUMNS} from draft_plans where id = ?",  # noqa: S608
        (plan_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("That draft plan does not exist.")
    return _decode_plan(conn, row)


def get_active_plan(conn: sqlite3.Connection, artifact_id: int) -> dict[str, object] | None:
    """Read a draft's active plan, or None before planning has begun."""
    _require_draft(conn, artifact_id)
    row = conn.execute(
        f"select {_PLAN_COLUMNS} from draft_plans "  # noqa: S608
        "where artifact_id = ? and active = 1",
        (artifact_id,),
    ).fetchone()
    return _decode_plan(conn, row) if row is not None else None


def list_plan_versions(conn: sqlite3.Connection, artifact_id: int) -> list[dict[str, object]]:
    """All plan headers newest first; sections stay out of this history listing."""
    _require_draft(conn, artifact_id)
    rows = conn.execute(
        f"select {_PLAN_COLUMNS} from draft_plans "  # noqa: S608
        "where artifact_id = ? order by version desc",
        (artifact_id,),
    )
    result: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        item["active"] = bool(item["active"])
        item["argument_map"] = json.loads(str(item["argument_map"]))
        result.append(item)
    return result


def create_plan(
    conn: sqlite3.Connection,
    artifact_id: int,
    *,
    brief_analysis: str = "",
    thesis: str = "",
    argument_map: list[object] | dict[str, object] | str | None = None,
    sections: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """Create and activate the next plan version for a draft.

    This is also the primitive used by ``new_plan_version``. Existing active content is
    not copied here; callers that mean "edit by replanning" use that function instead.
    """
    _require_draft(conn, artifact_id)
    map_value: object = [] if argument_map is None else argument_map
    if isinstance(map_value, str):
        try:
            map_value = json.loads(map_value)
        except json.JSONDecodeError as exc:
            raise ValueError("argument_map must be valid JSON") from exc
    if not isinstance(map_value, (list, dict)):
        raise ValueError("argument_map must be a JSON list or object")
    map_json = json.dumps(map_value, ensure_ascii=False, separators=(",", ":"))

    # Validate section payloads before deactivating the current plan. A malformed new
    # version must leave the last good plan active.
    prepared = [_section_payload(section, ordinal) for ordinal, section in enumerate(sections)]
    if len({payload[0] for payload in prepared}) != len(prepared):
        raise ValueError("Plan section_ref values must be unique")
    if len({payload[1] for payload in prepared}) != len(prepared):
        raise ValueError("Plan section ordinal values must be unique")

    try:
        conn.execute("begin immediate")
        row = conn.execute(
            "select coalesce(max(version), 0) + 1 from draft_plans where artifact_id = ?",
            (artifact_id,),
        ).fetchone()
        version = int(row[0])
        conn.execute(
            "update draft_plans set active = 0 where artifact_id = ? and active = 1",
            (artifact_id,),
        )
        cursor = conn.execute(
            "insert into draft_plans "
            "(artifact_id, version, active, brief_analysis, thesis, argument_map) "
            "values (?, ?, 1, ?, ?, ?)",
            (artifact_id, version, brief_analysis.strip(), thesis.strip(), map_json),
        )
        plan_id = int(cursor.lastrowid or 0)
        conn.executemany(
            "insert into draft_plan_sections "
            "(plan_id, section_ref, ordinal, title, job, claim, evidence, source_ids, "
            " word_budget, research_notes) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ((plan_id, *payload) for payload in prepared),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_plan(conn, plan_id)


def update_plan(
    conn: sqlite3.Connection,
    plan_id: int,
    *,
    brief_analysis: str | None = None,
    thesis: str | None = None,
    argument_map: list[object] | dict[str, object] | str | None = None,
    sections: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Patch the active version; inactive history is immutable."""
    current = get_plan(conn, plan_id)
    if not current["active"]:
        raise ValueError("Historical draft plans are read-only")
    fields: dict[str, object] = {}
    if brief_analysis is not None:
        fields["brief_analysis"] = brief_analysis.strip()
    if thesis is not None:
        fields["thesis"] = thesis.strip()
    if argument_map is not None:
        map_value: object = argument_map
        if isinstance(map_value, str):
            try:
                map_value = json.loads(map_value)
            except json.JSONDecodeError as exc:
                raise ValueError("argument_map must be valid JSON") from exc
        if not isinstance(map_value, (list, dict)):
            raise ValueError("argument_map must be a JSON list or object")
        fields["argument_map"] = json.dumps(map_value, ensure_ascii=False, separators=(",", ":"))
    prepared = None
    if sections is not None:
        prepared = [_section_payload(section, ordinal) for ordinal, section in enumerate(sections)]
        if len({payload[0] for payload in prepared}) != len(prepared):
            raise ValueError("Plan section_ref values must be unique")
        if len({payload[1] for payload in prepared}) != len(prepared):
            raise ValueError("Plan section ordinal values must be unique")
    try:
        conn.execute("begin immediate")
        if fields:
            assignments = ", ".join(f"{column} = ?" for column in fields)
            conn.execute(
                f"update draft_plans set {assignments}, updated_at = datetime('now') "  # noqa: S608
                "where id = ? and active = 1",
                (*fields.values(), plan_id),
            )
        if prepared is not None:
            conn.execute("delete from draft_plan_sections where plan_id = ?", (plan_id,))
            conn.executemany(
                "insert into draft_plan_sections "
                "(plan_id, section_ref, ordinal, title, job, claim, evidence, source_ids, "
                " word_budget, research_notes) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ((plan_id, *payload) for payload in prepared),
            )
            conn.execute(
                "update draft_plans set updated_at = datetime('now') where id = ?", (plan_id,)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_plan(conn, plan_id)


def update_plan_section(
    conn: sqlite3.Connection,
    plan_id: int,
    section_ref: str,
    **fields: object,
) -> dict[str, object]:
    """Patch one active section, useful for research notes and student steering."""
    plan = get_plan(conn, plan_id)
    if not plan["active"]:
        raise ValueError("Historical draft plans are read-only")
    unknown = set(fields) - _SECTION_FIELDS
    if unknown:
        raise ValueError(f"Unknown plan section field(s): {', '.join(sorted(unknown))}")
    if not fields:
        for section in plan["sections"]:
            if section["section_ref"] == section_ref:
                return section
        raise NotFoundError("That plan section does not exist.")

    normalized: dict[str, object] = {}
    for key, value in fields.items():
        if key == "evidence":
            normalized[key] = _json(value, "section evidence", list)
        elif key == "source_ids":
            decoded = _structured(value, "section source_ids", list)
            if any(
                isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in decoded
            ):
                raise ValueError("section source_ids must contain positive integers")
            normalized[key] = json.dumps(decoded, separators=(",", ":"))
        elif key in ("ordinal", "word_budget"):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"section {key} must be a non-negative integer")
            normalized[key] = value
        else:
            normalized[key] = str(value).strip()
    assignments = ", ".join(f"{column} = ?" for column in normalized)
    try:
        cursor = conn.execute(
            f"update draft_plan_sections set {assignments}, updated_at = datetime('now') "  # noqa: S608
            "where plan_id = ? and section_ref = ?",
            (*normalized.values(), plan_id, section_ref),
        )
        if cursor.rowcount == 0:
            raise NotFoundError("That plan section does not exist.")
        conn.execute("update draft_plans set updated_at = datetime('now') where id = ?", (plan_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    updated = get_plan(conn, plan_id)
    return next(section for section in updated["sections"] if section["section_ref"] == section_ref)


def new_plan_version(
    conn: sqlite3.Connection,
    artifact_id: int,
    *,
    brief_analysis: str | None = None,
    thesis: str | None = None,
    argument_map: list[object] | dict[str, object] | str | None = None,
    sections: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Clone the active plan into a new version, applying supplied replacements."""
    current = get_active_plan(conn, artifact_id)
    if current is None:
        return create_plan(
            conn,
            artifact_id,
            brief_analysis=brief_analysis or "",
            thesis=thesis or "",
            argument_map=argument_map,
            sections=sections or (),
        )
    cloned_sections = sections
    if cloned_sections is None:
        cloned_sections = [
            {
                key: value
                for key, value in section.items()
                if key in _SECTION_FIELDS or key == "section_ref"
            }
            for section in current["sections"]
        ]
    return create_plan(
        conn,
        artifact_id,
        brief_analysis=(
            str(current["brief_analysis"]) if brief_analysis is None else brief_analysis
        ),
        thesis=str(current["thesis"]) if thesis is None else thesis,
        argument_map=current["argument_map"] if argument_map is None else argument_map,
        sections=cloned_sections,
    )


# Compact aliases used by pipeline callers.
read_plan = get_plan
list_versions = list_plan_versions
new_version = new_plan_version
update_section = update_plan_section
