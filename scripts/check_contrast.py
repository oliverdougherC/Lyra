"""Verify the Ex Libris contrast contract against globals.css.

Design system section 3.3: every text pair clears 4.5:1 on its actual ground, stone
included, in both modes; boundaries and the focus ring clear 3.0:1. This script recomputes
every recorded pair from the stylesheet itself, so a value that drifts in globals.css fails
the build rather than passing on a stale doc table. It gates every migration commit.
"""

import re
import sys
from pathlib import Path

CSS = Path("frontend/src/styles/globals.css").read_text()

# Grounds a pair may be read against. "paper" is the content sheet; "stone" is the canvas
# and rail. Both are named so text-on-stone is checked as explicitly as text-on-paper.
PAPER = "--bg-secondary"
STONE = "--bg-primary"
FILL = "--bg-tertiary"  # quiet fills: hovers, chips, code

# (foreground, background, floor). 4.5 for anything read as text; 3.0 for a boundary or the
# focus ring, which are shapes rather than glyphs.
PAIRS: list[tuple[str, str, float]] = [
    # Ink and its quieter voices, on every ground they land on.
    ("--text-primary", PAPER, 4.5),
    ("--text-primary", STONE, 4.5),
    ("--text-primary", FILL, 4.5),
    ("--text-secondary", PAPER, 4.5),
    ("--text-secondary", STONE, 4.5),
    ("--text-tertiary", PAPER, 4.5),
    ("--text-tertiary", STONE, 4.5),
    # Accent ink on the accent plaque, and quiet accent text on paper and stone.
    ("--accent-foreground", "--accent-primary", 4.5),
    ("--accent-surface-foreground", "--accent-surface", 4.5),
    ("--accent-text", PAPER, 4.5),
    ("--accent-text", STONE, 4.5),
    # Status words, each on the ground it prints against.
    ("--success-text", PAPER, 4.5),
    ("--success-text", "--success-fill", 4.5),
    ("--info-text", PAPER, 4.5),
    ("--info-text", "--info-fill", 4.5),
    ("--danger-text", PAPER, 4.5),
    ("--danger-text", STONE, 4.5),
    ("--danger-foreground", "--danger-fill", 4.5),
    # The pen and the Mark, where they carry meaning as text.
    ("--hand", PAPER, 4.5),
    ("--hand", STONE, 4.5),
    ("--hand-red", PAPER, 4.5),
    ("--trust", PAPER, 4.5),
    ("--trust", STONE, 4.5),
    # Boundaries and the focus ring: shapes, so 3.0 is the floor.
    ("--border-strong", PAPER, 3.0),
    ("--border-strong", STONE, 3.0),
    ("--focus-ring", PAPER, 3.0),
    ("--focus-ring", STONE, 3.0),
]


def blocks(css: str) -> tuple[str, str]:
    """The `:root` (light) and `.dark` token blocks."""
    light = re.search(r":root\s*\{(.*?)\n\}", css, re.DOTALL)
    dark = re.search(r"\.dark\s*\{(.*?)\n\}", css, re.DOTALL)
    if not light or not dark:
        sys.exit("could not locate :root or .dark token blocks")
    return light.group(1), dark.group(1)


def tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block))


def to_rgb(value: str, table: dict[str, str], depth: int = 0) -> tuple[float, float, float]:
    value = value.strip()
    if depth > 10:
        sys.exit(f"token reference loop at {value}")
    ref = re.fullmatch(r"var\(\s*(--[\w-]+)\s*\)", value)
    if ref:
        return to_rgb(table[ref.group(1)], table, depth + 1)
    hex_match = re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", value)
    if hex_match:
        digits = hex_match.group(1)
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        return tuple(int(digits[i : i + 2], 16) for i in (0, 2, 4))
    rgb = re.fullmatch(r"rgba?\(([^)]+)\)", value)
    if rgb:
        parts = re.split(r"[,\s/]+", rgb.group(1).strip())
        return tuple(float(p) for p in parts[:3])
    sys.exit(f"unsupported color form: {value!r}")


def luminance(rgb: tuple[float, float, float]) -> float:
    channels = []
    for raw in rgb:
        c = raw / 255
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg: str, bg: str, table: dict[str, str]) -> float:
    a, b = luminance(to_rgb(table[fg], table)), luminance(to_rgb(table[bg], table))
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


light_block, dark_block = blocks(CSS)
light, dark = tokens(light_block), tokens(dark_block)
# The dark block overrides a subset; anything it does not restate keeps its light value.
dark = {**light, **dark}

failures = 0
print(f"{'pair':<46} {'floor':>6} {'light':>10} {'dark':>10}")
print("-" * 76)
for fg, bg, floor in PAIRS:
    for name, table in (("light", light), ("dark", dark)):
        if fg not in table or bg not in table:
            print(f"MISSING TOKEN {fg} or {bg} in {name}")
            failures += 1
    actual_l, actual_d = ratio(fg, bg, light), ratio(fg, bg, dark)
    ok_l, ok_d = actual_l >= floor, actual_d >= floor
    failures += (not ok_l) + (not ok_d)
    print(
        f"{fg + ' on ' + bg:<46} {floor:>6.1f} "
        f"{actual_l:>6.2f} {'ok ' if ok_l else 'OFF'} "
        f"{actual_d:>6.2f} {'ok ' if ok_d else 'OFF'}"
    )

print()
print("FAILURES:", failures)
sys.exit(1 if failures else 0)
