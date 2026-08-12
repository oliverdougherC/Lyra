"""The solver eval harness's scoring, asserted against fixed reports.

An evaluation harness that scores wrongly is worse than none: it reports a number nobody
can check, and the solver gets tuned against it. `scripts/eval_solver.py` drives the real
solver against a real corpus and a live endpoint, which no CI run can do - but the
arithmetic that turns its recorded runs into four stage scores is pure, and that is what is
tested here, the same way `test_eval_extract` and `test_eval_ingest` guard theirs.

Nothing here reaches an endpoint, a corpus, or the student's own database. The reports are
hand-built to exercise the scoring, not produced by a solve.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval_solver import score_stages  # noqa: E402

# Three sets: one stable, one unstable, one that failed segmentation with a single run.
SEGMENTATION: dict[str, object] = {
    "homework_1": [
        {"state": "awaiting_review", "error": None, "seconds": 3.0, "problem_count": 4},
        {"state": "awaiting_review", "error": None, "seconds": 3.1, "problem_count": 4},
    ],
    "homework_2": [
        {"state": "awaiting_review", "error": None, "seconds": 2.0, "problem_count": 3},
        {"state": "awaiting_review", "error": None, "seconds": 2.2, "problem_count": 5},
    ],
    "homework_3": [
        {"state": "failed", "error": "segmenting crashed", "seconds": 1.0, "problem_count": 0},
    ],
}

SOLUTIONS: dict[str, object] = {
    "homework_1": {
        "state": "ready",
        "minutes": 5.0,
        "problem_count": 3,
        "solved_count": 2,
        "problems": [
            {"verdict": "verified", "step_count": 4, "grounded_steps": 3, "check_count": 2},
            {"verdict": "refuted", "step_count": 3, "grounded_steps": 1, "check_count": 1},
            {"verdict": "uncheckable", "step_count": 2, "grounded_steps": 0, "check_count": 0},
        ],
    },
    "homework_2": {
        "state": "failed",
        "minutes": 1.0,
        "problem_count": 2,
        "solved_count": 1,
        "problems": [
            {"verdict": "unchecked", "step_count": 1, "grounded_steps": 1, "check_count": 0},
            {"verdict": "verified", "step_count": 5, "grounded_steps": 5, "check_count": 3},
        ],
    },
}

GRADES: dict[str, object] = {
    "homework_1": [
        {"label": "1", "verdict": "agrees"},
        {"label": "2", "verdict": "disagrees"},
        {"label": "3", "verdict": "not_solved"},
    ],
    "homework_2": [
        {"label": "1", "verdict": "agrees"},
        {"label": "2", "verdict": "key_not_found"},
    ],
}


def test_parsing_score_measures_stability_only_where_a_set_repeated() -> None:
    parsing = score_stages(SEGMENTATION, {}, {})["parsing"]

    assert parsing["sets"] == 3
    assert parsing["runs"] == 5
    assert parsing["failures"] == 1
    # Only the two sets run more than once can speak to stability; the single-run set does
    # not enter the denominator, and one of the two changed its count.
    assert parsing["sets_measured_for_stability"] == 2
    assert parsing["stable_sets"] == 1
    assert parsing["stability_rate"] == 0.5
    # Problems come from each set's first run, so a repeat does not double count them.
    assert parsing["problems_found"] == 7
    assert parsing["mean_problems_per_set"] == 2.33


def test_reasoning_score_totals_solves_and_grounding() -> None:
    reasoning = score_stages(SEGMENTATION, SOLUTIONS, {})["reasoning"]

    assert reasoning["sets"] == 2
    assert reasoning["set_failures"] == 1
    assert reasoning["problems"] == 5
    assert reasoning["solved"] == 3
    assert reasoning["solve_rate"] == 0.6
    assert reasoning["steps"] == 15
    assert reasoning["grounded_steps"] == 10
    assert reasoning["grounded_rate"] == 0.667


def test_verification_score_counts_only_a_ran_check_as_checked() -> None:
    verification = score_stages(SEGMENTATION, SOLUTIONS, {})["verification"]

    assert verification["problems"] == 5
    assert verification["verdicts"] == {
        "verified": 2,
        "refuted": 1,
        "uncheckable": 1,
        "unchecked": 1,
    }
    assert verification["checks"] == 6
    # uncheckable and unchecked did not settle by calculation, so they are out of the rate.
    assert verification["checked"] == 3
    assert verification["coverage_rate"] == 0.6
    assert verification["verified"] == 2
    assert verification["verified_rate"] == 0.667


def test_final_answer_score_marks_only_solved_and_covered_problems() -> None:
    final = score_stages({}, {}, GRADES)["final_answer"]

    assert final["graded"] == 5
    assert final["verdicts"] == {
        "agrees": 2,
        "disagrees": 1,
        "not_solved": 1,
        "key_not_found": 1,
    }
    # not_solved and key_not_found are reported but never in the denominator: the rate is
    # over the problems that were both solved and covered by the key.
    assert final["marked"] == 3
    assert final["agrees"] == 2
    assert final["agreement_rate"] == 0.667


def test_a_stage_never_exercised_reads_as_unmeasured_not_perfect() -> None:
    """Every rate is None on empty input, so an unrun stage cannot read as a flat zero or a
    hollow 1.0, and the report never divides by zero."""
    scores = score_stages({}, {}, {})

    assert scores["parsing"]["stability_rate"] is None
    assert scores["parsing"]["mean_problems_per_set"] is None
    assert scores["reasoning"]["solve_rate"] is None
    assert scores["reasoning"]["grounded_rate"] is None
    assert scores["verification"]["coverage_rate"] is None
    assert scores["verification"]["verified_rate"] is None
    assert scores["final_answer"]["agreement_rate"] is None


def test_verified_rate_is_unmeasured_when_no_tool_could_run() -> None:
    """A set whose every problem was uncheckable has full coverage of nothing: the verified
    rate is None, not a zero that would read as the checker failing."""
    solutions = {
        "homework_1": {
            "state": "ready",
            "problem_count": 1,
            "solved_count": 1,
            "problems": [
                {"verdict": "uncheckable", "step_count": 2, "grounded_steps": 0, "check_count": 0}
            ],
        }
    }

    verification = score_stages({}, solutions, {})["verification"]

    assert verification["checked"] == 0
    assert verification["coverage_rate"] == 0.0
    assert verification["verified_rate"] is None
