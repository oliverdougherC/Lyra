"""Finding the problems in a homework set, from two sources of evidence.

The chunker already did most of this. `rag/chunk.py` splits documents detected as
homework on problem markers and stamps `problem_number` and `part_index` on every chunk,
so a problem list is largely a read over data that exists. That pass is deterministic,
free, and correct on the common case.

What it cannot do is read. A set numbered by section heading alone, a problem introduced
by prose, or a sub-part marked in a way `SUBPART_MARKER` deliberately excludes are all
invisible to a regex. So a model pass runs alongside it, and the two are reconciled here.

**The chunker is the spine, and the model is the reading.** The markers decide which
problems exist, because they are positions in the document rather than a recollection of
it. The model contributes labels, sub-parts, problems the markers missed, and the
mathematics transcribed back into LaTeX from an extraction that flattened it. Where the
two disagree about a statement, the model's version is taken only when it kept the
sheet's own words; a summary loses to the document's own text.

None of this has to be right. It has to be *reviewable*: the job stops at
`awaiting_review` and a person confirms or corrects the list before a minute of compute is
spent on it. That is why a failed model pass falls back to the chunker alone rather than
failing the run, and why nothing here raises on a reply it cannot read.
"""

import asyncio
import json
import logging
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field

from backend.core.app_settings import document_text_allowed, resolve_tutor_config
from backend.core.errors import ConfigurationError
from backend.llm import client
from backend.llm.prompts import build_segmentation_prompt
from backend.rag.tokens import CHARS_PER_TOKEN

logger = logging.getLogger(__name__)

# Share of the tutor context window one segmentation pass may spend on document text. Held
# below the extraction share because the reply is longer: the model echoes every problem
# statement back, so the output needs room the input would otherwise take.
SEGMENTATION_BUDGET_SHARE = 0.5

# A problem number as the model might write it: `4`, `3.14`, `Problem 4`, `Q4`.
_NUMBER = re.compile(r"\d+(?:\.\d+)*")

_CHUNKS_SQL = """
select id, content, page_number, problem_number, part_index
from chunks
where document_id = ? and problem_number is not null
order by id
"""


@dataclass(frozen=True)
class SegmentedPart:
    """One lettered or numbered sub-part of a problem."""

    label: str
    statement: str


@dataclass(frozen=True)
class SegmentedProblem:
    """One problem proposed for review.

    Attributes:
        label: What the sheet calls it. Falls back to `Problem {number}` when only the
            chunker saw it, because a list of bare numbers reads as an index, not a sheet.
        number: The number alone, used to match the two sources against each other.
        statement: The problem text. From the chunker where it has it, so it is the
            document's own text rather than the model's recollection of it.
        document_id: Source document, which is what lets a multi-file set stay ordered.
        page_number: Page it starts on, when known.
        chunk_ids: Chunks this problem came from, kept as its provenance.
        parts: Sub-parts, in order. Empty for a problem with none.
        origin: Who wrote this entry. `generated` for anything Lyra proposed,
            `user_corrected` for a problem the student edited, merged, split, or typed at
            the review gate. Carried here rather than decided by the writer because only
            the caller that took the correction knows which entries the student touched,
            and the interface marks those so a later read knows the statement is not
            verbatim from the sheet.
    """

    label: str
    number: str
    statement: str
    document_id: int
    page_number: int | None = None
    chunk_ids: tuple[int, ...] = ()
    parts: tuple[SegmentedPart, ...] = ()
    origin: str = "generated"


@dataclass
class _Draft:
    """A problem being assembled from chunk rows before the model has had its say."""

    number: str
    pieces: list[str] = field(default_factory=list)
    chunk_ids: list[int] = field(default_factory=list)
    page_number: int | None = None


def _normalise(value: object) -> str:
    """The bare number inside a label, or an empty string when there is none."""
    match = _NUMBER.search(str(value or ""))
    return match.group(0) if match else ""


def _text(value: object) -> str:
    """A JSON value as trimmed text, with anything that is not a string discarded."""
    return value.strip() if isinstance(value, str) else ""


def _label_for(proposed: str, number: str) -> str:
    """The sheet's own wording for a problem, or `Problem {n}` when there is none.

    Models routinely answer the label field with the bare number, which tells the reader
    nothing the number did not, and a card labelled `4` sitting beside its own position
    index prints the same digit twice. Only a label carrying something more than the
    number survives, which is what `Exercise 3.14` and `Problem 4 (bonus)` do.
    """
    stripped = proposed.strip()
    if not stripped or stripped.strip(" .)#:") == number:
        return f"Problem {number}"
    return stripped


def _statement_for(problem: SegmentedProblem, proposal: SegmentedProblem) -> str:
    """Which of the two readings of one problem the student should be shown.

    The chunker's text is the document's own, character for character, which is why it is
    the spine. What it is not is *readable*: PDF extraction flattens exponents,
    subscripts, and fractions into the line, so the sheet's $e^{-2t}u(t-3)$ arrives as
    `e−2tu(t −3)` and a student asked to check Lyra's reading against their homework is
    comparing something their sheet does not say. Transcribing that back into LaTeX is a
    large part of what the model pass is for.

    So the model's version wins when it is a transcription and loses when it is a summary,
    and the two are told apart by whether the sheet's own words survived in it. A
    transcription changes the notation and keeps the words; a paraphrase drops them. The
    model's sub-parts count as part of its reading, because the segmentation prompt asks
    for them separately and comparing against the statement alone would read every problem
    with lettered parts as a summary.
    """
    whole = "\n".join([proposal.statement, *(part.statement for part in proposal.parts)])
    return proposal.statement if _keeps_the_words(problem.statement, whole) else problem.statement


# Words long enough not to be notation, and every run of digits. Both survive being
# rewritten into LaTeX, which is exactly what makes them evidence that nothing was cut.
_CONTENT_TOKEN = re.compile(r"[a-z]{4,}|\d+")

# How much of the sheet's own text a reading has to keep to count as a transcription.
# Below 1.0 because the chunker's text carries page numbers, running heads, and section
# titles that a faithful reading of one problem is right to leave out.
_TRANSCRIPTION_SHARE = 0.8


def _keeps_the_words(original: str, candidate: str) -> bool:
    """Whether `candidate` still contains what `original` said, notation aside."""
    wanted = _CONTENT_TOKEN.findall(original.lower())
    if not wanted:
        # Nothing comparable to go on, so the document's own text stands.
        return False
    have = set(_CONTENT_TOKEN.findall(candidate.lower()))
    kept = sum(1 for token in wanted if token in have)
    return kept >= _TRANSCRIPTION_SHARE * len(wanted)


def chunked_problems(conn: sqlite3.Connection, document_id: int) -> list[SegmentedProblem]:
    """The problems the chunker already found in one document.

    A problem split across several chunks is reassembled in part order, which is the same
    reassembly retrieval does when one part of a problem matches a query.

    **A number that comes back later is a different problem.** Sheets restart their
    numbering under each section heading: one real set runs 1 to 3 under `System of LTI
    systems`, 1 to 4 under the next heading, and 1 to 5 under the one after that, which is
    twelve problems and not five. Collecting by number folded each of those groups into
    one row carrying three unrelated statements, and because the chunker is the spine, a
    model pass that read the sheet correctly was reconciled back down to the same five.
    Chunks arrive in document order, so a run of one number is one problem and a repeat
    after a gap is a new one.
    """
    drafts: list[_Draft] = []
    for row in conn.execute(_CHUNKS_SQL, (document_id,)):
        number = str(row["problem_number"])
        if not drafts or drafts[-1].number != number:
            drafts.append(_Draft(number=number, page_number=row["page_number"]))
        drafts[-1].pieces.append(str(row["content"]))
        drafts[-1].chunk_ids.append(int(row["id"]))

    return [
        SegmentedProblem(
            label=f"Problem {draft.number}",
            number=draft.number,
            statement="\n\n".join(draft.pieces).strip(),
            document_id=document_id,
            page_number=draft.page_number,
            chunk_ids=tuple(draft.chunk_ids),
        )
        for draft in drafts
    ]


def _read_parts(value: object) -> tuple[SegmentedPart, ...]:
    """Read a problem's sub-parts from the model's reply, dropping anything empty."""
    if not isinstance(value, list):
        return ()
    parts: list[SegmentedPart] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            continue
        statement = _text(item.get("statement"))
        if not statement:
            continue
        # A sub-part with no label still has a position, and `(a)`, `(b)` is what the
        # sheet almost certainly used.
        label = _text(item.get("label")) or f"({chr(ord('a') + index)})"
        parts.append(SegmentedPart(label=label, statement=statement))
    return tuple(parts)


def parse_segmentation(content: str, document_id: int) -> list[SegmentedProblem]:
    """Read the model's reply into problems, tolerating everything except nonsense.

    Returns an empty list for a reply that cannot be read. That is not treated as a
    failure anywhere: it means this source of evidence contributed nothing, and the
    chunker's list stands on its own.
    """
    try:
        payload = json.loads(_strip_code_fence(content))
    except ValueError:
        logger.warning("Segmentation returned a reply that is not JSON")
        return []
    if not isinstance(payload, Mapping):
        return []

    entries = payload.get("problems")
    if not isinstance(entries, list):
        return []

    problems: list[SegmentedProblem] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping):
            continue
        statement = _text(entry.get("statement"))
        if not statement:
            continue
        label = _text(entry.get("label"))
        number = _normalise(entry.get("number")) or _normalise(label) or str(index)
        page = entry.get("page")
        problems.append(
            SegmentedProblem(
                label=_label_for(label, number),
                number=number,
                statement=statement,
                document_id=document_id,
                page_number=page if isinstance(page, int) and page > 0 else None,
                parts=_read_parts(entry.get("parts")),
            )
        )
    return problems


def _strip_code_fence(content: str) -> str:
    """Remove one wrapping markdown fence, tagged or bare."""
    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def reconcile(
    from_chunks: list[SegmentedProblem], from_model: list[SegmentedProblem]
) -> list[SegmentedProblem]:
    """Merge the two lists, with the chunker as the spine.

    A model problem whose number matches a chunked one contributes its label and its
    sub-parts, and nothing else: the statement stays the document's own text. A model
    problem with no match is appended as its own entry, because a problem the regex missed
    is exactly what the model pass is for.

    **A number can appear more than once**, because a sheet that restarts its numbering
    under each section heading has three problem 1s and they are three problems. Equal
    numbers are therefore matched in document order, first to first, rather than through a
    lookup that would silently keep only the last of them.

    Order is chunker order first, then the model's additions in the order it found them.
    Both are document order, and a person is about to look at the result anyway.
    """
    if not from_chunks:
        return from_model

    positions: dict[str, list[int]] = {}
    for index, problem in enumerate(from_model):
        if problem.number:
            positions.setdefault(problem.number, []).append(index)

    taken: dict[str, int] = {}
    matched: set[int] = set()
    merged: list[SegmentedProblem] = []

    for problem in from_chunks:
        candidates = positions.get(problem.number, [])
        offset = taken.get(problem.number, 0)
        if offset >= len(candidates):
            merged.append(problem)
            continue
        index = candidates[offset]
        taken[problem.number] = offset + 1
        matched.add(index)
        proposal = from_model[index]
        merged.append(
            SegmentedProblem(
                # The model read the sheet's own wording, which the chunker cannot: it only
                # ever produces `Problem {n}`. A model that answered with the bare number
                # has added nothing, so the chunker's label stands.
                label=_label_for(proposal.label, problem.number),
                number=problem.number,
                statement=_statement_for(problem, proposal),
                document_id=problem.document_id,
                page_number=problem.page_number or proposal.page_number,
                chunk_ids=problem.chunk_ids,
                parts=proposal.parts,
            )
        )

    merged.extend(problem for index, problem in enumerate(from_model) if index not in matched)
    return merged


def propose_problems(
    conn: sqlite3.Connection, document_id: int, filename: str, text: str
) -> list[SegmentedProblem]:
    """Propose the problem list for one document, from both sources of evidence.

    The model pass is best-effort. No endpoint, an endpoint that fails, and a reply that
    cannot be read all produce the same outcome: the chunker's list, unaugmented. None of
    them raises, because a segmentation nobody can improve on is still a segmentation the
    student can correct at the gate, and failing the run instead would cost them the
    upload.

    Args:
        conn: Open database connection.
        document_id: Document to segment.
        filename: Original upload name, passed to the model as context.
        text: Full document text. Truncated here to the segmentation budget.

    Returns:
        Problems in document order. An empty list is a real outcome for a document that
        is not a numbered problem set, and the interface says so rather than erroring.
    """
    from_chunks = chunked_problems(conn, document_id)

    # The model pass sends the whole document to the tutor endpoint, exactly as profile
    # extraction does, so it is bound by the same rule: document text is never sent to a
    # non-local endpoint the student has not acknowledged. Asked before the text is read,
    # truncated, or built into a prompt, so there is no path on which it leaks.
    blocked = document_text_allowed(conn)
    if blocked is not None:
        logger.info("Segmenting document %s from chunk markers only: %s", document_id, blocked)
        return from_chunks

    try:
        config = resolve_tutor_config(conn)
    except ConfigurationError:
        logger.info("Segmenting document %s from chunk markers only: no endpoint", document_id)
        return from_chunks

    budget = int(config.context_window * SEGMENTATION_BUDGET_SHARE) * CHARS_PER_TOKEN
    messages = build_segmentation_prompt(text[:budget], filename)
    try:
        # The solver worker is a plain thread with no event loop, and `complete` is async.
        # Owning a loop for the length of the call keeps this function synchronous.
        content = asyncio.run(
            client.complete(config.endpoint_url, config.api_key, config.model, messages)
        )
    except Exception:
        logger.exception("Segmentation model pass failed for document %s", document_id)
        return from_chunks

    return reconcile(from_chunks, parse_segmentation(content, document_id))
