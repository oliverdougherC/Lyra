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
import logging
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field

from backend.core.app_settings import document_text_allowed, resolve_tutor_config
from backend.core.errors import ConfigurationError
from backend.llm import client, replies
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


# What the model is asked to say about a problem's parts, and what it means here.
_SEPARATE = "separate"
_ONE_SOLUTION = "one_solution"


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
        separate_parts: Whether those sub-parts are questions in their own right, each
            with its own answer, rather than steps of one solution. False for a problem
            with no parts, and false whenever the reading is uncertain: solving parts
            together is what Lyra has always done, and a wrong `True` solves a part
            without the earlier part it depends on. See `parts_are_separate`.
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
    separate_parts: bool = False
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
    transcription changes the notation and keeps the words; a paraphrase drops them.

    The comparison is against the model's *whole* reading: its label and its sub-parts as
    well as its statement. The prompt asks for all three separately, so a faithful reading
    of `Problem 1 (Time Shift)` puts those words in the label and a reading of a problem
    with lettered parts puts most of its text in the parts. Comparing against the
    statement alone read both as summaries and printed the flattened extraction instead.
    """
    whole = "\n".join(
        [proposal.label, proposal.statement, *(part.statement for part in proposal.parts)]
    )
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


# A part pointing at another part: "using your answer to (a)", "from part (b)", "repeat
# the above for", "as in (i)". Any one of these means the parts are a chain, because a
# part that names another cannot be handed to a model on its own -- the thing it refers
# to would simply not be in the turn.
#
# Bare "(a)" is deliberately not enough. Sub-part statements are full of parentheses that
# are mathematics: `x(t)`, `u(t-1)`, `(2 + j)`. The reference has to be worded.
_CROSS_REFERENCE = re.compile(
    r"""
    \b(?:part|parts)\s*\(?[a-z0-9]{1,3}\)?        # part (a), parts b
    | \byour\s+(?:answer|result|expression|sketch|solution)   # your answer to ...
    | \b(?:from|in|of|using|use)\s+\(\s*[a-z]\s*\)             # from (a), using (b)
    | \b(?:the\s+)?(?:previous|preceding|earlier|above)\s+(?:part|result|answer)
    | \brepeat\s+(?:the\s+)?(?:above|previous)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# A stem that hands the same question to every part: the parts are its instances, and
# each instance has an answer of its own. "For each system below, determine whether it is
# linear" is five questions, and reading it as one is what put five results in one answer
# sentence and one verdict over all five.
_DISTRIBUTES = re.compile(
    r"""
    \bfor\s+each\b
    | \bfor\s+every\b
    | \bfor\s+(?:all\s+of\s+)?the\s+following\b
    | \beach\s+of\s+the\s+following\b
    | \b(?:determine|find|compute|evaluate|sketch|state|classify|simplify|solve)
      \s+(?:\w+\s+){0,4}?following\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def parts_are_separate(
    statement: str, parts: tuple[SegmentedPart, ...], claimed: str | None
) -> bool:
    """Whether a problem's parts are questions of their own or steps of one solution.

    Three readings, in order of how much they can be trusted:

    1. **A part that refers to another part settles it, and settles it as one solution.**
       This is evidence rather than opinion: "using your answer to (a)" cannot be solved
       in a turn that does not contain (a). It overrides everything below, including the
       model, because the cost of being wrong here is a part solved against a result it
       was never given.
    2. **Otherwise the model's own reading, which saw the whole sheet.** It is asked
       directly, in the words of the thing it is deciding, and it is right on the cases
       that matter most: a section of five systems to classify, and a derivation whose
       parts hand results forward.
    3. **Otherwise the shape of the stem.** "For each of the following" distributes one
       question over its parts, which is exactly what makes each part a question. This is
       a backstop for a model that ignored the field or answered it with something that
       is neither value, and it is never asked to overrule a model that did answer.

    Anything still undecided is one solution. That is what Lyra did before this existed,
    so an uncertain reading costs the student nothing they had.

    Args:
        statement: The problem's own text -- the stem above the parts.
        parts: Its sub-parts. No parts, nothing to separate.
        claimed: What the model said, or None when it said nothing usable.

    Returns:
        True when each part should be solved, answered, and checked on its own.
    """
    if len(parts) < 2:
        # One part is not a set of questions; it is a problem whose statement runs on.
        return False
    if any(_CROSS_REFERENCE.search(part.statement) for part in parts):
        return False
    if claimed in (_SEPARATE, _ONE_SOLUTION):
        return claimed == _SEPARATE
    return bool(_DISTRIBUTES.search(statement))


def parse_segmentation(content: str, document_id: int) -> list[SegmentedProblem]:
    """Read the model's reply into problems, tolerating everything except nonsense.

    Returns an empty list for a reply that cannot be read. That is not treated as a
    failure anywhere: it means this source of evidence contributed nothing, and the
    chunker's list stands on its own.
    """
    # A problem statement carries the notation it was set in, and reading it strictly makes
    # a `\theta` anywhere in the sheet cost the whole segmentation.
    payload = replies.loads(_strip_code_fence(content))
    if payload is None:
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
        parts = _read_parts(entry.get("parts"))
        problems.append(
            SegmentedProblem(
                label=_label_for(label, number),
                number=number,
                statement=statement,
                document_id=document_id,
                page_number=page if isinstance(page, int) and page > 0 else None,
                parts=parts,
                separate_parts=parts_are_separate(
                    statement, parts, _text(entry.get("parts_relation")).lower() or None
                ),
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


# How much coarser the model's reading has to be before the two lists are treated as
# describing different things rather than disagreeing about the same one. Half is far
# outside the range of an ordinary disagreement, where the model finds one problem the
# markers missed or misses one they found.
_GRAIN_RATIO = 2


def _reads_at_a_coarser_grain(
    from_chunks: list[SegmentedProblem], from_model: list[SegmentedProblem]
) -> bool:
    """Whether the model grouped into sections what the markers split into problems.

    One real sheet runs 1 to 3, 1 to 4, and 1 to 5 under three headings. The markers see
    twelve problems; the model saw three, each with the rest as sub-parts. Neither is
    wrong, and matching them against each other by number is meaningless: problem 2 of the
    first heading gets annotated with the second heading, and every problem the model
    folded into a sub-part is then appended again as its own row. The result is a gate
    listing the same questions twice.

    So this is detected rather than merged. The chunker's list stands alone, because its
    entries are positions in the document rather than a recollection of it, and because a
    per-problem verdict and a per-problem citation are worth more than one verdict covering
    five questions. The cost is that this sheet's statements stay as extraction left them.

    Recognised by the model's own arithmetic: far fewer problems than the markers found,
    while its problems and their sub-parts together account for all of them.
    """
    if not from_model or len(from_chunks) < _GRAIN_RATIO * len(from_model):
        return False
    covered = sum(1 + len(problem.parts) for problem in from_model)
    return covered >= len(from_chunks)


def reconcile(
    from_chunks: list[SegmentedProblem], from_model: list[SegmentedProblem]
) -> list[SegmentedProblem]:
    """Merge the two lists, with the chunker as the spine.

    A model problem whose number matches a chunked one contributes its label, its
    sub-parts, and its transcription of the statement. A model problem with no match is
    appended as its own entry, because a problem the regex missed is exactly what the
    model pass is for.

    **A number can appear more than once**, because a sheet that restarts its numbering
    under each section heading has three problem 1s and they are three problems. Equal
    numbers are therefore matched in document order, first to first, rather than through a
    lookup that would silently keep only the last of them.

    **The two can also read the same sheet at different granularities**, and then they are
    not comparable at all. See `_reads_at_a_coarser_grain`.

    Order is chunker order first, then the model's additions in the order it found them.
    Both are document order, and a person is about to look at the result anyway.
    """
    if not from_chunks:
        return from_model
    if _reads_at_a_coarser_grain(from_chunks, from_model):
        return from_chunks

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
                # Both from the proposal, and they have to travel together: the reading
                # of how the parts relate was made about *these* parts, and the chunker
                # never had either.
                parts=proposal.parts,
                separate_parts=proposal.separate_parts,
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
