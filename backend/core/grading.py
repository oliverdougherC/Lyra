"""Layered grading for free-response quiz answers (PLA-496).

A fill-blank answer is graded the way a careful TA grades one: cheap exact
equivalence first, then the typed checks that settle what they can decide, then one
constrained provider judgment against the question's hidden grading contract, and never
a confident "wrong" where the evidence is thin. The layers, cheapest first:

1. Trivial equivalence. Whitespace, case, and irrelevant punctuation folded away; a
   second normalized form where a match is settled outright, so an endpoint is never
   called for `2π/Ts` when the student wrote `2pi/Ts`.
2. Rubric alternatives. The acceptable alternative forms the question generator wrote
   down, compared the same way.
3. Numeric. Magnitudes in base units with a relative tolerance, so `4200 mHz` equals
   `4.2 Hz`, `42%` equals `0.42`, and three-significant-figure rounding counts. Pint
   runs in-process because its parser whitelists every node before it evaluates, the
   same reasoning `backend/tools/units.py` already recorded.
4. Sets and lists. Order-insensitive item comparison for answers that are lists,
   settled only where membership is mathematics: a complete deterministic assignment
   settles right, a settled numeric mismatch settles wrong - an unmatched prose item,
   or a unit the numeric layer will not decide, is the judge's call, never a confident
   wrong from this layer.
5. Symbolic. The two sides normalized into plain mathematical notation and compared for
   equality in the bounded SymPy subprocess (`backend/tools/cas.py`). SymPy is never
   imported here; the subprocess is the boundary that keeps an evaluating parser out of
   the request process.
6. Constrained judge. One small JSON call against the configured tutor endpoint with the
   question, its hidden grading contract, the reference answer, and the student's words.
   The verdict is mapped onto the three outcomes the quiz flow can show, and an
   `incorrect` that the judge itself does not state confidently becomes `uncertain`.

The contract's `answer_kind` controls which typed layers may settle the answer: a
`text` answer is never settled by unit, set, or algebra comparison, and a legacy
question with no contract infers carefully, layer by layer.

Every layer that cannot decide returns no verdict rather than a wrong one, and the
fallback is `uncertain`: a recorded "not confidently right, not confidently wrong" that
the interface renders neutrally. The raw response is stored beside the verdict by the
route, so an uncertain or failed grading is never an irreversible wrong answer.
"""

import asyncio
import hashlib
import json
import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from backend.llm import client
from backend.llm.budget import generation_reserve
from backend.llm.turn_budget import input_ceiling
from backend.rag.tokens import estimate_tokens
from backend.tools import cas

if TYPE_CHECKING:
    from pint import Quantity, UnitRegistry

    from backend.core.app_settings import TutorConfig

logger = logging.getLogger(__name__)

# The version of the grading contract this code applies. Bump it when a verdict-semantics
# change makes stored results no longer comparable to fresh ones: a resubmitted answer is
# then regraded against the new contract instead of replaying the old result.
# Version 2: the set layer no longer settles an unmatched declared-set prose member as
# incorrect (PLA-496 F1), and it no longer lets a greedy item order settle a tolerance
# match that a different assignment satisfies; version-1 verdicts regrade on
# resubmission instead of replaying a wrong the new layers would not have made.
# Version 3: the numeric layer no longer credits a fixed base-unit absolute allowance
# (PLA-496 R1) - a tiny answer is held to the same relative tolerance as a large one, an
# exact zero is an exact match, and one side zero is a full-scale relative mismatch
# against the other; version-2 verdicts regrade on resubmission instead of replaying a
# credit the current layers would not have made.
# Version 4: the numeric comparison holds the question's tolerance inclusively
# (PLA-496 R2) - an answer exactly `rel_tol` away from the reference receives credit,
# in either operand order, where the version-3 normalized difference rounded an exact
# boundary just outside itself (100 versus 99 at one percent); version-3 verdicts
# regrade on resubmission instead of replaying a boundary false negative the current
# comparison would not make.
GRADING_VERSION = 4

VERDICT_CORRECT = "correct"
VERDICT_INCORRECT = "incorrect"
VERDICT_UNCERTAIN = "uncertain"

# The default relative tolerance for numeric answers: one percent, which accepts the
# rounding a student does by hand to two significant figures. A question's rubric may
# tighten or relax it, within a bound that keeps a tolerance from becoming "anything in
# the neighborhood counts".
DEFAULT_RELATIVE_TOLERANCE = 1e-2
MAX_RUBRIC_TOLERANCE = 5e-2

# The judge is a single small call; the wall-clock bound is set for an interactive answer,
# not a background generation.
JUDGE_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
JUDGE_MAX_TOKENS = 512

# The judge's own five grades, before they are mapped onto the quiz flow's three outcomes.
JUDGE_VERDICTS: frozenset[str] = frozenset(
    ("correct", "mostly_correct", "partially_correct", "incorrect", "uncertain")
)
# A judge that calls an answer wrong without at least this much confidence has not
# cleared the bar the whole layer exists for: no confident false negatives.
JUDGE_INCORRECT_CONFIDENCE_FLOOR = 0.6

# A typed response is bounded before any layer touches it, so a paste cannot force a
# long parse or a long prompt.
MAX_RESPONSE_CHARS = 2000

_GRADING_FIELDS: frozenset[str] = frozenset(
    (
        "answer_kind",
        "tolerance",
        "units",
        "acceptable_alternatives",
        "required_ideas",
        "common_misconceptions",
        "contradictions",
        "partial_understanding_accepted",
    )
)
_ANSWER_KINDS: frozenset[str] = frozenset(("numeric", "symbolic", "set", "text"))


@dataclass(frozen=True)
class GradingResult:
    """The outcome of grading one answer: a verdict the quiz flow can act on, and the
    bounded internal detail the route stores beside it (never shown to the student)."""

    verdict: str
    detail: dict[str, object] = field(default_factory=dict)

    @property
    def correct(self) -> bool:
        return self.verdict == VERDICT_CORRECT

    @property
    def uncertain(self) -> bool:
        return self.verdict == VERDICT_UNCERTAIN


def question_digest(content: str) -> str:
    """A short fingerprint of the question payload a result was graded against.

    The stored detail carries it so the rubric and reference answer that applied to a
    stored verdict can be told apart from a regenerated question.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The grading contract the generator writes with a question
# ---------------------------------------------------------------------------


def parse_rubric(raw: object) -> dict[str, object] | None:
    """The question's hidden grading contract, or None when it carries none.

    The model's output is a proposal: every field is revalidated here, and anything
    malformed is dropped rather than trusted. A question with no contract at all still
    grades - the layers run against the reference answer alone, and the judge is asked
    to weigh the response against it.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None
    if not raw:
        return None

    kind = raw.get("answer_kind")
    if kind is not None and kind not in _ANSWER_KINDS:
        return None
    tolerance = raw.get("tolerance")
    if tolerance is not None and (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or not (0 < float(tolerance) <= MAX_RUBRIC_TOLERANCE)
    ):
        return None
    units = raw.get("units")
    if units is not None and (not isinstance(units, str) or not units.strip() or len(units) > 100):
        return None
    lists = {}
    for key in (
        "acceptable_alternatives",
        "required_ideas",
        "common_misconceptions",
        "contradictions",
    ):
        value = raw.get(key, [])
        if not isinstance(value, list) or len(value) > 8:
            return None
        items: list[str] = []
        for entry in value:
            if not isinstance(entry, str) or not entry.strip() or len(entry) > 300:
                return None
            items.append(entry.strip())
        lists[key] = items
    partial = raw.get("partial_understanding_accepted")
    if partial is not None and not isinstance(partial, bool):
        return None
    return {
        "answer_kind": kind if kind in _ANSWER_KINDS else None,
        "tolerance": float(tolerance) if tolerance is not None else None,
        "units": units.strip() if isinstance(units, str) else None,
        "acceptable_alternatives": lists["acceptable_alternatives"],
        "required_ideas": lists["required_ideas"],
        "common_misconceptions": lists["common_misconceptions"],
        "contradictions": lists["contradictions"],
        "partial_understanding_accepted": bool(partial),
    }


def tolerance_for(rubric: dict[str, object] | None) -> float:
    """The relative tolerance one grading pass runs with."""
    if rubric is not None:
        value = rubric.get("tolerance")
        if isinstance(value, float):
            return value
    return DEFAULT_RELATIVE_TOLERANCE


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_text(value: str) -> str:
    """Whitespace, case, and trailing sentence punctuation folded away.

    The fast path for trivial equivalence: `Photosynthesis.` and `photosynthesis` are the
    same answer, `2pi/Ts.` and `2pi/Ts` are the same expression. Deliberately conservative
    - nothing here reinterprets anything, so a miss here costs only the layers below.
    """
    collapsed = " ".join((value or "").casefold().split())
    return collapsed.rstrip(".?!:;, ")


_DIGIT = re.compile(r"[0-9]")


def _trivially_equivalent(left: str, right: str) -> bool:
    """The trivial-equivalence fast path, prose-safe but math-aware.

    Identical words are always the same answer. Beyond that, the text layer folds
    whitespace, case, and sentence punctuation - but only where folding cannot erase
    meaning. It refuses to run on anything with a digit (a value, a unit, a factorial:
    `1 mW` and `1 MW` are not the same answer, neither are `3!` and `3`), any operator
    or mathematical name (a symbol, a function, a constant), or a lone short identifier
    where case distinguishes symbols - `x` from `X`, `Na` from `na`. Those texts go to
    the typed layers, which compare case-sensitively, or to the judge. Benign prose
    keeps its forgiving comparison.
    """
    left_raw = (left or "").strip()
    right_raw = (right or "").strip()
    if not left_raw or not right_raw:
        return False
    if left_raw == right_raw:
        return True
    for raw in (left_raw, right_raw):
        if _DIGIT.search(raw) or _looks_like_math(raw):
            return False
        if " " not in raw and len(raw) <= 2:
            # A single short word is an identifier in math, where case carries meaning.
            return False
    return normalize_text(left_raw) != "" and normalize_text(left_raw) == normalize_text(right_raw)


# Unicode a student or a KaTeX-writing model reaches for, mapped to the ASCII notation
# the algebra subprocess and the unit parser read.
_UNICODE_MATH: dict[str, str] = {
    "π": "pi",
    "θ": "theta",
    "φ": "phi",
    "α": "alpha",
    "β": "beta",
    "γ": "gamma",
    "δ": "delta",
    "ε": "epsilon",
    "λ": "lambda",
    "μ": "mu",
    "ν": "nu",
    "ω": "omega",
    "σ": "sigma",
    "ρ": "rho",
    "τ": "tau",
    "ζ": "zeta",
    "ξ": "xi",
    "χ": "chi",
    "ψ": "psi",
    "Δ": "Delta",
    "Σ": "Sigma",
    "Ω": "Omega",
    "∇": "nabla",
    "√": "sqrt",
    "×": "*",
    "÷": "/",
    "−": "-",
    "⁄": "/",
    "·": "*",
    "∞": "oo",
    "≈": "=",
    "≠": "!=",
    "≤": "<=",
    "≥": ">=",
}

# LaTeX a model is told to use, reduced to the plain notation both downstream parsers
# accept. Applied in rounds so a nested `\frac{\sqrt{a}}{b}` resolves.
_LATEX_PASSES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\\left|\\right"), ""),
    (re.compile(r"\\[ ]"), " "),
    (re.compile(r"\\[,;:!]"), " "),
    (re.compile(r"\\%"), "%"),
    (re.compile(r"\\cdot|\\times"), "*"),
    (re.compile(r"\\div"), "/"),
    (re.compile(r"\\pi"), "pi"),
    (re.compile(r"\\infty"), "oo"),
    (re.compile(r"\\sqrt\{([^{}]*)\}"), r"sqrt(\1)"),
    (re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}"), r"(\1)/(\2)"),
    (re.compile(r"\\(begin|end)\{[^{}]*\}"), ""),
)

# What a quiz answer may carry in. Longer than this, no layer will run with it: the
# algebra subprocess caps its own input, and a longer paste belongs to the judge, which
# bounds its prompt by its own budget.
_MATH_INPUT_CAP = 2000
_CAS_EXPRESSION_CAP = 2000

# `T_s` and `Ts` name the same quantity in every answer this grader sees; subscripts are
# folded into the symbol name rather than read as syntax.
# Names the algebra subprocess calls functions, so `cos T` is an application and the
# operand is wrapped rather than multiplied.
_FUNCTIONS = (
    "asin|acos|atan|acot|asinh|acosh|atanh|sinh|cosh|tanh|csc|sec|cot"
    "|sin|cos|tan|exp|log|ln|sqrt|cbrt|abs|sign|floor|ceiling|erf|erfc|gamma|factorial"
)
_FUNCTION_OPERAND = re.compile(rf"\b({_FUNCTIONS})\s+([A-Za-z][A-Za-z0-9]*)")
# Names the subprocess binds to constants: `pi(t)` is pi times t, not an application.
_CONSTANT_PRODUCT = re.compile(r"\b(pi|E|I|oo|zoo|nan)\s*(?=\()")
# A letter or a digit following a digit is scientific notation, not a product: `1e5` is
# never `1*e*5`. The `\s*` is part of the match, so `2 pi` becomes `2*pi`, not `2 *pi`.
_DIGIT_PRODUCT = re.compile(r"(?<=\d)\s*(?=[A-DF-Za-df-z(])")
_CLOSE_PRODUCT = re.compile(r"\)\s*(?=[A-Za-z0-9(])")
# A whitespace product of names fires only when at least one side is multi-letter:
# `T cos` is a product (cos is three letters), but `T s` is a subscripted name that lost
# its underscore - splitting it would read T times s where the student meant T sub s.
# Two single letters with no space between them is the same name, for the same reason.
_NAME_PRODUCT_LEFT = re.compile(r"(?<=[A-Za-z][A-Za-z])\s+(?=[A-Za-z])")
_NAME_PRODUCT_RIGHT = re.compile(r"(?<=[A-Za-z])\s+(?=[A-Za-z][A-Za-z])")


def _insert_multiplication(text: str) -> str:
    """Put the `*` back where mathematical notation leaves it out.

    The algebra subprocess refuses implicit multiplication out loud, so `2pi` has to
    arrive as `2*pi`. The insertions are the unambiguous cases: a digit times a name, a
    closing paren times the next factor, a constant times a parenthesised factor, and a
    whitespace-separated product of names. A name next to another name with no space is
    one symbol (`Ts`), and the parser's own conventions keep it that way.
    """
    for _ in range(2):
        # A function name followed by a bare operand is an application: `cos T` becomes
        # `cos(T)`, so the name-product rule does not read it as `cos*T`. The second
        # round catches a product the wrapping itself made reachable (`cos(T) x`).
        text = _FUNCTION_OPERAND.sub(r"\1(\2)", text)
        text = _DIGIT_PRODUCT.sub("*", text)
        text = _CLOSE_PRODUCT.sub(")*", text)
        text = _CONSTANT_PRODUCT.sub(r"\1*", text)
        text = _NAME_PRODUCT_LEFT.sub("*", text)
        text = _NAME_PRODUCT_RIGHT.sub("*", text)
    return text


def normalize_math(text: str) -> str | None:
    """One answer's notation, reduced to the plain form both typed parsers read.

    Returns None when the text cannot be reduced to something the parsers will accept:
    empty, over the cap, or still carrying characters outside the mathematical allowlist.
    A None is an abstention - the layer below it (or the judge) decides, never a guess.
    """
    s = (text or "").strip()
    if not s or len(s) > _MATH_INPUT_CAP:
        return None
    # Dollar math delimiters, whichever pair the model used. A dollar sign has no place
    # inside a quiz answer's math, so a bare one comes out with them.
    s = s.replace("$", "")
    for char, replacement in _UNICODE_MATH.items():
        s = s.replace(char, replacement)
    for _ in range(4):
        for pattern, replacement in _LATEX_PASSES:
            s = pattern.sub(replacement, s)
    s = s.replace("_", "")
    s = _insert_multiplication(s)
    s = re.sub(r"\s+", "", s)
    if not s or len(s) > _CAS_EXPRESSION_CAP:
        return None
    if not re.fullmatch(r"[A-Za-z0-9+\-*/^().,=<>!% ]*", s):
        return None
    return s


# ---------------------------------------------------------------------------
# Numeric layer
# ---------------------------------------------------------------------------

# Pint's own gates, restated for a second caller: a length cap, a character allowlist that
# ends at unit notation, and a ceiling on any literal exponent, because pint evaluates
# whitelisted arithmetic in-process (see backend/tools/units.py for the reasoning).
_QUANTITY_CAP = 500
_QUANTITY_CHARS = re.compile(r"^[A-Za-z0-9_+\-*/^().% \t]*$")
_QUANTITY_EXPONENT = re.compile(r"(?:\*\*|\^)\s*\(?\s*(\d+)")
_MAX_EXPONENT = 1000

# A bare name is not a number. The ones that are: substituted, so `pi` can be checked
# against `3.14159`. Anything else (a variable, a unit name) leaves the numeric layer.
_CONSTANT_VALUES: dict[str, str] = {
    "pi": "3.141592653589793",
    "E": "2.718281828459045",
    "e": "2.718281828459045",
}
_BARE_NAME = re.compile(r"^[A-Za-z_]+$")

_registry: "UnitRegistry | None" = None


def _unit_registry() -> "UnitRegistry":
    """The shared unit registry, built once. Two registries would make quantities
    incomparable, which is a failure no caller can read."""
    global _registry
    if _registry is None:
        from pint import UnitRegistry

        _registry = UnitRegistry()
    return _registry


def _numeric_text(text: str) -> str | None:
    """The text as the unit parser should read it, or None when it is not a number.

    A bare name is only a number when it is one of the constants students mean: `pi`,
    `e`, `E`. A bare `omega` or `Ts` parses as a unit of magnitude one in pint, and
    equating that to `1` would be a wrong answer the layer invented, so bare names that
    are not constants leave the numeric layer entirely.
    """
    t = (text or "").strip()
    if _BARE_NAME.fullmatch(t):
        return _CONSTANT_VALUES.get(t)
    return t or None


def _finite_float(magnitude: object) -> float | None:
    """The magnitude as a finite float, or None when it cannot be reported honestly."""
    if isinstance(magnitude, bool) or not isinstance(magnitude, int | float):
        return None
    if isinstance(magnitude, int) and abs(magnitude).bit_length() > 1024:
        return None
    try:
        value = float(magnitude)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


_POWER_LITERAL = re.compile(r"^[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")


def _safe_powers(text: str) -> bool:
    """Whether every power in the text is a bare literal raised to a bare literal.

    Pint evaluates whitelisted arithmetic in the parent process, and a power whose base
    or exponent is anything but a plain number - `2**(1000*1000*1000)`, `2**1000**1000`,
    `(2**1000)**1000`, `x**2` - could force an astronomical allocation before the
    finite-magnitude check below ever runs. Such texts leave the numeric layer; the
    bounded algebra subprocess grades them under its own limits, or the judge does.
    """
    t = (text or "").replace("^", "**")
    for match in re.finditer(r"\*\*", t):
        # The exponent run: everything after the operator up to the next operator. A
        # second `**` inside it is a chained power - `2**1000**1000` is `2**(1000**1000)`.
        end = match.end()
        while end < len(t) and t[end] not in "+-*/(":
            end += 1
        if t[end : end + 2] == "**":
            return False
        if not _POWER_LITERAL.match(t[match.end() : end].strip()):
            return False
        # The base run: everything before the operator, up to the previous operator.
        start = match.start() - 1
        while start >= 0 and t[start] not in "+-*/)":
            start -= 1
        if not _POWER_LITERAL.match(t[start + 1 : match.start()].strip()):
            return False
    return True


def _quantity(text: str) -> "Quantity | None":
    """One text as a pint quantity, or None when it is not a quantity worth checking.

    Pint's parser whitelists arithmetic but evaluates it in-process, so the same gates
    `backend/tools/units.py` applies stand in front of it here: a length cap, a character
    allowlist that ends at unit notation, powers limited to a literal raised to a
    literal, and a ceiling on any literal exponent.
    """
    t = (text or "").strip()
    if not t or len(t) > _QUANTITY_CAP or "__" in t:
        return None
    if not _QUANTITY_CHARS.match(t):
        return None
    if not _safe_powers(t):
        return None
    if any(int(exponent) > _MAX_EXPONENT for exponent in _QUANTITY_EXPONENT.findall(t)):
        return None
    try:
        return _unit_registry().parse_expression(t.replace("^", "**"))
    except Exception:
        return None


def _numeric_value(text: str) -> tuple[float, str] | None:
    """The answer's magnitude in base units with its dimensionality, or None."""
    source = _numeric_text(text)
    if source is None:
        return None
    quantity = _quantity(source)
    if quantity is None:
        return None
    base = quantity.to_base_units()
    value = _finite_float(getattr(base, "magnitude", None))
    if value is None:
        return None
    return value, str(base.dimensionality)


def _numeric_verdict(canonical: str, response: str, rel_tol: float) -> str | None:
    """Whether two numeric answers agree in base units within the tolerance.

    A dimensionality mismatch - `4.2 Hz` against `4.2` - is a None, not a verdict:
    whether an omitted unit is acceptable is the judge's call against the rubric, not
    dimensional analysis'.

    The check is relative to the larger magnitude, at the tolerance the question sets:
    a small answer is held to the same relative standard as a large one, so `1 pF` is
    not within one percent of `500 pF` although the two differ by less than any fixed
    base-unit allowance would suggest. The check is `math.isclose` at exactly that
    tolerance and no absolute allowance. This avoids the additional rounding introduced
    by separately normalizing the operands; the comparison is symmetric and two exact
    zeros match. Parsing and base-unit conversion still use floating point, so quantities
    extremely close to a boundary retain those representation limits.
    """
    left = _numeric_value(canonical)
    right = _numeric_value(response)
    if left is None or right is None:
        return None
    left_value, left_dimension = left
    right_value, right_dimension = right
    if left_dimension != right_dimension:
        return None
    # The tolerance is evaluated against the larger magnitude by the standard
    # relative check itself: a separate normalization step (divide both sides, then
    # difference) can round an exact boundary just outside itself, so the boundary
    # the question set is the boundary the comparison holds.
    if math.isclose(left_value, right_value, rel_tol=rel_tol, abs_tol=0.0):
        return VERDICT_CORRECT
    return VERDICT_INCORRECT


# ---------------------------------------------------------------------------
# Set and list layer
# ---------------------------------------------------------------------------

_SET_SEPARATORS = re.compile(r"[,;|]")


def _split_items(text: str) -> list[str] | None:
    """The items one answer lists, or None when the text is not a list.

    A slash is never a separator: `1/2` is a fraction, and a list of fractions is carried
    in brackets. Whether a different item count means a wrong answer or a judge's call
    is the caller's: this only reads the items.
    """
    t = (text or "").strip()
    if not t or len(t) > _MATH_INPUT_CAP:
        return None
    if t.startswith("[") and t.endswith("]"):
        t = t[1:-1]
    if not _SET_SEPARATORS.search(t):
        return None
    items = [item.strip() for item in _SET_SEPARATORS.split(t) if item.strip()]
    return items or None


_RELATION_EQUIVALENT = "equivalent"
_RELATION_DIFFERENT = "different"
_RELATION_UNKNOWN = "unknown"


def _item_relation(left: str, right: str, rel_tol: float) -> str:
    """How one listed item stands against the other.

    `equivalent` - settled by a deterministic layer: trivially the same text, the same
    mathematics in another notation, or numerically the same value within the tolerance.
    `different` - the numeric layer has settled the values as decisively not the same
    (a mismatch beyond the tolerance, in the same dimensionality). `unknown` - nothing
    deterministic can tell them apart: a paraphrase, a domain synonym, or a value whose
    units the numeric layer deliberately will not decide. Unknown is an abstention, not
    a guess: that comparison belongs to the judge.

    The trivial comparison is the math-aware one: item text that carries units, digits,
    or symbols is never case-folded into equivalence.
    """
    if _trivially_equivalent(left, right):
        return _RELATION_EQUIVALENT
    left_math, right_math = normalize_math(left), normalize_math(right)
    if left_math is not None and left_math == right_math:
        return _RELATION_EQUIVALENT
    verdict = _numeric_verdict(left, right, rel_tol)
    if verdict == VERDICT_CORRECT:
        return _RELATION_EQUIVALENT
    if verdict == VERDICT_INCORRECT:
        return _RELATION_DIFFERENT
    return _RELATION_UNKNOWN


def _item_equivalent(left: str, right: str, rel_tol: float) -> bool:
    """Whether one listed item is the other, in any of the deterministic layers."""
    return _item_relation(left, right, rel_tol) == _RELATION_EQUIVALENT


def _all_numeric(items: list[str]) -> bool:
    return all(_numeric_value(item) is not None for item in items)


# The assignment search runs on lists up to this many items; beyond it the layer
# abstains rather than pay for a search its bounds no longer keep honest.
_COMPLETE_MATCHING_CAP = 32


def _complete_equivalent_matching(relations: list[list[str]]) -> bool:
    """Whether every required item can take a distinct equivalent response item.

    One augmenting-path search (Kuhn's algorithm) over the equivalence edges: a greedy
    first pass can steal a partner that a later item is the only equivalent of, so the
    question is the assignment, not any single item's neighbors. The caller bounds the
    input - this runs only on lists of at most `_COMPLETE_MATCHING_CAP` items, where the
    edge count stays trivial.
    """
    taken: dict[int, int] = {}  # response index -> required index

    def augment(index: int, seen: set[int]) -> bool:
        for partner, relation in enumerate(relations[index]):
            if relation != _RELATION_EQUIVALENT or partner in seen:
                continue
            seen.add(partner)
            if partner not in taken or augment(taken[partner], seen):
                taken[partner] = index
                return True
        return False

    return all(augment(index, set()) for index in range(len(relations)))


def _set_verdict(canonical: str, response: str, rel_tol: float, *, unordered: bool) -> str | None:
    """Comparison of two listed answers, complete membership only.

    A complete deterministic match - every required item equivalent to a distinct
    offered item - settles right for an all-numeric list or under a genuine
    unordered-set contract, where membership *is* the idea and reordering is accepted;
    the match is an assignment, so a greedy order that steals a later item's only
    partner cannot settle the answer wrong. A list settles wrong only where the
    mismatch is settled mathematics: a cardinality difference in an all-numeric list,
    or an all-numeric list whose members cannot be assigned to distinct offered
    equivalents - and even there a pair the numeric layer will not decide (an omitted
    unit, say) leaves the call to the judge. Everything else - a paraphrase, a
    synonym, an extra or missing idea in a different form - abstains against the
    contract, never a confident wrong from this layer.
    """
    canonical_items = _split_items(canonical)
    response_items = _split_items(response)
    if canonical_items is None or response_items is None:
        return None
    all_numeric = _all_numeric(canonical_items) and _all_numeric(response_items)
    decisive = all_numeric or unordered
    if len(canonical_items) != len(response_items):
        # An extra or a missing listed item: mathematics where every item is a number,
        # the judge's call where a prose idea may travel in a different form or count.
        return VERDICT_INCORRECT if all_numeric else None
    used: set[int] = set()
    for canonical_item in canonical_items:
        for index, response_item in enumerate(response_items):
            if index in used:
                continue
            if _item_equivalent(canonical_item, response_item, rel_tol):
                used.add(index)
                break
        else:
            # A required item has no equivalent among the *unused* response items. The
            # failure may be the greedy order's own doing, so the assignment is asked.
            if len(canonical_items) <= _COMPLETE_MATCHING_CAP:
                matrix = [
                    [_item_relation(required, offered, rel_tol) for offered in response_items]
                    for required in canonical_items
                ]
                if _complete_equivalent_matching(matrix):
                    # A different assignment satisfies the whole set: the greedy
                    # order just stole this item's partner. Complete membership is
                    # shown, so the answer settles as the all-matched case does.
                    return VERDICT_CORRECT if decisive else None
                # No assignment satisfies the set. It settles wrong only where the
                # mismatch is settled mathematics: every item a number and no pair the
                # numeric layer will not decide - an omitted unit is the judge's call,
                # as is any paraphrase or synonym.
                if all_numeric and all(
                    relation != _RELATION_UNKNOWN for row in matrix for relation in row
                ):
                    return VERDICT_INCORRECT
                return None
            # Beyond the cap the search is not worth it: if an equivalent partner
            # exists at all (a used one), a different order might satisfy the set, so
            # abstain; only a decisive mismatch against every offered item settles.
            item_relations = [
                _item_relation(canonical_item, response_item, rel_tol)
                for response_item in response_items
            ]
            if _RELATION_EQUIVALENT in item_relations:
                return None
            if all_numeric and all(relation == _RELATION_DIFFERENT for relation in item_relations):
                return VERDICT_INCORRECT
            return None
    # Every required item matched, with none left over. For an all-numeric list or a
    # declared set that is the answer; for a prose list with no set contract, whether
    # the order and the exact wording matter is the judge's call.
    return VERDICT_CORRECT if decisive else None


# ---------------------------------------------------------------------------
# Symbolic layer
# ---------------------------------------------------------------------------

_MATH_SIGNAL = re.compile(
    r"[+\-*/^=<>!()_~%]|\\(?:frac|sqrt|pi|infty|cdot|times|div|left|right|begin|end)\b"
    r"|\b(?:pi|sqrt|exp|log|ln|sin|cos|tan|csc|sec|cot|asin|acos|atan|sinh|cosh|tanh"
    r"|abs|factorial|gamma|erf|oo|inf|E|I)\b"
    r"|[\u03b1-\u03c9\u0391-\u03a9\u2211\u2212\u221a\u2248\u2260\u2264\u2265\u221e]"
)


# A normalized expression only counts as mathematical content when it carries a number
# or an operator: a product of bare words is prose the normalizer happened to parse.
_MATH_CONTENT = re.compile(r"[0-9+\-*/^=<>!%()]")


def _looks_like_math(text: str) -> bool:
    """Whether an answer carries any sign it is mathematical rather than prose.

    The symbolic layer runs when either side does: `0.628` against `2*pi/10` compares
    fine in the algebra subprocess. Prose against prose never does - `the Krebs cycle`
    normalizes into a product of three free symbols, and equating two such products
    would be a match the layer invented.
    """
    return bool(_MATH_SIGNAL.search(text or ""))


def _symbolic_verdict(canonical: str, response: str) -> str | None:
    """Whether two mathematical answers are equivalent expressions, via the bounded
    SymPy subprocess.

    The runner's `certain` is the contract: equal-and-settled is correct,
    different-and-shown is incorrect, and not settled is a None - the judge gets the
    call, because `simplify` failing to reach zero is not proof of inequality.
    """
    if not (_looks_like_math(canonical) or _looks_like_math(response)):
        return None
    canonical_math = normalize_math(canonical)
    response_math = normalize_math(response)
    if canonical_math is None or response_math is None:
        return None
    if not (_MATH_CONTENT.search(canonical) and _MATH_CONTENT.search(response)):
        # One side is bare words with no number and no operator in the raw text - prose
        # the normalizer happened to parse (its implicit multiplications would otherwise
        # masquerade as mathematical content). Equating `the carbon oxidation cycle`
        # with `2*pi/Ts` is a verdict the algebra invented, not a proof: the judge
        # decides.
        return None
    if canonical_math == response_math:
        return VERDICT_CORRECT
    result = cas.evaluate(response_math, canonical_math)
    if not result.ok:
        return None
    equal = bool(result.value.get("equal"))
    certain = bool(result.value.get("certain"))
    if equal and certain:
        return VERDICT_CORRECT
    if not equal and certain:
        return VERDICT_INCORRECT
    return None


# ---------------------------------------------------------------------------
# The constrained judge
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM = """\
You grade one free-response answer on a study quiz. Judge substance, not wording:
equivalent phrasings, notation, units, and formatting of the same idea are correct, and
an answer that contains a stated contradiction is incorrect no matter what else it says.
Keyword matching alone is never enough. When you cannot tell, say uncertain: an uncertain
call costs nothing, and a confident wrong call teaches the student the wrong lesson.

The question may carry a hidden grading contract: the ideas that make an answer right,
acceptable alternative phrasings, supporting details, common misconceptions, and
contradictions. Judge the student's answer against that contract when it is present, and
against the reference answer and its explanation otherwise.

Reply with one JSON object and nothing else, with exactly three keys:
"verdict" - one of the five grades named below, "confidence" - a number from 0.0 to 1.0,
"reason" - one short sentence.

Verdict meanings: correct - substantively right; mostly_correct - every required idea is
present, only minor detail missing; partially_correct - some but not all of the required
ideas; incorrect - wrong, or contradicted; uncertain - not enough evidence to decide.
"""

JUDGE_SCHEMA = client.JsonSchema(
    name="answer_judgment",
    schema={
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": [
                    "correct",
                    "mostly_correct",
                    "partially_correct",
                    "incorrect",
                    "uncertain",
                ],
            },
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["verdict", "confidence", "reason"],
        "additionalProperties": False,
    },
)


def _judge_messages(
    question: str, rubric: dict[str, object] | None, reference: str, response: str
) -> list[dict[str, str]]:
    contract = json.dumps(rubric, ensure_ascii=False, sort_keys=True) if rubric else "none"
    user = (
        f"Question: {question}\n"
        f"Grading contract: {contract}\n"
        f"Reference answer: {reference}\n"
        f"Student's answer: {response}\n"
        "Return the JSON verdict."
    )
    return [
        {"role": "system", "content": _JUDGE_SYSTEM},
        {"role": "user", "content": user},
    ]


def _map_judge_verdict(
    verdict: str, confidence: float, *, partial_accepted: bool
) -> tuple[str, str | None]:
    """The judge's five grades onto the quiz flow's three outcomes.

    `partially_correct` is `uncertain`, not `incorrect`: the student showed part of the
    understanding, and the flow's honest rendering of that is a neutral comparison, not a
    confident wrong - unless the question's contract says partial understanding is
    accepted, in which case it is credit. An `incorrect` below the confidence floor is
    the same abstention.
    """
    if verdict in ("correct", "mostly_correct"):
        return VERDICT_CORRECT, None
    if verdict == "partially_correct":
        if partial_accepted:
            return VERDICT_CORRECT, "partial understanding accepted"
        return VERDICT_UNCERTAIN, "partially correct"
    if verdict == "uncertain":
        return VERDICT_UNCERTAIN, None
    if confidence < JUDGE_INCORRECT_CONFIDENCE_FLOOR:
        return VERDICT_UNCERTAIN, "incorrect, low confidence"
    return VERDICT_INCORRECT, None


def judge_free_response(
    config: "TutorConfig",
    *,
    question: str,
    rubric: dict[str, object] | None,
    reference: str,
    response: str,
) -> GradingResult | None:
    """One constrained judgment of one response, or None when it cannot be made.

    None covers every failure mode - no room in the context window, a refused or
    truncated call, an unreadable reply, a verdict outside the contract - and the caller
    turns a None into `uncertain` rather than into a wrong answer.
    """
    messages = _judge_messages(question, rubric, reference, response)
    ceiling = input_ceiling(config.context_window, generation_reserve(config.context_window))
    if sum(estimate_tokens(str(message["content"])) for message in messages) > ceiling:
        logger.info("Answer judge skipped: prompt does not fit the configured window")
        return None
    try:
        reply = asyncio.run(
            client.complete(
                config.endpoint_url,
                config.api_key,
                config.model,
                messages,
                temperature=client.DETERMINISTIC_TEMPERATURE,
                schema=JUDGE_SCHEMA,
                max_tokens=JUDGE_MAX_TOKENS,
                request_timeout=JUDGE_TIMEOUT,
                fail_on_truncation=True,
            )
        )
        payload: object = json.loads(reply)
    except Exception:
        logger.debug("Answer judge failed", exc_info=True)
        return None
    if not isinstance(payload, dict):
        return None
    verdict = payload.get("verdict")
    if not isinstance(verdict, str) or verdict not in JUDGE_VERDICTS:
        return None
    # The confidence is load-bearing: an `incorrect` near the floor becomes an abstention,
    # so a missing, boolean, NaN, or out-of-range value is a malformed reply, not a
    # number to coerce.
    raw_confidence = payload.get("confidence")
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
        return None
    confidence = float(raw_confidence)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None
    reason = str(payload.get("reason") or "")[:300]
    partial_accepted = bool(rubric.get("partial_understanding_accepted")) if rubric else False
    mapped, note = _map_judge_verdict(verdict, confidence, partial_accepted=partial_accepted)
    detail: dict[str, object] = {
        "grader": "judge",
        "judge_verdict": verdict,
        "confidence": confidence,
        "reason": reason,
    }
    if note:
        detail["note"] = note
    return GradingResult(mapped, detail)


# ---------------------------------------------------------------------------
# The layered pass
# ---------------------------------------------------------------------------


def grade_choice(question: dict[str, Any], selected_index: int) -> GradingResult:
    """A multiple-choice or true/false answer: the stored index is the whole answer."""
    try:
        correct = selected_index == int(question["correct_index"])
    except (KeyError, TypeError, ValueError):
        correct = False
    return GradingResult(VERDICT_CORRECT if correct else VERDICT_INCORRECT, {"grader": "choice"})


def grade_free_response(
    question: dict[str, Any],
    response: str,
    *,
    judge: Callable[..., GradingResult | None] | None = None,
) -> GradingResult:
    """Grade one free-response answer, cheapest settled layer first.

    The question carries the reference answer (the one option a fill-blank holds) and,
    when its generator wrote one, the hidden grading contract. A layer that returns a
    verdict ends the pass; a layer that returns None hands to the next. The judge, when
    provided, is the last word - and a judge that cannot be reached, or a pass that no
    layer settles, lands on `uncertain`, never on a confident wrong.
    """
    options = question.get("options")
    reference = str(options[0]) if isinstance(options, list) and options else ""
    rubric = parse_rubric(question.get("grading"))
    rel_tol = tolerance_for(rubric)
    kind = rubric.get("answer_kind") if rubric is not None else None

    if _trivially_equivalent(response, reference):
        return GradingResult(VERDICT_CORRECT, {"grader": "text"})

    # Rubric alternatives are equivalent forms the generator wrote down with the
    # question: trust them, compared with the same care a typed layer brings.
    if rubric is not None:
        for alternative in rubric.get("acceptable_alternatives") or []:
            if isinstance(alternative, str) and _item_equivalent(response, alternative, rel_tol):
                return GradingResult(VERDICT_CORRECT, {"grader": "alternative"})

    # The declared kind controls which typed layers may run: a `text` answer is never
    # settled by unit, set, or algebra comparison; a question without a contract
    # (legacy) infers carefully, layer by layer.
    if kind in (None, "numeric", "symbolic", "set"):
        verdict = _numeric_verdict(reference, response, rel_tol)
        if verdict is not None:
            return GradingResult(verdict, {"grader": "numeric"})
    if kind in (None, "set"):
        verdict = _set_verdict(reference, response, rel_tol, unordered=kind == "set")
        if verdict is not None:
            return GradingResult(verdict, {"grader": "set"})
    if kind in (None, "symbolic"):
        verdict = _symbolic_verdict(reference, response)
        if verdict is not None:
            return GradingResult(verdict, {"grader": "symbolic"})

    if judge is not None:
        try:
            result = judge(
                question=str(question.get("question") or ""),
                rubric=rubric,
                reference=reference,
                response=response,
            )
        except Exception:
            # The judge is a last word, not a dependency: any failure lands on
            # `uncertain` exactly as a missing judge does.
            logger.debug("Answer judge failed", exc_info=True)
            result = None
        if result is not None:
            return result
    return GradingResult(VERDICT_UNCERTAIN, {"grader": "fallback", "reason": "no layer settled"})
