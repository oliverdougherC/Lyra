# Ex Libris Migration: Component Inventory and Transition Plan

Written 2026-08-07, alongside [exlibris-design-system.md](exlibris-design-system.md). This
plan moves the app from the parchment system to Ex Libris without colliding with the two
active work streams (Phase 4 agent tools; the writer roadmap W1 to W6). It is sequenced
against the ownership table in [ui-overhaul.md](ui-overhaul.md) section 7: the same items that
were "safe now" for UX work are safe now for reskinning, and the same writer-coupled items
remain the writer stream's to adopt.

**Ground rules for the whole migration:**

- Work happens on a dedicated branch or worktree; the main tree currently carries two agents'
  uncommitted work. One commit checkpoint per converted screen, tests green at each.
- A screen is converted when it passes the per-screen checklist in section 5, not when it
  compiles. Both modes, keyboard, contrast, reduced motion, print.
- The UX changes from ui-overhaul.md and this reskin land together where they touch the same
  component (one rewrite, not two), and separately where they do not.
- No file in components/drafts/ or chat-pane internals is touched outside coordination with
  the writer stream. No change to backend files, ever, in this effort.

## 1. Stage 0: foundations

Everything later is mechanical if this stage is done well.

1. **Fonts.** Swap next/font loads: Cinzel 600, EB Garamond 400/400i/600, Caveat 500 in;
   Fraunces, DM Sans, Source Serif 4 out; JetBrains Mono stays for code. Update the font CSS
   variables in globals.css (--font-heading, --font-sans, --font-ai-response map to
   inscription, print, print).
2. **Tokens.** Replace the values behind the existing token bridge in globals.css with the
   section 3 tables from the design system. The bridge's shape survives (components already
   consume --bg-primary, --accent-primary, and the shadcn aliases); this is a value swap plus
   a handful of new tokens: --hand, --hand-red, --trust, --hl, --gold-line, --lamp,
   --accent-text. Dark mode drops the marble variable entirely and gains the lamp radial.
   Delete the paper-grain overlay; stone texture replaces it in light, nothing replaces it in
   dark.
3. **Marble asset.** Bake the light texture once (generator recorded with the prototype
   source) into an inline data URI in globals.css.
4. **Contrast script.** Extend the recorded pairs to the new palette, both modes, including
   text-on-stone; run it; it gates every later commit.
5. **New primitives**, each small and shared:
   - StatusWord (quiet print, warn, hand variants; words only)
   - Penbar (printed track, stroke fill, numerals; fraction is data)
   - TheMark (ring and check; renders only from a verification result)
   - HandUnderline and HandRing (inline SVGs, width:100%, draw-in, reduced-motion aware)
   - Engraved (nameplate treatment), Lintel (crumb, dentil), LaurelRule
   - GroupSheet (ruled header, numeral count, columnar rows)
   - ArtifactListPanel and ErrorState (the ui-overhaul.md section 2.3 extraction, built
     directly in the new language rather than reskinned later)
6. **Rules as lint.** The em-dash guard becomes a test over UI strings; add a grep gate for
   font-style:italic outside math contexts.

## 2. Component inventory

Existing component to Ex Libris treatment. "Recipe" names refer to the design system.

| Current | Becomes | Notes |
| --- | --- | --- |
| app-shell.tsx, app-sidebar.tsx | Stone rail, navigation only | Actions leave the rail (ui-overhaul 2.2); classes expand to recent destinations; mode quick-toggle at foot |
| app-header.tsx | Lintel: crumb plus Local chip | The chip is the resurrected EndpointLocalityBadge (ui-overhaul 2.7); plain-noun titles |
| ClassCard / class list | Home resume rows | Sentence sub, in-flight penbar, deadline as printed warn (ui-overhaul 3.1) |
| class-hub.tsx tabs | Engraved-strip tabs with hand underline | Counts as printed superscripts; underline hugs the word |
| Hub overview digest | Synthesis sheet: In flight, Resume, Ahead | ui-overhaul 3.2 lands here in the same rewrite |
| documents-pane.tsx, document-row.tsx | GroupSheet archive | Filter field, doc-type groups, columnar rows; recognition affordances keep their ui-phase-3 behavior with new skin; drop the motion/react dependency |
| SourcePicker | GroupSheet picker variant | Problem-set-likely groups sort first (ui-overhaul 2.6) |
| SolveProgress | Stage strip: verb, hand depth, penbar, elapsed, Stop | Honesty rules identical; only the rendering changes |
| VerdictBadge | TheMark plus printed words for non-verified verdicts | Only the passing verdict may use the Mark; refuted or uncheckable stay printed words |
| ProvenanceChip | Footnote provenance line | Gold superscript marker, printed text |
| StepThread, ToolCallTrace | Inline thread sheet; dashed trace row | As prototyped in the solver view |
| chat-pane.tsx | Ladder, thread sheet, composer | Coordinate with writer stream (inline layout is theirs); highlighter and Mark render in assistant messages; suggestion prompts change is ui-overhaul 3.3 |
| settings-form.tsx | Sectioned sheets with side nav | Split the 667-line form; coordinate timing with W3's research toggle (ui-overhaul 3.5); switches flex:none |
| Empty | Empty on paper: label, sentence, one verb | Adopt everywhere, including the drafts surface (ui-overhaul 2.4) |
| Buttons (shadcn) | Sage and butter plaques, ghost variant | Focus ring from the hand token |
| DeckSession, QuizRunner | Deferred | After prototype round two designs them (section 4) |
| Draft workspace components | Writer stream's, with W1 to W5 | Gutter cards, Plan/Sources/Activity panel, chat drawer, suggestion hunks, all prototyped; handed over as design input, not converted here |

## 3. Screen conversion order

Each step is one commit-checkpointed unit; order minimizes rework and collision.

| # | Screen | Depends on | Coordination |
| --- | --- | --- | --- |
| 1 | Shell: rail, lintel, tokens live app-wide | Stage 0 | None; this flips the global look and is the point of no return, so it lands only when stage 0's checklist is green on the shell itself |
| 2 | Home | 1 | None |
| 3 | Class hub (overview synthesis, tabs, panels) | 1, ArtifactListPanel | Drafts panel list is fair game; Write/Review dialogs are not (W1 depth dial) |
| 4 | Documents tab and pickers | GroupSheet | None |
| 5 | Settings | 1 | Land before or with W3's toggle; agree the section map with the writer stream first |
| 6 | Chat | 1 | chat-pane split with writer stream; their inline layout keeps working at every commit |
| 7 | Solver workspace | Stage strip, TheMark, thread sheet | None expected; it is the largest single screen, hence late |
| 8 | Study screens | Prototype round two | Design first, then convert |
| 9 | Draft workspace | Writer stream schedule | They adopt the prototyped shape with W1 to W5; we supply primitives and review |

## 4. What prototype round two still owes

Owed before their screens convert, all in the existing prototype file on the same URL:

- Study: deck review session and quiz runner in the system's card recipes.
- Dialogs and destructive flows: create class, rename, delete confirmations, move document.
- The mobile posture (ui-overhaul 3.6): bottom-shelf reach, hub tab strip, and the draft
  workspace's read-and-review mobile surface.
- Segmentation review and the solver's waiting-for-you state.

## 5. Per-screen conversion checklist

A screen is done when:

- Both modes render correctly; dark is the late-night palette, not an inversion.
- The contrast script passes for every pair the screen uses.
- Keyboard: full traversal, pen-ring focus visible on every control, existing shortcuts work.
- Reduced motion: drawings complete instantly; counters and penbars unaffected.
- Print stylesheet still produces the light-palette document it does today.
- Status renders as words; no icon-only, no color-only state anywhere on the screen.
- No em dash, no interface italics (lint gates from stage 0).
- Existing frontend tests pass; snapshots updated deliberately, not wholesale.
- The four data states (loading, empty, error, populated) all render in the new language,
  skeletons matching the new layout.

## 6. Explicitly retired

- The parchment palette, sage accent, and paper-grain overlay.
- Fraunces, DM Sans, and Source Serif 4 (faces; the type discipline that used them survives).
- The gold VERIFIED seal concept and verde antico dark marble (never shipped; recorded in the
  workshop history).
- Tally-mark progress, icon-only statuses, italic metadata, engraved small labels: all
  replaced per the design system's rules of use.

What is *not* retired: every behavioral guarantee the current interface makes. Honest
machinery, the keyboard map, four states, aria-live discipline, print correctness, and the
verification-first posture carry over untouched; Ex Libris is a new skin on the same spine.
