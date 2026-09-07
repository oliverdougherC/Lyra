"""The evaluation's mechanical checks must not masquerade as semantic grading."""

from scripts.eval_study import structural_checks


def test_study_eval_flags_cosmetic_duplicates_and_excluded_provenance() -> None:
    content = [
        {"content": {"question": "What is current?"}, "provenance": [{"document_id": 1}]},
        {"content": {"question": " what IS current "}, "provenance": [{"document_id": 2}]},
    ]
    result = structural_checks(content, [1], "quiz")
    assert result["duplicate_stems"] == 1
    assert result["selected_provenance_only"] is False
    assert result["semantic_correctness"] == "not established by structural checks"


def test_study_eval_does_not_treat_empty_artifact_as_grounded() -> None:
    assert structural_checks([], [1], "deck")["selected_provenance_only"] is False


def test_study_eval_accepts_only_mechanical_card_contract() -> None:
    content = [{"content": {"front": "Why?"}, "provenance": [{"document_id": 1}]}]
    result = structural_checks(content, [1], "deck")
    assert result["duplicate_stems"] == 0
    assert result["selected_provenance_only"] is True
