# Phase 3 Interface Specification

What large documents and text recognition change on screen, down to states, motion, and copy.
Companion to [rag-pipeline.md](rag-pipeline.md), which owns the pipeline and its stages, and to
[design-system.md](design-system.md), which owns tokens and generic component patterns. Where they
disagree, design-system.md wins on tokens, rag-pipeline.md wins on behavior, and this document wins
on layout.

> **Historical note (2026-08-29).** This specification predates the Ex Libris migration;
> [design-system.md](design-system.md) documents the shipped system and governs tokens.
> Values named here from the older visual system are historical, not normative.

Everything in [ui-phase-1.md](ui-phase-1.md) and [ui-phase-2.md](ui-phase-2.md) still holds. The
principles, the shell, the keyboard map, the copy guidelines, and both definitions of done are not
restated here; only what Phase 3 adds or changes is.

## What Is New

Phase 3 is the first phase whose interface work is mostly **corrections to existing screens** rather
than new ones. That is the right shape for it. A textbook is not a new kind of thing in Lyra, it is
a document that is a hundred times larger, and a scanned page is not a new screen, it is a state
that finally has a way out of it.

Two things do change in kind:

- **A page becomes a unit of work.** Through Phase 2 a document either read or did not. With
  recognition, page 7 can fail while pages 1 to 39 succeed, and the interface has to hold that
  without calling the document failed or calling it fine.
- **A document acquires structure the student never wrote.** `section_path` is Lyra's reading of how
  a book is organized. Pillar 3 says Lyra shows what it inferred, so the outline is visible.

One Phase 1 rule is repaid here rather than added to. `IngestionProgress` deliberately shows a page
count and not a page counter, because `pages_done` was written once at the end of a run and a
counter reading `page 1 of 32` from the first second to the last looks like a stall. Phase 3 makes
that number real for the pages it applies to, and the counter appears exactly there and nowhere
else.

## Component Inventory

No new shadcn registry items. Phase 3 adds no overlay, no form pattern, and no navigation shape.

New Lyra components: `PageFailureNotice`, `DocumentOutline`, `FigureBlock`.

Changed: `IngestionProgress`, `DocumentRow`, `ScannedPopover`, `DocumentDropzone`, `ProvenanceChip`,
`ProblemPanel`.

The small count is deliberate and is the evidence that the phase is shaped correctly. A phase that
needed six new components to ingest a bigger file would be a phase that had misunderstood what
changed.

## Screen: Class Workspace, Document List

### Ingestion progress stays four steps

Recognition does **not** become a fifth step. Reading, Splitting, Indexing, Analyzing remain, and
`recognizing` renders under **Reading**.

The Phase 1 code says in a comment that there are four steps "because OCR is not in Phase 1", which
reads as a promise of a fifth. That promise is not kept, on purpose. A student does not have two
concepts here. There is text in the file or there is not, and either way what Lyra is doing is
reading the document; splitting it into a separate step would ask the reader to learn the difference
between an extractable text layer and a transcribed one in order to watch a progress bar. Backend
state and interface steps are already not one to one, since five states render as four steps today.

What recognition changes is what Reading is allowed to say about itself.

### The page counter, and where it is allowed to appear

| Situation | Line under the steps |
| --- | --- |
| Any stage, no recognition running | `608 pages`, exactly as today |
| Recognition running | `Reading page 41 of 608`, advancing on polled state |
| Recognition finished, pages failed | handled by `PageFailureNotice` below, not here |

The counter appears only while recognition is actually running, because that is the only stage whose
per-page progress is real. Parsing a text-based PDF takes under a second for 608 pages, so a counter
there would flash and mean nothing. The Phase 1 rule is unchanged and is being honored rather than
relaxed: a number that moves is shown, a number that would not move is not.

`Reading page 41 of 608` also carries an elapsed counter once the wait passes ten seconds, which is
longer than anywhere else in the app rather than the same as the solver: this document said it
matched the solver's threshold, and the solver's is three seconds, as is the chat loader's. Ten is
deliberate. Those two report a wait that is unexpected; this one reports a wait that is expected to
run into minutes, and a timer that starts ticking immediately reads as alarm rather than as
reassurance. Past an hour it reads `2 hours 15 min`, because a recognition run on a book is hours
and `135 minutes` is a number the reader has to convert before it means anything.

### A large document does not get a different screen

A 608-page textbook uses the same row, the same steps, and the same copy as a syllabus. Nothing
switches to a "large document" mode at a page threshold, and no estimated time is shown.

Estimates are the tempting thing here and they are refused for the usual reason: an estimate is a
promise the machine cannot keep, hardware varies by an order of magnitude, and a countdown that
overruns is worse than no countdown. The page count and the honest stage are what is known, so they
are what is said.

`Analyzing` is the stage that can actually take minutes on a book, because it is one model call over
the whole document. Its existing subtitle already covers the silence and needs no change.

### Pages that could not be read

`PageFailureNotice`, a `caption` line on the row in `--text-tertiary`, with an action:

> `3 pages could not be read` `Try those pages`

The document is `ready`, not `failed`, and the notice is quiet rather than alarming. Thirty-nine
good pages and one bad one is a document that works, and styling it as a failure would tell the
student to throw away something that is mostly fine. `Try those pages` retries only the failed pages
and re-runs the stages downstream of them; it never re-reads the pages that already worked.

This replaces nothing. The existing `ready` row with `pages_skipped` reports pages that had no text
to find, which is a different fact from pages recognition tried and could not transcribe, and both
lines can be true of the same document at once.

**A document where every page failed** lands `failed` with the existing treatment, because there is
nothing to keep.

### Unsupported documents, which now have a way out

This state exists because OCR was cut from Phase 1, and ui-phase-1.md requires it to be excellent
rather than an afterthought. Phase 3 is the update its copy promises, so the copy has to stop
promising it.

The popover becomes:

> **Needs text recognition**
> This looks like a scanned document, so there was no text to read when it was uploaded. Lyra can
> read it now.

with a primary action `Read this document`.

The row keeps `FileWarning` in `--info-text` until the student runs it. Nothing is transcribed on
the student's behalf, and no migration sweeps the existing `unsupported` documents into a queue on
first launch. Two reasons, and the second is the one that matters: transcription is minutes of model
time per document, and against a configured remote endpoint it sends page images of the student's
own material somewhere. A capability arriving is not consent to use it on everything already on
disk.

**When the endpoint cannot see**, the action is absent and the popover instead reads:

> **Needs text recognition**
> This looks like a scanned document. Reading it needs a model that can see images, and the one in
> Settings cannot.

with a link to Settings. This is the same shape as the `unchecked` verdict's hover-card in
ui-phase-2.md, for the same reason: a feature that is unavailable says so plainly and points at the
thing that would make it available. It never renders as a failure of the document.

### The dropzone accepts images

The rejected-type error names the accepted types, and there are now more of them: `PDF, TXT, MD,
PNG, or JPG`. The idle copy is unchanged, because it already says `files or a folder` and does not
enumerate.

**WebP was in that list and has been removed**, because this PyMuPDF build refuses to decode one and
accepting it would mean carrying a second image dependency for a format a student's scan is very
unlikely to be in. Checked against a real `cwebp` file rather than against a format table. The rule
this follows is the one ui-phase-1.md set for the `unsupported` popover: the interface does not
promise a capability in order to look complete.

A dropped image ingests through the normal pipeline and is a one-page document. It shows the same
row, the same steps, and the page counter is absent, because one page is not progress.

### What the backend now offers these screens

Every screen in this document is built and verified — the Definition of Done at the end records
how. What they read and call:

| Screen element | What it reads |
| --- | --- |
| Page counter | `stage_detail == "recognizing"`, plus `pages_done` and `pages_total` |
| `PageFailureNotice` | `pages_failed`, only pages recognition tried and could not read |
| `Read this document`, `Try those pages` | `POST /api/documents/{id}/recognize`, one call |
| Whether either action is offered at all | `vision_supported` on the settings payload |
| Whether it has already been asked for | `recognize` on the document payload |

## Screen: Document Outline

`DocumentOutline` is a `collapsible` on a document row, closed by default, reading
`Structure Lyra found`. Open, it lists the section path hierarchy derived from the file, indented by
depth, each level in the small uppercase editorial label at its top level and body text below.

It exists because of pillar 3. `section_path` changes which chunks answer a question, and a student
whose 600-page book was parsed as one flat blob otherwise has no way to find that out except by
noticing that the answers got worse.

**It is read-only, and that is not a shortcut.** The path is derived from the PDF's own outline on
every ingest, so a hand edit would be overwritten by the next re-index and would be a control that
quietly does nothing. What the student can do about a wrong outline is re-index, and that action
sits at the foot of the disclosure.

**A document with no outline** shows the disclosure with the honest answer rather than hiding it:
`No structure found. Lyra is reading this document as continuous text.` A syllabus is supposed to
land here, so this is not phrased as a problem.

**A document indexed before Phase 3** shows `Indexed before Lyra could read structure` with a
`Re-index` action. Quiet, on the row, not a banner and not a dialog. The whole library is not swept
on upgrade for the same reason the `unsupported` documents are not: re-indexing is work the student
should choose, and their existing documents keep answering questions in the meantime.

## Changes In The Solver Workspace

### Provenance names the section

`ProvenanceChip` today reads the source filename and page. Where a chunk carries a `section_path` it
reads the filename, the path, and the page:

```
Kuttler.pdf · Matrices / Matrix Arithmetic · p. 90
```

The path is elided from the left when it does not fit, keeping the deepest section, because the
deepest one is the specific one and `... / Matrix Arithmetic` is more use than `Matrices / ...`.

**A resolved section reference gets no badge of its own.** When retrieval answered "use the result
from section 5.2" by looking the section up rather than by searching for it, the chip naming that
section is the evidence, and a second indicator saying `found structurally` would be narrating the
implementation to someone who wants to know where the answer came from. The provenance rule from
ui-phase-2.md is unchanged: provenance, never a score.

### Figures in a solution

`FigureBlock` renders an `artifact_part` with `kind = 'figure'` and `content_type = 'image'`: the
image on its own row at the reading column's width, a caption line beneath it in `caption` style,
and the same `ProvenanceChip` every other part carries, naming the page it was taken from.

- The image is served from the backend like a rendered page, and is cached the same way
- It never exceeds the reading column, and a wide figure scales down rather than scrolling. Math
  scrolls because cutting an equation loses information; a figure that is 20% smaller loses none
- A figure whose file is missing renders as its caption and provenance alone, with
  `Figure not available`, rather than a broken image or an empty row. The solution is worth more
  than the figure

**Figures print.** The print stylesheet has never carried an image, and export dropping the diagram
a solution refers to would leave a student holding a page of prose about a picture that is not
there.
Figures print at the reading width, are not split across a page break where the browser allows it,
and keep their caption and provenance line.

## Motion Inventory

Additions to the Phase 1 and Phase 2 inventories. Everything there still applies.

| Element | Motion | Duration, easing |
| --- | --- | --- |
| Page counter increment | none; the number is replaced | n/a |
| Outline disclosure | height transition, matching the accordion | 200ms |
| Figure entry in a solution | `Reveal` with the step it belongs to | 250ms, gentle |
| Page failure notice | `Reveal` on arrival | 250ms, gentle |

The page counter deliberately has no motion. A number that animates as it changes invites the reader
to watch it, and this one moves several hundred times over a long document. It is replaced in place.

Under `prefers-reduced-motion` the existing rules cover all four: `Reveal` becomes a 150ms opacity
fade, and the disclosure transition is dropped.

## Keyboard

Additions to the Phase 1 and Phase 2 maps. Nothing existing changes.

| Keys | Action |
| --- | --- |
| `Enter` | Expand or collapse the focused document outline |
| Arrows | Move within the outline, as they already do within the document list |

`Try those pages`, `Read this document`, and `Re-index` are ordinary buttons on the row and are in
the tab order where they appear. None of them is behind a hover.

The page counter is inside the row's existing `aria-live="polite"` region, and it is the reason that
region needs a rule it did not need before: an update several hundred times a minute would flood a
screen reader. The counter is announced on stage changes and on completion, not on every page.

## Copy Guidelines

The Phase 1 and Phase 2 guidelines hold in full. Two additions:

- **Never promise a future release.** Phase 1's unsupported copy said Lyra would read scans "in a
  future update", which was honest then and is a bug now. Copy that describes what a later version
  will do is copy that someone has to remember to delete.
- **A page is not a document.** `3 pages could not be read` is a fact about three pages. Never let
  it render as `This document could not be read`, and never let the row's state contradict the
  line.

## Definition Of Done For The Interface

In addition to every item in ui-phase-1.md and ui-phase-2.md's definitions of done:

- [x] The page counter appears only while recognition is running, and every number on it comes from
      polled backend state. Watched on the real thing: `Reading page 8 of 8 · 1 minute` under
      **Reading**, four steps, no fifth
- [x] A document with some failed pages is `ready`, says so quietly, and retries only those pages.
      The retry is the same call as the first run, so "only those pages" is a property of the
      backend rather than of this screen
- [x] An `unsupported` document can be read from the interface, and nothing is transcribed without
      the student asking. Driven end to end in the browser against the real endpoint: the scanned
      Fourier tables went from `no text` to `Ready` from the popover
- [x] With a non-vision endpoint configured, every recognition affordance is absent and explained,
      and none of them fails on use. Covered by test rather than by a second endpoint
- [x] Images upload, ingest, and appear as one-page documents. A PNG of a scanned page uploaded,
      landed `unsupported` with one page, and recognized to `ready`
- [x] The outline disclosure is correct for a book with an outline, a document without one, and a
      document indexed before Phase 3. All three seen: 107 sections over the reference textbook,
      "no sections found" on a problem set, and the same on every document predating migration 012
- [x] Provenance renders a section path where one exists and degrades to filename and page where
      none does. Read live off the chunk, so re-indexing improves old citations rather than
      leaving them quoting a reading that has been replaced
- [x] Figures render in the workspace and in print, and a missing figure costs a caption rather than
      the solution. An uncaptioned figure on a crowded page is now filed under its own problem where
      the page's diagrams and markers alternate, which is the acceptance homework's layout: its
      three block diagrams reach the first three of its seven questions and the rest get none. The
      rule and the two it replaced are in rag-pipeline.md
- [x] Correct at 1280, 768, and 375, in both themes. Driven in the browser against a copy of a real
      class - 37 documents, a 608-page book with a four-level outline, a scan waiting to be read, a
      document with three pages recognition could not transcribe, and a solution set carrying real
      crops. One defect found and fixed: `FigureBlock`'s image was being **blown up** at 1280,
      because the figure is a flex column and a stretched item takes the column's full width
      whatever its own is. The acceptance homework's diagrams are 771px and were rendering at 1215,
      which is a blurred picture of hairlines. `self-start` makes the scaling one-directional.
      Nothing else overflowed its box or the viewport at any of the three widths in either theme,
      and `scripts/check_contrast.py` passes with no failures
