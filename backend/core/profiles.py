"""Profile facts: extraction from a document, the active-fact filter, and confirmation.

An extracted fact is a proposal, not an assertion. `docs/rag-pipeline.md` sets the rule this
module exists to hold: a `high` confidence fact becomes active context immediately, a `low`
one is stored but stays out of every prompt until the user confirms it. `select_active_facts`
is the single place that rule is written as SQL, and `backend.llm.prompts` deliberately does
not filter again.

Extraction is the one path that can send document text to a remote endpoint, so
`extract_facts` asks `extraction_allowed` before it does anything else at all.
"""

import asyncio
import json
import logging
import sqlite3
from collections.abc import Mapping

from backend.core.app_settings import (
    EXTRACTION_DISABLED,
    NO_ENDPOINT,
    REMOTE_UNACKNOWLEDGED,
    extraction_allowed,
    resolve_tutor_config,
)
from backend.core.errors import NotFoundError
from backend.llm import client
from backend.llm.prompts import build_extraction_prompt
from backend.rag.tokens import CHARS_PER_TOKEN

logger = logging.getLogger(__name__)

UNPARSEABLE_RESPONSE = "unparseable_response"

# Every reason a document can carry in `stage_detail` instead of having been extracted.
# Three come from the settings rules; the fourth is the model's own fault.
KNOWN_SKIP_REASONS = (
    EXTRACTION_DISABLED,
    NO_ENDPOINT,
    REMOTE_UNACKNOWLEDGED,
    UNPARSEABLE_RESPONSE,
)

# Share of the tutor context window one extraction pass may spend on document text. The
# rest is left for the prompt itself and for the JSON the model has to write back.
EXTRACTION_BUDGET_SHARE = 0.6

# The six keys the extraction prompt asks for, mapped onto the `kind` column.
_PAYLOAD_KINDS: dict[str, str] = {
    "deadlines": "deadline",
    "topics": "topic",
    "grading": "grading",
    "professor_info": "professor",
    "prerequisites": "prerequisite",
    "notes": "note",
}

# Label for an item the model gave no label of its own, which is what a bare string is.
_DEFAULT_LABELS: dict[str, str] = {
    "deadline": "Deadline",
    "topic": "Topic",
    "grading": "Grading",
    "professor": "Professor",
    "prerequisite": "Prerequisite",
    "note": "Note",
}

_LABEL_KEYS = ("label", "name", "title")
_VALUE_KEYS = ("value", "date", "description")
_CONFIDENCE_KEY = "confidence"
_CONFIDENCE_VALUES = frozenset({"high", "low"})

# An unmarked fact is not a trusted fact. Defaulting to `low` keeps it out of prompts
# until a person has looked at it.
_DEFAULT_CONFIDENCE = "low"

# One statement serves both scopes. SQLite's `is` compares like `=` for a non-null
# parameter and like `is null` for a null one, so the filter itself is written once.
_SELECT_ACTIVE_FACTS = """
select * from profile_facts
where class_id is ? and rejected = 0 and (confirmed = 1 or confidence = 'high')
order by kind, id
"""

_SELECT_PROFILE_FACTS = """
select f.id, f.class_id, f.kind, f.label, f.value, f.confidence, f.confirmed, f.rejected,
       f.source_document_id, f.created_at, d.filename as source_filename
from profile_facts f
left join documents d on d.id = f.source_document_id
where f.class_id is ? and f.rejected = 0
order by f.kind, f.id
"""

# The class profile explains only the latest ingestion. An older skipped upload must not
# obscure a later successful extraction.
_SELECT_SKIP_REASON = """
select stage_detail from documents
where class_id = ?
order by id desc
limit 1
"""

_INSERT_FACT = """
insert into profile_facts (class_id, kind, label, value, confidence, source_document_id)
values (?, ?, ?, ?, ?, ?)
"""


def _scalar_text(value: object) -> str:
    """Render one JSON scalar as fact text. Anything else renders empty."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int | float):
        return str(value)
    return ""


def _as_text(value: object) -> str:
    """Render a JSON value as fact text, flattening a list of scalars onto one line."""
    if isinstance(value, list):
        rendered = [_scalar_text(item) for item in value]
        return ", ".join(item for item in rendered if item)
    return _scalar_text(value)


def _first_text(source: Mapping[str, object], keys: tuple[str, ...]) -> str:
    """The first of `keys` that renders non-empty text, or an empty string."""
    for key in keys:
        text = _as_text(source.get(key))
        if text:
            return text
    return ""


def _confidence_of(source: Mapping[str, object]) -> str:
    """The model's own confidence marking, normalised. Unmarked or unrecognised is `low`."""
    raw = source.get(_CONFIDENCE_KEY)
    if isinstance(raw, str) and raw.strip().lower() in _CONFIDENCE_VALUES:
        return raw.strip().lower()
    return _DEFAULT_CONFIDENCE


def _read_object(item: Mapping[str, object], fallback_label: str) -> list[tuple[str, str, str]]:
    """Read one JSON object as `(label, value, confidence)` triples.

    An object is read as a single labelled item when it carries one of the recognised
    value keys, and otherwise as a mapping whose own keys are the labels. That is what
    resolves the ambiguity: `{"name": "Dr Chen"}` has no value key, so reading it as an
    item would leave the value empty and throw the fact away, while reading it as a
    mapping keeps it, along with any sibling keys the model added.

    Presence decides the reading, not emptiness. An item whose value key is there but
    blank is an item with nothing in it, so it is dropped rather than re-read as a
    mapping of its own label key.

    `confidence` is metadata in both readings, so it never becomes a fact of its own.
    """
    confidence = _confidence_of(item)

    if any(key in item for key in _VALUE_KEYS):
        value = _first_text(item, _VALUE_KEYS)
        if not value:
            return []
        return [(_first_text(item, _LABEL_KEYS) or fallback_label, value, confidence)]

    facts: list[tuple[str, str, str]] = []
    for key, raw in item.items():
        if key == _CONFIDENCE_KEY:
            continue
        if isinstance(raw, Mapping):
            facts.extend(_read_object(raw, str(key)))
            continue
        text = _as_text(raw)
        if text:
            facts.append((str(key), text, confidence))
    return facts


def _read_section(section: object, kind: str) -> list[tuple[str, str, str]]:
    """Read one payload key into `(label, value, confidence)` triples.

    Every shape a model has been seen to answer with is accepted for every key, because
    the prompt's `deadlines[]` and `grading{}` notation is a request, not a guarantee: a
    list of strings, a list of objects, a single object, or a bare string.
    """
    fallback_label = _DEFAULT_LABELS[kind]

    if isinstance(section, Mapping):
        return _read_object(section, fallback_label)

    if isinstance(section, list):
        facts: list[tuple[str, str, str]] = []
        for item in section:
            if isinstance(item, Mapping):
                facts.extend(_read_object(item, fallback_label))
                continue
            text = _as_text(item)
            if text:
                # A bare string carries no marking of its own, so it is low by default.
                facts.append((fallback_label, text, _DEFAULT_CONFIDENCE))
        return facts

    text = _as_text(section)
    return [(fallback_label, text, _DEFAULT_CONFIDENCE)] if text else []


def _strip_code_fence(content: str) -> str:
    """Remove one wrapping markdown fence, tagged (```json) or bare (```)."""
    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_payload(content: str) -> dict[str, object] | None:
    """Parse the model's reply into a JSON object, or None when it is unusable.

    A bad reply is an expected outcome rather than a fault, so nothing raises here. The
    log lines carry no document text and no endpoint.
    """
    try:
        payload = json.loads(_strip_code_fence(content))
    except ValueError:
        logger.warning("Profile extraction returned a reply that is not JSON")
        return None
    if not isinstance(payload, dict):
        logger.warning("Profile extraction returned JSON that is not an object")
        return None
    return payload


def _get_document(conn: sqlite3.Connection, document_id: int) -> sqlite3.Row:
    """The document being extracted, for its `class_id`."""
    row = conn.execute("select id, class_id from documents where id = ?", (document_id,)).fetchone()
    if row is None:
        raise NotFoundError("That document does not exist.")
    return row


def _store_facts(
    conn: sqlite3.Connection, document: sqlite3.Row, payload: Mapping[str, object]
) -> None:
    """Insert the payload's facts, skipping anything this document has proposed before."""
    # Scoped to the one document, so `source_document_id` is already accounted for and
    # the remaining three columns are the identity. Rejected rows are in here too: they
    # are exactly what stops a rejected fact coming back on the next ingestion.
    seen = {
        (row["kind"], row["label"], row["value"])
        for row in conn.execute(
            "select kind, label, value from profile_facts where source_document_id = ?",
            (document["id"],),
        )
    }

    rows: list[tuple[object, ...]] = []
    for key, kind in _PAYLOAD_KINDS.items():
        for label, value, confidence in _read_section(payload.get(key), kind):
            identity = (kind, label, value)
            if identity in seen:
                continue
            seen.add(identity)
            rows.append((document["class_id"], kind, label, value, confidence, document["id"]))

    if not rows:
        return
    conn.executemany(_INSERT_FACT, rows)
    conn.commit()


def extract_facts(conn: sqlite3.Connection, document_id: int, text: str) -> str | None:
    """Propose profile facts for one document from its text.

    The permission check runs first, before the text is read, truncated, or built into a
    prompt, so there is no path on which document text reaches a remote endpoint the user
    has not acknowledged.

    Args:
        conn: Open database connection.
        document_id: Document the facts are proposed from and attributed to.
        text: Full document text. Truncated here to the extraction budget.

    Returns:
        None when facts were extracted and stored, otherwise the reason nothing was: one
        of `KNOWN_SKIP_REASONS`, which the ingestion job records in `stage_detail`.

    Raises:
        UpstreamError: The tutor endpoint failed. This is deliberately not caught here.
            The ingestion step wraps this call in try/except and still lands the document
            `ready`, because a document is searchable whether or not extraction worked.
        NotFoundError: No document carries that id, which is a caller bug rather than one
            of the expected skip conditions above.
    """
    reason = extraction_allowed(conn)
    if reason is not None:
        return reason

    document = _get_document(conn, document_id)
    # `extraction_allowed` has already established that an endpoint is configured, so
    # this cannot raise ConfigurationError. `context_window` comes off the settings row.
    config = resolve_tutor_config(conn)
    budget = int(config.context_window * EXTRACTION_BUDGET_SHARE) * CHARS_PER_TOKEN
    messages = build_extraction_prompt(text[:budget])

    # `client.complete` is async, and the ingestion worker is a plain thread with no
    # event loop. Owning one for the length of the call keeps `extract_facts` synchronous
    # and the worker free of async plumbing it has no other use for.
    content = asyncio.run(
        client.complete(config.endpoint_url, config.api_key, config.model, messages)
    )

    payload = _parse_payload(content)
    if payload is None:
        return UNPARSEABLE_RESPONSE

    _store_facts(conn, document, payload)
    return None


def select_active_facts(conn: sqlite3.Connection, class_id: int) -> list[sqlite3.Row]:
    """The facts about a class that may enter a prompt.

    This is the single filter deciding what the tutor model is allowed to see: not
    rejected, and either confirmed by the user or marked `high` by the model. Every
    prompt-building caller goes through here, and `backend.llm.prompts` deliberately does
    not filter again, so this function holds the only copy of the rule.
    """
    return list(conn.execute(_SELECT_ACTIVE_FACTS, (class_id,)))


def select_user_facts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The same filter as `select_active_facts`, over the facts belonging to no class."""
    return list(conn.execute(_SELECT_ACTIVE_FACTS, (None,)))


def _fact_dicts(conn: sqlite3.Connection, class_id: int | None) -> list[dict[str, object]]:
    """Every non-rejected fact in one scope, with its source document's filename."""
    facts: list[dict[str, object]] = []
    for row in conn.execute(_SELECT_PROFILE_FACTS, (class_id,)):
        fact = dict(row)
        # SQLite has no boolean type. The interface contract says these two are booleans.
        fact["confirmed"] = bool(fact["confirmed"])
        fact["rejected"] = bool(fact["rejected"])
        facts.append(fact)
    return facts


def _extraction_skipped_reason(conn: sqlite3.Connection, class_id: int) -> str | None:
    """Why the most recent ingestion for this class extracted nothing, if it did not."""
    row = conn.execute(_SELECT_SKIP_REASON, (class_id,)).fetchone()
    if row is None or row["stage_detail"] not in KNOWN_SKIP_REASONS:
        return None
    return str(row["stage_detail"])


def get_class_profile(conn: sqlite3.Connection, class_id: int) -> dict[str, object]:
    """The class profile as the interface renders it.

    Returns:
        `facts`, every non-rejected fact for the class, confirmed or not, each carrying
        the filename it came from, and `extraction_skipped_reason`, which lets the
        interface explain an empty profile instead of showing a bare empty state.
    """
    return {
        "facts": _fact_dicts(conn, class_id),
        "extraction_skipped_reason": _extraction_skipped_reason(conn, class_id),
    }


def get_user_profile(conn: sqlite3.Connection) -> dict[str, object]:
    """The global user profile: every non-rejected fact belonging to no class."""
    return {"facts": _fact_dicts(conn, None)}


def get_fact(conn: sqlite3.Connection, fact_id: int) -> sqlite3.Row:
    """One fact row.

    Raises:
        NotFoundError: when no fact carries that id. Every mutation below and the
            ownership checks in the routes look up through here, so the message that
            reaches the user is written once.
    """
    row = conn.execute("select * from profile_facts where id = ?", (fact_id,)).fetchone()
    if row is None:
        raise NotFoundError("That fact does not exist.")
    return row


def confirm_fact(conn: sqlite3.Connection, fact_id: int) -> None:
    """Confirm a fact, which makes it active context whatever its confidence.

    Confirming also clears `rejected`. Confirm and reject are two halves of one decision,
    so the later one wins rather than leaving a row that is somehow both.

    Raises:
        NotFoundError: when no fact carries that id.
    """
    get_fact(conn, fact_id)
    conn.execute("update profile_facts set confirmed = 1, rejected = 0 where id = ?", (fact_id,))
    conn.commit()


def reject_fact(conn: sqlite3.Connection, fact_id: int) -> None:
    """Reject a fact, leaving the row in place.

    The row is not deleted, because the row is the record: it is what stops
    `extract_facts` proposing the same fact again the next time its source document is
    ingested.

    Raises:
        NotFoundError: when no fact carries that id.
    """
    get_fact(conn, fact_id)
    conn.execute("update profile_facts set rejected = 1, confirmed = 0 where id = ?", (fact_id,))
    conn.commit()


def update_fact_value(conn: sqlite3.Connection, fact_id: int, value: str) -> None:
    """Replace a fact's value with the user's correction.

    Correcting a value does not confirm the fact. A low-confidence fact whose wording the
    user tidied is still a fact nobody has vouched for, so it waits for an explicit
    confirmation before it enters a prompt.

    Raises:
        NotFoundError: when no fact carries that id.
        ValueError: when the value is blank. The routes reject that with a 422 first, so
            this guards direct callers only.
    """
    get_fact(conn, fact_id)
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Fact value cannot be blank.")
    conn.execute("update profile_facts set value = ? where id = ?", (cleaned, fact_id))
    conn.commit()
