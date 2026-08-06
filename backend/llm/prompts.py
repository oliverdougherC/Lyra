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
# Guide is written to teach a problem end to end, and turned loose on a step it does what
# it was built to do: the student asks why one line follows from the one above it, answers
# the leading question correctly, and is told "perfect, now how do we do the next step?"
# They did not ask to be walked through the problem. They asked about a step, and the
# conversation is over when that step makes sense. Offering the walkthrough unprompted
# takes a thirty-second question and turns it into an assignment.
_ANCHORED_SCOPE = """\
Scope. This overrides the mode instructions above wherever the two disagree.

This conversation is about that step and nothing else. Answer what the student actually
asked, then stop. In Guide, that means at most one leading question rather than one per
step: once they have it, confirm it, answer what they asked, and end the turn.
Do not move on to the next step, do not recap the steps before it, and
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
