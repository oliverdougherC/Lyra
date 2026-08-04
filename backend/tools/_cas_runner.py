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
   builtins module. Any other name becomes a free symbol.
4. Resources. CPU time and, where the platform enforces it, address space.

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

# The only names an expression may resolve to. Deliberately small and deliberately
# mathematical: `integrate`, `diff`, `solve`, and the rest of SymPy's verbs are absent,
# because those are operations this runner exposes as named tools rather than things an
# expression string gets to invoke.
_ALLOWED_NAMES: tuple[str, ...] = (
    # Constants
    "pi", "E", "I", "oo", "zoo", "nan", "GoldenRatio", "EulerGamma",
    # Trigonometric and inverse trigonometric
    "sin", "cos", "tan", "cot", "sec", "csc",
    "asin", "acos", "atan", "acot", "atan2",
    # Hyperbolic
    "sinh", "cosh", "tanh", "coth", "asinh", "acosh", "atanh",
    # Elementary
    "exp", "log", "sqrt", "cbrt", "Abs", "sign", "floor", "ceiling",
    # Combinatorial and special
    "factorial", "binomial", "gamma", "beta", "erf", "erfc",
    # Structural
    "Min", "Max", "Rational", "Integer", "Float", "Symbol", "Piecewise",
    "re", "im", "conjugate", "arg", "Sum", "Product",
)  # fmt: skip


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


def _allowed_names() -> dict[str, object]:
    """Resolve `_ALLOWED_NAMES` against SymPy, with nothing else reachable."""
    import sympy

    allowed: dict[str, object] = {name: getattr(sympy, name) for name in _ALLOWED_NAMES}
    # `ln` is what a student writes and what a model emits; SymPy only knows `log`.
    allowed["ln"] = sympy.log
    # Without this key, `eval` injects the real builtins module into the globals it is
    # given. This is the single most important line in the file.
    allowed["__builtins__"] = {}
    return allowed


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
    return stripped


def _parse(text: str, label: str = "expression") -> object:
    """Parse one expression under every gate above."""
    from sympy.parsing.sympy_parser import (
        convert_xor,
        implicit_multiplication_application,
        parse_expr,
        standard_transformations,
    )

    checked = _check(text, label)
    transformations = (
        *standard_transformations,
        implicit_multiplication_application,
        # `^` is what a student types for a power and what a model echoes back.
        convert_xor,
    )
    try:
        return parse_expr(
            checked,
            global_dict=_allowed_names(),
            local_dict={},
            transformations=transformations,
            evaluate=True,
        )
    except Exception as exc:
        raise ValueError(f"Could not read the {label}: {type(exc).__name__}") from exc


def _parse_equation(text: str) -> object:
    """Parse `lhs = rhs` into an equation, or a bare expression as `expression = 0`."""
    import sympy

    if text.count("=") > 1:
        raise ValueError("An equation may contain at most one equals sign.")
    if "=" not in text:
        return sympy.Eq(_parse(text, "equation"), 0)
    left, right = text.split("=", 1)
    return sympy.Eq(_parse(left, "left side"), _parse(right, "right side"))


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


def _op_evaluate(arguments: dict[str, object]) -> dict[str, object]:
    """Simplify an expression, or test whether two expressions are equal."""
    import sympy

    expression = _parse(str(arguments.get("expression", "")))
    compare_to = arguments.get("compare_to")
    if compare_to in (None, ""):
        return {"simplified": str(sympy.simplify(expression))}

    other = _parse(str(compare_to), "comparison expression")
    difference = sympy.simplify(expression - other)
    # `simplify` returning something other than zero is not proof of inequality: it may
    # simply not have found the identity. `equal` is reported as unknown rather than
    # false in that case, because a check that guesses is worse than one that abstains.
    equal = difference == 0
    return {
        "equal": bool(equal),
        "difference": str(difference),
        "certain": bool(equal) or bool(sympy.simplify(sympy.nsimplify(difference)) != 0),
    }


def _op_solve(arguments: dict[str, object]) -> dict[str, object]:
    """Solve one equation or a system for named unknowns."""
    import sympy

    given = _listing(arguments.get("equations"), "equations")
    equations = [_parse_equation(text) for text in given]
    unknowns = _symbols(arguments.get("unknowns"))
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

    expression = _parse(str(arguments.get("expression", "")))
    variable = sympy.Symbol(_check(str(arguments.get("variable", "")), "variable"))
    lower, upper = arguments.get("lower"), arguments.get("upper")
    if lower in (None, "") or upper in (None, ""):
        return {"result": str(sympy.integrate(expression, variable)), "definite": False}
    bounds = (_parse(str(lower), "lower bound"), _parse(str(upper), "upper bound"))
    return {
        "result": str(sympy.integrate(expression, (variable, *bounds))),
        "definite": True,
    }


def _op_differentiate(arguments: dict[str, object]) -> dict[str, object]:
    """Differentiate to a given order, which covers partial derivatives."""
    import sympy

    expression = _parse(str(arguments.get("expression", "")))
    variable = sympy.Symbol(_check(str(arguments.get("variable", "")), "variable"))
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
