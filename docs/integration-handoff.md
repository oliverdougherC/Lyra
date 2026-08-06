# Integration Handoff: Study Tools, Hybrid Retrieval, and the Draft Workspace

This document specifies three workstreams that port the best ideas from two external projects
into Lyra. It is written to be handed to an implementation agent and executed without further
design decisions. Where a decision could have gone two ways, it has been made here, and the
reason is recorded so a reader who disagrees knows what they are disagreeing with.

The two source projects:

- **kuhn** (`/Users/ofhd/Developer/kuhn`) — the owner's own AI-assisted scientific writing
  tool. Node/TypeScript, Milkdown editor, Anthropic Agent SDK backend. Its code may be read
  and adapted freely.
- **NitroAI** (`https://github.com/Blueturboguy07/NitroAI`) — a TypeScript/Electron study
  app. **AGPL-3.0 licensed. Do not clone it, do not open its source, do not copy any code or
  prompt text from it.** Everything Lyra needs from it — the spaced-repetition algorithm, the
  two-phase flashcard design, the quiz schema rules — is re-specified in full in this
  document, in original wording. Implement from this spec only.

## 0. Ground rules — read before writing any code

1. **Lyra's constraints are non-negotiable.** Backend binds to `127.0.0.1` only. CORS
   allowlist is exactly `:3000`. No telemetry, no accounts, no plugin system. Infrastructure
   models (embedding, reranker, OCR) never leave the machine. Course content may be sent to
   the **tutor endpoint** exactly as chat already sends it — the endpoint-locality
   acknowledgement flow in `backend/llm/locality.py` already governs this; none of the new
   features need new gating, because they all ride the same tutor endpoint chat rides.
2. **Generated content is proposed, never asserted.** Every model output the user keeps must
   be visible, correctable, and revisable. The artifact revision machinery
   (`backend/core/artifacts.py`) and the pending-edit flow specified in Workstream 3 are both
   instances of this rule. Do not add a confidence percentage anywhere.
3. **Conventions.** `docs/conventions.md` is binding: ruff (line 100, py312), Google
   docstrings with `Raises:`, long why-comments in the codebase's existing style, prettier +
   eslint + `tsc --noEmit` on the frontend, hand-written TS mirrors of Pydantic schemas in
   `frontend/src/types/index.ts`. Tests defend observable contracts; never call a live model
   in a test; temp SQLite per test; a bug fix adds the test that would have caught it.
   Frontend tests run against a throwing `fetch` (`frontend/tests/setup.ts`).
4. **Sync handlers.** Non-streaming FastAPI handlers are sync `def` (sqlite3 blocks; FastAPI
   threadpools them). Streaming handlers follow `routes_chat.py`.
5. **Response patterns.** Anything outliving a request is a background job with state on the
   owning row and a polled status endpoint (pattern: `backend/core/ingestion.py`,
   `backend/core/solver.py`). Anything watched in real time is SSE (pattern:
   `backend/api/routes_chat.py`).
6. **UI bar.** Every new screen implements all four data states (loading skeleton matching
   real layout, empty, error, populated), honors `prefers-reduced-motion`, uses tokens only
   (no hardcoded colors), works in both themes, and is keyboard-navigable. Server state goes
   through react-query hooks in `frontend/src/lib/hooks/`; all HTTP through
   `frontend/src/lib/api.ts`.
7. **Branches and commits.** One branch per workstream off `dev`:
   `feature/hybrid-retrieval`, `feature/study-tools`, `feature/draft-workspace`. Conventional
   commits (`feat:`, `fix:`, `test:`, `docs:`), small and atomic, subject lines in the
   repo's prose-like style.
8. **Order.** Workstream 1, then 2, then 3. Workstream 2's migration (016) is a prerequisite
   of Workstream 3's (017). Workstream 1 is independent but improves the grounding quality of
   both others, which is why it goes first.
9. **Measurement is part of done.** Workstream 1 is not done until its numbers are recorded.
   Where this document names a constant as *provisional*, the eval harness decides whether it
   survives, and the result — either way — gets recorded in the relevant doc.

### Reference files the implementer may open

| Purpose | File |
| --- | --- |
| Suggestion-mode algorithm (behavioral reference) | `/Users/ofhd/Developer/kuhn/agent-backend/src/pending-edits.js` |
| `/write` widget (adapt directly; owner's code) | `/Users/ofhd/Developer/kuhn/webapp/src/write-suggestion.ts` |
| Milkdown Crepe assembly reference | `/Users/ofhd/Developer/kuhn/webapp/src/editor-core.ts` |
| FTS5 query shape and term sanitization | `/Users/ofhd/Developer/kuhn/agent-backend/src/db/org-documents.js` |
| Writing-craft source material (adapt) | `/Users/ofhd/Developer/kuhn/guidance-docs/shared/scientific-writing-style-guide.md` |
| Known retrieval failure case to reproduce | `docs/phase-3-verification-handoff.md`, `docs/phase-3-handoff.md` |

Do **not** open anything from NitroAI (AGPL — see above). Do not lift kuhn's agent runtime
(`agents/runtime.js`), Yjs stack, auth, or multi-tenancy; they are listed in §4.

---

## 1. Workstream 1 — Hybrid retrieval: BM25 beside the vectors

**What this discharges.** The open Phase 3 item "Lexical retrieval before a new embedding
model, measured on the answer-key case" (`docs/feature-roadmap.md`, Phase 3). The known
failure: a problem set restates its questions verbatim in its answer key, the embedder cannot
tell the documents apart, and the key never appears in the top 128 by cosine distance. The
words are identical — the textbook case for lexical matching.

### 1.1 Migration `015_chunks_fts.sql`

External-content FTS5 table over `chunks.content`, kept in sync by triggers:

```sql
-- Lexical index beside the vectors. External-content: FTS5 stores only the index and
-- reads text back from `chunks`, so content is never duplicated. Porter stemming so
-- "integrating" matches "integrate"; unicode61 handles the math-adjacent punctuation.
create virtual table chunks_fts using fts5(
  content,
  content='chunks',
  content_rowid='id',
  tokenize='porter unicode61'
);

insert into chunks_fts(rowid, content) select id, content from chunks;

create trigger chunks_fts_insert after insert on chunks begin
  insert into chunks_fts(rowid, content) values (new.id, new.content);
end;
create trigger chunks_fts_delete after delete on chunks begin
  insert into chunks_fts(chunks_fts, rowid, content) values ('delete', old.id, old.content);
end;
create trigger chunks_fts_update after update of content on chunks begin
  insert into chunks_fts(chunks_fts, rowid, content) values ('delete', old.id, old.content);
  insert into chunks_fts(rowid, content) values (new.id, new.content);
end;
```

**Required tests** (in a new `backend/tests/test_lexical.py`):
- FTS5 is available in the bundled SQLite (`select fts5_version()` succeeds) — so a missing
  compile flag fails with a diagnosis, not a mystery.
- Inserting chunks makes them findable; deleting a **document** removes its chunks from the
  index (this exercises the cascade-delete → trigger interaction; if SQLite's FK cascade
  does not fire the delete trigger on some build, fall back to an explicit
  `delete from chunks where document_id = ?` in the document-delete path — the test decides).
- Re-ingesting a document leaves no orphaned FTS rows.

### 1.2 Lexical search in `backend/rag/retrieve.py`

New module-level pieces:

```python
LEXICAL_FETCH_K = 64   # same width the reranker already reads
RRF_K = 60             # standard reciprocal-rank-fusion constant
# Provisional; §1.4 measures it. Half of a single list's top-rank contribution, so it
# breaks ties between comparable fused scores without promoting a weak match: the same
# philosophy as RECENCY_COEFFICIENT.
SOLUTIONS_RRF_BONUS = 1.0 / (2 * (RRF_K + 1))
```

`_lexical_ranks(conn, class_id, query, limit, document_id=None) -> list[int]` (chunk ids in
BM25 order):

- Sanitize the query the way kuhn's `sanitizeFtsTerms` does: split on whitespace, strip `"`
  from each term, drop empties, wrap each term in double quotes so FTS5 operator words
  (`AND`, `OR`, `NEAR`, parens) are always literals. Zero terms → empty result.
- First query joins all quoted terms with implicit AND. If that returns zero rows **and**
  there are ≥ 2 terms, retry joined with `OR` — one absent word must not zero the result;
  BM25 still ranks chunks matching more terms higher.
- SQL shape (class-scoped, ready-only — the `d.state = 'ready'` rule in `_READY_ONLY`
  applies to every retrieval path, including this one):

```sql
select c.id
from chunks_fts
join chunks c on c.id = chunks_fts.rowid
join documents d on d.id = c.document_id
where chunks_fts match ? and c.class_id = ? and d.state = 'ready'
order by bm25(chunks_fts)
limit ?
```

- When `document_id` is pinned, add `and c.document_id = ?`.

### 1.3 Fusion inside `retrieve()`

Replace the current candidate-selection block (structural resolution stays exactly as it is,
in front, deduped after, untouched):

1. Vector candidates: always fetch `RERANK_FETCH_K` (64) — fusion needs a deep list even
   when no reranker is installed. (The old rationale for fetching only `K` without a
   reranker was that the surplus fell into the budget; under fusion only the fused top-`K`
   is served, so the surplus never reaches the budget.)
2. Lexical candidates: `_lexical_ranks(...)` with `LEXICAL_FETCH_K`.
3. **Reciprocal rank fusion.** For each chunk appearing in either list, with 1-based rank
   `r` in each list it appears in: `fused = Σ 1 / (RRF_K + r)`. The vector list is ranked by
   the existing `score` (similarity + recency bonus), so recency keeps its effect. Then add
   `SOLUTIONS_RRF_BONUS` to any chunk whose `doc_type == 'solutions'` (the documents-type
   boost toward answer keys the roadmap item names).
4. Load any lexical-only chunks through `_SCORED_CHUNK_SELECT` restricted by `c.id in (...)`
   so every candidate carries a real `similarity` (the docstring contract on
   `RetrievedChunk.similarity` — always the embedder's measurement — must keep holding).
   The fused value goes in `score`.
5. If the reranker is available: rerank the fused top-64 (dedup first), exactly as today.
   If not: serve the fused order. Budgeting, `_expand_problem_parts`, trim reporting — all
   unchanged.
6. Document-pinned retrieval fuses the same way within the document scope.

Update the module docstring and the `RERANK_FETCH_K` comment block to record the new shape,
in the codebase's measured-comment style — after §1.4 supplies the numbers.

### 1.4 Measurement (this closes the workstream, not the code)

1. Read `docs/phase-3-handoff.md` and `docs/phase-3-verification-handoff.md` for the
   answer-key failure case. Add question(s) reproducing it to
   `scripts/eval_questions/ece203-class.json` with `expect_document` set to the answer-key
   document, following the existing entry format.
2. Run `scripts/eval_ingest.py retrieve` on the textbook set and the class set, before and
   after the change (before = `main`, after = the branch).
3. **Acceptance:** the answer-key question lands in the served eight. The textbook set does
   not regress from 17/17 at k=8. The class set does not regress below 12/16 rank-1 with
   the reranker. If `SOLUTIONS_RRF_BONUS` does not move the answer-key case, or hurts
   another, delete the bonus and record that it was tried — the roadmap item requires the
   *measurement*, not the boost.
4. Record the numbers: check the roadmap item off with a short results note (the existing
   items show the format), and move "Hybrid retrieval" from `docs/rag-pipeline.md`'s
   future-extensions list into the pipeline description.

### 1.5 Unit tests

`backend/tests/test_lexical.py` additions: term sanitization (quotes stripped, operator
words neutralized, empty query), AND→OR fallback fires only on zero rows with ≥2 terms,
class scoping (a matching chunk in another class never returns), ready-only filtering,
document pinning, RRF math on hand-built lists (a chunk in both lists outranks a chunk that
is top of one list only), solutions bonus applies only to `doc_type='solutions'`, and
fusion determinism (stable order on ties — break ties by `chunk_id` ascending).

---

## 2. Workstream 2 — Study tools: flashcard decks and quizzes

**What this is.** The first two Phase 5 items — "Flashcard generation with spaced
repetition" and "Quiz mode with score tracking and weakness identification" — built as new
artifact kinds on the existing artifact substrate, grounded through `retrieve()`, with
provenance. The design (two-phase generation, per-type quiz rules, the scheduler) is ported
from NitroAI *as specified here*; the implementation and prompt text below are original and
must not be checked against the AGPL source.

### 2.1 Migration `016_study_artifacts.sql`

SQLite cannot alter `check` constraints, so three tables are rebuilt. `migrate()` runs
scripts through `executescript` on a connection with foreign keys on; the script must
manage that itself:

```sql
pragma foreign_keys = off;
```

then for each of `artifacts`, `artifact_parts`, `artifact_sources`: create `<name>_new`
with the identical column list and the widened checks below, `insert into <name>_new
select * from <name>`, `drop table <name>`, `alter table <name>_new rename to <name>`,
recreate that table's indexes exactly as `001`/`005` defined them, and finally:

```sql
pragma foreign_keys = on;
```

Widened checks (everything else in each table stays byte-identical, including the
solver-era column names `problems_total`/`problems_done`, which are reused as generic
progress counters — renaming them would ripple through the solver, the API schemas, and
the frontend types for zero user-visible gain; say so in a comment):

- `artifacts.kind` → `('solution_set','flashcard_deck','quiz','draft')`
- `artifacts.state` → `('pending','segmenting','awaiting_review','solving','generating','ready','failed','cancelled')`
- `artifact_parts.kind` → `('problem','step','answer','figure','card','quiz_question','draft_body')`
- `artifact_parts.content_type` → `('markdown','image','json')`
- `artifact_sources.role` → `('problem_set','reference_solutions','study_source')`

New tables, same migration:

```sql
-- Scheduling state is not part content: it changes on every review, has no revision
-- history, and dies with its card. One row per card part.
create table card_states (
  part_id integer primary key references artifact_parts(id) on delete cascade,
  due_at text not null,
  stability real not null default 0,
  difficulty real not null default 5,
  reps integer not null default 0,
  lapses integer not null default 0,
  state text not null default 'new'
    check (state in ('new','learning','relearning','review')),
  last_review_at text
);

create table card_review_log (
  id integer primary key autoincrement,
  part_id integer not null references artifact_parts(id) on delete cascade,
  rating text not null check (rating in ('again','hard','good','easy')),
  reviewed_at text not null default (datetime('now'))
);
create index idx_review_log_part on card_review_log(part_id);

create table quiz_attempts (
  id integer primary key autoincrement,
  artifact_id integer not null references artifacts(id) on delete cascade,
  started_at text not null default (datetime('now')),
  finished_at text
);
create index idx_attempts_artifact on quiz_attempts(artifact_id);

create table quiz_answers (
  attempt_id integer not null references quiz_attempts(id) on delete cascade,
  part_id integer not null references artifact_parts(id) on delete cascade,
  selected_index integer not null,
  correct integer not null,
  answered_at text not null default (datetime('now')),
  primary key (attempt_id, part_id)
);
```

**Required migration test** (`backend/tests/test_migration_016.py`): seed a pre-016
database with a solver artifact (artifact + sources + parts + revisions + provenance +
checks), run `migrate`, assert every child row survives, `pragma foreign_key_check`
returns no rows, and both an insert with `kind='flashcard_deck'` and one with the old
`kind='solution_set'` succeed.

### 2.2 Content model

- **Card**: leaf part, `kind='card'`, `content_type='json'`, `label` = topic,
  `content` = `{"front": "<markdown>", "back": "<markdown>", "topic": "<str>"}`.
- **Quiz question**: leaf part, `kind='quiz_question'`, `content_type='json'`, `label` =
  topic, `content` = `{"type": "mcq"|"true_false"|"fill_blank", "question": "<markdown>",
  "options": ["<str>", ...], "correct_index": <int>, "explanation": "<markdown>",
  "topic": "<str>", "difficulty": "basic"|"intermediate"|"exam"}`.

JSON leaf parts keep the whole existing machinery for free: revisions on edit, provenance
per part, ordinal ordering, cascade delete. Markdown inside the JSON strings renders
through the existing KaTeX pipeline client-side. Server-side validation of the payload
shape happens at generation-parse time and on every PATCH (reject malformed JSON with a
422, per-type rules from §2.5).

### 2.3 The scheduler — `backend/core/scheduler.py`

Pure functions, no I/O, no ambient clock: every function takes `now: datetime` (UTC-aware)
from the caller. This is a simplified FSRS/SM-2 hybrid. Store `due_at`/`last_review_at` as
ISO-8601 UTC strings (`datetime('now')`-compatible format) so lexicographic comparison in
SQL is chronological.

Model: `stability` is estimated memory strength in days (a card with stability S should
still be recallable ~S days after its last success); it only grows on success and only
shrinks — never below a positive floor — on a lapse. `difficulty` is a 1–10 ease knob kept
for display; scheduling keys off the rating directly. Ratings: `again | hard | good | easy`.

Constants:

| Constant | Value |
| --- | --- |
| Initial difficulty | 5.0, clamped to [1, 10] forever after |
| Difficulty delta per rating | again +1.0, hard +0.5, good −0.1, easy −0.5 |
| Stability seed floor | 1.0 day (applied before scaling, so a fresh card never multiplies zero) |
| Stability factor per success | hard ×1.2, good ×2.0, easy ×2.8 |
| Lapse decay | stability ×0.2, floored at 0.5 days |
| Relearn interval on `again` | 10 minutes (10/1440 days) |
| Mastered threshold | state `review` and stability ≥ 21 days |

Functions and contracts:

- `new_card_state(now) -> CardState`: due immediately (`due_at = now`), stability 0,
  difficulty 5, reps 0, lapses 0, state `new`, `last_review_at` None.
- `review(card_state, rating, now) -> CardState` (returns a new value, does not mutate):
  - `reps += 1`; difficulty adjusted by the delta table and clamped.
  - `again`: `lapses += 1`; stability = `max(stability * 0.2, 0.5)`; interval = 10 min;
    state → `relearning` if the card was in `review` or `relearning`, else stays
    `learning` (a card that never graduated has nothing to lapse out of).
  - success (`hard|good|easy`): stability = `max(stability, 1.0) * factor`; interval in
    days = new stability; state → for a `new` card, `review` if rated `easy` (the learner
    already knows it) else `learning`; for every other state, `review`.
  - `due_at = now + interval`; `last_review_at = now`.
- `bucket(card_state) -> 'new'|'learning'|'mastered'`: `new` if reps 0 or state `new`;
  `mastered` if state `review` and stability ≥ 21; else `learning`.
- `study_order(card_states, now) -> ordering`: due cards first (state priority `new`=0,
  `learning`/`relearning`=1, `review`=2; ties by soonest due), then not-yet-due cards by
  soonest due, so a session queue never runs dry.

**Tests** (`backend/tests/test_scheduler.py`, written fresh): a new card is due
immediately; `easy` on a new card fast-tracks to `review`; `good` twice grows the interval
monotonically; `again` on a review card goes to `relearning` with a 10-minute due and
decayed-but-positive stability; difficulty clamps at both ends; a lapse then a success
returns to `review`; bucket boundaries at reps 0 and stability 21; study order interleaves
per the priority table; determinism given a fixed `now`.

### 2.4 Generation pipeline — `backend/core/study.py`

Mirror `backend/core/ingestion.py`: an in-memory queue, one worker thread started from the
`main.py` lifespan, state on the artifact row, `reconcile_interrupted_study()` at startup
marking any `flashcard_deck`/`quiz` artifact in `pending`/`generating` as `failed` with
"interrupted by restart" (match the wording pattern the solver reconcile uses).

**Deck creation** (`POST /api/classes/{id}/decks`, body
`{title: str, document_ids?: [int], cards_per_topic?: int (default 4, clamp 2–6)}`):

- If the class has no `ready` documents (or none of the named ones are `ready`): reject at
  the route with 409 and an honest detail — no artifact row for a job that cannot start.
- Create artifact `kind='flashcard_deck'`, `state='pending'`, one `artifact_sources` row
  per source document with `role='study_source'`; enqueue; return 202 with the artifact.
- Worker stages (update `stage_detail` as each starts):
  1. `state='generating'`. Gather topic-extraction input: iterate the source documents
     (default: every `ready` document in the class); for each, concatenate its chunks in
     document order up to 6,000 estimated tokens (`backend/rag/tokens.estimate_tokens`),
     capped at 12,000 total across documents, round-robin one document at a time so one
     textbook cannot crowd out the syllabus.
  2. Topics: one constrained-JSON call (§2.5) returning 4–8 topic strings.
     `problems_total = len(topics)`.
  3. Per topic: `retrieve(conn, class_id, topic, budget_tokens=2500)` → context block via
     `format_context_block`; one constrained-JSON call returning cards for that topic;
     create each card as a part (§2.2) with sequential ordinals across the deck, part
     `status='complete'`; insert its `card_states` row via `new_card_state(now)`; record
     provenance (`set_provenance`) for the topic's top 3 retrieved chunks with
     `label` = topic; then `increment_problems_done`.
  4. A topic whose call fails is skipped and counted in `stage_detail`
     ("2 of 6 topics failed"); zero cards overall → `state='failed'` with the underlying
     error message. Otherwise `state='ready'`.

**Quiz creation** (`POST /api/classes/{id}/quizzes`, body `{title: str, document_ids?:
[int], count?: int (default 10, clamp 3–30), difficulty?: 'basic'|'intermediate'|'exam'
(default 'intermediate'), types?: subset of ['mcq','true_false','fill_blank'] (default all)}`):

- Same 409 guard, same artifact/sources/enqueue shape, `kind='quiz'`.
- Worker: gather input as deck step 1; one constrained-JSON call (§2.5) for `count`
  questions; validate each against the per-type rules below, dropping invalid items; if
  fewer than half of `count` survive, retry once appending the validation failures to the
  prompt; if fewer than 3 survive after that, `state='failed'`. Create surviving questions
  as parts, `problems_total = count`, `problems_done` = survivors, provenance = top 3
  chunks of the gathered input's source documents is **not** meaningful here, so instead
  record provenance per question only when the generation context came from `retrieve`
  (it does not in this pipeline) — skip provenance for quiz v1 and say so in a comment.

Per-type validation (enforced in code, not trusted from the model): `mcq` — exactly 4
distinct options, `correct_index` in 0–3. `true_false` — options exactly
`["True", "False"]`, `correct_index` in 0–1. `fill_blank` — exactly 1 option,
`correct_index` 0, question contains `___`. All types — non-empty question, non-empty
explanation, topic present.

### 2.5 Prompts — new builders in `backend/llm/prompts.py`

Follow the existing builder style (`build_segmentation_prompt` etc.: return
`list[dict[str, str]]` message lists; JSON schemas as `JsonSchema` for the client's
constrained decoding). Prompt text below is the spec; adjust phrasing only to match the
file's voice.

`build_topics_prompt(source_text)` — system:

> You are mapping course material so a student can study it. Read the material and name
> the 4 to 8 topics that together cover what it teaches. A topic is a short noun phrase of
> two to four words, specific enough to study ("eigenvalue decomposition", not "math").
> Prefer the course's own terminology. Return JSON only.

Schema: `{"topics": [str]}`, required, no additional properties.

`build_flashcards_prompt(topic, context_block, cards_per_topic)` — system:

> You are writing flashcards for the topic "{topic}", grounded in the course material
> below. Each card tests one atomic fact or skill: the front is a question or prompt that
> forces recall (never yes/no), the back is a complete, self-contained answer a student
> could check themselves against. Use the notation the course material uses; write math in
> KaTeX ($...$ inline, $$...$$ display). Write {cards_per_topic} cards. Base every card on
> the material provided; if the material does not support that many distinct cards, write
> fewer rather than inventing content.

followed by the context block. Schema:
`{"cards": [{"front": str, "back": str, "topic": str}]}`.

`build_quiz_prompt(source_text, count, difficulty, types)` — system:

> You are writing a {count}-question quiz at {difficulty} difficulty from the course
> material below, using only these question types: {types}. Rules by type — mcq: exactly
> four plausible options with one correct; the wrong options must be believable mistakes,
> not filler. true_false: the options are exactly ["True", "False"]. fill_blank: the
> question contains a ___ blank, the options array holds exactly the one correct answer,
> and correct_index is 0. Every question carries a one-or-two-sentence explanation of why
> the answer is correct, and a topic label. Use the course's notation; math in KaTeX.
> Base every question on the material provided.

Schema mirrors §2.2's question payload (array field `questions`, `correct_index` integer,
enums for `type` and `difficulty`).

### 2.6 API — `backend/api/routes_study.py`

All sync `def`, errors through `LyraError` subclasses, schemas in
`backend/api/schemas.py`:

- `POST /api/classes/{id}/decks` → 202, artifact json (above).
- `POST /api/classes/{id}/quizzes` → 202, artifact json.
- `GET /api/classes/{id}/study` → decks and quizzes for the hub panel: artifact fields
  plus, per deck, counts by bucket and `due_count` (join `card_states`).
- `GET /api/decks/{artifact_id}` → deck + cards (part id, payload, label, card_state).
  404 if the artifact is not `kind='flashcard_deck'` (same guard style for every route
  here: kind-checked, class-scoped through the artifact row).
- `GET /api/decks/{artifact_id}/session?limit=20` → cards in `study_order`, each flagged
  `due: bool`.
- `POST /api/cards/{part_id}/review` body `{rating}` → applies `review(...)`, updates
  `card_states`, appends `card_review_log`, returns the new state. 409 if the deck's
  artifact is not `ready`.
- `PATCH /api/cards/{part_id}` body `{front, back, topic}` → validates, serializes,
  `set_part_content(origin='user_corrected', note='card edited')`. Scheduling state is
  untouched (editing a card does not reset progress; a comment says so).
- `DELETE /api/cards/{part_id}` → `delete_part` (cascade removes state and log).
- `GET /api/quizzes/{artifact_id}` → quiz + questions (full payloads including
  `correct_index` and `explanation`: Lyra is local and trusts the user; the UI, not the
  API, controls when the answer is revealed).
- `POST /api/quizzes/{artifact_id}/attempts` → `{attempt_id, question_part_ids}`.
- `POST /api/attempts/{attempt_id}/answers` body `{part_id, selected_index}` → grades
  against the stored payload, upserts `quiz_answers`, returns
  `{correct, correct_index, explanation}`. 409 on a finished attempt.
- `POST /api/attempts/{attempt_id}/finish` → sets `finished_at`, returns
  `{score, total, by_topic: [{topic, correct, total}]}` — the weakness surface: the UI
  highlights topics under 60% accuracy.
- `PATCH`/`DELETE` on `/api/decks/{id}` and `/api/quizzes/{id}` → rename/delete via the
  existing `rename_artifact`/`delete_artifact`.
- Status polling reuses the pattern of `GET /solutions/{id}/status` — add
  `GET /api/decks/{id}/status` and `GET /api/quizzes/{id}/status` returning state,
  stage_detail, problems_total/done.

**Tests** (`backend/tests/test_study.py`, plus route tests following the existing route
test style): pipeline happy path with a stubbed LLM client (topics → cards → parts +
states + provenance, counters correct), per-type quiz validation drops and retry
behavior, empty-class 409, reconcile marks interrupted, review endpoint round-trips
through the scheduler, grading correctness incl. finished-attempt 409, card edit
preserves scheduling state, kind-guard 404s.

### 2.7 Frontend

- `HUB_TABS` (`frontend/src/components/classes/class-hub.tsx:47`) gains `'study'` between
  `'solutions'` and `'documents'`. New `class-study-panel.tsx` modeled on
  `class-solutions-panel.tsx`: two sections (Decks, Quizzes), each row showing title,
  state (generating rows poll status like solutions do), deck rows show bucket counts and
  a "N due" badge, create dialogs for both (title, source documents multi-select
  defaulting to all, and the per-kind options from §2.4).
- Route `frontend/src/app/classes/[id]/study/[artifactId]/page.tsx` renders by kind:
  - **Deck session**: one card centered; front shown; Space or click flips; rating row
    Again/Hard/Good/Easy bound to keys 1–4, each showing its next interval (from a dry-run
    of the scheduler mirrored in TS — implement the same pure functions in
    `frontend/src/lib/scheduler.ts` with the constants table above, tested in Vitest
    against the same cases as the Python tests so the two cannot drift). Flip animation is
    a CSS transform, replaced by an instant swap under `prefers-reduced-motion`. Card
    text renders through the existing markdown/KaTeX components (`math-text.tsx`).
    Session end screen: counts by rating, buckets after.
  - **Quiz runner**: one question at a time, options as buttons (fill_blank gets a text
    input compared case-insensitively and whitespace-trimmed against the stored answer,
    then mapped to `selected_index` 0 on match — send `selected_index: -1` on no match
    and have the API treat any index ≠ `correct_index` as incorrect), immediate
    reveal with explanation after answering, progress indicator, summary screen with
    score and the per-topic bars (topics under 60% styled with the destructive token).
- Types mirrored by hand in `frontend/src/types/index.ts`; hooks
  `use-study.ts` (react-query, polling only while `state` is `pending`/`generating`,
  matching the solutions hooks' interval).
- All four data states on both the panel and the session/runner screens. Verify 1280/768/
  375 and both themes.

---

## 3. Workstream 3 — The draft workspace: essay writing

**What this is.** A new surface: free-form documents the student writes with AI
assistance, grounded in the class's own material. Three mechanisms, all ported from kuhn:
a Milkdown WYSIWYG markdown editor, the `/write` inline streamed suggestion (AI text lives
outside the document until accepted), and suggestion-mode pending edits (whole-document AI
revisions reviewed hunk by hunk, server-authoritative). No filesystem involvement — drafts
are artifacts in the database, so nothing here touches Phase 4's security boundary.

### 3.1 Data model — migration `017_drafts.sql` (requires 016)

Draft = artifact `kind='draft'` with exactly one body part: `kind='draft_body'`,
`content_type='markdown'`, ordinal 1, no children. The artifact `title` is the document
title. Revisions, provenance, and step-scoped chat (`chat_sessions.artifact_part_id`)
attach to the body part and work unchanged.

```sql
-- One pending AI revision per draft body, reviewed hunk by hunk. Base and proposed are
-- full blobs; hunks are DERIVED at read time and never stored. `note` is the user's
-- instruction, carried into the revision note on accept.
create table pending_edits (
  id integer primary key autoincrement,
  part_id integer not null unique references artifact_parts(id) on delete cascade,
  base_content text not null,
  base_hash text not null,
  proposed_content text not null,
  stale integer not null default 0,
  note text,
  created_at text not null default (datetime('now')),
  updated_at text not null default (datetime('now'))
);
```

### 3.2 Suggestion algorithm — `backend/core/suggestions.py`

Port the behavioral contract of kuhn's `pending-edits.js` (read it; the contract below is
binding) onto `difflib`:

- `compute_hunks(base: str, proposed: str) -> list[Hunk]`: split both into lines
  (keepends); `difflib.SequenceMatcher(None, base_lines, proposed_lines)
  .get_grouped_opcodes(2)` — context 2, matching kuhn. Each `Hunk` carries `index`,
  `old_start`, `old_lines`, `new_start`, `new_lines`, `lines` (unified-diff style: ` `
  context, `-` removed, `+` added, newlines stripped for display), and
  `hash` = sha256 of `"\n".join(lines)`. The hash guards accept/reject races: the client
  echoes `{index, hash}` and a mismatch means the hunk set moved under it.
- `apply_hunk(content, hunk) -> str | None`: apply ONE hunk to base-side content at its
  old-side coordinates with exact context match (no fuzz). None if it does not apply.
- `unapply_hunk(content, hunk) -> str | None`: remove one hunk from proposed-side content
  at its new-side coordinates — the per-hunk reject primitive.
- `apply_patch(content, base, proposed) -> str | None`: apply the whole diff(base,
  proposed) to `content`, exact context — used only by staleness refresh.
- **Staleness refresh** (`refresh(conn, row, current_content) -> row | None`), run on
  every read and before every accept/reject:
  - `sha256(current) == base_hash` → fresh, unchanged.
  - `current == proposed_content` → the user made the proposed change themselves; delete
    the row, return None (resolved).
  - Otherwise try `apply_patch(current, base, proposed)`: success and ≠ current → rebase
    (base := current, proposed := patched, stale := 0); success and == current → delete,
    None; failure → `stale := 1`, blobs untouched.
- `propose(conn, part_id, proposed_content, note)`: base = the part's current content at
  FIRST proposal; a later proposal to the same part replaces `proposed_content` only
  (coalesce — the stored base survives, which is what makes sequential AI passes
  coherent).
- `accept(conn, id, hunk=None, force=False)`:
  - stale and not force → 409 ("review side by side; accept with force replaces the
    document").
  - hunk accept (fresh only): verify `{index, hash}` against freshly computed hunks (409
    on mismatch or failed apply); new base = `apply_hunk(base, hunk)`; write the new base
    to the part via `set_part_content(origin='generated',
    note=f"Accepted suggestion: {note}")`; recompute remaining hunks; delete the row if
    none remain, else update `base_content`/`base_hash`.
  - accept all (or force on stale): part content := `proposed_content` via
    `set_part_content` (same origin/note); delete the row.
- `reject(conn, id, hunk=None)`: no hunk → delete the row. Hunk (fresh only; stale → 409
  "reject all or review side by side"): verify, `unapply_hunk` from proposed, delete row
  if no hunks remain else update proposed. Never writes part content.

Every accept writes a normal part revision, so the existing revision history and restore
UI are the undo story. **Tests** (`backend/tests/test_suggestions.py`): hunk round-trip
(accept every hunk one at a time == accept all), reject one hunk leaves the rest intact,
hash race 409, stale detection on user edit, rebase when the user edited elsewhere in the
document, self-resolution delete, coalesce keeps first base, force accept on stale,
empty-diff propose (base == proposed) creates nothing.

### 3.3 API — `backend/api/routes_drafts.py`

- `POST /api/classes/{id}/drafts` `{title}` → creates artifact (`state='ready'`) + empty
  body part; returns it. Drafts are born empty; the first AI pass comes through
  `/suggest`.
- `GET /api/classes/{id}/drafts`; `GET /api/drafts/{id}` (artifact + body content +
  whether a pending edit exists); `PATCH /api/drafts/{id}` `{title}`;
  `DELETE /api/drafts/{id}`. All kind-guarded (`404` when not a draft).
- `PATCH /api/drafts/{id}/body` `{content, snapshot?: bool, note?: str}` — the autosave
  path. Default (`snapshot` false): update the part content **without** writing a
  revision; add a `record_revision: bool = True` parameter to
  `core/artifacts.set_part_content` for this (a debounced autosave every 1.5 s would
  otherwise bury the meaningful revisions; kuhn made the same call — autosave to storage,
  history at explicit points). `snapshot: true` writes a revision
  (`origin='user_corrected'`, note = the provided note or "snapshot"). Every `/suggest`
  accept also writes a revision (§3.2), so AI changes are always in history.
- `POST /api/drafts/{id}/write` — **SSE**, the `/write` inline generation. Body
  `{instruction, heading?: str, selection?: str, nearby?: str}` (the client gathers these
  from the editor exactly as kuhn's `gatherContext` does). Stateless: no session row, no
  persistence — the suggestion lives only in the client until accepted, which lands it in
  the document through the normal autosave. Stream the existing chat frame subset
  (`token`, `done`, `error`). Prompt: §3.5, grounded with
  `retrieve(conn, class_id, instruction + " " + (selection or heading or ""),
  budget_tokens=2000)`.
- `POST /api/drafts/{id}/suggest` `{instruction}` → 202. Background job on the study
  worker's pattern (its own small queue in `backend/core/drafting.py`; artifact
  `state='generating'` → `'ready'`, reconcile-on-startup marks interrupted runs back to
  `ready` — the draft itself is intact; only the suggestion run died, and `stage_detail`
  says so). The job: full body content + instruction + retrieval context (2,500 tokens,
  query = instruction) + class profile facts → one model call producing the complete
  revised document (§3.5) → `propose(...)`. A response that is empty or byte-identical to
  the base sets `stage_detail` to "no changes suggested" and proposes nothing.
- `GET /api/drafts/{id}/pending` → the refreshed edit in REST shape: `{id, stale, note,
  hunks: [...], proposed_content, base_content? (stale only)}`, or `null`.
- `POST /api/pending-edits/{id}/accept` `{hunk?: {index, hash}, force?}` and
  `POST /api/pending-edits/{id}/reject` `{hunk?}` → §3.2, returning
  `{remaining: int, edit?: ...}`. 409s surface with their honest messages.

Chat about the draft: the existing sessions API with `artifact_part_id` = the body part —
zero new chat code; the drafts workspace links "Discuss this draft" into the step-thread
component exactly as solution steps do.

### 3.4 Editor frontend

Dependencies (add to `frontend/package.json`): `@milkdown/crepe` and `@milkdown/kit`,
pinned `^7.21.2` (the version kuhn runs in production — known good). **No** collab
plugin, no Yjs, no CodeMirror.

- `frontend/src/components/drafts/draft-editor.tsx` — client component,
  `next/dynamic` with `ssr: false`. Assemble Crepe per kuhn's `editor-core.ts` reference
  (CrepeBuilder, minimal feature set; skip everything Yjs/collab/room-related). Autosave:
  1.5 s debounce after the last change → `PATCH .../body`; flush on unmount and on
  `visibilitychange` hidden. A dirty/saved indicator in the workspace header (writing
  tools earn honest save state).
- **`/write` widget**: adapt `write-suggestion.ts` from kuhn (it is deliberately
  self-contained). Keep: the widget-decoration architecture (suggestion never enters the
  document, autosave, or undo until accepted), position mapping across edits, plain-text
  streaming with parse-on-accept via Milkdown's parser (`toSlice`), Esc-to-dismiss at the
  document level, the reveal animation with the `prefers-reduced-motion` instant path,
  single-active-suggestion. Replace: `runAgentTask` → the new `/write` SSE call through
  `lib/api.ts`; kuhn's icons/status/toast → lucide icons and sonner; class names → Lyra
  tokens. Trigger: a slash command is out of scope for v1 — a "Draft with AI" button in
  the workspace toolbar plus `Mod-/` keybinding both call `startWrite` at the caret.
- **Suggestion review panel** — `components/drafts/suggestion-panel.tsx`, a right-rail
  panel (not inline decorations; kuhn's inline hunk decorations are its hardest 575 lines
  and the server contract is identical either way — inline rendering is named as a
  follow-up in §5, not silently dropped). Renders each hunk as a small card: removed
  lines and added lines styled with the existing destructive/success token pairs, ✓ and ✗
  per hunk, Accept-all / Reject-all in the header, the instruction (`note`) as the panel
  title. On any accept: refetch the draft body and reset the editor content. Stale state:
  the panel swaps to a two-pane view (current vs. proposed, plain rendered markdown) with
  Reject / Replace-document (force) actions and one sentence explaining why hunks no
  longer anchor.
- Workspace route `frontend/src/app/classes/[id]/drafts/[artifactId]/page.tsx`: editor
  center; right rail tabs for Suggestion (when one exists), History (existing
  revision-history component against the body part), Chat (step-thread). Header: title
  (inline rename), save state, "Suggest changes" (opens instruction input → `/suggest`,
  then polls status until the pending edit appears), Snapshot, Print. Export = print, via
  the existing `@media print` pattern; the editor content renders cleanly (verify KaTeX).
- Hub: `HUB_TABS` gains `'drafts'` after `'study'`; `class-drafts-panel.tsx` modeled on
  the solutions panel. Verify the tab row at 375 px — with seven tabs it must scroll or
  wrap without breaking the shell; match whatever the existing overflow behavior is.
- Vitest coverage: hunk-panel rendering from a fixture edit, accept/reject wiring
  (mocked api), autosave debounce + flush, the write-widget context gatherer.

### 3.5 Prompts — additions to `backend/llm/prompts.py`

`_WRITING_CRAFT` — a shared block, distilled from kuhn's style guide (owner's content;
adapt wording to student essays, not clinical prose):

> Write so the reader never has to re-read. Every claim carries exactly the confidence
> its evidence earns: "shows" only when it shows, "suggests" when it suggests; never
> inflate with "very", "clearly", "obviously", or verdict words like "interesting" or
> "important" that do the reader's judging for them. Lead each paragraph with its point;
> one idea per paragraph; open sentences with what the reader already knows and close
> them with what is new. Cut surplusage: "it is worth noting that X" is "X"; prefer the
> plain word (use, not utilize; before, not prior to). Active voice unless the actor
> genuinely does not matter. Spell out an abbreviation at first use. Keep the student's
> own voice and vocabulary level — polish, do not transplant.

`build_write_prompt(instruction, heading, selection, nearby, context_block, facts_block)`
— system: the assistant drafts one passage to insert at the cursor of a student's
document; `_WRITING_CRAFT`; ground in the provided course material where relevant and
otherwise write from the instruction alone; **return only the markdown passage — no
preamble, no explanation, no code fence** (this contract is what makes the widget's
parse-on-accept safe). User message: the instruction, then a context section with the
current heading / selected text / surrounding text (whichever exist), then the retrieval
context block and any confirmed profile facts.

`build_suggest_prompt(draft, instruction, context_block, facts_block)` — system: the
assistant revises a student's draft per their instruction; `_WRITING_CRAFT`; **return the
complete revised document as markdown — the entire document, not a fragment, no preamble,
no fence**; leave untouched everything the instruction does not reach (the diff the
student reviews is computed from what you return, so an unnecessary rewrite of an
untouched paragraph shows up as noise they must reject); preserve the document's heading
structure unless asked to change it. User: the full draft, the instruction, retrieval
context, facts.

### 3.6 What "done" looks like

A student creates a draft in a class, types an outline, hits "Draft with AI" mid-document
and accepts a streamed paragraph that lands in the editor, asks for "make the second
section argue the converse" via Suggest, reviews the change hunk by hunk — accepting two
and rejecting one — sees the accepted change appear in revision history, edits a sentence
by hand (which makes a later stale suggestion behave per §3.2), discusses a paragraph in
a chat thread pinned to the draft, and prints the result. Both suites pass; every screen
meets the §0.6 bar.

---

## 4. Explicitly not adopted (and why)

From **kuhn** — the reference is its design, not its runtime:

- The Claude Agent SDK runtime (`agents/runtime.js`), sessions, token budgets,
  `dispatch_agent`, detachable runs. Lyra's in-house tool loop against a local endpoint
  is the deliberate architecture; kuhn's runtime is SDK-shaped below its own abstraction
  line.
- The six-agent role team. Lyra has one tutor model; writer/reviewer become prompt modes,
  not processes.
- Yjs collaboration, room seeding/eviction, multi-tenancy, orgs, auth, magic links,
  external review surfaces — multi-user is explicitly off Lyra's roadmap.
- Typst/Pandoc Docker rendering. Print-to-PDF is Lyra's export; revisit only if real
  demand for .docx export appears.
- PubMed/arXiv citation search and citation chips. Wrong domain for coursework; Lyra's
  provenance-to-course-documents is the native equivalent. If formal citations return,
  they return as their own designed feature.
- Inline hunk decorations in the editor (the *rendering*, not the algorithm) — follow-up
  polish, listed in §5.

From **NitroAI** — ideas only, never code (AGPL):

- All literal code, all prompt text. Re-specified fresh in §2.
- Electron/Tauri shells, IndexedDB storage, the engine/provider abstraction — Lyra has
  its own answers to all three.
- Ollama auto-provisioning — Lyra's llama-server lifecycle already exists; bundled
  inference is Phase 6 as planned.
- The podcast generator — cloud-TTS-only, and squarely the bloat this integration is
  scoped to avoid.
- YouTube ingestion — video lecture processing is explicitly excluded on Lyra's roadmap;
  adopting it would be a scope decision for the owner, not an integration detail.
- Whole-source context stuffing for chat/generation — Lyra grounds through `retrieve()`,
  which is strictly better and already built.

## 5. Follow-ups this document creates (not part of the three workstreams)

- Inline suggestion-hunk decorations inside the Milkdown editor (kuhn
  `suggestion-hunks.ts` as the reference) replacing the side panel.
- Study-guide generation (Phase 5) as a `draft` artifact seeded by a generation pass —
  the draft workspace is its natural home.
- Practice problems (Phase 5) distinguished from quiz mode: open-ended, attempted in the
  workspace, feedback through Guide-mode chat.
- A `SOLUTIONS_RRF_BONUS` follow-up sweep if the first measurement is ambiguous.

## 6. Suite health

Before starting: 794 backend / ~360 frontend tests pass. Each workstream lands with its
suites green and its new tests in place. Nothing in this document may be closed with a
failing or skipped test, and no test may call a live model.
