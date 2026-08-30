# UI Overhaul: Audit and Design Brief

> **2026-08-07 addendum.** The visual language question this brief deliberately left open is
> now settled: the Ex Libris design system, workshopped to an approved interactive prototype.
> [exlibris-design-system.md](exlibris-design-system.md) is the approved brief (a historical
> reference); [design-system.md](design-system.md) documents the shipped system and governs
> new work; and [exlibris-migration.md](exlibris-migration.md) records the completed reskin,
> sequenced against this brief's section 7 table so both landed as one rewrite per
> component. The UX findings below stand unchanged. Note that the visual language this
> brief describes - the parchment palette and its display face - is that pre-Ex-Libris
> system, recorded here as the brief's baseline, not a description of the current app.

Written 2026-08-07 from a walk through the running app with real course data (ECE 203, 36
documents, two drafts carrying a finished review and a pending revision), a full inventory of
`frontend/src`, and the specs in `ui-phase-1.md` through `ui-phase-3.md`. Two other work streams
were active while this was written — Phase 4 (agent tools) and the writer roadmap (W1–W6) — so
this brief is split by ownership on purpose: §6 designs the draft workspace as **input to** W1–W5
rather than a retrofit of it, and §7 sequences everything so no item collides with either stream.

**This is not a reskin brief.** The visual language — parchment palette, Fraunces display, the
paper grain, honest machinery — is distinctive and worth keeping. Dark mode holds up everywhere
it was checked, print is deliberate, contrast is verified by script. The dissatisfaction this
brief answers lives one level up: duplicated affordances, screens that repeat instead of
synthesize, a component layer that has started to drift from its own conventions, and one
workspace (drafts) whose interaction model has been outgrown by what the writer is becoming.

---

## 1. What the walk found, in one paragraph

The shell is quiet and confident, and every screen has designed states. But the class hub shows
three ways to start a chat on one viewport; the home page shows two ways to create a class plus a
shortcut; the hub's Overview tab restates the other tabs' lists without adding anything they
don't already say; a 36-document class gets no search, no sort, no grouping anywhere documents
are listed or picked; document status renders as an unlabeled ✓ on one screen and the word
"Indexed" on another; the chat header says "Documents 36" while the empty state under it says
"34 documents indexed" with no reconciliation; and the draft workspace crams six panels —
Suggestion, Plan, Sources, Comments, History, Chat — into a ~250px rail whose tab strip
overflows and clips. At 375px the draft editor renders as one ~45,000px column stacked behind
those panels with no way to switch between document and tools, and the bottom shelf reaches only
Classes and Settings — Solutions, Study, and Drafts are unreachable from it.

## 2. System-level findings

These are the faults that repeat across screens. Fixing them once, at the primitive layer, fixes
most screens for free.

### 2.1 One action, one home

Every creation verb currently has two or three affordances visible at once:

- Class hub: **New chat** appears as a header button, as a link in the Conversations section
  header, and in the sidebar's expanded class. Same for New solution set (sidebar + section
  header) on the same screen.
- Home: a **New class** header button, a dashed New-class row at the end of the list, and
  Cmd+N.

Rule to adopt: a verb gets **one primary placement per screen** (the section that owns it), plus
optionally the keyboard shortcut. The header button belongs only on screens where the section
isn't visible. The sidebar stops carrying actions entirely (§2.2).

### 2.2 The sidebar is navigation, not a second hub

The expanded class in the rail carries "+ New chat" and a SOLUTIONS group with "+ New solution
set" — a miniature of the hub, built before the hub existed (the hub itself was added because
"clicking a class used to open a conversation"). Now that the hub owns class management, the rail
duplicating its actions is legacy. Proposed: the rail lists classes and, when expanded, recent
*destinations* (recent conversations, recent drafts) — never verbs. This also frees the rail to
answer "where was I?", which nothing currently answers (§3.1).

### 2.3 A shared artifact-list primitive

`class-chats-panel`, `class-solutions-panel`, `class-drafts-panel`, and `class-study-panel` each
independently implement skeleton rows → destructive Alert → Empty → row list → DropdownMenu →
RenameDialog → delete AlertDialog. Skeleton heights have already drifted (h-14 vs h-16). The
error Alert + backend message + Retry block is repeated in 23 files. Extract:

- `ArtifactListPanel` — the four-state list shell with one skeleton spec
- `ErrorState` — the Alert + message + Retry block
- Adopt the existing `Empty` primitive everywhere (see §2.4)

This is mechanical, high-value, and touches only hub panels — safe from both active streams.

### 2.4 The empty-state contract is broken exactly where it will matter most

ui-phase-1.md mandates that empty is "a designed screen with a next action." Twelve files honor
it through the `Empty` primitive. The drafts surface systematically does not:
`plan-panel.tsx:76,176`, `source-ledger.tsx:37`, `brief-card.tsx:38`, `comment-list.tsx:51`,
`revision-history.tsx:84` render bare tertiary-text paragraphs, and `class-hub.tsx:374,399,414`
pass bare `empty="…"` strings. The Plan and Sources panels are precisely the surfaces W2/W3 will
make load-bearing; their empty states are the first thing every student sees before their first
deep pass. Bring the drafts surface under the `Empty` contract as part of the rail redesign
(§6.3), not before it.

### 2.5 Status must be words, consistently

- Documents tab rows: status is an unlabeled ✓ icon. Hub overview rows: the word "Indexed."
  Same fact, two renderings, one of them illegible. ui-phase-2.md's own rule ("never
  color-only") extends naturally to "never icon-only."
- Chat empty state: "34 documents indexed" under a header reading "Documents 36." Both are
  true (two documents are presumably not ready) but nothing explains the gap. Either count the
  same thing or say "34 of 36 ready" and link the two that aren't.
- "Structure Lyra found" (document outline disclosure) reads as broken English. "Outline" with
  a chevron says the same thing.
- The suggestion panel titles pending revisions "revise 1.4" — an internal stage label leaked
  into the interface. Say what it is: "Suggested revision · 4 changes" or the instruction that
  produced it.

### 2.6 Density: the 36-document class is the real class

Phase 1 screens were designed against syllabus-sized classes; the measured reality is 36+ files.
Nowhere that lists documents offers search, sort, or type grouping:

- Documents tab: heavy cards (~90px each), ~4 visible per viewport, dropzone permanently
  pinned below. Finding one file in 36 is a scroll hunt.
- New solution set: a flat checkbox list of all 36 documents, problem sets mixed with
  solutions, labs, and textbook chapters — even though `detect_doc_type` already classifies
  them and the chunker knows which carry problem markers.

Target: a compact row (one line: name, type chip, status word, size, kebab) with a filter box
and doc-type grouping, reused by both the Documents tab and every document picker
(`SourcePicker` included). The picker additionally pre-sorts likely problem sets first, using
metadata the backend already has.

### 2.7 Spec drift to resolve, one way or the other

- **`EndpointLocalityBadge` does not exist.** ui-phase-1.md specs it "always visible in the
  header"; the only locality readout lives inside Settings. This is the privacy pillar's one
  ambient affordance, it becomes *more* important when W3 adds web research and Phase 4 adds
  tools, and it is missing. Rebuild it as a quiet header chip (Local / Remote · hostname) that
  links to Settings. Small, shell-only, safe now.
- Solution export is a bare `window.print()` button; the spec says dropdown (PDF via print,
  and later formats land with `exporting.py`). Fine to leave until the export surface grows —
  but note it in ui-phase-2.md instead of letting the spec claim otherwise.
- ui-phase-2.md's component inventory lists `context-menu` and `StepGuidePanel`; neither
  exists (`StepThread` replaced the latter). Update the inventory.
- `source-pane.tsx:365` uses `bg-white` for the rendered PDF sheet — the single hardcoded
  color in the codebase. If it is intentional (paper is paper in both themes), name it as a
  token (`--paper-sheet`) so the definition of done stays true.

### 2.8 Oversized components

`drafts/[artifactId]/page.tsx` at 1,338 lines and `chat-pane.tsx` at 1,009 are the two clearest
splits. The drafts page split should ride along with the rail redesign in §6 (writer-stream
territory; don't touch it independently). `chat-pane.tsx` carries three layouts (full, inline,
mobile tabs) and can be split any time — but coordinate with the writer stream, which embeds its
inline layout.

## 3. Screen-by-screen: findings and target designs

Ordered by leverage. Items marked **[safe now]** touch neither active stream.

### 3.1 Home **[safe now]**

**Today:** two identical-weight rows (name, doc count, relative time), a redundant second
New-class affordance, and most of the viewport empty.

**Target:** the home page answers "what was I doing, and what's near?" A class card carries: last
activity as a *sentence* ("You asked about Fourier series · yesterday"), anything in flight
(ingestion, a running solve or draft pass — the polling hooks already exist), and the next
deadline from confirmed profile facts ("Midterm · Mar 12") when one exists. Keep the dashed
New-class row, drop the header button. The card already links to the hub; make its in-flight
line deep-link to the running thing.

### 3.2 Class hub

**Today:** the Overview tab is a table of contents for the other tabs — Conversations (list),
Solution sets (list), Documents (list) — each section repeating what its tab shows, with empty
sections stacked above the one list that has content. Seven tabs across the top; counts on only
some of them; three New-chat affordances (§2.1).

**Target:** Overview earns its place by *synthesizing* instead of repeating: a resume-work strip
(most recent conversation, draft, or solution — one card each, only if they exist), in-flight
jobs with stage words, upcoming deadlines from the profile, and a compact "what Lyra knows"
line (documents ready / total, facts confirmed / proposed) linking into the tabs. Sections with
nothing to say don't render; a genuinely empty class gets the existing designed Empty with its
three verbs. Tab counts become consistent (all or none — recommend all, they're cheap and
honest). Every list row shows its status as words (§2.5).

**[safe now]** except any text the writer stream is actively changing in the Drafts panel — the
panel list itself is fair game, the Write/Review dialogs are not (they gain the depth dial in
W1).

### 3.3 Chat **[mostly safe now]**

- The "Try asking" prompts are generic boilerplate ("What are the main topics in this
  class?"). The class profile holds extracted facts, deadlines, and topics; generate the
  suggestions from them ("Ask about Lab 3, due Friday") and fall back to the generic set only
  for a class with no profile. This is the cheapest visible intelligence in the whole brief.
- The count contradiction (§2.5).
- The Guide/Show segmented control and the separate Solve button are the product's core ladder
  rendered as two unrelated widgets. Make the ladder one control — three positions, one
  metaphor — with Solve routing exactly as today. (The ladder is pedagogy *and* marketing;
  it deserves to be legible.)
- Coordinate anything touching `chat-pane.tsx` internals with the writer stream (§2.8).

### 3.4 Documents (tab + pickers) **[safe now]**

The compact-row + filter + grouping redesign of §2.6, applied to `documents-pane.tsx`,
`document-row.tsx`, and `SourcePicker`. Keep the per-page recognition affordances exactly as
specced in ui-phase-3.md — they are right — but rename the outline disclosure (§2.5). The
dropzone stops being pinned viewport furniture and becomes the list's final row (it is an
occasional action, not a permanent one; drag-anywhere still works).

### 3.5 Settings **[safe now, with one landmine]**

The page is honest and well-written, and it is about to grow: W3 adds the web-research toggle
(specced to sit beside the remote-endpoint acknowledgement), Phase 4 adds tool/security posture,
Phase 6 adds bundled-model management. One long column won't carry that. Restructure into
sectioned navigation now (Model · Privacy · Appearance today; Research, Tools, Models later) —
**but land it before or coordinated with W3's toggle**, or the writer stream inherits a merge
conflict in `settings-form.tsx` (667 lines, also due for the §2.3 split treatment).

### 3.6 Mobile

- The bottom shelf gets Classes · Study · Settings at minimum; better, it becomes contextual
  inside a class (Overview · Chat · Docs · More). Two destinations for a five-surface app is
  the roadmap's own "shipped capabilities with no way to reach them" fault, recurring at 375px.
- The hub's scrollable tab strip clips labels mid-word ("Draf"); give it fade edges and
  snap-to-tab so truncation reads as scrollable rather than broken.
- The draft workspace at 375px is not usable (§1). The honest v1 is a **reading + reviewing**
  surface: document first, a bottom sheet for comments/suggestions, editing deferred to
  desktop. Ship that deliberately rather than shipping the accident. This belongs to the §6
  redesign, not to a quick fix.

## 4. What already works — keep and defend

Named so the overhaul doesn't erode them: the token bridge and zero-hardcoded-colors rule (one
exception, §2.7); four-states-always where the `Empty` contract is honored; honest machinery
(no timer-driven progress anywhere — `SolveProgress` and the per-page recognition counter are
the reference implementations); the keyboard map and focus discipline; reduced-motion and print
correctness; typography (assistant serif at reading measure; Fraunces display; the eyebrow).
Any new component in this brief inherits all of it.

## 5. Two small bugs found while walking **[safe now]**

- Home-page class rows: clicks that miss the inner link do nothing — the hit target is the
  text, not the row. Make the whole row the link (the list already renders a hover state that
  promises as much).
- Draft rail tab strip overflows without any scroll affordance at desktop widths (six tabs in
  ~250px; "Suggestion" clips to "ggestion" when Comments/History exist). Subsumed by §6.3, but
  worth knowing it's broken today.

---

## 6. The draft workspace: design input for W1–W5

The writer roadmap settles process (depth dial, plan artifact, source ledger, convergence loop,
narration). What it does not settle is **where these live on screen**. This section proposes
that, so W2–W5 land into a layout designed for them instead of adding a seventh tab to a rail
built for one. Everything here is a proposal to the writer stream, not parallel work: files in
`components/drafts/` and the drafts route belong to that stream until W-phases land.

### 6.1 The diagnosis

The rail is a stack of six coequal tabs, added one feature at a time. But the six are not peers —
they are three different kinds of thing:

1. **The conversation** — chat, the steering wheel (roadmap principle 8)
2. **The plan and its sources** — persistent artifacts the student reads and edits (W2, W3)
3. **The work** — pending suggestions, review findings, history: things demanding a decision

A tab strip flattens that hierarchy, hides five of the six at all times, and already overflows.
Margin comments (W1's "single highest-leverage UX fix") cannot live in a tab at all — their
entire point is adjacency to the anchored line.

### 6.2 The shape proposed

Three regions, replacing the six-tab rail:

- **The document** stays the widest column. It gains a **gutter**: margin cards for anchored
  comments (W1) and, during a pass, per-section status ticks (§6.4). Suggestion hunks stay
  inline where they already render; the rail's Suggestion panel reduces to a summary header
  (counts, Accept all / Reject all, and the instruction that produced the revision in plain
  words — not "revise 1.4").
- **One right panel with three modes — Plan · Sources · Activity** — segmented, not
  overflow-scrolled. *Plan* is W2's thesis / argument map / section jobs, editable, with
  "re-run from here" affordances (W5.3's closed loop generalized). *Sources* is W3's ledger
  (compact rows: favicon-or-doc-icon, title, excerpt count; access date demoted to the detail
  view — today it repeats "Accessed 8/7/2026" on every card). *Activity* absorbs History and
  review-run summaries: one chronological record of passes, reviews, and snapshots.
- **Chat as a drawer** across the bottom of the document column, collapsed to a composer line.
  The steering wheel is always reachable, never consuming rail width, and big asks routing to
  the pipeline (W1.1) means it never needs to display long prose — which is exactly what makes
  a drawer sufficient.

Comments get no panel: anchored ones live in the gutter, and the rare unanchored remainder
(post-W1 fuzzy anchoring, only "hopeless mismatches") lands in Activity with a jump-to-nearest.

### 6.3 What this asks of each W-phase

- **W1 (margin anchoring):** the gutter is the deliverable, not a Comments-tab improvement.
  Card = severity word + one-line finding + "Fix this" (queues the W5.3 targeted pass).
  Severity as words with color, never color alone. The review banner ("Review complete:
  1 critical, 8 major…") becomes clickable chips that filter the gutter.
- **W1 (depth dial):** the Write/Review dialogs present quick / standard / deep with honest
  time words ("a few minutes" / "maybe an hour"), the deep description quoting the roadmap's
  own promise: time spent is the feature. The pause-at-plan checkbox sits beside the dial.
- **W2 (plan):** the Plan mode renders thesis and argument map as structure, not JSON — the
  map as an ordered claim list with relations, each section job showing claim + evidence +
  word budget, every piece editable in place. Its `Empty` state (§2.4) sells the feature:
  what a plan will contain, one "Start a pass" verb.
- **W3 (sources):** ledger rows distinguish course material from web by icon, never by
  separate sections (the roadmap makes them one mechanism; the UI should agree). Citation
  markers in prose highlight their ledger row on hover, both directions.
- **W4 (narration):** §6.4.
- **W5 (review):** findings arrive as gutter cards with the same anatomy as W1's; a deep
  review's per-section rubric verdicts render on the plan's section list, making Plan mode the
  review's summary view for free.

### 6.4 Narrating an hour honestly

W4 promises "an hour-long deep pass reads as a writer at work, not a hang." Concretely:

- A **status strip** under the toolbar during any pass: current stage verb ("Attacking the
  argument in §2 · round 3"), elapsed (`14:32`), depth badge, Stop. Verbs come from
  `stage_detail`; nothing advances on a timer (the `SolveProgress` honesty rules apply
  verbatim).
- The plan's section list doubles as the **live board**: each section shows queued → research →
  draft → skeptic round n → converged, with the ticks mirrored in the document gutter as
  sections fill in. A student watching a deep pass sees the cast working through *their*
  outline — which is the roadmap's vision statement rendered literally.
- Stage transitions are announced via the existing `aria-live` discipline (change-only, no
  spam), and elapsed time follows the Phase 3 rule: counter appears past 10s, hours phrased as
  "1 hour 12 min."

### 6.5 Suggestion legibility (small, but daily)

The suggestion panel renders replacement text as raw monospace, unrendered math included
(`h_eq(t) = h1(t) * h2(t)` as plain text in a draft whose editor renders KaTeX). Suggested
content should render as it will appear — markdown and math — with the diff signaled by the
existing added/removed treatments, mono reserved for actual code. This is worth doing whenever
suggestion components are next open.

---

## 7. Sequencing and ownership

| # | Item | §  | Owner / timing |
|---|------|----|----------------|
| 1 | Shared primitives: `ArtifactListPanel`, `ErrorState`, `Empty` adoption outside drafts | 2.3, 2.4 | Safe now |
| 2 | Documents density: compact rows, filter, grouping, picker reuse | 2.6, 3.4 | Safe now |
| 3 | Status words everywhere; count reconciliation; outline rename | 2.5 | Safe now |
| 4 | One-action-one-home + sidebar becomes navigation | 2.1, 2.2 | Safe now |
| 5 | Home page resume-work cards | 3.1 | Safe now |
| 6 | Hub Overview synthesis | 3.2 | Safe now |
| 7 | `EndpointLocalityBadge` header chip | 2.7 | Safe now; before W3/Phase 4 make it matter more |
| 8 | Settings sectioning + form split | 3.5 | Coordinate with writer stream (W3 toggle) |
| 9 | Chat suggested-prompts from profile; ladder control | 3.3 | Safe except `chat-pane` internals |
| 10 | Mobile bottom-nav reach + tab-strip affordance | 3.6 | Safe now |
| 11 | Draft workspace: gutter, three-mode panel, chat drawer, status strip | 6 | Writer stream, with W1–W5 |
| 12 | Draft mobile read/review surface | 3.6, 6 | Writer stream, after 11 |
| 13 | Spec-drift cleanups in ui-phase-2.md | 2.7 | Whenever |

Items 1–10 are implementable today in a worktree without touching `components/drafts/`,
`chat-pane.tsx` internals, or any backend file either stream owns. Item 11 is the writer
stream's to adopt, amend, or reject — its value is that the workspace's end state is now
designed *before* W2 lands a Plan tab into a rail that was already full.
