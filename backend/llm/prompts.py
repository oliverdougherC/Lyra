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

Write mathematics in LaTeX. Put equations in $$...$$ on their own line for display math; reserve $...$ for short inline quantities.
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
