"""Prompt construction for the tutor model, and the schemas the replies are held to.

Every prompt an LLM sees lives here except the two that travel with an image
(`rag/transcribe.py`) and the eval grader in `scripts/eval_solver.py`.

**These are written for a small local model.** That is the constraint that shapes all of
them, and it is worth stating because it is not the usual one. A frontier model reads a
paragraph of reasoning and follows the argument; a 7B model reads the same paragraph and
pattern-matches its nouns, which means a rule phrased as "do not record the course code"
is, to that reader, a prompt containing the words "course code". So the prompts here are
built rather than written: the fields a document type may not fill are *absent from the
schema*, not forbidden in prose, and the rules that remain are numbered, short, and
followed by a worked example. Anything a deterministic check can settle is settled in
Python instead - which is what `quote` is for, below.

Four things are therefore true of every structured prompt in this module:

1. It asks for exactly the fields the caller can use, assembled per document type.
2. It ships a `JsonSchema` that `llm/client.py` sends as `response_format`, so a server
   that supports constrained decoding cannot leave the shape at all.
3. It shows one worked example built from the same field list, never a hand-written one
   that can drift from the schema beside it.
4. It states what an empty answer looks like, because "omit what you had to reach for"
   is an instruction models obey only when they have been shown that nothing is a
   permitted reply.

Fact filtering is the caller's contract. `build_system_prompt` receives rows that
`backend.core.profiles.select_active_facts` has already filtered, which is the one SQL helper
holding the rule that a rejected fact never enters a prompt and an unconfirmed low-confidence
fact does not either. This module deliberately does not import that helper and does not filter
again: a second copy of the rule is a second place for it to drift.
"""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from backend.llm.client import JsonSchema
from backend.rag.chunk import (
    EXAM,
    GENERIC,
    HOMEWORK,
    LAB,
    LECTURE_NOTES,
    SOLUTIONS,
    SYLLABUS,
    TEXTBOOK,
)

ChatMode = Literal["guide", "show"]

# The version of the tutor's model-facing contract: the base rules, the Guide and Show
# semantics, and the anchored-scope rule, together. The semantic eval corpus
# (`scripts/eval_corpora/tutor_semantic.json`) pins the version it was written against,
# so a prompt change that moves the behavior a case grades shows up as a mismatch the
# harness reports rather than as a silent drift. Version 1 encoded Guide as mandatory
# Socratic questioning with the answer withheld until the student earned it; version 2
# (PLA-401) encodes Guide as a teaching contract - see docs/tutor-prompt-contract.md.
# Bump it when a mode's semantics change, not when wording is polished.
TUTOR_PROMPT_CONTRACT_VERSION = "2"

# Said once, in one wording, in every prompt that parses its reply. It used to be written
# out four times in four places, so a fix to one was a fix to one.
_JSON_ONLY = "Reply with JSON only. No prose, no explanation, and no code fence."

# Likewise for the LaTeX rules, which were stated four times in four different wordings.
_LATEX_RULES = (
    "Write mathematics in LaTeX. Use $...$ for a quantity inside a line of text and "
    "$$...$$ on its own line for a displayed equation. Every LaTeX command belongs inside "
    "those delimiters: a bare \\frac outside them reaches the student as the characters "
    "you typed rather than as a fraction."
)

# Kind order is presentation order. A kind absent from the rows renders no heading at all.
_KIND_HEADINGS: dict[str, str] = {
    "deadline": "Deadlines",
    "topic": "Topics",
    "grading": "Grading",
    "professor": "Professor",
    "prerequisite": "Prerequisites",
    "note": "Conventions this course follows",
}

# How many facts of one kind may enter the system prompt. The profile shares 15% of the
# window with the system prompt itself (see the context budget in docs/rag-pipeline.md), and
# a course with ninety extracted topics would otherwise spend all of it listing them.
MAX_FACTS_PER_KIND = 15

# Roughly half the length it was. The three paragraphs it used to spend on one verbal tic
# were well argued and correct, and they were also most of the prompt: a small model given
# five paragraphs weights them about equally, so the rules that actually matter - do not
# invent a deadline, put mathematics in delimiters - were competing with a style note.
_BASE_PROMPT = (
    """\
You are Lyra, a study tutor for one student. The retrieved context below comes from
documents that student uploaded for this class. It is there for you to use, not to talk
about.

1. Start with the answer, in your own voice. Never open by narrating where your
   information came from: the student knows what they uploaded, so "according to the
   course materials" and "based on the provided context" tell them nothing.
2. Name a source only when the citation is part of the answer - which section a problem
   is from, where a theorem is stated, which document gives a deadline. "This is problem 4
   in section 8.2" is useful; "according to your course materials, the derivative of x
   squared is 2x" is not.
3. When the context does not cover the question, say so plainly. Never invent course
   material, a deadline, or a problem statement. You may then answer from general
   knowledge as long as you say that is what you are doing.
4. """
    + _LATEX_RULES
)
# Guide is a teaching contract, not a response format. Version 1 of this prompt made
# questioning mandatory and the answer a reward: the reply that followed "Explain
# convolution" was a Socratic setup about which values of a variable make two functions
# nonzero, and a student who asked for the answer was told to take a hint first. That is
# the failure PLA-401 exists to remove.
#
# So this block states the contract - what the mode optimizes for, and the handful of
# request shapes a small model will not infer on its own - and stops there. It does not
# enumerate every possible request into a rulebook: a small model given the principle and
# a few patterns generalizes to the rest, and a longer list would recreate the same
# failure as a different one (a tutor that follows the letter of a rule it was handed).
_GUIDE_PROMPT = """\
Mode: Guide.

Guide means the student understands more after this reply than before it. Match the help
to their actual request.

- "Explain X" or "What is X?": explain it; mental model first, then useful formalism or an
  example. Do not ask the student to derive framing you can explain.
- Getting started: give one concrete first move and why; stop at a useful setup, before
  the rest of the solution or final result unless requested.
- Simpler: use less abstraction, not a second full lecture. Keep one familiar picture or
  example; omit the formal definition or notation that caused difficulty.
- Read and diagnose an attempt: what is right, wrong, and why.
- "Just give me the answer": give it with brief justification.

A question is a tool, not a format: never ask one merely because this is Guide, and never
withhold an explanation or answer the student asked for outright. A quick question gets
a quick, complete answer; before an exam, give the essentials."""

_SHOW_PROMPT = """\
Mode: Show.

Give a direct, complete, worked explanation. State the result, then show every step that
leads to it in order, naming the rule or definition each step relies on. Close with a short
summary of the idea worth carrying forward. Do not withhold the answer and do not turn the
reply into a quiz."""


def mode_contract(mode: ChatMode) -> str:
    """The shared Guide/Show teaching contract for every surface that rides the
    conversation's mode.

    The tutoring prompt owns this text (Workstream A, PLA-401). Surfaces such as the
    contextual agent's system prompt call this instead of restating the mode semantics,
    so the contract cannot drift between surfaces: when the contract changes here, every
    surface that inherits the mode changes with it.
    """
    return _GUIDE_PROMPT if mode == "guide" else _SHOW_PROMPT


# --------------------------------------------------------------------------------------
# Profile extraction.
#
# `docs/rag-pipeline.md` describes what this is for; read that before changing it.
#
# The old version of this prompt asked every document for all six kinds and then spent a
# paragraph listing what not to report. Both halves were wrong for a small model. Asking a
# homework sheet for `professor_info` is an instruction to go and find one, and a field
# list is a far stronger signal than a sentence of prose asking for restraint; and the
# paragraph of exclusions is, to a pattern-matcher, a list of the exact nouns to emit.
#
# So the field list is now assembled per document type and the exclusions are mostly gone,
# because a field that is absent from the schema cannot be filled. What is left of the
# prose is the part no schema can express: that a document may not be from this course at
# all.

_TOPICS = "topics"
_NOTES = "notes"
_DEADLINES = "deadlines"
_GRADING = "grading"
_PROFESSOR_INFO = "professor_info"
_PREREQUISITES = "prerequisites"

# Kinds whose entry is a bare name, against kinds whose entry is a label and a value. The
# split matches `_NAMED_KINDS` in `core/profiles.py`, which reads these replies.
_NAMED_FIELDS = frozenset({_TOPICS, _PREREQUISITES})

# What each field means, written as one line because it sits in a numbered list.
_FIELD_SPECS: dict[str, str] = {
    _TOPICS: (
        "the subject matter this document teaches or exercises, each named the way a "
        'textbook index would name it: "Convolution", "Fourier series", "Region of '
        'convergence". One idea per entry, in its plainest form. Never a whole sentence, '
        "never a problem restated, never the course title"
    ),
    _NOTES: (
        "a convention this course holds that a tutor would otherwise get wrong: the sign "
        "or factor convention it uses for a transform, the notation it uses for a standard "
        "quantity, a method it requires or forbids, a standing rule about how work is to "
        "be presented. Only a convention that holds beyond this one document"
    ),
    _DEADLINES: (
        "something this course requires by a date, with the date in the value. An entry "
        "with no date in it is not a deadline and does not belong here"
    ),
    _GRADING: (
        "what determines the grade: the weight of each component, the letter scale, the late policy"
    ),
    _PROFESSOR_INFO: "the instructor's name, contact address, or office hours",
    _PREREQUISITES: (
        "knowledge or software the course assumes the student already has before it starts"
    ),
}

# The example is generated from the same field list the prompt asks for, so it can never
# show a field the schema forbids or miss one the schema requires. `notes` is deliberately
# left empty in every example: a model that has been shown an empty list is enormously more
# willing to return one, and "nothing here" is the correct answer far more often than a
# model reaching to fill six fields will ever produce on its own.
_FIELD_EXAMPLES: dict[str, list[dict[str, str]]] = {
    _TOPICS: [
        {
            "name": "Convolution",
            "quote": "Compute the convolution y(t) = x(t) * h(t) for the signals below.",
        }
    ],
    _NOTES: [],
    _DEADLINES: [
        {
            "label": "Problem Set 3",
            "value": "Friday 14 March, 5pm",
            "quote": "Problem Set 3 is due Friday 14 March at 5pm.",
        }
    ],
    _GRADING: [
        {
            "label": "Final exam",
            "value": "40% of the final grade",
            "quote": "The final exam is worth 40% of the course grade.",
        }
    ],
    _PROFESSOR_INFO: [
        {
            "label": "Office hours",
            "value": "Tuesdays 2-4pm, Room 3.14",
            "quote": "Office hours: Tuesdays 2-4pm, Room 3.14.",
        }
    ],
    _PREREQUISITES: [
        {
            "name": "Linear algebra",
            "quote": "Students are expected to have completed a course in linear algebra.",
        }
    ],
}


@dataclass(frozen=True)
class ExtractionProfile:
    """What one kind of document may be asked about, and how much of it.

    Attributes:
        description: What the document is, and what its topics mean. Two of these carry a
            reuse warning as well, which is the one exclusion no schema can express.
        fields: The payload keys to request, in the order the prompt lists them. A key
            absent here is absent from the prompt, from the example, and from the schema.
        max_topics: Ceiling on the topic list. Without one a model reading a textbook
            returns ninety, and `MAX_FACTS_PER_KIND` then truncates at render time, after
            all ninety have been stored and shown to the student in the profile screen.
    """

    description: str
    fields: tuple[str, ...]
    max_topics: int = 8


# Documents that are routinely photocopied forward from one term to the next. This is the
# case the student named and the one nothing in Lyra used to handle: a practice midterm
# with another professor's name on it, an answer key from a course with a different code.
# Both types are restricted to topics and notes by their field list; this says why, because
# a model that understands the reason stops volunteering the same facts in the topic list.
_REUSE_WARNING = (
    "Documents of this kind are reused between terms and between courses, and the copy "
    "the student has may not have been written for their class at all. Any name, date, "
    "term, course code, or room number printed on it is evidence about some other "
    "offering. Record none of them, anywhere, in any field."
)

EXTRACTION_PROFILES: dict[str, ExtractionProfile] = {
    SYLLABUS: ExtractionProfile(
        description=(
            "This document is the course syllabus. It is the one document that speaks for "
            "the course itself, so its deadlines, its grading policy, its prerequisites, "
            "and its instructor's details are all genuinely this class's. Read it for all "
            "of them."
        ),
        fields=(_TOPICS, _NOTES, _DEADLINES, _GRADING, _PROFESSOR_INFO, _PREREQUISITES),
        max_topics=12,
    ),
    HOMEWORK: ExtractionProfile(
        description=(
            "This document is a homework assignment the student was set. Its topics are "
            "the subjects its problems exercise, and its due date, if it prints one, is "
            "this course's."
        ),
        fields=(_TOPICS, _NOTES, _DEADLINES),
    ),
    LAB: ExtractionProfile(
        description=(
            "This document is a lab handout. Its topics are what the lab exercises, and "
            "its due date, if it prints one, is this course's."
        ),
        fields=(_TOPICS, _NOTES, _DEADLINES),
    ),
    SOLUTIONS: ExtractionProfile(
        description=(
            "This document is a worked solution set or answer key. Its topics are the "
            f"subjects the problems it answers exercise.\n\n{_REUSE_WARNING}"
        ),
        fields=(_TOPICS, _NOTES),
    ),
    EXAM: ExtractionProfile(
        description=(
            "This document is an exam or a practice exam. Its topics are the subjects its "
            f"questions test.\n\n{_REUSE_WARNING}"
        ),
        fields=(_TOPICS, _NOTES),
    ),
    LECTURE_NOTES: ExtractionProfile(
        description=(
            "This document is a set of lecture notes. Its topics are what it teaches, and "
            "the notation and conventions it uses are the course's own."
        ),
        fields=(_TOPICS, _NOTES),
    ),
    TEXTBOOK: ExtractionProfile(
        description=(
            "This document is a textbook or a long reference work. Its topics are the "
            "subjects it covers. A textbook is not a course: it was written for many of "
            "them, so its author, its edition, and any schedule, deadline, or policy it "
            "prints belong to the book and not to this class."
        ),
        fields=(_TOPICS, _NOTES),
        max_topics=12,
    ),
    GENERIC: ExtractionProfile(
        description=(
            "What kind of document this is could not be determined. Be conservative: "
            "record the subjects it covers and any notation convention it states outright, "
            "and nothing else. A document you cannot identify is not evidence about who "
            "teaches this course or when its work is due."
        ),
        fields=(_TOPICS, _NOTES),
    ),
}

# The profile for a document whose type is missing or unrecognised. `generic` is the
# conservative one, which is what an unknown should get.
_DEFAULT_PROFILE = EXTRACTION_PROFILES[GENERIC]

_EXTRACTION_HEADER = """\
You are reading ONE document from a student's course and recording what it says about the
COURSE. You are not summarising the document."""

_EXTRACTION_RULES = """\
Rules. All of them apply to every entry you write.

1. Record only what this document states in words. Do not infer, do not deduce, and do not
   fill a field from what a document of this kind usually contains.
2. Every entry carries a "quote": the sentence or phrase from the document that states it,
   copied exactly, character for character. If you cannot find the words, there is no entry
   to write. The quote is checked against the document afterwards.
3. An empty list is a correct and expected answer. Most documents have nothing to say for
   most of these fields. Returning [] is right; reaching for something to put there is not.
4. Write about the course, never about the document. Its title, its assignment number, its
   page count, and how many problems it has are not facts about the course.
5. One idea per entry, in the plainest words that name it."""


def _entry_shape(field: str) -> str:
    """The object shape one field's entries take, as the prompt states it."""
    return '{"name", "quote"}' if field in _NAMED_FIELDS else '{"label", "value", "quote"}'


def _extraction_fields_block(profile: ExtractionProfile) -> str:
    """The requested fields, with the topic cap attached to the topic line.

    The specs are stored uncapitalised so they read as one clause; only the first letter is
    raised here. `str.capitalize` would lowercase everything after it, which turns the
    `"Convolution"` in the topic spec into `"convolution"` and quietly makes the example of
    a well-named topic an example of a badly-named one.
    """
    lines = ["Return JSON with exactly these fields, and no others:", ""]
    for field in profile.fields:
        spec = _FIELD_SPECS[field]
        if field == _TOPICS:
            spec = f"{spec}. At most {profile.max_topics}, the ones this document is about"
        sentence = f"{spec[:1].upper()}{spec[1:]}."
        lines.append(f'- "{field}": a list of {_entry_shape(field)} objects. {sentence}')
    return "\n".join(lines)


def _extraction_example(profile: ExtractionProfile) -> str:
    """A complete worked reply, built from the same field list the prompt just asked for."""
    payload = {field: _FIELD_EXAMPLES[field] for field in profile.fields}
    return "An example of a well-formed reply:\n\n" + json.dumps(payload, indent=2)


def extraction_schema(doc_type: str | None) -> JsonSchema:
    """The JSON Schema for one document type's reply, for constrained decoding.

    Strict-mode shaped: every property is required and no additional ones are allowed, so
    a server that compiles this to a grammar cannot emit a field this document type is not
    permitted to fill. That is the same rule the prompt states, enforced where a model
    cannot decline to follow it.
    """
    profile = extraction_profile(doc_type)
    properties: dict[str, object] = {}
    for field in profile.fields:
        keys = ["name"] if field in _NAMED_FIELDS else ["label", "value"]
        keys.append("quote")
        properties[field] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {key: {"type": "string"} for key in keys},
                "required": keys,
                "additionalProperties": False,
            },
        }
    return JsonSchema(
        name="course_profile_facts",
        schema={
            "type": "object",
            "properties": properties,
            "required": list(profile.fields),
            "additionalProperties": False,
        },
    )


def extraction_profile(doc_type: str | None) -> ExtractionProfile:
    """What this document type may be asked about. An unknown type gets the careful one."""
    return EXTRACTION_PROFILES.get(doc_type or "", _DEFAULT_PROFILE)


_CONSOLIDATION_PROMPT = f"""\
You are tidying the profile of one course. The numbered entries below were pulled out of the
student's documents one file at a time, so the same thing often appears more than once in
different words, and some of them turned out to describe a file rather than the course.

Return JSON with exactly two fields:

- "duplicates": a list of groups. Each group is a list of the numbers that name the same
  thing, best-worded first. The first number in a group is kept and the rest are folded
  into it.
- "not_about_the_course": a list of the numbers that describe a file rather than the
  course. Nothing is deleted; these are set aside for the student to look at.

Rules. All of them apply to every number you return.

1. Group two entries only when a student would call them one entry written twice.
2. Related is not the same. "Fourier transform" and "inverse Fourier transform" stay
   separate. So do "convolution" and "circular convolution", and so do any two entries
   where one is a special case of the other.
3. An entry belongs in "not_about_the_course" when it names an assignment number, a
   document's title or type, how many problems a file contains, a course code or term, or
   an instruction that applies to one worksheet only.
4. When you are not sure, leave the number out of both lists. Doing nothing is always a
   permitted answer, and both lists may be empty.
5. Every number you return must be one from the list below.
   Do not invent entries, do not rename anything, and do not return a number twice.

Given these entries:

  1. [topic] Time Shift
  2. [topic] Time-Shift Property
  3. [topic] Fourier transform
  4. [topic] Inverse Fourier transform
  5. [note] This is Problem Set 4 of 8
  6. [topic] Convolution

a well-formed reply is:

{{
  "duplicates": [[1, 2]],
  "not_about_the_course": [5]
}}

1 and 2 are one property written twice. 3 and 4 are two different transforms and are left
alone. 6 is grouped with nothing. 5 describes a file.

{_JSON_ONLY}"""

# Every number the reply may contain is checked against the list that was sent, in
# `core/consolidation.py`, so the schema constrains the shape rather than the range: a
# grammar cannot express "an integer that was one of the ones I gave you".
CONSOLIDATION_SCHEMA = JsonSchema(
    name="profile_consolidation",
    schema={
        "type": "object",
        "properties": {
            "duplicates": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "integer"}},
            },
            "not_about_the_course": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["duplicates", "not_about_the_course"],
        "additionalProperties": False,
    },
)

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
- "parts_relation": how those sub-parts relate to each other. "none" when there are none.
  - "separate" when each part is its own question with its own final answer, and
    answering one does not need the answer to another. A stem that hands the same
    question to a list of cases -- "For each system below, determine whether it is
    linear", "Sketch each of the following signals" -- is this: the parts are the cases,
    and the sheet expects one answer per case.
  - "one_solution" when the parts build a single solution: a later part uses an earlier
    part's result, the parts are stages of one derivation, or the problem asks for one
    final answer that all of them lead to.
  Decide it from the sheet, not from how many parts there are. Five parts can be one
  derivation and two parts can be two questions.

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

# `parts_relation` carries a third value the prompt does not offer, because strict-mode
# schemas have no optional properties: a problem with no sub-parts has to answer something,
# and `none` is that answer. `core/segmentation.py` treats anything outside its own two
# values as unstated and falls back to reading the stem, which is exactly right here.
_PART_SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string"}, "statement": {"type": "string"}},
    "required": ["label", "statement"],
    "additionalProperties": False,
}

SEGMENTATION_SCHEMA = JsonSchema(
    name="homework_problems",
    schema={
        "type": "object",
        "properties": {
            "problems": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "number": {"type": "string"},
                        "statement": {"type": "string"},
                        "page": {"type": ["integer", "null"]},
                        "parts": {"type": "array", "items": _PART_SCHEMA},
                        "parts_relation": {
                            "type": "string",
                            "enum": ["separate", "one_solution", "none"],
                        },
                    },
                    "required": [
                        "label",
                        "number",
                        "statement",
                        "page",
                        "parts",
                        "parts_relation",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["problems"],
        "additionalProperties": False,
    },
)


# The segmentation pass restores LaTeX while it lists the problems, but only for the
# problems it lists. A problem it dropped, mis-numbered, read at a coarser grain, or whose
# reading was rejected as a summary keeps the chunker's text, and that text is what PDF
# extraction left: `e^{-2t}u(t-3)` flattened to `e-2tu(t -3)`. This pass exists to catch
# exactly those, one narrow job on text that is already the right problem's own words, so
# it cannot lose or reorder a problem the way a whole-sheet re-reading can. It is handed a
# list of statements with an id apiece and asked to hand each back with its mathematics put
# back, and nothing else changed.
_LATEX_RESTORE_PROMPT = f"""\
Each entry below is a homework problem's text that came out of a PDF, and extraction
flattened its mathematics onto the line: exponents, subscripts, fractions, integrals, and
operators all lost their layout. Put the mathematics back into LaTeX and change nothing
else.

Return JSON with one field, "statements", holding a list with one object per entry you were
given:
- "id": the id that entry was given, copied exactly.
- "statement": that entry's text with its mathematics written in LaTeX, using $...$ for a
  quantity inside a line of text and $$...$$ on its own line for a displayed equation.

This is a transcription, not a rewrite. Keep every word, every number, and the order they
came in. "x(t) = e-2tu(t -3)" is "$x(t) = e^{{-2t}}u(t-3)$", and a starting pair written
"e-2tu(t) <-> 1 2 + jw" is "$e^{{-2t}}u(t) \\longleftrightarrow \\frac{{1}}{{2 + j\\omega}}$".
A run of loose numbers that was a fraction is a fraction; a stack that was an integral is an
integral. Do not solve anything, do not explain, and do not add or drop a single word.

{_JSON_ONLY}"""

LATEX_RESTORE_SCHEMA = JsonSchema(
    name="restored_statements",
    schema={
        "type": "object",
        "properties": {
            "statements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "statement": {"type": "string"},
                    },
                    "required": ["id", "statement"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["statements"],
        "additionalProperties": False,
    },
)


# Assembled by concatenation rather than by `format` or an f-string, and it has to be: the
# body contains `e^{-t}u(t)`, and both of those read a LaTeX brace group as a placeholder.
_SOLVE_BODY = """\
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
  A title is mostly words, but any mathematics in one is mathematics and takes $...$ the
  same as the content does: "Part (a) Convolution of $u(t)$ and $e^{-t}u(t)$", never
  "Part (a) Convolution of u(t) and e^{-t}u(t)". A title is a heading, so it never takes
  display math.
- "answer": the final result, stated plainly. Include units where the problem has them.

Citation rules:

1. Cite in "sources" every numbered entry a step actually used, and only those. A step you
   derived yourself cites nothing, and an empty list is the right answer for it.
2. The numbers go in "sources" and nowhere else. Writing "as shown in [15]" into a step's
   text puts a marker on screen pointing at a list the student cannot see. Name the source
   in words, or say nothing and let the citation carry it.
3. Reference solutions share one numbering sequence with the retrieved context and are
   cited exactly the same way.

Both halves of rule 1 matter: an invented citation tells the student a step is grounded
when it is not, and a missing one tells them their own material went unused when it did.
"""

_SOLVE_PROMPT = "\n".join(
    [
        _SOLVE_BODY,
        f'{_LATEX_RULES} This applies to "answer" as well as to every step.',
        "",
        _JSON_ONLY,
    ]
)

SOLVE_SCHEMA = JsonSchema(
    name="worked_solution",
    schema={
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "sources": {"type": "array", "items": {"type": "integer"}},
                    },
                    "required": ["title", "content", "sources"],
                    "additionalProperties": False,
                },
            },
            "answer": {"type": "string"},
        },
        "required": ["steps", "answer"],
        "additionalProperties": False,
    },
)

_REFERENCE_HEADING = """\
Worked solutions the student already has. Follow their notation, layout, and method: this
is the form their course expects and the form their marker reads.

Where one of them covers the problem you are solving now, it is the authority on the
answer. Follow it, say in the step that you are following it, and if your own working
disagrees with it, say that too rather than quietly picking one. Where they cover
different problems, take the method and not the content."""

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
- "verdict": one of "agrees", "disagrees", or "nothing_to_check".
- "detail": one or two sentences. For "disagrees", name the step, what the solution says,
  and what your check returned. Write about the solution, never about the student.

Which verdict:
- "agrees" only when every check you ran matched the solution.
- "disagrees" when any check contradicted it. One contradicted step is a disagreement even
  if the final answer happens to come out right.
- "nothing_to_check" only when the solution contains no equation, no numeric result, and
  no quantity with units - which is the honest outcome for a proof or a conceptual answer,
  and for nothing else. If the solution contains mathematics, you must run at least one
  tool before answering. Not having run a check is not a reason to use this verdict; it is
  the reason not to.

A check you did not run is not agreement. If you could not settle something, say so in
"detail" rather than letting it pass."""

VERIFICATION_SCHEMA = JsonSchema(
    name="solution_check",
    schema={
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["agrees", "disagrees", "nothing_to_check"]},
            "detail": {"type": "string"},
        },
        "required": ["verdict", "detail"],
        "additionalProperties": False,
    },
)

_STEP_CONTEXT_HEADING = "The student is asking about one step of a solution Lyra wrote:"

# The scope of an anchored conversation, which is narrower than the mode it runs in.
#
# The mode prompts are written to teach, and a conversation anchored to one step of a
# solution keeps teaching past the step: the student asks why one line follows from the
# one above it, gets the answer, and the reply drifts into the next step or offers to
# walk through the rest. They did not ask to be walked through the problem. They asked
# about a step, and the conversation is over when that step makes sense. Offering the
# walkthrough unprompted takes a thirty-second question and turns it into an assignment.
_ANCHORED_SCOPE = """\
Scope. This overrides the mode instructions above wherever the two disagree.

This conversation is about that step and nothing else. Answer what the student actually
asked, then stop. Do not move on to the next step, do not recap the steps before it, and
never offer to work through the rest of the problem. If the student wants that, they will
ask, and then it is theirs to ask for rather than yours to start."""


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


def build_latex_restore_prompt(items: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Build the messages that ask the model to restore LaTeX in flattened statements.

    Args:
        items: `(id, statement)` pairs. The id is echoed back so a reply that drops or
            reorders entries still maps to the right statement rather than the wrong one.

    Returns:
        OpenAI-shaped messages. Best-effort like segmentation itself: a statement the reply
        does not cover keeps the flattened text it had, which is no worse than today.
    """
    body = "\n\n".join(f"id: {item_id}\n{statement}" for item_id, statement in items)
    return [
        {"role": "system", "content": _LATEX_RESTORE_PROMPT},
        {"role": "user", "content": body},
    ]


def build_solve_prompt(
    statement: str,
    label: str,
    *,
    preamble: str = "",
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
        preamble: The instruction above this part, when the problem being solved is one
            part of a set of them. `(b) $y(t) = x^2(t)$` is not a question; it becomes
            one under "For each system below, determine whether it is linear", and
            sending the part without the sentence that asks something of it is sending
            the model an expression and hoping.
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
    sections = [
        f"{label}\n\n{preamble}\n\n{statement}\n\nSolve this part only. Its neighbours are "
        "being solved separately and are not yours to answer."
        if preamble
        else f"{label}\n\n{statement}"
    ]
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


def format_reference_block(documents: list[tuple[str, str]], start_index: int = 1) -> str:
    """Render reference solutions as the labelled examples section of a solve prompt.

    Args:
        documents: `(filename, text)` pairs, already truncated to their share of the
            budget by the caller.
        start_index: The first citation number to use. References share one numbering
            sequence with the retrieved context so a step can cite either, which is what
            makes "grounded in your material" true of a step that followed the answer key
            the student attached.

    Returns:
        The block, or an empty string for no documents so callers can append it
        unconditionally.
    """
    if not documents:
        return ""
    entries = [
        f"[{start_index + offset}] {filename}\n{text}"
        for offset, (filename, text) in enumerate(
            [(name, body) for name, body in documents if body.strip()]
        )
    ]
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

    The scope rule travels with the step rather than living in the mode prompts, because it
    is a property of *where the question was asked from*, not of how it is answered: Show
    has the same appetite for finishing the problem, and both are wrong here.
    """
    heading = f"{_STEP_CONTEXT_HEADING[:-1]}, {label}:" if label else _STEP_CONTEXT_HEADING
    problem_heading = f"The problem, {problem_label}:" if problem_label else "The problem:"
    return f"{heading}\n\n{problem_heading}\n{problem}\n\nThe step:\n{step}\n\n{_ANCHORED_SCOPE}"


def _fact_line(row: sqlite3.Row) -> str:
    """One fact as a prompt line, without a label that only restates its section heading."""
    label = str(row["label"]).strip()
    value = str(row["value"]).strip()
    if not value:
        return f"- {label}"
    # A topic arrives labelled "Topic" under a heading that already reads "Topics". Printing
    # both spends tokens to say nothing and shows the model the shape of the extractor.
    if not label or label.casefold() == str(row["kind"]).casefold():
        return f"- {value}"
    return f"- {label}: {value}"


def _render_facts(facts: list[sqlite3.Row], heading: str) -> str:
    """Render one already-filtered fact list, or an empty string when there is nothing to show.

    Rows arrive from `select_active_facts` ordered by evidence, most-attested first, so the
    per-kind cap keeps the topics the course revolves around and drops the ones a single
    document mentioned once. The profile is orientation; retrieval carries the detail.
    """
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
        lines += [_fact_line(row) for row in rows[:MAX_FACTS_PER_KIND]]
        sections.append("\n".join(lines))
    return f"{heading}\n" + "\n".join(sections)


def build_system_prompt(
    mode: ChatMode,
    user_facts: list[sqlite3.Row],
    class_facts: list[sqlite3.Row],
) -> str:
    """Build the chat system prompt for one turn.

    The system message carries the whole model-facing contract for the turn: the base
    rules, the mode's semantics (contract `TUTOR_PROMPT_CONTRACT_VERSION`, documented in
    `docs/tutor-prompt-contract.md`), and the class profile facts. The pinned step and
    the retrieved context are joined on by the caller.

    Args:
        mode: `guide` for teaching toward understanding - direct explanation, worked
            examples, scaffolding, and questions only when they help - or `show` for a
            direct worked result.
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


def _course_identity(course: Mapping[str, object] | None) -> str:
    """Tell the model what the class already is, so it stops reporting it as a finding."""
    if course is None:
        return ""
    described = ", ".join(
        str(course[key]).strip()
        for key in ("name", "code", "semester")
        if str(course.get(key) or "").strip()
    )
    if not described:
        return ""
    return (
        f"The student has already recorded this class as: {described}. That is settled, so "
        "do not report any part of it back as a fact you found."
    )


def build_extraction_prompt(
    text: str,
    doc_type: str | None = None,
    course: Mapping[str, object] | None = None,
) -> list[dict[str, str]]:
    """Build the profile-extraction messages for one document.

    The whole prompt is assembled from the document type's `ExtractionProfile`: what the
    document is, which fields may be asked for, the example, and the schema all come from
    the same place, so they cannot disagree with each other.

    Args:
        text: Document text, already truncated to the extraction budget by the caller.
        doc_type: What `detect_doc_type` decided. This chooses the profile, and an
            unrecognised or missing type gets the conservative one rather than the
            permissive one it used to get.
        course: The class row, for its `name`, `code`, and `semester`. Naming them is what
            stops every upload proposing them again as if they were discoveries.

    Returns:
        OpenAI-shaped messages. Pair with `extraction_schema(doc_type)` at the call site.
    """
    profile = extraction_profile(doc_type)
    parts = [
        _EXTRACTION_HEADER,
        profile.description,
        _EXTRACTION_RULES,
        _extraction_fields_block(profile),
        _extraction_example(profile),
    ]
    identity = _course_identity(course)
    if identity:
        parts.append(identity)
    parts.append(_JSON_ONLY)
    return [
        {"role": "system", "content": "\n\n".join(parts)},
        {"role": "user", "content": text},
    ]


def build_consolidation_prompt(entries: list[str]) -> list[dict[str, str]]:
    """Build the class-scope consolidation messages.

    Args:
        entries: Fact subjects, already prefixed with their kind by the caller, in the order
            the caller will read the reply's numbers back against.

    Returns:
        OpenAI-shaped messages. Entries are numbered from one, because a model handles a
        short ordinal far more reliably than a database id.
    """
    numbered = "\n".join(f"{index}. {entry}" for index, entry in enumerate(entries, start=1))
    return [
        {"role": "system", "content": _CONSOLIDATION_PROMPT},
        {"role": "user", "content": numbered},
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
        # The path where a document has an outline, the flat title where it does not. A
        # path tells the model which of a book's two sections called `Isomorphisms` this
        # is, which the title on its own cannot.
        section = chunk.get("section_path") or chunk.get("section_title")
        if section:
            label.append(str(section))
        number = chunk.get("section_number")
        if number:
            label.append(f"section {number}")
        problem = chunk.get("problem_number")
        if problem:
            label.append(f"problem {problem}")
        entries.append(f"[{index}] {', '.join(label)}\n{chunk.get('content') or ''}")
    return f"{_CONTEXT_HEADING}\n\n" + "\n\n".join(entries)


_TOPICS_PROMPT = """\
You are mapping course material so a student can study it. Read the material and name
the 4 to 8 topics that together cover what it teaches. A topic is a short noun phrase of
two to four words, specific enough to study ("eigenvalue decomposition", not "math").
Prefer the course's own terminology. Return JSON only."""

TOPICS_SCHEMA = JsonSchema(
    name="study_topics",
    schema={
        "type": "object",
        "properties": {"topics": {"type": "array", "items": {"type": "string"}}},
        "required": ["topics"],
        "additionalProperties": False,
    },
)

# Built per call rather than as one constant: the topic and the card count are the two
# things the model must not be left to guess, so they sit inside the instruction itself.
_FLASHCARDS_PROMPT = """\
You are writing flashcards for the topic "{topic}", grounded in the course material
below. Each card tests one atomic fact or skill: the front is a question or prompt that
forces recall (never yes/no), the back is a complete, self-contained answer a student
could check themselves against. Use the notation the course material uses; write math in
KaTeX ($...$ inline, $$...$$ display). Write {count} cards. Base every card on the
material provided; if the material does not support that many distinct cards, write
fewer rather than inventing content."""

FLASHCARDS_SCHEMA = JsonSchema(
    name="flashcards",
    schema={
        "type": "object",
        "properties": {
            "cards": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "front": {"type": "string"},
                        "back": {"type": "string"},
                        "topic": {"type": "string"},
                    },
                    "required": ["front", "back", "topic"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["cards"],
        "additionalProperties": False,
    },
)

_QUIZ_PROMPT = """\
You are writing a {count}-question quiz at {difficulty} difficulty from the course
material below, using only these question types: {types}. Rules by type - mcq: exactly
four plausible options with one correct; the wrong options must be believable mistakes,
not filler. true_false: the options are exactly ["True", "False"]. fill_blank: the
question contains a ___ blank, the options array holds exactly the one correct answer,
and correct_index is 0. Every question carries a one-or-two-sentence explanation of why
the answer is correct, and a topic label. Use the course's notation; math in KaTeX.
Base every question on the material provided."""

QUIZ_QUESTION_TYPES: tuple[str, ...] = ("mcq", "true_false", "fill_blank")

QUIZ_SCHEMA = JsonSchema(
    name="quiz_questions",
    schema={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": list(QUIZ_QUESTION_TYPES)},
                        "question": {"type": "string"},
                        "options": {"type": "array", "items": {"type": "string"}},
                        "correct_index": {"type": "integer"},
                        "explanation": {"type": "string"},
                        "topic": {"type": "string"},
                        "difficulty": {
                            "type": "string",
                            "enum": ["basic", "intermediate", "exam"],
                        },
                    },
                    "required": [
                        "type",
                        "question",
                        "options",
                        "correct_index",
                        "explanation",
                        "topic",
                        "difficulty",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["questions"],
        "additionalProperties": False,
    },
)


def build_topics_prompt(source_text: str) -> list[dict[str, str]]:
    """Build the messages that map a set of source documents to 4-8 study topics."""
    return [
        {"role": "system", "content": _TOPICS_PROMPT},
        {"role": "user", "content": source_text},
    ]


def build_flashcards_prompt(
    topic: str, context_block: str, cards_per_topic: int
) -> list[dict[str, str]]:
    """Build the messages that write one topic's cards against retrieved course material.

    The context block is `format_context_block` output, so every card the model writes
    can be traced to a labelled chunk of the student's own documents.
    """
    return [
        {
            "role": "system",
            "content": _FLASHCARDS_PROMPT.format(topic=topic, count=cards_per_topic),
        },
        {"role": "user", "content": context_block},
    ]


def build_quiz_prompt(
    source_text: str, count: int, difficulty: str, types: list[str]
) -> list[dict[str, str]]:
    """Build the messages that write one quiz against the gathered source text.

    The per-type rules live in the prompt, but the same rules are enforced again in code
    when the reply is parsed (`core/study.py`): the model's output is a proposal, never
    trusted by construction.
    """
    return [
        {
            "role": "system",
            "content": _QUIZ_PROMPT.format(
                count=count, difficulty=difficulty, types=", ".join(types)
            ),
        },
        {"role": "user", "content": source_text},
    ]


# The craft bar every drafting prompt holds the model to. Distilled from the owner's
# scientific-writing style guide and adapted to student essays.
_WRITING_CRAFT = """\
Write so the reader never has to re-read. Every claim carries exactly the confidence
its evidence earns: "shows" only when it shows, "suggests" when it suggests; never
inflate with "very", "clearly", "obviously", or verdict words like "interesting" or
"important" that do the reader's judging for them. Lead each paragraph with its point;
one idea per paragraph; open sentences with what the reader already knows and close
them with what is new. Cut surplusage: "it is worth noting that X" is "X"; prefer the
plain word (use, not utilize; before, not prior to). Active voice unless the actor
genuinely does not matter. Spell out an abbreviation at first use. Keep the student's
own voice and vocabulary level - polish, do not transplant.

None of this is an instruction to write less. Concision is density, not brevity: it
means every sentence you write earns its place, not that you write fewer of them. When
you are given a length to write to, reach it by developing the material - evidence,
mechanism, worked reasoning, the objection and the answer to it - and never by padding
and never by stopping early. A section that stops short of what it was asked for is not
concise; it is unfinished."""

_WRITE_BODY = """\
You are drafting one passage to insert at the cursor of a document the student is
writing. Ground the passage in the provided course material where it is relevant, and
otherwise write from the instruction alone.

Return only the markdown passage: no preamble, no explanation, and no code fence. The
reply is parsed as document text where it lands, so anything that is not the passage
becomes part of the student's document."""

# Assembled by concatenation rather than `format`: the craft text and the user's draft
# both carry braces and dollars that a format call would read as placeholders.
_WRITE_PROMPT = "\n\n".join([_WRITE_BODY, _WRITING_CRAFT])

_SUGGEST_BODY = """\
You are revising a student's draft per their instruction.

Return the complete revised document as markdown: the entire document, not a fragment,
no preamble, and no code fence. Leave untouched everything the instruction does not
reach - the diff the student reviews is computed from what you return, so an
unnecessary rewrite of an untouched paragraph is noise they must reject. Preserve the
document's heading structure unless asked to change it."""

_SUGGEST_PROMPT = "\n\n".join([_SUGGEST_BODY, _WRITING_CRAFT])


def format_facts_block(facts: list[sqlite3.Row]) -> str:
    """Confirmed class facts for the drafting prompts, or an empty string."""
    return _render_facts(facts, "What you know about this class:")


def format_brief_block(brief: Mapping[str, object] | None) -> str:
    """The draft's brief for the writer prompts, or an empty string when there is none.

    A proposed brief renders with a caveat line instead of being withheld: a guess at
    the assignment beats no idea of the assignment, and the caveat is what keeps the
    model from asserting it back to the student as settled fact.
    """
    if brief is None:
        return ""
    labelled = [
        ("Assignment", brief.get("assignment_type")),
        ("Brief", brief.get("summary")),
        ("Audience", brief.get("audience")),
        ("Length", brief.get("length_target")),
    ]
    lines = [
        f"- {label}: {str(value).strip()}" for label, value in labelled if str(value or "").strip()
    ]
    if not lines:
        return ""
    if brief.get("status") != "confirmed":
        lines.append(
            "- Note: the student has not confirmed this brief. Treat it as your working "
            "guess, and say so when you lean on it."
        )
    return "What this document is:\n" + "\n".join(lines)


def build_write_prompt(
    instruction: str,
    heading: str | None,
    selection: str | None,
    nearby: str | None,
    context_block: str,
    facts_block: str,
    brief_block: str = "",
) -> list[dict[str, str]]:
    """Build the messages for the `/write` inline generation.

    The user message is the instruction first, then whatever the editor gathered around
    the caret (current heading, selected text, surrounding text - whichever exist), then
    the brief, the retrieval context, and confirmed class facts.
    """
    sections = [f"Instruction: {instruction}"]
    surroundings: list[str] = []
    if heading:
        surroundings.append(f"Current section: {heading}")
    if selection:
        surroundings.append(f"Selected text:\n{selection}")
    if nearby:
        surroundings.append(f"Surrounding text:\n{nearby}")
    if surroundings:
        sections.append("\n\n".join(surroundings))
    if brief_block:
        sections.append(brief_block)
    if context_block:
        sections.append(context_block)
    if facts_block:
        sections.append(facts_block)
    return [
        {"role": "system", "content": _WRITE_PROMPT},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


def build_suggest_prompt(
    draft: str,
    instruction: str,
    context_block: str,
    facts_block: str,
    brief_block: str = "",
) -> list[dict[str, str]]:
    """Build the messages that revise a whole draft per the student's instruction.

    The reply becomes the proposed side of the pending edit the student reviews hunk by
    hunk, which is why the prompt forbids a fragment: a partial reply would read as a
    deletion of everything it left out.
    """
    sections = [f"The draft:\n\n{draft}", f"Instruction: {instruction}"]
    if brief_block:
        sections.append(brief_block)
    if context_block:
        sections.append(context_block)
    if facts_block:
        sections.append(facts_block)
    return [
        {"role": "system", "content": _SUGGEST_PROMPT},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


# The structure pass: the first pass over a document, headings before prose. The reply
# replaces or proposes the whole document, so the contract forbids fragments the same
# way the suggest prompt does.
_STRUCTURE_BODY = """\
You are laying out the structure of a document a student is about to write. Return the
complete document as markdown: a heading for each section the assignment needs, and
under each heading exactly one [TODO: ...] marker stating, in a sentence, what that
section will do. No prose outside the TODO markers - a section's intent lives inside
its marker, because the writing happens section by section, later, against the intent
you record now, and prose left here would read as writing already done.

Return only the markdown document: no preamble, no explanation, no code fence.

If the student has already written text, every word of it must survive: place their
prose under the heading where it belongs, unchanged, and add headings and TODOs around
it. Losing or rewording their text is the one failure this pass cannot have.

The structure carries the length. Each section is written separately, later, at roughly
the size you plan for here, so the number of sections you lay out decides how long the
finished document can be: a document asked to run several pages and given three headings
cannot reach it however well those three are written. Where a target length is given
below, plan enough sections to carry it and say in each TODO roughly how many words that
section should run."""

# The section pass: one section at a time, because the whole document does not fit and
# would not be written well in one breath if it did.
_SECTION_BODY = """\
You are writing one section of a student's document. Return the complete section as
markdown, beginning with its heading line exactly as given, and nothing outside the
section: no preamble, no explanation, no code fence. The reply replaces the section
where it stands.

Write the section to do what its intent says. Ground it in the provided course
material where it is relevant, and where a needed fact is not in front of you, write
[TODO: ...] naming what is missing rather than inventing it. Open in a way that
follows from the end of the preceding section, and close in a way the next section's
opening can follow.

Write as much of the complete section as fits cleanly in this reply. The writing
controller may ask for a continuation when the endpoint stops at its output limit; if
it does, that later call will append to this one rather than replace it."""

_SECTION_CONTINUATION_BODY = """\
You are continuing one section that was too long for a single model reply. Return only
new markdown prose to append after the supplied tail. Do not repeat the heading, the
tail, or any earlier paragraph. Begin exactly where the existing prose leaves off and
finish a sentence or paragraph before stopping when possible. Keep following the
original section assignment, evidence constraints, and citation rules."""

# The revise pass: after every section is written, the pass reads the document whole and
# decides what its own first draft got wrong. Code can already see which sections are
# thin or still hold TODO markers; this is for what only a reader can see - a section
# that never does what its heading promised, an argument that skips a step, two sections
# saying the same thing, a join that does not carry.
_REVISE_EVAL_BODY = """\
You are reading a draft you have just written, whole, before handing it to the student.
Name only what a targeted rewrite of one section would fix.

Report a section when: it does not do what its heading and the brief say it should; it
repeats another section rather than advancing on it; it asserts something the draft
never supports; it is markedly thinner than the work its place in the argument requires;
or the handoff into or out of it does not carry.

Do not report matters of taste, and do not report a section merely because it could be
longer - the word counts below are given so you can tell "underdeveloped" from "short
and complete". A draft that holds up is an empty list, and an empty list is a good
answer: say nothing rather than manufacturing a finding.

Return JSON: {"sections": [{"section": "2.1", "problem": "..."}]}. The section is its
number exactly as the outline gives it, and the problem is one sentence saying what is
wrong and what the rewrite should do about it."""

REVISE_SCHEMA = JsonSchema(
    name="draft_revision_targets",
    schema={
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "section": {"type": "string"},
                        "problem": {"type": "string"},
                    },
                    "required": ["section", "problem"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["sections"],
        "additionalProperties": False,
    },
)

# The ghostwriter pipeline keeps each planning question deliberately narrow.  These
# schemas are public constants because the pipeline passes them straight through to
# constrained decoding, and tests can therefore lock the contract independently of a
# particular model's prose habits.
PLAN_BRIEF_SCHEMA = JsonSchema(
    name="writer_plan_brief",
    schema={
        "type": "object",
        "properties": {
            "assignment_type": {"type": "string"},
            "task": {"type": "string"},
            "success_criteria": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["assignment_type", "task", "success_criteria"],
        "additionalProperties": False,
    },
)

PLAN_THESIS_SCHEMA = JsonSchema(
    name="writer_plan_thesis",
    schema={
        "type": "object",
        "properties": {
            "candidates": {"type": "array", "items": {"type": "string"}},
            "selected": {"type": "string"},
            "rationale": {"type": "string"},
        },
        "required": ["candidates", "selected", "rationale"],
        "additionalProperties": False,
    },
)

PLAN_ARGUMENT_SCHEMA = JsonSchema(
    name="writer_plan_argument",
    schema={
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "claim": {"type": "string"},
                "supports": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["id", "claim", "supports"],
            "additionalProperties": False,
        },
    },
)

PLAN_SECTIONS_SCHEMA = JsonSchema(
    name="writer_plan_sections",
    schema={
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string"},
                        "title": {"type": "string"},
                        "job": {"type": "string"},
                        "claim": {"type": "string"},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                        "source_ids": {"type": "array", "items": {"type": "integer"}},
                        "word_budget": {"type": "integer"},
                    },
                    "required": [
                        "ref",
                        "title",
                        "job",
                        "claim",
                        "evidence",
                        "source_ids",
                        "word_budget",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["sections"],
        "additionalProperties": False,
    },
)

# A section plan is still too large a unit for reliable small-model drafting.  The
# paragraph outline is produced one section at a time and becomes the executable work
# queue: every prose call receives one stable job and a deliberately small word budget.
PARAGRAPH_OUTLINE_SCHEMA = JsonSchema(
    name="writer_paragraph_outline",
    schema={
        "type": "object",
        "properties": {
            "paragraphs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "purpose": {"type": "string"},
                        "claim": {"type": "string"},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                        "target_words": {"type": "integer"},
                        "transition_in": {"type": "string"},
                        "transition_out": {"type": "string"},
                    },
                    "required": [
                        "key",
                        "purpose",
                        "claim",
                        "evidence",
                        "target_words",
                        "transition_in",
                        "transition_out",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["paragraphs"],
        "additionalProperties": False,
    },
)

TRANSITION_REVIEW_SCHEMA = JsonSchema(
    name="writer_transition_review",
    schema={
        "type": "object",
        "properties": {
            "needs_change": {"type": "boolean"},
            "rationale": {"type": "string"},
            "revised_next_paragraph": {"type": "string"},
        },
        "required": ["needs_change", "rationale", "revised_next_paragraph"],
        "additionalProperties": False,
    },
)

OVERALL_ASSESSMENT_SCHEMA = JsonSchema(
    name="writer_overall_assessment",
    schema={
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "block_key": {"type": "string"},
                        "problem": {"type": "string"},
                        "revision_instruction": {"type": "string"},
                    },
                    "required": ["block_key", "problem", "revision_instruction"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["summary", "issues"],
        "additionalProperties": False,
    },
)

SKEPTIC_SCHEMA = JsonSchema(
    name="writer_section_skeptic",
    schema={
        "type": "object",
        "properties": {
            "passes": {"type": "boolean"},
            "faults": {"type": "array", "items": {"type": "string"}},
            "rewrite_instruction": {"type": "string"},
        },
        "required": ["passes", "faults", "rewrite_instruction"],
        "additionalProperties": False,
    },
)

RESEARCH_NOTES_SCHEMA = JsonSchema(
    name="writer_section_research_notes",
    schema={
        "type": "object",
        "properties": {
            "notes": {"type": "array", "items": {"type": "string"}},
            "source_ids": {"type": "array", "items": {"type": "string"}},
            "gaps": {"type": "array", "items": {"type": "string"}},
            "relied_on": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "integer"},
                        "excerpt": {"type": "string"},
                    },
                    "required": ["source_id", "excerpt"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["notes", "source_ids", "gaps", "relied_on"],
        "additionalProperties": False,
    },
)

CONTINUITY_SCHEMA = REVISE_SCHEMA


def build_plan_brief_prompt(
    title: str,
    existing_body: str,
    brief_block: str,
    instruction: str | None,
    length_block: str = "",
) -> list[dict[str, str]]:
    """Interrogate the assignment before proposing an argument."""
    user = [f'The document is titled "{title}".', brief_block]
    if instruction:
        user.append(f"The student's request:\n{instruction}")
    if length_block:
        user.append(length_block)
    if existing_body.strip():
        user.append(f"Existing notes or prose:\n{existing_body}")
    return _review_messages(
        (
            "Identify only the assignment type, the exact task, and the concrete "
            "criteria a strong submission must satisfy. "
        )
        + _JSON_ONLY,
        user,
    )


def build_plan_thesis_prompt(
    title: str, brief_analysis: Mapping[str, object], context_block: str
) -> list[dict[str, str]]:
    """Generate a small candidate set and select the defensible thesis."""
    return _review_messages(
        (
            "Propose three distinct, arguable thesis candidates. Select the one best "
            "supported by the available evidence and explain the selection in one sentence. "
        )
        + _JSON_ONLY,
        [f'The document is titled "{title}".', json.dumps(dict(brief_analysis)), context_block],
    )


def build_plan_argument_prompt(
    thesis: str, brief_analysis: Mapping[str, object], context_block: str
) -> list[dict[str, str]]:
    """Turn the selected thesis into an ordered dependency map of claims."""
    return _review_messages(
        (
            "Build the smallest ordered argument map that proves the thesis. Each claim "
            "gets a stable short id; supports lists earlier claim ids it depends on. "
        )
        + _JSON_ONLY,
        [f"Selected thesis: {thesis}", json.dumps(dict(brief_analysis)), context_block],
    )


def build_plan_sections_prompt(
    title: str,
    thesis: str,
    argument_map: list[object],
    total_words: int | None,
    context_block: str,
    existing_outline: str = "",
) -> list[dict[str, str]]:
    """Produce the annotated outline consumed by every downstream stage."""
    length = (
        f"The section word budgets must total about {total_words:,} words."
        if total_words
        else "Choose concise, realistic section word budgets."
    )
    return _review_messages(
        (
            "Turn the argument into an annotated section plan. Every section needs one "
            "job, one claim, evidence to use, exact source ids when known, and a word "
            "budget. Use refs 1.1, 1.2, ... in document order. "
        )
        + _JSON_ONLY,
        [
            f'The document is titled "{title}".',
            f"Selected thesis: {thesis}",
            json.dumps(argument_map),
            length,
            context_block,
            (
                "The document already has this outline. Preserve every heading, its "
                f"order, and its section ref exactly:\n{existing_outline}"
                if existing_outline
                else ""
            ),
        ],
    )


def build_paragraph_outline_prompt(
    title: str,
    document_map: str,
    section_plan: str,
    research_block: str,
    target_words: int,
) -> list[dict[str, str]]:
    """Turn one planned section into paragraph-sized executable jobs.

    Keeping this call section-local prevents a small model from having to emit a large
    nested document plan while the document map preserves its global responsibilities.
    """
    paragraph_count = max(1, round(target_words / 180))
    return _review_messages(
        (
            "Create the complete paragraph outline for this one section. Produce "
            f"roughly {paragraph_count} paragraph jobs whose word budgets total about "
            f"{target_words} words. Each paragraph must do one distinct job, advance "
            "the section claim, name the evidence it will use, and state its logical "
            "handoff from and to neighbouring paragraphs. Use stable keys supplied as "
            "short identifiers, not prose headings. Do not draft prose. "
        )
        + _JSON_ONLY,
        [
            f'The document is titled "{title}".',
            f"Global document map:\n{document_map}",
            f"Section plan:\n{section_plan}",
            f"Research available to this section:\n{research_block}",
        ],
    )


def build_paragraph_draft_prompt(
    title: str,
    *,
    document_map: str,
    section_plan: str,
    paragraph_plan: str,
    research_block: str,
    ledger_block: str,
    previous_paragraph: str | None,
    next_paragraph_summary: str | None,
    target_words: int,
) -> list[dict[str, str]]:
    """Draft exactly one paragraph with local evidence and compact global context."""
    context = [
        f'The document is titled "{title}".',
        f"Global document map:\n{document_map}",
        f"Section plan:\n{section_plan}",
        f"This paragraph's fixed job:\n{paragraph_plan}",
        f"Write about {target_words} words. Write one paragraph only.",
    ]
    if research_block:
        context.append(f"Research for this paragraph:\n{research_block}")
    if ledger_block:
        context.extend(
            [
                ledger_block,
                "Cite only listed sources, using [@lyra:<ID>] immediately after the "
                "claim the source supports.",
            ]
        )
    if previous_paragraph:
        context.append(f"The preceding paragraph:\n{previous_paragraph}")
    if next_paragraph_summary:
        context.append(f"The next paragraph will:\n{next_paragraph_summary}")
    return [
        {
            "role": "system",
            "content": (
                _WRITING_CRAFT
                + "\n\nExecute the supplied paragraph job. Return prose only: no heading, "
                "outline, notes, preface, or explanation. Establish the paragraph's "
                "relationship to the preceding idea through meaning, not a generic "
                "transition phrase. Do not perform work assigned to later paragraphs. "
                "Do not plan or reason about the job; begin the paragraph immediately."
            ),
        },
        {"role": "user", "content": "/no_think\n\n" + "\n\n".join(context)},
    ]


def build_transition_review_prompt(
    title: str,
    *,
    document_map: str,
    previous_plan: str,
    next_plan: str,
    previous_paragraph: str,
    next_paragraph: str,
) -> list[dict[str, str]]:
    """Review one paragraph boundary with enough global context to judge its logic."""
    return _review_messages(
        (
            "Evaluate only the handoff between these adjacent paragraphs. Prefer a "
            "meaningful transition that begins with old information and then introduces "
            "new information. If the relationship is already clear, set needs_change "
            "false and return the next paragraph unchanged. Otherwise revise the next "
            "paragraph only, preserving its facts, citations, purpose, and approximate "
            "length. Do not rewrite the preceding paragraph. "
        )
        + _JSON_ONLY,
        [
            f'The document is titled "{title}".',
            f"Global document map:\n{document_map}",
            f"Previous paragraph job:\n{previous_plan}",
            f"Next paragraph job:\n{next_plan}",
            f"Previous paragraph:\n{previous_paragraph}",
            f"Next paragraph:\n{next_paragraph}",
        ],
    )


def build_overall_assessment_prompt(
    title: str,
    *,
    document_map: str,
    chunk_label: str,
    block_summaries: str,
    prose_chunk: str,
) -> list[dict[str, str]]:
    """Assess one context-sized chunk and return only targeted block instructions."""
    return _review_messages(
        (
            "Act as the document-level editor for this chunk. Check assignment coverage, "
            "argument progression, contradictions, repetition, support, pacing, tone, "
            "and terminology against the global document map. Report only material, "
            "actionable issues. Point every issue at one stable block key and give a "
            "bounded revision instruction; never return a rewritten document. "
        )
        + _JSON_ONLY,
        [
            f'The document is titled "{title}".',
            f"Global document map:\n{document_map}",
            f"Chunk: {chunk_label}",
            f"Block summaries:\n{block_summaries}",
            f"Prose in this chunk:\n{prose_chunk}",
        ],
    )


def format_plan_block(plan: Mapping[str, object] | None, section_ref: str | None = None) -> str:
    """Render persistent plan context, optionally narrowed to one section job."""
    if not plan:
        return ""
    payload = {
        key: plan[key]
        for key in ("brief_analysis", "thesis", "argument_map", "sections", "research_notes")
        if key in plan
    }
    entries = payload.get("sections")
    if section_ref and isinstance(entries, list):
        chosen = [
            {
                key: entry[key]
                for key in (
                    "section_ref",
                    "title",
                    "job",
                    "claim",
                    "evidence",
                    "source_ids",
                    "word_budget",
                    "research_notes",
                )
                if key in entry
            }
            for entry in entries
            if isinstance(entry, Mapping)
            and str(entry.get("section_ref") or entry.get("ref") or "") == section_ref
        ]
        payload["sections"] = chosen
        research_notes = payload.get("research_notes")
        if isinstance(research_notes, Mapping):
            payload["research_notes"] = (
                {section_ref: research_notes[section_ref]} if section_ref in research_notes else {}
            )
        else:
            payload["research_notes"] = {}
    label = "Persistent writing plan"
    if section_ref:
        label += f" (section {section_ref} job)"
    return f"{label}:\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"


def format_ledger_block(entries: list[Mapping[str, object]] | None) -> str:
    """Render source ids and relied-on excerpts without inventing citation syntax."""
    if not entries:
        return ""
    return "Source ledger (cite only these stable source IDs):\n" + json.dumps(
        [dict(entry) for entry in entries], ensure_ascii=False, sort_keys=True
    )


def build_skeptic_prompt(
    title: str,
    section_text: str,
    plan_block: str,
    ledger_block: str,
    previous_tail: str | None = None,
    next_heading: str | None = None,
) -> list[dict[str, str]]:
    """Structured adversarial read for one draft/critique/rewrite iteration."""
    return _review_messages(
        (
            "Act as a skeptical editor. Pass only if the section performs its planned "
            "job, supports its claim with the named evidence and sources, connects "
            "reasoning, carries both seams, meets its word budget, and clears the prose "
            "craft bar. Name only actionable faults and combine them into one targeted "
            "rewrite instruction. "
        )
        + _JSON_ONLY,
        [
            f'The document is titled "{title}".',
            plan_block,
            ledger_block,
            f"Section:\n{section_text}",
            f"Previous tail:\n{previous_tail}" if previous_tail else "",
            f"Next heading: {next_heading}" if next_heading else "",
        ],
    )


def build_research_notes_prompt(
    title: str,
    section_job: str,
    context_block: str,
    ledger_block: str,
) -> list[dict[str, str]]:
    """Distill raw retrieval into persistent, source-bound notes for one section."""
    return _review_messages(
        (
            "Act as the section researcher. Extract only facts, quotations, and reasoning "
            "useful for the named section job. Every note that depends on a source must "
            "name its exact source ID; list honest gaps instead of inventing support. "
            "In relied_on, include only passages you actually selected and used, copied "
            "exactly from the candidate text. Never put summaries, paraphrases, or every "
            "available candidate in relied_on. "
        )
        + _JSON_ONLY,
        [f'The document is titled "{title}".', section_job, context_block, ledger_block],
    )


def build_revise_eval_prompt(
    title: str,
    outline: str,
    word_counts: str,
    seams: str,
    brief_block: str,
    length_block: str,
    plan_block: str = "",
    ledger_block: str = "",
) -> list[dict[str, str]]:
    """The revise stage's one evaluation call over the whole document."""
    sections = [f'The document is titled "{title}".']
    if brief_block:
        sections.append(brief_block)
    if length_block:
        sections.append(length_block)
    if plan_block:
        sections.append(plan_block)
    if ledger_block:
        sections.append(ledger_block)
    sections.append(f"Its outline:\n{outline}")
    sections.append(f"What each section actually runs to:\n{word_counts}")
    if seams:
        sections.append(f"Where each section hands off to the next:\n\n{seams}")
    return [
        {"role": "system", "content": _REVISE_EVAL_BODY},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


_STRUCTURE_PROMPT = _STRUCTURE_BODY

_SECTION_PROMPT = "\n\n".join([_SECTION_BODY, _WRITING_CRAFT])


def format_length_block(total_words: int | None) -> str:
    """The document's length target as a section-count instruction, or nothing.

    The brief renders "Length: 5 pages" as one labelled line among several, which a model
    planning headings reads as background. This says the arithmetic out loud, because the
    section count is the only lever the structure stage has over how long the finished
    document can be.
    """
    if not total_words:
        return ""
    # A section much under ~250 words is a stub and one much over ~450 is really two;
    # the range is what keeps a "five page" document from becoming three vast headings
    # or twenty thin ones.
    fewest = max(3, round(total_words / 450))
    most = max(fewest + 1, round(total_words / 250))
    return (
        f"The finished document should run about {total_words:,} words. Plan roughly "
        f"{fewest} to {most} sections that will be written to that total, and give each "
        "TODO the approximate word count its section should run."
    )


def build_structure_prompt(
    title: str,
    existing_body: str,
    brief_block: str,
    context_block: str,
    facts_block: str,
    instruction: str | None = None,
    length_block: str = "",
) -> list[dict[str, str]]:
    """Build the messages for the pipeline's structure stage.

    The instruction is the student's own words for the pass ("write a five page argument
    about X"). It used to be dropped here and used only as the proposal's note, so a
    length or an emphasis the student typed had no effect on the shape that was planned.
    """
    sections = [f'The document is titled "{title}".']
    if instruction:
        sections.append(f"What the student asked for:\n\n{instruction}")
    if existing_body.strip():
        sections.append(f"What the student has written so far:\n\n{existing_body}")
    if brief_block:
        sections.append(brief_block)
    if length_block:
        sections.append(length_block)
    if context_block:
        sections.append(context_block)
    if facts_block:
        sections.append(facts_block)
    return [
        {"role": "system", "content": _STRUCTURE_PROMPT},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


def build_section_prompt(
    title: str,
    outline: str,
    section_text: str,
    previous_tail: str | None,
    next_heading: str | None,
    instruction: str | None,
    brief_block: str,
    context_block: str,
    facts_block: str,
    target_words: int | None = None,
    plan_block: str = "",
    ledger_block: str = "",
) -> list[dict[str, str]]:
    """Build the messages for one section of the pipeline's drafting stage.

    The section's current text rides in full - for an empty section that is its heading
    and intent, which is the assignment for the run; for an occupied one it is what the
    lens instruction revises. Neighbours arrive as a tail and a heading, not whole
    sections, because the transition is what they are there to carry.

    `target_words` is the document's target divided by its sections. A model writing one
    section of a five-page paper cannot infer from "Length: 5 pages" whether its share is
    200 words or 900, and left to guess it consistently guesses low.
    """
    sections = [f'The document is titled "{title}".', f"Its outline:\n{outline}"]
    if brief_block:
        sections.append(brief_block)
    if plan_block:
        sections.append(plan_block)
    if ledger_block:
        sections.append(ledger_block)
        sections.append(
            "Bind every factual citation marker to a source ID from that ledger. Use "
            "the exact marker [@lyra:<ID>] immediately after the supported claim; never "
            "invent, renumber, or cite an ID that is not listed."
        )
    if target_words:
        sections.append(
            f"Write about {target_words:,} words for this section. That is this "
            "section's share of the document's length, so treat it as the size the "
            "section is meant to be: develop the material to reach it, and do not pad "
            "to reach it."
        )
    sections.append(f"The section to write, as it stands:\n\n{section_text}")
    if previous_tail:
        sections.append(f"The end of the preceding section:\n\n{previous_tail}")
    if next_heading:
        sections.append(f"The next section opens with: {next_heading}")
    if instruction:
        sections.append(f"Instruction for this pass: {instruction}")
    if context_block:
        sections.append(context_block)
    if facts_block:
        sections.append(facts_block)
    return [
        {"role": "system", "content": _SECTION_PROMPT},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


def build_section_continuation_prompt(
    original: list[dict[str, str]],
    section_ref: str,
    section_title: str,
    drafted_tail: str,
    words_written: int,
    words_remaining: int | None,
) -> list[dict[str, str]]:
    """Narrow append-only follow-up for a section that needs another output chunk.

    The original evidence-bearing request remains in the conversation. Only a bounded
    tail of generated prose is repeated, so a small context window does not have to hold
    the growing section twice.
    """
    progress = f"The section currently contains about {words_written:,} words."
    if words_remaining is not None:
        progress += (
            f" Add about {words_remaining:,} more words, developing the remaining "
            "reasoning without padding."
        )
    first = original[0] if original else {"role": "system", "content": _SECTION_PROMPT}
    rest = original[1:] if original else []
    return [
        {
            "role": "system",
            "content": f"{first['content']}\n\n{_SECTION_CONTINUATION_BODY}",
        },
        *rest,
        {
            "role": "user",
            "content": (
                f"Continue section {section_ref} {section_title}. {progress}\n\n"
                "The exact tail already written is below. Do not repeat it; write only "
                f"what follows.\n\n{drafted_tail}"
            ),
        },
    ]


# The reviewer: the adversarial read, delivered as margin comments. The core carries
# kuhn's conventions - be specific, cite, do not rewrite, do not fill gaps - and its
# severity scale with the definitions that make the scale mean something. Each lens
# below prepends its own scope; the core is what makes four lenses one reviewer.
_REVIEWER_CORE = """\
You are reviewing a student's document before their grader does. You are deliberately
critical: find the problems now, specifically, so they can be fixed. You never change
the document - you file findings as margin comments with add_comment, and the fixing
is someone else's job.

How to file:

- One comment per finding, on the most specific passage that shows the problem. Copy
  the quote verbatim from the current text - re-read the section first if you drafted
  the finding from an earlier read. Omit the quote only for a finding about the whole
  document.
- Be specific. "Section 2 claims X but the handout says Y" is a finding; "the methods
  need work" is not. When a finding leans on a source, name the document and where in
  it.
- Distinguish fact from judgment. A contradiction or a missing element is a fact; how
  persuasive a justification reads is a judgment, and the comment should read as one.
- Do not rewrite. Identify the problem and what needs to change, never the new text.
- Do not fill gaps. A claim you cannot verify is filed as exactly that, not assumed
  right or wrong.
- [TODO: ...] markers are the document's own scaffolding for unwritten work. Do not
  review what is inside them.

Severity, one per comment:

- critical: unsupported or contradicted by sources, or a flaw that would sink the
  piece. Must be fixed.
- major: ambiguous, incomplete, or under-justified on a point a grader would flag.
  Should be fixed.
- minor: formatting, clarity, or consistency; the argument survives it.
- note: an observation that could strengthen the piece; not a deficiency.

When you have filed your findings, reply with one short sentence saying how many you
filed, and nothing else. Filing nothing is a legitimate outcome; do not invent a
finding to have one."""

_REVIEW_STRUCTURE_BODY = """\
This pass reviews structure only, from the outline and the brief: does the document
have the sections its assignment type needs, in an order that serves the point?
Missing moves, ordering that buries the argument, imbalance between sections, and
sections that do not serve the brief. Read a full section only when the outline alone
cannot settle a finding. A structural finding rarely has one passage: quote one heading
line from the list below, copied character for character, or omit the quote when the
finding is about the document as a whole. The outline's numbers and word counts are
navigation, not text - they appear nowhere in the document, so a quote built out of them
cannot anchor."""

_REVIEW_ARGUMENT_BODY = """\
This pass reviews the argument at section granularity: does each section earn the one
that follows, are claims sequenced so each stands on what came before, and does the
conclusion follow from what was argued rather than restating it? The seams below show
where each section hands off to the next - judge every handoff, and read a full
section when a seam suggests the fault is inside it. File a transition finding on the
sentence that fails to carry the handoff."""

_REVIEW_PROSE_BODY = """\
This pass calibrates one section's prose against the craft bar below, sentence by
sentence: claims stronger than their evidence, empty intensifiers and verdict words,
surplusage, actors hidden by the passive voice, abbreviations never spelled out. File
each finding on the exact sentence that shows it. Judge this section's own prose
only - its place in the document is reviewed separately."""

_REVIEW_CLAIMS_BODY = """\
This pass checks one section's factual claims against the source ledger. Extract each
claim of fact and each citation, read the cited ledger entry and its recorded excerpts,
and search course material when a course claim needs more context. File a comment wherever
the prose says more than its source does, contradicts it, cites an unknown ledger ID, or
leans on no source at all. Quote the claim, name the ledger source you checked, and say
what it does or does not support. Web and course sources follow the same rule: verify the
claim against the cited ledger entry and never guess beyond its recorded evidence."""

_REVIEW_STRUCTURE_PROMPT = "\n\n".join([_REVIEW_STRUCTURE_BODY, _REVIEWER_CORE])
_REVIEW_ARGUMENT_PROMPT = "\n\n".join([_REVIEW_ARGUMENT_BODY, _REVIEWER_CORE])
_REVIEW_PROSE_PROMPT = "\n\n".join([_REVIEW_PROSE_BODY, _WRITING_CRAFT, _REVIEWER_CORE])
_REVIEW_CLAIMS_PROMPT = "\n\n".join([_REVIEW_CLAIMS_BODY, _REVIEWER_CORE])
_REVIEW_SKEPTIC_PROMPT = "\n\n".join(
    [
        (
            "This is a full skeptical read of one section. Judge whether it performs its "
            "planned job, supports its claim with real ledger evidence, connects the "
            "reasoning, carries its seams, respects its word budget, and clears the prose "
            "craft bar. File one specific margin comment per actionable fault; a clean "
            "section may produce none."
        ),
        _WRITING_CRAFT,
        _REVIEWER_CORE,
    ]
)


def _review_messages(system: str, sections: list[str]) -> list[dict[str, str]]:
    """Assemble one lens's messages from its system prompt and user-message blocks."""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(block for block in sections if block)},
    ]


def build_review_structure_prompt(
    title: str,
    outline: str,
    headings: str,
    brief_block: str,
    facts_block: str,
    plan_block: str = "",
    ledger_block: str = "",
) -> list[dict[str, str]]:
    """The structure lens: the outline and the brief are the whole exhibit.

    The headings ride along verbatim so a finding about a section has something quotable
    in it - the outline is rendered for navigation and none of its lines exist in the
    document.
    """
    return _review_messages(
        _REVIEW_STRUCTURE_PROMPT,
        [
            f'The document is titled "{title}".',
            brief_block,
            plan_block,
            ledger_block,
            f"Its outline:\n{outline}",
            f"Its headings, verbatim - quote from these:\n{headings}" if headings else "",
            facts_block,
        ],
    )


def build_review_argument_prompt(
    title: str,
    outline: str,
    seams: str,
    brief_block: str,
    plan_block: str = "",
    ledger_block: str = "",
) -> list[dict[str, str]]:
    """The argument lens: the outline plus the code-built seams between sections."""
    return _review_messages(
        _REVIEW_ARGUMENT_PROMPT,
        [
            f'The document is titled "{title}".',
            brief_block,
            plan_block,
            ledger_block,
            f"Its outline:\n{outline}",
            f"Where each section hands off to the next:\n\n{seams}",
        ],
    )


def build_review_prose_prompt(
    title: str,
    section_text: str,
    brief_block: str,
    plan_block: str = "",
    ledger_block: str = "",
) -> list[dict[str, str]]:
    """The prose lens, one section at a time."""
    return _review_messages(
        _REVIEW_PROSE_PROMPT,
        [
            f'The document is titled "{title}".',
            brief_block,
            plan_block,
            ledger_block,
            f"The section under review:\n\n{section_text}",
        ],
    )


def build_review_claims_prompt(
    title: str,
    section_text: str,
    brief_block: str,
    plan_block: str = "",
    ledger_block: str = "",
) -> list[dict[str, str]]:
    """The claims lens, one section at a time; retrieval happens through the search tool."""
    return _review_messages(
        _REVIEW_CLAIMS_PROMPT,
        [
            f'The document is titled "{title}".',
            brief_block,
            plan_block,
            ledger_block,
            f"The section under review:\n\n{section_text}",
            (
                "Check every [@lyra:<ID>] marker against the ledger entry with that "
                "exact ID. A missing, unknown, or mismatched ID is a claim finding."
                if ledger_block
                else ""
            ),
        ],
    )


def build_review_skeptic_prompt(
    title: str,
    section_text: str,
    brief_block: str,
    plan_block: str,
    ledger_block: str,
) -> list[dict[str, str]]:
    """Deep review's full rubric, retaining the reviewer tool contract."""
    return _review_messages(
        _REVIEW_SKEPTIC_PROMPT,
        [
            f'The document is titled "{title}".',
            brief_block,
            plan_block,
            ledger_block,
            f"The section under review:\n\n{section_text}",
        ],
    )


# The writer: one assistant for the draft workspace, no modes. It talks like chat and
# works like an editor-in-the-room: reads before advising, grounds in the class's own
# material, and never lands a change except as a proposal the student reviews.
_WRITER_CHAT_PROMPT = """\
You are Lyra, working with a student on a document they are writing. You are their
writing partner: you can research the class material, help them plan, draft passages,
rework what is there, and give a straight editorial read. The document is theirs - your
job is to make their writing better, in their voice, never to take the pen away.

How to work:

- Know what the document is before advising. The brief, when there is one, says so. If
  there is no brief, work it out: the title, what is already written, and the class
  documents (an assignment handout or rubric, if one was uploaded - search for it). Save
  what you conclude with save_brief and say it is your guess until the student confirms.
  If you cannot work it out, ask - one or two plain questions, not an interview.
- Read before you advise. The outline is in front of you; read the sections your answer
  depends on. Advice about a paragraph you have not read is guessing with confidence.
- Ground factual help in the course material. Search when the document leans on the
  class's content, and say which source you used. Do not invent citations or facts.
- Changes are proposals. propose_revision records a suggestion the student reviews hunk
  by hunk; the document does not change until they accept. Never say you changed the
  document - say what you proposed. For a sentence-level idea it is often better to
  quote the rewrite in your reply and let them take it themselves.
- Addressing the reviewer's comments is a loop, not a rewrite: read_comments, then for
  each finding worth acting on, propose the fix and reply to that thread with what you
  proposed - or reply with why you disagree. Work the critical and major findings
  first. Resolving a thread is the student's gesture; yours is the reply.
- Work at whole-document scale through the pipeline, never in the chat window. Any
  request to draft, extend, or rework the document as a whole or across several sections
  - "write the draft", "write me five pages on X", "flesh this out", "tighten the whole
  thing" - is a start_draft_pass call carrying the student's request as its instruction,
  and if they named a length, save_brief that length first. The pass structures the
  document and writes it section by section, which is the only way a document longer
  than a couple of pages gets written well. Answering such a request with a few
  paragraphs in the chat window is the one wrong response: it looks like a refusal to do
  the work, and the paragraphs land nowhere.
- Answer in chat like a colleague: short, specific, in plain words. That is a rule about
  the conversation, not about the document - long prose belongs in a pass or a proposal,
  and it belongs there at full length."""


def build_writer_chat_prompt(
    title: str,
    brief_block: str,
    outline: str,
    facts_block: str,
) -> str:
    """The writer conversation's system prompt.

    The outline rides in the prompt on every turn rather than being left to the tool:
    it is cheap, it is the map the model orients by, and a stale map is worse than the
    tokens it saves. Tools re-read it when the turn itself changes the document.
    """
    parts = [
        _WRITER_CHAT_PROMPT,
        _WRITING_CRAFT,
        f'The document is titled "{title}".',
    ]
    if brief_block:
        parts.append(brief_block)
    parts.append(f"The document right now:\n{outline}")
    if facts_block:
        parts.append(facts_block)
    return "\n\n".join(parts)
