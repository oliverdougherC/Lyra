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


def test_unknown_names_resolve_to_symbols_that_compute_nothing() -> None:
    # `eval` and `chr` are not in the allowed namespace, so they parse as an undefined
    # function applied to another one: a SymPy expression tree that holds the names and
    # evaluates neither. This is what the empty `__builtins__` buys, and it is unchanged by
    # the parser no longer splitting names into letters. What matters is not the shape the
    # expression takes but that nothing on this machine is reachable through it.
    result = cas.evaluate("eval(chr(105))")

    assert result.ok is True
    assert result.value["simplified"] == "eval(chr(105))"


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


# The notation of a continuous-signals course, which is what this tool is used on. Every
# one of these came off a real solve where the check failed and the student was told their
# work had not been checked. `u` and `delta` were the reported failure: with no unit step
# and no impulse, nothing in a convolution or impulse-response problem can be settled.
@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("u(t - tau)", "Heaviside(t - tau)"),
        ("delta(t - 2)", "DiracDelta(t - 2)"),
        ("step(t)", "Heaviside(t)"),
        # Engineering writes the imaginary unit `j`, both applied and as a literal.
        ("j*w", "I*w"),
        ("4j*w", "4*I*w"),
        # `abs` and `ln` are what a model types; SymPy calls them something else.
        ("abs(t)", "Abs(t)"),
        ("ln(E)", "1"),
        # A subscripted symbol. This died on a bare NameError, because the transformation
        # that produced it emitted a `Number` the namespace did not hold.
        ("x1 + t0", "t0 + x1"),
        # An undefined function, which is how every system property is written.
        ("h(t)", "h(t)"),
        ("x(t - t0)", "x(t - t0)"),
        # `inf` unbound became a free symbol, so an improper integral came back plausible
        # and wrong rather than refused.
        ("limit(exp(-2*t), t, inf)", "0"),
    ],
)
def test_the_notation_this_course_is_written_in_parses(expression: str, expected: str) -> None:
    result = cas.evaluate(expression)

    assert result.ok is True, result.error
    assert result.value["simplified"] == expected


def test_a_definite_integral_may_be_written_the_way_the_tool_takes_one() -> None:
    """`integrate(f, t, a, b)` is the shape of `cas_integrate`, so it is what a model writes."""
    flat = cas.evaluate("integrate(exp(-2*t), t, 0, oo)")
    tupled = cas.evaluate("integrate(exp(-2*t), (t, 0, oo))")

    assert flat.ok is True, flat.error
    assert flat.value["simplified"] == "1/2"
    assert tupled.value["simplified"] == "1/2"


def test_an_expression_is_never_read_as_something_it_does_not_say() -> None:
    """The rule the whole runner answers to: refuse, but never quietly answer differently.

    Each of these parsed silently into something else. `abs(t)` became the product
    `a*b*s*t` and `h(t)` became `h*t`, so a checker comparing an impulse response against
    a claim was comparing two expressions that had nothing to do with either.
    """
    assert cas.evaluate("abs(t)").value["simplified"] == "Abs(t)"
    assert cas.evaluate("h(t)").value["simplified"] == "h(t)"
    assert cas.evaluate("foo(t)").value["simplified"] == "foo(t)"


@pytest.mark.parametrize("expression", ["2x", "3(x + 1)", "sin(2t)"])
def test_a_missing_multiplication_sign_is_refused_with_the_fix(expression: str) -> None:
    """Refusing is the cost of the rule above, so the refusal has to be worth reading."""
    result = cas.evaluate(expression)

    assert result.ok is False
    assert "write multiplication explicitly" in result.error


def test_a_boolean_expression_says_what_to_do_instead() -> None:
    result = cas.evaluate("-2 <= -1 and -1 <= 3")

    assert result.ok is False
    assert "`and`, `or` or `not`" in result.error
    assert "compare_to" in result.error


def test_an_unreducible_difference_is_reported_as_unsettled_not_as_disagreement() -> None:
    """The verdict-level bug: `certain` used to be `not equal` restated.

    SymPy declines this transform pair without assumptions on the parameter and leaves an
    unevaluated integral behind. A difference that is not zero was read as proof of
    inequality, so the tool asserted with certainty that correct work was wrong, and the
    checker refuted it. `certain` false is what "we could not settle this" looks like, and
    the tool description already told the model to read it that way.
    """
    result = cas.evaluate("2 * integrate(exp(-4*t)*cos(w*t), t, 0, oo)", "8 / (16 + w**2)")

    assert result.ok is True, result.error
    assert result.value["equal"] is False
    assert result.value["certain"] is False
    assert "not a disagreement" in result.value["note"]


def test_a_real_difference_is_still_reported_with_certainty() -> None:
    """Abstaining may not become the answer to everything, or the check stops checking."""
    result = cas.evaluate("(a*x1 + b*x2)**2", "a*x1**2 + b*x2**2")

    assert result.value["equal"] is False
    assert result.value["certain"] is True
    assert "note" not in result.value


@pytest.mark.parametrize(
    "expression",
    ["sin(x)**2 + cos(x)**2", "integrate(exp(-2*t), t, 0, oo) + 1/2", "(a + b)**2"],
)
def test_a_true_identity_is_equal_and_certain(expression: str) -> None:
    comparisons = {
        "sin(x)**2 + cos(x)**2": "1",
        "integrate(exp(-2*t), t, 0, oo) + 1/2": "1",
        "(a + b)**2": "a**2 + 2*a*b + b**2",
    }
    result = cas.evaluate(expression, comparisons[expression])

    assert result.value["equal"] is True
    assert result.value["certain"] is True


def test_an_identity_that_only_holds_on_part_of_the_domain_is_not_called_certain() -> None:
    """`sqrt(x**2)` is `x` for positive x and not otherwise, so neither answer is available."""
    result = cas.evaluate("sqrt(x**2)", "x")

    assert result.value["equal"] is False
    assert result.value["certain"] is False


@pytest.mark.parametrize("spelling", ["oo", "inf", "infty", "infinity", "Infinity"])
def test_every_spelling_of_infinity_is_a_bound_and_not_a_symbol(spelling: str) -> None:
    """An unbound spelling is not a failed parse, it is a wrong answer that looks right.

    `inf` used to reach the evaluator as a free symbol, so this integral came back as
    `1/4 - exp(-4*inf)/4` and reported success. The checker then made a verdict on it.
    """
    result = cas.integrate("exp(-4*t)", "t", "0", spelling)

    assert result.ok is True, result.error
    assert result.value["result"] == "1/4"


def test_a_declared_variable_outranks_a_name_this_module_binds() -> None:
    """`u` is the unit step until the caller says `u` is what it is integrating over.

    Both readings were in play at once: the integrand parsed `u` as the step while the
    variable was the symbol `u`, so the integral answered neither question and said so
    with an `ok`.
    """
    assert cas.integrate("exp(u)", "u").value["result"] == "exp(u)"
    assert cas.differentiate("u**2", "u").value["result"] == "2*u"
    assert cas.solve(["u**2 - 4 = 0"], ["u"]).value["solutions"] == [{"u": "-2"}, {"u": "2"}]
    # And with any other variable, `u` is the step again.
    assert cas.integrate("u(t - 1)*exp(-t)", "t", "0", "oo").value["result"] == "exp(-1)"


def test_the_imaginary_unit_is_the_imaginary_unit_however_it_is_written() -> None:
    """`j` reaching the evaluator as a free symbol refuted correct work with certainty.

    Twelve comparisons in a single real solve set were reported `equal: false,
    certain: true` for no reason other than this, each of them a true identity.
    """
    result = cas.evaluate("j * (-j / (2 + j*w)**2)", "1/(2 + j*w)**2")

    assert result.value["equal"] is True
    assert result.value["certain"] is True
    assert cas.evaluate("(2 + j)*(1 - 3*j)", "5 - 5*j").value["equal"] is True
