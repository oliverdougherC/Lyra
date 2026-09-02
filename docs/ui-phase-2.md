# Phase 2 Interface Specification

The solver's screens, down to states, motion, and copy. Companion to
[solver-phase-2.md](solver-phase-2.md), which owns the data model and behavior, and to
[design-system.md](design-system.md), which owns tokens and generic component patterns. Where they
disagree, design-system.md wins on tokens, solver-phase-2.md wins on behavior, and this document
wins on layout.

> **Historical note (2026-08-29).** This specification predates the Ex Libris migration.
> The visual system it names - faces and palette included - was replaced;
> [design-system.md](design-system.md) documents the shipped system and governs tokens, so
> values named here from the older system are historical, not normative.

Everything in [ui-phase-1.md](ui-phase-1.md) still holds. The principles, the shell, the keyboard
map, the copy guidelines, and the definition of done are not restated here; only what Phase 2 adds
or changes is.

## What Is New

Phase 1's interface is a place to ask. Phase 2's is a place to read something Lyra produced and
decide whether to trust it. That changes what the screen owes the reader: not just an answer, but
an account of where the answer came from and what was checked.

Two principles from Phase 1 carry most of the weight here:

- **The machine is honest.** A solve can run for tens of minutes. Every second of that has to name
  what is actually happening, from real backend state, and a verdict never overstates itself.
- **Four states, always.** Plus one this phase adds: a solve is also `waiting for you`, which is
  neither loading nor done and must not be dressed as either.

## Component Inventory

Additional shadcn registry items:

| Purpose                      | shadcn item                                              |
| ---------------------------- | -------------------------------------------------------- |
| Split panes                  | `resizable` (already listed in Phase 1, first used here) |
| Problem list                 | `accordion`                                              |
| Revision history, tool calls | `collapsible`, `hover-card`                              |
| Step actions                 | `context-menu`                                           |

New Lyra components: `SolveProgress`, `SegmentationReview`, `ProblemCard`, `SolutionStep`,
`VerdictBadge`, `ProvenanceChip`, `SourcePane`, `StepGuidePanel`, `ToolCallTrace`,
`RevisionHistory`, `SourcePicker`.

## Navigation

A class in the sidebar currently expands into its conversations while that class is open. It gains
a second group under the same expansion:

```
· ECE 203 Continuous-Time Signals
    CONVERSATIONS
    · Laplace transform of a ramp
    · What is due next week?
      New chat
    SOLUTIONS
    · Problem set 4
    · Problem set 3
      New solution set
```

Both groups use the small uppercase editorial label reserved for pane and section context. A
solution set still running carries a quiet state dot beside its name, matching the document list's
treatment: `--text-tertiary` for waiting, the `progress` ring for working.

Routes:

- `/classes/[id]/solutions/new` - setup
- `/classes/[id]/solutions/[artifactId]` - the solver workspace, in whichever state the artifact is

The breadcrumb reads `Classes / ECE 203 Continuous-Time Signals / Problem set 4`. Below 640px the
ancestor crumbs fold as they already do.

## Screen: New Solution Set

Route `/classes/[id]/solutions/new`. A single column at 720px, matching Settings, because this is a
form and not a workspace.

**Populated.** Three raised-paper sections, each a `Card` with a display heading:

1. **Problem set.** The `DocumentDropzone` from Phase 1, reused unchanged, over a `SourcePicker`
   list of the class's `ready` documents with checkboxes. A document uploaded here ingests through
   the normal pipeline and appears in the list when it becomes ready. Selecting more than one
   document is ordinary, not an edge case: a set that spans two files is common.
2. **Reference solutions, optional.** The same picker over the same documents, with the helper text
   `Worked solutions Lyra should follow for notation and method.` Nothing is selected by default.

   Choosing a problem set offers any file that looks like its answers - `homework_5.pdf` offers
   `ECE203_homework5_solution.pdf` - naming what it matched, with `Use it` and a dismiss. The match
   requires agreement on both the kind of assignment and its number, so lab 5 is never offered for
   homework 5: a loose match hands the solver a wrong reference that then looks plausible. Nothing
   is ever selected on the student's behalf. The panel says that solutions to the same set steer the
   answers rather than only the notation, because that changes what the result means and the moment
   of the decision is where it is worth saying.

3. **Title.** An `input`, prefilled from the first selected document's filename with its extension
   removed, editable.

The primary action is `Find problems`, not `Solve`, because that is what pressing it does. Disabled
until at least one problem-set document is selected, with the reason stated beside it rather than
in a tooltip.

**Loading.** Skeleton rows in the picker matching the real row height.

**Empty.** The class has no `ready` documents. The `empty` primitive: `FileText` at 32px in
`--text-tertiary`, heading `Nothing to solve yet`, body `Upload a problem set and Lyra will read it
first.`, and the dropzone directly beneath. Not a dead end pointing back at another screen.

**Error.** `alert` in danger tokens with the backend message and a `Retry`.

**A document still ingesting** appears in the list, dimmed and not selectable, with its stage label.
A student who just dropped a file should see it there rather than wonder whether the upload worked.

**An `unsupported` document** appears dimmed with the Phase 1 popover. The solver cannot read a scan
either, and saying so here is better than omitting the file and looking like it was lost.

## Screen: Solver Workspace

Route `/classes/[id]/solutions/[artifactId]`. One route, four phases driven by artifact state. The
phase is never guessed from elapsed time.

| Artifact state          | What the screen is                       |
| ----------------------- | ---------------------------------------- |
| `pending`, `segmenting` | Finding problems                         |
| `awaiting_review`       | Segmentation review                      |
| `solving`               | Solving, with results landing            |
| `ready`, `cancelled`    | The solution document                    |
| `failed`                | Failure, with what failed and what to do |

### Phase: Finding problems

`SolveProgress` on its own, centered in the workbench. One line naming the stage, the source
document being read, and an elapsed counter once the wait passes three seconds. The `LyraMark`
animates alongside, as it does while a turn is thinking.

Stage labels, verbs rather than internal state names:

| State        | Label                      |
| ------------ | -------------------------- |
| `pending`    | `Queued`                   |
| `segmenting` | `Reading your problem set` |

Segmentation is a model pass over a whole document and can take a minute on local hardware, so
silence would read as a hang.

### Phase: Segmentation review

The gate. This screen exists so a missed or merged problem costs one edit instead of a full re-run,
and it has to make that trade obvious rather than feeling like a speed bump.

Header: `Lyra found 8 problems` with the subtitle `Check these before solving. Fixing a problem now
is much faster than re-solving one later.` Concrete count, stated reason.

Below it, a reorderable list of `ProblemCard`s. Each card carries:

- The problem label (`Problem 4`, `Exercise 3.14`), editable inline: click to edit, `Enter` commits,
  `Escape` cancels, matching `FactRow`
- The source filename and page, `caption` in `--text-tertiary`, shown only when it differs from the
  card above, matching the profile view's source-line rule
- The whole problem statement, typeset, with a `collapsible` holding the raw text to edit it
- Sub-parts as a nested list, each shown in full and individually removable
- A `dropdown-menu`: `Merge with next`, `Split here`, `Remove`

**Nothing on this screen is truncated.** The card showed two clamped lines for one revision and
that was wrong: on a signals sheet the first two lines are the sentence before the mathematics, so
the student was asked to confirm a reading of their homework with the equations cut off. A gate is
worth exactly what can be read at it, and a list of ten problems that is long to scroll is a much
smaller cost than a merged problem discovered forty minutes into a solve.

Statements and sub-parts are typeset, not printed raw. PDF extraction flattens exponents,
subscripts, and piecewise definitions into the line, so segmentation transcribes the mathematics
back into LaTeX and this screen renders it. That is what makes the check possible: a student
comparing `x(t) = e-2tu(t -3)` against their sheet is comparing something the sheet does not say.
The collapsible editor stays raw text, because the LaTeX is what a correction has to change, and
its control reads `Edit the statement` rather than promising to reveal something already visible.

Where a problem has sub-parts, the statement is cut back to the text that introduces them, and the
parts themselves are printed underneath. The segmenter copies verbatim and so repeats the sub-parts
inside the statement as well; printing both shows the same problem twice. Nothing is lost by the
cut, because what is cut is the list rendered directly below it.

A card whose text the student edited carries a quiet `Edited` badge, so a later re-read knows the
statement is not verbatim from the page.

Every structural edit - removing a sub-part or a problem, merging, splitting, adding - is undoable
with `Cmd/Ctrl+Z`, and an `Undo` button appears beside `Read it again` once there is something to
take back. Typing is not on that stack: the statement editor keeps the browser's own undo. Removing
a sub-part is one click on a small target beside text the student is still reading, and the only
route back used to be re-reading the whole sheet.

The primary action is `Solve 8 problems`. Beside it, `Cancel`, which deletes the artifact after an
`alert-dialog` confirmation naming what is discarded.

**Empty.** Segmentation found nothing. Not an error: some documents are prose, and a homework set
Lyra cannot segment is a real outcome. Heading `Lyra could not find separate problems`, body `This
document does not look like a numbered problem set. You can add problems yourself, or solve it as
one.`, with `Add a problem` and `Solve as one problem` actions. No dead end.

**Error.** Segmentation failed upstream. The backend message, `Retry`, and `Delete`.

### Phase: Solving

The workbench splits into its two panes, and results land in the right one as they complete. The
student can read problem 1 while problem 7 is still running, which is the whole reason results are
written per problem.

Above the panes, a `SolveProgress` strip:

- `Solving problem 3 of 8` with a tokenized `progress` bar. Both numbers are real; when
  `problems_total` is null the bar is absent and the copy reads `Finding problems` rather than
  showing an empty bar
- The current problem's label, so a long problem does not look like a stall
- An elapsed counter
- A `Stop` action, which cancels the run and keeps completed problems

The strip is built in-house and driven entirely by polled backend state. Nothing on it advances on
a timer, and it never narrates a step that has not happened. That is the reason an off-the-shelf
loading component is not used: canned components narrate fixed sequences on a timer, which is
exactly the dishonesty ui-phase-1.md rules out.

Per-problem status appears on each problem's own row:

| Part status | Indicator                                          |
| ----------- | -------------------------------------------------- |
| `pending`   | `--text-tertiary` dot, row dimmed                  |
| `solving`   | `progress` ring plus `Solving`                     |
| `verifying` | `progress` ring plus `Checking`                    |
| `complete`  | the verdict badge                                  |
| `failed`    | `AlertCircle` in `--danger-text`, with `Try again` |

A newly completed problem enters with the standard `Reveal`: fade plus 8px rise, no scroll jump.
The view never scrolls itself to a problem that just landed; the student is reading.

### Phase: The solution document

Two panes in a `resizable` group filling the window, source left at 45% and solution right at 55% by
default, with the split persisted per class in `localStorage`.

```
┌──────────────────────────────────────────────────────────────┐
│ Classes / ECE 203 / Homework 7                [Delete] [◉]   │
├─────────────────────────────┬────────────────────────────────┤
│ hw7.pdf                 [⤢] │ 1 2 3 4 5 6 7 8   [Export] [⤢] │
│                             │ ⟨4⟩ Problem 4       ✓ Checked  │
│    rendered page            │  │   Step 1 …                  │
│    problem 4 banded         │  │   Step 2 …        [notes]   │
│                             │  │   Answer …                  │
│                             │ ⟨5⟩ Problem 5       ! Refuted  │
│      ◂ page 2 of 6 ▸        │  │   Step 1 …                  │
└─────────────────────────────┴────────────────────────────────┘
```

**No card, and no title row.** This screen is the two panes; they run to the window edges, the app
header is their title bar, and the pane headers are the only chrome between the header and the work.
Measured at 1280x800, which is the 13-inch laptop this is built for, the framing this replaced cost
220px of height and 309px of width, leaving the reading column at 525x580. It is now 560x703, and
the source page renders 427px wide rather than 398.

**Either pane can take the window.** A `FocusToggle` in each pane header hands it the full width and
collapses the other; the toggle in the enlarged pane hands the width back. The split is the right
default, because the point of the screen is a solution beside the sheet it came from, but half of
what is left after the rail renders a Letter page at about 47 DPI, and reading the sheet closely is
worth one pane for a minute. Clicking a problem's band on the page while the document has the window
brings the solutions back with it: asking for a solution is asking to see it.

**Source pane.** PDFs render as page images produced by PyMuPDF and served per page, not as an
embedded PDF viewer. That choice buys exact anchoring, identical rendering in both themes and every
browser, no new frontend dependency, and it is the same rasterization Phase 3 needs for figures and
text recognition. TXT and MD sources render as their extracted text instead, with the same anchors.

Each problem occupies a band of the page, drawn over the image rather than into it: the render is a
faithful copy of the student's own sheet, and marking it up would let the two columns disagree
about what the sheet says. A band runs from its own problem's marker to the next one's, so the
answer to "where does problem 3 end" is always "where problem 4 starts", and geometry can never
contradict the segmentation confirmed at the review gate. The problem being read carries a 2px
`--accent-primary` edge and a wash at 8%; the rest appear on hover.

Where a marker sits comes from `rag/locate.py`, which searches the page for the leading marker of
the label segmentation recorded (`Problem 3`, `3.`) and stores it as fractions of the page box, so
it survives the page being rendered at whatever width the pane has. A bare word is never searched
for: it would hit the first paragraph that happened to use it. A set whose markers could not be
found simply has no bands.

Page navigation is a footer control; the pane also scrolls freely, and scrolling it away from the
anchored page does not change the selection.

**Solution pane.** A document, not a control panel. Every problem is rendered and the pane scrolls;
collapsing is still there for a set of fourteen, but it is a thing the reader does rather than a
state they have to undo. Steps render as the assistant reading surface from Phase 1: Source Serif 4
at 1.0625rem, KaTeX display math on its own rows, wide math scrolling horizontally rather than
overlapping prose.

The pane header holds a `ProblemStrip` where its title used to be: one chip per problem, carrying
the number and a verdict dot, scrolling horizontally rather than wrapping so the two panes' content
stays on one line. It says where you are and takes one click to be somewhere else. The grounding
line (`6 of 7 steps grounded in your material`) sits at the foot of a problem beside its check
trace, not under its title: read first it looks like a mark out of ten for work nobody has read yet.

**A problem is a section, not a row.** Its heading pins to the top of the reading pane while you are
inside it, carrying a number chip in the same token the strip uses, the label, and the verdict; a
rail runs down the length of its body; and problems are separated by space rather than a rule, so
one ends where the next one's heading begins. Reading a set straight through, it was otherwise very
easy to scroll from one problem into the next without noticing, which on a page of mathematics means
reading an answer to a question you are not looking at. Colour is on the number chip and nowhere
else: tinted blocks behind long derivations get tiring in a way a rule and a heading do not.

**Anchoring is bidirectional.** The problem under the top of the reading pane is the selected one,
measured from scroll position rather than from which panel is open, and the source page follows it
without the reader selecting anything. Clicking a band in the source scrolls to that problem and
opens it. Neither direction steals focus.

**Loading.** Skeletons matching the two-pane layout: a page-shaped block left, four problem-header
rows right. Never a spinner.

**Error.** `alert` in danger tokens spanning both panes, with the backend message and `Retry`.

### Verdict badges

`VerdictBadge` is a compact status pill. Color is never the only signal; the label always differs.

| Verdict       | Presentation                                  | Label              |
| ------------- | --------------------------------------------- | ------------------ |
| `verified`    | `--success-text` on `--success-fill`, `Check` | `Checked`          |
| `refuted`     | `--danger-text`, `AlertTriangle`              | `Check failed`     |
| `uncheckable` | `--text-tertiary`, hollow dot                 | `Nothing to check` |
| `unchecked`   | `--text-tertiary`, hollow dot                 | `Not checked`      |

`uncheckable` and `unchecked` deliberately look alike and read differently, because they are both
honest non-answers and neither is a pass. Their `hover-card` states the difference: `Nothing in
this solution could be checked mechanically. This is normal for a proof.` versus `Your model
endpoint does not support the tools Lyra checks with.` with a link to Settings.

`refuted` is never quiet. The problem header carries the badge, and the body opens with a
`--danger-fill` note naming the check that disagreed and what it returned. A refuted solution is
still shown in full: hiding it would leave the student with nothing, and they may well spot the
error themselves.

### Provenance and tool calls

A step grounded in retrieved course material carries a `ProvenanceChip`: a compact badge reading
the source filename and page, on the step's own row rather than inline in its prose. A step with no
provenance carries nothing at all. There is no confidence percentage anywhere, because a number
nobody can audit reads as precision that does not exist.

`ToolCallTrace` is a `collapsible` under a problem, closed by default, reading `3 checks run`. Open,
it lists each tool call: the tool name in `JetBrains Mono`, the expression it was given, and what
came back. This is the audit trail, and it is the reason to believe the badge.

### Corrections

Every step and the final answer carry an action row:

- `Ask about this step` is always visible. Asking is the thing a student most wants from a step they
  do not follow, and an affordance that only exists once you are hovering the right row is one most
  readers never find
- `Edit`, `History`, and `Copy` stay hidden until the row is hovered or something in it takes focus,
  matching the message action row from Phase 1. `Edit` makes the step's markdown editable in place;
  `Escape` cancels and an explicit `Save` commits, because a step is long enough that `Enter` must
  insert a newline

At problem level, a `dropdown-menu` offers `Mark wrong and re-solve`, `Regenerate`, and `History`.

**Mark wrong and re-solve** opens a `dialog` with one `textarea`: `What is wrong with it?`,
optional, with the helper `Lyra will use this when it tries again.` Submitting re-solves that
problem only. The existing solution stays visible and dimmed with a `Re-solving` indicator until
the new one is written, so a regeneration that fails upstream costs the student nothing.

**History** is a `sheet` listing the part's revisions newest first, each with its origin
(`Generated`, `Regenerated`, `Your edit`), its note where one exists, and a `Restore`.

### Asking about a step

`Ask about this step` opens a `StepThread` **underneath that step, in the solutions column**, under
a rail in `--accent-primary`. Nothing is blurred, nothing is covered, and the step before this one
stays exactly where it was.

This was a 480px `sheet` with the workspace blurred behind it, and that was wrong: a student asking
"how did we get here" needs to see the previous step, and the panel covered it. A conversation about
a passage belongs in the margin of the document, not in a room next to it.

Inside is the Phase 1 conversation surface, unchanged: the same composer, the same streaming
markdown, the same thinking indicator and reasoning trace, the same Guide and Show toggle opening on
Guide. `ChatPane` grows an `inline` layout for this, which gives up its own scroll container and
follows the stream in the pane it was opened inside, so there is one implementation of a turn rather
than two. It is an ordinary session that happens to be anchored, and it appears in the sidebar under
Conversations like any other.

**Nothing is created until the first message is sent.** Reading a solution with the thread open on
six different steps used to leave six untitled conversations behind it.

**It opens ready to type.** The thread scrolls its composer into view and takes focus, and there is
no empty-state copy at all: the header names the step and the cursor is already in the box, so a
paragraph explaining that you may type in the box you are typing in was spending a third of the
thread's height to say nothing.

**The conversation is scoped to the step.** The tutor is written to teach, and left alone on a
step it answers the question and then drifts into the next one. An anchored session answers what
was asked, and stops; the walkthrough is the student's to ask for. The rule is in
`format_step_context`, described in solver-phase-2.md.

### Export

`Export` in the workspace header opens a `dropdown-menu` with `Print or save as PDF`.

Export is the browser's print path with a dedicated print stylesheet, not a backend renderer. KaTeX
already typesets the math correctly in the page; a Python PDF library would mean re-solving math
typesetting and adding a dependency to do it worse.

The print stylesheet:

- prints the solution pane only, at full width, with the source pane and all chrome removed
- expands every problem regardless of accordion state
- prints on the light palette in both themes, because paper is paper
- opens with the artifact title, the class, and the source filenames
- keeps verdict labels as text, never as color alone, and prints refutation notes in full
- prints provenance as a footnote-style line under each step it belongs to
- avoids breaking a step across pages where the browser allows it

An exported document that hides a failed check would be worse than no export, so nothing about the
verdicts is dropped for tidiness.

## Failure, Cancellation, And The Long Wait

**A failed run** shows what failed and what to do, never a bare error. The heading names the stage
in plain words (`Lyra could not read your problem set`), the body carries the backend message, and
the actions are `Try again` and `Delete`. Problems already solved before the failure are listed
below, still readable.

**A cancelled run** is not a failure and is not styled as one. `Stopped at problem 5 of 8` with the
five completed problems in full, and a `Solve the rest` action that resumes from problem 6.

**A run left waiting** appears in the sidebar and the solutions list as `Waiting for you`, in
`--info-text`. It is the one state that is neither working nor finished, and the student is the
thing it is blocked on, so it says so.

**Closing the tab does not stop a solve.** The job is a background job. Returning to the route
picks up the current state from the poll, and a finished run confirms with a `sonner` toast on
arrival if the tab was still open.

## Motion Inventory

Additions to the Phase 1 inventory. Everything there still applies.

| Element                    | Motion                                                 | Duration, easing |
| -------------------------- | ------------------------------------------------------ | ---------------- |
| Problem card entry, review | staggered fade plus 8px rise, capped at five steps     | 250ms, gentle    |
| Completed problem landing  | `Reveal` on the problem row, no scroll movement        | 250ms, gentle    |
| Accordion expand           | height transition                                      | 200ms            |
| Pane anchor scroll         | scroll to the anchored page or problem                 | 200ms, gentle    |
| Source outline             | opacity and border-color change on the selected region | 150ms            |
| Re-solving a problem       | existing content dims to 60% opacity, no collapse      | 200ms            |
| Step Guide panel           | sheet fade plus 8px, per the Phase 1 overlay rule      | 200ms            |
| Solve progress bar         | width transition to the polled value only              | 200ms, linear    |

Under `prefers-reduced-motion`: the anchor scroll becomes instant, `Reveal` becomes a 150ms opacity
fade, the progress bar jumps rather than eases, and the dim on a re-solving problem is applied
without transition. The progress bar never animates ahead of a polled value under any preference,
because an eased bar that arrives before its data is a fiction.

## Keyboard

Additions to the Phase 1 map. Nothing existing changes.

| Keys         | Action                                                                             |
| ------------ | ---------------------------------------------------------------------------------- |
| Arrows       | Move between problem cards in review, and between problems in the solution outline |
| `Enter`      | Expand or collapse the focused problem; commit an inline label edit                |
| `Escape`     | Close the step panel, cancel an inline edit, or close a dialog                     |
| `Cmd/Ctrl+P` | Browser print, which is the export path, deliberately not overridden               |

Every action in the solution pane is reachable without a pointer, including the per-step action row,
which becomes visible on focus as well as on hover. The two panes are separate landmarks with
accessible names, so a screen reader user can move between source and solution directly.

Icon-only controls carry `aria-label`s: page navigation, the stop action, per-step copy and edit,
and the panel close.

## Copy Guidelines

The Phase 1 guidelines hold in full. Three additions specific to this phase:

- **Never claim a check that did not happen.** `Not checked` is a complete sentence. Do not soften
  it to `Looks right` or omit the badge.
- **A refutation names the check, not the student.** `The integral in step 3 does not match: Lyra
computed 4/3, the solution says 3/4.` Never `Your solution is wrong`, and never a bare `Error`.
- **Counts are exact and come from the backend.** `Solving problem 3 of 8`, not `Almost done`. When
  a count is not yet known, say what is happening instead of guessing at a number.

## Definition Of Done For The Interface

In addition to every item in ui-phase-1.md's definition of done:

- [x] All five artifact phases implemented as designed screens, including `awaiting_review`
- [x] No progress indicator advances on anything but polled backend state
- [x] Every verdict is distinguishable without color, and no non-verdict renders as a pass
- [x] Both panes are keyboard-navigable and anchoring works in both directions from the keyboard
- [x] The print stylesheet drops no verdict, refutation, or provenance
- [x] A solve survives closing the tab, and the route recovers its state from the poll
- [x] Correct at 1280, 768, and 375, with the panes becoming line tabs below 1024
