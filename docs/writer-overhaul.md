# Writer Overhaul: One Assistant That Actually Writes

This document specifies the overhaul of the draft workspace's AI into a single, capable writing
assistant - a writer, not a completion box. It is written to be handed to an implementation agent
and executed without further design decisions. Where a decision could have gone two ways, it has
been made here, and the reason is recorded so a reader who disagrees knows what they are
disagreeing with.

**Status: all six slices landed (2026-08-06/07), then repaired (2026-08-07).** Two departures from
the letter of the spec, both verified live: the export requires Pandoc alongside Typst (kuhn's
actual renderer - LaTeX math does not survive a hand-rolled markdown-to-Typst conversion), and the
review pipeline tolerates one upstream failure per run before aborting, because a local
llama-server flakes on long runs and one 500 should not cost the remaining lenses.

**The repair pass (2026-08-07).** Everything above was built and none of it was reaching the
student. Five faults, all now fixed and pinned by tests:

1. **The review looked dead.** `/review` and `/pass` queued their jobs while leaving the artifact
   `ready`, so the workspace's status poll - which stops the moment the artifact is not pending or
   generating, and is never restarted - died on its first request. A review that filed four
   comments settled into a Comments tab that would never refetch. Both endpoints now mark the
   artifact `pending` inside the request (`routes_drafts._begin_run`), the way every other
   pipeline already did.
2. **Failures were invisible.** Both pipelines settled `ready` on failure and wrote the reason to
   `error_message`, which the workspace only renders beside `failed`. Both now settle `failed`.
3. **No length target reached any prompt.** `brief.length_target` was one labelled line among
   several; the structure stage never saw the pass instruction at all; the only size guidance in
   `_WRITING_CRAFT` argued for brevity; no `max_tokens` was ever sent, so truncation was silent.
   The target is now parsed to a word count (`briefs.length_target_words`), divided across the
   planned sections, and carried into both stages - and a section that comes back far short is
   asked for again, once.
4. **A first draft shipped as the draft.** The pass now ends with a bounded revise stage
   (`writer_pipeline._revise_stage`): deterministic checks for thin and TODO-bearing sections plus
   one whole-document evaluation call, then targeted rewrites, at most two rounds.
5. **Math did not render.** Display equations opened as raw CodeMirror boxes (the `codeMirror`
   feature was added with no config, so `previewOnlyByDefault` fell through to the read-only flag);
   KaTeX's stylesheet sat below Tailwind's preflight in the cascade layers, so `border: 0` erased
   every fraction bar and square-root overline; and nothing converted the `\(..\)`, `\[..\]`, and
   bare `\begin{align}` that models actually write. Fixed at the feature config, at the `@layer`
   order in `globals.css`, and by normalizing at every AI landing (`core/mathnorm.py`) and on load
   (`lib/drafts/math-delimiters.ts`).

The measure also went from a hardcoded 760px beside a fixed 380px rail to a wider column and a
draggable, remembered split.

The source of the ideas is **kuhn** (`/Users/ofhd/Developer/kuhn`), the owner's AI-assisted
scientific writing tool. The August 2026 integration ([integration-handoff.md](integration-handoff.md),
Workstream 3) ported kuhn's editor and its pending-edit review machinery, and it ported them well.
What it did not port is the part that made kuhn worth building: the agent runtime. This document
ports that - translated from a cloud swarm of six concurrent agents into a serial system for one
locally served model with a modest context window.

## 0. Diagnosis: what the quick integration kept, and what it lost

Kept, and working:

- The Milkdown Crepe editor, autosave, revision history (`frontend/src/components/drafts/`)
- `/write`: a stateless streamed passage at the caret (`backend/api/routes_drafts.py`)
- `/suggest`: one whole-document revision call, landing as a pending edit reviewed hunk by hunk
  (`backend/core/drafting.py`, `backend/core/suggestions.py`)
- The rail's Chat tab, which is `StepThread` - Lyra's Guide/Show tutor thread pinned to the
  draft's body part

Lost in translation:

- **The assistant does not know what it is writing.** Kuhn's PM interview and `project.json`
  gave every agent the assignment. Lyra's writer gets a retrieval query and hopes.
- **Nothing can address the document by section.** Kuhn's `read_sections.py` let agents work on
  a fifteen-page document without holding it in context. Lyra's `/suggest` stuffs the whole body
  into one prompt and asks for the whole body back - which stops working exactly when drafts get
  long enough to need help.
- **No tools.** Kuhn's agents read files, searched literature, filed comments, asked the user
  questions. Lyra's writer is two one-shot prompt builds. The irony: Lyra already owns a real
  tool loop (`backend/llm/tools.py`, built in Phase 2 for verification) that the draft workspace
  never touches.
- **No review.** Kuhn's reviewer - the adversarial pass over structure, claims, transitions, and
  citations, delivered as margin comments - has no counterpart at all.
- **No pipelines.** Kuhn's seeding pipeline (research, then skeleton, as code dispatching agent
  tasks) demonstrated that a long document is drafted in passes, not in one shot. Lyra has no
  multi-pass anything.
- **Guide/Show makes no sense here.** The tutor's Socratic mode toggle is about not doing the
  student's homework for them. A draft is the student's own writing; the assistant's job is to
  help produce and improve it. Two modes where there should be one assistant.
- **Export regressed to `window.print()`.** Kuhn rendered markdown through Typst to a typeset
  PDF. The print stylesheet is a fine stopgap and not a deliverable.

## 1. Ground rules

1. **Lyra's constraints are non-negotiable.** Backend binds to `127.0.0.1`. No telemetry, no
   accounts. Course content and draft text go only to the tutor endpoint, under the existing
   locality acknowledgement (`document_text_allowed` in `backend/core/app_settings.py`). No new
   gating is needed: every feature here rides the tutor endpoint that chat and `/suggest`
   already ride.
2. **One model, one request at a time.** The tutor endpoint serves exactly one concurrent
   request. There are no parallel agents anywhere in this design. "Multi-agent" here means
   multiple prompt profiles taking turns over one connection, orchestrated by code.
3. **Generated content is proposed, never asserted** - with one carve-out, decided deliberately:
   a section that is empty (or holds only a TODO marker) may be written directly, after a
   revision snapshot, because there is nothing of the student's to protect. Any section carrying
   the student's prose is only ever changed through a pending edit. This is kuhn's seeding
   bypass, made into a per-section rule.
4. **The model is small and the context is modest.** Every prompt in this document is built
   section-scoped with an explicit token budget. Nothing ever assumes the whole document fits.
   When something does not fit, the assistant works in more, smaller passes - that is the design,
   not a degraded mode.
5. **Web access is out of scope for this overhaul.** Verification against the class's own
   material lands now; verification against the web waits for the self-hosted FireCrawl instance
   and the Phase 4 security posture, and is marked deferred below.

## 2. The shape

### One writer, three prompt profiles, pipelines as code

There is **one assistant** in the draft workspace. The student talks to it, and it can research
the class material, draft, revise, review, and answer questions about the piece. No mode picker,
no agent picker.

Internally it wears three prompt profiles, all served by the same endpoint and the same tool
loop, differing only in system prompt and tool grant:

| Profile  | Used by                          | Tools | May change the document |
|----------|----------------------------------|-------|-------------------------|
| chat     | The rail conversation            | read + search + propose | Through pending edits only |
| drafter  | The drafting pipeline's stages   | read + search + write   | Direct on empty sections, else proposes |
| reviewer | The review pipeline's lenses     | read + search + comment | Never - it files comments |

Multi-stage work (a full draft, a full review) is a **pipeline: deterministic code that
dispatches profile runs in sequence**. This is kuhn's key runtime decision (`agents/seeding.js`),
kept verbatim: control flow an agent improvises is control flow nobody can debug. Under a
one-request serial constraint it is also simply the only shape that works.

### Run model: chat streams, passes queue

- **Conversational turns** run on the request, streamed over SSE like `/write` today.
- **Passes** (full draft, full review) are queued jobs on the existing drafting worker
  (`backend/core/drafting.py` already owns the queue, the single worker thread, and the
  interrupted-run reconciliation). Progress reports through the artifact state machinery
  (`state`, `stage_detail`, `problems_total/done` = sections or lenses), which the workspace
  already polls via `/drafts/{id}/status`. The student keeps editing while a pass runs; a
  pass reads the body fresh at each stage precisely because the body may have moved.

One honesty note on "chat streams": a chat turn that uses tools cannot stream its tool rounds -
`complete_with_tools` is non-streaming, and parsing tool calls out of a llama.cpp SSE stream is
fragile enough that Phase 2 deliberately did not. So a writer chat turn streams **activity
frames** (one per tool call: "Reading section 3", "Searching the course material"), and the
final answer arrives as text frames. Liveness comes from the activity, not token-by-token tool
rounds. If a turn uses no tools it streams tokens exactly like chat today. Accepted tradeoff;
revisit only if turns feel dead in practice.

### What is deliberately not ported from kuhn

- **The Claude Agent SDK.** Lyra's in-house loop exists, is tested, and records every call.
  Adopting an SDK for one local OpenAI-compatible endpoint buys abstraction nobody asked for.
- **`spawn_agent` / parallel dispatch.** Serial endpoint. Pipelines are the replacement.
- **`ask_user` as a tool.** In a conversational surface, asking the user is called "replying".
  Pipelines never ask mid-run (kuhn's seeding rule, kept): they proceed with the brief and
  record what they assumed.
- **The org knowledge library, multi-tenancy, Yjs collab, review links.** Single-user, local.
- **DB-stored editable prompts.** Lyra's prompts live in `backend/llm/prompts.py` under version
  control; runtime prompt editing is a product Lyra is not.
- **The claims manifest as a separate file.** Its job - forcing the drafter to notice
  unsupported claims - is folded into the reviewer's claims lens, which checks prose against
  retrieved course material directly.

## 3. Data model

Three additions, one migration each in `backend/storage/migrations/` (next free number: 019).

**`draft_briefs`** - what the document is (Workstream 1):

```sql
create table draft_briefs (
    artifact_id        integer primary key references artifacts(id) on delete cascade,
    assignment_type    text not null default '',      -- 'essay', 'lab report', ...
    summary            text not null default '',      -- freeform: purpose, topic, constraints
    audience           text not null default '',
    length_target      text not null default '',      -- '5 pages', '2000 words', as stated
    source_document_id integer references documents(id) on delete set null,
    status             text not null default 'proposed',  -- 'proposed' | 'confirmed'
    created_at         text not null default (datetime('now')),
    updated_at         text not null default (datetime('now'))
);
```

**`draft_comments`** - margin comments (Workstream 3):

```sql
create table draft_comments (
    id          integer primary key,
    part_id     integer not null references artifact_parts(id) on delete cascade,
    parent_id   integer references draft_comments(id) on delete cascade,  -- null = thread root
    author      text not null,                 -- 'reviewer' | 'writer' | 'student'
    severity    text,                          -- root only: 'critical'|'major'|'minor'|'note'
    quote       text,                          -- root only: verbatim anchor text
    hint        integer,                       -- root only: char offset at filing time
    body        text not null,
    resolved    integer not null default 0,    -- root only
    orphaned    integer not null default 0,    -- root only: quote no longer resolves
    created_at  text not null default (datetime('now'))
);
```

**`chat_sessions.mode`** gains the value `'writer'`. The draft rail's conversation becomes a
writer-mode session anchored to the body part via the existing `artifact_part_id` column, so
transcripts, titles, and deletion all work unchanged. Guide/Show remains exactly what it is in
the class chat - this overhaul does not touch the tutor.

## 4. Workstream 1: the brief, the section index, and the unified writer chat

The first slice. Everything later is a pipeline over these primitives.

### 4.1 The section index (`backend/core/sections.py`)

Pure functions over a markdown body. No database, no LLM.

- `parse(body) -> list[Section]` - one entry per ATX heading plus a preamble entry when text
  precedes the first heading. `Section` carries: hierarchical number ("2.1"), title, level,
  line span, char span, word count, and `is_empty` (body is whitespace and/or TODO markers
  only). Setext headings are normalized as level 1/2. Code fences are opaque: a `#` inside a
  fence is not a heading.
- `outline(body) -> str` - the numbered outline with word counts and empty-flags, rendered for
  prompts and for the `read_outline` tool.
- `extract(body, ref) -> Section | None` - by number or case-insensitive title match, kuhn's
  `read_sections.py` addressing rule: refs starting with a digit match by number, all others by
  title.
- `splice(body, section, new_text) -> str` - replace one section's span, byte-exact outside it.

Property tests: `splice(body, s, extract(body, s.ref).text) == body` for every section of every
corpus document; parse/splice round-trips under CRLF, fences, and setext forms.

### 4.2 The brief (`backend/core/briefs.py`)

The brief is how the assistant knows what it is writing. Its lifecycle honors
propose-never-assert:

1. **Discern.** On the first writer interaction with a briefless draft (or on demand via a
   "Set up brief" affordance), the chat profile attempts to fill a brief: from the title and
   any existing body first; failing confidence, it searches the class documents for an
   assignment handout, outline, or rubric whose content matches the title (the
   `search_course_material` and `list_class_documents` tools below - retrieval is the
   cross-reference, and the source document lands in `source_document_id`).
2. **Ask.** If nothing cross-references confidently, the assistant asks - in chat, in plain
   language: what is this, for which class context, roughly how long, anything the grader said.
   One or two questions, not an interview wizard. The student can also just answer "go off
   nothing" and the brief records that.
3. **Confirm.** The proposed brief renders as a card in the rail (fields editable inline).
   Confirming flips `status`; the assistant treats a proposed brief as usable but says so
   ("working from my guess at the assignment - confirm the brief to pin it down").

The confirmed brief is injected into **every** writer prompt - chat turns and pipeline stages -
as a `format_brief_block` alongside the existing facts block. It is the single highest-leverage
piece of context in this document.

### 4.3 Generalizing the tool loop

`backend/llm/tools.py` currently closes over a module-level registry of pure tools. Two changes,
both small and both preserving Phase 2 behavior byte for byte:

- `run_tool_loop(...)` accepts an optional `registry` parameter defaulting to the current
  module-level set. A registry entry is what `_tool()` already builds: name, description,
  schema, callable returning `ToolResult`.
- The purity rule ("tools are pure", rule 3 of the module docstring) becomes a property of the
  Phase 2 registry, not of the loop. Writer tools may touch the database; the loop does not
  care. Every call still lands in `ToolLoopResult.calls` - the transcript rule is what actually
  protects the student, and it is unchanged.

An `on_call` callback (invoked per round with the recorded call) is added so the chat route can
emit activity frames while the loop runs. Pipelines pass nothing and read the transcript after.

### 4.4 The writer's tools (`backend/core/writer_tools.py`)

A factory: `build_registry(conn, artifact_id, profile) -> registry`, granting by profile.

| Tool | Grant | Does |
|------|-------|------|
| `read_brief` | all | The brief, or "no brief yet" |
| `read_outline` | all | `sections.outline` of the current body |
| `read_section` | all | One section's text by number or title |
| `search_course_material` | all | `rag.retrieve` over the class, k trimmed to a token budget, results carrying document/section provenance |
| `list_class_documents` | all | Filenames, kinds, and one-line descriptions |
| `read_comments` | all | Unresolved margin comments (Workstream 3; returns empty until then) |
| `propose_revision` | chat | Section-scoped: takes a section ref and replacement markdown, splices into the current body, lands the result via `suggestions.propose`. The student reviews hunks as today |
| `write_section` | drafter | Direct write of an **empty** section (snapshot first); refuses - as a `ToolResult` failure the model can read - on a non-empty one |
| `add_comment` | reviewer | Files a margin comment with a verbatim quote (Workstream 3) |
| `save_brief` | chat | Writes the proposed brief for the student to confirm |

Two constraints the tools enforce rather than trust:

- **One pending edit per part** (the `suggestions` invariant). `propose_revision` on a draft
  with an open pending edit merges: it splices against the already-proposed content and updates
  the same edit, so the student always reviews one coherent diff.
- **`write_section` re-checks emptiness at write time**, not at loop start - the student may
  have typed into that section while the model was thinking.

### 4.5 The writer chat surface

Backend: `POST /drafts/{artifact_id}/chat/{session_id}/turns` (in `routes_drafts.py`), SSE.
The turn builds the writer-chat system prompt (new, `backend/llm/prompts.py`: the
`_WRITING_CRAFT` bar, the brief block, the outline - cheap and always current - the facts
block, and the tool contract), then runs the tool loop with the chat registry, emitting:

```
data: {"type":"activity","tool":"read_section","label":"Reading \"Methods\""}
data: {"type":"token","text":"..."}          // final answer, streamed when toolless,
data: {"type":"proposed","edit_id":123}      // whole otherwise (see 2)
data: {"type":"done"}
```

Frontend: the rail's Chat tab drops `StepThread` and mounts a `WriterThread` - same visual
grammar as chat, no mode toggle, activity lines rendered inline the way the solver renders its
check transcript. A `proposed` frame flips the rail to the Suggestion tab exactly as `/suggest`
does today.

`/write` (the caret passage) and `/suggest` (the one-shot whole-document revision) remain: the
first is a good micro-tool the chat should not replace; the second becomes a thin alias that
the SuggestDialog keeps using until Workstream 2 replaces its guts, then retires.

### 4.6 Done when

- A briefless draft, on first chat turn, either proposes a brief citing the handout it found in
  the class documents, or asks one plain question.
- "What should my intro do differently?" produces an answer that demonstrably read the intro
  (activity frames say so) and cites course material where it leans on it.
- "Rework the transition between 2 and 3" lands a pending edit touching only those sections.
- Guide/Show is gone from the draft rail. The class chat is untouched.
- Backend tests: brief lifecycle, registry grants by profile, merge-into-one-pending-edit,
  emptiness re-check, activity frame order. Frontend tests: brief card, activity rendering,
  proposed-frame rail flip.

## 5. Workstream 2: the long-draft pipeline

A fifteen-page document is not drafted in one shot, and a good draft goes through once for
structure before any prose. The pipeline (`backend/core/writer_pipeline.py`) is code with two
stages, run as one queued job on the drafting worker:

**Stage 1 - structure.** One drafter run: brief + outline of whatever exists + retrieval on the
brief's summary. Output: a skeleton - headings with one or two sentences of intent and a TODO
marker per section (kuhn's `writerInput` contract). Lands direct if the document is empty
(snapshot first); lands as a pending edit if the student already has structure, and the
**pipeline parks** until that edit is resolved: `state` returns to `ready`,
`stage_detail = 'awaiting structure review'`, and accepting or rejecting the edit is what the
student does next. No hidden background continuation - the student resumes the pipeline
explicitly. Fighting the student for the document's shape is how trust is lost.

**Stage 2 - sections, serially.** For each section in outline order, one drafter run scoped to
that section: the brief, the full (cheap) outline, the tail of the preceding section (~300
words, for the transition), the next section's heading and intent line, and a per-section
retrieval on the section's title + intent (budget ~2,000 tokens, the `/write` precedent).
Empty sections land direct via `write_section`; occupied ones accumulate into the single merged
pending edit. `problems_total/done` counts sections; `stage_detail` names the one in flight.
Each direct write is one revision snapshot, so history reads as the pass proceeded and a crash
loses at most one section. `drafting.reconcile_interrupted` extends to pipeline jobs unchanged:
interrupted means `ready` + "the pass was interrupted; N of M sections landed", never a
corrupted body.

Iteration is the same machinery: "run another pass over sections 3-5 tightening the argument"
is stage 2 with a lens instruction and a section filter. The chat profile can enqueue it (the
route takes an optional instruction and section refs), which is how "constantly iterate and
polish" actually gets exercised.

Done when: an empty draft with a confirmed brief becomes a complete skeleton and then a full
grounded draft, unattended, on the reference model; a mid-run kill leaves a clean partial state
the status endpoint explains; a section the student edits mid-pass is re-read fresh and never
clobbered (the emptiness re-check test, at pipeline scale).

## 6. Workstream 3: the review pass and margin comments

### 6.1 Anchoring (`backend/core/comments.py`)

Kuhn's model, ported: the store is position-dumb. A root comment keeps the verbatim `quote` and
a char-offset `hint`; nothing stores live positions. `resolve_quote(content, quote, hint)`
returns exact matches first (nearest occurrence to the hint), then a whitespace-normalized
fallback with an offset map back to raw coordinates, else None - at which point the comment is
flagged `orphaned`, kept, and listed unanchored (the finding is still worth reading; only its
pin is lost). Port the algorithm from `kuhn/agent-backend/src/db/comments.js` (owner's code,
free to adapt) with tests over the same edge cases: repeated quotes, edits above the anchor,
reflowed whitespace, deleted passages.

### 6.2 The review pipeline

Sequential lenses, each one reviewer run, each filing comments through `add_comment` as it
goes - so a slow model shows findings incrementally, and an interrupted review keeps what it
found. `problems_total/done` counts lenses.

1. **Structure** - outline + brief only: missing moves for the assignment type, ordering,
   balance, sections that do not serve the brief.
2. **Argument and transitions** - walks section pairs with tails, whole-document logic at
   section granularity: does each section earn the next, are claims sequenced, does the
   conclusion follow.
3. **Prose calibration** - per section: the `_WRITING_CRAFT` bar as a checklist - claim
   strength vs. evidence, surplusage, empty intensifiers, abbreviations at first use.
4. **Claims and citations** - per section: extract factual claims and citations, retrieve
   against the course material, and file a comment wherever the source does not say what the
   prose says - or no source exists at all. (Web-cited sources: flagged "not checkable
   locally" until FireCrawl lands. Deferred, not forgotten.)

Severity scale is kuhn's: critical / major / minor / note, with its definitions. The reviewer
prompt carries kuhn's two best conventions verbatim in spirit: **be specific** (quote and say
why) and **do not rewrite** - identifying the problem is its job; the fix is a chat ask away,
where the writer profile reads the comment thread (`read_comments`) and proposes.

A closing chat message summarizes counts by severity and the two findings that matter most.
Comments are the review; the summary is not a restatement.

### 6.3 The editor surface

The real work in this workstream, chosen with eyes open:

- A ProseMirror plugin (sibling to `write-suggestion.ts`) resolves each unresolved comment's
  quote against the live document text (client-side, `doc.textBetween` with a text-to-position
  map), decorates the range with a severity-tinted underline, and re-resolves through position
  mapping on edits - falling back to full re-resolution on structural changes, orphaning on
  failure.
- A **Comments** rail tab: severity-sorted thread list; click scrolls to and flashes the
  anchor; reply and resolve inline; orphaned comments grouped at the bottom with their quotes.
- The student replies in threads; the writer profile reads threads; "address the reviewer's
  comments" becomes a first-class chat ask that proposes revisions and replies to each thread
  with what it did.

Done when: a review of a real essay files anchored, severity-sorted, specific comments; edits
above an anchor do not detach it; a deleted passage orphans its comment without losing it; the
address-the-comments loop closes end to end.

## 7. Workstream 4: PDF export

Small, self-contained, and last. Markdown to typeset PDF via **Typst** (kuhn's renderer;
Pandoc's docx export is explicitly not ported - Lyra's deliverable is the PDF). A
`POST /drafts/{id}/export` endpoint shells out to a bundled or user-installed `typst` binary
with one clean default template (title from the artifact, class name, date); the Export button
replaces Print, and Print remains as the fallback when the binary is absent. Rendering is
arbitrary code execution by another name, so the invocation gets the solver's `_cas_runner`
treatment: no network, temp dir, timeout. Template choice and per-class styles are explicitly
out of scope until the base export earns its keep.

## 8. Build order

| # | Slice | Ships |
|---|-------|-------|
| 1 | Sections + brief + loop generalization | 4.1-4.3, migration 019, backend tests |
| 2 | Writer chat | 4.4-4.6: tools, route, WriterThread, Guide/Show removed from the rail |
| 3 | Draft pipeline | Section 5: structure + section stages, park/resume, iteration passes |
| 4 | Comments substrate + review pipeline | 6.1-6.2: anchoring, lenses, rail list read-only |
| 5 | Comments in the editor | 6.3: decorations, threads, address-the-comments loop |
| 6 | Typst export | Section 7 |

Each slice lands green on `pytest` and the frontend suite before the next begins. Slices 1-2
are the first PR series; nothing in 3-6 changes their public shapes.

## 9. Open questions, parked deliberately

- **FireCrawl bundling** (web search/fetch for the reviewer's citation lens and the chat's
  research asks): waits for the Phase 4 security posture; the tool registry and the "not
  checkable locally" flag are the seams it will land in.
- **Practice problems and study guides on the draft substrate** (roadmap Phase 5): both become
  cheap once the pipeline exists; neither is specified here.
- **Suggestion-mode `/write`**: whether the caret passage should also ground through the brief
  block. Trivially yes, once the brief exists; do it in slice 2.
