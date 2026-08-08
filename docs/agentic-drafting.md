# Agentic long-form drafting

Lyra treats a long draft as a durable document-building run, not as one model reply.
The application owns the workflow and gives the configured model short, bounded jobs.
The generated work lives in a separate live suggestion until the writer accepts it.

## Why this shape

Writing guidance and long-form generation research agree on the useful decomposition:

- Purdue's writing-process guidance separates outlining, drafting, and revising, and
  asks each paragraph to develop one focused idea with evidence and transitions.
- Harvard's paragraph guidance treats a paragraph as a bounded unit with a topic,
  development, and a useful handoff. Its transition guidance recommends making the
  logical relationship explicit, often by moving from old information to new.
- PAIR found planning plus iterative refinement more coherent and relevant than direct
  long-form generation. Later work on structured intermediate steps likewise found
  gains in organization, relevance, and verifiability.

References:

- [Purdue OWL: The Writing Process](https://owl.purdue.edu/owl/resources/teaching_resources/documents/the-writing-process-20250724.pdf)
- [Harvard Writing Center: Anatomy of a Body Paragraph](https://writingcenter.fas.harvard.edu/anatomy-body-paragraph)
- [Harvard Writing Center: Transitions](https://writingcenter.fas.harvard.edu/transitions)
- [PAIR: Planning and Iterative Refinement in Pre-trained Transformers for Long Text Generation](https://aclanthology.org/2020.emnlp-main.57/)
- [Integrating Planning into Single-Turn Long-Form Text Generation](https://arxiv.org/abs/2410.06203)

## Fixed workflow

Every full Draft run advances through these stages in order:

1. `gathering` — analyze the assignment, choose a defensible thesis, map the argument,
   retrieve course context, and collect source-bound research notes.
2. `outlining` — create the complete section plan, then turn each section into ordered
   paragraph jobs with stable keys, purposes, claims, evidence, transitions, and word
   budgets.
3. `drafting` — execute one paragraph job at a time. Stream the answer into that live
   block and never ask the model to emit the whole document.
4. `transitions` — review every adjacent paragraph boundary in document order. The
   reviewer sees both paragraphs, both paragraph jobs, and a compact global map.
5. `reviewing` — assess context-sized chunks for assignment coverage, progression,
   contradiction, repetition, support, pacing, tone, and terminology. Findings target
   stable block keys; they never return an unbounded whole-document rewrite.
6. `finalizing` — check structural coverage and length, assemble the live blocks, and
   publish one reviewable pending edit against the run's original base.
7. `completed` — retain the live suggestion and its block history as the run artifact.

The stage list is application code. A model can answer a stage's question, but cannot
skip, reorder, invent, or recursively expand the workflow.

## Context contract

A paragraph call receives:

- the assignment and length target;
- a compact global document map (thesis, ordered sections, and their responsibilities);
- its section plan and exact paragraph job;
- research notes and the source ledger relevant to that job;
- the preceding paragraph when available;
- the next paragraph's purpose, not its unwritten prose; and
- a small explicit word budget.

This is enough context to preserve the document's argument without repeatedly placing
the entire growing document in a small model's context window.

Transition calls receive the global map, the two adjacent paragraph jobs, and the two
actual paragraphs. Chunk review calls receive the global map, block summaries, and a
bounded group of actual paragraphs.

## Live suggestion invariants

- The real draft body is never changed by a running full-draft pass.
- Suggestion blocks have stable identities and monotonically increasing revisions.
- A user patch is compare-and-swap guarded by the revision it was edited from.
- Model replacement cannot overwrite a block after a user edit.
- Streamed model deltas may append to the latest user text, so typing during generation
  does not stop the run or lose the writer's work.
- Every stage and completed block is persisted before the next job starts. A restart
  resumes at a safe block boundary instead of regenerating finished work.
- Finalization diffs against the base captured when the live suggestion was created.
  If the real document changed meanwhile, the existing stale/rebase review rules apply.

## Small-model budgets

Paragraphs, not sections or pages, are the normal generation unit. Target size is kept
well below the endpoint's output ceiling. Planning and review use constrained JSON with
small schemas. Long documents scale by increasing the number of jobs, not the size of a
single completion. Retries are bounded per job, and already persisted blocks survive a
later failure or cancellation.
