# Phase 2 Handoff

Phase 2 is the homework solver. It is built, and it has been measured against a real course rather
than against its own test fixtures.

This is not a specification. [solver-phase-2.md](solver-phase-2.md) owns the data model, the job,
and the verification rules; [ui-phase-2.md](ui-phase-2.md) owns the screens. Both were kept current
as the phase closed, so where this document and one of those disagree, they win.

What this document holds is the part that does not belong in a specification: what the solver did
when it met eight real problem sets, what that broke, what is still weak, and what a reader picking
up Phase 3 should know before touching any of it.

## What shipped

The build order in solver-phase-2.md, all eight steps.

| Step | What it is                   | Where it lives                                                            |
| ---- | ---------------------------- | ------------------------------------------------------------------------- |
| 1    | Artifact model               | `005_artifacts.sql`, `core/artifacts.py`                                  |
| 2    | Tool loop and the tools      | `llm/tools.py`, `tools/cas.py`, `tools/_cas_runner.py`, `tools/units.py`  |
| 3    | Segmentation and the gate    | `core/segmentation.py`, `core/solver.py`, `SegmentationReview`            |
| 4    | Solving                      | `core/solving.py`, `SolutionWorkspace`, `ProblemPanel`, `SolutionStep`    |
| 5    | Verification                 | `core/verification.py`, `VerdictBadge`, `ToolCallTrace`, `ProvenanceChip` |
| 6    | Corrections and regeneration | `routes_solutions.py` part routes, `MarkWrongDialog`, `RevisionHistory`   |
| 7    | Asking about a step          | `007_session_artifact_part.sql`, `core/sessions.py`, `StepGuidePanel`     |
| 8    | Export                       | Print stylesheet in `globals.css`, `print:` rules through the workspace   |

The two general pieces, the artifact model and the tool loop, are the ones Phase 4 inherits. Neither
mentions homework.

## What it was measured against

`scripts/eval_solver.py` drives the real code path in process: `core.ingestion`, `core.solver`, and
the configured tutor endpoint. Nothing in it reimplements what the product does, so a result is
evidence about the product rather than about the harness. It works in its own workspace directory
with its own database and never touches the student's own data.

The corpus is one term of ECE 203 Continuous-Time Signals: eight problem sets, the professor's
answer key for each, four sets of lecture notes, five labs, a practice midterm and a practice final
with solutions. Thirty-five documents, thirty-four of which ingest. The thirty-fifth is a scanned
Fourier transform table, reported as unsupported rather than silently ingested as empty, which is
the Phase 1 behaviour working as specified and Phase 3's problem to solve.

The tutor endpoint was Qwen3.6 27B on llama.cpp, which is the development baseline in the README.

Ground truth for segmentation is the problem count on each sheet, read off the PDFs by hand.

## Segmentation, measured

Two runs per set, on the code as it ships.

| Set        | On the sheet | Found before | Found now |
| ---------- | ------------ | ------------ | --------- |
| homework_1 | 14           | 14           | 14        |
| homework_2 | 4            | 4            | 4         |
| homework_3 | 12           | 5            | 12        |
| homework_4 | 10           | 5            | 10        |
| homework_5 | 2            | 2            | 2         |
| homework_6 | 5            | 5            | 5         |
| homework_7 | 10           | 10           | 10        |
| homework_8 | 7            | 7            | 7         |

Both runs of every set returned the same count, so the pass is stable at this granularity. One
segmentation takes 22 to 55 seconds against the baseline model, averaging 41, for one to three
pages.

Four faults surfaced, and all four are fixed. They are worth reading as a group, because each one
was invisible to a test suite that supplies its own homework text:

1. **A problem numbered without a full stop was invisible.** `PROBLEM_MARKER` required `[.):]` after
   the number, and `Problem 1 (Time Shift)` has a bracket there instead. Two of the eight sets came
   back with no chunker markers at all. The delimiter is now required only of a bare number.
2. **Numbering that restarts under a section heading collapsed.** `chunked_problems` collected
   chunks into a dictionary keyed by number, so three sections numbered 1 to 3, 1 to 4 and 1 to 5
   became five rows each holding three unrelated statements, and the model pass was reconciled
   straight back down to the same five.
3. **Numbered sub-items were read as problems.** A sheet that writes `Problem 1` above five numbered
   questions numbers both, and taking every marker split one sampling problem into six rows each
   holding one line.
4. **The gate printed the flattened extraction.** The chunker's text is the document's own and it is
   also unreadable: extraction turns e^{-2t}u(t-3) into `e−2tu(t −3)`. The student was being asked
   to check a reading of their homework against text their sheet does not contain.

Solving surfaced two more, both about what reaches the screen rather than what reaches the answer.
The model wrote its context numbers into the prose, so a step read `by the definition [6]` above a
provenance chip that named the file properly two lines below; those markers are now lifted into the
step's sources rather than printed or deleted. And a checker that narrated a sentence around its
JSON had its verdict discarded as unreadable, which is the `unchecked` in the table below.

The first three are in `rag/chunk.py` and `core/segmentation.py` and are covered by tests written
from the sheets that broke them. The fourth is the reconciliation rule described under Two Sources
Of Evidence in solver-phase-2.md.

## Solving and verification, measured

Two sets solved end to end, without their answer keys attached, and then marked against those keys.
`homework_5` is two problems carrying nine lettered sub-parts between them; `homework_7` is ten
Fourier transform property problems.

| Set        | Solved   | Time     | Verdicts                |
| ---------- | -------- | -------- | ----------------------- |
| homework_5 | 2 of 2   | 5.0 min  | 2 verified              |
| homework_7 | 10 of 10 | 14.2 min | 9 verified, 1 unchecked |

**Twelve of twelve answers agree with the professor's key**, which is nineteen distinct results once
the sub-parts are counted. Every one was also checked by hand while writing this document, because a
model marking a model is exactly the kind of evidence this project does not accept on its own.

The run produced 43 steps, 14 of them carrying provenance back to the student's own material, off
112 SymPy and unit calls. Verification is the expensive half: a problem takes about 85 seconds
including its check, and one problem in `homework_7` spent 22 calls on its own.

The single `unchecked` was a checker that narrated a sentence around its JSON report, so a verdict it
had reached was discarded as unreadable. `read_report` now finds a report inside a longer reply, but
that landed after this run and the reply itself was not kept, so the table above stands as measured
rather than being restated as what it would be today.

What the two sets do not include is a refutation. Nothing in twelve problems was wrong, so the
re-derive path, which solver-phase-2.md specifies as running exactly once, was exercised only by
the test suite. It is the largest untested-against-reality path in the phase.

## What is known weak

Written plainly, because the next person to touch this will find these anyway.

- **A sheet with no numbering at all rests entirely on the model pass.** `homework_2` numbers
  nothing: it has four section headings and lettered parts underneath. The chunker contributes
  nothing, the model finds all four, and the reconciliation has nothing to reconcile. That works,
  and it means the deterministic half of the design is doing no work on that shape of sheet. With
  no endpoint configured, or a remote one the student has not acknowledged, that sheet segments as
  paragraphs and the gate shows a list nobody can use. The empty state and the review gate cover it
  honestly; nothing recovers it.
- **A problem that is a figure comes back with an empty statement.** Three problems on `homework_3`
  are block diagrams: the sheet's text for them is literally `1.`, `2.`, `3.`, and their shared
  setup sits above the first of them where it reads as the document's header. Segmentation finds
  three problems and can state none of them, which the gate shows plainly and the student can
  correct. Figure extraction in Phase 3 is what actually fixes this, and the artifact model already
  holds the content type it needs.
- **A granularity disagreement costs the transcription.** When the model reads by section and the
  markers read by problem, the chunker's list stands alone, which means those sheets show the
  flattened extraction at the gate rather than typeset mathematics. Correct structure was the right
  thing to keep and readable notation was the wrong thing to lose; a later pass could ask the model
  to transcribe one problem at a time once the list is settled.
- **Two sets is not a sample.** Twelve problems from one course, one term, one subject, against one
  model. Fourier transform properties are unusually friendly to a computer algebra check, which is
  part of why the verdicts look as good as they do. A proof-based course would land on
  `uncheckable` far more often, and that is the honest outcome rather than a regression.
- **Grounding is thin where it should be thickest.** Fourteen of forty-three steps carried a
  citation. Some of that is correct, a step that applies the differentiation property is not using
  anything the student uploaded, but a solve of a set the course has lecture notes for should be
  reaching them more often than a third of the time.
- **Retrieval is eight chunks per problem, class-scoped.** `K = 8` in `rag/retrieve.py` is a Phase 1
  constant chosen for chat turns. Nothing has measured whether it is the right number for solving,
  and a problem whose method lives in a ninth chunk is solved from the model's own knowledge with
  nothing on screen distinguishing it beyond the missing provenance.
- **Method alignment is not verifiable and is not claimed to be.** There is no automated check for
  "solved it the way the course teaches". What exists is the evidence: retrieval runs over the
  student's own material and every grounded step names its source.
- **The review gate is the safety net, not a formality.** Everything above is survivable because a
  person confirms the problem list before any compute is spent. Removing or automating past that
  gate would make each of these faults expensive rather than visible.

## Deferred, and why

Unchanged from the phase specification, and all three are still the right calls:

- **Figures in solutions** need the structural parsing in Phase 3. The artifact model already holds
  mixed content, so this lands without a migration.
- **Web method lookup** needs the Phase 4 tool set, and a wrong answer retrieved from the web is
  more dangerous than the model's own wrong answer because it arrives with borrowed authority.
- **Overlaying solutions on the source PDF.** Anchored side by side is a fraction of the work for
  most of the value, and it is what shipped.

## Re-running the evaluation

```bash
python scripts/eval_solver.py ingest --fresh --corpus '/path/to/course_files_export'
python scripts/eval_solver.py segment --repeat 2
python scripts/eval_solver.py solve --sets homework_7 homework_5
python scripts/eval_solver.py grade
python scripts/eval_solver.py report
```

Each stage writes its results into the workspace as JSON, so a later stage reads what an earlier one
produced and a run interrupted after forty minutes of solving is not a run thrown away. The
workspace defaults to `data/eval/`, which is gitignored along with the rest of `data/`.

The harness needs what the app needs: the embedding server it starts itself, and a tutor endpoint
configured in the real database, whose row it copies. It turns profile extraction off on the copy,
because a model call per document tells a solver evaluation nothing.

## What Phase 3 and Phase 4 inherit

**Phase 3** starts with the two things this corpus made visible. Scanned documents are still
refused, and the one refused here is a Fourier transform table, which is precisely the sort of
reference a solver wants in retrieval. Textbook-scale ingestion and retrieval remain untested: the
largest document here is 127 chunks.

**Phase 4** inherits the artifact model and the tool loop unchanged. Both were built general and
neither knows what a homework problem is. The loop's guarantees, a depth ceiling, a wall clock, a
registry that is the whole allowlist, and a transcript of every call, are the preconditions for
trusting an agent with tools that are not pure, and the threat model architecture.md requires before
any tool touches the filesystem has not been written yet.
