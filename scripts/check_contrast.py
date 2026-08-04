"""Verify the recorded contrast contracts in design-system.md against globals.css.

The design system states that a documentation value differing from `globals.css` is a defect,
so this recomputes every recorded pair from the stylesheet rather than trusting the table.
"""

import re
import sys
from pathlib import Path

CSS = Path("frontend/src/styles/globals.css").read_text()
DOC = Path("docs/design-system.md").read_text()


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

rows = re.findall(
    r"\|\s*`(--[\w-]+)`\s+on\s+`(--[\w-]+)`\s*\|\s*([\d.]+):1\s*\|\s*([\d.]+):1\s*\|", DOC
)
if not rows:
    sys.exit("no contrast rows parsed from design-system.md")

TOLERANCE = 0.05
failures = 0
print(f"{'pair':<58} {'light':>16} {'dark':>16}")
print("-" * 92)
for fg, bg, doc_light, doc_dark in rows:
    for name, table in (("light", light), ("dark", dark)):
        if fg not in table or bg not in table:
            print(f"MISSING TOKEN {fg} or {bg} in {name}")
            failures += 1
    actual_l, actual_d = ratio(fg, bg, light), ratio(fg, bg, dark)
    ok_l = abs(actual_l - float(doc_light)) <= TOLERANCE
    ok_d = abs(actual_d - float(doc_dark)) <= TOLERANCE
    failures += (not ok_l) + (not ok_d)
    mark_l = "ok " if ok_l else "OFF"
    mark_d = "ok " if ok_d else "OFF"
    print(
        f"{fg + ' on ' + bg:<58} "
        f"{actual_l:>6.2f} vs {doc_light:>5} {mark_l} "
        f"{actual_d:>6.2f} vs {doc_dark:>5} {mark_d}"
    )

print()
print("Floor checks (body/status text >= 4.5, boundaries and focus >= 3.0):")
FLOORS = {"--border-strong": 3.0, "--focus-ring": 3.0}
for fg, bg, _, _ in rows:
    floor = FLOORS.get(fg, 4.5)
    for name, table in (("light", light), ("dark", dark)):
        value = ratio(fg, bg, table)
        if value < floor:
            print(f"  BELOW FLOOR {name}: {fg} on {bg} = {value:.2f} < {floor}")
            failures += 1

print()
print("FAILURES:", failures)
sys.exit(1 if failures else 0)
