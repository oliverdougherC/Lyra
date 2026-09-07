# Tutor prompt contract

The model-facing contract for the tutor chat: what the system prompt tells the model, what
each mode means, and how the behavior is evaluated. **Contract version: 2**
(`backend.llm.prompts.TUTOR_PROMPT_CONTRACT_VERSION`), established by PLA-401.

## What changed from version 1

Version 1 encoded Guide as a response format: *open with a leading question, give one step
at a time, withhold the answer until the student earns it, offer a hint when the answer is
asked for.* The failure it produced: a student who asks "Explain convolution" gets an
indirect Socratic setup (which values of a variable make two functions nonzero) instead of
the explanation they asked for. Version 2 encodes Guide as a **teaching contract** - a
statement of what the mode optimizes for, the request shapes a small model will not infer
on its own, and an explicit ban on withholding a directly requested answer. The mode
instructions now agree with the base rule they sit under ("start with the answer, in your
own voice") instead of contradicting it.

## The contract, semantically

### Guide

Guide optimizes for **understanding and productive progress**. The reply succeeds when the
student understands more after it than before it. The tutor chooses the move that serves
the student's actual question:

- explain directly;
- work one example;
- scaffold the next step;
- diagnose an attempt the student sent;
- check that they followed;
- ask a question **when it tells the tutor what to teach next**.

Hard rules that survive as rules (everything else is judgment):

1. **A question is a tool, not a format.** Never ask one merely because Guide is active.
2. **Nothing is withheld on demand.** An explanation or answer the student asked for
   outright is given, preferably with a concise justification.
3. **Proportionality.** Detail matches the request and the time the student has; a quick
   question gets a quick, complete answer, and a short window before an exam gets the
   essentials.

Request shapes the prompt names explicitly, and the behavior they map to:

|Request|Behavior|
|---|---|
|"Explain X" / "What is X?"|Explain it; mental model first (the idea in a sentence or two), then the formalism or an example. Never make the student derive framing the tutor can simply explain.|
|An attempt is supplied|Acknowledge valid work, locate the first invalid transition, explain exactly what changed or was lost, and correct it within the student’s method. Do not blame an operation that is valid when applied correctly.|
|"Just give me the answer"|Give it, with the briefest justification that makes it trustworthy.|

The prompt also names first-step help, simpler explanations, and explicit requests to omit
questions. First-step help stops at a useful setup; simpler explanations reduce abstraction.
Other requests are handled using the same scope and teaching principles. The class-chat tool
layer limits verification claims to the expressions and conditions actually checked: a
successful calculation does not certify its inputs, assumptions, bounds, or surrounding prose.

### Show

Show optimizes for a **complete worked result**: state the result, show every step in order
naming the rule each step relies on, close with the idea worth carrying forward. It must
not withhold the answer and must not turn the reply into a quiz. Show is a format, so it
stays stated as one.

### Anchored scope

A conversation anchored to one step of a solution carries `_ANCHORED_SCOPE` above the mode
instructions: answer the step the student asked about, then stop. Do not move on to the
next step, do not recap earlier steps, never offer to walk through the rest of the
problem. Version 1 of this block also carried a Guide question budget ("at most one
leading question"); version 2 dropped the budget with the Socratic mode it was sized for.

## What is in the system message

Assembled by `build_system_prompt` and joined by the chat route (`routes_chat._build_turn`):

1. `_BASE_PROMPT` - the shared rules: answer first, cite only when the citation carries
   information, say plainly when the context does not cover the question and never invent
   course material, LaTeX delimiters.
2. `_GUIDE_PROMPT` or `_SHOW_PROMPT` - the mode contract above.
3. Class profile facts (student facts, then class facts), omitted entirely when empty.

The route then joins, when present: the pinned step (`_STEP_CONTEXT_HEADING` +
`format_step_context` + `_ANCHORED_SCOPE`) and the retrieved context block
(`format_context_block`). The tutor conversation carries no tool definitions.

## Invariants this pass preserved

- **RAG grounding**: the retrieved context still rides in the system message, rendered by
  the same `format_context_block`, and the base rule "say so plainly when the context does
  not cover the question" is unchanged.
- **Class-profile usage**: fact filtering is still the caller's contract
  (`profiles.select_active_facts`); `build_system_prompt` still takes pre-filtered rows and
  still omits empty sections.
- **Privacy/remote-consent behavior**: untouched; that gate lives in `routes_chat`
  (`require_document_allowed`) and `app_settings`.
- **Citations/provenance**: the base citation rule is unchanged; the context renderer is
  unchanged.
- **Context-window budgeting**: the turn budget, history trim, and retrieval fit check in
  `routes_chat` are unchanged. Current prompt size is checked by the budget tests; measured token counts from the
  original audit are not a guarantee for later prompt revisions.
- **Retries/regeneration/concurrency, solution handoffs, safety invariants**: no route
  logic changed; only the mode prompt text, one comment, and the anchored-scope wording.

## How the behavior is evaluated

Prompt *behavior* is not proved by exact-string tests; it is held to a versioned semantic
contract:

- **`scripts/eval_corpora/tutor_semantic.json`** - the corpus. `corpus_version`
  (`1.2.0`) and `prompt_contract_version` (must equal
  `TUTOR_PROMPT_CONTRACT_VERSION`). Thirteen cases covering at minimum the request shapes
  PLA-401 names: explain convolution, what is a derivative, why can they cancel these
  terms, I have no idea how to start, here's my attempt, is my answer correct, just tell me
  the answer, explain that more simply, don't ask me questions, intuition not derivation,
  five minutes before an exam - plus Show-mode contrast cases.
- **`scripts/eval_tutor.py`** - the harness. `run` sends each case through the same
  building blocks the route uses and durably records every terminal result - ok, empty,
  and failed cases alike land in `runs.json`, so a run in which every case failed still
  leaves a record `grade` can consume. The run metadata carries a locality class (local
  or remote, via the same conservative rule as the consent gate), the model identity, and
  the context-window configuration - never the endpoint URL itself. `grade` asks a
  grader model for per-item judgments on the case's `must`/`must_not` behaviors and on
  the seven semantic qualities (directness, proportionality, prerequisite setup,
  questioning, withholding, correctness, pedagogical usefulness); the judge defaults to
  the tutor's own endpoint and model, and `grade_meta` records that - or the separate
  `--judge-source-db` / `--judge-model` configuration when one is given - so a report
  says what graded it. The **pass/fail verdict is computed deterministically** (all
  `must` met, no `must_not` violated, correctness not a hard failure) rather than taken
  from the grader. `report` prints the matrix and exits nonzero below `--fail-under`.
- **`backend/tests/test_eval_tutor.py`** - the deterministic contract tests: the corpus
  loads versioned against the current contract, every required request shape is covered,
  the convolution regression case keeps its contract, the harness assembles the same
  messages the route assembles (checked against `routes_chat._build_turn` per case), and
  the verdict arithmetic is pinned.
- **`backend/tests/test_prompts.py`** - the prompt-level guards, including
  `test_explain_convolution_is_explained_not_interrogated`, which protects the motivating
  failure at the prompt surface.

Bumping `TUTOR_PROMPT_CONTRACT_VERSION` means the semantics moved; the corpus and the
eval run then need a re-read against the new version, which the harness reports.

## Historical model-facing instruction audit (PLA-401, 2026-09-02)

This table describes that audit revision. Later compact-prompt evaluation is recorded in
[release guide prompt evidence](release-guide-prompt-evidence.md).

Inventory of the surfaces this pass examined, and the findings:

|Surface|Location|Finding|
|---|---|---|
|Tutor base rules|`prompts.py:_BASE_PROMPT`|Sound; v1's Guide block contradicted rule 1 ("start with the answer"). v2 removes the contradiction. Unchanged.|
|Guide mode|`prompts.py:_GUIDE_PROMPT`|**Rewritten** (v1: mandatory Socratic questioning + answer withholding + hint-first).|
|Show mode|`prompts.py:_SHOW_PROMPT`|Already compliant ("do not withhold the answer and do not turn the reply into a quiz"). Unchanged.|
|Anchored scope|`prompts.py:_ANCHORED_SCOPE`|**Revised**: the scope rule survives; the Guide "one leading question" budget is removed with the mode it was sized for.|
|Context rendering|`prompts.py:format_context_block`|Semantic, not UI-coupled. Unchanged.|
|Route-local instructions|`routes_chat.py`|The tutor turn carries no route-local prompt text beyond the system prompt, pinned step, and context block, and no tool definitions. One stale comment ("a Socratic reply") fixed.|
|LLM client|`llm/client.py`|Transport. Capability-probe prompts (vision, tool support) are not tutoring surfaces. Unchanged.|
|Transcription|`rag/transcribe.py`, `llm/ocr_server.py`|Mechanical transcription prompts; no tutoring pedagogy, no UI wording. Unchanged.|
|Study generation|`prompts.py` topics/flashcards/quiz|Generates study content; the quiz prompt intentionally generates questions. No tutoring leakage. Unchanged.|
|Solver/verification|`prompts.py` solve/verify|Separate pipeline; shares nothing with the chat mode prompts except LaTeX rules, which are unchanged. Unchanged.|
|Writer|`prompts.py` writer chat + drafting pipeline|One assistant, no modes by design; no Socratic tutoring. Unchanged (one stale doc reference fixed).|
|Agent + tools|`routes_agent_chat.py`, `core/agent_tools.py`|Not a tutor surface (the tutor conversation has no tools). Tool-description/semantic-event wording is inventoried here for the record; changes belong to the agent workstream.|
|UI wording about the tutor|`frontend/src/components/chat/chat-pane.tsx`|The Guide/Show hints described v1 ("asks leading questions and holds back the answer"). **Updated** to describe v2.|

## September 6 beta quality work

[Learning beta evidence](learning-beta-evidence/README.md) retains current-main baselines,
candidate repeats, held-out transfer cases and separate independent-agent review. Corpus
1.2.0 corrects an inaccurate factoring criterion; it is used for both baseline and candidate
grading. Model self-judging is supporting evidence and cannot waive a critical semantic failure.
