"""Intent classification and tool-contract checks for writer chat turns.

The writer chat is allowed to answer ordinary questions inline, but a few request
shapes are only honest when they move through the document tools first: drafting,
revising, reviewing, and source-finding. This module makes that contract explicit so
the route can ask for the right tool path up front and validate the finished turn
after the loop returns.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from backend.core import briefs

DRAFT = "draft"
REVISE = "revise"
REVIEW = "review"
RESEARCH = "research"
QUESTION = "question"

KINDS: tuple[str, ...] = (DRAFT, REVISE, REVIEW, RESEARCH, QUESTION)

_WHOLE_DOCUMENT_HINTS = (
    "whole draft",
    "whole paper",
    "whole essay",
    "whole report",
    "whole document",
    "entire draft",
    "entire paper",
    "entire essay",
    "entire report",
    "entire document",
    "full draft",
    "full paper",
    "full essay",
    "full report",
    "all sections",
    "whole thing",
    "as a whole",
    "overall",
)
_LOCAL_SCOPE_HINTS = (
    "paragraph",
    "sentence",
    "intro",
    "introduction",
    "conclusion",
    "thesis",
    "section",
    "abstract",
    "methods",
    "results",
    "discussion",
)
_REVIEW_HINTS = (
    "review the",
    "review this",
    "review my",
    "review our",
    "review it",
    "review draft",
    "review paper",
    "review essay",
    "review report",
    "feedback",
    "critique",
    "peer review",
    "grade",
    "mark up",
    "line edit",
)
_RESEARCH_HINTS = (
    "research",
    "find sources",
    "find me sources",
    "look up",
    "track down",
    "gather evidence",
    "find papers",
    "find citations",
    "public sources",
    "web sources",
    "compare sources",
    "contradictory sources",
    "contradictions in the sources",
)
_DRAFT_HINTS = (
    "write",
    "draft",
    "compose",
    "generate",
    "continue writing",
    "finish writing",
    "expand",
    "flesh out",
    "turn this into",
    "resume the pass",
    "resume the draft pass",
    "resume the deep pass",
    "deep pass",
)
_REVISION_HINTS = (
    "revise",
    "rewrite",
    "reword",
    "tighten",
    "polish",
    "improve",
    "edit",
    "fix",
    "address this comment",
    "address these comments",
)
_ASSIGNMENT_HINTS = (
    "essay",
    "paper",
    "report",
    "lab report",
    "draft",
    "discussion post",
)
_QUESTION_OPENERS = (
    "how can i ",
    "how should i ",
    "what can i ",
    "what should i ",
    "do you think ",
    "does ",
    "is ",
    "should i ",
    "could i ",
    "what is ",
    "what's ",
    "what does ",
    "why is ",
    "why does ",
    "can you explain ",
    "define ",
    "explain ",
)
_POLITE_ACTION_OPENERS = (
    "can you ",
    "could you ",
    "would you ",
    "will you ",
)
_MULTISPACE_RE = re.compile(r"\s+")

_FAILURES = {
    DRAFT: (
        "I did not route that drafting request into the document pipeline. Please try the "
        "request once more; long drafting belongs in the workspace, not in chat."
    ),
    REVISE: (
        "I did not turn that revision request into a draft suggestion or pass. Please try "
        "the request once more; document changes belong in the workspace, not in chat."
    ),
    REVIEW: (
        "I did not start the review for that request. Please try it once more; whole-draft "
        "reviews belong in the background review flow, not in chat."
    ),
    RESEARCH: (
        "I answered that research request without gathering sources first. Please try it "
        "once more; research requests need the course or web search tools, not memory."
    ),
}

_PROMPTS = {
    DRAFT: (
        "Turn intent: draft. If the student is asking you to draft, extend, continue, or "
        "rework the document across a section or the whole piece, call start_draft_pass "
        "before you answer. Do not write the draft in chat."
    ),
    REVISE: (
        "Turn intent: revise. If the student wants prose changed, use propose_revision for "
        "a localized change, or start_draft_pass when the request is really whole-document."
    ),
    REVIEW: (
        "Turn intent: review. If the student wants feedback on the draft as a draft, start "
        "the background review with start_review before you answer; do not substitute a "
        "shallow chat-only review."
    ),
    RESEARCH: (
        "Turn intent: research. Ground the answer in source-finding tools: search the "
        "course material first, and use public-web tools when they are allowed and needed. "
        "Do not answer a research request from memory."
    ),
    QUESTION: (
        "Turn intent: question. Answer directly in chat, after reading whatever brief, "
        "outline, sections, or comments you actually need."
    ),
}

_RESEARCH_TOOLS = {
    "search_course_material",
    "search_web",
    "fetch_source",
    "record_source_excerpt",
}


@dataclass(frozen=True, slots=True)
class ContractCheck:
    kind: str
    observed_tools: tuple[str, ...]
    satisfied: bool
    failure_message: str | None = None


def classify(message: str) -> str:
    """Classify one writer-chat request into the narrow routing buckets we enforce."""
    text = _normalize(message)
    if not text:
        return QUESTION
    if _is_polite_action_request(text):
        if _is_draft_request(text):
            return DRAFT
        if _is_research_request(text):
            return RESEARCH
        if _is_revision_request(text):
            return REVISE
    if _is_question_request(text):
        return QUESTION
    if _is_review_request(text):
        return REVIEW
    if _is_draft_request(text):
        return DRAFT
    if _is_research_request(text):
        return RESEARCH
    if _is_revision_request(text):
        return REVISE
    return QUESTION


def prompt_contract(kind: str) -> str:
    """The explicit per-turn tool contract to append to the writer prompt."""
    return _PROMPTS.get(kind, _PROMPTS[QUESTION])


def validate(
    kind: str,
    observed_tools: Iterable[str],
    *,
    complete: bool = True,
    pass_started: bool = False,
    review_started: bool = False,
    proposed_edit_id: int | None = None,
) -> ContractCheck:
    """Whether a completed turn satisfied the tool path required by its intent.

    Incomplete turns surface their own error contract from the loop and are not rewritten
    into routing failures here.
    """
    tools = tuple(dict.fromkeys(str(name) for name in observed_tools if str(name).strip()))
    if not complete or kind == QUESTION:
        return ContractCheck(kind=kind, observed_tools=tools, satisfied=True)
    if kind == DRAFT:
        satisfied = pass_started or "start_draft_pass" in tools
    elif kind == REVISE:
        satisfied = (
            proposed_edit_id is not None
            or pass_started
            or "propose_revision" in tools
            or "start_draft_pass" in tools
        )
    elif kind == REVIEW:
        satisfied = review_started or "start_review" in tools
    elif kind == RESEARCH:
        satisfied = any(tool in _RESEARCH_TOOLS for tool in tools)
    else:
        satisfied = True
    return ContractCheck(
        kind=kind,
        observed_tools=tools,
        satisfied=satisfied,
        failure_message=None if satisfied else _FAILURES.get(kind),
    )


def _normalize(text: str) -> str:
    return _MULTISPACE_RE.sub(" ", text.casefold()).strip()


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _has_whole_document_scope(text: str) -> bool:
    return _contains_any(text, _WHOLE_DOCUMENT_HINTS)


def _has_local_scope(text: str) -> bool:
    return _contains_any(text, _LOCAL_SCOPE_HINTS)


def _mentions_length_target(text: str) -> bool:
    return briefs.length_target_words(text, require_unit=True) is not None


def _is_question_request(text: str) -> bool:
    if any(text.startswith(prefix) for prefix in _QUESTION_OPENERS):
        return True
    return text.endswith("?") and not _has_whole_document_scope(text)


def _is_polite_action_request(text: str) -> bool:
    return any(text.startswith(prefix) for prefix in _POLITE_ACTION_OPENERS)


def _is_review_request(text: str) -> bool:
    if not _contains_any(text, _REVIEW_HINTS):
        return False
    if text.startswith(("what is ", "what's ", "what does ", "define ", "explain ")):
        return False
    if _has_whole_document_scope(text):
        return True
    return not _has_local_scope(text)


def _is_research_request(text: str) -> bool:
    return _contains_any(text, _RESEARCH_HINTS)


def _is_draft_request(text: str) -> bool:
    if "deep pass" in text or "draft pass" in text:
        return True
    if _mentions_length_target(text) and (
        _contains_any(text, _DRAFT_HINTS + _REVISION_HINTS)
        or _contains_any(text, _ASSIGNMENT_HINTS)
    ):
        return True
    if _contains_any(text, _DRAFT_HINTS):
        return (
            _has_whole_document_scope(text)
            or _contains_any(text, _ASSIGNMENT_HINTS)
            or _has_local_scope(text)
        )
    return _has_whole_document_scope(text) and _contains_any(text, _REVISION_HINTS)


def _is_revision_request(text: str) -> bool:
    return _contains_any(text, _REVISION_HINTS)
