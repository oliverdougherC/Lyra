# Design System

## Core Principle

Lyra's interface should feel expensive, polished, and intentional. Every element, spacing,
typography, animation, and color should communicate quality. This is not a prototype aesthetic. It
is a product-level finish.

The visual language is **earthy and educational**: warm neutrals, soft pastels, natural texture.
Think linen paper, chalkboard, wood grain. Grounded, not digital. Calm, not energetic.

## Accessibility Is Part Of The Finish

Every color pair in this document has been checked against WCAG 2.1 AA and the required ratio is
recorded beside it. A token that cannot meet its contract is not a style choice, it is a defect.

- Body and UI text: **4.5:1** minimum against its background
- Large text (18.66px bold or 24px plain and above): **3:1** minimum
- Interactive component boundaries and focus indicators: **3:1** minimum against adjacent color
- Purely decorative dividers and surface separation are exempt

Two consequences shape the palette below, and both were mistakes worth naming:

1. **The brand sage and the actionable accent cannot be the same token.** `#7BA17D` is a beautiful
   surface color but only reaches 2.74 against the page background, so it can be neither readable
   text nor a reliable focus indicator. The palette therefore splits them: `--accent-primary` is the
   deeper, actionable sage used for buttons, links, and focus, while `--accent-surface` keeps the
   original brand sage for decorative fills.
2. **Every colored fill needs a declared foreground.** Never leave the label color on a button to the
   component author.

## Color Palette

### Light Theme (Default)

| Token | Hex | Usage | Contract |
|-------|-----|-------|----------|
| `--bg-primary` | `#FAF8F5` | Page background, input fill | warm off-white |
| `--bg-secondary` | `#F3F0EB` | Cards, panels, elevated surfaces | |
| `--bg-tertiary` | `#EBE7DF` | Subtle fills, hover surfaces, skeletons | |
| `--border` | `#DCD6CB` | Decorative dividers and card edges | decorative, exempt |
| `--border-strong` | `#8F8579` | Input, checkbox, and control borders | 3.42 on bg-primary, 3.19 on bg-secondary |
| `--text-primary` | `#2C2C2C` | Body text | 13.17 on bg-primary |
| `--text-secondary` | `#5C5C5C` | Captions, metadata | 6.31 / 5.88 |
| `--text-tertiary` | `#776B5F` | Placeholders, disabled text | 4.89 / 4.56 |
| `--accent-primary` | `#456F47` | Primary button fill, links, active nav, focus | 5.48 / 5.11 / 4.71 on the three surfaces |
| `--accent-primary-hover` | `#3B5F3D` | Primary fill hover | 6.85 |
| `--accent-foreground` | `#FAF8F5` | Label on `--accent-primary` | 5.48 |
| `--accent-surface` | `#7BA17D` | Brand sage decorative fill: badges, avatars | fill only, never text |
| `--accent-surface-foreground` | `#1E2A1F` | Label on `--accent-surface` | 5.15 |
| `--focus-ring` | `#456F47` | Focus indicator | 5.48, needs 3.0 |
| `--accent-secondary` | `#C4A882` | Warm tan decorative fill | fill only, never text |
| `--success-fill` | `#A8C5A0` | Success surfaces and badges | fill only |
| `--success-text` | `#566F4D` | Success text and icons | 5.25 / 4.89 / 4.51 |
| `--info-fill` | `#8FAEC4` | Information surfaces | fill only |
| `--info-text` | `#4A6B82` | Information text and icons | 5.33 / 4.97 / 4.58 |
| `--danger-fill` | `#C4948A` | Destructive button, error surfaces | fill only |
| `--danger-foreground` | `#2C2C2C` | Label on `--danger-fill` | 5.29 |
| `--danger-text` | `#885E53` | Error message text, inline validation | 5.25 / 4.90 / 4.51 |

### Dark Theme

| Token | Hex | Usage | Contract |
|-------|-----|-------|----------|
| `--bg-primary` | `#1A1A1A` | Page background, input fill | warm dark |
| `--bg-secondary` | `#242424` | Cards, panels | |
| `--bg-tertiary` | `#2F2F2F` | Subtle fills, hover surfaces | |
| `--border` | `#3A3A3A` | Decorative dividers and card edges | decorative, exempt |
| `--border-strong` | `#736E68` | Control borders | 3.45 / 3.07 |
| `--text-primary` | `#E8E4DF` | Body text | 13.75 on bg-primary |
| `--text-secondary` | `#A09C96` | Captions, metadata | 6.37 / 5.68 |
| `--text-tertiary` | `#918A83` | Placeholders, disabled text | 5.11 / 4.56 |
| `--accent-primary` | `#8FB88E` | Primary fill, links, active nav, focus | 7.80 / 6.96 |
| `--accent-primary-hover` | `#A1C4A0` | Primary fill hover | 9.05 |
| `--accent-foreground` | `#1A1A1A` | Label on `--accent-primary` | 7.80 |
| `--accent-surface` | `#3C5A3C` | Brand sage decorative fill | fill only |
| `--accent-surface-foreground` | `#E8E4DF` | Label on `--accent-surface` | 6.10 |
| `--focus-ring` | `#8FB88E` | Focus indicator | 7.80 / 6.96 |
| `--accent-secondary` | `#D1B899` | Warm tan decorative fill | 37.8% saturation |
| `--success-fill` | `#4A6B4A` | Success surfaces | fill only |
| `--success-text` | `#8FB88E` | Success text | 7.80 / 6.96 |
| `--info-fill` | `#3D5566` | Information surfaces | fill only |
| `--info-text` | `#7DA3C4` | Information text | 6.05 / 5.39 |
| `--danger-fill` | `#C48A80` | Destructive button | fill only |
| `--danger-foreground` | `#1A1A1A` | Label on `--danger-fill` | 6.05 |
| `--danger-text` | `#C48A80` | Error message text | 6.05 / 5.39 |

### Design Notes

- No pure black (`#000`) or pure white (`#FFF`) anywhere. Always warm variants.
- **No token exceeds 40% HSL saturation.** This is checked, not asserted.
- Accent colors are desaturated, never vivid or neon.
- Color is used sparingly for function. Surfaces stay neutral.
- Pastels read like watercolor, not marker.
- Never place text on `--accent-surface`, `--accent-secondary`, or any `*-fill` token without using
  the paired `*-foreground` or a token whose contract covers it.

## Design Token Bridge

The tokens above are the single source of truth. Tailwind v4 and shadcn/ui both expect their own
naming, so the mapping is declared once in `frontend/src/styles/globals.css` and never duplicated.
Without this bridge the project would grow two parallel color vocabularies.

```css
@import 'tailwindcss';

:root {
  /* Lyra tokens: source of truth */
  --bg-primary: #faf8f5;
  --accent-primary: #456f47;
  --accent-foreground: #faf8f5;
  --accent-surface: #7ba17d;
  /* ...full set as tabled above... */
}

@theme inline {
  /* shadcn/ui contract, mapped onto Lyra tokens */
  --color-background: var(--bg-primary);
  --color-foreground: var(--text-primary);
  --color-card: var(--bg-secondary);
  --color-card-foreground: var(--text-primary);
  --color-muted: var(--bg-tertiary);
  --color-muted-foreground: var(--text-secondary);
  --color-primary: var(--accent-primary);
  --color-primary-foreground: var(--accent-foreground);
  --color-secondary: var(--bg-secondary);
  --color-destructive: var(--danger-fill);
  --color-destructive-foreground: var(--danger-foreground);
  --color-border: var(--border);
  --color-input: var(--border-strong);
  --color-ring: var(--focus-ring);
}
```

Rules:
- Components reference Tailwind utilities or shadcn semantic names. They never hardcode a hex value.
- Adding a color means adding a Lyra token first, then mapping it.
- Dark theme overrides only the Lyra tokens under `.dark`. The `@theme inline` block is written once.

**Spacing uses Tailwind's built-in scale.** Tailwind's spacing is already a 4px system, so `p-4` is
16px and `gap-6` is 24px. Lyra does **not** define parallel `--space-*` tokens; doing so would create
a second spacing vocabulary for zero benefit.

| Tailwind | Value | Usage |
|----------|-------|-------|
| `1` | 4px | Icon gaps, tight spacing |
| `2` | 8px | Small gaps, padding edges |
| `3` | 12px | Button and input padding |
| `4` | 16px | Card internals, section padding |
| `5` | 20px | Generous padding |
| `6` | 24px | Card margins, section gaps |
| `8` | 32px | Major section breaks |
| `10` | 40px | Page-level spacing |
| `12` | 48px | Wide spacing |
| `16` | 64px | Full section dividers |

## Typography

**Headings and UI:** Inter. Weights 400, 500, 600.
**Monospace:** JetBrains Mono, for inline code, equations, file names, technical notation.

| Level | Size | Weight | Line Height | Usage |
|-------|------|--------|-------------|-------|
| `h1` | 32px | 600 | 1.2 | Page titles |
| `h2` | 24px | 600 | 1.3 | Section headers |
| `h3` | 20px | 500 | 1.4 | Card titles |
| `h4` | 16px | 500 | 1.5 | Labels, small headers |
| `body-lg` | 16px | 400 | 1.6 | Main body text |
| `body` | 14px | 400 | 1.6 | Default body |
| `body-sm` | 13px | 400 | 1.5 | Secondary text |
| `caption` | 12px | 400 | 1.4 | Metadata, timestamps |

Text below 14px never carries essential information on its own.

## Border Radius

| Token | Value | Usage |
|-------|-------|-------|
| `radius-sm` | 6px | Badges, tags, small buttons |
| `radius-md` | 10px | Cards, inputs, buttons |
| `radius-lg` | 16px | Panels, modals |
| `radius-full` | 9999px | Avatars, pills, toggles |

## Shadows

Subtle, warm shadows. No harsh drop shadows.

| Token | Value | Usage |
|-------|-------|-------|
| `shadow-sm` | `0 1px 2px rgba(44,44,44,0.05)` | Buttons, small elements |
| `shadow-md` | `0 4px 12px rgba(44,44,44,0.08)` | Cards, elevated surfaces |
| `shadow-lg` | `0 8px 24px rgba(44,44,44,0.10)` | Modals, dropdowns |

Shadows carry surface separation together with `--border`, not alone. Card edges must remain
perceptible on both themes; if a card reads as flat, raise the border, not the shadow opacity.

## Animation

### Philosophy

Animation reveals relationships and guides attention. No gratuitous motion.

### Easing And Duration

| Name | Cubic Bezier | Usage |
|------|-------------|-------|
| `ease-out` | `cubic-bezier(0.25, 0.1, 0.25, 1)` | Default |
| `ease-in-out` | `cubic-bezier(0.4, 0, 0.2, 1)` | Entering and leaving view |
| `spring` | `cubic-bezier(0.34, 1.56, 0.64, 1)` | Micro-interactions |
| `gentle` | `cubic-bezier(0.25, 0.1, 0.3, 1)` | Content reveals |

| Token | Value | Usage |
|-------|-------|-------|
| `duration-fast` | 150ms | Hover, micro-interactions |
| `duration-normal` | 250ms | Default transitions |
| `duration-slow` | 400ms | Page transitions, major reveals |

### Principles

- Content enters from below or fades in, never from the sides
- Stagger lists by 50ms per item, maximum 5 staggered, then simultaneous
- Hover uses scale (1.02x maximum) and shadow, not color shifts
- Loading uses skeleton screens, not spinners
- Page transitions crossfade, never slide
- Nothing exceeds 400ms

### Reduced Motion

Honoring `prefers-reduced-motion` is mandatory, not optional polish. Under the reduced preference:

- Transform and scale animation is removed; opacity crossfades are kept at `duration-fast`
- List stagger is disabled and items appear together
- Skeleton shimmer becomes a static placeholder

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
```

Framer Motion components read the preference through `useReducedMotion()` and skip transform
variants rather than relying only on the CSS override.

## Focus And Keyboard

Every interactive element is reachable and visibly focused.

- Focus styling uses `:focus-visible`, never `:focus`, so pointer users see no ring
- Ring: 2px `--focus-ring` with a 2px offset, meeting the 3:1 boundary contract
- `outline: none` without a replacement indicator is prohibited
- Modals and dropdowns trap focus and restore it to the trigger on close
- Escape closes any dismissible overlay
- A skip-to-content link precedes the sidebar
- Hit targets are at least 24x24px, with 44x44px preferred for primary actions

## Component Patterns

### Cards
- Background `--bg-secondary`, 1px `--border`, `radius-md`, `p-4`, `shadow-sm`
- Hover elevates to `shadow-md`

### Buttons
- **Primary:** `--accent-primary` fill, `--accent-foreground` label
- **Secondary:** `--bg-secondary` fill, `--text-primary` label, 1px `--border-strong`
- **Destructive:** `--danger-fill` fill, `--danger-foreground` label
- **Ghost:** transparent, `--accent-primary` label, `--bg-tertiary` on hover
- Padding `py-2 px-4`, `radius-md`
- Hover: 1.02x scale plus shadow, and the `-hover` fill token
- Disabled: `--bg-tertiary` fill, `--text-tertiary` label, no scale. WCAG exempts text in an
  inactive control from the contrast minimum, which is why this pair sits at 4.20 rather than 4.5.
  Disabled state MUST therefore also be conveyed non-visually with `aria-disabled` and a
  `not-allowed` cursor, never by color alone.

### Inputs
- Fill `--bg-primary`, 1px `--border-strong`, `radius-md`, `px-3 py-2`
- Focus: border becomes `--focus-ring` plus the 2px ring at 2px offset
- Placeholder `--text-tertiary`
- Invalid: 1px `--danger-text` border with the message in `--danger-text`

### Toggle Switches
- Track `--bg-tertiary` off, `--accent-primary` on, so the on state clears 3:1 against the page
- Thumb `--bg-primary` with `shadow-sm` and a 1px `--border-strong` edge so it stays visible on the
  active track
- 44x24px, `radius-full`

### Avatars
- Circular, `--bg-tertiary` fill, `--text-secondary` initials
- Sizes 32px, 40px, 48px

### States
Every list and data surface defines four states, and none may be an afterthought:
- **Loading:** skeleton matching the real layout
- **Empty:** icon, one line of explanation, one primary action
- **Error:** `--danger-text` message plus a retry action
- **Partial:** shown when retrieval was heavily trimmed, per rag-pipeline.md

## Layout

- Max content width 1200px
- Sidebar 260px, collapsible to a 60px icon bar
- Main padding `p-6` desktop, `p-4` tablet
- Chat column max 720px, centered

### Responsive Breakpoints
- Mobile below 640px: single column, sidebar becomes bottom navigation
- Tablet 640 to 1024px: sidebar collapses to icons
- Desktop above 1024px: full layout

Responsive layout keeps the web app usable on a narrow window. It is not a mobile app, which stays
out of scope.

## Dark Mode

- Follows system preference by default, with an override in Settings
- Only Lyra tokens are redefined under `.dark`
- Code blocks and math rendering adapt to the dark surface

## Iconography

- Lucide React, 1.5px stroke
- 20px inline, 24px standalone
- `--text-secondary` default, `--text-primary` active, `--accent-primary` when indicating accent state
- Every icon-only control carries an `aria-label`

## Anti-Patterns (What Lyra Is Not)

- No gradient backgrounds, text gradients, or neon glows
- No emoji in the UI; icons only
- No `radius-full` everywhere; use measured radius
- No heavy borders; 1px is the standard
- No animation beyond 400ms
- No pure black or pure white surfaces
- No saturation above 40%
- No text on a fill token without its paired foreground
- No `outline: none` without a replacement focus indicator
- No hardcoded hex values in component files
