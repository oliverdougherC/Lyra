"""Unit tests for the layered free-response grader (PLA-496).

Everything here is deterministic: the provider judge is a synthetic fixture - a stubbed
transport for the full path, a plain callable for the layered pass - and the symbolic
layer uses the real bounded algebra subprocess exactly the way production does. No user
data, no network.
"""

import json
from collections.abc import Callable

import pytest

from backend.core import grading
from backend.core.app_settings import TutorConfig
from backend.llm import client


def _question(
    reference: str, question: str = "Express the angular sampling frequency.", **extra
) -> dict:
    payload = {
        "type": "fill_blank",
        "question": question,
        "options": [reference],
        "correct_index": 0,
        "explanation": "Because the derivation says so.",
        "topic": "sampling",
        "difficulty": "intermediate",
    }
    payload.update(extra)
    return payload


def _grade(
    reference: str,
    response: str,
    judge: Callable[..., grading.GradingResult | None] | None = None,
    **extra,
) -> grading.GradingResult:
    return grading.grade_free_response(_question(reference, **extra), response, judge=judge)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_normalize_text_folds_case_whitespace_and_punctuation() -> None:
    assert grading.normalize_text("Photosynthesis.") == "photosynthesis"
    assert grading.normalize_text("  the   Krebs  cycle ") == "the krebs cycle"
    assert grading.normalize_text("2pi/Ts") == "2pi/ts"


def test_normalize_math_maps_unicode_and_latex_to_math_notation() -> None:
    # The normalized form is one *notation*, not one string: equivalent math may differ
    # in parentheses, and that difference is the algebra's to settle, not the normalizer's.
    for variant in ("2π/T_s", "2pi/Ts", "2*pi/T_s", "2 pi / T s", "$\\frac{2\\pi}{T_s}$", "2π/T s"):
        assert grading.normalize_math(variant) is not None, variant


def test_normalize_math_keeps_scientific_notation_intact() -> None:
    assert grading.normalize_math("1e5") == "1e5"
    assert grading.normalize_math("3E") is not None
    assert "e*" not in (grading.normalize_math("1e5") or "")


def test_normalize_math_inserts_implicit_multiplication() -> None:
    assert grading.normalize_math("2pi") == "2*pi"
    assert grading.normalize_math("(2)pi") == "(2)*pi"
    assert grading.normalize_math("pi(t)") == "pi*(t)"
    # A subscripted name is one symbol, never a product.
    assert grading.normalize_math("T s") == "Ts"


def test_normalize_math_keeps_function_applications() -> None:
    assert grading.normalize_math("cos T") == "cos(T)"
    assert grading.normalize_math("T cos(x)") == "T*cos(x)"


def test_normalize_math_refuses_what_the_parsers_cannot_read() -> None:
    assert grading.normalize_math("") is None
    assert grading.normalize_math("   ") is None
    assert grading.normalize_math("x = y; import os") is None
    assert grading.normalize_math("a" * 3000) is None


# ---------------------------------------------------------------------------
# The acceptance example and symbolic equivalence
# ---------------------------------------------------------------------------


def test_pi_over_ts_is_equivalent_to_pi_over_t_subscript_s() -> None:
    # The issue's example: `2pi/Ts` against `2π/T_s`.
    result = _grade("2π/T_s", "2pi/Ts")
    assert result.verdict == grading.VERDICT_CORRECT
    assert result.detail["grader"] == "symbolic"


def test_the_latex_form_of_the_acceptance_example_is_equivalent() -> None:
    # The same example, typeset: delimiters, \frac, and subscripts all read to the same
    # mathematics.
    result = _grade("2π/T_s", "$\\frac{2\\pi}{T_s}$")
    assert result.verdict == grading.VERDICT_CORRECT


def test_a_different_formula_is_settled_incorrect() -> None:
    # `Ts` against `T`: the difference against the free symbol shows nonzero at the
    # sampler's fixed points, so the formulas are settled distinct.
    result = _grade("2*pi/Ts", "2*pi/T")
    assert result.verdict == grading.VERDICT_INCORRECT
    assert result.detail["grader"] == "symbolic"


def test_an_unsettled_algebraic_difference_abstains_to_the_judge() -> None:
    # More than six free symbols: the sampler cannot show the difference nonzero, and an
    # honest check abstains on what it cannot show rather than call the student wrong.
    result = _grade("a1+a2+a3+a4+a5+a6+a7", "b1+b2+b3+b4+b5+b6+b7")
    assert result.verdict == grading.VERDICT_UNCERTAIN
    assert result.detail["grader"] == "fallback"


def test_a_settled_symbolic_inequality_is_incorrect() -> None:
    # Both sides settle to constants the algebra can compare with certainty.
    result = _grade("cos(0)", "sin(0)")
    assert result.verdict == grading.VERDICT_INCORRECT
    assert result.detail["grader"] == "symbolic"


def test_equivalent_rearrangements_are_accepted() -> None:
    result = _grade("2*pi/Ts", "(2*pi)/(Ts)")
    assert result.verdict == grading.VERDICT_CORRECT


def test_a_numeric_value_is_not_the_formula_that_generates_it() -> None:
    # `0.628` is `2*pi/10` - a value, not the formula `2*pi/Ts`. The algebra settles the
    # difference against the free symbol: not the same expression.
    result = _grade("2*pi/Ts", "0.628")
    assert result.verdict == grading.VERDICT_INCORRECT
    assert result.detail["grader"] == "symbolic"


def test_prose_answers_never_reach_the_symbolic_layer() -> None:
    # `the Krebs cycle` would normalize into a product of free symbols; equating two
    # such products would be a match the layer invented.
    result = _grade("the Krebs cycle", "Krebs cycle")
    assert result.verdict == grading.VERDICT_UNCERTAIN
    assert result.detail["grader"] == "fallback"


# ---------------------------------------------------------------------------
# Numeric layer
# ---------------------------------------------------------------------------


def test_numeric_unit_conversion_is_equivalent() -> None:
    result = _grade("4.2 Hz", "4200 mHz")
    assert result.verdict == grading.VERDICT_CORRECT
    assert result.detail["grader"] == "numeric"


def test_numeric_dimension_mismatch_is_not_decided_here() -> None:
    # `4.2` against `4.2 Hz`: whether an omitted unit counts is the judge's call.
    result = _grade("4.2 Hz", "4.2")
    assert result.verdict == grading.VERDICT_UNCERTAIN


def test_numeric_percentages_are_equivalent() -> None:
    assert _grade("0.42", "42%").verdict == grading.VERDICT_CORRECT


def test_numeric_scientific_notation_is_equivalent() -> None:
    assert _grade("1000", "1e3").verdict == grading.VERDICT_CORRECT
    assert _grade("1e-3", "0.001").verdict == grading.VERDICT_CORRECT


def test_numeric_rounding_within_tolerance_is_equivalent() -> None:
    # Two significant figures of 0.6283 round to 0.63, inside the default one percent.
    assert _grade("0.6283", "0.63").verdict == grading.VERDICT_CORRECT


def test_numeric_beyond_tolerance_is_a_settled_mismatch() -> None:
    assert _grade("0.6283", "0.6").verdict == grading.VERDICT_INCORRECT


def test_rubric_tolerance_relaxes_the_check() -> None:
    result = _grade("0.6283", "0.61", grading={"tolerance": 0.05})
    assert result.verdict == grading.VERDICT_CORRECT
    result = _grade("0.6283", "0.61")
    assert result.verdict == grading.VERDICT_INCORRECT


def test_a_bare_pi_is_compared_as_a_constant() -> None:
    assert _grade("pi", "3.14159").verdict == grading.VERDICT_CORRECT


# ---------------------------------------------------------------------------
# Small-scale numeric grading (PLA-496 R1)
#
# The numeric layer must hold a tiny answer to the same relative standard as a large
# one: a gross relative error at 1e-12 is a wrong answer exactly as at 1, and a value
# within the relative tolerance is credit at either scale. A fixed base-unit absolute
# allowance was erasing exactly that difference for small answers.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reference", "response"),
    [
        # Tiny dimensionless answers: 100 percent off, 500x off, and even the opposite
        # sign, in base units.
        ("1e-12", "0"),
        ("1e-12", "5e-10"),
        ("1e-12", "-1e-12"),
        # The same gross errors in physical units, converted to base units first.
        ("1 pF", "500 pF"),
        ("1 pF", "-1 pF"),
        ("1 pF", "0 F"),
        ("0 F", "1 pF"),
        # Zero and a nonzero value are a settled mismatch in both directions.
        ("0", "1e-12"),
    ],
)
def test_small_numeric_mismatches_do_not_receive_absolute_floor_credit(
    reference: str, response: str
) -> None:
    result = _grade(reference, response, grading={"answer_kind": "numeric", "tolerance": 0.01})
    assert result.verdict == grading.VERDICT_INCORRECT
    assert result.detail["grader"] == "numeric"


@pytest.mark.parametrize(
    ("reference", "response"),
    [
        # A valid unit conversion at the small scale.
        ("1 pF", "1e-12 F"),
        # Legitimate rounding inside the relative tolerance, at a tiny scale and at an
        # ordinary one: the repair tightens gross errors, not rounding.
        ("1e-12", "1.005e-12"),
        ("1 pF", "1.005 pF"),
        ("0.6283", "0.63"),
        # Exact zero matches exact zero, written in either notation.
        ("0", "0.0"),
    ],
)
def test_small_numeric_rounding_and_conversion_still_receive_credit(
    reference: str, response: str
) -> None:
    result = _grade(reference, response, grading={"answer_kind": "numeric", "tolerance": 0.01})
    assert result.verdict == grading.VERDICT_CORRECT
    assert result.detail["grader"] == "numeric"


def test_the_exact_zero_comparison_is_settled_relative() -> None:
    # The predicate takes canonical and response in either order: zero and nonzero are
    # a settled mismatch in both directions, and two exact zeros are an exact match for
    # which no tolerance is needed.
    assert grading._numeric_verdict("1e-12", "0", 0.01) == grading.VERDICT_INCORRECT
    assert grading._numeric_verdict("0", "1e-12", 0.01) == grading.VERDICT_INCORRECT
    assert grading._numeric_verdict("0", "0", 0.01) == grading.VERDICT_CORRECT
    assert grading._numeric_verdict("0.0", "0", 0.01) == grading.VERDICT_CORRECT


def test_a_stricter_rubric_tolerance_still_rejects_a_gross_small_mismatch() -> None:
    # A tighter rubric tolerance stays tight at a tiny scale: 50 percent off is outside
    # one-tenth percent no matter how small the absolute difference happens to be.
    gross = _grade("1e-12", "1.5e-12", grading={"answer_kind": "numeric", "tolerance": 0.001})
    assert gross.verdict == grading.VERDICT_INCORRECT
    # And the same tightened tolerance still accepts a value inside it, at the scale.
    inside = _grade("1e-12", "1.0005e-12", grading={"answer_kind": "numeric", "tolerance": 0.001})
    assert inside.verdict == grading.VERDICT_CORRECT


def test_the_relative_check_survives_extreme_finite_scales() -> None:
    # The comparison normalizes each side by the larger magnitude before differencing,
    # so the largest and smallest finite magnitudes neither overflow the difference nor
    # underflow the tolerance itself.
    assert _grade("1e308", "1.0005e308").verdict == grading.VERDICT_CORRECT
    assert _grade("1e308", "1.5e308").verdict == grading.VERDICT_INCORRECT
    assert _grade("1e308", "-1e308").verdict == grading.VERDICT_INCORRECT
    assert _grade("1e-320", "1.005e-320").verdict == grading.VERDICT_CORRECT
    assert _grade("1e-320", "0").verdict == grading.VERDICT_INCORRECT


def test_a_small_dimension_mismatch_still_abstains() -> None:
    # `1 pF` against a bare `1` is a dimensionality mismatch at any scale: whether an
    # omitted unit counts is the judge's call, so the pass lands on the fallback.
    result = _grade("1 pF", "1", grading={"answer_kind": "numeric", "tolerance": 0.01})
    assert result.verdict == grading.VERDICT_UNCERTAIN
    assert result.detail["grader"] == "fallback"


def test_a_small_mismatch_in_a_numeric_set_cannot_bypass_the_predicate() -> None:
    # Set membership is decided through the same numeric comparison: a member whose
    # gross relative error an absolute floor would have excused must not find an
    # "equivalent" partner and carry the whole set to credit.
    question = _question("1 pF, 2 pF", grading={"answer_kind": "set", "tolerance": 0.01})
    result = grading.grade_free_response(question, "500 pF, 2 pF", judge=None)
    assert result.verdict == grading.VERDICT_INCORRECT
    assert result.detail["grader"] == "set"
    # A legitimate member written in another unit still matches, so the set layer keeps
    # crediting real equivalence at the small scale.
    converted = grading.grade_free_response(question, "1e-12 F, 2e-12 F", judge=None)
    assert converted.verdict == grading.VERDICT_CORRECT
    assert converted.detail["grader"] == "set"


def test_a_small_mismatch_cannot_bypass_the_predicate_through_alternatives() -> None:
    # An acceptable alternative is an equivalent *form* of the reference, compared with
    # the same numeric comparison: a grossly off value is not an alternative form,
    # however close its base-unit magnitude happens to sit to any fixed allowance.
    question = _question(
        "1 pF",
        grading={
            "answer_kind": "numeric",
            "tolerance": 0.01,
            "acceptable_alternatives": ["1e-12 F"],
        },
    )
    gross = grading.grade_free_response(question, "500 pF", judge=None)
    assert gross.verdict == grading.VERDICT_INCORRECT
    assert gross.detail["grader"] == "numeric"
    # The genuine alternative form still earns credit through the alternative path.
    converted = grading.grade_free_response(question, "1e-12 F", judge=None)
    assert converted.verdict == grading.VERDICT_CORRECT
    assert converted.detail["grader"] == "alternative"


# ---------------------------------------------------------------------------
# Inclusive tolerance boundary (PLA-496 R2)
#
# The comparison must hold the tolerance the question sets, inclusive: an answer
# exactly `rel_tol` away in these representable cases receives credit, in
# either operand order and on the negative side - and one step past the boundary
# is still a settled mismatch. The version-3 normalized difference rounded an
# exact boundary just outside itself, so these answers came back wrong.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("canonical", "response", "rel_tol"),
    [
        # Exactly 1% away, at an ordinary and at a scaled magnitude.
        ("100", "99", 0.01),
        ("1000", "990", 0.01),
        # Exactly 5% away, at an ordinary and at a scaled magnitude.
        ("100", "95", 0.05),
        ("10000", "9500", 0.05),
        # Exactly 0.1% away, at an ordinary and at a scaled magnitude.
        ("1000", "999", 0.001),
        ("100000", "99900", 0.001),
        # Reversed operands sit on the same boundary, not a different one.
        ("99", "100", 0.01),
        ("95", "100", 0.05),
        ("999", "1000", 0.001),
        # And so does the negative of a boundary answer.
        ("-100", "-99", 0.01),
        ("-100", "-95", 0.05),
        ("-1000", "-999", 0.001),
    ],
)
def test_an_answer_exactly_at_the_relative_boundary_is_correct(
    canonical: str, response: str, rel_tol: float
) -> None:
    assert grading._numeric_verdict(canonical, response, rel_tol) == grading.VERDICT_CORRECT


@pytest.mark.parametrize(
    ("canonical", "response", "rel_tol"),
    [
        # One step inside the boundary: credit at every tolerance, the repair's
        # job being to hold the limit, not to sit comfortably within it.
        ("100", "99.001", 0.01),
        ("100", "95.001", 0.05),
        ("1000", "999.001", 0.001),
    ],
)
def test_a_step_inside_the_relative_boundary_is_correct(
    canonical: str, response: str, rel_tol: float
) -> None:
    assert grading._numeric_verdict(canonical, response, rel_tol) == grading.VERDICT_CORRECT


@pytest.mark.parametrize(
    ("canonical", "response", "rel_tol"),
    [
        # One step past the boundary, in both operand orders: the repair must hold
        # the inclusive limit, not enlarge it.
        ("100", "98.999", 0.01),
        ("98.999", "100", 0.01),
        ("100", "94.999", 0.05),
        ("1000", "998.999", 0.001),
    ],
)
def test_a_step_past_the_relative_boundary_is_still_incorrect(
    canonical: str, response: str, rel_tol: float
) -> None:
    assert grading._numeric_verdict(canonical, response, rel_tol) == grading.VERDICT_INCORRECT


def test_the_boundary_holds_on_the_full_grader_pass() -> None:
    # The numeric layer settles an exactly-on-boundary answer as correct - and one step
    # past the boundary as wrong - before any other layer can abstain.
    on = _grade("100", "99", grading={"answer_kind": "numeric"})
    assert on.verdict == grading.VERDICT_CORRECT
    assert on.detail["grader"] == "numeric"
    off = _grade("100", "98.999", grading={"answer_kind": "numeric"})
    assert off.verdict == grading.VERDICT_INCORRECT
    assert off.detail["grader"] == "numeric"


def test_a_same_unit_physical_quantity_holds_the_boundary_too() -> None:
    # The boundary is about the comparison, not the dimension: a same-unit physical
    # quantity exactly `rel_tol` away receives credit through the numeric layer, and
    # one step past it settles wrong there too.
    on = _grade("100 Hz", "99 Hz", grading={"answer_kind": "numeric"})
    assert on.verdict == grading.VERDICT_CORRECT
    assert on.detail["grader"] == "numeric"
    off = _grade("100 Hz", "98.999 Hz", grading={"answer_kind": "numeric"})
    assert off.verdict == grading.VERDICT_INCORRECT
    assert off.detail["grader"] == "numeric"


def test_a_numeric_set_member_exactly_at_the_boundary_still_matches() -> None:
    # Set membership runs through the same numeric comparison: a member exactly on the
    # boundary finds its equivalent partner, and a member one step past it does not.
    question = _question("100 Hz, 200 Hz", grading={"answer_kind": "set"})
    on = grading.grade_free_response(question, "99 Hz, 200 Hz", judge=None)
    assert on.verdict == grading.VERDICT_CORRECT
    assert on.detail["grader"] == "set"
    off = grading.grade_free_response(question, "98.999 Hz, 200 Hz", judge=None)
    assert off.verdict == grading.VERDICT_INCORRECT
    assert off.detail["grader"] == "set"


def test_an_acceptable_alternative_carries_the_boundary_too() -> None:
    # An alternative is an equivalent *form* of the reference, compared with the same
    # numeric comparison: an answer exactly on the boundary earns credit through the
    # alternative path, and one step past it still settles wrong in the numeric layer.
    question = _question(
        "100 Hz",
        grading={
            "answer_kind": "numeric",
            "tolerance": 0.01,
            "acceptable_alternatives": ["0.1 kHz"],
        },
    )
    on = grading.grade_free_response(question, "99 Hz", judge=None)
    assert on.verdict == grading.VERDICT_CORRECT
    assert on.detail["grader"] == "alternative"
    off = grading.grade_free_response(question, "98.999 Hz", judge=None)
    assert off.verdict == grading.VERDICT_INCORRECT
    assert off.detail["grader"] == "numeric"


# ---------------------------------------------------------------------------
# Set and list layer
# ---------------------------------------------------------------------------


def test_sets_are_order_insensitive() -> None:
    result = _grade("1, 2, 3", "3; 1; 2")
    assert result.verdict == grading.VERDICT_CORRECT
    assert result.detail["grader"] == "set"


def test_a_list_with_a_wrong_numeric_item_is_a_settled_mismatch() -> None:
    # Same length, every item numeric: a required item with no equivalent settles wrong.
    result = _grade("1, 2, 3", "1, 2, 4")
    assert result.verdict == grading.VERDICT_INCORRECT
    assert result.detail["grader"] == "set"


def test_a_missing_or_extra_listed_item_is_a_settled_mismatch() -> None:
    # Complete membership is the rule for a numeric list: an extra item ("1, 2" versus
    # "1, 2, 999") or a missing one is a wrong answer, not a judgment call.
    extra = _grade("1, 2", "1, 2, 999")
    assert extra.verdict == grading.VERDICT_INCORRECT
    assert extra.detail["grader"] == "set"
    missing = _grade("1, 2, 3", "1, 2")
    assert missing.verdict == grading.VERDICT_INCORRECT
    assert missing.detail["grader"] == "set"


def test_a_legacy_prose_list_reorder_is_the_judges_call() -> None:
    # Nothing declared the list unordered, so a reorder of prose items is not decided by
    # the set layer; only a declared set contract accepts reordering.
    assert (
        _grade("oxygen, carbon dioxide", "carbon dioxide, oxygen").verdict
        == grading.VERDICT_UNCERTAIN
    )


def test_a_set_contract_permits_reordering_and_defers_a_prose_swap_to_the_judge() -> None:
    question = _question("oxygen, carbon dioxide", grading={"answer_kind": "set"})
    reordered = grading.grade_free_response(question, "carbon dioxide, oxygen", judge=None)
    assert reordered.verdict == grading.VERDICT_CORRECT
    assert reordered.detail["grader"] == "set"
    # A swapped-out prose member is not a membership the deterministic layers can
    # settle: `nitrogen` is neither `oxygen` nor `carbon dioxide` in any layer that
    # decides, so the set layer abstains - and with no judge to take the call, the
    # honest outcome is `uncertain`, never a confident wrong.
    swapped_out = grading.grade_free_response(question, "oxygen, nitrogen", judge=None)
    assert swapped_out.verdict == grading.VERDICT_UNCERTAIN
    assert swapped_out.detail["grader"] == "fallback"
    # The judge has the call: a confident rejection is a confident wrong, and the
    # rejection is the judge's, not the set layer's.
    rejected = grading.grade_free_response(
        question,
        "oxygen, nitrogen",
        judge=lambda **_: grading.GradingResult(
            grading.VERDICT_INCORRECT,
            {"grader": "judge", "judge_verdict": "incorrect", "confidence": 0.9},
        ),
    )
    assert rejected.verdict == grading.VERDICT_INCORRECT
    assert rejected.detail["grader"] == "judge"


def test_a_declared_prose_set_with_a_synonym_member_is_the_judges_call() -> None:
    # F1: `plasma membrane` is not `cell membrane` under any deterministic layer - not
    # string-equivalent, not math-equivalent, not numeric-equivalent - yet it is the same
    # idea. The set layer must abstain and hand the answer to the constrained judge,
    # which decides against the question's contract. A set-layer `incorrect` that lands
    # before the judge ever sees the answer is a confident false negative the whole
    # layered design exists to avoid.
    question = _question("cell membrane, nucleus", grading={"answer_kind": "set"})
    judged = grading.grade_free_response(
        question,
        "plasma membrane, nucleus",
        judge=lambda **_: grading.GradingResult(
            grading.VERDICT_CORRECT,
            {"grader": "judge", "judge_verdict": "correct", "confidence": 0.9},
        ),
    )
    assert judged.verdict == grading.VERDICT_CORRECT
    assert judged.detail["grader"] == "judge"


def test_a_declared_prose_set_reorder_still_passes_deterministically() -> None:
    # The abstention cuts one way only: an exact or trivial match of every member under a
    # declared set contract is still decided by the set layer - no judge round trip for
    # a plain reordering.
    question = _question("cell membrane, nucleus", grading={"answer_kind": "set"})
    result = grading.grade_free_response(question, "nucleus, cell membrane", judge=None)
    assert result.verdict == grading.VERDICT_CORRECT
    assert result.detail["grader"] == "set"


def test_a_declared_numeric_set_with_a_wrong_member_is_still_settled() -> None:
    # Numeric members keep the old decisiveness: a required item whose value is a
    # settled mismatch against every item offered is a wrong answer, decided by the set
    # layer without a judge.
    question = _question("1, 2, 3", grading={"answer_kind": "set"})
    result = grading.grade_free_response(question, "1, 2, 4", judge=None)
    assert result.verdict == grading.VERDICT_INCORRECT
    assert result.detail["grader"] == "set"
    reordered = grading.grade_free_response(question, "3, 1, 2", judge=None)
    assert reordered.verdict == grading.VERDICT_CORRECT
    assert reordered.detail["grader"] == "set"


def test_a_declared_numeric_set_with_a_missing_item_is_still_settled() -> None:
    # Cardinality is mathematics for an all-numeric set: an extra or a missing member
    # settles wrong, not a judgment call.
    question = _question("1, 2, 3", grading={"answer_kind": "set"})
    extra = grading.grade_free_response(question, "1, 2, 3, 999", judge=None)
    assert extra.verdict == grading.VERDICT_INCORRECT
    assert extra.detail["grader"] == "set"
    missing = grading.grade_free_response(question, "1, 2", judge=None)
    assert missing.verdict == grading.VERDICT_INCORRECT
    assert missing.detail["grader"] == "set"


def test_a_greedy_order_cannot_settle_a_tolerance_match_wrong() -> None:
    # Counterexample: every member of `100, 101` lies within one percent of a member of
    # `100.5, 99.5`, and a valid complete matching exists (100 -> 99.5, 101 -> 100.5).
    # A greedy first choice hands 100 to 100.5 and leaves 101 against a 99.5 that is
    # 1.5 percent away - the set layer must not let that order settle the answer wrong.
    # The assignment settles correct in both directions, without a judge.
    question = _question("100, 101", grading={"answer_kind": "set"})
    forward = grading.grade_free_response(question, "100.5, 99.5", judge=None)
    assert forward.verdict == grading.VERDICT_CORRECT
    assert forward.detail["grader"] == "set"
    reversed_question = _question("101, 100", grading={"answer_kind": "set"})
    reversed_result = grading.grade_free_response(reversed_question, "99.5, 100.5", judge=None)
    assert reversed_result.verdict == grading.VERDICT_CORRECT
    assert reversed_result.detail["grader"] == "set"


def test_a_numeric_set_that_cannot_be_matched_still_settles_wrong() -> None:
    # The matching refines credit, not doubt: a numeric set whose members cannot be
    # assigned to distinct offered equivalents still settles wrong - here both
    # required ~100s compete for the single ~100 offered, and 250 is decisively off.
    question = _question("100, 100", grading={"answer_kind": "set"})
    result = grading.grade_free_response(question, "100, 250", judge=None)
    assert result.verdict == grading.VERDICT_INCORRECT
    assert result.detail["grader"] == "set"


def test_a_set_beyond_the_matching_cap_abstains_instead_of_settling() -> None:
    # Beyond the matching cap the layer refuses to settle either way: a response that
    # cannot be fully matched - and whose equivalent partners the greedy pass
    # consumed - lands with the judge (uncertain without one), never a confident wrong
    # from a bounded search the layer did not run.
    reference = ", ".join(["1"] * 33)
    response_text = ", ".join(["1"] * 32 + ["50"])
    question = _question(reference, grading={"answer_kind": "set"})
    result = grading.grade_free_response(question, response_text, judge=None)
    assert result.verdict == grading.VERDICT_UNCERTAIN
    assert result.detail["grader"] == "fallback"


def test_a_declared_prose_set_with_a_contradiction_is_rejected_by_the_judge() -> None:
    # Members the set layer cannot decide all go to the judge together; where the
    # response contradicts the contract, a confident rejection is a confident wrong -
    # but it is the judge's rejection, carried by the judge's detail, not a set-layer
    # verdict.
    question = _question("cell membrane, nucleus", grading={"answer_kind": "set"})
    result = grading.grade_free_response(
        question,
        "plasma membrane, mitochondria",
        judge=lambda **_: grading.GradingResult(
            grading.VERDICT_INCORRECT,
            {"grader": "judge", "judge_verdict": "incorrect", "confidence": 0.9},
        ),
    )
    assert result.verdict == grading.VERDICT_INCORRECT
    assert result.detail["grader"] == "judge"
    assert result.detail["judge_verdict"] == "incorrect"


def test_an_unmatched_prose_member_without_a_usable_judge_is_uncertain() -> None:
    # An unmatched ambiguous member abstains to the judge; a judge that is absent,
    # unavailable, or failing leaves the answer `uncertain` - the recorded "not
    # confidently wrong" - and never the set layer's confident wrong.
    question = _question("cell membrane, nucleus", grading={"answer_kind": "set"})
    absent = grading.grade_free_response(question, "plasma membrane, nucleus", judge=None)
    assert absent.verdict == grading.VERDICT_UNCERTAIN
    assert absent.detail["grader"] == "fallback"
    unavailable = grading.grade_free_response(
        question, "plasma membrane, nucleus", judge=lambda **_: None
    )
    assert unavailable.verdict == grading.VERDICT_UNCERTAIN
    assert unavailable.detail["grader"] == "fallback"

    def failing_judge(**_kwargs: object) -> grading.GradingResult:
        raise RuntimeError("endpoint refused")

    failing = grading.grade_free_response(question, "plasma membrane, nucleus", judge=failing_judge)
    assert failing.verdict == grading.VERDICT_UNCERTAIN
    assert failing.detail["grader"] == "fallback"


def test_a_declared_prose_set_with_an_extra_item_is_the_judges_call() -> None:
    # An extra prose item is not membership the deterministic layers can settle: the
    # student may carry the idea in a different form, or split one idea across two.
    # The judge weighs the count against the contract.
    question = _question("cell membrane, nucleus", grading={"answer_kind": "set"})
    judged = grading.grade_free_response(
        question,
        "cell membrane, nucleus, cytoplasm",
        judge=lambda **_: grading.GradingResult(
            grading.VERDICT_CORRECT,
            {"grader": "judge", "judge_verdict": "mostly_correct", "confidence": 0.8},
        ),
    )
    assert judged.verdict == grading.VERDICT_CORRECT
    assert judged.detail["grader"] == "judge"
    without_judge = grading.grade_free_response(
        question, "cell membrane, nucleus, cytoplasm", judge=None
    )
    assert without_judge.verdict == grading.VERDICT_UNCERTAIN
    assert without_judge.detail["grader"] == "fallback"


def test_an_undecidable_unit_in_a_declared_set_is_the_judges_call() -> None:
    # `4.2` against `4.2 Hz` is a dimensionality mismatch: the numeric layer
    # deliberately will not decide whether an omitted unit counts, so a declared set
    # whose members only differ by such a unit abstains instead of settling wrong.
    question = _question("4.2 Hz, 0.42", grading={"answer_kind": "set"})
    result = grading.grade_free_response(question, "4.2, 0.42", judge=None)
    assert result.verdict == grading.VERDICT_UNCERTAIN
    assert result.detail["grader"] == "fallback"


def test_a_text_kind_answer_is_never_settled_by_a_typed_layer() -> None:
    # The declared kind gates the layers: a `text` answer cannot be settled by unit, set,
    # or algebra comparison - the judge has the call, or the honest fallback.
    assert _grade("200", "200.0", judge=None).verdict == grading.VERDICT_CORRECT
    question = _question("200", grading={"answer_kind": "text"})
    result = grading.grade_free_response(question, "200.0", judge=None)
    assert result.verdict == grading.VERDICT_UNCERTAIN


def test_a_paraphrased_set_goes_to_the_judge() -> None:
    # Text items are not decided by the set layer: a paraphrase may still carry the set.
    assert _grade("sin, cos", "sine and cosine").verdict == grading.VERDICT_UNCERTAIN


def test_bracketed_fractions_are_not_split_on_their_slash() -> None:
    result = _grade("[1/2, 1/3]", "0.5, 0.3333")
    assert result.verdict == grading.VERDICT_CORRECT
    assert result.detail["grader"] == "set"


# ---------------------------------------------------------------------------
# Trivial equivalence and rubric alternatives
# ---------------------------------------------------------------------------


def test_trivial_equivalence_is_the_fast_path() -> None:
    result = _grade("photosynthesis", "Photosynthesis.")
    assert result.verdict == grading.VERDICT_CORRECT
    assert result.detail["grader"] == "text"


def test_rubric_alternatives_are_checked_before_the_expensive_layers() -> None:
    question = _question(
        "T_s",
        grading={
            "answer_kind": "symbolic",
            "acceptable_alternatives": ["Ts", "t s"],
        },
    )
    result = grading.grade_free_response(question, "Ts")
    assert result.verdict == grading.VERDICT_CORRECT
    assert result.detail["grader"] == "alternative"


# ---------------------------------------------------------------------------
# Type-aware text: units, factors, and symbols are not punctuation
# ---------------------------------------------------------------------------


def test_case_sensitive_unit_prefixes_are_not_folded() -> None:
    # `1 mW` and `1 MW` differ by a factor of a million: a casefold is a wrong answer.
    result = _grade("1 mW", "1 MW")
    assert result.verdict == grading.VERDICT_INCORRECT
    assert result.detail["grader"] == "numeric"


def test_a_factorial_is_not_a_number_with_trailing_punctuation() -> None:
    # The `!` is a factorial, not sentence punctuation: `3` is not `3!`.
    assert _grade("3!", "3").verdict == grading.VERDICT_INCORRECT


def test_short_symbolic_tokens_are_not_case_folded() -> None:
    # `x` and `X`, `Na` and `na` are different tokens. The trivial comparison refuses to
    # decide them, and no other layer can, so the pass abstains rather than accept.
    assert _grade("x", "X").verdict == grading.VERDICT_UNCERTAIN
    assert _grade("Na", "na").verdict == grading.VERDICT_UNCERTAIN


def test_alternatives_are_compared_with_the_same_care() -> None:
    # A rubric alternative is an equivalent form, not a casefold: `1 MW` is not an
    # alternative form of `1 mW`, while the alternative itself still matches.
    question = _question(
        "1 mW", grading={"answer_kind": "numeric", "acceptable_alternatives": ["1 mW"]}
    )
    assert grading.grade_free_response(question, "1 mW").verdict == grading.VERDICT_CORRECT
    assert grading.grade_free_response(question, "1 MW").verdict == grading.VERDICT_INCORRECT


# ---------------------------------------------------------------------------
# The semantic judge: synthetic fixtures only
# ---------------------------------------------------------------------------


def test_judge_partial_understanding_is_uncertain_not_wrong() -> None:
    result = _grade(
        "the conversion of light into chemical energy",
        "plants use light",
        judge=lambda **_: grading.GradingResult(
            grading.VERDICT_UNCERTAIN, {"grader": "judge", "judge_verdict": "partially_correct"}
        ),
    )
    assert result.verdict == grading.VERDICT_UNCERTAIN
    assert result.detail["judge_verdict"] == "partially_correct"


def test_judge_uncertain_is_uncertain() -> None:
    result = _grade(
        "the Krebs cycle",
        "a cycle",
        judge=lambda **_: grading.GradingResult(
            grading.VERDICT_UNCERTAIN, {"grader": "judge", "judge_verdict": "uncertain"}
        ),
    )
    assert result.verdict == grading.VERDICT_UNCERTAIN


def test_the_judge_verdict_mapping_is_conservative() -> None:
    # The five provider grades onto the flow's three outcomes: partial understanding and
    # a low-confidence wrong are abstentions, never confident wrongs.
    assert grading._map_judge_verdict("correct", 0.9, partial_accepted=False) == ("correct", None)
    assert grading._map_judge_verdict("mostly_correct", 0.9, partial_accepted=False) == (
        "correct",
        None,
    )
    assert grading._map_judge_verdict("partially_correct", 0.9, partial_accepted=False) == (
        "uncertain",
        "partially correct",
    )
    # The contract's partial-acceptance policy turns that abstention into credit.
    assert grading._map_judge_verdict("partially_correct", 0.9, partial_accepted=True) == (
        "correct",
        "partial understanding accepted",
    )
    assert grading._map_judge_verdict("uncertain", 0.9, partial_accepted=False) == (
        "uncertain",
        None,
    )
    assert grading._map_judge_verdict("incorrect", 0.5, partial_accepted=False) == (
        "uncertain",
        "incorrect, low confidence",
    )
    assert grading._map_judge_verdict("incorrect", 0.6, partial_accepted=False) == (
        "incorrect",
        None,
    )
    assert grading._map_judge_verdict("incorrect", 0.9, partial_accepted=False) == (
        "incorrect",
        None,
    )


def _complete_that_replies(reply: str):
    """An LLM `complete` that answers whatever it is asked with one fixed reply."""

    async def complete(*args: object, **kwargs: object) -> str:
        return reply

    return complete


def test_a_malformed_confidence_is_a_malformed_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The confidence is load-bearing (an `incorrect` near the floor abstains), so a
    # missing, boolean, NaN, or out-of-range value is a malformed reply: the judgment is
    # refused, never coerced.
    for reply in (
        '{"verdict": "incorrect", "reason": "no confidence"}',
        '{"verdict": "incorrect", "confidence": true}',
        '{"verdict": "incorrect", "confidence": NaN}',
        '{"verdict": "incorrect", "confidence": 1.5}',
        '{"verdict": "incorrect", "confidence": -0.1}',
    ):
        monkeypatch.setattr(client, "complete", _complete_that_replies(reply))
        result = grading.judge_free_response(
            _config(), question="q", rubric=None, reference="r", response="s"
        )
        assert result is None, reply


def test_partial_understanding_accepted_by_contract_is_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_complete(*args: object, **kwargs: object) -> str:
        return '{"verdict": "partially_correct", "confidence": 0.8, "reason": "core idea"}'

    monkeypatch.setattr(client, "complete", fake_complete)
    result = grading.judge_free_response(
        _config(),
        question="q",
        rubric={"partial_understanding_accepted": True},
        reference="r",
        response="s",
    )
    assert result is not None
    assert result.verdict == grading.VERDICT_CORRECT
    assert result.detail["note"] == "partial understanding accepted"


def test_mostly_correct_is_credit() -> None:
    result = _grade(
        "the conversion of light into chemical energy",
        "plants turn light into chemical energy",
        judge=lambda **_: grading.GradingResult(
            grading.VERDICT_CORRECT, {"grader": "judge", "judge_verdict": "mostly_correct"}
        ),
    )
    assert result.verdict == grading.VERDICT_CORRECT


def test_no_judge_and_no_settled_layer_is_uncertain() -> None:
    result = _grade("the conversion of light into chemical energy", "photosynthesis stuff")
    assert result.verdict == grading.VERDICT_UNCERTAIN
    assert result.detail["grader"] == "fallback"


def test_a_judge_failure_is_uncertain_not_wrong() -> None:
    def judge(**_) -> grading.GradingResult:
        raise RuntimeError("endpoint refused")

    result = _grade("the Krebs cycle", "a cycle", judge=judge)
    assert result.verdict == grading.VERDICT_UNCERTAIN


# ---------------------------------------------------------------------------
# judge_free_response: the constrained provider call, transport stubbed
# ---------------------------------------------------------------------------


def _config(window: int = 8192) -> TutorConfig:
    return TutorConfig(
        endpoint_url="http://127.0.0.1:9/v1", api_key=None, model=None, context_window=window
    )


def test_judge_free_response_maps_a_synthetic_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies: list[str] = []

    async def fake_complete(
        endpoint: str,
        api_key: str | None,
        model: str | None,
        messages: list[dict[str, object]],
        *,
        transport=None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        schema: client.JsonSchema | None = None,
        request_timeout=None,
        fail_on_truncation: bool = False,
        truncated: list[bool] | None = None,
        enable_thinking: bool | None = None,
    ) -> str:
        replies.append(json.dumps(messages, ensure_ascii=False))
        return json.dumps({"verdict": "incorrect", "confidence": 0.9, "reason": "contradicted"})

    monkeypatch.setattr(client, "complete", fake_complete)
    result = grading.judge_free_response(
        _config(),
        question="Which cycle oxidizes carbon?",
        rubric={"contradictions": ["it fixes nitrogen"]},
        reference="the Krebs cycle",
        response="the cycle that fixes nitrogen",
    )

    assert result is not None
    assert result.verdict == grading.VERDICT_INCORRECT
    assert result.detail["judge_verdict"] == "incorrect"
    assert result.detail["confidence"] == 0.9
    # The prompt carries the question, the contract, the reference, and the words.
    prompt = json.loads(replies[-1])
    user = [message for message in prompt if message.get("role") == "user"][0]["content"]
    assert "Which cycle oxidizes carbon?" in user
    assert "the Krebs cycle" in user
    assert "the cycle that fixes nitrogen" in user
    assert "contradictions" in user


def test_judge_free_response_refuses_a_prompt_that_will_not_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_complete(*args: object, **kwargs: object) -> str:
        calls.append("called")
        return "{}"

    monkeypatch.setattr(client, "complete", fake_complete)
    result = grading.judge_free_response(
        _config(window=64),
        question="q" * 400,
        rubric=None,
        reference="r" * 400,
        response="s" * 400,
    )
    assert result is None
    assert calls == []


def test_judge_free_response_rejects_an_unreadable_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_complete(*args: object, **kwargs: object) -> str:
        return '{"verdict": "probably right"}'

    monkeypatch.setattr(client, "complete", fake_complete)
    result = grading.judge_free_response(
        _config(), question="q", rubric=None, reference="r", response="s"
    )
    assert result is None


def test_judge_free_response_rejects_a_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_complete(*args: object, **kwargs: object) -> str:
        raise client.UpstreamError("endpoint down")

    monkeypatch.setattr(client, "complete", fake_complete)
    result = grading.judge_free_response(
        _config(), question="q", rubric=None, reference="r", response="s"
    )
    assert result is None


# ---------------------------------------------------------------------------
# Multiple choice and the grading contract
# ---------------------------------------------------------------------------


def test_choice_grading_is_the_stored_index() -> None:
    question = {"type": "mcq", "correct_index": 2, "options": ["a", "b", "c", "d"]}
    assert grading.grade_choice(question, 2).verdict == grading.VERDICT_CORRECT
    assert grading.grade_choice(question, 1).verdict == grading.VERDICT_INCORRECT


def test_parse_rubric_accepts_a_well_formed_contract() -> None:
    rubric = grading.parse_rubric(
        {
            "answer_kind": "symbolic",
            "tolerance": 0.01,
            "units": "rad/s",
            "acceptable_alternatives": ["2pi/Ts", "2*pi/T_s"],
            "required_ideas": ["angular frequency", "sampling period"],
            "common_misconceptions": ["the period itself"],
            "contradictions": ["the sampling frequency"],
            "partial_understanding_accepted": True,
        }
    )
    assert rubric is not None
    assert rubric["answer_kind"] == "symbolic"
    assert rubric["tolerance"] == 0.01
    assert rubric["acceptable_alternatives"] == ["2pi/Ts", "2*pi/T_s"]
    assert grading.tolerance_for(rubric) == 0.01


def test_parse_rubric_rejects_malformed_fields() -> None:
    assert grading.parse_rubric(None) is None
    assert grading.parse_rubric("not an object") is None
    assert grading.parse_rubric({"answer_kind": "vibes"}) is None
    assert grading.parse_rubric({"tolerance": 0.5}) is None
    assert grading.parse_rubric({"tolerance": True}) is None
    assert grading.parse_rubric({"units": 42}) is None
    assert grading.parse_rubric({"required_ideas": "not a list"}) is None
    assert grading.parse_rubric({"required_ideas": [f"idea {n}" for n in range(9)]}) is None
    assert grading.parse_rubric({"contradictions": ["fine", 7]}) is None
    assert grading.parse_rubric({"partial_understanding_accepted": "yes"}) is None
    # Unknown fields are dropped, not trusted: they never reach a grading layer.
    parsed = grading.parse_rubric({"answer_kind": "numeric", "unknown_field": 1})
    assert parsed is not None
    assert "unknown_field" not in parsed


def test_question_digest_is_stable() -> None:
    assert grading.question_digest("{}") == grading.question_digest("{}")
    assert grading.question_digest("{}") != grading.question_digest("other")


# ---------------------------------------------------------------------------
# Refusals stay refusals
# ---------------------------------------------------------------------------


def test_student_input_is_never_evaluated_beyond_the_bounded_layers() -> None:
    # The algebra subprocess refuses the characters outright; nothing raises, and the
    # pass lands on the judge (or the fallback) rather than executing anything.
    for hostile in ("__import__('os')", "import os", "2**999999999", "(lambda: 1)()"):
        result = _grade("the Krebs cycle", hostile)
        assert result.verdict == grading.VERDICT_UNCERTAIN, hostile
        assert result.correct is False
        assert result.uncertain is True


def test_powers_beyond_bare_literals_leave_the_numeric_layer() -> None:
    # Pint would evaluate a power in the parent process, and a computed or chained
    # exponent - `2**(1000*1000*1000)`, `2**1000**1000` - could force an allocation no
    # finite check can bound. Such texts are refused before parsing; a plain literal
    # raised to a plain literal still compares.
    for hostile in ("2**(1000*1000*1000)", "2**1000**1000", "(2**1000)**1000", "x**2"):
        assert grading._safe_powers(hostile) is False, hostile
        assert grading._quantity(hostile) is None, hostile
    for benign in ("2**10", "2^10", "2**10*3", "2**10+3", "2**1000"):
        assert grading._safe_powers(benign) is True, benign
    assert _grade("1024", "2**10").verdict == grading.VERDICT_CORRECT


def test_an_oversized_response_is_not_parsed() -> None:
    result = _grade("the Krebs cycle", "a" * 5000)
    assert result.verdict == grading.VERDICT_UNCERTAIN
