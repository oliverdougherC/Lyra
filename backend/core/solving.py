"""Solving one problem: what is retrieved, what is asked, and how the reply is read.

`core/solver.py` owns the job. This module owns one problem: gathering the evidence,
building the turn, and turning the model's reply into steps. It is separated for the same
reason `core/segmentation.py` is: the orchestration is about state transitions and the
worker thread, and this is about a prompt and a parser, and neither is easier to read
inside the other.

**Solving runs without tools.** That is deliberate and it is specified: it keeps solving
working against any OpenAI-compatible endpoint, including one that does not implement
`tools` at all, and it keeps checking independent of the work, which is the whole reason a
check is worth anything. Verification is a separate pass in `core/verification.py`.

**Method alignment is the point, and it is the phase's least verifiable claim.** There is
no automated check for "solved it the way the course does". What this module can do is
make the evidence visible: retrieval runs against the student's own material, the model is
asked which retrieved entries each step rests on, and those become the step's provenance.
A student who knows their course can then see at a glance whether a step came from their
notes or from the model.
"""

import json
import logging
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from backend.config import settings
from backend.core import artifacts
from backend.core.app_settings import TutorConfig
from backend.llm.prompts import build_solve_prompt, format_context_block, format_reference_block
from backend.rag.retrieve import RetrievalResult, RetrievedChunk, retrieve
from backend.rag.tokens import CHARS_PER_TOKEN

logger = logging.getLogger(__name__)

# Share of the tutor context window one solve may spend on retrieved course material.
# Below the chat turn's 0.40 because a solve also carries reference solutions and expects
# a long structured reply, and the generation reserve is what pays for that reply.
SOLVE_RETRIEVAL_SHARE = 0.30

# Share of the retrieval budget reference solutions may take. Capped so a long reference
# document cannot crowd out the course material for the problem actually being solved,
# which is the failure mode that makes references a liability rather than an advantage.
REFERENCE_SHARE = 0.4

_EMPTY_RETRIEVAL = RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)


@dataclass(frozen=True)
class SolvedStep:
    """One step of a solution as the model returned it.

    Attributes:
        title: Short phrase naming what the step does. May be empty.
        content: The working, in markdown with LaTeX.
        sources: One-based indices into the context block the step claims to rest on.
            Indices outside the block are dropped when provenance is written, because a
            citation that resolves to nothing would render as grounding that does not
            exist.
    """

    title: str
    content: str
    sources: tuple[int, ...] = ()


@dataclass(frozen=True)
class SolvedProblem:
    """A finished solution: its steps in order, and the answer they arrive at."""

    steps: tuple[SolvedStep, ...]
    answer: str

    def as_markdown(self) -> str:
        """The whole solution as one document, which is what the verifier is given."""
        blocks = [
            f"Step {index}. {step.title}\n\n{step.content}" if step.title else step.content
            for index, step in enumerate(self.steps, start=1)
        ]
        if self.answer:
            blocks.append(f"Answer: {self.answer}")
        return "\n\n".join(blocks)


@dataclass(frozen=True)
class SolveInput:
    """Everything one problem needs solving, gathered before the model is called."""

    statement: str
    label: str
    sub_parts: tuple[tuple[str, str], ...] = ()
    correction: str = ""


def _strip_code_fence(content: str) -> str:
    """Remove one wrapping markdown fence, tagged or bare."""
    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _text(value: object) -> str:
    """A JSON value as trimmed text, with anything that is not a string discarded."""
    return value.strip() if isinstance(value, str) else ""


def _sources(value: object) -> tuple[int, ...]:
    """Read a step's cited context numbers, dropping anything that is not one."""
    if not isinstance(value, list):
        return ()
    return tuple(
        int(entry) for entry in value if isinstance(entry, int) and not isinstance(entry, bool)
    )


# A citation marker the model wrote into its prose: `[6]`, or `[2, 5]`. The lookbehind is
# what keeps `x[6]` and `X[k]` out of it, which matters here more than most places: this is
# a signals course, and a bracketed index after an identifier is ordinary notation.
_CITATION_MARKER = re.compile(
    # The lookbehind sits against the bracket rather than at the start of the match, so
    # the space before a marker is eaten with it and `x[6]` is still left alone.
    r"[ \t]*(?<![A-Za-z0-9_}\])])\[[ \t]*(\d+(?:[ \t]*,[ \t]*\d+)*)[ \t]*\]"
)

# Whitespace a removed marker left behind, mid-line only so indentation survives.
_LEFTOVER_SPACE = re.compile(r"(?<=\S)[ \t]{2,}")


def _lift_citations(content: str, declared: tuple[int, ...]) -> tuple[str, tuple[int, ...]]:
    """Move `[6]` markers out of a step's prose and into its sources.

    The prompt asks for citations in the `sources` field and says plainly that a number
    written into the text refers to a list the student cannot see. Models write them into
    the text anyway, and on screen `[6]` is a footnote marker pointing at nothing.

    Deleting them would throw away a real citation, so they are lifted: added to the
    step's sources, where the provenance chip turns them into a filename and a page, and
    removed from the prose. A marker outside the context block is dropped downstream by
    `_provenance_for`, the same as a declared one.
    """
    found: list[int] = []

    def take(match: re.Match[str]) -> str:
        found.extend(int(number) for number in match.group(1).replace(" ", "").split(","))
        return ""

    cleaned = _LEFTOVER_SPACE.sub(" ", _CITATION_MARKER.sub(take, content)).strip()
    if not cleaned:
        # The step was nothing but a marker. Keeping the original is the safer failure:
        # a step with no text at all would render as a gap in the working.
        return content, declared
    merged = list(declared) + [number for number in found if number not in declared]
    return cleaned, tuple(dict.fromkeys(merged))


def parse_solution(content: str) -> SolvedProblem:
    """Read the model's reply into steps and an answer.

    A model that ignores the structure and returns one prose block is not an error. Its
    reply becomes a single untitled step, because degrading to a worse-organised solution
    beats failing a problem the model in fact solved.

    Returns:
        The parsed solution. A reply with no usable text at all comes back with no steps
        and no answer, which is the one case the caller treats as a failed problem.
    """
    stripped = _strip_code_fence(content)
    try:
        payload = json.loads(stripped)
    except ValueError:
        return _as_prose(stripped)
    if not isinstance(payload, Mapping):
        return _as_prose(stripped)

    entries = payload.get("steps")
    answer = _text(payload.get("answer"))
    if not isinstance(entries, list):
        # Structured enough to hold an answer but not steps. Keep the answer rather than
        # discarding a correct result over its packaging.
        return SolvedProblem(steps=(), answer=answer) if answer else _as_prose(stripped)

    steps: list[SolvedStep] = []
    for entry in entries:
        if isinstance(entry, str):
            # A list of plain strings is a shape models fall into often enough to read.
            if entry.strip():
                steps.append(SolvedStep(title="", content=entry.strip()))
            continue
        if not isinstance(entry, Mapping):
            continue
        step_content = _text(entry.get("content")) or _text(entry.get("text"))
        if not step_content:
            continue
        step_content, sources = _lift_citations(step_content, _sources(entry.get("sources")))
        steps.append(
            SolvedStep(
                title=_text(entry.get("title")),
                content=step_content,
                sources=sources,
            )
        )

    if not steps and not answer:
        return _as_prose(stripped)
    # The answer carries markers too, and it has nowhere to put them: an answer is a
    # result, not a step, so there is no provenance row for it to become.
    return SolvedProblem(steps=tuple(steps), answer=_lift_citations(answer, ())[0])


def _as_prose(content: str) -> SolvedProblem:
    """Store an unstructured reply as one step, or as nothing when it is empty."""
    cleaned = content.strip()
    if not cleaned:
        return SolvedProblem(steps=(), answer="")
    return SolvedProblem(steps=(SolvedStep(title="", content=cleaned),), answer="")


def retrieve_for(
    conn: sqlite3.Connection, class_id: int, statement: str, budget_tokens: int
) -> RetrievalResult:
    """Retrieve course material for one problem, treating a retrieval failure as none.

    The query is the problem statement itself, which is exactly the shape retrieval
    already handles well. A retrieval that cannot run, most often because the local
    embedding server is not up, must not fail the solve: an ungrounded solution is worth
    more than no solution, and the interface says which steps were grounded either way.
    """
    if budget_tokens <= 0:
        return _EMPTY_RETRIEVAL
    try:
        return retrieve(conn, class_id, statement, budget_tokens)
    except Exception:
        logger.exception("Retrieval failed while solving; continuing without context")
        return _EMPTY_RETRIEVAL


@dataclass(frozen=True)
class ReferenceDocument:
    """One reference solution as a solve turn carries it.

    The id travels with the text because a step that cites this document has to become a
    provenance row pointing at it, the same way a cited chunk does.
    """

    document_id: int
    filename: str
    text: str


def reference_documents(
    conn: sqlite3.Connection, artifact_id: int, budget_tokens: int
) -> list[ReferenceDocument]:
    """The reference solutions attached to this run, truncated to their shared budget.

    The budget is split evenly across the documents rather than first-come, so two
    references contribute one half each instead of the first one taking all of it.
    """
    sources = artifacts.list_sources(conn, artifact_id, artifacts.REFERENCE_SOLUTIONS)
    if not sources or budget_tokens <= 0:
        return []

    per_document = (budget_tokens // len(sources)) * CHARS_PER_TOKEN
    documents: list[ReferenceDocument] = []
    for source in sources:
        document_id = int(source["document_id"])
        text = _document_text(document_id)
        if text:
            documents.append(
                ReferenceDocument(document_id, str(source["filename"]), text[:per_document])
            )
    return documents


def _document_text(document_id: int) -> str:
    """The text ingestion extracted for one document, or empty when it is not on disk."""
    path = settings.text_dir / f"{document_id}.txt"
    if not path.exists():
        return ""
    return Path(path).read_text(encoding="utf-8")


def build_prompt(
    problem: SolveInput, retrieval: RetrievalResult, references: list[ReferenceDocument]
) -> list[dict[str, str]]:
    """Assemble the solve turn from the problem and the evidence gathered for it.

    References are numbered continuing from the retrieved chunks, in one sequence, so a
    step that followed the answer key can cite it. Numbering them separately, or not at
    all, made a solve whose whole point was a reference document report every step
    ungrounded: the model had followed something it had no way to name.
    """
    return build_solve_prompt(
        problem.statement,
        problem.label,
        sub_parts=list(problem.sub_parts),
        context_block=format_context_block([_context_entry(c) for c in retrieval.chunks]),
        reference_block=format_reference_block(
            [(one.filename, one.text) for one in references],
            start_index=len(retrieval.chunks) + 1,
        ),
        correction=problem.correction,
    )


def _context_entry(chunk: RetrievedChunk) -> dict[str, object]:
    """The dict shape `format_context_block` labels a retrieved chunk from."""
    return {
        "content": chunk.content,
        "filename": chunk.filename,
        "page_number": chunk.page_number,
        "section_title": chunk.section_title,
        "problem_number": chunk.problem_number,
    }


def plan_budgets(config: TutorConfig) -> tuple[int, int]:
    """Split one solve's evidence budget into `(retrieval, reference)` tokens."""
    total = int(config.context_window * SOLVE_RETRIEVAL_SHARE)
    reference = int(total * REFERENCE_SHARE)
    return total - reference, reference
