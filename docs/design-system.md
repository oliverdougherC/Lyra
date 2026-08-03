# Lyra Design System

## Core direction

Lyra is a light-first, local-study workspace. Its material language is parchment canvas, raised paper,
warm rules, espresso type, muted terracotta, and actionable sage. It earns depth through the three
paper surfaces, thin boundaries, and restrained elevation—not gradients, glow, texture imagery, or
marketing-page ornament.

`frontend/src/styles/globals.css` is the only visual-token bridge. Components consume semantic
utilities; they do not introduce component-local hex values, a Tailwind configuration, or a second
token file.

Theme storage is light-first. A missing `localStorage['lyra-theme']` key leaves `<html>` light.
Explicit stored `system` and `dark` values retain their meanings. Settings presents Light, System,
then Dark; Light reads “Use the parchment palette by default.”

## Accessibility contracts

- Body and status text is at least **4.5:1** on every documented surface.
- Strong control boundaries and focus indicators are at least **3:1** against their adjacent paper
  surface.
- Decorative fills always have a named foreground token when text or an icon appears in them.
- `:focus-visible` uses a 2px ring with a 2px offset. No control removes focus without replacing it.
- Primary actions are 44px when `size="lg"`; standard actions and default icon actions are 40px.
  Dense menu controls retain their compact 28px/32px use cases.
- Keyboard behavior stays native: the skip link is first, Radix overlays trap and restore focus,
  Escape dismisses overlays, and existing Cmd/Ctrl+B behavior remains unchanged.

Calculated WCAG ratios from the values in `globals.css`:

| Pair | Light | Dark |
| --- | ---: | ---: |
| `--text-primary` on `--bg-primary` | 12.86:1 | 15.27:1 |
| `--text-secondary` on `--bg-secondary` | 6.72:1 | 8.73:1 |
| `--text-tertiary` on `--bg-primary` | 4.62:1 | 6.93:1 |
| `--border-strong` on `--bg-primary` | 3.22:1 | 5.40:1 |
| `--focus-ring` on `--bg-primary` | 5.60:1 | 9.15:1 |
| `--accent-foreground` on `--accent-primary` | 5.94:1 | 9.15:1 |
| `--accent-surface-foreground` on `--accent-surface` | 8.43:1 | 6.11:1 |
| `--accent-secondary-foreground` on `--accent-secondary` | 5.05:1 | 5.91:1 |
| `--accent-tertiary-foreground` on `--accent-tertiary` | 5.04:1 | 5.80:1 |
| `--success-text` on `--success-fill` | 5.11:1 | 4.55:1 |
| `--info-text` on `--info-fill` | 5.50:1 | 4.83:1 |
| `--danger-foreground` on `--danger-fill` | 8.35:1 | 6.63:1 |
| `--danger-text` on `--bg-primary` | 8.07:1 | 9.93:1 |

## Color tokens

### Light palette

| Token | Value | Role |
| --- | --- | --- |
| `--bg-primary` | `#F5F1EB` | Parchment canvas and assistant reading surface |
| `--bg-secondary` | `#F9F8F6` | Raised paper, fields, popovers, rail |
| `--bg-tertiary` | `#EBE4DB` | Insets, hovers, code, skeletons |
| `--border` / `--border-strong` | `#DCD2C6` / `#9E8167` | Decorative rules / visible controls |
| `--text-primary` / `--text-secondary` / `--text-tertiary` | `#302821` / `#655548` / `#7D6959` | Espresso hierarchy |
| `--accent-primary` / `--accent-primary-hover` / `--accent-foreground` | `#45684A` / `#36543A` / `#F9F8F6` | Sage action, hover, paired action label |
| `--accent-surface` / `--accent-surface-foreground` | `#D8E4D2` / `#2A412D` | Sage decorative course mark |
| `--accent-secondary` / `--accent-secondary-foreground` | `#DCC9B7` / `#634B36` | Tan decorative course mark |
| `--accent-tertiary` / `--accent-tertiary-foreground` | `#DEC7BA` / `#6B4733` | Muted-clay decorative course mark |
| `--success-fill` / `--success-text` | `#D3E0CC` / `#466039` | Success surface and status text |
| `--info-fill` / `--info-text` | `#D1DEE0` / `#395960` | Informational surface and status text |
| `--danger-fill` / `--danger-foreground` / `--danger-text` | `#E0C6BE` / `#3D2A24` / `#673E32` | Destructive fill, paired label, inline error |
| `--focus-ring` | `#45684A` | Focus indicator |
| `--overlay` | `rgb(48 40 33 / 0.20)` | The only light overlay color |

### Dark palette

| Token | Value |
| --- | --- |
| `--bg-primary`, `--bg-secondary`, `--bg-tertiary` | `#1D1A16`, `#28241F`, `#37302A` |
| `--border`, `--border-strong` | `#5B5044`, `#A18C78` |
| `--text-primary`, `--text-secondary`, `--text-tertiary` | `#F3F0ED`, `#CDC1B6`, `#B1A195` |
| `--accent-primary`, `--accent-primary-hover`, `--accent-foreground` | `#9FC6A3`, `#B1D3B4`, `#1D1A16` |
| `--accent-surface`, `--accent-surface-foreground` | `#3D5C40`, `#E0EBE1` |
| `--accent-secondary`, `--accent-secondary-foreground` | `#66503D`, `#EBE2DB` |
| `--accent-tertiary`, `--accent-tertiary-foreground` | `#744F3E`, `#F0E5E0` |
| `--success-fill`, `--success-text` | `#43593B`, `#B3CFAA` |
| `--info-fill`, `--info-text` | `#3B5459`, `#B1CDD3` |
| `--danger-fill`, `--danger-foreground`, `--danger-text` | `#6A483E`, `#F1E7E4`, `#D7BFB7` |
| `--focus-ring`, `--overlay` | `#9FC6A3`, `rgb(14 12 10 / 0.48)` |

Sage is the only actionable accent. Tan and muted clay are decorative fills only; always pair them
with their named foreground. Status and destructive surfaces use their own semantic pairs.

## Token bridge

`globals.css` owns raw Lyra tokens, shadcn aliases, and Tailwind v4 `@theme inline` mappings. The
bridge maps `bg-overlay` to `--overlay`, `shadow-sm`/`shadow-md`/`shadow-lg` to the elevation
tokens, and all custom text, fill, and foreground utilities to their semantic source.

```css
:root {
  --background: var(--bg-primary);
  --card: var(--bg-secondary);
  --primary: var(--accent-primary);
  --primary-foreground: var(--accent-foreground);
  --destructive: var(--danger-text);
  --destructive-foreground: var(--accent-foreground);
  --input: var(--border-strong);
  --ring: var(--focus-ring);
}

@theme inline {
  --color-overlay: var(--overlay);
  --color-destructive: var(--danger-text);
  --color-destructive-foreground: var(--accent-foreground);
  --shadow-sm: var(--elevation-sm);
  --shadow-md: var(--elevation-md);
  --shadow-lg: var(--elevation-lg);
}
```

Invalid-control borders and inline errors therefore use `--danger-text`. The destructive Button
variant explicitly uses `--danger-fill` and `--danger-foreground`; do not infer its colors from the
shadcn destructive alias.

Highlight.js is local, not a GitHub theme import: code uses `--bg-tertiary` and `--text-primary`;
keywords use `--danger-text`; titles and tags use `--accent-primary`; literals and numbers use
`--info-text`; strings use `--accent-tertiary-foreground`; comments use `--text-tertiary`.

## Typography

`frontend/src/app/layout.tsx` loads DM Sans (`400`, `500`, `600`, `700`) as
`--font-dm-sans`, Fraunces (`500`, `600`, `700`) as `--font-fraunces`, keeps JetBrains Mono, and
loads Source Serif 4 (`400`, `600`, both with italics) as `--font-source-serif`.
`globals.css` maps them to `--font-sans`, `--font-heading`, `--font-mono`, and the assistant-only
`--font-ai-response` token.

- **DM Sans:** body copy, labels, controls, metadata, state text, and navigation.
- **Fraunces:** `h1`, `h2`, `h3`, and Card, Dialog, Sheet, and Empty titles only.
- **JetBrains Mono:** code, file-oriented technical notation, and keyboard notation.
- **Source Serif 4:** assistant response reading surfaces only, at `1.0625rem` and `1.65` leading;
  KaTeX retains its own math font.

The reading face must ship a real bold. Tutor answers are prose with emphasis, and a single-weight
face leaves the browser to synthesize every `**bold**` run, which renders visibly wrong at reading
size. Tables inside a response drop back to `--font-sans`: tabular data is not prose, and the
serif's wider figures cost column width the message can rarely spare.

Display hierarchy comes from tighter tracking and scale, not heavier weights. Heading defaults use
`tracking-tight`; small uppercase editorial labels use measured positive tracking.

## Surface geometry and elevation

| Token | Light | Dark |
| --- | --- | --- |
| `--radius-sm` | `6px` | `6px` |
| `--radius-md` | `10px` | `10px` |
| `--radius-lg` | `16px` | `16px` |
| `--elevation-sm` | `0 2px 8px rgb(48 40 33 / 0.05)` | `0 2px 8px rgb(14 12 10 / 0.40)`, `inset 0 1px 0 rgb(255 250 244 / 0.04)` |
| `--elevation-md` | `0 12px 30px rgb(48 40 33 / 0.08)` | `0 12px 30px rgb(14 12 10 / 0.50)`, `inset 0 1px 0 rgb(255 250 244 / 0.05)` |
| `--elevation-lg` | `0 24px 60px rgb(48 40 33 / 0.12)` | `0 24px 60px rgb(14 12 10 / 0.62)`, `inset 0 1px 0 rgb(255 250 244 / 0.06)` |

A drop shadow is a light-theme device: on a dark canvas it darkens dark and reads as nothing. Dark
therefore carries a second, inset hairline of lifted paper along the top of each elevated surface,
and a lighter `--border`, so raised paper still reads as raised in both themes.

Cards are 16px raised-paper surfaces: `--bg-secondary`, a 1px `--border`, and `shadow-sm`.
Inputs, textareas, selects, settings rows, and the composer well use 10px radii and
`--border-strong`. Borders lead separation; shadows only reinforce it. Full rounding is reserved for
avatars, status dots, switches, and compact metadata badges.

## Component recipes

- **Buttons:** default is solid sage; outline is raised paper with a strong edge; secondary is quiet
  paper; ghost is transparent; destructive is `--danger-fill` with `--danger-foreground`.
- **Fields:** paper fill, strong edge, native labels and validation markup, 2px focus ring with a 2px
  offset. Invalid controls use `--danger-text`.
- **Switches:** default size is exactly 44×24px; the thumb is bordered paper.
- **Badges:** pills are limited to compact status and scope metadata.
- **Tabs:** default workspace tabs use `TabsList variant="line"`; the active state is a 2px sage rule,
  never a filled rounded segment.
- **Course marks:** class initials use a deterministic sage/tan/clay mapping keyed by class ID.
  Course-mark avatars are rectangular through `rounded-[inherit]` children.
- **Overlays and surfaces:** AlertDialog, Dialog, and Sheet overlays use `bg-overlay`. Dialogs, sheets,
  dropdowns, popovers, selects, tooltips, and Sonner use paper, border, and semantic elevation.
- **Shell:** desktop uses a 260px inset rail that moves off-canvas when closed; mobile uses a floating,
  64px paper shelf below 640px. Main content keeps the 1320px cap.
- **Workspace:** compact Documents/Chat uses line tabs, defaulting to Chat; on desktop one
  raised-paper workbench holds the conversation, with documents opening as a 340px right column
  into the gutter the 860px reading measure was never going to use. Document rows are paper items
  with a sage selected edge. Student messages are muted-paper notes; assistant messages are
  full-width parchment reading surfaces.
- **Course marks:** the mark is the code's subject prefix (`ECE 203` marks as `ECE`), falling back
  to name initials when a class has no code. Never per-word initials, which render `ECE 203` as
  `E2` and collide across a department.
- **Settings:** Tutor model, Privacy, and Appearance are raised-paper sections. Appearance rows have
  a token swatch and a 44px minimum target. Remote endpoint warnings remain semantic danger alerts.

## Motion and reduced motion

Motion is useful only when it explains structure. Content may fade and move vertically at most 8px;
no surface slides sideways, zooms, or lasts more than 250ms.

`Reveal` in `frontend/src/components/ui/reveal.tsx` is the only shared visual reveal utility. It
enters at opacity `0`, `y: 8` over 250ms with `ease: [0.25, 0.1, 0.3, 1]`; delay is capped at 200ms.
Class cards use the five-step, 50ms stagger through this cap. With reduced motion it uses only a
150ms opacity fade, zero delay, and no transform.

`Reveal` takes an `once` id and plays at most once per session for that id. Motion explains that
something arrived; replaying the cascade every time the user navigates back to a list they have
already seen explains nothing and reads as latency.

Skeletons, Spinner, and Sonner's loading icon use `motion-safe` animation. Under
`prefers-reduced-motion: reduce`, skeletons remain static, spinners stay visible without rotating,
the streaming caret remains a solid 2px sage marker, and panel transitions do not transform.

## Focus and keyboard

- The skip link precedes the shell and targets `main#main-content`.
- Icon-only buttons have accessible names; document actions, message copy/retry, send/stop, profile,
  sidebar, and mobile navigation remain named.
- Dialogs and sheets preserve Radix focus trapping and trigger restoration. Existing Escape and form
  Enter behavior remains intact.
- Document picker behavior remains native; keyboard activates the compact upload well.
- The visual selected state of Light, System, and Dark accompanies the accessible radio label.

## Implementation rules

1. `frontend/src/styles/globals.css` remains the one source of truth for colors, fonts, elevations,
   global syntax styling, and reduced-motion policy.
2. Do not add a Tailwind config, token file, external theme package, visual registry dependency, or
   component-local hex color.
3. Do not add gradients, glows, literal texture images, continuous effects, scroll-triggered reveals,
   model selectors, simulated uploads, or unrelated prompt controls.
4. Do not replace Radix behavior, existing keyboard paths, four data states, API behavior, hooks, or
   schema merely to restyle a route.
5. Keep Light, System, and Dark coherent. A documentation value or contrast claim that differs from
   `globals.css` is a design-system defect.
