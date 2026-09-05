"""Server-side query guard for bounded public web search.

The guard accepts only a short, normalized search string and refuses inputs that
look like bulk private material, local paths, credentials, or other obviously
unsafe payloads. It intentionally does not log the proposed query or any private
context. The remaining limitation is explicit: semantic paraphrase cannot be
detected reliably, so this module only blocks verbatim and pattern-shaped leaks.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from math import log2

MAX_QUERY_TERMS = 12
MAX_QUERY_CHARS = 500
MIN_OVERLAP_WORDS = 6
MIN_OVERLAP_CHARS = 24
MIN_QUOTED_WORDS = 5
MIN_QUOTED_CHARS = 32

# Bounds for the run-local private-context ledger: each piece of private material a run
# exposes is chunked for overlap checks rather than kept whole, and the ledger stops
# growing once the caps are hit. The caps keep the per-query comparison work bounded
# (each guard call normalizes every stored chunk) without so few characters that an
# overlap check cannot see across a chunk boundary of an ordinary file or document.
PRIVATE_CONTEXT_CHUNK_CHARS = 8_000
PRIVATE_CONTEXT_MAX_ITEMS = 128
SEMANTIC_LIMITATION = (
    "This guard blocks verbatim and obviously secret material, but it cannot detect "
    "semantic paraphrase or inferred disclosures."
)

_URL_RE = re.compile(r"(?i)(?:https?|file)://\S+|\bwww\.[^\s/$.?#].[^\s]*")
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_UNIX_PATH_RE = re.compile(r"(?<![\w:])(?:~|/)[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")
_REL_PATH_RE = re.compile(r"(?<!\w)\.\.?/[^\s]+")
_WINDOWS_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\(?:[^\\\s]+\\?)+")
_QUOTED_RE = re.compile(
    r'"([^"\n]{1,500})"|“([^”\n]{1,500})”|\'([^\'\n]{1,500})\'|‘([^’\n]{1,500})’'
)
_SECRET_VALUE_RE = re.compile(
    r"(?ix)"
    r"\b(?:"
    r"sk-[a-z0-9]{12,}|"
    r"gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"AIza[0-9A-Za-z_-]{20,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"bearer\s+[A-Za-z0-9._~+/=-]{16,}|"
    r"(?:api(?:_|-|\s)?key|token|secret|password)\s*[:=]\s*[^\s]{8,}"
    r")\b"
)
_SECRETISH_TOKEN_RE = re.compile(r"^[A-Za-z0-9_+=/-]{20,}$")
_COMPARE_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class SafeQuery:
    query: str
    limitation: str = SEMANTIC_LIMITATION


@dataclass(frozen=True, slots=True)
class QueryRefusal:
    code: str
    message: str
    limitation: str = SEMANTIC_LIMITATION


QueryGuardResult = SafeQuery | QueryRefusal


class PrivateContextLedger:
    """Private text already exposed to one run, chunked for the guard's overlap checks.

    A run-local accumulator for the material the guard must recognize: everything private
    that a turn has shown the model *up to this point*. A turn that combines private
    conversation, retrieved document chunks, active profile facts, and workspace file
    contents read mid-turn with a public web search must guard the search against the
    union of all of it - not only the tuple frozen before the tools ran. Callers seed the
    ledger with the private material present before tool execution, and the tools that
    return private text add their bounded results as they return them; `snapshot` is read
    at each `search_web` dispatch, so later reads are visible to later searches in the same
    turn.

    The ledger is process-local and run-scoped: raw private text is never persisted here or
    through the guard - the guard only ever compares it in memory, and the audit records
    hashed projections of tool arguments, never the context itself.

    Additions are chunked (``PRIVATE_CONTEXT_CHUNK_CHARS``) and deduplicated, so a large
    file costs a bounded, fixed number of entries no matter how many times it is read, and
    growth stops at ``PRIVATE_CONTEXT_MAX_ITEMS``.
    """

    def __init__(self, *values: object) -> None:
        self._items: list[str] = []
        self._seen: set[str] = set()
        self.add(*values)

    def add(self, *values: object) -> None:
        """Fold one or more values (str, dict, list, or None) into the ledger."""
        for value in values:
            self._add_one(value)

    def snapshot(self) -> tuple[str, ...]:
        """The ledger as the guard's `private_context`, current at the moment of the read."""
        return tuple(self._items)

    def _add_one(self, value: object) -> None:
        if value is None:
            return
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        else:
            text = str(value).strip()
        if not text:
            return
        for start in range(0, len(text), PRIVATE_CONTEXT_CHUNK_CHARS):
            if len(self._items) >= PRIVATE_CONTEXT_MAX_ITEMS:
                return
            chunk = text[start : start + PRIVATE_CONTEXT_CHUNK_CHARS].strip()
            if not chunk or chunk in self._seen:
                continue
            self._seen.add(chunk)
            self._items.append(chunk)


def guard_web_query(
    proposed_query: str, *, private_context: Sequence[str] = ()
) -> QueryGuardResult:
    """Return a normalized query or a typed refusal.

    The caller is expected to resolve capability policy before this runs and to
    audit only the returned code/message or normalized query, never the raw
    private context.
    """
    clean = _normalize_visible_text(proposed_query)
    if not clean:
        return QueryRefusal("empty_query", "The search query is empty after normalization.")
    if len(clean) > MAX_QUERY_CHARS:
        return QueryRefusal(
            "query_too_long",
            f"The search query exceeds {MAX_QUERY_CHARS} characters after normalization.",
        )
    if len(clean.split()) > MAX_QUERY_TERMS:
        return QueryRefusal(
            "too_many_terms",
            f"The search query exceeds {MAX_QUERY_TERMS} terms after normalization.",
        )
    if _URL_RE.search(clean):
        return QueryRefusal("contains_url", "The search query may not contain URLs.")
    if _EMAIL_RE.search(clean):
        return QueryRefusal("contains_email", "The search query may not contain email addresses.")
    if _UNIX_PATH_RE.search(clean) or _REL_PATH_RE.search(clean) or _WINDOWS_PATH_RE.search(clean):
        return QueryRefusal("contains_path", "The search query may not contain local file paths.")
    if _SECRET_VALUE_RE.search(clean):
        return QueryRefusal(
            "contains_secret_pattern",
            "The search query may not contain secrets, credentials, or token-shaped values.",
        )
    if _contains_high_entropy_token(clean):
        return QueryRefusal(
            "contains_high_entropy_token",
            "The search query may not contain long high-entropy tokens.",
        )
    if _contains_long_quoted_passage(clean):
        return QueryRefusal(
            "contains_quoted_passage",
            "The search query may not contain long quoted passages.",
        )
    if _has_significant_private_overlap(clean, private_context):
        return QueryRefusal(
            "overlaps_private_context",
            "The search query overlaps too closely with private context available to this turn.",
        )
    return SafeQuery(clean)


def _normalize_visible_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    without_controls = "".join(
        " " if unicodedata.category(char).startswith("C") else char for char in normalized
    )
    return " ".join(without_controls.split())


def _normalize_for_comparison(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens = _COMPARE_TOKEN_RE.findall(normalized)
    return " ".join(tokens)


def _contains_long_quoted_passage(text: str) -> bool:
    for match in _QUOTED_RE.finditer(text):
        fragment = next(group for group in match.groups() if group is not None)
        stripped = _normalize_visible_text(fragment)
        if len(stripped) >= MIN_QUOTED_CHARS or len(stripped.split()) >= MIN_QUOTED_WORDS:
            return True
    return False


def _contains_high_entropy_token(text: str) -> bool:
    for raw_token in text.split():
        token = raw_token.strip("()[]{}<>,;:.!?")
        if not _SECRETISH_TOKEN_RE.fullmatch(token):
            continue
        if _shannon_entropy(token) >= 3.5:
            return True
    return False


def _shannon_entropy(text: str) -> float:
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    length = len(text)
    return -sum((count / length) * log2(count / length) for count in counts.values())


def _has_significant_private_overlap(query: str, private_context: Sequence[str]) -> bool:
    normalized_query = _normalize_for_comparison(query)
    if len(normalized_query) < MIN_OVERLAP_CHARS:
        return False
    query_tokens = normalized_query.split()
    if len(query_tokens) < MIN_OVERLAP_WORDS:
        return False
    windows = [
        " ".join(query_tokens[index : index + MIN_OVERLAP_WORDS])
        for index in range(len(query_tokens) - MIN_OVERLAP_WORDS + 1)
    ]
    for context in private_context:
        normalized_context = _normalize_for_comparison(context)
        if len(normalized_context) < MIN_OVERLAP_CHARS:
            continue
        if normalized_query in normalized_context:
            return True
        if any(
            window in normalized_context for window in windows if len(window) >= MIN_OVERLAP_CHARS
        ):
            return True
    return False
