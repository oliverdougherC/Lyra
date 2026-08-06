"""The extraction eval harness, driven against a stubbed model.

An evaluation harness that scores wrongly is worse than none: it reports a number nobody
can check, and the prompt work gets tuned against it. So the scoring is tested the same way
the product is, with the model replaced and the arithmetic asserted.

Nothing here reaches an endpoint or the student's own database.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval_extract import (  # noqa: E402
    Case,
    load_corpus,
    run_case,
)
from scripts.eval_extract import (  # noqa: E402
    _summary as summary,
)

CORPUS = ROOT / "scripts" / "eval_corpora" / "extraction.json"

_SHEET = (
    "ECE 301 Signals and Systems, Spring 2026\n"
    "Instructor: Dr. Amara Osei\n"
    "Problem 1. Compute the convolution of x(t) and h(t).\n"
)


@pytest.fixture(autouse=True)
def local_endpoint(db: sqlite3.Connection) -> None:
    """Settings that permit extraction, so a case is not scored as a skip."""
    from backend.core.app_settings import update_settings_row

    update_settings_row(db, {"endpoint_url": "http://127.0.0.1:8080/v1", "extraction_enabled": 1})


def _reply(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    from backend.core import profiles

    async def _complete(*args: object, **kwargs: object) -> str:
        return content

    monkeypatch.setattr(profiles.client, "complete", _complete)


def _case(**overrides: object) -> Case:
    fields: dict[str, object] = {
        "name": "sheet",
        "filename": "ECE301-hw4.pdf",
        "text": _SHEET,
        "expect_doc_type": "homework",
    }
    fields.update(overrides)
    return Case(**fields)  # type: ignore[arg-type]


def test_the_shipped_corpus_loads_and_every_case_classifies_as_it_claims() -> None:
    """A case whose document type is wrong is measuring a prompt it did not mean to."""
    from backend.rag.chunk import detect_doc_type

    cases = load_corpus(CORPUS)

    assert cases
    for case in cases:
        assert detect_doc_type(case.filename, case.text) == case.expect_doc_type, case.name


def test_a_case_scores_what_was_found_what_was_missed_and_what_was_invented(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reply(
        monkeypatch,
        '{"topics": [{"name": "Convolution",'
        ' "quote": "Compute the convolution of x(t) and h(t)."}],'
        ' "notes": [{"label": "Instructor", "value": "Dr. Amara Osei", "quote": "no such line"}],'
        ' "deadlines": []}',
    )

    result = run_case(db, _case(expect=("convolution", "fourier"), forbid=("osei",)))

    assert result.doc_type == "homework"
    assert result.doc_type_ok
    assert result.found == ["convolution"]
    assert result.missed == ["fourier"]
    assert result.contaminants == ["Instructor: Dr. Amara Osei"]
    assert result.recall == 0.5


def test_the_verified_rate_counts_only_facts_whose_quote_was_in_the_document(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The metric that needs no labelling, and the one that catches a model inventing."""
    _reply(
        monkeypatch,
        '{"topics": ['
        '{"name": "Convolution", "quote": "Compute the convolution of x(t) and h(t)."},'
        '{"name": "Fourier series", "quote": "This sentence is nowhere in the sheet."}'
        '], "notes": [], "deadlines": []}',
    )

    result = run_case(db, _case())

    assert len(result.facts) == 2
    assert result.verified == 1
    assert result.verified_rate == 0.5


def test_a_case_that_labelled_nothing_has_no_recall_rather_than_perfect_recall(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scoring 1.0 for an unlabelled case would let a thin corpus report a perfect run."""
    _reply(monkeypatch, '{"topics": [], "notes": [], "deadlines": []}')

    result = run_case(db, _case())

    assert result.recall is None
    assert summary([result])["recall"] is None


def test_each_case_is_scored_against_its_own_facts_and_not_the_previous_ones(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extraction merges into a class profile, which is right for the product and wrong
    for a measurement: two cases sharing a class would score the second against the first."""
    _reply(
        monkeypatch,
        '{"topics": [{"name": "Convolution",'
        ' "quote": "Compute the convolution of x(t) and h(t)."}],'
        ' "notes": [], "deadlines": []}',
    )

    first = run_case(db, _case(name="first"))
    _reply(monkeypatch, '{"topics": [], "notes": [], "deadlines": []}')
    second = run_case(db, _case(name="second"))

    assert len(first.facts) == 1
    assert second.facts == []


def test_a_model_that_fails_is_reported_as_an_error_rather_than_as_a_perfect_score(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.core import profiles
    from backend.core.errors import UpstreamError

    async def _fail(*args: object, **kwargs: object) -> str:
        raise UpstreamError("endpoint down")

    monkeypatch.setattr(profiles.client, "complete", _fail)

    result = run_case(db, _case(expect=("convolution",)))

    assert "endpoint down" in result.error
    assert summary([result])["errors"] == 1
    # An errored case contributes to neither average, so it cannot flatter a run.
    assert summary([result])["recall"] is None


def test_the_summary_totals_every_case(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reply(
        monkeypatch,
        '{"topics": [{"name": "Convolution",'
        ' "quote": "Compute the convolution of x(t) and h(t)."}],'
        ' "notes": [{"label": "Instructor", "value": "Dr. Amara Osei", "quote": "nope"}],'
        ' "deadlines": []}',
    )

    results = [
        run_case(db, _case(name="a", expect=("convolution",), forbid=("osei",))),
        run_case(db, _case(name="b", expect=("convolution",), forbid=("osei",))),
    ]

    totals = summary(results)
    assert totals["cases"] == 2
    assert totals["errors"] == 0
    assert totals["doc_type_correct"] == 2
    assert totals["facts_total"] == 4
    assert totals["facts_per_document"] == 2.0
    assert totals["recall"] == 1.0
    assert totals["verified_rate"] == 0.5
    assert totals["contamination"] == 2
