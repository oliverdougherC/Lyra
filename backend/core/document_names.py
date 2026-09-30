"""Display names for source documents; original filenames remain immutable identity data."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping

from backend.core.errors import UnprocessableError

MAX_NICKNAME_CHARS = 120


def normalize_nickname(value: str | None) -> str | None:
    """Blank resets a nickname; controls and formatting characters cannot become labels."""
    if value is None:
        return None
    nickname = value.strip()
    if not nickname:
        return None
    if len(nickname) > MAX_NICKNAME_CHARS or any(
        unicodedata.category(char).startswith("C") or char in "\u2028\u2029" for char in nickname
    ):
        raise UnprocessableError(
            "Use a nickname of at most 120 characters without control characters."
        )
    return nickname


def display_name(row: Mapping[str, object]) -> str:
    nickname = row["nickname"]
    return str(nickname) if nickname and str(nickname).strip() else str(row["filename"])
