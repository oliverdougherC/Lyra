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


def test_a_shorter_list_is_the_judges_call() -> None:
    # A student who listed two of three items may have shown most of the understanding;
    # the set layer does not decide that alone.
    result = _grade("1, 2, 3", "1, 2")
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
    assert grading._map_judge_verdict("correct", 0.9) == ("correct", None)
    assert grading._map_judge_verdict("mostly_correct", 0.9) == ("correct", None)
    assert grading._map_judge_verdict("partially_correct", 0.9) == (
        "uncertain",
        "partially correct",
    )
    assert grading._map_judge_verdict("uncertain", 0.9) == ("uncertain", None)
    assert grading._map_judge_verdict("incorrect", 0.5) == (
        "uncertain",
        "incorrect, low confidence",
    )
    assert grading._map_judge_verdict("incorrect", 0.6) == ("incorrect", None)
    assert grading._map_judge_verdict("incorrect", 0.9) == ("incorrect", None)


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


def test_an_oversized_response_is_not_parsed() -> None:
    result = _grade("the Krebs cycle", "a" * 5000)
    assert result.verdict == grading.VERDICT_UNCERTAIN
