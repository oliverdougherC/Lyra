"""Profile facts: the class-scoped fact store, extraction, the active filter, confirmation.

A fact is a claim about the **class**, and documents are evidence for it. Text only ever
arrives one document at a time, so extraction is per-document, but the second document to
state a course's Fourier series topic adds a row to `profile_fact_sources`, not a second
copy of the topic. `_identity` is where that is decided, and it deliberately merges only
what differs by formatting: wording differences are judgment, and they wait for
`backend.core.consolidation`. See `docs/rag-pipeline.md` for the full rule.

An extracted fact is a proposal, not an assertion. `select_active_facts` is the single place
the promotion rule is written as SQL: not rejected, and either confirmed by the user, marked
`high` by the model, or attested by two documents independently. `backend.llm.prompts`
deliberately does not filter again.

Extraction is the one path that can send document text to a remote endpoint, so
`extract_facts` asks `extraction_allowed` before it does anything else at all.
"""

import asyncio
import logging
import sqlite3
import unicodedata
from collections.abc import Mapping

from backend.core.app_settings import (
    EXTRACTION_DISABLED,
    NO_ENDPOINT,
    REMOTE_UNACKNOWLEDGED,
    extraction_allowed,
    resolve_tutor_config,
)
from backend.core.errors import NotFoundError
from backend.llm import client, replies
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

# And never more than this, however large the window is.
#
# The share alone tied the size of every extraction to a number the student set for chat: a
# 262144-token window asked the model to read 629,144 characters of a problem set to decide
# what its deadlines were. That takes minutes on a hosted model and routinely overran the
# 300-second client timeout, which costs the whole run - a document that times out yields no
# facts at all, so the larger prompt was not buying better extraction, it was buying none.
# The ingestion worker takes one document at a time, so it also held every later upload
# behind it: one slow pass at the front of the queue is what "stuck on Analyzing" was.
#
# 6000 tokens is the budget an 8192 window gives, which is the default this prompt was
# written and tuned against. What extraction is looking for - dates, topics, grading, the
# professor's conventions - is stated near the front of a syllabus, not spread evenly
# through a hundred pages, so reading further is mostly paying to be told nothing.
EXTRACTION_MAX_TOKENS = 6000

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

# Kinds whose whole fact is a name. A topic is "Convolution", not "Convolution: <gloss>",
# so its entry has one field rather than a label and a value, and the value column carries
# the name while the label stays the section's own word. That is what bare strings already
# did, so the two shapes land identically.
_NAMED_KINDS = frozenset({"topic", "prerequisite"})
_NAME_KEYS = ("name", "label", "title", "value", "topic", "description")

# How many documents must independently state a fact before it counts as corroborated.
CORROBORATION_THRESHOLD = 2

# An unmarked fact is not a trusted fact. Defaulting to `low` keeps it out of prompts
# until a person has looked at it.
_DEFAULT_CONFIDENCE = "low"

# One statement serves both scopes. SQLite's `is` compares like `=` for a non-null
# parameter and like `is null` for a null one, so the filter itself is written once.
#
# The third clause of the `having` is corroboration: a fact two documents state independently
# has been vouched for by the material itself, which is evidence in the same way the model's
# own `high` marking is. Ordering is by that same evidence, because `_render_facts` caps each
# kind and the cap should fall on what one document mentioned once.
_SELECT_ACTIVE_FACTS = """
select f.*, count(s.document_id) as source_count
from profile_facts f
left join profile_fact_sources s on s.fact_id = f.id
where f.class_id is ? and f.rejected = 0
group by f.id
having f.confirmed = 1 or f.confidence = 'high' or count(s.document_id) >= ?
order by f.kind, source_count desc, f.label
"""

_SELECT_PROFILE_FACTS = """
select f.id, f.class_id, f.kind, f.label, f.value, f.confidence, f.confirmed, f.rejected,
       f.edited, f.source_document_id, f.created_at,
       count(s.document_id) as source_count
from profile_facts f
left join profile_fact_sources s on s.fact_id = f.id
where f.class_id is ? and f.rejected = 0
group by f.id
order by f.kind, source_count desc, f.label
"""

# Every document backing every fact in one scope, in one pass. Filenames are stitched onto
# their facts in Python rather than concatenated in SQL, because a filename may contain
# whatever separator the concatenation would have chosen.
_SELECT_FACT_SOURCES = """
select s.fact_id, d.filename
from profile_fact_sources s
join documents d on d.id = s.document_id
join profile_facts f on f.id = s.fact_id
where f.class_id is ?
order by s.fact_id, d.id
"""

# Identity is computed in Python, so the merge target is found by normalizing these rows
# rather than by matching a stored key. That keeps the normalization rule free to improve
# without a migration to re-key everything that came before.
_SELECT_CLASS_FACTS = """
select id, kind, label, value, confidence, edited, consolidated
from profile_facts where class_id is ? order by id
"""

_ATTEST = """
insert or ignore into profile_fact_sources (fact_id, document_id) values (?, ?)
"""

# Facts this document is the only evidence for, and that no person has ruled on. Deleting an
# upload has to withdraw what it alone claimed, or the profile keeps asserting things whose
# source the student has already thrown away.
_SELECT_SOLE_EVIDENCE = """
select f.id from profile_facts f
where f.confirmed = 0 and f.rejected = 0 and f.edited = 0
  and exists (select 1 from profile_fact_sources s
              where s.fact_id = f.id and s.document_id = ?)
  and (select count(*) from profile_fact_sources s where s.fact_id = f.id) = 1
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


def _read_named(item: object, kind: str) -> list[tuple[str, str, str]]:
    """Read one entry of a kind whose whole fact is a name.

    `{"name": "Fourier series"}` read as a general object would come back labelled `name`,
    which is the extractor's schema leaking out as a fact about the course. Here the name is
    the fact: it lands in the value, under the section's own label, exactly where a bare
    string lands, so both shapes produce the same row.
    """
    fallback_label = _DEFAULT_LABELS[kind]
    if isinstance(item, Mapping):
        if name := _first_text(item, _NAME_KEYS):
            return [(fallback_label, name, _confidence_of(item))]
        # No name key at all, so this is a mapping *of* names rather than one of them:
        # `{"1": "Convolution", "2": "Fourier series"}`. Reading only the first would
        # silently drop the rest.
        confidence = _confidence_of(item)
        return [
            (fallback_label, text, confidence)
            for key, raw in item.items()
            if key != _CONFIDENCE_KEY and (text := _as_text(raw))
        ]
    # A bare string carries no marking of its own, so it is low by default.
    text = _as_text(item)
    return [(fallback_label, text, _DEFAULT_CONFIDENCE)] if text else []


def _read_section(section: object, kind: str) -> list[tuple[str, str, str]]:
    """Read one payload key into `(label, value, confidence)` triples.

    Every shape a model has been seen to answer with is accepted for every key, because
    the prompt's `deadlines[]` and `grading{}` notation is a request, not a guarantee: a
    list of strings, a list of objects, a single object, or a bare string.
    """
    named = kind in _NAMED_KINDS
    fallback_label = _DEFAULT_LABELS[kind]

    if isinstance(section, list):
        facts: list[tuple[str, str, str]] = []
        for item in section:
            if named:
                facts.extend(_read_named(item, kind))
            elif isinstance(item, Mapping):
                facts.extend(_read_object(item, fallback_label))
            elif text := _as_text(item):
                # A bare string carries no marking of its own, so it is low by default.
                facts.append((fallback_label, text, _DEFAULT_CONFIDENCE))
        return facts

    if isinstance(section, Mapping):
        # A named kind answered as one object is one name; a general kind answered as one
        # object is a mapping of labels, which `_read_object` already knows how to read.
        return _read_named(section, kind) if named else _read_object(section, fallback_label)

    text = _as_text(section)
    return [(fallback_label, text, _DEFAULT_CONFIDENCE)] if text else []


def _parse_payload(content: str) -> dict[str, object] | None:
    """Parse the model's reply into a JSON object, or None when it is unusable.

    A bad reply is an expected outcome rather than a fault, so nothing raises here. The
    log lines carry no document text and no endpoint.
    """
    payload = replies.loads_object(content)
    if payload is None:
        logger.warning("Profile extraction returned a reply that is not a JSON object")
    return payload


def _get_document(conn: sqlite3.Connection, document_id: int) -> sqlite3.Row:
    """The document being extracted, for its `class_id`."""
    row = conn.execute("select id, class_id from documents where id = ?", (document_id,)).fetchone()
    if row is None:
        raise NotFoundError("That document does not exist.")
    return row


def _get_course(conn: sqlite3.Connection, class_id: int) -> dict[str, object] | None:
    """The class's own name, code, and term, for the extraction prompt to rule out."""
    row = conn.execute(
        "select name, code, semester from classes where id = ?", (class_id,)
    ).fetchone()
    return None if row is None else dict(row)


def _subject(kind: str, label: str, value: str) -> str:
    """The text that identifies a fact: its label when the model gave a real one, else its value.

    `Midterm 1` identifies a deadline whatever date the value carries, so a second document
    giving the same exam a differently worded date does not create a second deadline. A topic
    or a prerequisite is labelled with nothing but the section's own word, so its value is
    what identifies it, which is why the two cases collapse into one rule.
    """
    return label if label and label != _DEFAULT_LABELS.get(kind) else value


def _strip_trailing_gloss(text: str) -> str:
    """Drop one trailing parenthetical, which glosses a subject rather than naming it.

    Nesting is counted rather than pattern-matched, because the parentheses this has to cope
    with are mathematics: `Fourier transform computation (X(jw) and x(t))` closes twice at
    the end, and a rule that matched only the innermost pair would leave the gloss on and
    file that as a second topic.

    A subject that is nothing but a parenthetical is left alone: stripping it would leave no
    identity at all.
    """
    stripped = text.rstrip()
    if not stripped.endswith(")"):
        return text
    depth = 0
    for index in range(len(stripped) - 1, -1, -1):
        if stripped[index] == ")":
            depth += 1
        elif stripped[index] == "(":
            depth -= 1
            if depth == 0:
                return head if (head := stripped[:index].rstrip()) else text
    # Unbalanced, so there is no parenthetical here to be confident about.
    return text


def _normalize(text: str) -> str:
    """The comparison form of a subject: everything that is only formatting, removed.

    Merges are restricted to differences a reader would call typography, never wording, so
    that nothing here can join two things a student considers distinct. `Time Shift` and
    `Time-Shift Property` are left alone; consolidation decides those.

    A single trailing parenthetical goes, because it glosses the subject rather than naming
    it: `Convolution Property (Periodic Convolution)` is the convolution property.
    """
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    folded = _strip_trailing_gloss(folded).casefold()
    # `isalnum` rather than an ASCII class, so a Greek or CJK subject keeps its characters
    # instead of normalizing to the empty string and losing its identity entirely.
    spaced = "".join(char if char.isalnum() else " " for char in folded)
    return " ".join(spaced.split())


def _identity(kind: str, label: str, value: str) -> tuple[str, str]:
    """What makes two observations the same fact: their kind and their normalized subject."""
    return kind, _normalize(_subject(kind, label, value))


def _subject_column(kind: str, label: str) -> str:
    """Which column carries the subject: the label when there is a real one, else the value.

    The mirror of `_subject`, and the reason `_absorb` can rewrite a subject at all. A topic's
    subject is its value and a deadline's is its label, so a rule written against one column
    would either leave topics unmerged or overwrite a deadline's date with its own name.
    """
    return "label" if label and label != _DEFAULT_LABELS.get(kind) else "value"


def _absorb(
    conn: sqlite3.Connection,
    existing: dict[str, object],
    kind: str,
    label: str,
    value: str,
    confidence: str,
) -> None:
    """Fold a repeated observation into the fact it restates.

    Nothing a person has typed is overwritten, which is what `edited` is for. Otherwise the
    shortest wording of the subject wins: the two already agree modulo formatting, so the
    shorter is the name and the longer is the name with a gloss on it. `Fourier Transform`
    displaces `Fourier Transform (computation of X(jw))`.

    Confidence may rise but never fall, and only while consolidation has not yet ruled on the
    fact. A pass that demoted something as document metadata is a later and better-informed
    judgment than the next upload restating it.
    """
    if existing["edited"]:
        return

    changes: dict[str, object] = {}
    column = _subject_column(kind, str(existing["label"]))
    incoming = _subject(kind, label, value)
    if incoming and len(incoming) < len(str(existing[column])):
        changes[column] = incoming
    # A labelled fact whose payload arrived empty the first time takes one when it turns up.
    if column == "label" and value and not str(existing["value"]).strip():
        changes["value"] = value
    if confidence == "high" and existing["confidence"] == "low" and not existing["consolidated"]:
        changes["confidence"] = "high"
    if not changes:
        return

    assignments = ", ".join(f"{column} = ?" for column in changes)
    conn.execute(
        f"update profile_facts set {assignments} where id = ?",  # noqa: S608
        (*changes.values(), existing["id"]),
    )
    existing.update(changes)


def _store_facts(
    conn: sqlite3.Connection, document: sqlite3.Row, payload: Mapping[str, object]
) -> None:
    """Merge the payload's facts into the class profile and record this document as evidence.

    The merge is class-scoped, which is the whole point: a fact this document restates from
    another one gains a source rather than a row. Rejected facts take part, and that is what
    stops a rejected fact coming back the next time any document proposes it.
    """
    class_id = document["class_id"]
    known: dict[tuple[str, str], dict[str, object]] = {}
    for row in conn.execute(_SELECT_CLASS_FACTS, (class_id,)):
        # The earliest row wins a collision. Rows that predate this rule can already share an
        # identity, and later evidence should gather on one of them rather than spread.
        known.setdefault(_identity(row["kind"], row["label"], row["value"]), dict(row))

    for key, kind in _PAYLOAD_KINDS.items():
        for label, value, confidence in _read_section(payload.get(key), kind):
            identity = _identity(kind, label, value)
            if not identity[1]:
                # Nothing identifying survived normalization, so there is no fact here.
                continue
            existing = known.get(identity)
            if existing is None:
                fact_id = conn.execute(
                    _INSERT_FACT, (class_id, kind, label, value, confidence, document["id"])
                ).lastrowid
                known[identity] = {
                    "id": fact_id,
                    "kind": kind,
                    "label": label,
                    "value": value,
                    "confidence": confidence,
                    "edited": 0,
                    "consolidated": 0,
                }
            else:
                fact_id = int(existing["id"])
                _absorb(conn, existing, kind, label, value, confidence)
            conn.execute(_ATTEST, (fact_id, document["id"]))

    conn.commit()


def extraction_budget_chars(context_window: int) -> int:
    """How much document text one extraction pass may read, in characters.

    A share of the window, capped, so that raising the chat context window does not turn
    every upload into a multi-minute model call that times out before it answers.
    """
    return min(int(context_window * EXTRACTION_BUDGET_SHARE), EXTRACTION_MAX_TOKENS) * (
        CHARS_PER_TOKEN
    )


def extract_facts(
    conn: sqlite3.Connection, document_id: int, text: str, doc_type: str | None = None
) -> str | None:
    """Propose profile facts for one document from its text, merged into the class profile.

    The permission check runs first, before the text is read, truncated, or built into a
    prompt, so there is no path on which document text reaches a remote endpoint the user
    has not acknowledged.

    Args:
        conn: Open database connection.
        document_id: Document the facts are proposed from and attributed to.
        text: Full document text. Truncated here to the extraction budget.
        doc_type: What `detect_doc_type` decided, passed through to the prompt so the model
            knows whether it is holding a syllabus or the ninth problem set.

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
    budget = extraction_budget_chars(config.context_window)
    messages = build_extraction_prompt(
        text[:budget], doc_type, _get_course(conn, int(document["class_id"]))
    )

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
    """The facts about a class that may enter a prompt, most-attested first within a kind.

    This is the single filter deciding what the tutor model is allowed to see: not rejected,
    and either confirmed by the user, marked `high` by the model, or stated independently by
    `CORROBORATION_THRESHOLD` documents. Every prompt-building caller goes through here, and
    `backend.llm.prompts` deliberately does not filter again, so this function holds the only
    copy of the rule.
    """
    return list(conn.execute(_SELECT_ACTIVE_FACTS, (class_id, CORROBORATION_THRESHOLD)))


def select_user_facts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The same filter as `select_active_facts`, over the facts belonging to no class."""
    return list(conn.execute(_SELECT_ACTIVE_FACTS, (None, CORROBORATION_THRESHOLD)))


def _fact_dicts(conn: sqlite3.Connection, class_id: int | None) -> list[dict[str, object]]:
    """Every non-rejected fact in one scope, carrying every document that attests it.

    Most-attested first within a kind, so the interface leads with what the class is
    actually about rather than with whichever upload happened to land first.
    """
    sources: dict[int, list[str]] = {}
    for row in conn.execute(_SELECT_FACT_SOURCES, (class_id,)):
        sources.setdefault(int(row["fact_id"]), []).append(str(row["filename"]))

    facts: list[dict[str, object]] = []
    for row in conn.execute(_SELECT_PROFILE_FACTS, (class_id,)):
        fact = dict(row)
        # SQLite has no boolean type. The interface contract says these three are booleans.
        fact["confirmed"] = bool(fact["confirmed"])
        fact["rejected"] = bool(fact["rejected"])
        fact["edited"] = bool(fact["edited"])
        filenames = sources.get(int(row["id"]), [])
        fact["sources"] = filenames
        # The first document to say it, kept because a single-source fact still reads best
        # as "From homework_1.pdf" rather than as a count of one.
        fact["source_filename"] = filenames[0] if filenames else None
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
    # `edited` is what protects the correction from the consolidation pass, which is
    # otherwise free to merge this row into another one and delete it.
    conn.execute("update profile_facts set value = ?, edited = 1 where id = ?", (cleaned, fact_id))
    conn.commit()


def forget_document_evidence(conn: sqlite3.Connection, document_id: int) -> int:
    """Drop the facts this document is the only evidence for. Returns the row count.

    Called before a document is deleted. A claim whose only source the student has just
    thrown away should not go on being asserted, but a claim they confirmed, rejected, or
    corrected is theirs rather than the document's, and it stays.

    The caller commits, because deleting the document is the same unit of work.
    """
    doomed = [(int(row["id"]),) for row in conn.execute(_SELECT_SOLE_EVIDENCE, (document_id,))]
    conn.executemany("delete from profile_facts where id = ?", doomed)
    return len(doomed)
