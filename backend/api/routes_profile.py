"""Class profile and global user profile endpoints.

Every route returns the whole profile rather than the one fact it touched, because the
interface renders the profile as a list and a partial response would leave it guessing.

Handlers are sync `def`: `sqlite3` blocks, and FastAPI runs sync handlers in a threadpool,
which is exactly where blocking work belongs.
"""

import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from backend.core import profiles
from backend.core.classes import get_class
from backend.core.errors import NotFoundError
from backend.storage.database import get_db

router = APIRouter(prefix="/api", tags=["profile"])

DbConn = Annotated[sqlite3.Connection, Depends(get_db)]

FactKind = Literal["deadline", "topic", "grading", "professor", "prerequisite", "note"]
Confidence = Literal["high", "low"]

# Keep in step with `profiles.KNOWN_SKIP_REASONS`, which is what fills this field.
ExtractionSkipReason = Literal[
    "extraction_disabled", "no_endpoint", "remote_unacknowledged", "unparseable_response"
]


class FactRead(BaseModel):
    """One profile fact. `class_id` is null for a fact about the student themselves."""

    id: int
    class_id: int | None
    kind: FactKind
    label: str
    value: str
    confidence: Confidence
    confirmed: bool
    rejected: bool
    source_document_id: int | None
    source_filename: str | None
    created_at: str


class ClassProfileRead(BaseModel):
    """A class profile, plus why its most recent ingestion extracted nothing."""

    facts: list[FactRead]
    extraction_skipped_reason: ExtractionSkipReason | None


class UserProfileRead(BaseModel):
    """The global user profile. Nothing is extracted into it, so it has no skip reason."""

    facts: list[FactRead]


class FactValueUpdate(BaseModel):
    """Body of both PATCH routes: the user's correction to one fact's value."""

    fact_id: int
    value: str = Field(min_length=1)

    @field_validator("value")
    @classmethod
    def _check_value(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Value cannot be blank.")
        return cleaned


class FactResolution(BaseModel):
    """Body of the confirm route. Rejecting keeps the row; it never deletes it."""

    fact_id: int
    action: Literal["confirm", "reject"]


def _require_class_fact(conn: sqlite3.Connection, class_id: int, fact_id: int) -> None:
    """Refuse a fact id that belongs to another class, or to no class at all.

    The message is the one `get_fact` raises for an id that does not exist, deliberately:
    a route scoped to one class has no business confirming that a fact elsewhere exists.
    """
    if profiles.get_fact(conn, fact_id)["class_id"] != class_id:
        raise NotFoundError("That fact does not exist.")


def _require_user_fact(conn: sqlite3.Connection, fact_id: int) -> None:
    """Refuse a class fact reached through the global profile route, for the same reason."""
    if profiles.get_fact(conn, fact_id)["class_id"] is not None:
        raise NotFoundError("That fact does not exist.")


@router.get("/classes/{class_id}/profile", response_model=ClassProfileRead)
def read_class_profile(class_id: int, conn: DbConn) -> dict[str, object]:
    # An unknown class has no facts and no skip reason, which is indistinguishable from
    # an empty profile. Look the class up so it answers 404 rather than empty.
    get_class(conn, class_id)
    return profiles.get_class_profile(conn, class_id)


@router.patch("/classes/{class_id}/profile", response_model=ClassProfileRead)
def correct_class_fact(class_id: int, payload: FactValueUpdate, conn: DbConn) -> dict[str, object]:
    _require_class_fact(conn, class_id, payload.fact_id)
    profiles.update_fact_value(conn, payload.fact_id, payload.value)
    return profiles.get_class_profile(conn, class_id)


@router.post("/classes/{class_id}/profile/confirm", response_model=ClassProfileRead)
def resolve_class_fact(class_id: int, payload: FactResolution, conn: DbConn) -> dict[str, object]:
    _require_class_fact(conn, class_id, payload.fact_id)
    if payload.action == "confirm":
        profiles.confirm_fact(conn, payload.fact_id)
    else:
        profiles.reject_fact(conn, payload.fact_id)
    return profiles.get_class_profile(conn, class_id)


@router.get("/profile", response_model=UserProfileRead)
def read_user_profile(conn: DbConn) -> dict[str, object]:
    return profiles.get_user_profile(conn)


@router.patch("/profile", response_model=UserProfileRead)
def correct_user_fact(payload: FactValueUpdate, conn: DbConn) -> dict[str, object]:
    _require_user_fact(conn, payload.fact_id)
    profiles.update_fact_value(conn, payload.fact_id, payload.value)
    return profiles.get_user_profile(conn)
