# Homework Solver Specification

Phase 2. This document owns the solver's data model, job architecture, segmentation, solving,
verification, and API. [ui-phase-2.md](ui-phase-2.md) owns its screens. Where the two disagree, this
one wins on behavior and that one wins on layout.

Two pieces specified here are deliberately general rather than solver-shaped, because Phase 4
depends on both: the **artifact model** and the **tool-calling loop**. They are built now because
the solver is their first consumer, not because they belong to it.

## What the solver is

Phase 1 answers questions. The solver produces something: a student uploads a problem set and gets
back a complete, checked, editable set of solutions that follow the method their course teaches.
It is the Solve rung of the Guide/Show/Solve ladder, and it is a real rung. Clicking any step of a
solution to ask about it drops into a Guide exchange on that step, so the solver and the
conversation are one product at two altitudes rather than two features.

**Accuracy is the entire value proposition.** A confidently wrong solution is worse than none,
because the student stops checking. Every design decision below that looks expensive is paying for
that, and the two that pay most are the segmentation review gate and independent verification.

### Principles

1. **The machine is honest about what it knows.** A step verified by a computer algebra system, a
   step grounded in retrieved course material, and a step the model supplied on its own are three
   different things and are never presented as one.
2. **Nothing is asserted, everything is correctable.** The propose-and-confirm posture from
   `profile_facts` carries over unchanged. The student can correct a segmentation before it costs
   compute, and correct a solution after.
3. **Results land as they are produced.** Per-problem results are written to the artifact as they
   complete, never buffered until the end. A student who closes the laptop mid-solve comes back to
   finished work.
4. **Deterministic checks beat second opinions.** Models ratify their own work. Self-critique is
   kept because it is nearly free, but it is not the safety net.

## The Artifact Model

Through Phase 1, Lyra holds four kinds of thing: inputs the user supplied (`documents`), a derived
index (`chunks`), a transcript (`messages`), and claims about a class (`profile_facts`). None of
these is a thing Lyra produced that the user keeps, edits, and returns to.

An artifact is that missing primitive. `profile_facts` is already the pattern in miniature:
generated content with a source document, a confidence, a confirmed flag, and inline correction.
This generalizes it rather than inventing a second idea.

### Schema

Migration `005_artifacts.sql`.

```sql
create table artifacts (
  id integer primary key autoincrement,
  class_id integer not null references classes(id) on delete cascade,
  kind text not null check (kind in ('solution_set')),
  title text not null,
  state text not null check (state in
    ('pending','segmenting','awaiting_review','solving','ready','failed','cancelled')),
  stage_detail text,
  problems_total integer,
  problems_done integer not null default 0,
  error_message text,
  created_at text not null default (datetime('now')),
  updated_at text not null default (datetime('now'))
);
create index idx_artifacts_class on artifacts(class_id);

create table artifact_sources (
  artifact_id integer not null references artifacts(id) on delete cascade,
  document_id integer not null references documents(id) on delete cascade,
  role text not null check (role in ('problem_set','reference_solutions')),
  ordinal integer not null,
  primary key (artifact_id, document_id)
);

create table artifact_parts (
  id integer primary key autoincrement,
  artifact_id integer not null references artifacts(id) on delete cascade,
  parent_part_id integer references artifact_parts(id) on delete cascade,
  kind text not null check (kind in ('problem','step','answer','figure')),
  ordinal integer not null,
  label text,
  content text not null default '',
  content_type text not null default 'markdown'
    check (content_type in ('markdown','image')),
  status text not null default 'pending'
    check (status in ('pending','solving','verifying','complete','failed')),
  origin text not null default 'generated'
    check (origin in ('generated','regenerated','user_corrected')),
  verdict text not null default 'unchecked'
    check (verdict in ('unchecked','verified','refuted','uncheckable')),
  error_message text,
  created_at text not null default (datetime('now')),
  updated_at text not null default (datetime('now'))
);
create index idx_parts_artifact on artifact_parts(artifact_id);
create index idx_parts_parent on artifact_parts(parent_part_id);

create table artifact_part_revisions (
  id integer primary key autoincrement,
  part_id integer not null references artifact_parts(id) on delete cascade,
  revision integer not null,
  content text not null,
  origin text not null check (origin in ('generated','regenerated','user_corrected')),
  note text,
  created_at text not null default (datetime('now'))
);
create index idx_revisions_part on artifact_part_revisions(part_id);

create table artifact_provenance (
  id integer primary key autoincrement,
  part_id integer not null references artifact_parts(id) on delete cascade,
  chunk_id integer references chunks(id) on delete set null,
  document_id integer references documents(id) on delete set null,
  page_number integer,
  label text
);
create index idx_provenance_part on artifact_provenance(part_id);
```

Two later migrations extend this. `006_solving.sql` adds `artifact_checks`, the tool calls behind
one verdict; `artifact_parts.verdict_detail`, the sentence a verdict is explained by; and
`settings.tools_supported` plus `tools_message`, the recorded capability probe.
`007_session_artifact_part.sql` adds the anchored-session column described under Asking About A
Step.

### Why it is shaped this way

**Parts are a tree, not a list.** `parent_part_id` is what lets a problem own its steps. It is also
what lets a Phase 3 figure hang off the step that references it without a schema change, which is
why `content_type` and the `figure` kind exist now rather than later. Adding non-text content after
the fact would mean rewriting rendering, export, and storage at once.

**Three columns, three questions.** `status` answers "is this part still being worked on",
`origin` answers "who wrote what is currently here", and `verdict` answers "what did checking
conclude". Folding them into one column would produce states like `user_corrected_but_unverified`
that no query wants.

**Revisions are per part, not per artifact.** Parts are what the user clicks, questions, corrects,
and regenerates. An artifact revised only as a whole cannot support any of those. `note` carries
the reason a revision exists: the student's correction text, or the verifier's refutation.

**Job state lives on the artifact row.** Note that `architecture.md` described an `ingestion_jobs`
table that was never built; document ingestion put its state on `documents` columns instead, and
that has worked well. The solver follows the implemented pattern rather than the documented one,
and architecture.md is corrected accordingly.

**A per-problem regeneration does not move the artifact's state.** It moves that one part to
`solving` and leaves the artifact `ready`, because the rest of the document is still readable. Only
the initial run walks the artifact state machine.

**Chunk provenance is nullable with `on delete set null`.** A student may delete and re-upload a
document after a solution is produced. Losing the citation is acceptable; losing the solution is
not.

## Job Architecture

**The solver is ingestion-shaped, not chat-shaped.** This is the most consequential decision in the
phase. A full problem set with verification passes can run for tens of minutes on local hardware,
which is well past what an open connection should be trusted with. It is a background job with a
polled status endpoint, following the pattern proven by document ingestion.

`backend/core/solver.py` mirrors `backend/core/ingestion.py`: a module-level queue, one worker
thread started idempotently from the app lifespan, its own connection per run, every transition
committed as it happens, and a `try`/`except` around each run so one bad artifact cannot take the
worker down.

**It is a second worker, not a second consumer of the ingestion queue.** Ingestion is throughput
against a single local embedding server; solving is a long chain of tutor-model calls. Sharing one
worker would let a thirty-minute solve block every upload behind it.

### State machine

```
pending -> segmenting -> awaiting_review -> solving -> ready
```

Terminal states besides `ready`:

- `failed` carries a user-facing `error_message` and the stage that failed in `stage_detail`.
- `cancelled` is the student stopping a run. Parts already complete are kept: a partly solved set
  is worth more than nothing, and discarding finished work to honor a cancel is a bad trade.

`awaiting_review` is not a transient stage. It is where the job stops and waits, indefinitely, for
the student to confirm the segmentation. See below.

Per-part status moves independently once solving begins: `pending -> solving -> verifying ->
complete`, or `failed` for one problem that could not be solved. A failed problem never fails the
artifact; the run continues and the artifact lands `ready` with that problem carrying its own error.

### Interruption

`reconcile_interrupted` gains an artifact arm, matching the document one. An artifact caught in
`segmenting` or `solving` when the process stopped is marked `failed` with the interrupted message.
An artifact in `awaiting_review` is left exactly as it is: it was not working, it was waiting, and
a restart does not change that. This is why the review gate is a state rather than a flag on an
otherwise-running job.

### Progress reporting

`GET /api/solutions/{id}/status` returns the artifact state, `stage_detail`, `problems_total`,
`problems_done`, and a per-part status array. Progress is real: `problems_done` increments when a
problem's verification finishes, never on a timer and never estimated. When the count is unknown,
because segmentation has not finished, `problems_total` is null and the interface says so rather
than showing a bar at zero.

Polling backs off from 500ms to 2s and stops on a terminal state, matching the ingestion poll.

## Segmentation

**Identify each problem and sub-part in an uploaded homework set, then show the result before
solving anything.**

Segmentation sends whole documents to the tutor model, exactly as profile extraction
does, so it is bound by the same rule: **document text is never sent to a non-local
endpoint the student has not acknowledged.** That rule lives in one place,
`app_settings.document_text_allowed`, which both callers ask before reading any text. A
blocked pass is not a failure: the chunker's list stands on its own and the gate is where
the student sees it.

### Two sources of evidence

1. **The chunker, already done.** `chunks.problem_number` and `chunks.part_index` are populated
   today for documents detected as `homework`, and `rag/chunk.py` already splits on `1.`,
   `Problem 1`, `Problem 1 (Parseval)`, `Exercise 3.14`, `Q1`, and on lettered sub-parts. This is
   deterministic, free, and correct on the common case.
2. **A model pass**, over the document text, asked to return the problem list as JSON: number,
   label, the first line of the statement, the page it starts on, and its sub-parts. This catches
   what a regex cannot: a set numbered by section heading alone, a problem introduced by prose, a
   sub-part marked in a way `SUBPART_MARKER` deliberately excludes.

The chunker's markers are the spine: they decide **which problems exist**. The model may add
problems it found, split one the regex merged, and supply labels. Every difference between the two
is still just a row in the reviewable list; the reconciliation does not need to be right, because a
person is about to look at it.

Three rules make that reconciliation hold on real sheets, and each one was written after a sheet
that broke without it:

- **A number that comes back later is a different problem.** Sheets restart their numbering under
  each section heading. Collecting chunks by number folded twelve problems into five rows carrying
  three unrelated statements each, and because the chunker is the spine, a model pass that read the
  sheet correctly was reconciled straight back down to the same five. Equal numbers are matched
  between the two lists in document order, first to first.
- **A named marker outranks a bare one.** `Problem 1` above a list of numbered sub-items numbers
  both, and they are not the same thing. Where a document's first marker is a named one, the named
  markers are its problems and the bare numbers underneath them are their parts.
- **The chunker owns which problems exist; the model owns how they read.** The chunker's text is
  the document's own, character for character, and it is also flattened: extraction turns
  $e^{-2t}u(t-3)$ into `e−2tu(t −3)`. Presenting that at the gate asks the student to check a
  reading of their homework against text their sheet does not contain. So the model's statement
  replaces it **when it is a transcription rather than a summary**, decided by whether the sheet's
  own words survive in it, counting its sub-parts as part of its reading. A model that dropped what
  the sheet said keeps nothing, and the chunker's text stands.

### Multi-file sets

`artifact_sources` holds one or more documents with role `problem_set`, each with an `ordinal`.
Segmentation runs per document and problems are ordered by `(source ordinal, problem ordinal)`.
Duplicate numbering across files is resolved by showing the filename beside the problem in the
interface, never by renumbering: a student who reads "Problem 1" on the page and "Problem 7" in
Lyra has to translate between the two on every glance.

### The review gate

The job stops at `awaiting_review` and does no further work until the student confirms.

This costs a mandatory interaction on every run. It is worth it because the alternative costs tens
of minutes of local compute: a merged problem produces one long wrong solution, a missed problem
produces silence, and neither is visible until the run is over. Confirming a correct segmentation
is one click; recovering from a bad one is a full re-run.

At the gate the student can merge, split, delete, reorder, and relabel problems, and edit a
problem's statement text. `PATCH /api/solutions/{id}/segmentation` replaces the problem list
wholesale rather than patching individual rows, because merge and split are not expressible as
per-row edits. `POST /api/solutions/{id}/start` confirms and begins solving.

An artifact left in `awaiting_review` stays there across restarts. It is listed as `Waiting for
you` rather than as in progress, because it is.

## Solving

One problem at a time, in order, each written to the artifact when it completes.

### Retrieval and method alignment

Each problem is solved against retrieved course material, using the existing `rag/retrieve.py`
scoped to the class. The retrieval query is the problem statement itself, which is exactly the
shape retrieval already handles well.

**Method alignment is the point.** The instruction is to prefer the approach the course teaches
over the approach the model prefers, and the evidence for what the course teaches comes from
lecture notes and textbook sections in the same retrieval. Where retrieval returns a worked example
using a particular technique, that technique is what the solution uses, and the solution says so.

This is the phase's least verifiable claim. There is no automated check for "solved it the way the
course does", and this document does not pretend otherwise. What can be done is make the evidence
visible: a step grounded in retrieved material carries its provenance, so a student who knows their
course can see at a glance whether the method came from their notes or from the model.

### Reference solutions

A student who has last term's solutions, or the professor's solutions to an earlier set, holds the
strongest available signal for notation, style, and method. It is also the clearest advantage over
pasting a problem into a general chatbot.

Documents are designated as reference solutions **per solve run**, in the setup screen, not by a
persistent role on the document. That is the moment the student is actually thinking about it, it
needs no schema change to `documents`, and the same document can be a reference for one run and
irrelevant to another. It is recorded as an `artifact_sources` row with role `reference_solutions`.

Reference solutions enter the prompt labelled by what they are: the notation, layout, and method the
course expects. They are capped at a share of the retrieval budget so a long reference document
cannot crowd out the retrieved course material for the problem actually being solved.

The label distinguishes two cases, because the picker allows both and they are not the same
instruction. Solutions to a **different** set are a method to follow and not content to copy; a
model that answers from them is answering the wrong question. Solutions to the **set being solved**
are the authority on the answer: the student attached them deliberately, and a solve told to look
away from them is a solve that ignores what the student asked it to use. In that case the model is
told to follow the reference, to say in the step that it is doing so, and to say so explicitly where
its own working disagrees rather than silently picking one. The setup screen states this too, at the
moment the reference is chosen.

Reference documents are also reachable through ordinary retrieval, which is what makes them citable:
a step that follows the answer key cites the retrieved entry and the provenance chip names the file.
Without that, a run whose whole point was the answer key reports every step ungrounded.

### Structure

**Solutions are structured by step, because steps are what the user clicks, asks about, and
regenerates.** The model is asked for a structured response: a list of steps, each with its own
prose and math, plus a final answer. Steps become `artifact_parts` of kind `step` under the
problem, and the final answer becomes a part of kind `answer`.

A model that ignores the structure and returns one prose block is not an error. The solution is
stored as a single step, and the interface renders it. Degrading to a worse experience beats
failing the problem.

### Regeneration

`POST /api/solutions/{id}/parts/{part_id}/regenerate` re-solves one problem, optionally carrying a
correction the student supplied. The correction is passed to the model as input, stored as the
`note` on the revision it produces, and never silently discarded.

The existing reply is replaced only once the new one has been written, following the same rule the
chat retry already follows: a regeneration that fails upstream leaves the student with the solution
they already had rather than nothing.

## Verification

The single highest-value check available for the math and engineering work Lyra is best at, and the
reason the tool-calling loop is built in this phase rather than in Phase 4.

### Architecture: a separate pass

Solving runs **without tools**. Verification is a second pass over the finished solution, with the
tools attached.

This is deliberate. Keeping tools out of solving means solving works against any OpenAI-compatible
endpoint, including one that does not implement `tools` at all; it keeps solving cheap, since tool
round trips multiply token cost per problem on a local model; and it keeps the check independent of
the work, which is the whole reason a check is worth anything. When the endpoint has no tool
support, verification degrades and solving does not.

Verification is per problem and runs immediately after that problem is solved, not as a global
phase after everything. A student reads problem 1 while problem 7 is still being solved, and each
problem shows its own verdict as it lands.

### What is checked

The verifier receives the solution and is asked to check it, with tools available. The tool calls
it makes **are** the checkable claims, and the transcript of those calls is the audit trail the
interface renders. There is no separate claim-extraction format to keep in sync with the tools.

Tools available in Phase 2, all pure computation:

| Tool                | Purpose                                                           |
| ------------------- | ----------------------------------------------------------------- |
| `cas_evaluate`      | Simplify an expression, or test whether two expressions are equal |
| `cas_solve`         | Solve an equation or system for named unknowns                    |
| `cas_integrate`     | Definite and indefinite integration                               |
| `cas_differentiate` | Differentiation, including partial derivatives                    |
| `cas_linalg`        | Determinant, inverse, eigenvalues, rank, linear solve             |
| `check_units`       | Dimensional analysis: does an expression carry the expected units |

Unit and dimensional checking is cheap and catches a large share of physics and engineering errors,
which is why it is in the first tool set rather than a later refinement.

### Verdicts

- **`verified`**: every check the verifier ran agreed with the solution, and at least one check
  actually ran. A checker that answers "looks right" without calling anything has ratified its own
  work, which is what deterministic checking exists to avoid, so that outcome is `uncheckable`.
- **`refuted`**: a check disagreed. The problem is re-derived once, with the refutation supplied as
  input. If the second attempt is refuted too, the problem is marked `refuted` and the document
  says so plainly, naming the check that failed. It is not silently re-run until it passes.
- **`uncheckable`**: nothing in the solution was mechanically checkable, which is the honest
  outcome for a proof or a conceptual answer. This is distinct from `unchecked` and must not be
  rendered as if it were a pass.
- **`unchecked`**: verification did not run, because the endpoint has no tool support or the run
  was cancelled. Reported with its reason, never as a pass.

### Grounding, shown separately

Per-problem confidence in the interface distinguishes **steps grounded in retrieved course
material from steps the model supplied on its own.** This is not a score, it is provenance: a step
with `artifact_provenance` rows shows its source, a step without shows nothing, and the problem
header states how many of its steps are grounded. A number nobody can audit would be worse than
saying nothing.

### Not used for verification: web lookup

Deliberately excluded, and the reason is not squeamishness about fetching. Answer sites are
paywalled and hostile to fetching, matching a problem across textbook editions is its own hard
problem, and above all **a wrong answer retrieved from the web is more dangerous than the model's
own wrong answer**, because it arrives with borrowed authority and the model will defer to it. Web
search earns its place looking up unfamiliar methods, which is Phase 4.

## Tool Calling

Built in-house against the existing LLM client rather than adopted from an agent framework. The
tool surface is small, and what frameworks add beyond the loop itself is multi-provider
abstraction, plugin systems, sandboxing, and session state that Lyra already has or explicitly
excludes.

### The client does not support tools today

`backend/llm/client.py` sends only `messages`, `stream`, and `model`, and its delta parser reads
only `content` and the reasoning fields. Tool calling is new code there, not a flag:

- `_chat_body` gains an optional `tools` array and `tool_choice`.
- A non-streaming `complete_with_tools` returns the assistant message including `tool_calls`,
  because the verification loop wants whole calls, not fragments. Verification is not read live by
  anyone, so there is nothing streaming buys it.
- Tool results are appended as `{"role": "tool", "tool_call_id": ..., "content": ...}` messages.

### Endpoints without tool support

Not every OpenAI-compatible server the student points at implements `tools`. A server that rejects
the request, or that ignores the array and answers in prose, must not be treated as a failure of
the solver.

The capability is probed once and recorded on the settings row alongside the endpoint. On a
negative result: solving proceeds unchanged, verification is skipped, every problem carries verdict
`unchecked`, and Settings states plainly that the configured endpoint cannot run verification and
what that costs. This is live scaffolding of the same kind as the endpoint locality machinery, and
it goes away when inference is bundled in Phase 6.

### The loop, and its guarantees

`backend/llm/tools.py` holds the loop and the tool registry. In the order the requirements matter:

- **Termination is guaranteed.** A call-depth ceiling and a wall-clock timeout, both configurable
  and both with defaults. The wall clock is what actually bounds a run; the depth ceiling is the
  backstop for a model that has stopped making progress, and it is set generously because a
  homework problem is routinely five lettered sub-parts and a checker works through them a few at
  a time. Measured against a real signals set, one such problem took fifteen rounds. A loop that
  silently stops producing is worse than one that says it gave up, so hitting either is reported:
  the verdict becomes `unchecked` with the reason, never `verified` by default.
- **Tool calls are visible in the transcript.** The student can always see what was run and what
  came back. This is a debugging affordance now and the precondition for trusting the agent in
  Phase 4.
- **Tools are pure until Phase 4.** The Phase 2 tool set only computes. Nothing reads or writes
  outside `data/`, nothing opens a socket, and no tool call is dispatched to a name outside the
  registry.
- **A tool error is a result, not an exception.** A malformed expression, a timeout, or an
  unsolvable integral comes back to the model as a structured error result so it can try
  differently. Only a bug in the loop itself raises.

### Computer algebra, and its containment

`backend/tools/cas.py` wraps SymPy. `backend/tools/units.py` wraps `pint`. Both live outside
`backend/llm/` because they are pure computation and must be importable and testable without an
LLM anywhere in the picture.

**SymPy's `sympify` is `eval` underneath, and the expressions come from a model that has read an
untrusted uploaded document.** This is the first place in Lyra where model output reaches an
evaluator, and it is handled as such:

- Parsing goes through `parse_expr` with an explicit restricted namespace, never bare `sympify`.
- Every call runs in a subprocess with a wall-clock timeout, because SymPy can genuinely hang on a
  hard integral and a hung worker is a hung solver.
- The subprocess sets CPU and address-space limits where the platform provides them.
- Expression length is capped before parsing.

This is defense in depth, not a sandbox, and this document does not claim otherwise. A real
boundary is Phase 4's work, gated behind the written threat model covering a poisoned upload that
architecture.md already requires before any tool touches the filesystem. What is true today is that
the tool set computes and nothing else, the process running it is short-lived and bounded, and a
crash in it costs one verification rather than the backend.

## Asking About A Step

Clicking any step opens a Guide-mode exchange scoped to that step. This is what makes the solver
and the conversation one product rather than two.

It reuses the chat stack entirely. `chat_sessions` gains a nullable `artifact_part_id` (migration
`007_session_artifact_part.sql`), and a session created with one:

- pins that step's content, and its problem's statement, into the turn as context
- opens in `guide` mode, because dropping from Solve to Guide is the point of the interaction,
  though the toggle remains and the student may switch to Show
- retrieves normally on top of the pinned context, so the exchange still reaches the course material
- appears in the sidebar under its class like any other conversation, named from its first message

No new streaming protocol, no second message store, no parallel prompt builder.

## API Surface

All routes are class-scoped for creation and listing, artifact-scoped thereafter, matching the
existing document and session split.

**Solutions**

- `POST /api/classes/{class_id}/solutions` - Create a solution set from selected documents. Returns
  `202` with the artifact in state `pending` and begins segmentation. Body carries the source
  document ids with their roles and an optional title.
- `GET /api/classes/{class_id}/solutions` - List this class's solution sets with state and counts
- `GET /api/solutions/{artifact_id}` - The full artifact: parts, provenance, verdicts
- `GET /api/solutions/{artifact_id}/status` - Poll target. State, stage, counts, per-part status
- `PATCH /api/solutions/{artifact_id}/segmentation` - Replace the problem list at the review gate
- `POST /api/solutions/{artifact_id}/start` - Confirm the segmentation and begin solving
- `POST /api/solutions/{artifact_id}/cancel` - Stop the run, keeping completed problems
- `DELETE /api/solutions/{artifact_id}` - Delete the artifact and everything it owns

**Parts**

- `PATCH /api/solutions/{artifact_id}/parts/{part_id}` - Store the student's edit of a part
- `POST /api/solutions/{artifact_id}/parts/{part_id}/regenerate` - Re-solve one problem, optionally
  with a correction
- `GET /api/solutions/{artifact_id}/parts/{part_id}/revisions` - Revision history for one part

**Documents**, extended

- `GET /api/documents/{document_id}/pages/{page_number}` - One page rendered to PNG by PyMuPDF and
  cached under `data/pages/`. The solver's source pane needs exact page-level anchoring, which an
  embedded PDF viewer cannot give reliably, and Phase 3 needs the same rasterization for figures
  and text recognition. Text sources have no pages and are served from their extracted text instead

**Chat**, extended

- `POST /api/classes/{class_id}/sessions` gains an optional `artifact_part_id`

The route prefix is `/api/solutions` while the table is `artifacts`, and that is intentional. The
model is general; this is the solver's view of it. When Phase 4 produces artifacts of its own kind
they get their own route rather than overloading this one with a `kind` query parameter.

## Module Layout

New modules, following the structure in conventions.md:

```
backend/
  core/
    solver.py           # Job orchestration: segment, solve, verify, regenerate
    segmentation.py     # Chunker markers plus a model pass, reconciled
    artifacts.py        # Artifact and part CRUD, revisions, provenance
  llm/
    tools.py            # The tool-calling loop and the tool registry
    prompts.py          # Gains the segmentation, solving, and verification prompts
  tools/
    __init__.py
    result.py           # ToolResult: the one shape every tool returns
    cas.py              # SymPy behind a bounded subprocess
    _cas_runner.py      # The child process. Never imported by the backend
    units.py            # Dimensional analysis via pint
  api/
    routes_solutions.py
```

`backend/tools/` is a new top-level package deliberately. Its contents are pure computation with no
knowledge of prompts, models, or the database, and they are tested that way.

`_cas_runner.py` is the only module in the codebase that is executed rather than imported. It is
started as `python -m backend.tools._cas_runner`, reads one JSON request from stdin, and writes one
JSON response to stdout. Keeping SymPy behind that boundary is what makes the containment claims
above true rather than aspirational, so nothing may import it into the backend process.

`units.py` runs in-process and `cas.py` does not, which is a deliberate asymmetry rather than an
oversight. Pint walks a unit expression as an AST and asserts each node against a small whitelist
before evaluating, and unit arithmetic terminates; neither the injection risk nor the hang risk that
puts SymPy in a subprocess applies. If pint ever grows an evaluator that runs arbitrary work, units
belongs in the runner beside SymPy.

## Testing

Following the rule in conventions.md: tests defend observable contracts, and never call a live LLM
or a real endpoint.

Worth testing:

- Segmentation reconciliation: chunker markers plus a model list, including a merge, a split, and a
  problem the regex missed
- The review gate: an artifact in `awaiting_review` does not advance, survives
  `reconcile_interrupted` untouched, and only `start` moves it
- Job state transitions including every failure path, and that a failed problem does not fail the
  artifact
- Per-problem results are committed as they complete, not at the end
- Cancellation keeps completed parts
- Tool loop termination: depth ceiling and timeout both reached, both reported as `unchecked` with
  a reason, never as `verified`
- Tool dispatch rejects a call to a name outside the registry
- CAS containment: a malformed expression returns a structured error, an over-long expression is
  refused before parsing, a hanging call is killed by the timeout
- Verification verdicts, especially that `uncheckable` and `unchecked` never render as a pass
- Regeneration replaces only once the new content exists, matching the chat retry rule
- Endpoint without tool support: solving completes, verification is skipped, verdicts are honest

Not worth testing: that the schema has the columns it was declared with, and that a Pydantic model
round-trips.

The CAS tools are tested directly against known results. That is the one place in Lyra where the
expected value of a computation can be asserted exactly, and it should be.

## Build Order

Each step leaves the product working, and the dependencies are real rather than tidy.

1. **Artifact model.** Migration, `core/artifacts.py`, CRUD and revision tests. No UI, no job.
2. **Tool loop and tools.** `llm/tools.py`, `tools/cas.py`, `tools/units.py`, client tool support,
   the capability probe. Tested against a faked client and against real SymPy.
3. **Segmentation and the gate.** The solver worker, segmenting, `awaiting_review`, the API. The
   interface's setup and review screens.
4. **Solving.** Per-problem generation, retrieval, reference solutions, structured steps, per-part
   writes. The solving and solution screens.
5. **Verification.** The second pass, verdicts, provenance display.
6. **Corrections and regeneration.** Part editing, mark-wrong, re-solve with correction.
7. **Asking about a step.** The `artifact_part_id` session.
8. **Export.**

Steps 1 and 2 are the shared substrate and are the ones Phase 4 inherits. They are first so nothing
downstream is designed around a solver-specific shape.

## Deferred Within This Phase

- **Figure extraction into solutions.** Needs the structural parsing in Phase 3. The artifact model
  holds mixed content from the start so this lands without a migration.
- **Web method lookup.** Needs the tools in Phase 4.
- **Overlaying solutions on the source PDF.** Anchored side-by-side is a fraction of the work for
  most of the value. This stays open as a later refinement.

The phase closes without all three.

## Definition Of Done

A student uploads a problem set, watches it segment, corrects a merged problem, starts the solve,
watches solutions land one at a time, reads solutions that use the method their course teaches,
sees which steps were checked and which were only generated, catches a wrong answer and has that
one problem re-solved with their correction, asks a clarifying question about a single step and
gets a Guide exchange on it, and exports the result.
