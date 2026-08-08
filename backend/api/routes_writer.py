"""Persistent writer artifacts that are not document-body operations.

The main draft router owns writing, review, comments, and export. This router keeps the
versioned plan, class source ledger, and per-class capability overrides small enough to
reason about independently while preserving the same `/api` resource layout.
"""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from backend.core import source_ledger, writer_budgets, writer_plans
from backend.storage.database import get_db

router = APIRouter(prefix="/api", tags=["writer"])
DbConn = Annotated[sqlite3.Connection, Depends(get_db)]


class PlanSectionWrite(BaseModel):
    id: int | None = None
    section_ref: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    title: str = ""
    job: str = ""
    claim: str = ""
    evidence: list[str] = Field(default_factory=list)
    sources: list[int] = Field(default_factory=list)
    word_budget: int | None = Field(default=None, ge=0)
    research_notes: str = ""

    @field_validator("section_ref")
    @classmethod
    def clean_ref(cls, value: str) -> str:
        return value.strip()


class PlanWrite(BaseModel):
    brief_analysis: str = ""
    thesis: str = ""
    argument_map: list[dict[str, object]] = Field(default_factory=list)
    sections: list[PlanSectionWrite] = Field(default_factory=list)

    @field_validator("argument_map", mode="before")
    @classmethod
    def adapt_legacy_argument_map(cls, value: object) -> object:
        return _argument_map_list(value)


def _argument_map_list(value: object) -> list[dict[str, object]]:
    """The public list contract, adapting the planner's former `{claims: [...]}` row."""
    if isinstance(value, list):
        return [dict(entry) for entry in value if isinstance(entry, dict)]
    if isinstance(value, dict):
        claims = value.get("claims")
        if isinstance(claims, list):
            return [dict(entry) for entry in claims if isinstance(entry, dict)]
        return [dict(value)] if value else []
    return []


def _section_payload(section: PlanSectionWrite) -> dict[str, object]:
    return {
        "section_ref": section.section_ref,
        "ordinal": section.ordinal,
        "title": section.title,
        "job": section.job,
        "claim": section.claim,
        "evidence": section.evidence,
        "source_ids": section.sources,
        "word_budget": section.word_budget or 0,
        "research_notes": section.research_notes,
    }


def _plan_response(plan: dict[str, object] | None) -> dict[str, object] | None:
    if plan is None:
        return None
    result = dict(plan)
    result["status"] = "active" if result.pop("active", True) else "historical"
    result["brief_analysis"] = str(result.get("brief_analysis") or "")
    result["argument_map"] = _argument_map_list(result.get("argument_map"))
    sections = []
    for raw in result.get("sections", []):
        section = dict(raw)
        section["sources"] = section.pop("source_ids", [])
        section["word_budget"] = int(section.get("word_budget") or 0) or None
        sections.append(section)
    result["sections"] = sections
    return result


@router.get("/drafts/{artifact_id}/plan", response_model=None)
def read_plan(artifact_id: int, conn: DbConn) -> dict[str, object] | None:
    return _plan_response(writer_plans.get_active_plan(conn, artifact_id))


@router.put("/drafts/{artifact_id}/plan", response_model=None)
def write_plan(artifact_id: int, payload: PlanWrite, conn: DbConn) -> dict[str, object]:
    sections = [_section_payload(section) for section in payload.sections]
    plan = writer_plans.new_plan_version(
        conn,
        artifact_id,
        brief_analysis=payload.brief_analysis,
        thesis=payload.thesis,
        argument_map=payload.argument_map,
        sections=sections,
    )
    response = _plan_response(plan)
    if response is None:  # create/new-version always returns a row; defensive contract guard.
        raise RuntimeError("The saved writer plan could not be read back.")
    return response


def _sync_course_sources(conn: sqlite3.Connection, class_id: int) -> None:
    """Make every ready course document visible through the same ledger as the web."""
    rows = conn.execute(
        "select id, filename from documents where class_id = ? and state = 'ready' order by id",
        (class_id,),
    ).fetchall()
    for row in rows:
        source_ledger.upsert_source(
            conn,
            class_id,
            source_type=source_ledger.COURSE,
            document_id=int(row["id"]),
            title=str(row["filename"]),
        )


@router.get("/classes/{class_id}/sources", response_model=None)
def read_sources(class_id: int, conn: DbConn) -> list[dict[str, object]]:
    _sync_course_sources(conn, class_id)
    return source_ledger.list_sources(conn, class_id)


class CapabilityOverridesWrite(BaseModel):
    allow_web_research: bool | None = None
    parallel_requests: bool | None = None
    parallel_concurrency: int | None = Field(default=None, ge=1)


def _capability_response(conn: sqlite3.Connection, class_id: int) -> dict[str, object]:
    overrides = writer_budgets.get_class_capability_overrides(conn, class_id)
    effective = writer_budgets.get_writer_capabilities(conn, class_id)
    return {
        "overrides": overrides,
        "effective": {
            "allow_web_research": effective.allow_web_research,
            "parallel_requests": effective.parallel_requests,
            "parallel_concurrency": effective.parallel_concurrency,
        },
    }


@router.get("/classes/{class_id}/writer-settings", response_model=None)
def read_writer_settings(class_id: int, conn: DbConn) -> dict[str, object]:
    return _capability_response(conn, class_id)


@router.put("/classes/{class_id}/writer-settings", response_model=None)
def write_writer_settings(
    class_id: int, payload: CapabilityOverridesWrite, conn: DbConn
) -> dict[str, object]:
    writer_budgets.update_class_capability_overrides(
        conn,
        class_id,
        **payload.model_dump(exclude_unset=True),
    )
    return _capability_response(conn, class_id)
