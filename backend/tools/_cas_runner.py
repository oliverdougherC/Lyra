"""The child process that actually runs SymPy. Never imported by the backend.

`backend/tools/cas.py` starts this with `python -m backend.tools._cas_runner`, writes one
JSON request to stdin, and reads one JSON response from stdout. It is a separate process
on purpose: SymPy's parser evaluates, the expressions come from a model that has read an
untrusted upload, and SymPy can genuinely hang on a hard integral. A crash, a hang, or a
runaway allocation here costs one verification rather than the backend.

Four gates apply before anything is evaluated, in this order:

1. Length. An expression above the cap is refused unparsed.
2. Characters. Input must match a mathematical-notation allowlist, and may not contain a
   double underscore. That is what keeps dunder attribute paths out of the evaluator.
3. Names. `parse_expr` is given an explicit `global_dict` holding only the mathematical
   functions below, with `__builtins__` set empty so `eval` cannot re-inject the real
   builtins module. Any other name becomes a free symbol, or an undefined function where
   it is applied to something, which computes nothing either way.
4. Resources. CPU time and, where the platform enforces it, address space.

One rule sits above those four and outranks them: **this runner may refuse, but it may
never quietly answer a different question than it was asked.** It is the tool a checker
uses to catch wrong answers, so an expression read as something other than what was written
does not cost a check, it produces a wrong verdict on a student's work. That is why
implicit multiplication is not accepted (see `_parse`) and why `certain` in `_op_evaluate`
is what it is. Refusals here are meant to be readable and specific, so the caller can fix
its input and ask again.

This is defense in depth, not a sandbox, and it is not claimed to be one. The real
boundary is Phase 4's work, gated behind the threat model that architecture.md requires
before any tool touches the filesystem. What holds today is that this process computes
and does nothing else, it is short-lived and bounded, and it holds no handles to the
database, the network, or the user's files.
"""

import json
import re
import sys

# The response is written to stdout, so nothing else may ever print there.
MAX_EXPRESSION_CHARS = 2000
MAX_ITEMS = 24

# CPU seconds. The parent also kills on a wall-clock timeout; this is the backstop for a
# child that is spinning rather than sleeping.
CPU_SECONDS = 20
# 2 GB. Generous for SymPy, low enough that a runaway expansion dies rather than swaps.
ADDRESS_SPACE_BYTES = 2 * 1024 * 1024 * 1024

# Mathematical notation only. No colon, no semicolon, no quote, no backslash, no dot
# outside a decimal (the dot is allowed, but `__` is refused below, which is what makes
# attribute traversal to a dunder useless).
_SAFE_CHARACTERS = re.compile(r"^[A-Za-z0-9_+\-*/^().,=<>! \t]*$")

# The only names an expression may resolve to.
#
# `integrate`, `diff` and `limit` are here rather than only as named tools. The named tools
# cannot express the question a checker actually asks, which is not "what is this integral"
# but "does this integral equal the closed form the student wrote": that needs the integral
# and its comparison in one call, and `cas_evaluate` is the only operation with a
# `compare_to`. Every one of them is a pure SymPy computation on a parsed expression, so
# reaching them through an expression is no more reachable than the tool that already wraps
# them. `solve` stays out: it takes unknowns and returns a mapping, which is a tool result
# rather than an expression.
_ALLOWED_NAMES: tuple[str, ...] = (
    # Constants
    "pi", "E", "I", "oo", "zoo", "nan", "GoldenRatio", "EulerGamma",
    # Trigonometric and inverse trigonometric
    "sin", "cos", "tan", "cot", "sec", "csc",
    "asin", "acos", "atan", "acot", "atan2", "sinc",
    # Hyperbolic
    "sinh", "cosh", "tanh", "coth", "asinh", "acosh", "atanh",
    # Elementary
    "exp", "log", "sqrt", "cbrt", "Abs", "sign", "floor", "ceiling",
    # Combinatorial and special
    "factorial", "binomial", "gamma", "beta", "erf", "erfc",
    # Signals: the unit step and the impulse. Nothing in a continuous-signals course can be
    # checked without them, and every convolution, impulse response, and sampling identity
    # is written in terms of one or the other.
    "Heaviside", "DiracDelta",
    # Calculus, for the comparison case above.
    "integrate", "diff", "limit", "expand", "factor", "simplify",
    # Structural
    "Min", "Max", "Rational", "Integer", "Float", "Number", "Symbol", "Function", "Piecewise",
    "re", "im", "conjugate", "arg", "Sum", "Product",
)  # fmt: skip

# What a student or a model writes, against what SymPy calls it. `Symbol`, `Function` and
# `Number` above are not for an author to type: the parser's own transformations emit them
# for a bare name, an applied unknown name, and a literal, and without them in scope an
# expression as ordinary as `x1` or `h(t)` dies with a bare `NameError`.
_ALIASES: dict[str, str] = {
    "ln": "log",
    "abs": "Abs",
    # Electrical engineering writes the imaginary unit `j`, and this whole tool exists for a
    # signals course.
    "j": "I",
    # `u(t)` is the unit step in every signals text there is. Binding the name costs the use
    # of a bare `u` as a variable, which is the right way round: an expression that meant `u`
    # as a symbol fails loudly here, where one that meant the step would otherwise have been
    # checked against an undefined function and quietly agreed with anything.
    "u": "Heaviside",
    "step": "Heaviside",
    "delta": "DiracDelta",
    "Dirac": "DiracDelta",
    "impulse": "DiracDelta",
}

# Every spelling of infinity a caller has been seen to write. Missing one is not a failed
# parse but a wrong answer: an unbound `infinity` is a perfectly good free symbol, so the
# integral evaluates to a bound it was never given and comes back looking like an answer.
_ALIASES.update(dict.fromkeys(("inf", "infty", "infinity", "Infinity", "INF"), "oo"))

# `and`, `or` and `not` are Python's, not SymPy's, and an expression built on them is a
# question this operation cannot answer. Caught by name so the refusal can say what to do
# instead, rather than surfacing as the bare `SyntaxError` that says nothing.
_BOOLEAN_OPERATOR = re.compile(r"(?<![A-Za-z0-9_])(and|or|not)(?![A-Za-z0-9_])")

# A number against a name or a bracket, which is a product with the `*` left out. `4j` and
# `1j` are excluded: those are Python's own complex literals, which parse correctly and are
# how an engineering checker writes the imaginary unit.
_IMPLICIT_PRODUCT = re.compile(r"[0-9](?!j(?![A-Za-z0-9_]))\s*[A-Za-z(]")


def _limit_resources() -> None:
    """Cap CPU and address space where the platform supports it.

    Applied in the child rather than through `preexec_fn`, which is documented as unsafe
    in a threaded parent, and the solver runs on a worker thread.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows has no resource module
        return
    for name, limit in (("RLIMIT_CPU", CPU_SECONDS), ("RLIMIT_AS", ADDRESS_SPACE_BYTES)):
        which = getattr(resource, name, None)
        if which is None:
            continue
        try:
            resource.setrlimit(which, (limit, limit))
        except (ValueError, OSError):
            # macOS does not enforce RLIMIT_AS. The parent's wall-clock timeout still
            # holds, so a platform that refuses a limit is not a reason to refuse to run.
            continue


def _flat_integrate(expression: object, *rest: object) -> object:
    """`integrate`, accepting the flat bounds a model writes as well as SymPy's own tuple.

    SymPy spells a definite integral `integrate(f, (t, 0, oo))`. Every checker measured
    against a real signals set wrote `integrate(f, t, 0, oo)` instead, which is not a
    mistake so much as the shape of `cas_integrate`, the tool it has already been handed:
    expression, variable, lower, upper. Refusing it taught the model nothing and cost the
    check, so both spellings are read here.

    Three trailing arguments beginning with a symbol are read as a variable and its bounds.
    That is formally ambiguous with a triple indefinite integral, `integrate(f, x, y, z)`,
    and the ambiguity is resolved toward the definite reading deliberately: one of the two
    turns up in every Fourier transform on the sheet and the other turns up in none of them.
    """
    import sympy

    if len(rest) == 3 and isinstance(rest[0], sympy.Symbol):
        return sympy.integrate(expression, (rest[0], rest[1], rest[2]))
    return sympy.integrate(expression, *rest)


def _allowed_names(reserved: tuple[str, ...] = ()) -> dict[str, object]:
    """Resolve `_ALLOWED_NAMES` against SymPy, with nothing else reachable.

    Args:
        reserved: Names the caller has declared as its own variable or unknown. These are
            removed, so they parse as plain symbols. Without it, `integrate(exp(u), u)`
            read `u` as the unit step in the integrand and as a symbol in the variable,
            and returned an answer to neither question. A caller that names a variable has
            said what that letter means, and it outranks anything this file thinks.
    """
    import sympy

    allowed: dict[str, object] = {name: getattr(sympy, name) for name in _ALLOWED_NAMES}
    allowed.update({alias: allowed[target] for alias, target in _ALIASES.items()})
    allowed["integrate"] = _flat_integrate
    for name in reserved:
        allowed.pop(name, None)
    # Without this key, `eval` injects the real builtins module into the globals it is
    # given. This is the single most important line in the file.
    allowed["__builtins__"] = {}
    return allowed


def _why(exc: Exception, text: str) -> str:
    """Why a parse failed, in terms the caller can act on.

    The type name alone was all this reported, and `Could not read the expression:
    NameError` gives a checker nothing to retry differently. A `NameError` message is the
    offending name and nothing else, and a `SyntaxError` quotes the input, so both are the
    model's own text coming back rather than anything about this machine. Every other type
    keeps the name only, because those messages are SymPy's and undocumented.

    The implicit-multiplication hint is the one piece of real advice available: it is the
    single most likely reason a plainly mathematical expression will not parse now that the
    parser insists the `*` be written.
    """
    if isinstance(exc, SyntaxError | TypeError) and _IMPLICIT_PRODUCT.search(text):
        return (
            f"{type(exc).__name__}: write multiplication explicitly, as `2*x` rather than "
            "`2x` and `3*(x + 1)` rather than `3(x + 1)`"
        )
    if isinstance(exc, NameError | SyntaxError):
        detail = str(exc).split("(<string>")[0].strip().rstrip(".")
        return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return type(exc).__name__


def _check(text: str, label: str) -> str:
    """Refuse anything that is not plainly mathematical notation."""
    if not isinstance(text, str):
        raise ValueError(f"{label} must be text.")
    stripped = text.strip()
    if not stripped:
        raise ValueError(f"{label} is empty.")
    if len(stripped) > MAX_EXPRESSION_CHARS:
        raise ValueError(f"{label} is longer than {MAX_EXPRESSION_CHARS} characters.")
    if "__" in stripped:
        raise ValueError(f"{label} contains a double underscore.")
    if not _SAFE_CHARACTERS.match(stripped):
        raise ValueError(f"{label} contains characters that are not mathematical notation.")
    if _BOOLEAN_OPERATOR.search(stripped):
        raise ValueError(
            f"{label} uses `and`, `or` or `not`, which this operation cannot evaluate. "
            "Check one comparison at a time, using `compare_to` to test two expressions "
            "for equality."
        )
    return stripped


def _parse(text: str, label: str = "expression", reserved: tuple[str, ...] = ()) -> object:
    """Parse one expression under every gate above.

    Multiplication must be written. `implicit_multiplication_application`, which was used
    here, bought `2x` and `sin x` at a price this tool cannot pay: it split any unlisted
    name into its letters, so `abs(t)` parsed as `a*b*s*t` and `foo(t)` as `f*o**2*t`, and
    inserting the `*` before an opening bracket meant `h(t)` parsed as `h*t`. All of it
    silent. A checker handed a silently different expression can agree with working that
    says something else, and a wrong answer from the one tool whose whole job is catching
    wrong answers is worse than no answer at all.

    So the transformations are the standard set and `convert_xor`, nothing more, and the
    two things that buys are worth more than what it costs. `h(t)`, `x(t - t0)` and `y(t)`
    become undefined functions, which is the notation every linearity, time-invariance and
    impulse-response argument in this material is written in. And what is now refused --
    `2x`, `3(x+1)` -- is refused out loud, with `_why` telling the caller to write the
    multiplication, rather than quietly becoming something else.
    """
    from sympy.parsing.sympy_parser import (
        convert_xor,
        parse_expr,
        standard_transformations,
    )

    checked = _check(text, label)
    transformations = (
        *standard_transformations,
        # `^` is what a student types for a power and what a model echoes back.
        convert_xor,
    )
    try:
        return parse_expr(
            checked,
            global_dict=_allowed_names(reserved),
            local_dict={},
            transformations=transformations,
            evaluate=True,
        )
    except Exception as exc:
        raise ValueError(f"Could not read the {label}: {_why(exc, checked)}") from exc


def _parse_equation(text: str, reserved: tuple[str, ...] = ()) -> object:
    """Parse `lhs = rhs` into an equation, or a bare expression as `expression = 0`."""
    import sympy

    if text.count("=") > 1:
        raise ValueError("An equation may contain at most one equals sign.")
    if "=" not in text:
        return sympy.Eq(_parse(text, "equation", reserved), 0)
    left, right = text.split("=", 1)
    return sympy.Eq(_parse(left, "left side", reserved), _parse(right, "right side", reserved))


def _symbols(names: object) -> list[object]:
    """Parse the unknowns to solve for, which must be plain symbols."""
    import sympy

    if not isinstance(names, list) or not names:
        raise ValueError("Give at least one unknown to solve for.")
    if len(names) > MAX_ITEMS:
        raise ValueError(f"At most {MAX_ITEMS} unknowns.")
    return [sympy.Symbol(_check(str(name), "unknown")) for name in names]


def _listing(value: object, label: str) -> list[str]:
    """Read a JSON list of strings, bounded."""
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list.")
    if len(value) > MAX_ITEMS:
        raise ValueError(f"{label} holds more than {MAX_ITEMS} entries.")
    return [str(item) for item in value]


def _matrix(rows: object) -> object:
    """Parse a matrix given as a list of rows of expression strings."""
    import sympy

    if not isinstance(rows, list) or not rows:
        raise ValueError("The matrix must be a non-empty list of rows.")
    if len(rows) > MAX_ITEMS:
        raise ValueError(f"At most {MAX_ITEMS} rows.")
    parsed = []
    for row in rows:
        entries = _listing(row, "Each matrix row")
        parsed.append([_parse(entry, "matrix entry") for entry in entries])
    widths = {len(row) for row in parsed}
    if len(widths) != 1:
        raise ValueError("Every row of the matrix must have the same length.")
    return sympy.Matrix(parsed)


# Where a difference is sampled to see whether it is really nonzero. Positive and rational,
# because the parameters in this material are magnitudes and rates: a negative sample walks
# into the branch cut of a `sqrt` or a `log` and reports a difference that is the sample's
# fault. Awkward values rather than round ones, so an expression does not sit on a root of
# its own by luck.
_SAMPLE_ROUNDS: tuple[tuple[int, int], ...] = ((3, 7), (5, 3), (11, 4), (2, 9))

# Below this a sampled difference counts as zero. `evalf` carries far more precision than
# this, so the gap between "cancelled" and "genuinely small" is not a close call.
_ZERO_TOLERANCE = 1e-9

# The heads SymPy leaves in place when it could not decide. A difference still holding one
# of these has not been evaluated, so nothing may be concluded from its shape.
_UNDECIDED_HEADS = ("Integral", "Sum", "Product", "Limit", "Piecewise", "Derivative")


def _sampled_nonzero(difference: object) -> bool:
    """Whether the difference can be *shown* to be nonzero by evaluating it.

    This is the whole basis of `certain`, so it is written to abstain rather than to
    conclude. A round that comes back at zero, or an expression SymPy left unevaluated, or
    anything that raises, all mean the same thing: not shown. Only a difference that
    evaluated cleanly and came back away from zero every time it was asked earns a `True`.
    """
    import sympy

    if not isinstance(difference, sympy.Basic):
        return False
    if any(difference.has(getattr(sympy, head)) for head in _UNDECIDED_HEADS):
        # An unevaluated integral is SymPy reporting that it did not settle this. Reading
        # the leftover as a nonzero difference is how a correct Fourier pair gets refuted.
        return False

    symbols = sorted(difference.free_symbols, key=str)
    if len(symbols) > 6:
        return False

    evaluated = 0
    for numerator, denominator in _SAMPLE_ROUNDS:
        point = sympy.Rational(numerator, denominator)
        try:
            value = complex(
                difference.subs(
                    {symbol: point * (index + 1) for index, symbol in enumerate(symbols)}
                ).evalf()
            )
        except (TypeError, ValueError, ZeroDivisionError, AttributeError):
            continue
        if value != value or abs(value) == float("inf"):  # NaN or a pole at this sample
            continue
        if abs(value) <= _ZERO_TOLERANCE:
            # It vanished here, so the two may well be equal and simplify merely missed it.
            return False
        evaluated += 1
        if not symbols:
            break

    return evaluated > 0


def _op_evaluate(arguments: dict[str, object]) -> dict[str, object]:
    """Simplify an expression, or test whether two expressions are equal.

    The three outcomes are equal, certainly different, and not settled, and the third is
    reported as itself. `simplify` failing to reach zero is not proof of inequality: it is
    routinely what happens to a true identity written two ways, and every definite integral
    SymPy declines to evaluate leaves a difference that is not zero and means nothing.
    Reporting those as a certain difference is how the one tool whose job is catching wrong
    answers came to call correct ones wrong.
    """
    import sympy

    expression = _parse(str(arguments.get("expression", "")))
    compare_to = arguments.get("compare_to")
    if compare_to in (None, ""):
        return {"simplified": str(sympy.simplify(expression))}

    other = _parse(str(compare_to), "comparison expression")
    difference = sympy.simplify(expression - other)
    equal = bool(difference == 0)
    certain = equal or _sampled_nonzero(difference)
    result: dict[str, object] = {
        "equal": equal,
        "difference": str(difference),
        "certain": certain,
    }
    if not certain:
        result["note"] = (
            "Not settled either way: the difference did not reduce to zero, and could not "
            "be shown to be nonzero either. This is not a disagreement, and the two "
            "expressions may well be equal."
        )
    return result


def _op_solve(arguments: dict[str, object]) -> dict[str, object]:
    """Solve one equation or a system for named unknowns."""
    import sympy

    given = _listing(arguments.get("equations"), "equations")
    unknowns = _symbols(arguments.get("unknowns"))
    reserved = tuple(str(symbol) for symbol in unknowns)
    equations = [_parse_equation(text, reserved) for text in given]
    solutions = sympy.solve(equations, unknowns, dict=True)
    return {
        "solutions": [
            {str(symbol): str(value) for symbol, value in solution.items()}
            for solution in solutions
        ]
    }


def _op_integrate(arguments: dict[str, object]) -> dict[str, object]:
    """Definite or indefinite integration in one variable."""
    import sympy

    name = _check(str(arguments.get("variable", "")), "variable")
    variable = sympy.Symbol(name)
    expression = _parse(str(arguments.get("expression", "")), reserved=(name,))
    lower, upper = arguments.get("lower"), arguments.get("upper")
    if lower in (None, "") or upper in (None, ""):
        return {"result": str(sympy.integrate(expression, variable)), "definite": False}
    bounds = (
        _parse(str(lower), "lower bound", reserved=(name,)),
        _parse(str(upper), "upper bound", reserved=(name,)),
    )
    return {
        "result": str(sympy.integrate(expression, (variable, *bounds))),
        "definite": True,
    }


def _op_differentiate(arguments: dict[str, object]) -> dict[str, object]:
    """Differentiate to a given order, which covers partial derivatives."""
    import sympy

    name = _check(str(arguments.get("variable", "")), "variable")
    variable = sympy.Symbol(name)
    expression = _parse(str(arguments.get("expression", "")), reserved=(name,))
    order = arguments.get("order", 1)
    if not isinstance(order, int) or not 1 <= order <= 10:
        raise ValueError("The order must be a whole number between 1 and 10.")
    return {"result": str(sympy.diff(expression, variable, order))}


def _op_linalg(arguments: dict[str, object]) -> dict[str, object]:
    """Determinant, inverse, eigenvalues, rank, or a linear solve."""
    import sympy

    operation = str(arguments.get("operation", ""))
    matrix = _matrix(arguments.get("matrix"))

    if operation == "determinant":
        return {"result": str(matrix.det())}
    if operation == "rank":
        return {"result": str(matrix.rank())}
    if operation == "inverse":
        if matrix.rows != matrix.cols:
            raise ValueError("Only a square matrix has an inverse.")
        if matrix.det() == 0:
            return {"result": None, "note": "The matrix is singular, so it has no inverse."}
        return {"result": str(matrix.inv().tolist())}
    if operation == "eigenvalues":
        if matrix.rows != matrix.cols:
            raise ValueError("Only a square matrix has eigenvalues.")
        return {"result": {str(value): count for value, count in matrix.eigenvals().items()}}
    if operation == "solve":
        entries = _listing(arguments.get("vector"), "vector")
        vector = sympy.Matrix([_parse(entry, "vector entry") for entry in entries])
        return {"result": str(matrix.solve(vector).tolist())}
    raise ValueError(f"Unknown linear algebra operation: {operation}")


_OPERATIONS = {
    "evaluate": _op_evaluate,
    "solve": _op_solve,
    "integrate": _op_integrate,
    "differentiate": _op_differentiate,
    "linalg": _op_linalg,
}


def main() -> int:
    """Read one request, run it, write one response. Never raises past this frame."""
    _limit_resources()
    try:
        request = json.loads(sys.stdin.read())
        operation = _OPERATIONS.get(str(request.get("operation", "")))
        if operation is None:
            raise ValueError(f"Unknown operation: {request.get('operation')}")
        arguments = request.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("Arguments must be an object.")
        response: dict[str, object] = {"ok": True, "value": operation(arguments)}
    except ValueError as exc:
        # The refusals above, all written to be read by the model.
        response = {"ok": False, "error": str(exc)}
    except MemoryError:
        response = {"ok": False, "error": "That computation needed more memory than allowed."}
    except RecursionError:
        response = {"ok": False, "error": "That expression nests too deeply to evaluate."}
    except Exception as exc:
        # SymPy raises a wide and undocumented set. The type name is useful to the model
        # and carries nothing about this machine; the message might, so it is dropped.
        response = {"ok": False, "error": f"The computation failed: {type(exc).__name__}"}
    sys.stdout.write(json.dumps(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
