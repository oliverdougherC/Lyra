# Ex Libris: The Lyra Design System (Approved Brief)

> **Status (2026-08-29): historical reference.** This document is the approved Ex Libris
> design brief, workshopped against a live interactive prototype. The canonical, current
> design-system reference is [design-system.md](design-system.md), which describes the
> shipped implementation; where this brief and the shipped system differ, the shipped
> system governs new work. This document is kept for provenance: the seven workshop
> rounds, the approved prototype, and the rules of use distilled from them. The migration
> this brief governed is recorded in
> [exlibris-migration.md](exlibris-migration.md), and this brief's section numbers are
> preserved in design-system.md so that "design system section N" comments in code
> resolve against the canonical document.
>
> Where the shipped system diverges from this brief: the content column is 1320px, not
> 1220px; EB Garamond also loads 500, which reading-surface headings set at; the quiet
> accent text is `#48684b` in light; the rail carries recent work and an archived section
> beyond navigation; the mobile posture ships as a fixed bottom shelf below 640px; the
> Lyra mark (Vega) ships with a thinking animation; and the highlighter swipe, the gold
> slab frame, and the group-sheet recipe remain defined budgets without shipped
> implementations.

Approved 2026-08-07 after seven workshop rounds against a live interactive prototype. The
prototype is the visual reference for the workshop's spacing and hover behavior:
https://claude.ai/code/artifact/caffc538-0960-4bf2-af4f-b8bce9c0b500. The earlier exploration
page (Bench, Marginalia, and the first three Ex Libris drafts) survives in the workshop
artifact's version history; note that its final revision still shows the pre-correction dark
palette. The pre-Ex-Libris parchment system this one superseded is historical; its
retirement is recorded in exlibris-migration.md section 6.

## 1. The idea

Lyra's interface is a historic university rendered as software: polished stone, engraved
nameplates, paper in the reading room. Over that institutional base rides the student's own
hand in colored ink, taking the materials over and making them theirs. This is the product
thesis drawn as a picture: Lyra is not the syllabus, it is the student's copy of the syllabus.

The split is the system. Everything follows from one rule:

**The printed layer is still; the hand layer is alive.** Everything engraved or printed never
moves and never performs. Everything the student chose or is doing right now renders in pen,
and is the only thing that animates, always as drawing, never as sliding.

It is a power tool that happens to be beautiful, in that order. When beauty and usability
argue, usability wins, and the seven rounds of this system's history are mostly a record of
that argument being settled correctly (see section 9).

## 2. Materials

Every element belongs to exactly one material. No element mixes materials.

| Material | Role | Light ("Reading room") | Dark ("After hours") |
| --- | --- | --- | --- |
| Stone | Structure: chrome, headers, navigation | Pale honed marble, baked veining | Recedes into deep warm black; a faint lamp-glow pool at the top of the page is all that remains |
| Paper | Content: lists, conversation, documents | Ivory sheets set into the stone | Slightly lifted warm panels in the lamp light |
| Printed ink | Lyra's voice and all nominal state | Warm near-black | Warm off-white |
| Gold | Engraving only: frames, dentil, laurel | Muted gold hairlines | Becomes pale ivory; gold does not survive the dark |
| The pen | The student: notes, place, progress, focus | Biro blue, red pencil, highlighter | Periwinkle, salmon, butter wash |
| Verdigris | The Mark, machine verification, nothing else | Bronze-green | Pastel mint |

## 3. Color

### 3.1 Reading room (light)

| Token | Value | Role |
| --- | --- | --- |
| stone | #E9E4D8 + marble texture | Page and slab ground |
| paper | #FAF7EE | Content sheets |
| ink | #28231A | Text |
| muted | #645C4C | Secondary text; clears 4.5:1 on paper and stone |
| line | #D3C9B2 | Rules and borders |
| accent fill | #A9C3A0 | Primary buttons, selected segment; pastel sage |
| accent ink | #1D3324 | Text on accent fill |
| accent text | #4D6F51 | Ghost buttons, quiet accent text |
| gold | #A9863C, lines at 50% | Engraved detail only |
| hand | #2440C0 | The pen |
| red pencil | #B23A2E | The pen, urgent; also printed warnings |
| trust | #2F7052 | The Mark, exclusively |
| highlighter | #F7E14C at 55% | The takeaway wash |

### 3.2 After hours (dark)

Optimized for late night: deep warm blacks, gentle contrast, pastel emphasis only. Nothing
saturated, nothing glowing.

| Token | Value | Role |
| --- | --- | --- |
| body | #0F0D0A | The room with the lights off |
| lamp | radial rgba(233,217,155,.05) at top | The one atmospheric touch; replaces marble |
| slab | #14110C | Screen surfaces |
| paper | #171410 | Content sheets |
| ink | #E7DFCF | Text |
| muted | #9A917D | Secondary text |
| line | #2C2820 | Rules and borders |
| accent fill | #E6D69B | Butter; primary buttons, selected segment |
| accent ink | #2A2410 | Text on accent fill |
| accent text | #D9C98C | Ghost buttons, quiet accent text |
| inscription | #D6CCB4, lines rgba(233,217,155,.18) | What was gold by day |
| hand | #A8B6F0 | Periwinkle pen |
| red pencil | #E2A191 | Pastel salmon |
| trust | #9CCFAE | Pastel mint Mark |
| highlighter | rgba(233,217,155,.22) | Butter wash |

### 3.3 Color rules

- **The nominal state is quiet print.** "Ready" renders in muted ink. Color marks exceptions
  (red family) and the student's own ink (hand family). A screen where everything is fine
  reads as silence.
- **Gold is engraved or earned, never functional.** Frames, the dentil course, the laurel.
  Never a button, never text that carries meaning, never in motion.
- **Verdigris is exclusive.** The trust color and the ring-and-check shape belong to machine
  verification alone. Nothing else may borrow either, in either mode. This is what makes the
  Mark a logo of trust instead of an icon.
- **Every text pair clears 4.5:1 on its actual ground**, stone included, both modes. The
  Phase 1 contrast script extends to these pairs and re-runs whenever a token moves.

## 4. Typography

Three voices, strict jurisdictions. Hierarchy comes from size, weight, and letterspaced caps.

| Voice | Face | Jurisdiction |
| --- | --- | --- |
| Inscription | Cinzel 600 | Nameplates only, 17px and up: the wordmark, class names, workspace titles. Carved treatment: incised text-shadow on light stone; pale ivory, gently embossed, in the dark |
| Print | EB Garamond 400 and 600 | Everything read. Body at 15 to 16.5px; labels as 11.5px letterspaced caps (0.18em); numerals tabular wherever they align |
| The hand | Caveat 500 | The student's acts only, 17 to 20px |

Rules:

- **No italics in the interface.** Italics are reserved for mathematics, where slanted
  variables are notation students already read. If text needs emphasis, change its size or
  weight, not its slant.
- **Engraving is for names, not navigation.** Below 17px, plain letterspaced caps;
  scannability beats ceremony.
- Code renders in JetBrains Mono; mono is for actual code, never for status or numerals.
- Reading measure 65ch; assistant prose 15.5px minimum at 1.6 leading.
- **No em dashes, anywhere, in any interface copy, ever.** The prototype build fails if one
  appears; the real build should too.

## 5. The hand

The pen renders only what the student did, chose, or must do; the machine never writes in the
student's pen, with one negotiated exception.

The pen's jurisdiction: the active tab and nav underline (their place), margin notes, the
progress stroke (their assignment filling in), keyboard focus (section 10), the chosen depth
of a pass, red-pencil flags on what needs them.

Not the pen's: counts, timestamps, statuses, verdicts, file sizes; all facts print.

The exception is the **highlighter**: Lyra may swipe one takeaway phrase per answer so a
re-reader finds the point at a glance. Budget is hard: one per answer, never inside
mathematics, swipes in exactly once on first reveal.

## 6. Signature elements

- **The Mark.** A bare ring and check in verdigris beside the word Verified and a plain
  detail ("SymPy, 2 calls"). Appears only when deterministic verification passed. Fades in at
  150ms. No rotation, no gold, no theatrics: its authority is exclusivity.
- **The penbar.** Progress is a printed track filled by a single pen stroke with plain
  numerals beside it ("4 / 8"). Nothing the user has to count. The stroke's length is data,
  not animation: motion-off and reduced-motion must never alter it.
- **Hand underlines.** Active tabs, nav, and row hovers take a slightly wobbled pen stroke
  that draws in at 280ms and always matches the exact width of the word it underlines,
  excluding count superscripts. Implementation note that has bitten twice: an absolutely
  positioned SVG keeps its intrinsic width; every underline SVG needs explicit width:100%.
- **The engraved lintel.** Screens open with the wordmark or crumb cut into stone, a dentil
  course beneath (4px teeth at 40% gold-line), and, under nameplates, a hairline rule with a
  small laurel at center.
- **The gold frame.** Slabs carry a 1px gold-line inner frame inset 6px. Two gilded moments
  per screen maximum: the frame plus one earned mark.
- **The lamp.** Dark mode's only atmosphere: one faint butter radial at the top of the page.
  No marble, no texture behind anything at night.

## 7. Motion

- Stone never moves. Paper changes instantly or in a 150ms fade.
- The pen draws: underlines, circles, the progress stroke, at 280 to 500ms, ease-out.
- The Mark fades in at 150ms. Trust doesn't perform.
- Honest machinery inherits unchanged from ui-phase-1.md: no timer-driven progress, elapsed
  counters really count, stage verbs come from real backend events.
- Never: sliding panels, parallax, shimmer, bounces, gold in motion, anything ink couldn't do.
- prefers-reduced-motion: all drawing completes instantly (dashoffset 0), reveals render in
  final state, counters still tick (they are information, not decoration), and data strokes
  like the penbar are untouched.

## 8. Layout and density

- **The rail is navigation, never verbs.** Wordmark, Home, classes with recent destinations,
  Settings, the mode toggle. Actions live in the screens that own them; one primary placement
  per verb per screen.
- **Sheets on stone.** Structure is slab; content is paper sheets inset in it. Texture never
  sits behind running text.
- **Screen titles are plain glanceable nouns.** Home, Chat, Documents. No course code under a
  class name, no activity timestamp, no document count in a header; show only what a student
  acts on. Model names live in Settings and appear nowhere else.
- **The group-sheet recipe** is the template for every dense list (documents, solution sets,
  decks, sessions): one paper sheet per group, a ruled header in full ink with a numeral
  count, then columnar single-line rows (name in full ink; size and status in aligned columns
  with tabular figures) at 32 to 36px. Scanning is vertical.
- Airy screens (hub overview, chat) run rows at 40 to 44px with a one-line printed sub.
- Content column max 1220px; wide content scrolls in its own container.

## 9. Rules of use: the power-user contract

The distilled judgment calls, each one a scar from a workshop round:

1. Show only what a student acts on; everything else is Settings or a tooltip.
2. The nominal state is quiet print; color marks exceptions and the student's ink.
3. Progress is a filled track plus numerals; nothing the user counts, nothing at odd angles.
4. Engraving is for names; below 17px use plain caps.
5. No italics in the interface; no em dashes anywhere.
6. One highlighter swipe per answer; never in math.
7. Two gilded moments per screen; the Mark and its color are exclusive to verification.
8. Every control is reachable and visibly focused by keyboard (section 10).
9. Density is designed, not accidental: dense lists use the group-sheet recipe.
10. All text clears 4.5:1 on its actual ground, both modes, verified by script.

## 10. Accessibility contract

- **Focus is the pen pointing at it:** a 2px hand-colored ring, offset 2px, on every
  interactive element, both modes, always visible under :focus-visible.
- Contrast per section 3.3, scripted.
- Reduced motion per section 7.
- Interactive elements are real buttons and inputs; switches are flex:none so no layout can
  push the knob outside its track.
- Status is never color-only and never icon-only: words, always.
- The keyboard map, four data states, print stylesheet, and aria-live discipline inherit
  unchanged from the Phase 1 contract (ui-overhaul.md section 4 keeps the full list).

## 11. Assets and implementation notes

- **Fonts:** Cinzel 600, EB Garamond 400/400i/600, Caveat 500, plus JetBrains Mono for code.
  In the app these load through next/font exactly as the current faces do. EB Garamond
  italic exists solely for mathematics fallback where KaTeX is not in play.
- **Marble:** a baked SVG texture, light mode only: fractal-noise clouds plus two band-passed
  turbulence vein layers, roughly 1KB. The generator script and final parameters live with
  the prototype source (session scratchpad, marble-final.json); regenerate rather than
  hand-edit, and bake one asset so veining does not repeat per element.
- **Underline and ring SVGs:** inline, stroke from the hand token, dasharray draw-in;
  explicit width:100% (see section 6).
- The prototype (single file, hash-routed) is the living reference for spacing, exact hover
  behavior, and both palettes; view source before re-deriving anything from prose.
