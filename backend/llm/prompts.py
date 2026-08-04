"""Prompt construction for the tutor model.

Three prompts live here: the chat system prompt, the profile-extraction prompt, and the
retrieved-context block that sits between them and the conversation history, per the prompt
structure in `docs/rag-pipeline.md`.

Fact filtering is the caller's contract. `build_system_prompt` receives rows that
`backend.core.profiles.select_active_facts` has already filtered, which is the one SQL helper
holding the rule that a rejected fact never enters a prompt and an unconfirmed low-confidence
fact does not either. This module deliberately does not import that helper and does not filter
again: a second copy of the rule is a second place for it to drift.
"""

import sqlite3
from typing import Literal

ChatMode = Literal["guide", "show"]

# Kind order is presentation order. A kind absent from the rows renders no heading at all.
_KIND_HEADINGS: dict[str, str] = {
    "deadline": "Deadlines",
    "topic": "Topics",
    "grading": "Grading",
    "professor": "Professor",
    "prerequisite": "Prerequisites",
    "note": "Notes",
}

_BASE_PROMPT = """\
You are Lyra, a study tutor for one student. The retrieved context below comes from
documents that student uploaded for this class. It is there for you to use, not to talk
about.

Answer in your own voice and start with the answer. Do not open a reply by narrating where
your information came from. Phrases like "according to the course materials", "based on the
provided context", or "the documents say" tell the student nothing they do not already know,
and using them every turn makes a tutor sound like a search result. The student knows what
they uploaded.

Cite a source when the citation is itself part of the answer: the section a worked problem
comes from, where a definition or theorem is stated, the document a deadline appears in, or
when the question is about the course rather than the subject, such as what the class covers
or what is due. "This is problem 4 in section 8.2" is useful. "According to your course
materials, the derivative of x squared is 2x" is not.

When the context does not cover the question, say so plainly rather than inventing course
material, deadlines, or problem statements. You may then answer from general knowledge, as
long as you say that is what you are doing.

Write mathematics in LaTeX. Put equations in $$...$$ on their own line for display math; \
reserve $...$ for short inline quantities.
"""
_GUIDE_PROMPT = """\
Mode: Guide.

Teach by Socratic questioning. Open with one leading question aimed at the very next step,
then give one step at a time and wait for the student's attempt before moving on. Do not give
the final answer immediately: withhold it until the student has worked toward it, and only
then confirm their result and fill in what they missed. If they ask outright for the answer,
offer the next hint first."""

_SHOW_PROMPT = """\
Mode: Show.

Give a direct, complete, worked explanation. State the result, then show every step that
leads to it in order, naming the rule or definition each step relies on. Close with a short
summary of the idea worth carrying forward. Do not withhold the answer and do not turn the
reply into a quiz."""

# Quoted verbatim from the "Extraction prompt" block in docs/rag-pipeline.md. Edit that
# document first if this needs to change.
_EXTRACTION_PROMPT = """\
You are analyzing a course document. Extract the following structured information.
Only extract facts that are explicitly stated. Do not infer or guess.
Mark any field you are not certain about with confidence "low".
Return JSON with these fields: deadlines[], topics[], professor_info{}, grading{},
prerequisites[], notes[]"""

_EXTRACTION_SUFFIX = """\
Give every item a "confidence" of either "high" or "low".
Reply with JSON only. No prose, no explanation, and no code fence."""

_CONTEXT_HEADING = "Retrieved context from the student's uploaded material:"

_SEGMENTATION_PROMPT = """\
You are reading a homework assignment and listing the problems in it. You are not solving
anything.

Return JSON with one field, "problems", holding a list in the order they appear. Each
problem has:
- "label": what the sheet calls it, such as "Problem 4" or "Exercise 3.14". Use the
  sheet's own wording, not a number you assigned.
- "number": just the number, as text, such as "4" or "3.14".
- "statement": the problem text, copied as written. Do not summarise it, do not fix it,
  and do not add anything the sheet does not say. When the problem has lettered sub-parts,
  this is the text that introduces them and nothing more; the sub-parts go in "parts" and
  repeating them here prints every one of them twice.
- "page": the page it starts on, as a whole number, or null if you cannot tell.
- "parts": a list of its lettered or numbered sub-parts, each with "label" and
  "statement". An empty list when the problem has none.

Copy statements verbatim. A statement you paraphrased is one the student cannot check
against their own sheet.

Write the mathematics in LaTeX in every statement, the problem's own and each of its
parts, using $...$ for inline quantities and $$...$$ on its own line for a displayed
equation. This is not a rewrite: the text you are given came out of a PDF, and extraction
flattens exponents, subscripts, integrals, and fractions into the line. "x(t) = e-2tu(t
-3)" on the sheet is $x(t) = e^{-2t}u(t-3)$, and writing it as the former is the
paraphrase, not the latter. A piecewise definition that arrived as a run of loose numbers
is a cases environment. Restore what the layout carried and change nothing else: keep the
sheet's own wording, its numbering, and its order.

Course headers, due dates, and general instructions belong to no problem. Leave them out
rather than attaching them to the first one.

Reply with JSON only. No prose, no explanation, and no code fence."""


_SOLVE_PROMPT = """\
You are solving one homework problem for the student whose course material is quoted
below. Solve it completely and correctly.

Follow the method this course teaches. The retrieved context is what the student's own
lectures, textbook, and worked examples say; where it shows a technique for this kind of
problem, use that technique rather than the one you would reach for by default, and name
it. Where the context does not cover the problem, solve it with general knowledge and say
which step you did that on.

Return JSON with two fields:
- "steps": a list, in order. Each step has "title", a short phrase naming what the step
  does; "content", the working for that step written in markdown with LaTeX for
  mathematics; and "sources", a list of the bracketed context numbers that step relies
  on, or an empty list when it relies on none.
- "answer": the final result, stated plainly. Include units where the problem has them.

Only cite a context number in "sources" if that entry is what the step actually rests on.
An invented citation is worse than none, because the student is told the step is grounded
when it is not.

Write mathematics in LaTeX. Put equations in $$...$$ on their own line for display math; \
reserve $...$ for short inline quantities.

Reply with JSON only. No prose, no explanation, and no code fence."""

_REFERENCE_HEADING = """\
Solutions the student already has, as examples of the notation, layout, and method their
course expects. Follow their style. Do not copy their content: they are a different
problem."""

_VERIFY_PROMPT = """\
You are checking a solution that has already been written. You are not rewriting it and
you are not being asked whether you would have solved it differently.

Use the tools to check the claims the solution makes: evaluate the algebra, redo the
integrals and derivatives, solve the equations, and check that quantities carry the units
they should. Check the final answer against the problem's own numbers. Run a tool for
anything a tool can settle rather than judging it by eye.

Ask for every check you can see the need for in the same turn rather than one at a time.
A problem with several lettered parts is several checks, and requesting them together is
what lets you finish checking all of them.

When you have finished checking, reply with JSON and nothing else:
- "verdict": "agrees" if every check you ran matched the solution, "disagrees" if a check
  contradicted it, or "nothing_to_check" if the solution contains no claim a tool could
  settle, which is the honest outcome for a proof or a conceptual answer.
- "detail": one or two sentences. For "disagrees", name the step, what the solution says,
  and what your check returned. Write about the solution, never about the student.

A check you did not run is not agreement. If you could not settle something, say so in
"detail" rather than letting it pass."""

_STEP_CONTEXT_HEADING = "The student is asking about one step of a solution Lyra wrote:"


def build_segmentation_prompt(text: str, filename: str) -> list[dict[str, str]]:
    """Build the messages that ask the model to list a homework set's problems.

    The filename is included because a sheet's own numbering is often only legible
    alongside what the file is called: `hw4.pdf` numbering its problems 1 to 5 is a
    different reading from `chapter4.pdf` doing the same.

    Args:
        text: Document text, already truncated to the segmentation budget by the caller.
        filename: Original upload filename.

    Returns:
        OpenAI-shaped messages. The result is a proposal that a person reviews before any
        solving happens, so the parser downstream tolerates a loose reply.
    """
    return [
        {"role": "system", "content": _SEGMENTATION_PROMPT},
        {"role": "user", "content": f"File: {filename}\n\n{text}"},
    ]


def build_solve_prompt(
    statement: str,
    label: str,
    *,
    sub_parts: list[tuple[str, str]] | None = None,
    context_block: str = "",
    reference_block: str = "",
    correction: str = "",
) -> list[dict[str, str]]:
    """Build the messages that ask the model to solve one problem.

    Tools are deliberately not attached to this turn. Solving has to work against any
    OpenAI-compatible endpoint, including one that does not implement `tools` at all;
    checking is a separate pass, which is also what makes it worth anything.

    Args:
        statement: The problem text, as confirmed at the review gate.
        label: What the sheet calls it, used so the model's own wording matches.
        sub_parts: Lettered sub-parts as `(label, statement)`, solved in the same turn
            because they share context.
        context_block: Retrieved course material, already numbered by
            `format_context_block`. The step `sources` field cites into it.
        reference_block: Reference solutions, already rendered by
            `format_reference_block`.
        correction: What the student says is wrong with the previous attempt. Present only
            on a re-solve, and placed last so it is the most recent instruction the model
            reads.

    Returns:
        OpenAI-shaped messages.
    """
    sections = [f"{label}\n\n{statement}"]
    if sub_parts:
        sections.append(
            "Sub-parts, all of which this solution must answer:\n"
            + "\n".join(
                f"{part_label} {part_statement}" for part_label, part_statement in sub_parts
            )
        )
    if reference_block:
        sections.append(reference_block)
    if context_block:
        sections.append(context_block)
    if correction:
        # Last, and named as the student's own words. A correction buried above the
        # course material reads to the model as one more piece of context rather than as
        # the reason it is being asked again.
        sections.append(
            "The student read your previous attempt at this problem and said this was "
            f"wrong with it. Take it as correct and solve the problem again:\n\n{correction}"
        )
    return [
        {"role": "system", "content": _SOLVE_PROMPT},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


def build_verification_prompt(
    statement: str, label: str, solution: str, *, refutation: str = ""
) -> list[dict[str, object]]:
    """Build the messages that ask the model to check a finished solution, with tools.

    Args:
        statement: The problem as confirmed at the gate.
        label: What the sheet calls it.
        solution: The written solution, steps and answer together.
        refutation: What the previous check concluded, present only when this is the
            second pass over a re-derived solution. Included so the checker looks hardest
            at the place that already failed.

    Returns:
        Messages shaped for `complete_with_tools`, which is why the value type is `object`
        rather than `str`: the same list later carries assistant turns holding tool calls.
    """
    sections = [f"{label}\n\n{statement}", f"The solution to check:\n\n{solution}"]
    if refutation:
        sections.append(
            "An earlier check of this problem disagreed with the solution, and it was "
            f"re-derived after that. What the earlier check said:\n\n{refutation}"
        )
    return [
        {"role": "system", "content": _VERIFY_PROMPT},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


def format_reference_block(documents: list[tuple[str, str]]) -> str:
    """Render reference solutions as the labelled examples section of a solve prompt.

    Args:
        documents: `(filename, text)` pairs, already truncated to their share of the
            budget by the caller.

    Returns:
        The block, or an empty string for no documents so callers can append it
        unconditionally.
    """
    if not documents:
        return ""
    entries = [f"--- {filename}\n{text}" for filename, text in documents if text.strip()]
    if not entries:
        return ""
    return f"{_REFERENCE_HEADING}\n\n" + "\n\n".join(entries)


def format_step_context(
    problem: str, step: str, label: str | None = None, problem_label: str | None = None
) -> str:
    """Render the step a session is anchored to, pinned into its system prompt.

    The step and the problem it belongs to are both included, both with their own labels:
    a step read without its question is ambiguous, and the student clicked it precisely
    because they are looking at both. The labels are the sheet's own wording, so the
    model and the student refer to the same thing by the same name.
    """
    heading = f"{_STEP_CONTEXT_HEADING[:-1]}, {label}:" if label else _STEP_CONTEXT_HEADING
    problem_heading = f"The problem, {problem_label}:" if problem_label else "The problem:"
    return f"{heading}\n\n{problem_heading}\n{problem}\n\nThe step:\n{step}"


def _render_facts(facts: list[sqlite3.Row], heading: str) -> str:
    """Render one already-filtered fact list, or an empty string when there is nothing to show."""
    if not facts:
        return ""
    grouped: dict[str, list[sqlite3.Row]] = {}
    for fact in facts:
        grouped.setdefault(str(fact["kind"]), []).append(fact)

    ordered = list(_KIND_HEADINGS) + [kind for kind in grouped if kind not in _KIND_HEADINGS]
    sections: list[str] = []
    for kind in ordered:
        rows = grouped.get(kind)
        if not rows:
            continue
        lines = [f"{_KIND_HEADINGS.get(kind, kind.capitalize())}:"]
        lines += [f"- {row['label']}: {row['value']}" for row in rows]
        sections.append("\n".join(lines))
    return f"{heading}\n" + "\n".join(sections)


def build_system_prompt(
    mode: ChatMode,
    user_facts: list[sqlite3.Row],
    class_facts: list[sqlite3.Row],
) -> str:
    """Build the chat system prompt for one turn.

    Args:
        mode: `guide` for Socratic tutoring, `show` for a direct worked explanation.
        user_facts: Active facts about the student, already filtered by the caller.
        class_facts: Active facts about this class, already filtered by the caller.

    Returns:
        The system prompt. Fact sections are omitted entirely when their list is empty, so
        the model never sees a bare heading with nothing under it.
    """
    parts = [_BASE_PROMPT, _GUIDE_PROMPT if mode == "guide" else _SHOW_PROMPT]
    user_block = _render_facts(user_facts, "What you know about the student:")
    if user_block:
        parts.append(user_block)
    class_block = _render_facts(class_facts, "What you know about this class:")
    if class_block:
        parts.append(class_block)
    return "\n\n".join(parts)


def build_extraction_prompt(text: str) -> list[dict[str, str]]:
    """Build the profile-extraction messages for one document.

    Args:
        text: Document text, already truncated to the extraction budget by the caller.

    Returns:
        OpenAI-shaped messages. The parser downstream is defensive regardless, but asking
        for bare JSON keeps the common case clean.
    """
    return [
        {"role": "system", "content": f"{_EXTRACTION_PROMPT}\n\n{_EXTRACTION_SUFFIX}"},
        {"role": "user", "content": text},
    ]


def format_context_block(chunks: list[dict[str, object]]) -> str:
    """Render retrieved chunks as the labelled context section of the prompt.

    Args:
        chunks: Retrieved chunks carrying `content`, `filename`, `page_number`,
            `section_title`, and `problem_number`.

    Returns:
        The context block, or an empty string for no chunks so callers can append it
        unconditionally.
    """
    if not chunks:
        return ""
    entries: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        label = [str(chunk.get("filename") or "Unknown document")]
        page = chunk.get("page_number")
        if page is not None:
            label.append(f"page {page}")
        section = chunk.get("section_title")
        if section:
            label.append(str(section))
        problem = chunk.get("problem_number")
        if problem:
            label.append(f"problem {problem}")
        entries.append(f"[{index}] {', '.join(label)}\n{chunk.get('content') or ''}")
    return f"{_CONTEXT_HEADING}\n\n" + "\n\n".join(entries)
