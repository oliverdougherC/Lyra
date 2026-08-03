# Phase 1 Interface Specification

This document specifies every screen Lyra ships in Phase 1, down to states, motion, and copy. It is
the companion to [design-system.md](design-system.md), which owns tokens and generic component
patterns. Where the two disagree, design-system.md wins on tokens and this document wins on layout
and behavior.

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
`MessageBubble`, `StreamingMarkdown`, `EndpointLocalityBadge`, `FactRow`, `RetrievalNotice`.

## Application Shell

```
┌────────────────────────────────────────────────────────────┐
│ ┌──────────────┐ ┌────────────────────────────────────────┐│
│ │              │ │  Header: breadcrumb · endpoint badge   ││
│ │   Sidebar    │ ├────────────────────────────────────────┤│
│ │   260px      │ │                                        ││
│ │              │ │  Route content, max 1200px, centered   ││
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

- Sidebar 260px, collapsible to a 60px icon rail, state persisted in `localStorage`
- Active class row uses `--accent-primary` text with a 2px left rule in the same token
- A skip-to-content link is the first focusable element and is visually hidden until focused
- The header carries the breadcrumb and the endpoint locality badge, nothing else
- Route content is capped at 1200px and centered, `p-6` desktop and `p-4` tablet

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
- `avatar` with the class code's first two characters on `--accent-surface` with
  `--accent-surface-foreground` text
- Class name, `h3`
- Course code and semester, `body-sm` in `--text-secondary`
- Footer row: document count and relative last-activity time, `caption` in `--text-tertiary`
- Whole card is one link; a `dropdown-menu` on the top right offers Rename and Delete
- Hover raises `shadow-sm` to `shadow-md` and scales 1.02x

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

## Screen: Class Workspace

Route `/classes/[id]`. Two panes via `resizable`, documents left and conversation right, with the
divider position persisted per class.

```
┌──────────────────────┬────────────────────────────────────┐
│ Documents        [+] │  Guide | Show        Profile >     │
│ -------------------- │ ---------------------------------- │
│  syllabus.pdf  ready │                                    │
│  hw3.pdf       ready │   conversation, max 720px          │
│  notes.md       60%  │                                    │
│  scan.pdf    no text │                                    │
│                      │ ---------------------------------- │
│  [ drop files here ] │  [ composer            send ]      │
└──────────────────────┴────────────────────────────────────┘
```

Below 1024px the panes become `tabs` labeled Documents and Chat. Below 640px the sidebar collapses to
bottom navigation per design-system.md.

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
- Page-level counters appear as `page 4 of 12` when known
- `Analyzing` carries the subtitle `Reading your syllabus for dates and topics`, because this stage
  can take minutes on a local model and silence reads as a hang
- Polled through TanStack Query, backing off from 500ms to 2s, and stopping on a terminal state
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
| Idle | dashed 1px `--border-strong`, `Upload` icon, `Drop PDF, TXT, or MD` |
| Drag over | border and icon become `--accent-primary`, fill `--bg-tertiary`, 1.01x scale |
| Rejected type | border `--danger-text`, message naming accepted types |
| Uploading | `progress` bar with filename and percent |

It is also a keyboard-accessible button that opens the native file picker, and it accepts multiple
files, queueing them.

### Conversation

Message rows, not chat bubbles on both sides. The student's message is right-aligned in
`--bg-tertiary` at `radius-md`; Lyra's response is full-width on the page background with no
container, so long explanations read like a document.

- Lyra messages carry a 24px `avatar` in `--accent-surface`
- Markdown renders incrementally during streaming, with `JetBrains Mono` code blocks, syntax
  highlighting themed per mode, and KaTeX math
- A caret pulses at the stream tail at `duration-normal`, and is removed under reduced motion
- Hover reveals Copy on any message, and Retry on the last Lyra message
- While streaming, the send button becomes Stop
- Timestamps are `caption` in `--text-tertiary`, shown on hover or for the first message in a
  time gap

**Empty conversation.** Not a blank pane. It shows the class name, a one-line statement of what Lyra
knows (`4 documents indexed, syllabus analyzed`), and three suggested prompts generated from the
class profile, such as `What is due next week?`. Each is a `button` that fills the composer without
sending, so the user stays in control.

**Composer.** Auto-growing `textarea`, three rows maximum before internal scroll. `Enter` sends,
`Shift+Enter` inserts a newline, and the hint is shown once using `kbd` for a new user. Disabled with
an explanation when no endpoint is configured, linking to Settings.

### Guide And Show Toggle

A two-option `button-group` above the conversation, not a `switch`, because both options are named
and neither is a default-off state.

- **Guide:** Socratic. Lyra asks leading questions and withholds the final answer.
- **Show:** direct. Lyra explains the full solution.

The active option uses `--accent-primary` fill with `--accent-foreground`. A `tooltip` explains each.
The setting is per session, persisted, and applied to the next turn rather than retroactively.

### Retrieval Notice

When retrieval was trimmed by more than half, a quiet `caption` row appears beneath the response:
`Some material did not fit in the model's context.` with a `tooltip` naming the omitted document
count. It is deliberately understated but never hidden, because the alternative is the user
mistaking a truncation artifact for the model being wrong.

## Screen: Class Profile

A `sheet` from the right, opened from the workspace header. In Phase 1 it is read-and-confirm, not a
full editor.

Facts are grouped into Deadlines, Topics, Grading, Professor, and Prerequisites. Each `FactRow`
shows the value, its source document, and its confidence.

- **Confirmed** facts: plain `--text-primary` with a `Check` in `--success-text`
- **Unconfirmed low-confidence** facts: `--bg-tertiary` fill, `HelpCircle` in `--info-text`, and a
  `caption` reading `Not used until you confirm this`, with Confirm and Reject buttons

That caption is load-bearing. It tells the user the system is not silently acting on a guess, which
is the whole reason this screen exists in Phase 1.

Correcting a value is inline: click to edit, `Enter` commits, `Escape` cancels. Rejecting removes the
fact and does not re-propose it from the same document.

**Empty.** `No profile yet. Upload a syllabus and Lyra will pull out dates, topics, and grading.`

**Extraction skipped.** When extraction was skipped because the endpoint is remote and
unacknowledged, this screen explains exactly that and offers the acknowledgement inline, rather than
appearing mysteriously empty.

## Screen: Settings

Route `/settings`. A single scrolling column at 720px, sectioned with `separator`, using `form` with
Zod throughout. No section is a raw key-value dump.

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

Theme `radio-group`: System, Light, Dark. Switching applies immediately with a crossfade at
`duration-normal`, suppressed under reduced motion.

## Motion Inventory

Every animation in Phase 1, so nothing is improvised:

| Element | Motion | Duration, easing |
|---------|--------|------------------|
| Route change | crossfade | `duration-normal`, `ease-in-out` |
| Class card entry | fade plus 8px rise, 50ms stagger, max 5 | `duration-normal`, `gentle` |
| Card hover | 1.02x scale, shadow raise | `duration-fast`, `spring` |
| Dialog and sheet | fade plus 8px rise, backdrop fade | `duration-normal`, `ease-out` |
| Message entry | fade plus 4px rise | `duration-fast`, `gentle` |
| Streaming caret | opacity pulse, loops | `duration-normal` |
| Ingestion step advance | check scale from 0.8, label crossfade | `duration-fast`, `spring` |
| Dropzone drag over | 1.01x scale, border color | `duration-fast`, `ease-out` |
| Skeleton | opacity shimmer, loops | 1200ms |
| Sidebar collapse | width transition | `duration-normal`, `ease-in-out` |
| Toast | slide from bottom right plus fade | `duration-normal`, `ease-out` |

Under `prefers-reduced-motion`: all transform and looping motion is dropped, opacity transitions are
capped at `duration-fast`, stagger is removed, skeletons become static, and the streaming caret is
solid.

## Keyboard Map

| Keys | Action |
|------|--------|
| `Tab`, `Shift+Tab` | Move focus; first stop is skip-to-content |
| `Enter` | Send message, or activate focused control |
| `Shift+Enter` | Newline in the composer |
| `Escape` | Close overlay, cancel inline edit, or stop generation |
| `Cmd/Ctrl+K` | Focus the composer from anywhere in a workspace |
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
- Sentence case for all headings, labels, and buttons. No title case, no all caps.
- No em dashes, no emoji, per conventions.md.
- Numbers are concrete: `4 documents indexed`, not `Several documents processed`.

## Definition Of Done For The Interface

A screen is complete when all of the following hold:

- [ ] All four data states implemented, each visually designed
- [ ] Skeletons match final layout dimensions, so nothing shifts on load
- [ ] Correct in light and dark, verified against the design-system contrast contracts
- [ ] Fully keyboard operable, with visible `:focus-visible` rings throughout
- [ ] Correct at all three breakpoints
- [ ] `prefers-reduced-motion` respected in both CSS and Framer Motion variants
- [ ] Zero hardcoded colors; every value resolves to a token
- [ ] Every icon-only control has an `aria-label`
- [ ] No `console` noise, no layout shift after hydration
