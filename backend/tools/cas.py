"""Computer algebra, behind a bounded subprocess.

This is the deterministic half of solution verification, and the single highest-value
check available for the mathematics Lyra is best at. It is deterministic on purpose: a
second model opinion is not a check, because models ratify their own work.

Every call starts a short-lived child process (`_cas_runner`), hands it one JSON request,
and reads one JSON response. SymPy is never imported into the backend process. The reasons
are stacked and each is sufficient on its own:

- SymPy's parser evaluates, and the expressions come from a model that has read an
  untrusted upload.
- SymPy can genuinely hang on a hard integral, and a hung worker is a hung solver.
- A runaway expansion can exhaust memory, and it should cost one check rather than the
  backend.

The cost is process startup plus a SymPy import, on the order of a fifth of a second. The
model call that produced the claim being checked takes seconds to minutes on local
hardware, so this is not the part to optimize. If it ever becomes one, the fix is a
long-lived pool of runners, not moving SymPy into this process.

See `_cas_runner` for the four gates applied before anything is evaluated, and for the
honest limits of what they are worth.
"""

import json
import subprocess
import sys
from pathlib import Path

from backend.tools.result import ToolResult, failure, success

# Wall-clock ceiling per call. The runner also caps CPU seconds, which catches a child
# spinning inside a C extension; this catches one that is stuck any other way.
TIMEOUT_SECONDS = 15

_RUNNER_MODULE = "backend.tools._cas_runner"
# The repository root, so the child can import `backend` whichever directory it was
# started from and whether or not the package is installed.
_IMPORT_ROOT = Path(__file__).resolve().parents[2]

_TIMEOUT_ERROR = "That computation took too long and was stopped."
_CRASHED_ERROR = "That computation could not be completed."
_UNREADABLE_ERROR = "The computation returned a result that could not be read."

LINALG_OPERATIONS: tuple[str, ...] = (
    "determinant",
    "inverse",
    "eigenvalues",
    "rank",
    "solve",
)


def _run(
    operation: str, arguments: dict[str, object], timeout: float = TIMEOUT_SECONDS
) -> ToolResult:
    """Run one operation in the child and return its result.

    Every failure mode becomes a `ToolResult` rather than an exception, because a check
    that could not run is information the verifier acts on: it reports the claim as
    unchecked rather than treating silence as agreement.
    """
    payload = json.dumps({"operation": operation, "arguments": arguments})
    try:
        completed = subprocess.run(  # noqa: S603
            # `sys.executable` is an absolute interpreter path and the module name is a
            # module constant. No part of this argument vector comes from the model; the
            # model's input travels on stdin, where it cannot become an argument.
            [sys.executable, "-m", _RUNNER_MODULE],
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=_IMPORT_ROOT,
        )
    except subprocess.TimeoutExpired:
        return failure(_TIMEOUT_ERROR)
    except OSError:
        return failure(_CRASHED_ERROR)

    if completed.returncode != 0 or not completed.stdout.strip():
        # A negative return code is the child killed by a signal, which is what hitting
        # the CPU limit looks like from here.
        return failure(_TIMEOUT_ERROR if completed.returncode < 0 else _CRASHED_ERROR)

    try:
        response = json.loads(completed.stdout)
    except ValueError:
        return failure(_UNREADABLE_ERROR)
    if not isinstance(response, dict):
        return failure(_UNREADABLE_ERROR)
    if not response.get("ok"):
        return failure(str(response.get("error") or _CRASHED_ERROR))
    value = response.get("value")
    if not isinstance(value, dict):
        return failure(_UNREADABLE_ERROR)
    return success(**value)


def evaluate(expression: str, compare_to: str | None = None) -> ToolResult:
    """Simplify an expression, or test whether two expressions are equal.

    Args:
        expression: The expression to simplify or compare.
        compare_to: A second expression. When given, the check is whether the two are
            equal rather than what the first one simplifies to.

    Returns:
        `simplified` for a one-sided call. For a comparison, `equal`, the simplified
        `difference`, and `certain`, which is False when simplification could not settle
        it either way. A check that guesses is worse than one that abstains, so a caller
        must treat `equal: false, certain: false` as unresolved rather than as a
        refutation.
    """
    return _run("evaluate", {"expression": expression, "compare_to": compare_to})


def solve(equations: list[str], unknowns: list[str]) -> ToolResult:
    """Solve an equation or a system for named unknowns.

    Args:
        equations: Each as `lhs = rhs`, or as a bare expression meaning `expression = 0`.
        unknowns: Symbol names to solve for.

    Returns:
        `solutions`, a list of mappings from unknown to value. An empty list means the
        system has no solution, which is a result rather than a failure.
    """
    return _run("solve", {"equations": equations, "unknowns": unknowns})


def integrate(
    expression: str,
    variable: str,
    lower: str | None = None,
    upper: str | None = None,
) -> ToolResult:
    """Integrate in one variable, definitely when both bounds are given.

    Returns:
        `result` and `definite`. SymPy returns an unevaluated `Integral` for an integral
        it cannot do, which is reported as it stands rather than dressed up as an answer.
    """
    return _run(
        "integrate",
        {"expression": expression, "variable": variable, "lower": lower, "upper": upper},
    )


def differentiate(expression: str, variable: str, order: int = 1) -> ToolResult:
    """Differentiate to a given order, which is also how a partial derivative is taken."""
    return _run(
        "differentiate",
        {"expression": expression, "variable": variable, "order": order},
    )


def linalg(operation: str, matrix: list[list[str]], vector: list[str] | None = None) -> ToolResult:
    """Determinant, inverse, eigenvalues, rank, or a linear solve.

    Args:
        operation: One of `LINALG_OPERATIONS`.
        matrix: Rows of expression strings. Entries may be symbolic.
        vector: Right-hand side, required only by `solve`.

    Returns:
        `result`, shaped per operation. A singular matrix asked for its inverse returns a
        null result and a note, rather than an error: it is a fact about the matrix.
    """
    return _run("linalg", {"operation": operation, "matrix": matrix, "vector": vector})
