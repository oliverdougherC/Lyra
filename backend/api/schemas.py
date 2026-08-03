"""Shared request and response models.

Only the class models live here. Every other route module declares its own Pydantic
models beside the handlers that use them.
"""

from pydantic import BaseModel, Field, field_validator


def _clean_name(value: str) -> str:
    """Strip a class name and reject one that was only whitespace."""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Name cannot be blank.")
    return cleaned


class ClassCreate(BaseModel):
    """Body of `POST /api/classes`."""

    name: str = Field(min_length=1)
    code: str | None = None
    semester: str | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _clean_name(value)


class ClassUpdate(BaseModel):
    """Body of `PATCH /api/classes/{class_id}`. Absent fields are left alone.

    `code` and `semester` accept an explicit null, which clears them. `name` does not:
    the column is not nullable, so a null there is bad input rather than a clear.
    `archived` moves a class to (or back from) the archived section without deleting it.
    """

    name: str | None = None
    code: str | None = None
    semester: str | None = None
    archived: bool | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("Name cannot be blank.")
        return _clean_name(value)


class ClassRead(BaseModel):
    """A class workspace as the interface sees it."""

    id: int
    name: str
    code: str | None
    semester: str | None
    archived: bool = False
    document_count: int
    created_at: str
    last_active_at: str
