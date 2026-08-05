"""Dimensional analysis.

Cheap, and it catches a large share of physics and engineering errors: a velocity that
came out in newtons is wrong no matter how clean the algebra looked. That is why this is
in the first tool set rather than a later refinement.

Unlike `cas`, this runs in the backend process, and the difference is deliberate rather
than an inconsistency. SymPy is out of process because its parser evaluates and its work
is unbounded. Pint's parser walks the expression as an AST and asserts every node against
a small whitelist, refusing anything else before evaluation, and unit arithmetic
terminates.

Terminating is not the same as being bounded, and that difference is what the gates here
have to cover. Arithmetic on plain numbers is exactly what the whitelist allows, so
`2**999999999` is evaluated rather than refused: it finishes, and it builds an integer a
billion bits wide in this process, where there is no child to absorb it. The gates are
therefore a length cap, a character allowlist that ends at mathematical notation, a
ceiling on any literal exponent, and a ceiling on the magnitude that may be reported back.

If pint ever grows an evaluator that runs arbitrary work, this belongs in the runner
beside SymPy, and the honest reason it is not there today is stated above rather than
assumed.
"""

import math
import re
from typing import TYPE_CHECKING

from backend.tools.result import ToolResult, failure, success

if TYPE_CHECKING:
    from pint import UnitRegistry

MAX_EXPRESSION_CHARS = 500

# The largest power a unit expression has any business raising anything to.
#
# `2**999999999` is a whitelisted AST of arithmetic on numbers, so pint evaluates it, and
# it terminates: the property this module's gates were written against holds, and it is
# not the property that matters. What it builds is an integer a billion bits wide, in the
# backend process, because unlike `cas` there is no child here to absorb it. Terminating
# and being bounded are two different things, and only the first was ever checked.
MAX_EXPONENT = 1000

# `2**30`, `2^30`, `2**(30)`. Only a literal exponent is read: a symbolic one is not a
# number pint will raise anything to, and it fails as an undefined unit either way.
_EXPONENT = re.compile(r"(?:\*\*|\^)\s*\(?\s*(\d+)")

# An integer wider than this cannot be written into the tool transcript. `json.dumps`
# refuses an integer above CPython's 4300-digit conversion limit, and that refusal happens
# where nothing can act on it, so the ceiling is enforced here instead. Far above any
# magnitude a real quantity carries.
_MAX_MAGNITUDE_BITS = 4096

# Narrower than the CAS allowlist: a unit expression needs no comparison operators. The
# two sets are written separately rather than shared because they gate two different
# parsers, and coupling them would let a change for one quietly widen the other.
_SAFE_CHARACTERS = re.compile(r"^[A-Za-z0-9_+\-*/^().% \t]*$")

_DIMENSIONLESS = "dimensionless"

_registry: "UnitRegistry | None" = None


def _get_registry() -> "UnitRegistry":
    """The shared unit registry, built once.

    Building it parses pint's unit definitions, which is fast but not free, and the
    registry is read-only in use. Two registries would also make their quantities
    incomparable, which is a confusing failure to debug.
    """
    global _registry
    if _registry is None:
        from pint import UnitRegistry

        _registry = UnitRegistry()
    return _registry


def _check(text: str, label: str) -> str:
    """Refuse anything that is not plainly a unit expression."""
    stripped = (text or "").strip()
    if not stripped:
        raise ValueError(f"{label} is empty.")
    if len(stripped) > MAX_EXPRESSION_CHARS:
        raise ValueError(f"{label} is longer than {MAX_EXPRESSION_CHARS} characters.")
    if "__" in stripped:
        raise ValueError(f"{label} contains a double underscore.")
    if not _SAFE_CHARACTERS.match(stripped):
        raise ValueError(f"{label} contains characters that are not a unit expression.")
    # Before pint evaluates, because the cost of this one is paid during evaluation and
    # there is no process boundary here to pay it on our behalf.
    if any(int(found) > MAX_EXPONENT for found in _EXPONENT.findall(stripped)):
        raise ValueError(f"{label} raises something to a power above {MAX_EXPONENT}.")
    return stripped


def _reportable(magnitude: object) -> bool:
    """Whether a magnitude can be written into the tool transcript at all.

    The transcript is serialized with `json.dumps`, which raises on an integer wider than
    CPython's conversion limit. That raise happens in the tool loop, outside the guard
    around the handler, so it does not fail one check: it travels out of the loop and
    costs the whole verification pass, discarding every check that had already run and
    reporting the problem as one nobody could check.

    A magnitude that cannot be written is therefore left out. The dimensional answer,
    which is what this tool exists for, still gets through.
    """
    if isinstance(magnitude, bool) or not isinstance(magnitude, int | float):
        return False
    if isinstance(magnitude, float):
        # A non-finite float is written as a bare `Infinity`, which is not JSON.
        return math.isfinite(magnitude)
    return magnitude.bit_length() <= _MAX_MAGNITUDE_BITS


def _dimensionality(quantity: object) -> str:
    """A readable dimensionality, with the dimensionless case named rather than blank."""
    rendered = str(getattr(quantity, "dimensionality", ""))
    return rendered or _DIMENSIONLESS


def check(expression: str, expected: str) -> ToolResult:
    """Check whether an expression carries the expected dimensions.

    Dimensions, not units: `1000 m` and `1 km` are the same answer, and a check that
    called them different would fire on every correct solution written in another unit.
    Magnitude is reported alongside so a caller can see the number too, but it is not
    what `matches` is about.

    Args:
        expression: A quantity with units, such as `9.8 m/s^2` or `(2 * 5 kg) * 3 m/s^2`.
            A bare number is read as dimensionless, which is a real answer and not an
            error.
        expected: The units the value should carry, such as `m/s^2` or `N`.

    Returns:
        `matches`, the expression's `units` and `dimensionality`, the
        `expected_dimensionality`, and `magnitude` when the value is numeric.
    """
    try:
        left = _check(expression, "The expression")
        right = _check(expected, "The expected units")
    except ValueError as exc:
        return failure(str(exc))

    registry = _get_registry()
    try:
        # `^` is what a student writes for a power; pint wants `**`.
        value = registry.parse_expression(left.replace("^", "**"))
    except Exception:
        # Pint raises undefined-unit errors, tokenizer errors, and bare assertions for
        # syntax outside its whitelist. The type name would tell the model nothing useful.
        return failure(f"Could not read the expression as a quantity with units: {left}")

    try:
        expected_units = registry.Unit(right.replace("^", "**"))
    except Exception:
        return failure(f"Not a unit Lyra recognises: {right}")

    actual = _dimensionality(value)
    wanted = _dimensionality(expected_units)
    result: dict[str, object] = {
        "matches": actual == wanted,
        "units": str(getattr(value, "units", _DIMENSIONLESS)),
        "dimensionality": actual,
        "expected_dimensionality": wanted,
    }
    magnitude = getattr(value, "magnitude", value)
    if _reportable(magnitude):
        result["magnitude"] = magnitude
    return success(**result)
