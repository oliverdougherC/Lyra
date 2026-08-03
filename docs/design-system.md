# Design System

## Core Principle

Lyra's interface should feel expensive, polished, and intentional. Every element - spacing, typography, animation, color - should communicate quality. This is not a prototype aesthetic. It is a product-level finish.

The visual language is **earthy and educational**: warm neutrals, soft pastels, natural textures. Think linen paper, chalkboard, wood grain. Grounded, not digital. Calm, not energetic.

## Color Palette

### Light Theme (Default)

| Token | Hex | Usage |
|-------|-----|-------|
| `--bg-primary` | `#FAF8F5` | Page background (warm off-white, not pure white) |
| `--bg-secondary` | `#F3F0EB` | Cards, panels, elevated surfaces |
| `--bg-tertiary` | `#EBE7DF` | Borders, dividers, subtle backgrounds |
| `--text-primary` | `#2C2C2C` | Main body text (warm dark gray, not pure black) |
| `--text-secondary` | `#5C5C5C` | Secondary text, captions, metadata |
| `--text-tertiary` | `#8A8A8A` | Placeholders, disabled text |
| `--accent-primary` | `#7BA17D` | Primary actions, links, active states (muted sage green) |
| `--accent-secondary` | `#C4A882` | Secondary accents, highlights (warm tan) |
| `--accent-pastel-green` | `#A8C5A0` | Success states, positive indicators |
| `--accent-pastel-blue` | `#8FAEC4` | Information states, neutral highlights |
| `--accent-pastel-rose` | `#C4948A` | Error states, warnings (muted, not harsh red) |

### Dark Theme

| Token | Hex | Usage |
|-------|-----|-------|
| `--bg-primary` | `#1A1A1A` | Page background (warm dark, not pure black) |
| `--bg-secondary` | `#242424` | Cards, panels, elevated surfaces |
| `--bg-tertiary` | `#2F2F2F` | Borders, dividers, subtle backgrounds |
| `--text-primary` | `#E8E4DF` | Main body text (warm light, not pure white) |
| `--text-secondary` | `#A09C96` | Secondary text, captions, metadata |
| `--text-tertiary` | `#6B6863` | Placeholders, disabled text |
| `--accent-primary` | `#8FB88E` | Primary actions, links, active states |
| `--accent-secondary` | `#D4B896` | Secondary accents, highlights |
| `--accent-pastel-green` | `#8FB88E` | Success states |
| `--accent-pastel-blue` | `#7DA3C4` | Information states |
| `--accent-pastel-rose` | `#C48A80` | Error states |

### Design Notes

- No pure black (`#000`) or pure white (`#FFF`) anywhere. Always warm variants.
- Accent colors are desaturated, never vivid or neon.
- Color is used sparingly for function (states, actions). Surfaces are neutral.
- Pastels are soft and muted, not candy-colored. They should feel like watercolor, not marker.

## Typography

### Font Stack

**Headings and UI:** Inter (sans-serif)
- Clean, professional, highly legible at all sizes
- Weights: 400 (regular), 500 (medium), 600 (semibold)

**Monospace:** JetBrains Mono (code, math, data)
- Distinct character shapes for readability
- Used for: inline code, equation snippets, file names, technical notation

### Scale

| Level | Size | Weight | Line Height | Usage |
|-------|------|--------|-------------|-------|
| `h1` | 32px | 600 | 1.2 | Page titles |
| `h2` | 24px | 600 | 1.3 | Section headers |
| `h3` | 20px | 500 | 1.4 | Card titles, subsections |
| `h4` | 16px | 500 | 1.5 | Labels, small headers |
| `body-lg` | 16px | 400 | 1.6 | Main body text |
| `body` | 14px | 400 | 1.6 | Default body |
| `body-sm` | 13px | 400 | 1.5 | Secondary text, captions |
| `caption` | 12px | 400 | 1.4 | Metadata, timestamps |

## Spacing System

Base unit: 4px. All spacing is a multiple of 4.

| Token | Value | Usage |
|-------|-------|-------|
| `space-1` | 4px | Tight spacing, icon gaps |
| `space-2` | 8px | Small gaps, padding edges |
| `space-3` | 12px | Button padding, input spacing |
| `space-4` | 16px | Section padding, card internal |
| `space-5` | 20px | Generous padding |
| `space-6` | 24px | Card margins, section gaps |
| `space-8` | 32px | Major section breaks |
| `space-10` | 40px | Page-level spacing |
| `space-12` | 48px | Wide spacing, hero areas |
| `space-16` | 64px | Full section dividers |

## Border Radius

| Token | Value | Usage |
|-------|-------|-------|
| `radius-sm` | 6px | Badges, tags, small buttons |
| `radius-md` | 10px | Cards, inputs, buttons |
| `radius-lg` | 16px | Panels, modals, elevated surfaces |
| `radius-full` | 9999px | Avatars, pills, toggle switches |

## Shadows

Subtle, warm shadows. No harsh drop shadows.

| Token | Value | Usage |
|-------|-------|-------|
| `shadow-sm` | `0 1px 2px rgba(44,44,44,0.04)` | Buttons, small elements |
| `shadow-md` | `0 4px 12px rgba(44,44,44,0.06)` | Cards, elevated surfaces |
| `shadow-lg` | `0 8px 24px rgba(44,44,44,0.08)` | Modals, dropdowns |

## Animation

### Philosophy

Animations should feel natural and purposeful - they reveal relationships between elements and guide attention. No gratuitous motion.

### Easing Curves

| Name | Cubic Bezier | Usage |
|------|-------------|-------|
| `ease-out` | `cubic-bezier(0.25, 0.1, 0.25, 1)` | Default for most transitions |
| `ease-in-out` | `cubic-bezier(0.4, 0, 0.2, 1)` | Entering/leaving view |
| `spring` | `cubic-bezier(0.34, 1.56, 0.64, 1)` | Micro-interactions (buttons, toggles) |
| `gentle` | `cubic-bezier(0.25, 0.1, 0.3, 1)` | Content reveals |

### Duration

| Token | Value | Usage |
|-------|-------|-------|
| `duration-fast` | 150ms | Hover states, micro-interactions |
| `duration-normal` | 250ms | Default transitions |
| `duration-slow` | 400ms | Page transitions, major reveals |

### Animation Principles

- Content enters from below or fades in, never from the sides
- Stagger lists by 50ms per item (max 5 items staggered, then simultaneous)
- Hover states use scale (1.02x max) and shadow, not color changes
- Loading states use skeleton screens, not spinners
- Page transitions use crossfade, not slides

## Component Patterns

### Cards
- Background: `--bg-secondary`
- Border: 1px solid `--bg-tertiary`
- Border radius: `radius-md`
- Padding: `space-4` internal
- Shadow: `shadow-sm`, elevates to `shadow-md` on hover

### Buttons
- Primary: `--accent-primary` background, `--text-primary` (white variant) text
- Secondary: `--bg-secondary` background, `--text-primary` text, 1px border
- Destructive: `--accent-pastel-rose` background
- Padding: `space-2` vertical, `space-4` horizontal
- Border radius: `radius-md`
- Hover: subtle scale (1.02x) + shadow elevation

### Inputs
- Background: `--bg-primary`
- Border: 1px solid `--bg-tertiary`, transitions to `--accent-primary` on focus
- Border radius: `radius-md`
- Padding: `space-3` horizontal, `space-2` vertical
- Focus ring: 2px `--accent-primary` with 2px offset
- Placeholder: `--text-tertiary`

### Toggle Switches
- Track: `--bg-tertiary` (off), `--accent-primary` (on)
- Thumb: `--bg-primary` with `shadow-sm`
- Border radius: `radius-full`
- Size: 44px wide, 24px tall

### Avatars
- Circular (`radius-full`)
- Background: `--bg-tertiary`
- Text: First two letters of class/user name, `--text-secondary`
- Size variants: 32px, 40px, 48px

## Layout

### Grid
- Max content width: 1200px
- Sidebar width: 260px (collapsible to 60px icon bar)
- Main content padding: `space-6` on desktop, `space-4` on tablet
- Chat area: max 720px width, centered

### Responsive Breakpoints
- Mobile: < 640px (single column, sidebar becomes bottom nav)
- Tablet: 640-1024px (sidebar collapses to icons)
- Desktop: > 1024px (full layout)

## Dark Mode

- Toggle in settings, respects system preference by default
- All tokens swap to dark theme values
- Images and embedded content should have dark mode variants where possible
- Code blocks and math rendering should adapt to dark background

## Iconography

- **Lucide React** for all icons
- Stroke width: 1.5px (default)
- Size: 20px for inline, 24px for standalone
- Color: `--text-secondary` by default, `--text-primary` for active states

## Anti-Patterns (What Lyra is NOT)

- No gradient backgrounds, text gradients, or neon glows
- No emoji in the UI (icons only)
- No rounded-full everywhere - use measured border radius
- No card borders that are too heavy - 1px is the standard
- No animation that exceeds 400ms duration
- No pure black or pure white surfaces
- No color saturation above 40% - everything is muted and warm
