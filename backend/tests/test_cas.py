"""Computer algebra and dimensional analysis, against known results.

This is the one place in Lyra where the expected value of a computation can be asserted
exactly, so it is. The containment tests matter as much as the arithmetic: this is the
first path on which model output reaches an evaluator.
"""

import subprocess

import pytest

from backend.tools import _cas_runner, cas, units

# The expression that reliably outruns the ceilings: `simplify` on a seven-term multinomial
# raised to the fortieth power takes tens of CPU seconds. Used for both timeout tests so
# neither depends on how fast the machine running them is.
_EXPENSIVE = "(a + b + c + d + e + f + g)**40"


def test_indefinite_and_definite_integration() -> None:
    indefinite = cas.integrate("x^2", "x")
    definite = cas.integrate("x**2", "x", "0", "2")

    assert indefinite.value == {"result": "x**3/3", "definite": False}
    assert definite.value == {"result": "8/3", "definite": True}


def test_differentiation_to_a_given_order() -> None:
    first = cas.differentiate("sin(x)*x", "x")
    second = cas.differentiate("x**3", "x", order=2)

    assert first.value["result"] == "x*cos(x) + sin(x)"
    assert second.value["result"] == "6*x"


def test_an_identity_is_confirmed_and_an_error_is_caught() -> None:
    identity = cas.evaluate("sin(x)**2 + cos(x)**2", "1")
    wrong = cas.evaluate("x + 1", "x + 2")

    assert identity.value["equal"] is True
    assert wrong.value["equal"] is False
    # A refutation the caller acts on has to be certain. An uncertain one is unresolved.
    assert wrong.value["certain"] is True


def test_solving_one_equation_and_a_system() -> None:
    quadratic = cas.solve(["x**2 - 4 = 0"], ["x"])
    system = cas.solve(["x + y = 3", "x - y = 1"], ["x", "y"])

    assert quadratic.value["solutions"] == [{"x": "-2"}, {"x": "2"}]
    assert system.value["solutions"] == [{"x": "2", "y": "1"}]


def test_an_equation_with_no_solution_is_a_result_not_a_failure() -> None:
    result = cas.solve(["x = x + 1"], ["x"])

    assert result.ok is True
    assert result.value["solutions"] == []


def test_linear_algebra_operations() -> None:
    assert cas.linalg("determinant", [["1", "2"], ["3", "4"]]).value["result"] == "-2"
    assert cas.linalg("rank", [["1", "2"], ["2", "4"]]).value["result"] == "1"
    assert cas.linalg("eigenvalues", [["2", "0"], ["0", "3"]]).value["result"] == {"2": 1, "3": 1}
    assert (
        cas.linalg("solve", [["1", "1"], ["1", "-1"]], ["3", "1"]).value["result"] == "[[2], [1]]"
    )


def test_a_singular_matrix_reports_itself_rather_than_failing() -> None:
    result = cas.linalg("inverse", [["1", "2"], ["2", "4"]])

    # Having no inverse is a fact about the matrix, not a fault in the check.
    assert result.ok is True
    assert result.value["result"] is None
    assert "singular" in str(result.value["note"])


def test_ln_is_accepted_because_that_is_what_people_write() -> None:
    assert cas.evaluate("ln(E)").value["simplified"] == "1"


def test_an_unknown_operation_is_refused() -> None:
    result = cas.linalg("transpose", [["1"]])

    assert result.ok is False
    assert "transpose" in result.error


@pytest.mark.parametrize(
    "attack",
    [
        "__import__('os').system('id')",
        "x.__class__.__mro__",
        "open('/etc/passwd')",
        "exec('import os')",
        "lambda: 1",
        "x; import os",
        "'a' * 10",
    ],
)
def test_code_shaped_input_never_reaches_the_evaluator(attack: str) -> None:
    result = cas.evaluate(attack)

    # Refused by the character allowlist or the double-underscore ban, before parsing.
    assert result.ok is False
    assert "double underscore" in result.error or "not mathematical notation" in result.error


def test_unknown_names_become_symbols_rather_than_calls() -> None:
    # `eval` and `chr` are not in the allowed namespace, so they parse as free symbols and
    # multiply out. This is the behaviour that makes the empty `__builtins__` load-bearing:
    # a name that is not on the list resolves to nothing callable.
    result = cas.evaluate("eval(chr(105))")

    assert result.ok is True
    assert result.value["simplified"] == "105*a*c*e*h*l*r*v"


def test_an_over_long_expression_is_refused_before_parsing() -> None:
    result = cas.evaluate("x" * (_cas_runner.MAX_EXPRESSION_CHARS + 1))

    assert result.ok is False
    assert "longer than" in result.error


def test_a_malformed_expression_comes_back_as_a_result() -> None:
    result = cas.evaluate("3 +* 4")

    # A tool error is a result, not an exception: the model can try a different form.
    assert result.ok is False
    assert result.error


def test_a_slow_computation_is_stopped_by_the_wall_clock() -> None:
    result = cas._run("evaluate", {"expression": _EXPENSIVE}, timeout=1)

    assert result.ok is False
    assert result.error == cas._TIMEOUT_ERROR


def test_a_child_killed_by_a_signal_is_reported_as_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # What hitting the runner's own CPU limit looks like from the parent: a negative
    # return code and no output. Exercised directly rather than by actually burning
    # twenty seconds of CPU, which is what it takes to trip the real limit.
    killed = subprocess.CompletedProcess(args=[], returncode=-24, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: killed)

    result = cas.evaluate("x + 1")

    assert result.ok is False
    assert result.error == cas._TIMEOUT_ERROR


def test_an_unreadable_child_response_does_not_look_like_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    garbled = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: garbled)

    result = cas.evaluate("x + 1")

    assert result.ok is False
    assert result.error == cas._UNREADABLE_ERROR


def test_dimensions_match_across_different_units() -> None:
    result = units.check("1000 m", "km")

    # Dimensions, not units. A check that called these different would fire on every
    # correct solution written in another unit.
    assert result.value["matches"] is True


def test_a_wrong_dimension_is_caught() -> None:
    result = units.check("9.8 m/s^2", "N")

    assert result.value["matches"] is False
    assert result.value["dimensionality"] == "[length] / [time] ** 2"
    assert result.value["expected_dimensionality"] == "[mass] * [length] / [time] ** 2"


def test_units_compose_through_arithmetic() -> None:
    result = units.check("(2 * 5 kg) * 3 m/s^2", "N")

    assert result.value["matches"] is True
    assert result.value["magnitude"] == 30.0


def test_a_bare_number_is_dimensionless_rather_than_an_error() -> None:
    result = units.check("42", "dimensionless")

    assert result.ok is True
    assert result.value["matches"] is True


@pytest.mark.parametrize(
    ("expression", "expected", "fragment"),
    [
        ("__import__('os')", "m", "double underscore"),
        ("open('x')", "m", "not a unit expression"),
        ("3 flurbles", "m", "Could not read"),
        ("3 m", "notaunit", "Not a unit"),
    ],
)
def test_unit_input_is_gated_and_failures_explain_themselves(
    expression: str, expected: str, fragment: str
) -> None:
    result = units.check(expression, expected)

    assert result.ok is False
    assert fragment in result.error
