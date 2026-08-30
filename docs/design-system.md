# Lyra Design System

**Canonical, current reference.** This document describes the Ex Libris design system as the
shipped application implements it today. Every value is verified against
`frontend/src/styles/globals.css` (the single source of truth for colors, fonts, elevations,
easings, the print palette, and the reduced-motion policy), `frontend/src/app/layout.tsx`
(the font loads), the Ex Libris primitives, and the rule tests named in section 10. A value
here that disagrees with `globals.css` is a design-system defect, and it will not survive
long: `scripts/check_contrast.py` recomputes the contrast pairs from the stylesheet itself,
and `frontend/tests/ex-libris-rules.test.ts` gates the typography discipline.

**Provenance.** Ex Libris replaced the pre-Ex-Libris parchment system in August 2026. The
approved workshop brief that started it is preserved as a historical record in
[exlibris-design-system.md](exlibris-design-system.md), and the migration plan that governed
the transition is preserved in [exlibris-migration.md](exlibris-migration.md). Where the
brief and this document differ, this document governs: the shipped implementation is the
baseline, and the brief's section numbers are kept here so that the "design system section N"
references in code comments resolve against this page.

## 1. The idea

Lyra's interface is a historic university rendered as software: polished stone, engraved
nameplates, paper in the reading room. Over that institutional base rides the student's own
hand in colored ink, taking the materials over and making them theirs.

The split is the system. Everything follows from one rule:

**The printed layer is still; the hand layer is alive.** Everything engraved or printed never
moves and never performs. Everything the student chose or is doing right now renders in the
pen, and is the only thing that animates, always as drawing, never as sliding.

It is a power tool that happens to be beautiful, in that order. When beauty and usability
argue, usability wins.

## 2. Materials

Every element belongs to exactly one material. No element mixes materials.

| Material | Role | Reading room (light) | After hours (dark) |
| --- | --- | --- | --- |
| Stone | Structure: the canvas, the rail, the header | Pale honed marble `#e9e4d8` with baked veining (`--stone-texture`) | Recedes into deep warm black `#0f0d0a`; one faint lamp-glow pool at the top of the page (`--lamp`) is all that remains |
| Paper | Content: lists, conversation, documents, overlays | Ivory sheets `#faf7ee` set into the stone | Slightly lifted warm sheets `#171410` in the lamp light |
| Printed ink | Lyra's voice and all nominal state | Warm near-black `#28231a` | Warm off-white `#e7dfcf` |
| Gold | Engraving only: the dentil course, the laurel rule | Muted gold hairlines (`--gold`, `--gold-line`) | Becomes pale ivory; gold does not survive the dark |
| The pen | The student: their place, progress, in-flight work, and focus | Biro blue `#2440c0`, red pencil `#b23a2e` | Periwinkle `#a8b6f0`, salmon `#e2a191` |
| Verdigris | The Mark, machine verification, nothing else | Bronze-green `#2f7052` | Pastel mint `#9ccfae` |

Texture never sits behind running text. In light, the marble is one baked 600x600 SVG sheet,
alpha-keyed so it modulates the stone rather than tinting it, painted once on the body with
`background-attachment: fixed`. Paper sheets (`--bg-secondary`) are opaque on top. In dark
there is no texture at all: the lamp is the one atmospheric touch, and nothing else is added.

## 3. Color

### 3.1 Reading room (light)

Values from the `:root` block of `globals.css`.

| Token | Value | Role |
| --- | --- | --- |
| `--bg-primary` | `#e9e4d8` | Stone: the page and slab ground, and the rail |
| `--bg-secondary` | `#faf7ee` | Paper: content sheets, fields, popovers, dialogs |
| `--bg-tertiary` | `#efe9dc` | Quiet fill: hovers, chips, code blocks, the penbar track |
| `--border` | `#d3c9b2` | Rules and decorative borders |
| `--border-strong` | `#857a58` | Inputs, dividers, strong edges; clears 3:1 on both grounds |
| `--text-primary` | `#28231a` | Ink: the reading face |
| `--text-secondary` | `#645c4c` | Muted: secondary text and the eyebrow |
| `--text-tertiary` | `#695f4e` | Quiet metadata; still prints as a fact, still clears 4.5:1 |
| `--accent-primary` | `#a9c3a0` | Pastel sage plaque: primary buttons, the line-tab rule, the active rail row |
| `--accent-primary-hover` | `#9bb891` | The plaque's hover |
| `--accent-foreground` | `#1d3324` | Accent ink: text on the plaque |
| `--accent-surface` / `--accent-surface-foreground` | `#dce7d4` / `#1d3324` | Quiet sage surface; the sage course mark |
| `--accent-text` | `#48684b` | Ghost buttons and quiet accent text |
| `--accent-secondary` / `--accent-secondary-foreground` | `#dac9b4` / `#5a4b36` | Tan course mark; decorative only |
| `--accent-tertiary` / `--accent-tertiary-foreground` | `#e0cbbe` / `#6b4733` | Muted-clay course mark; decorative only |
| `--success-fill` / `--success-text` | `#dce7d4` / `#48684b` | Nominal-good status, distinct from the verdigris Mark |
| `--info-fill` / `--info-text` | `#dbe2e6` / `#3f5b62` | Informational status |
| `--danger-fill` | `#e7cfc7` | Destructive fill (soft salmon) |
| `--danger-foreground` | `#3a241e` | The paired label on the destructive fill |
| `--danger-text` | `#b23a2e` | The red pencil: printed warnings, invalid controls, inline errors |
| `--hand` | `#2440c0` | The pen: biro blue |
| `--hand-red` | `#b23a2e` | The red pencil |
| `--trust` | `#2f7052` | Verdigris: the Mark and machine verification, nothing else |
| `--hl` | `rgb(247 225 76 / 0.55)` | Highlighter wash; token defined, no shipped consumer yet (section 5) |
| `--gold` / `--gold-line` | `#a9863c` / `rgb(169 134 60 / 0.5)` | Engraved detail only |
| `--lamp` | `none` | The lamp is a dark-mode atmosphere; light has the marble |
| `--paper-sheet` | `#ffffff` | A rendered PDF page is white paper in both themes |
| `--focus-ring` | `var(--hand)` | Focus is the pen pointing at it |
| `--overlay` | `rgb(20 17 12 / 0.28)` | The only light overlay color |

### 3.2 After hours (dark)

Values from the `.dark` block of `globals.css`. Deep warm blacks, gentle contrast, pastel
emphasis only; nothing saturated, nothing glowing.

| Token | Value | Role |
| --- | --- | --- |
| `--bg-primary` | `#0f0d0a` | The room with the lights off |
| `--bg-secondary` | `#171410` | Content sheets, lifted in the lamp light |
| `--bg-tertiary` | `#221d16` | A slight lift for hovers and chips |
| `--border` / `--border-strong` | `#2c2820` / `#6b6449` | Rules; the strong edge still clears 3:1 |
| `--text-primary` / `--text-secondary` / `--text-tertiary` | `#e7dfcf` / `#9a917d` / `#8c826e` | Ink, muted, quiet metadata |
| `--accent-primary` / `--accent-primary-hover` | `#e6d69b` / `#efe1ae` | Butter: primary buttons, the line-tab rule |
| `--accent-foreground` | `#2a2410` | Accent ink on the butter plaque |
| `--accent-surface` / `--accent-surface-foreground` | `#2e2a1c` / `#e6d69b` | Quiet butter surface |
| `--accent-text` | `#d9c98c` | Quiet accent text |
| `--accent-secondary` / `--accent-secondary-foreground` | `#2a2419` / `#d8c3a0` | Tan course mark |
| `--accent-tertiary` / `--accent-tertiary-foreground` | `#2e2118` / `#e0b9a0` | Muted-clay course mark |
| `--success-fill` / `--success-text` | `#24291b` / `#bcc79e` | Nominal-good status |
| `--info-fill` / `--info-text` | `#1e2a2c` / `#a9c6ce` | Informational status |
| `--danger-fill` / `--danger-foreground` / `--danger-text` | `#3a2620` / `#f1e1db` / `#e2a191` | Destructive fill, paired label, pastel salmon |
| `--hand` / `--hand-red` | `#a8b6f0` / `#e2a191` | The periwinkle pen, the salmon pencil |
| `--trust` | `#9ccfae` | The pastel mint Mark |
| `--hl` | `rgb(233 217 155 / 0.22)` | The butter wash |
| `--gold` / `--gold-line` | `#d6ccb4` / `rgb(233 217 155 / 0.18)` | What was gold by day |
| `--lamp` | `radial-gradient(120% 62% at 50% -8%, rgb(233 217 155 / 0.05), transparent 62%)` | Dark mode's only atmosphere |
| `--paper-sheet` | `#ffffff` | A PDF page is white in both themes; paper is paper |
| `--focus-ring` | `var(--hand)` | The periwinkle pen |
| `--overlay` | `rgb(6 5 3 / 0.62)` | The dark overlay color |
| `--stone-texture` | `none` | The marble is a light-mode material |

### 3.3 Color rules

- **The nominal state is quiet print.** "Ready", "Indexed", and "Queued" render in muted ink;
  a screen where everything is fine reads as silence. Color marks exceptions (the red-pencil
  family) and the student's own ink.
- **Gold is engraved or earned, never functional.** The dentil course, the laurel rule. Never
  a button, never text that carries meaning, never in motion.
- **Verdigris is exclusive.** The trust color and the ring-and-check shape belong to machine
  verification alone, in either mode. Nothing else may borrow either; that exclusivity is what
  makes the Mark a logo of trust instead of an icon.
- **Sage is the only actionable accent.** Tan and muted clay are decorative fills only, always
  paired with their named foreground. Status and destructive surfaces use their own semantic
  pairs.
- **Every text pair clears 4.5:1 on its actual ground** - stone included, both modes - and
  boundaries and the focus ring clear 3:1. The recorded pairs are recomputed from
  `globals.css` by `scripts/check_contrast.py`; a value that drifts in the stylesheet fails
  the check rather than passing on a stale table.

### 3.4 Print palette

Export is the browser's print path. Both themes print on the reading-room palette so a
dark-mode reader does not get a page of ink or a light-on-light document: white page,
`#201c14` ink, `#4a4335` muted, `#5f5747` metadata, `#4d6f51` accent with white ink,
`#8a2f24` red pencil, and `#2f7052` trust. The student's ink and the Mark still print: a
solution's provenance is not decoration. The stone texture and the lamp do not. The full
behavior is in the Print section below.

### 3.5 The token bridge

`globals.css` owns the raw Lyra tokens, the shadcn aliases, and the Tailwind v4
`@theme inline` mappings. Components consume semantic utilities; they do not introduce
component-local hex values, a Tailwind configuration, or a second token file.

- The aliases keep shadcn primitives on Lyra paper: `--card` is `--bg-secondary`, `--muted`
  is `--bg-tertiary`, `--primary` is `--accent-primary`, `--input` is `--border-strong`,
  `--ring` is `--focus-ring`, and `--destructive` is `--danger-text`, so invalid-control
  borders and inline errors use the red pencil. The destructive Button variant does not use
  the alias; it explicitly uses `--danger-fill` and `--danger-foreground`.
- The rail is stone, not paper: `--sidebar` is `--bg-primary`, so the structure reads as the
  ground the sheets are set into.
- `@theme inline` exposes the semantic utilities (`bg-card`, `text-text-secondary`,
  `bg-bg-tertiary`, `border-border-strong`, ...), the signature colors (`text-hand`,
  `bg-trust`, `text-gold`, `bg-hl`, `bg-paper-sheet`), the elevation tokens as
  `shadow-sm`/`shadow-md`/`shadow-lg`, `bg-overlay`, the easings, and the font families.
- Highlight.js is local, not a GitHub theme import: code sits on `--bg-tertiary` in
  `--text-primary`; keywords use `--danger-text`; titles and tags use `--accent-primary`;
  literals and numbers use `--info-text`; strings use `--accent-tertiary-foreground`;
  comments use `--text-tertiary`.
- The draft editor (Milkdown Crepe) reads `--crepe-*` variables that are mapped onto the Lyra
  tokens rather than importing a Crepe color theme, so no foreign palette shows through.
- Selection is the house accent (a 24% wash of `--accent-primary`), and scrollbars are thin
  and warm, colored at 55% `--border-strong`.

## 4. Typography

`frontend/src/app/layout.tsx` loads four faces through `next/font/google`, each as a CSS
variable on `<html>` with `display: 'swap'` and the latin subset:

| Face | Variable | Loaded weights | Role |
| --- | --- | --- | --- |
| Cinzel | `--font-cinzel` | 600 | Inscription: nameplates only |
| EB Garamond | `--font-eb-garamond` | 400, 500, 600, each with italic | Print: everything read |
| Caveat | `--font-caveat` | 500 | The hand: the student's acts |
| JetBrains Mono | `--font-jetbrains-mono` | the variable font, full range | Code, and only code |

`globals.css` maps them onto the font tokens, with fallbacks behind every face:

- `--font-sans`: `--font-eb-garamond`, then `ui-serif, Georgia, serif` - the document face;
  `html` is set on it.
- `--font-heading`: `--font-cinzel`, then `ui-serif, Georgia, serif` - `h1` through `h3` are
  set on it with `tracking-tight`.
- `--font-mono`: `--font-jetbrains-mono`, then `ui-monospace, monospace`.
- `--font-ai-response`: the print face, for assistant reading surfaces.
- `--font-hand`: `--font-caveat`, then `ui-serif, cursive`.

### The inscription (Cinzel)

Engraving is for names, not navigation. Cinzel 600 is cut from Roman capitals, so a
nameplate is engraved rather than set: `.font-display` and `.font-wordmark` apply the incised
treatment (a light top edge and a soft shadow below, as if carved into the stone; in dark,
pale ivory, gently embossed, because gold and the light-stone bevel do not survive the
night). Nameplates only, 17px and up: the wordmark, class names, workspace titles. The
`Engraved` primitive wraps a nameplate in `.font-display`. Below 17px, plain letterspaced
caps do the work instead; scannability beats ceremony.

### The print (EB Garamond)

Print carries everything read. The interface runs at 14px; small uppercase labels run
through `.eyebrow` - 11.5px, weight 600, 0.18em tracking, set in the print face, in muted
ink. Numerals are tabular (`tabular-nums`) wherever they align.

The reading surfaces are one size larger, because a serif at the same nominal size reads
smaller: `.assistant-content` sets assistant prose at 17px on 1.65 leading, and emphasis is
drawn in a real weight (600) rather than synthesized. Headings inside a reply set in EB
Garamond 500 at 18/16/14px with tight tracking. Tables inside a reply keep the interface
face (`--font-sans`) at 14px: tabular data is not prose. Code in a reply is `--font-mono` on
`--bg-tertiary`; KaTeX retains its own math fonts. Quieter variants exist for the margin
material: `.math-text` inherits the surrounding size for typeset problem statements, and
`.reasoning-body` settles a thought at 15px on 1.6 in muted ink so an expanded trace never
competes with the reply. The loaded 500 weight exists for these reading-surface headings.

### The hand (Caveat)

Caveat 500 is loaded and mapped to `--font-hand` (and the `font-hand` utility), but no
shipped interface text currently sets the hand face. The student's acts render today as pen
strokes and hand-colored print: the underlines, the penbar, the active status word, and
keyboard focus. When a hand-written text surface lands, this is the face it uses; the
jurisdiction rules in section 5 already apply to it.

### Code (JetBrains Mono)

Mono is for actual code, never for status or numerals: `--font-mono` on code blocks and
inline code, in the reading surfaces and the draft editor alike.

### Type rules

- **No italics in the interface.** Italics are reserved for mathematics, where slanted
  variables are notation students already read (KaTeX renders its own; the EB Garamond
  italic instance exists solely as a fallback where KaTeX is not in play). If text needs
  emphasis, change its size or weight, never its slant. `frontend/tests/ex-libris-rules.test.ts`
  fails the build if a `className` carries the `italic` utility.
- **No em dashes, anywhere, in any interface copy, ever.** The same test fails the build if
  U+2014 appears in shipped strings. Use " · " or restructure.
- Hierarchy comes from size, weight, and letterspaced caps, not from a heavier display face.

## 5. The hand

The pen renders only what the student did, chose, or must do; the machine never writes in
the student's pen.

The pen's shipped jurisdiction:

- The active rail row and its 2px accent marker.
- The hand underlines under the active tab, the current nav item, and hovered rows.
- The penbar's progress stroke.
- The `active` status word: the student is doing this right now.
- Keyboard focus: `--focus-ring` is the hand itself.
- The chosen answer style: the traveling thumb on the Guide/Show control.

Not the pen's: counts, timestamps, statuses of fact, verdicts, file sizes. All facts print.

The brief's negotiated exception is the **highlighter**: Lyra may swipe one takeaway phrase
per answer so a re-reader finds the point at a glance. The budget is hard - one per answer,
never inside mathematics, swipes in exactly once on first reveal. The token is defined
(`--hl`: a 55% highlighter wash in light, a 22% butter wash in dark) but the app has no
shipped consumer yet; the swipe is not live.

## 6. Signature elements

- **The Mark.** `TheMark` (`components/ex-libris/the-mark.tsx`): a bare ring and check in
  verdigris beside the word "Verified" and a plain detail ("SymPy, 2 calls"). It appears only
  when deterministic verification actually passed; a non-passing verdict must not render it
  and stays a printed word. It fades in at 150ms (`.the-mark-reveal`); trust does not
  perform, so there is no rotation and no gold.
- **The penbar.** `Penbar`: progress is a printed track (a 6px quiet-fill rail with a 1px
  border) filled by a single pen stroke, with plain numerals beside it ("4 / 8") in tabular
  print. The stroke's length is data, not animation: the width is set from the fraction and a
  400ms `--ease-draw` transition eases a real change, while motion-off and reduced-motion
  remove the easing without ever moving the bar. It carries `role="progressbar"` with
  `aria-valuenow`, and the tone is the hand by default, verdigris only where the Mark owns
  it.
- **The hand underlines.** `HandUnderline`: a slightly wobbled pen stroke, 2px, under the
  word it marks. It draws in at 280ms (`--ease-draw`, `pathLength=1` so the dash geometry is
  word-independent), inherits `--hand` unless the caller recolors it (`text-hand-red` for a
  red-pencil flag), and takes `animate={false}` where a persistent underline, not a reveal,
  is wanted. Implementation note that has bitten twice: an absolutely positioned SVG keeps
  its intrinsic width, so this one carries an explicit `width:100%` and stretches to the
  word.
- **The engraved lintel.** Screens open under the app header's lintel: the breadcrumb cut
  into the stone with the dentil course beneath. `Dentil` - 4px teeth at 40% gold on an 8px
  period, `aria-hidden`, decorative and still. `LaurelRule` - a hairline broken at center by
  a small laurel, the detail that sits under a nameplate. `Engraved` - the nameplate
  treatment itself, for names 17px and up.
- **The Lyra mark.** `LyraMark` (`components/chat/lyra-mark.tsx`): Vega, the lyre's bright
  star, held at the center of a broken orbit that carries two smaller stars. It names the app
  (the wordmark's 24px mark), appears on the assistant's accent disc (`LyraAvatar`) beside
  every reply and on the empty conversation, and marks the model's work: `thinking` sets the
  orbit turning (7s linear), the primary star breathing (2.4s), and the companions twinkling
  off-phase (1.9s and 2.7s). All three stop under reduced motion, leaving the static mark.
- **The lamp.** Dark mode's only atmosphere: one faint butter radial at the top of the page
  (`--lamp`). No marble, no texture behind anything at night.
- **The fleuron.** `Asterism` (`components/ui/asterism.tsx`): three concave four-point stars
  drawn from the same star path as the Lyra mark, the middle one raised. It opens empty
  states and section breaks the way a fleuron opens a chapter, at `currentColor` - the empty
  conversation sets it at `text-border-strong`.

Budgets the brief defines that the app has not shipped: the slab's 1px gold-line inner frame,
and the group-sheet recipe for dense lists. Neither has a shipped implementation, and this
document does not describe either as live.

## 7. Motion and reduced motion

Stone never moves. Paper changes instantly or fades. The pen draws, at 280 to 500ms,
ease-out. The Mark fades in at 150ms. Nothing slides sideways, nothing zooms or bounces, no
parallax, no shimmer on content, no gold in motion.

| Effect | Where | Timing |
| --- | --- | --- |
| Arrival reveal (`.lyra-reveal`, `Reveal`) | List rows: the class ledger, the segmentation review | 250ms, 8px rise with fade, `--ease-gentle`, `both` fill; delay capped at 200ms; at most once per session per `once` id |
| Word streaming (`[data-stream-word]`) | Assistant replies as they arrive | 180ms per word, 2px rise, `--ease-gentle` |
| Label enter plus shimmer (`.lyra-label-enter`) | The thinking label | 240ms enter plus a 2.6s gradient sweep clipped to the glyphs |
| The mark thinking (`.lyra-mark-thinking`) | `LyraMark thinking` | Orbit 7s linear, breathe 2.4s, twinkle 1.9s and 2.7s |
| The hand underlines (`.hand-underline`) | `HandUnderline` | 280ms draw-in, `--ease-draw` |
| The penbar (`.penbar-fill`) | `Penbar` | 400ms width transition, `--ease-draw` |
| The Mark (`.the-mark-reveal`) | `TheMark` | 150ms opacity |
| Skeletons and spinners | `Skeleton`, `Spinner`, the toast loader | `motion-safe` pulse and spin on the quiet fill |

The easings are tokens: `--ease-gentle` `cubic-bezier(0.25, 0.1, 0.3, 1)`,
`--ease-spring` `cubic-bezier(0.34, 1.56, 0.64, 1)`, and `--ease-draw`
`cubic-bezier(0.33, 0, 0.2, 1)` - the pen's own timing: draws in, never slides.

`Reveal` in `frontend/src/components/ui/reveal.tsx` is the only shared arrival-reveal
utility. It is a CSS animation deliberately, not a script-driven one: the JS version could
stall mid-flight and strand content near opacity 0, and a compositor animation with `both`
fill always ends visible. Delay is capped at 200ms so a long list never spends a second
cascading in; list rows stagger through that cap in 50ms steps. The `once` id makes a
cascade play at most once per session: motion explains that something arrived, and replaying
it every time the user navigates back to a list they have already seen explains nothing and
reads as latency.

Honest machinery inherits unchanged: no timer-driven progress, elapsed counters really
count, and stage verbs come from real backend events.

### Reduced motion

Under `prefers-reduced-motion: reduce`, every animation and transition collapses to 0.01ms,
and then the specifics hold:

- Stream words render at full opacity immediately.
- Drawings land complete, not mid-stroke: underlines sit at `stroke-dashoffset: 0`, the Mark
  at full opacity.
- The penbar drops its transition but keeps its width: the stroke lands exactly where the
  data puts it.
- The shimmer is removed outright rather than paused, because freezing a text-clipped
  gradient would leave the label painted on a moving slice of it.
- Spinners and skeletons use `motion-safe` animation, so they stay visible without rotating
  or pulsing.

## 8. Layout and density

### Shape and elevation

| Token | Value |
| --- | --- |
| `--radius-sm` | 5px |
| `--radius-md` | 8px (this is `--radius`, the shadcn base) |
| `--radius-lg` | 14px |
| `--elevation-sm` | Light `0 2px 8px rgb(40 35 26 / 0.05)`; dark `0 2px 8px rgb(0 0 0 / 0.45)` plus a 1px lifted-paper inset hairline |
| `--elevation-md` | Light `0 12px 30px rgb(40 35 26 / 0.08)`; dark `0 12px 30px rgb(0 0 0 / 0.55)` plus the hairline |
| `--elevation-lg` | Light `0 24px 60px rgb(40 35 26 / 0.12)`; dark `0 24px 60px rgb(0 0 0 / 0.66)` plus the hairline |
| `--pane-control-row` | 3.75rem |

A drop shadow is a light-theme device: on a dark canvas it darkens dark and reads as
nothing, so dark carries a second, inset hairline of lifted paper along the top of each
elevated surface instead. Full rounding is reserved for avatars, status dots, switches, and
compact metadata badges. `--pane-control-row` sizes the action bars of the two workbench
panes from one value so their control rows part the same line across the seam.

Spacing runs on Tailwind's built-in 4px scale; there are no parallel `--space-*` variables.

### The shell

The application is one continuous surface, flush to the window (`AppShell`,
`components/layout/app-shell.tsx`):

- **The rail** is `Sidebar variant="sidebar" collapsible="offcanvas"` at
  `--sidebar-width: 260px`, stone. It is never `inset`: the inset variant floats the whole
  app inside a rounded, bordered, shadowed panel - a card, the largest one in the product -
  and the app is the content, not the card. Closed, the rail slides off-canvas; the
  preference persists at `lyra-sidebar-open`.
- **The header** is a 56px lintel on `bg-background/85` with a soft blur, carrying the
  breadcrumb, the route's portaled title and actions, the privacy readout, and the class
  profile button.
- **`main#main-content`** is the one scroll container below the header, so the rail and
  header stay put on long routes; pages set inside it at a 1320px cap with 16px padding
  (24px from 768px up).
- **The skip link** precedes the shell and targets `main#main-content`.
- **Below 768px** the rail becomes a sheet; **below 640px** the bottom shelf takes over: a
  64px paper bar, clear of the safe area, holding Classes and Settings.
- **Workspace routes** claim the whole window with `useFullBleed` and put their title and
  actions in the header through the `HeaderCrumb`/`HeaderActions` slots rather than spending
  another row on a title of their own. The draft workspace goes further with
  `useImmersiveChrome`: the rail and header go, and the route - not the student - gets them
  back on unmount. Immersive mode collapses the rail without touching the stored
  preference.

### Navigation

The rail is navigation, never verbs. Wordmark first: the Lyra mark at 24px beside "Lyra" set
in `.font-wordmark`, never a stock icon. Under the `Classes` eyebrow, one row per class with
its `CourseMark`; the open class expands to five recent conversations and four pieces of
work (solutions, drafts, study), and every other row stays a single line. An `Archived`
section folds the archived classes behind a count and a restore action. At the foot of the
rail, the mode quick-toggle - two states, one tap, the label naming the mode it switches to:
"Reading room" and "After hours" - and Settings. Actions live in the screens that own them;
one primary placement per verb per screen.

The header crumb names the page: `Classes / [code] name`, with the course code traveling
with the name whether or not the crumb links anywhere. Ancestor crumbs fold away below 640px:
three crumbs at 375px truncates every one of them, and the current page is the one worth
reading. The privacy readout stays on the lintel on every route, and a class route adds its
code and a Profile button that opens the class profile sheet.

### Screens

- **The class index** is a ledger, not a card grid: one class per line under hairlines in a
  centered `max-w-3xl` measure, name in the print face, counts and recency kept to the right
  margin, and a final quiet "New class" line closing the list. New class lives nowhere else.
- **The conversation** reads at an 860px measure. A question and the answer under it are one
  turn and sit 20px apart; the next question opens at 44px. Even spacing throughout is what
  made a transcript read as an undifferentiated stack of blocks.
- **The workbench** (the class chat) is one surface split into panes. Below 1024px,
  Chat/Documents/Agent take line tabs, Chat the default. On desktop, the conversation keeps
  the window, documents open as a 340px right column (380px from 1280px up), and the agent
  as a 420px column; only one opens at a time. The columns open and close per class and
  start closed.
- **The source pane** (solutions): the rendered page lies on a sunken desk tone
  (`bg-muted/40`) with `shadow-md`, so the student's sheet reads as a sheet; the page itself
  is white paper (`--paper-sheet`) with a 3px radius in both themes.
- **Settings and setup screens** are hairline-topped raised-paper sections on the page's own
  paper (Tutor model, Writer, Privacy, Appearance), not cards. Rows hold a 44px minimum
  target, and the Appearance rows carry a token swatch beside the choice.
- **Empty states** open on paper: the house fleuron, a nameplate title, a sentence, one
  verb. The empty conversation is a title page, not a dashboard.
- Density is designed, not accidental: compact lists use hairlines and aligned tabular
  columns, not card grids.

## 9. Rules of use

The distilled judgment calls, each one a scar from a workshop round:

1. Show only what a student acts on; everything else is Settings or a tooltip.
2. The nominal state is quiet print; color marks exceptions and the student's ink.
3. Progress is a filled track plus numerals; nothing the user counts.
4. Engraving is for names; below 17px, plain caps.
5. No italics in the interface; no em dashes anywhere.
6. One highlighter swipe per answer, never in math - the budget; the swipe itself has not
   shipped yet (section 5).
7. Gold is engraved or earned; the Mark and its color are exclusive to verification.
8. Every control is reachable and visibly focused by keyboard (section 10).
9. Density is designed: the ledger, the line tabs, the columnar rows - never a card grid.
10. Every text pair clears 4.5:1 on its actual ground, both modes, verified by script.

## 10. Accessibility contract

- **Focus is the pen pointing at it**: a 2px `--focus-ring` ring with a 2px offset on every
  interactive element, both modes, always visible under `:focus-visible`. No control removes
  focus without replacing it.
- **Contrast** per section 3.3: 4.5:1 for anything read as text on its actual ground - paper
  and stone both counted - and 3:1 for boundaries and the focus ring, both modes. The
  contract is scripted, not tabulated.
- **Status is never color-only and never icon-only**: words, always (`StatusWord`). An
  optional leading glyph may accompany the word for scanning; it is `aria-hidden` and never
  appears alone.
- **Reduced motion** per section 7.
- **Four data states** - loading, empty, error, populated - render on every screen, with
  skeletons matching the final layout.
- **Semantics**: interactive elements are real buttons and inputs; Radix overlays trap and
  restore focus; the answer-style switch carries `role="group"` with `aria-pressed` on each
  segment; the penbar carries `role="progressbar"`; the workbench Documents/Agent controls
  carry `aria-expanded` and `aria-controls`; `aria-current` marks the current page crumb and
  the active bottom-shelf item; a header that has slid out of the way is `inert`, not merely
  hidden, so focus cannot tab into something nobody can see.
- **Keyboard**: the skip link is first and Tab reaches every control. Escape dismisses
  overlays and form Enter behavior stays intact. The application shortcut map is
  Cmd/Ctrl+K (focus the composer - the one shortcut honored while typing in a field),
  Cmd/Ctrl+, (settings), and Cmd/Ctrl+B (toggle the rail, in the sidebar primitive).

### Automated checks

- `frontend/tests/ex-libris-rules.test.ts`: no em dashes and no interface italics across
  `frontend/src`, with comments stripped so guidance about the rules is not mistaken for a
  breach.
- `scripts/check_contrast.py`: the section 3.3 contract, recomputed from `globals.css` for
  both modes.
- `frontend/e2e/acceptance/accessibility.spec.ts`: keyboard, focus, and error-announcement
  assertions against the real stack for the highest-risk flows - tab reachability on the
  class index, Enter sending a message and returning focus to the composer, and announced
  errors rather than silent failures.

## 11. Assets and implementation notes

- **Fonts** load through `next/font` in `frontend/src/app/layout.tsx` exactly as section 4
  records them, each as a CSS variable on `<html>`. EB Garamond's italic exists solely as a
  mathematics fallback where KaTeX is not in play.
- **The marble** is a baked SVG texture - fractal-noise clouds plus a band-passed veining,
  alpha-keyed - recorded as one 600x600 data URI in `--stone-texture`, light mode only,
  painted once on the body so the veining never repeats per element. Regenerate it rather
  than hand-editing it.
- **Underline and ring SVGs** are inline, stroked from the hand token with a dasharray
  draw-in, and carry an explicit `width:100%` (section 6).
- **Where the primitives live**: `frontend/src/components/ex-libris/` for `StatusWord`,
  `HandUnderline`, `Penbar`, `TheMark`, and the lintel family (`Dentil`, `LaurelRule`,
  `Engraved`); `frontend/src/components/chat/lyra-mark.tsx` for `LyraMark` and `LyraAvatar`;
  `frontend/src/components/ui/asterism.tsx` for `Asterism`;
  `frontend/src/components/classes/course-mark.tsx` for `CourseMark`;
  `frontend/src/components/ui/reveal.tsx` for `Reveal`.
- **The approved prototype** remains the living reference for the workshop's spacing and
  hover behavior; its location and history are recorded in the historical brief's section 11.

## Component recipes

- **Buttons** (`components/ui/button.tsx`): default is the accent plaque - solid sage (butter
  in dark) with the `--accent-foreground` label; outline is paper with a strong edge;
  secondary is quiet paper; ghost is transparent; destructive is `--danger-fill` with
  `--danger-foreground` and a red-pencil focus ring; link is accent text. Primary actions
  are 44px (`size="lg"`), standard actions and default icon actions 40px, dense controls
  28px and 24px (`sm`/`xs`), and icon sizes run 44/40/28/24. Invalid states take the
  destructive border and ring.
- **Fields**: paper fill, `--border-strong` edge, native labels and validation markup, a 2px
  focus ring with a 2px offset. Invalid controls use the destructive color.
- **Switches**: 44x24px by default (28x16 at `sm`); the thumb is bordered paper on a quiet
  fill, and its move is `motion-safe`.
- **Tabs**: workspace panes use `TabsList variant="line"`; the active state is a 2px accent
  rule, never a filled rounded segment. The default variant - a quiet muted track with the
  active segment on paper - is for mode switches that do not navigate anywhere, such as the
  Guide/Show answer-style control: a rounded-full track with a traveling thumb,
  `role="group"` labeled "Answer style", `aria-pressed` on the active segment. That does not
  contradict the line rule: tabs navigate between panes and take the rule, while this
  changes how the next answer is written and has no pane rule to sit on once it lives in the
  header.
- **Badges**: pills are limited to compact status and scope metadata.
- **Course marks**: the mark is the code's subject prefix (`ECE 203` marks as `ECE`),
  falling back to the name's initials when a class has no code - never per-word initials,
  which would render `ECE 203` as `E2` and collide across a department. The tone is a
  deterministic sage/tan/clay mapping keyed by class id, so a class keeps the same mark
  everywhere it appears, and each tone is used only with its paired foreground. The mark is
  rectangular and `aria-hidden`; the class name is always beside it.
- **Overlays**: AlertDialog, Dialog, and Sheet overlays sit on `bg-overlay`; the content
  takes paper, border, and semantic elevation. Sonner toasts carry the paper treatment and
  a `motion-safe` loader.
- **Empty**: the `Empty` primitives (Header, Media, Title, Description, Content, Action)
  with the fleuron opening; the empty conversation is a title page - fleuron, nameplate,
  sentence, one verb, suggestions reading as a contents list.
- **The composer**: one raised writing well - `rounded-2xl` paper on the canvas with
  `shadow-sm`, an accent border and `shadow-md` on focus - with the send control riding the
  last line of type at 36px, round. The hint ("Enter sends") sits below the well and leaves
  after the first message; `--pane-control-row` keeps its control row level with the
  documents dropzone across the seam. Above it, where the conversation ends at the well, the
  content dissolves into the canvas over the last 40px rather than being sliced by a hard
  edge: the one sanctioned gradient, the only thing saying the text continues.

## Print

Export is the browser's print path with a stylesheet, not a backend renderer: KaTeX already
typesets the mathematics correctly in the page, and a Python PDF library would mean
re-solving math typesetting to do it worse. The rule the section exists to hold: an exported
document that hides a failed check would be worse than no export. Nothing about a verdict, a
refutation, or a provenance line is dropped for tidiness.

- Both themes print on the reading-room palette (section 3.4); the stone texture and the lamp
  are screen atmosphere and do not print.
- Application chrome carries no information on paper: the rail, the header, the resizable
  handles, and any toast on screen are removed rather than shrunk.
- Every scrolling region becomes its full height: a printed page cannot be scrolled, so a
  clipped region would silently drop the end of a solution.
- Every problem prints, whatever the accordion was showing on screen.
- A step and a figure are the units a reader follows, so they are kept whole
  (`break-inside: avoid`); a figure is capped at 9cm so a tall diagram cannot claim a page
  to itself, and it prints with its colors because it is a picture, not decoration.
- The draft workspace prints the document, not the desk: the editor's chrome and clipping
  go, and the suggestion block - never document content - does not print.

## Implementation rules

1. `frontend/src/styles/globals.css` remains the one source of truth for colors, fonts,
   elevations, easings, global syntax styling, the print palette, and the reduced-motion
   policy.
2. Do not add a Tailwind config, a token file, an external theme package, a visual-registry
   dependency, or a component-local hex color.
3. Do not add a second atmosphere (light: the marble; dark: the lamp), no gradients other
   than the composer's scrim and the lamp, no glows, no texture image assets beyond the
   baked marble, no scroll-triggered reveals, and no simulated state.
4. Do not replace Radix behavior, the existing keyboard paths, the four data states, or the
   API behavior, hooks, or schema merely to restyle a route.
5. Keep Light, System, and Dark coherent. A documentation value or contrast claim that
   differs from `globals.css` is a design-system defect.
6. The typography discipline is enforced by test (section 10): no em dashes, no interface
   italics, inscription only at 17px and up.
