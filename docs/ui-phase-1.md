# Phase 1 Interface Specification

This document specifies every screen Lyra ships in Phase 1, down to states, motion, and copy. It is
the companion to [design-system.md](design-system.md), which owns tokens and generic component
patterns. Where the two disagree, design-system.md wins on tokens and this document wins on layout
and behavior.

> **Historical note (2026-08-29).** This specification predates the Ex Libris migration.
> The visual system it assumes - faces, palette, tokens - was replaced;
> [design-system.md](design-system.md) documents the shipped Ex Libris system and still
> governs tokens, so where this document names values from the older system they are
> historical, not normative.

Interface quality is a core pillar, and OCR was cut from Phase 1 to fund it. That trade only pays off
if this specification is implemented completely rather than approximated.

## Principles For This Phase

1. **No unstyled intermediate.** A screen is never shipped with default borders, arbitrary hex
   values, or a bare spinner standing in for a real state. Skeletons come first, not last.
2. **Four states, always.** Every surface that loads data defines loading, empty, error, and
   populated. The empty state is a designed screen with a next action, not centered gray text.
3. **The machine is honest.** Long operations show what stage they are in. Failures say what failed
   and what to do. Nothing spins forever, and nothing claims success it did not achieve.
4. **Keyboard first, pointer second.** Every action has a keyboard path. Focus is always visible and
   never lost.
5. **Motion explains, never decorates.** If an animation does not communicate a relationship or a
   state change, it does not ship.

## Component Inventory

All base primitives come from shadcn/ui so behavior and accessibility are not reimplemented. These
registry items are used in Phase 1:

| Purpose | shadcn item |
|---------|-------------|
| Actions | `button`, `button-group` |
| Surfaces | `card`, `separator`, `scroll-area`, `resizable` |
| Navigation | `sidebar`, `tabs` |
| Overlays | `dialog`, `alert-dialog`, `sheet`, `popover`, `tooltip`, `dropdown-menu` |
| Forms | `form`, `field`, `input`, `textarea`, `label`, `select`, `switch`, `radio-group` |
| Feedback | `skeleton`, `spinner`, `progress`, `sonner`, `alert`, `badge`, `empty` |
| Content | `avatar`, `kbd`, `item`, `collapsible` |

`sonner` is the toast layer; the deprecated `toast` item is not used. `spinner` appears only inline
inside buttons and never as a page or list loading state.

Lyra-specific components not in the registry: `IngestionProgress`, `DocumentDropzone`,
`MessageBubble`, `StreamingMarkdown`, `ThinkingIndicator`, `ReasoningTrace`, `LyraMark`,
`EndpointLocalityBadge`, `FactRow`, `RetrievalNotice`.

`LyraMark` is the assistant's identity: Vega, the lyre's bright star, held at the center of an orbit
carrying two smaller companions. Lyra is a constellation, so the mark is drawn as one rather than as
the generic sparkle every assistant ships. The orbit is one ring broken at a single point, not a pair
of arcs, because a broken ring still reads as a ring while two arcs read as two parentheses.

## Application Shell

```
┌────────────────────────────────────────────────────────────┐
│ ┌──────────────┐ ┌────────────────────────────────────────┐│
│ │              │ │  Header: breadcrumb · endpoint badge   ││
│ │   Sidebar    │ ├────────────────────────────────────────┤│
│ │   260px      │ │                                        ││
│              │ │  Route content, max 1320px, centered ││
│ │  Lyra        │ │                                        ││
│ │  ─────────   │ │                                        ││
│ │  Classes     │ │                                        ││
│ │   · CALC 201 │ │                                        ││
│ │   · PHYS 110 │ │                                        ││
│ │  ─────────   │ │                                        ││
│ │  Settings    │ │                                        ││
│ └──────────────┘ └────────────────────────────────────────┘│
└────────────────────────────────────────────────────────────┘
```

- Desktop navigation is a 260px inset raised-paper rail, collapsible to a 60px icon rail with state persisted in `localStorage`
- Class rows carry the course mark, the class name, and the course code beneath it. The name is the primary line: a rail of bare codes (`ECE 203`, `ECE 380`) does not say which class is which
- Active class rows use `--accent-primary` text with a 2px sage marker inset within the row's rounded surface, not a `border-l` on its box, which lands outside the corner radius and reads as detached
- A class row links to the class hub, `/classes/{id}`, not into a conversation. Clicking a class used to open a chat, which made the class the chat and left its solution sets, files, and profile reachable only through this rail, where they could be opened but never renamed, moved, or deleted
- A class row expands into its conversations only while that class is open: each chat links to `/classes/{id}/chat?session={n}`, and a `New chat` action sits at the bottom of the list. Every other class stays a single line
- Only the five most recent conversations are listed, with a `Show all {n}` row under them. A term's worth of chats is a hundred rows, and a rail that long buries Solutions and everything below it. The conversation being read is always on the list wherever it sits in the history, or the rail would have no highlighted row and no way back to it
- `New chat` navigates to `?session=new` rather than creating anything. **A conversation starts existing when the first message is sent**: an empty chat is a click, not history, and creating one up front is what filled the rail with untitled conversations nobody had said anything in. Conversations already stored empty are swept once at startup
- Conversations are named by the backend from their first message, and an untitled one falls back to its creation date. Never by list position, which renumbers whenever a conversation is added or removed, so the same chat keeps changing name
- An `Archived` group below the classes is collapsed by default and holds archived classes, each with a hover restore action
- A skip-to-content link is the first focusable element and is visually hidden until focused
- The header carries the breadcrumb and endpoint locality badge on a translucent parchment rule; on class pages the breadcrumb reads `Classes / ECE 203 Continuous-Time Signals` and the header also carries the Profile button, so the workspace starts at the panes. Settings is a root crumb, not a child of Classes. Below 640px ancestor crumbs fold away, because three truncated crumbs name nothing
- Below 640px, navigation becomes a floating 64px raised-paper bottom shelf; main content keeps clear space beneath it
- Route content is capped at 1320px and centered, with `p-6` desktop and `p-4` on smaller screens
- The shell owns the viewport: `main` is the one scroll container below the header, so the rail and header never travel on a long route, while a full-height route sizes itself to exactly what is left

### Endpoint Locality Badge

Always visible in the header. It is the standing answer to "where is my data going."

| Condition | Presentation | Copy |
|-----------|--------------|------|
| Loopback endpoint | `--success-text` on `--bg-tertiary`, small dot | `Local` |
| Non-local, acknowledged | `--info-text`, dot | `Remote` |
| Non-local, not acknowledged | `--danger-text`, dot | `Remote, unconfirmed` |
| Unreachable or untested | `--text-tertiary`, hollow dot | `Not connected` |

Clicking it navigates to Settings. It is a `button`, not a decorative span, and its tooltip states
the host. Color is never the only signal: the label text always differs.

## Screen: Home, Class List

Route `/`. The class list is the product's front door and is worth real attention.

**Populated.** A responsive grid of class cards, one column below 640px, two to 1024px, three above.
Cards are sorted by most recently active.

Card anatomy:
- Rectangular class initials use a deterministic course-mark palette: sage `--accent-surface`, tan
  `--accent-secondary`, or muted clay `--accent-tertiary`, always with the paired foreground token
- Class name, `h2`
- Course code and semester, `body-sm` in `--text-secondary`
- Footer row: document count and relative last-activity time, `caption` in `--text-tertiary`
- Whole card is one link; a `dropdown-menu` on the top right offers Rename, Archive, and Delete
- Hover raises `shadow-sm` to `shadow-md` without a color shift or scale transform
- The name wraps to two lines rather than truncating: it is how the user tells one card from another
- Footers sit on the card's floor, so cards in a row square up however long their titles run
- A dashed `New class` tile closes the grid. A row of cards trailing off into empty canvas reads as an unfinished page, and the tile keeps the screen's one action within reach of the eye

Archived classes drop out of the grid (a small caption notes the count and points at the sidebar's
Archived section); they are restored from there without touching their data.

**Loading.** Six skeleton cards matching the real card's exact dimensions and internal rhythm, so
  nothing shifts when data arrives. Never a spinner.

**Empty.** The `empty` primitive: a `GraduationCap` icon at 32px in `--text-tertiary`, heading
`No classes yet`, body `Create a class to start uploading your course materials.`, and a primary
`New class` button. This is the first screen a new user sees, so it is designed, not a placeholder.

**Error.** `alert` in the danger tokens, heading `Could not load your classes`, the backend message,
and a `Retry` button. If the backend is unreachable, the copy says so plainly and mentions that Lyra
runs a local server, rather than showing a raw network error.

**Create class.** A `dialog` with three `field`s: name (required), course code (optional), semester
(optional, `select` of recent terms plus free text). Submit is disabled until name is non-empty.
Validation is inline via `form` and Zod, never a toast. On success the dialog closes, the new card
animates in, and focus moves to it.

**Delete class.** `alert-dialog`, not a plain dialog, because it is destructive and irreversible. It
names the class and states exactly what is removed: documents, chunks, conversations, and profile.
Confirmation requires typing the course code, or the class name when no code exists. The destructive
button uses `--danger-fill` with `--danger-foreground`.

## Screen: Class Hub

Route `/classes/[id]`, and where a class opens. A page, not a workspace: a centred column with the
course mark, the class name, its code and term, and one line of actions. Under that a tab bar -
Overview, Chats, Solutions, Documents, Profile - each tab carrying its count, so what the class
holds is readable without opening anything. The open tab is a `?tab=` parameter rather than state,
so a class opened on its files is a link, and Back leaves a tab rather than the class.

Overview is a digest, not a fifth list: the three most recent conversations, the three most recent
solution sets, the first few documents, and a line of what the profile knows, each section headed
by the one action that starts something new and closed by `View all`.

Documents is the one tab that takes the height it is given rather than asking for one. The file list
scrolls inside its box and the upload well sits on the box's floor, so the well is on screen at
every window size and the page itself never scrolls. A fixed height is a guess that is wrong in both
directions: too tall and a short window has to be scrolled to reach the well, too short and a tall
window leaves a band of dead page beneath it.

This is the only screen where a class can be managed rather than merely used. Rename or archive or
delete the class; rename or delete a conversation; rename or delete a solution set; and select
files to move them to another class, reindex them, or delete them. Bulk delete is confirmed and a
single row's is not: one file is a mistake you can see coming, several at once is the click that
empties a term of uploads.

Moving a file states its consequence rather than hiding it. The file arrives in its new class
unindexed and is read again from scratch, because retrieval is partitioned by class; the toast
says so. A file a solution set is built from cannot be moved, and the refusal names the set.

## Screen: Class Workspace

Route `/classes/[id]/chat`. The header carries the class code, name, and the Profile button, so the
workspace starts at the panes. The conversation is the primary surface and fills the window. The
class name in the breadcrumb links back up to the hub.

**A workspace is not a page.** Pages are a padded column inside the shell's rounded surface, which
is the right frame for a class list or a settings form. A workspace is the student's own material in
two panes, and it wants every pixel: framed as a page it sat inside three nested rounded surfaces
and the reading column came out at 41% of the width and 72% of the height of a 13-inch laptop. So a
workspace route asks the shell for the whole window (`useFullBleed`), and puts its title and actions
in the app header (`HeaderCrumb`, `HeaderActions`) rather than spending another row on a title bar.
Documents open as a 340px right column inside that same surface, into the gutter the 860px reading
measure was never going to use, so the conversation does not narrow when the list appears. The column is closed by default and its state is persisted per class; an upload
batch in flight opens it. The active conversation is part of the URL (`?session={n}`) so chats in
the sidebar are linkable and reloadable.

```
┌──────────────────────────────────────────────────────────┐
│ Header: Classes / ECE 203 Continuous-Time Signals · P…   │
├──────────────────────────────────────────────────────────┤
│ TUTOR              Guide | Show   [ Documents 17 ]       │
├──────────────────────────────────┬───────────────────────┤
│ ┌──────────────────────────────┐ │ DOCUMENTS 17      [→] │
│ │   conversation, max 860px    │ │  ▸ syllabus.pdf       │
│ │                              │ │  ▸ homework_1.pdf     │
│ │        [ Jump to latest ]    │ │  ▸ …                  │
│ └──────────────────────────────┘ │                       │
├──────────────────────────────────┤                       │
│ [ composer .............. send ] │ [ drop · Choose files]│
└──────────────────────────────────┴───────────────────────┘
```

The conversation opens at its latest message and follows the tail while the reader is already
there. Only the reader detaches that: a wheel, touch, or key scroll away from the bottom. Content
that grows under a scroll which already ran (KaTeX re-laying out, the documents column opening)
re-pins instead, because that is a layout change rather than the reader moving. When detached, a
`Jump to latest` control returns.

Below 1024px the panes become Chat and Documents line tabs with a 2px sage active rule, defaulting
to Chat. Below 640px navigation becomes the floating bottom shelf described in design-system.md.

### Document List

Each row is an `item`: file-type icon, filename truncated from the middle so the extension stays
visible, and a state indicator on the right.

| State | Indicator | Row treatment |
|-------|-----------|---------------|
| `pending` | `--text-tertiary` dot | dimmed |
| `parsing`, `chunking`, `embedding`, `extracting` | `progress` ring plus stage label | dimmed, not interactive |
| `ready` | `Check` in `--success-text` | normal |
| `unsupported` | `FileWarning` in `--info-text` | normal, with an explanatory affordance |
| `failed` | `AlertCircle` in `--danger-text` | normal, with Retry |

Selecting a ready document filters retrieval to it for the next turn and shows a removable `badge` in
the composer. Each row's `dropdown-menu` offers Retry where applicable and Delete.

### Ingestion Progress

The visible answer to "is it working." A four-step `IngestionProgress` for Phase 1, since OCR is
absent: **Reading, Splitting, Indexing, Analyzing**, mapped to `parsing`, `chunking`, `embedding`,
`extracting`. Verbs, not internal stage names.

- Completed steps show a check in `--success-text`; the active step shows the ring; later steps sit
  in `--text-tertiary`
- The document's size appears as `12 pages` once parsing knows it. Not `page 4 of 12`: `pages_done`
  is written once, at the very end of the run, so a per-page counter read `page 1 of 12` from the
  first second to the last, which looks like progress that has stalled. How long a document should
  take is worth saying; a number that never moves while claiming to is not
- `Analyzing` carries the subtitle `Looking for dates, topics, and course details`, because this
  stage can take minutes and silence reads as a hang. Not "your syllabus": the stage runs over every
  upload, so it told a student watching a lab handout that Lyra was reading their syllabus
- Polled through TanStack Query, backing off from 500ms to 2s, and stopping on a terminal state. The
  row falls back to the list's answer the moment its own poll is switched off, because a disabled
  query keeps its last result and would otherwise pin the row to a stage that had ended
- On completion the row transitions to `ready` and a `sonner` toast confirms it

### Unsupported Documents

The state that exists because OCR was cut, so it must be excellent rather than an afterthought.

A `popover` on the row explains:

> **Needs text recognition**
> This looks like a scanned document, so there is no text to read yet. Your file is saved. Lyra will
> be able to read scans in a future update, and this document will process automatically then.

No dead end, no apology, no raw error. Partially scanned documents ingest normally and show a
`caption` reading `3 pages skipped, no readable text`, with the same popover explaining why.

### Document Dropzone

Persistent target at the bottom of the document pane, and the whole pane accepts a drop.

| State | Presentation |
|-------|--------------|
| Idle | dashed 1px `--border-strong`, centred: upload glyph over `Drop files or a folder here, or click to browse`, with a quiet `choose a folder` link beneath |
| Drag over | sage-tinted fill with `--accent-primary` boundary and icon |
| Reading a folder | the walk named while it runs, before any upload starts |
| Rejected type | clay danger treatment with `--danger-text` error copy naming accepted types |
| Uploading | tokenized `progress` bar with filename and queued progress |

**The whole well is the control, and it is centred.** It was a caption on the left with two buttons
pinned to the right, which reads as a toolbar that happens to have a label rather than as somewhere
to put a file, and left the one part it is for - the target itself - as the part you could not
click. Clicking anywhere in the well opens the file picker.

The folder picker is a text link under the target rather than a second button beside the first:
picking a folder is the rarer half of the choice, and the only other way to reach it is to drag one
in. It says `files or a folder` because otherwise nothing does - recursive folder upload was built
and then effectively hidden behind copy that offered `PDF, TXT, or MD`, which reads as one file at a
time. A capability nobody can see is not shipped.

Dropped folders are scanned recursively through the file system entry API, and the picker offers
both a folder input (`webkitdirectory`) and the existing multi-file input. Every dropped entry is
claimed synchronously in the drop handler, before the walk awaits anything: a `DataTransferItemList`
is emptied the moment the handler yields, so reading it lazily meant a drop of three week folders
uploaded the first and silently discarded the rest. The walk can take seconds on a term of notes, so
the well names what it is doing while it runs rather than sitting idle and then filling up.

The whole pane is one drop target, well included, so a folder dropped on the rows and a folder
dropped on the well are read and reported the same way. The batch is uploaded one file at a time so
a large folder does not open dozens of request bodies at once; a `BatchLoader` above the well
reports the current stage verb and `processed of total` counts while rows poll toward a terminal
state. The document list is scrollable once rows exceed the pane.

### Conversation

The two voices are told apart by shape, not by decoration: the student's message is a right-aligned
muted-paper note at a pill radius; Lyra's response is unboxed prose on the page itself, led by the
mark. Wrapping every reply in a bordered card turns a conversation into a stack of receipts, and the
reply is the page's main content, so it gets the page.

- Lyra messages carry the `LyraMark` on a 28px `--accent-surface` disc
- Markdown renders incrementally during streaming, with `JetBrains Mono` code blocks, syntax
  highlighting themed per mode, table/pre/code overflow contained in its surface, and KaTeX math
- Equations use `$$...$$` on their own blank-line-separated display rows; `$...$` is reserved for
  short inline quantities, and wide display math scrolls horizontally rather than overlapping prose
- Newly arriving prose words fade in in source order with a 180ms opacity/2px-rise reveal; code and
  math remain intact and are never split into visual-only layers. A typeset equation is one unit of
  the cascade, revealed whole in its own place
- **Math that has not finished arriving is withheld rather than typeset.** Code and prose grow a
  character at a time and read fine doing it; an equation does not. Closing `$\frac{1}{2` on the
  reader's behalf draws a fraction with one arm, and a fragment long enough to look like display
  math is centred on its own line only to snap back inline when the closing delimiter lands. That is
  what made equations look like they populated ahead of the sentences holding them: they were being
  drawn out of the text flow before the text existed
- A unit's place in the cascade survives a re-render. Markdown is re-parsed on every frame, so the
  node holding a word can be replaced while its reveal is still pending, and re-applying the reveal
  without its delay would jump it ahead of every word queued in front of it. The cascade lives in
  `components/chat/reveal.ts` and is shared by every surface that shows written work
- There is no stream caret. Markdown blocks are block-level, so a trailing marker cannot sit
  at the end of the last word: it lands at the start of the line below, reading as stray
  punctuation. The word reveal is already the evidence that text is arriving
- An action row sits under each message, hidden until that message is hovered or something in the row
  takes focus: Copy on any message, Retry on the last Lyra message, and the timestamp
- While streaming, the send button becomes Stop
- Timestamps are `caption` in `--text-tertiary`, shown on hover, and pinned visible for the first
  message after a gap of more than an hour, so a thread resumed the next day says so
- Sending a message, or retrying one, always returns the view to the tail. It is the one case where
  following re-attaches without the reader scrolling there, and it is correct because they just acted

### Waiting, And Thinking

Between a question and its first word, Lyra shows one line: the `breathe` braille loader and a
shimmering label naming what is actually happening, plus an elapsed counter once the wait passes
three seconds. The mark animates alongside it, its orbit turning and its stars breathing.

This replaced a three-row stage checklist. On a machine that can run the model at all, prompt
assembly and retrieval finish in milliseconds, so the checklist spent a card narrating work nobody
waits for. The stages are still tracked, because on a large class they stop being instant, and a
reader who waits deserves to know which part is slow:

| Stage | Label |
|-------|-------|
| `prompt_processing` | `Reading your question` |
| `reviewing_documents` | `Looking through your material` |
| `composing_answer` | `Thinking` |

**Reasoning models.** Lyra assumes the user's model may think first. The thought is **always closed
until the reader opens it**, live or settled: it is the model's working, not the reply, and
unfolding it unasked pushes the answer down the page and makes the reader watch a draft they did not
ask for. What the wait actually needs is on the header, which is a trigger in both states: the
loader, `Thinking`, and an elapsed counter while the model works, then `Thought for 12 seconds` once
it stops. Opening it mid-turn shows the thought streaming live, scrolling itself under a faded top
edge with no scrollbar; that view returns to closed when the turn lands, because the answer is what
the reader came for by then. The duration is stored with the message, so a reopened conversation
still reports it. A settled thought renders as markdown at a smaller, quieter scale than the answer;
a streaming one stays plain text, because re-parsing thousands of characters per delta buys nothing
on text moving faster than anyone reads. A model that does not think never renders any of this.

The first row of a reply is one consistent 28px height whether it leads with a thought, a wait, or
the answer itself, and the mark is centered on it. A mark that shifts as a turn moves between those
states reads as the layout settling rather than as one speaker talking.

**Retry means answer again, not ask again.** The question stays where it is and its reply is
replaced. A student presses Retry because the answer was wrong, not because they forgot they asked.

**Empty conversation.** Not a blank pane. It shows the class name, a one-line statement of what Lyra
knows (`4 documents indexed, syllabus analyzed`), and three suggested prompts generated from the
class profile, such as `What is due next week?`. Each is a `button` that fills the composer without
sending, so the user stays in control.

**Composer.** Auto-growing `textarea`, three rows maximum before internal scroll. `Enter` sends,
`Shift+Enter` inserts a newline, and the hint is shown once using `kbd` for a new user, on pointer
breakpoints only: below 640px there is no physical Enter key to explain and the row costs real
reading height. Disabled with an explanation when no endpoint is configured, linking to Settings.

### Guide And Show Toggle

A two-option named control above the conversation, not a `switch`, because both options are named
and neither is a default-off state. It uses the line-tab treatment: the active option has a 2px sage
rule rather than a filled segment. Tooltips and per-session behavior remain unchanged.

- **Guide:** Socratic. Lyra asks leading questions and withholds the final answer.
- **Show:** direct. Lyra explains the full solution.

### Retrieval Notice

When retrieval was trimmed by more than half, a quiet `caption` row appears beneath the response:
`Some material did not fit in the model's context.` with a `tooltip` naming the omitted document
count. It is deliberately understated but never hidden, because the alternative is the user
mistaking a truncation artifact for the model being wrong.

## Screen: Class Profile

A `sheet` from the right, opened from the workspace header. In Phase 1 it is read-and-confirm, not a
full editor.

Facts are grouped into Deadlines, Topics, Grading, Professor, Prerequisites, and Other details. That
last group is not optional: without it, facts of kind `note` are stored and used in prompts while
being invisible to the one person who can correct them. Each `FactRow` shows the value, its source
document, and its confidence.

Extraction labels come from the model and are frequently filler, a topic labelled `topic` or a value
labelled `content`. A label that repeats its kind or is generic filler is dropped: it says nothing
the section heading has not, and printing it exposes the shape of the extraction prompt. A source
line is shown only when it differs from the row above, so five facts from one syllabus do not print
five identical citations.

- **Confirmed** facts: success surface with a `Check` in `--success-text`
- **Unconfirmed high-confidence** facts: no surface, and a neutral bullet in `--text-tertiary`. Not
  a check, which reads as verified when nobody has verified anything
- **Unconfirmed low-confidence** facts: information surface, `HelpCircle` in `--info-text`, and a
  `caption` reading `Not used until you confirm this`, with Confirm and Reject buttons
- **Rejected** facts: danger surface with a visible rejected state when retained for review

That caption is load-bearing. It tells the user the system is not silently acting on a guess, which
is the whole reason this screen exists in Phase 1.

Correcting a value is inline: click to edit, `Enter` commits, `Escape` cancels. Rejecting removes the
fact and does not re-propose it from the same document.

**Empty.** `No profile yet. Upload a syllabus and Lyra will pull out dates, topics, and grading.`

**Extraction skipped.** When extraction was skipped because the endpoint is remote and
unacknowledged, this screen explains exactly that and offers the acknowledgement inline, rather than
appearing mysteriously empty.

## Screen: Settings

Route `/settings`. A single scrolling column at 720px. Tutor model, Privacy, and Appearance each
live in a raised-paper `Card` with a display heading and supporting description; no section is a raw
key-value dump. Existing form, validation, mutation, and connection-result behavior is unchanged.

### Tutor Model

- **Endpoint URL** `input`, placeholder `http://127.0.0.1:8080/v1`. Helper text: `Lyra works best
  with a local model server. Remote endpoints send your documents over the network.`
- **API key** password `input`, shown as `Set` versus `Not set` and never echoed back from the
  backend. Helper text names where it is stored, the OS keychain, or warns plainly when a keychain
  is unavailable and it fell back to a file.
- **Test connection** button with four outcomes, each distinct:

| Outcome | Presentation |
|---------|--------------|
| Testing | inline `spinner`, button disabled |
| Success | `--success-text` check, `Connected. 7 models available.` |
| Reachable, no models | `--info-text`, explains the endpoint answered but advertises no models |
| Failed | `--danger-text` with the specific cause: refused, timed out, 401, or wrong path |

- **Model** `select`, populated by the test and disabled until one succeeds, with a `Refresh` action
- **Context window** numeric `input` with the warning from rag-pipeline.md when below 8192 tokens

### Privacy

Present in Phase 1 because the inference posture is a promise the UI has to keep.

- A statement of what runs locally: parsing, chunking, embeddings, and all storage
- A statement of what leaves the machine: only requests to the configured tutor endpoint
- Live locality readout for the current endpoint, matching the header badge
- When the endpoint is non-local: an `alert` in danger tokens with an explicit acknowledgement
  `switch` reading `I understand my document text will be sent to this endpoint.` Until it is on,
  profile extraction stays disabled and the reason is stated at the point of control, not buried.
- **Automatic profile extraction** `switch`, with a note that it costs a full-document pass on every
  upload

### Appearance

Theme `radio-group`: Light, System, Dark. Light is the fresh-user default and reads `Use the
parchment palette by default.`; explicit System and Dark choices remain immediate. Each labeled row
has a token swatch, visible selected state, and a 44px minimum target.

## Motion Inventory

Every animation in Phase 1, so nothing is improvised:

| Element | Motion | Duration, easing |
|---------|--------|------------------|
| Class card entry | `Reveal`: fade plus 8px rise, 50ms stagger capped at 200ms, once per card per session | 250ms, gentle |
| Card hover | `shadow-sm` to `shadow-md`, no scale | 200ms |
| Document list entry | staggered fade plus 8px rise, capped at five steps, with layout reordering | 250ms, gentle |
| Batch loader | two counter-rotating token rings; rotation stops under reduced motion | motion-safe, linear |
| Dialog, sheet, menu, popover, select, tooltip | fade plus at most 8px vertical movement | 200ms, never side-slide or zoom |
| Streaming word reveal | New prose words fade in source order with a 24ms stagger capped at 160ms | 180ms, gentle |
| Thinking loader | `breathe` braille cell, one character wide at every frame so the label never shifts; holds at full brightness under reduced motion | 100ms per frame |
| Thinking label | Light sweep clipped to the glyphs; removed outright under reduced motion, never frozen, because a paused clip leaves the text transparent | 2.6s, linear |
| Mark at work | Orbit turns; the primary star breathes and its companions twinkle off-phase | 7s linear, 2.4s gentle |
| Reasoning trace | Collapsible height transition; live thought scrolls itself under a faded top edge | 200ms |
| Dropzone drag over | border and surface-color change | 150ms |
| Skeleton and spinner | motion-safe only; static/no rotation under reduced motion | preference-controlled |
| Sidebar collapse | width transition | 200ms |

Under `prefers-reduced-motion`: transform and looping motion are removed, `Reveal` becomes a 150ms
opacity fade with zero delay, skeletons are static, spinners remain visible without rotating, the
thinking loader holds at full brightness, the shimmer is dropped rather than frozen, and word
reveals are immediately readable.

## Keyboard Map

| Keys | Action |
|------|--------|
| `Tab`, `Shift+Tab` | Move focus; first stop is skip-to-content |
| `Enter` | Send message, or activate focused control |
| `Shift+Enter` | Newline in the composer |
| `Escape` | Close overlay, cancel inline edit, or stop generation |
| `Cmd/Ctrl+K` | Focus the composer from anywhere, including from another field |
| `Cmd/Ctrl+N` | New class from home |
| `Cmd/Ctrl+B` | Toggle the sidebar |
| `Cmd/Ctrl+,` | Open Settings |
| Arrows | Move within the document list and radio groups |

Overlays trap focus and restore it to their trigger on close. No shortcut overrides browser
find, reload, or zoom.

## Copy Guidelines

- Plain, second person, present tense. `Upload a syllabus`, not `Syllabus upload is required`.
- Errors name the cause and the next step. Never `Something went wrong`.
- No blame: `Lyra could not reach your model server`, not `You configured this wrong`.
- Never expose internal stage names, file paths, stack traces, or endpoint URLs in error text.
- Sentence case for headings, labels, and buttons. Small uppercase editorial labels are reserved for
  pane and section context, never for body copy.
- No em dashes, no emoji, per conventions.md.
- Numbers are concrete: `4 documents indexed`, not `Several documents processed`.

## Definition Of Done For The Interface

A screen is complete when all of the following hold:

- [x] All four data states implemented, each visually designed
- [x] Skeletons match final layout dimensions, so nothing shifts on load
- [x] Correct in light and dark, verified against the design-system contrast contracts
- [x] Fully keyboard operable, with visible `:focus-visible` rings throughout
- [x] Correct at all three breakpoints
- [x] `prefers-reduced-motion` respected in CSS and the Motion `Reveal` contract
- [x] Zero hardcoded colors; every value resolves to a token
- [x] Every icon-only control has an `aria-label`
- [x] No `console` noise, no layout shift after hydration
