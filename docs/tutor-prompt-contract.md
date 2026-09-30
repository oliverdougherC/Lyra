# Tutor prompt contract

**Active contract version: 3** (`backend.llm.prompts.TUTOR_PROMPT_CONTRACT_VERSION`).
The class agent and tutor/anchored chat share one adaptive education contract. The student's
latest request and the conversation determine scope. Stored `guide`/`show` values remain
readable for compatibility, but neither selects a different teaching policy. The education
chat has no answer-style control.

A definition or simple concept gets a direct, compact, accurate answer. A formal definition
keeps necessary field, operation, and axiom conditions. A request for help getting started
gets one useful first move and reason. Attempt feedback checks the student's actual method
and names the first genuine error. An explicit full answer, proof, derivation, or request
for depth is completed without an arbitrary length cap. Follow-up requests narrow or
broaden the response based on the latest question. Routine tool use, recaps, closing
questions, and offers are not automatic.

The base prompt still governs course grounding, citations, missing context, and LaTeX.
Anchored chat stays on its step unless the student explicitly broadens the request.
The agent layer adds capability and trust-boundary instructions; a tool-less turn uses
this same teaching contract. Writer, structured solver, and study-generation prompts
are separate.

`scripts/eval_corpora/tutor_semantic.json` version 2.0.0 pins this contract. Its legacy
mode values test compatibility, not distinct response styles. `scripts/eval_tutor.py
run --surface class_chat` exercises the production planner/tool loop. Retained terminal
answers need independent semantic review; model self-grading and output length alone do
not establish quality. [PLA-461](https://linear.app/platinum-labs/issue/PLA-461) tracks
the bounded live evaluation and its limits.

The [September 29 production-path evidence](adaptive-education-evidence-20260929/README.md)
retains baseline and three candidate runs. The critical concise-definition and first-step
criteria still fail; the contract is implemented, but live quality acceptance remains open.

## Historical records

The material below records earlier Guide/Show work and remains historical evidence.
It does not define the active response policy.

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

## Contract follow-up — September 7

The [bounded learning follow-up](learning-followup-evidence/README.md) distinguishes
calculator execution from mathematical agreement, preserves conditional validity in attempt
feedback, and records real before/after failures. Solver verification stays uncheckable when
a transcript retains a false or unresolved comparison, even if the model says it agrees;
a fresh coherent check is needed rather than inferring that a later unrelated success resolved it.
This conservative rule also downgrades some valid floating-point-to-rational retries, recorded
for human review. Guide language still requires semantic evaluation and is not mechanically certified.

## Contract follow-up — September 8

The [PLA-461 local Guide quality pass](local-guide-quality-20260908.md) rewrites two Guide
bullets without moving the contract: a simpler explanation now shows the same mechanism in
one small concrete example, and attempt diagnosis checks what a student's step actually does
to the expression before naming it wrong, never explaining an error with an invented stricter
rule. Both were measured on the production class_chat loop with the previously failing
attempt-diagnosis and simpler-explanation cases, a fresh held-out variant, and Show/full-solution
controls. Contract version stays 2; the corpus and rubrics are unchanged. This is a
candidate change for parent integration (not yet built): the parent's independent reading of
the retained terminal text found residual issues in the attempt and simpler cases, the
same-model scores are supporting only, and PLA-461 stays open.
