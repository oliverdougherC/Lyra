"""The ingestion and retrieval eval harness, scored against known inputs.

An evaluation harness that scores wrongly is worse than none - the rule test_eval_extract.py
states holds here with more force, because this harness is the standard of evidence for every
retrieval number the Phase 3 documents quote. So the scoring arithmetic is tested the way the
product is: the retrieval call replaced with a stub, the ranks asserted, and the report's
hit-rate fractions checked against denominators counted by hand.

Nothing here reaches an endpoint, opens a PDF, or touches the student's own database.
"""

import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.rag import retrieve as retrieval  # noqa: E402
from backend.rag.rerank import RerankStatus  # noqa: E402
from scripts.eval_ingest import (  # noqa: E402
    NO_TARGET,
    PRODUCT_K,
    WIDE_K,
    Target,
    _already_ingested,
    _ask,
    _check_compatibility,
    _report_retrieval,
    _score_identity,
    _widened_k,
    section_ranges,
)

# ---------------------------------------------------------------------------- section_ranges


def _outline(entries: list[list[object]], page_count: int) -> dict[str, object]:
    return {"entries": entries, "entry_count": len(entries), "page_count": page_count}


def test_section_ranges_are_inclusive_at_both_ends() -> None:
    """A boundary page belongs to the section ending on it and the one starting on it."""
    ranges = section_ranges(_outline([[1, "First", 1], [1, "Second", 10]], page_count=20))

    assert ranges["First"] == (1, 10)
    assert ranges["Second"] == (10, 20)


def test_a_section_ends_at_the_next_entry_of_equal_or_shallower_depth() -> None:
    ranges = section_ranges(
        _outline(
            [[1, "A", 1], [2, "B", 3], [3, "C", 4], [2, "D", 8], [1, "E", 15]],
            page_count=20,
        )
    )

    # C is closed by D, which is shallower than C, not by an entry at C's own depth.
    assert ranges["A / B / C"] == (4, 8)
    # B is closed by its sibling D, not by C, which is nested inside it.
    assert ranges["A / B"] == (3, 8)
    # A is closed by E and spans everything nested under it.
    assert ranges["A"] == (1, 15)


def test_the_last_section_at_each_depth_runs_to_the_end_of_the_book() -> None:
    ranges = section_ranges(_outline([[1, "A", 1], [2, "B", 5]], page_count=30))

    assert ranges["A"] == (1, 30)
    assert ranges["A / B"] == (5, 30)


def test_paths_are_ancestry_joined_and_reset_when_depth_falls() -> None:
    ranges = section_ranges(
        _outline([[1, "One", 1], [2, "Sub", 2], [1, "Two", 5], [2, "Sub", 6]], page_count=9)
    )

    # The second `Sub` belongs to `Two`, not to `One`: ancestry was cut back at depth 1.
    assert set(ranges) == {"One", "One / Sub", "Two", "Two / Sub"}


def test_an_outline_with_no_entries_names_no_sections() -> None:
    assert section_ranges(_outline([], page_count=100)) == {}


# ------------------------------------------------------------------------------- rank in _ask


@dataclass(frozen=True)
class _Chunk:
    document_id: int
    page_number: int | None
    filename: str = "other.pdf"
    similarity: float = 0.5
    section_title: str | None = None
    content: str = "some content"


@dataclass(frozen=True)
class _Result:
    chunks: list[_Chunk]
    rerank_status: RerankStatus = RerankStatus.NOT_REQUESTED


def _stub_retrieve(monkeypatch: pytest.MonkeyPatch, chunks: list[_Chunk]) -> None:
    def fake(conn: object, class_id: int, query: str, budget: int) -> _Result:
        return _Result(chunks=chunks)

    monkeypatch.setattr(retrieval, "retrieve", fake)


def _question(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {"id": "q1", "question": "where is it"}
    fields.update(overrides)
    return fields


def test_rank_is_the_position_of_the_first_chunk_from_the_right_pages_of_the_right_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_retrieve(
        monkeypatch,
        [
            # Right pages, wrong document: a miss however well it scored.
            _Chunk(document_id=2, page_number=5),
            # Right document, wrong page.
            _Chunk(document_id=1, page_number=4, filename="book.pdf"),
            # A chunk with no page can never be the hit.
            _Chunk(document_id=1, page_number=None, filename="book.pdf"),
            _Chunk(document_id=1, page_number=5, filename="book.pdf"),
        ],
    )
    target = Target(document_id=1, filename="book.pdf", ranges={})

    record = _ask(None, 1, "book", target, _question(expect_pages=[5, 9]), k=8)

    assert record["passage_rank"] == 4
    assert record["document_rank"] == 2
    assert record["targeted"] is True
    # Only the wrong-document chunk above the hit is crowding; the right document is not.
    assert record["ahead"] == ["other.pdf"]
    assert record["from_expected"] == 3


def test_a_never_found_answer_has_no_rank_and_the_whole_served_k_ahead_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_retrieve(
        monkeypatch,
        [_Chunk(document_id=2, page_number=n, filename=f"doc{n}.pdf") for n in range(1, 13)],
    )
    target = Target(document_id=1, filename="book.pdf", ranges={})

    record = _ask(None, 1, "book", target, _question(expect_pages=[5, 9]), k=12)

    assert record["document_rank"] is None
    assert record["passage_rank"] is None
    assert record["targeted"] is True
    assert record["returned"] == 12
    # Everything the product would serve outranked the missing answer.
    assert record["ahead"] == sorted(f"doc{n}.pdf" for n in range(1, PRODUCT_K + 1))


def test_an_expected_section_resolves_through_the_outline_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_retrieve(
        monkeypatch,
        [
            _Chunk(document_id=1, page_number=2, filename="book.pdf"),
            _Chunk(document_id=1, page_number=11, filename="book.pdf"),
        ],
    )
    target = Target(document_id=1, filename="book.pdf", ranges={"Ch / Sec": (10, 12)})

    record = _ask(None, 1, "book", target, _question(expect_section="Ch / Sec"), k=8)

    assert record["passage_rank"] == 2
    assert record["document_rank"] == 1
    assert record["expect_pages"] == [10, 12]


def test_a_section_the_outline_does_not_carry_is_a_configuration_error_not_a_miss() -> None:
    target = Target(document_id=1, filename="book.pdf", ranges={"Ch / Sec": (10, 12)})

    with pytest.raises(SystemExit, match="no outline entry"):
        _ask(None, 1, "book", target, _question(expect_section="Ch / Nowhere"), k=8)


def test_a_control_is_never_ranked_and_never_credits_a_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_retrieve(monkeypatch, [_Chunk(document_id=3, page_number=1)])

    record = _ask(None, 1, None, NO_TARGET, _question(id="control"), k=8)

    assert record["targeted"] is False
    assert record["document_rank"] is None
    assert record["passage_rank"] is None
    assert record["from_expected"] == 0


def test_the_ranking_is_cut_to_k_before_anything_is_scored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hit past `k` is a miss: the harness must not see deeper than the run's width."""
    _stub_retrieve(
        monkeypatch,
        [_Chunk(document_id=2, page_number=1)] * 4
        + [_Chunk(document_id=1, page_number=5, filename="book.pdf")],
    )
    target = Target(document_id=1, filename="book.pdf", ranges={})

    record = _ask(None, 1, "book", target, _question(expect_pages=[5, 9]), k=4)

    assert record["document_rank"] is None
    assert record["passage_rank"] is None
    assert record["returned"] == 4


# --------------------------------------------------------------------------- the report path


def _reported(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "id": "q",
        "question": "where",
        "expect_section": None,
        "expect_pages": [1, 2],
        "targeted": True,
        "document_rank": 1,
        "passage_rank": 1,
        "document_hit": True,
        "passage_hit": True,
        "returned": 32,
        "top_similarity": 0.8,
        "from_expected": 4,
        "ahead": [],
    }
    fields.update(overrides)
    return fields


def test_hit_rates_exclude_controls_and_count_a_never_found_in_every_denominator(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _report_retrieval(
        {
            "document": "book",
            "reranked": False,
            "seconds_per_question": 0.5,
            "k": 32,
            "class_documents": 3,
            "class_chunks": 100,
            "questions": [
                _reported(id="first", document_rank=1, passage_rank=1),
                _reported(id="fifth", document_rank=5, passage_rank=5, ahead=["crowd.pdf"]),
                _reported(
                    id="lost",
                    document_rank=None,
                    passage_rank=None,
                    document_hit=False,
                    passage_hit=False,
                    ahead=["crowd.pdf"],
                ),
                _reported(
                    id="control",
                    targeted=False,
                    expect_pages=None,
                    document_rank=None,
                    passage_rank=None,
                    document_hit=None,
                    passage_hit=None,
                    top_similarity=0.3,
                ),
            ],
        }
    )

    out = capsys.readouterr().out
    # Three targeted questions, never four: the control is not a denominator.
    assert "doc 1/3, passage 1/3" in out
    # The rank-5 hit arrives at k=8; the never-found stays in the denominator forever.
    assert "doc 2/3, passage 2/3" in out
    # The report never claims a width the run did not have.
    assert "k=64" not in out
    assert "never found: lost" in out
    assert "control: top similarity 0.3" in out


def test_crowding_is_counted_over_the_product_k_not_the_run_k(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _report_retrieval(
        {
            "document": "book",
            "k": 32,
            "class_chunks": 100,
            "class_documents": 2,
            "questions": [
                _reported(id="a", from_expected=8),
                _reported(id="b", from_expected=2, ahead=["crowd.pdf"]),
            ],
        }
    )

    out = capsys.readouterr().out
    assert f"10/{PRODUCT_K * 2} came from the expected document" in out
    assert "crowd.pdf: 1" in out


def test_a_report_written_before_phase_3_still_reports_rather_than_crashing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Old retrieval files carry neither `targeted` nor `expect_section`, and `report`
    reads every retrieval file in a workspace, so one old run must not kill the command."""
    _report_retrieval(
        {
            "document": "book",
            "k": 8,
            "questions": [
                {
                    "id": "old",
                    "question": "where",
                    "document_rank": 2,
                    "passage_rank": 2,
                    "returned": 8,
                    "top_similarity": 0.7,
                }
            ],
        }
    )

    out = capsys.readouterr().out
    assert "Retrieval over book" in out
    assert "old: doc 2 / passage 2 of 8" in out


# -------------------------------------------------------------------------------- _widened_k


def test_widened_k_sets_both_widths_and_restores_both_afterwards() -> None:
    before = (retrieval.K, retrieval.RERANK_FETCH_K, retrieval.rerank_server)

    with _widened_k(50, reranking=True):
        assert retrieval.K == 50
        assert retrieval.RERANK_FETCH_K == 50
        # The switch answers for availability; the real server is still underneath.
        assert retrieval.rerank_server.available is True
        assert retrieval.rerank_server.inner is before[2]

    assert (retrieval.K, retrieval.RERANK_FETCH_K, retrieval.rerank_server) == before


def test_widened_k_can_force_reranking_off_whatever_the_machine_has() -> None:
    with _widened_k(50, reranking=False):
        assert retrieval.rerank_server.available is False


def test_widened_k_restores_the_module_even_when_the_run_dies() -> None:
    before = (retrieval.K, retrieval.RERANK_FETCH_K, retrieval.rerank_server)

    with pytest.raises(RuntimeError, match="mid-run"), _widened_k(50, reranking=True):
        raise RuntimeError("mid-run")

    assert (retrieval.K, retrieval.RERANK_FETCH_K, retrieval.rerank_server) == before


def test_the_default_width_is_the_products_rerank_fetch_width() -> None:
    """The documented repro commands rely on a bare run measuring what the product fetches."""
    assert WIDE_K == retrieval.RERANK_FETCH_K


# ------------------------------------------------------------------------- duplicate ingests


def test_a_file_already_in_the_workspace_is_found_by_its_stem(
    db: sqlite3.Connection, class_id: int
) -> None:
    document_id = int(
        db.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
            "values (?, 'Fourier_Tables.pdf', '', 'application/pdf', 1, 'ready')",
            (class_id,),
        ).lastrowid
        or 0
    )

    assert _already_ingested(db, class_id, Path("/elsewhere/Fourier_Tables.pdf")) == document_id
    assert _already_ingested(db, class_id, Path("/elsewhere/Other_Tables.pdf")) is None
    # A document in another class is not a duplicate: retrieval never crosses a class.
    assert _already_ingested(db, class_id + 1, Path("/elsewhere/Fourier_Tables.pdf")) is None


# --------------------------------------------------------------------------- execution metadata


def _stub_retrieve_with_status(
    monkeypatch: pytest.MonkeyPatch,
    chunks: list[_Chunk],
    rerank_status: RerankStatus,
) -> None:
    """Stub retrieval to return chunks with a specific rerank status."""

    @dataclass(frozen=True)
    class _StatusResult:
        chunks: list[_Chunk]
        rerank_status: RerankStatus

    def fake(conn: object, class_id: int, query: str, budget: int) -> _StatusResult:
        return _StatusResult(chunks=chunks, rerank_status=rerank_status)

    monkeypatch.setattr(retrieval, "retrieve", fake)


def test_rerank_status_surfaces_in_ask_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """_ask must record the observed rerank status, not a hardcoded value."""
    _stub_retrieve_with_status(
        monkeypatch,
        [_Chunk(document_id=1, page_number=5, filename="book.pdf")],
        RerankStatus.APPLIED,
    )
    target = Target(document_id=1, filename="book.pdf", ranges={})

    record = _ask(None, 1, "book", target, _question(expect_pages=[5, 9]), k=8)
    assert record["rerank_status"] == "applied"


def test_weights_absent_status_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_retrieve_with_status(
        monkeypatch,
        [_Chunk(document_id=1, page_number=5, filename="book.pdf")],
        RerankStatus.WEIGHTS_ABSENT,
    )
    target = Target(document_id=1, filename="book.pdf", ranges={})

    record = _ask(None, 1, "book", target, _question(expect_pages=[5, 9]), k=8)
    assert record["rerank_status"] == "weights_absent"


def test_start_refused_status_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_retrieve_with_status(
        monkeypatch,
        [_Chunk(document_id=1, page_number=5, filename="book.pdf")],
        RerankStatus.START_REFUSED,
    )
    target = Target(document_id=1, filename="book.pdf", ranges={})

    record = _ask(None, 1, "book", target, _question(expect_pages=[5, 9]), k=8)
    assert record["rerank_status"] == "start_refused"


def test_timeout_status_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_retrieve_with_status(
        monkeypatch,
        [_Chunk(document_id=1, page_number=5, filename="book.pdf")],
        RerankStatus.TIMEOUT,
    )
    target = Target(document_id=1, filename="book.pdf", ranges={})

    record = _ask(None, 1, "book", target, _question(expect_pages=[5, 9]), k=8)
    assert record["rerank_status"] == "timeout"


def test_malformed_response_status_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_retrieve_with_status(
        monkeypatch,
        [_Chunk(document_id=1, page_number=5, filename="book.pdf")],
        RerankStatus.MALFORMED_RESPONSE,
    )
    target = Target(document_id=1, filename="book.pdf", ranges={})

    record = _ask(None, 1, "book", target, _question(expect_pages=[5, 9]), k=8)
    assert record["rerank_status"] == "malformed_response"


def test_upstream_error_status_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_retrieve_with_status(
        monkeypatch,
        [_Chunk(document_id=1, page_number=5, filename="book.pdf")],
        RerankStatus.UPSTREAM_ERROR,
    )
    target = Target(document_id=1, filename="book.pdf", ranges={})

    record = _ask(None, 1, "book", target, _question(expect_pages=[5, 9]), k=8)
    assert record["rerank_status"] == "upstream_error"


def test_not_requested_status_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_retrieve_with_status(
        monkeypatch,
        [_Chunk(document_id=1, page_number=5, filename="book.pdf")],
        RerankStatus.NOT_REQUESTED,
    )
    target = Target(document_id=1, filename="book.pdf", ranges={})

    record = _ask(None, 1, "book", target, _question(expect_pages=[5, 9]), k=8)
    assert record["rerank_status"] == "not_requested"


# ------------------------------------------------------------------- report with observed path


def test_degraded_report_shows_invalid_and_requested_vs_observed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A degraded run must say INVALID and show the requested vs observed distinction."""
    _report_retrieval(
        {
            "document": "book",
            "requested_rerank": True,
            "reranked": False,
            "observed_path": "embedding_order",
            "degraded": True,
            "degradation_reasons": ["weights_absent"],
            "valid": False,
            "k": 8,
            "class_documents": 2,
            "class_chunks": 100,
            "questions": [
                _reported(id="q1", document_rank=1, passage_rank=1, rerank_status="weights_absent"),
            ],
        }
    )

    out = capsys.readouterr().out
    assert "INVALID" in out
    assert "DEGRADED" in out
    assert "requested: rerank" in out
    assert "observed: embedding_order" in out


def test_valid_reranked_report_shows_no_invalid(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _report_retrieval(
        {
            "document": "book",
            "requested_rerank": True,
            "reranked": True,
            "observed_path": "reranked",
            "valid": True,
            "k": 8,
            "class_documents": 2,
            "class_chunks": 100,
            "questions": [
                _reported(id="q1", document_rank=1, passage_rank=1, rerank_status="applied"),
            ],
        }
    )

    out = capsys.readouterr().out
    assert "INVALID" not in out
    assert "DEGRADED" not in out
    assert "reranked" in out


# ------------------------------------------------------ wrong-document scoring and no-answer


def test_a_right_passage_from_the_wrong_document_is_a_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hit pages are right, but the document id is wrong. Must be rank None."""
    _stub_retrieve(
        monkeypatch,
        [
            _Chunk(document_id=2, page_number=5, filename="wrong.pdf"),
            _Chunk(document_id=2, page_number=6, filename="wrong.pdf"),
        ],
    )
    target = Target(document_id=1, filename="book.pdf", ranges={})

    record = _ask(None, 1, "book", target, _question(expect_pages=[5, 9]), k=8)

    assert record["document_rank"] is None
    assert record["passage_rank"] is None
    assert record["targeted"] is True


def test_no_answer_control_has_zero_from_expected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A control question must have from_expected == 0 regardless of what comes back."""
    _stub_retrieve(
        monkeypatch,
        [_Chunk(document_id=3, page_number=1, filename="doc.pdf")],
    )

    record = _ask(None, 1, None, NO_TARGET, _question(id="control"), k=8)

    assert record["from_expected"] == 0
    assert record["targeted"] is False
    assert record["document_rank"] is None
    assert record["passage_rank"] is None


# ------------------------------------------------ invalid/degraded runs never published valid


def test_degraded_run_is_not_treated_as_valid_baseline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A report with valid=False must never be silently treated as a passing baseline."""
    report = {
        "document": "book",
        "requested_rerank": True,
        "reranked": False,
        "observed_path": "embedding_order",
        "degraded": True,
        "degradation_reasons": ["start_refused"],
        "valid": False,
        "k": 8,
        "class_documents": 2,
        "class_chunks": 50,
        "questions": [
            _reported(id="q1", document_rank=1, passage_rank=1, rerank_status="start_refused"),
            _reported(id="q2", document_rank=2, passage_rank=2, rerank_status="start_refused"),
        ],
    }

    _report_retrieval(report)
    out = capsys.readouterr().out

    assert "INVALID" in out
    assert report["valid"] is False
    assert report["reranked"] is False


# ---------------------------------------------------------- passage-level scoring (item 1)


def test_passage_hit_when_anchor_appears_in_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_retrieve(
        monkeypatch,
        [
            _Chunk(document_id=1, page_number=1, filename="book.pdf", content="other stuff"),
            _Chunk(
                document_id=1,
                page_number=1,
                filename="book.pdf",
                content="before 3.3 Newton's Third Law after",
            ),
        ],
    )
    target = Target(
        document_id=1,
        filename="book.pdf",
        ranges={},
        passage_anchors={"ch3::3.3-third-law": "3.3 Newton's Third Law"},
    )

    record = _ask(
        None,
        1,
        "book",
        target,
        _question(expect_passage_id="ch3::3.3-third-law", expect_pages=[1, 1]),
        k=8,
    )

    assert record["passage_hit"] is True
    assert record["passage_rank"] == 2
    assert record["targeted"] is True


def test_passage_miss_when_anchor_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_retrieve(
        monkeypatch,
        [
            _Chunk(document_id=1, page_number=1, filename="book.pdf", content="irrelevant"),
        ],
    )
    target = Target(
        document_id=1,
        filename="book.pdf",
        ranges={},
        passage_anchors={"ch3::3.3-third-law": "3.3 Newton's Third Law"},
    )

    record = _ask(
        None,
        1,
        "book",
        target,
        _question(expect_passage_id="ch3::3.3-third-law"),
        k=8,
    )

    assert record["passage_hit"] is False
    assert record["passage_rank"] is None


def test_right_document_wrong_passage_is_document_hit_passage_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Correct document but wrong section: document_rank set, passage_rank None."""
    _stub_retrieve(
        monkeypatch,
        [
            _Chunk(
                document_id=1,
                page_number=1,
                filename="book.pdf",
                content="3.5 Applications: Inclined Planes on a slope",
            ),
        ],
    )
    target = Target(
        document_id=1,
        filename="book.pdf",
        ranges={},
        passage_anchors={
            "ch3::3.3-third-law": "3.3 Newton's Third Law",
            "ch3::3.5-inclined": "3.5 Applications: Inclined Planes",
        },
    )

    record = _ask(
        None,
        1,
        "book",
        target,
        _question(expect_passage_id="ch3::3.3-third-law"),
        k=8,
    )

    assert record["document_rank"] == 1
    assert record["document_hit"] is True
    assert record["passage_rank"] is None
    assert record["passage_hit"] is False


def test_passage_id_with_no_anchors_in_target_is_a_config_error() -> None:
    """An expect_passage_id that cannot resolve is a broken annotation, not a miss."""
    target = Target(document_id=1, filename="book.pdf", ranges={})

    with pytest.raises(SystemExit, match="cannot be resolved"):
        _ask(
            None,
            1,
            "book",
            target,
            _question(expect_passage_id="ch3::3.3-third-law"),
            k=8,
        )


# ---------------------------------------------------------- EMPTY_INPUT semantics (item 6)


def test_empty_input_status_on_control_is_legitimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-answer control producing EMPTY_INPUT is not a failure."""
    _stub_retrieve_with_status(
        monkeypatch,
        [],
        RerankStatus.EMPTY_INPUT,
    )
    record = _ask(None, 1, None, NO_TARGET, _question(id="control"), k=8)

    assert record["rerank_status"] == "empty_input"
    assert record["targeted"] is False


def test_empty_input_status_on_targeted_is_a_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A targeted question that returns empty is a miss, not a success."""
    _stub_retrieve_with_status(
        monkeypatch,
        [],
        RerankStatus.EMPTY_INPUT,
    )
    target = Target(document_id=1, filename="book.pdf", ranges={})
    record = _ask(None, 1, "book", target, _question(expect_pages=[5, 9]), k=8)

    assert record["rerank_status"] == "empty_input"
    assert record["targeted"] is True
    assert record["document_rank"] is None
    assert record["passage_rank"] is None


# ----------------------------------------------------- compare identity pairing (items 3, 4)


def test_score_identity_uses_observed_path_and_report_file() -> None:
    a = {"observed_path": "reranked", "report_file": "scores-phys201.json"}
    b = {"observed_path": "embedding_order", "report_file": "scores-phys201.json"}
    assert _score_identity(a) != _score_identity(b)


def test_score_identity_same_when_content_matches() -> None:
    a = {"observed_path": "reranked", "report_file": "scores-phys201.json", "mrr": 0.8}
    b = {"observed_path": "reranked", "report_file": "scores-phys201.json", "mrr": 0.5}
    assert _score_identity(a) == _score_identity(b)


def test_compatibility_check_rejects_different_corpus_hash() -> None:
    shared = {"questions_hash": "qh1", "embedding_model": "e5", "embedding_dim": 384}
    meta_a = {"corpus_hash": "abc123", **shared}
    meta_b = {"corpus_hash": "def456", **shared}
    problems = _check_compatibility(meta_a, meta_b, allow_override=False)
    assert any("INCOMPATIBLE" in p and "corpus_hash" in p for p in problems)


def test_compatibility_check_accepts_matching_metadata() -> None:
    meta = {
        "corpus_hash": "abc123",
        "questions_hash": "qh1",
        "embedding_model": "e5",
        "embedding_dim": 384,
    }
    problems = _check_compatibility(meta, meta, allow_override=False)
    assert problems == []


def test_compatibility_check_warns_with_force() -> None:
    shared = {"questions_hash": "qh1", "embedding_model": "e5", "embedding_dim": 384}
    meta_a = {"corpus_hash": "abc123", **shared}
    meta_b = {"corpus_hash": "def456", **shared}
    problems = _check_compatibility(meta_a, meta_b, allow_override=True)
    assert any("WARNING" in p for p in problems)
    assert not any("INCOMPATIBLE" in p for p in problems)


def test_compatibility_fails_closed_on_missing_required_fields() -> None:
    """A required field absent on either side is INCOMPATIBLE, not silently skipped."""
    meta_a = {"corpus_hash": None, "embedding_model": "e5"}
    meta_b = {"corpus_hash": "def456", "embedding_model": "e5"}
    problems = _check_compatibility(meta_a, meta_b, allow_override=False)
    assert any("INCOMPATIBLE" in p and "corpus_hash" in p for p in problems)


# ------------------------------------------------- INVALID_JSON status surfaces (item 5)


def test_invalid_json_status_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_retrieve_with_status(
        monkeypatch,
        [_Chunk(document_id=1, page_number=5, filename="book.pdf")],
        RerankStatus.INVALID_JSON,
    )
    target = Target(document_id=1, filename="book.pdf", ranges={})

    record = _ask(None, 1, "book", target, _question(expect_pages=[5, 9]), k=8)
    assert record["rerank_status"] == "invalid_json"


# ------------------------------------------------- wrong document identity (item 2)


def test_wrong_document_top1_uses_expect_filename() -> None:
    """The wrong-document check must use expect_filename, not reconstruct from neighbours."""
    import tempfile

    from scripts.eval_ingest import Workspace, cmd_score

    with tempfile.TemporaryDirectory() as tmp:
        ws = Workspace(root=Path(tmp))
        retrieval_data = {
            "k": 8,
            "valid": True,
            "observed_path": "embedding_order",
            "questions": [
                {
                    "id": "q1",
                    "question": "what?",
                    "expect_document": "notes",
                    "expect_filename": "notes.md",
                    "targeted": True,
                    "document_rank": 1,
                    "passage_rank": 1,
                    "document_hit": True,
                    "passage_hit": True,
                    "returned": 8,
                    "top_similarity": 0.9,
                    "rerank_status": "not_requested",
                    "from_expected": 4,
                    "ahead": [],
                    "neighbours": [
                        {
                            "document": "notes.md",
                            "page": 1,
                            "similarity": 0.9,
                            "section_title": None,
                            "opening": "x",
                        }
                    ],
                },
            ],
        }
        ws.write("retrieval-test", retrieval_data)

        import argparse

        args = argparse.Namespace(workspace=tmp)
        cmd_score(args)

        scores = ws.read("scores-test")
        assert scores["wrong_document_top1"] == 0


def test_cmd_score_nonzero_exit_on_invalid_run() -> None:
    """cmd_score must return nonzero when valid=False so CI catches it."""
    import tempfile

    from scripts.eval_ingest import Workspace, cmd_score

    with tempfile.TemporaryDirectory() as tmp:
        ws = Workspace(root=Path(tmp))
        retrieval_data = {
            "k": 8,
            "valid": False,
            "observed_path": "embedding_order",
            "questions": [
                {
                    "id": "q1",
                    "question": "what?",
                    "expect_document": "notes",
                    "expect_filename": "notes.pdf",
                    "targeted": True,
                    "document_rank": 1,
                    "passage_rank": 1,
                    "document_hit": True,
                    "passage_hit": True,
                    "returned": 8,
                    "top_similarity": 0.9,
                    "rerank_status": "not_requested",
                    "from_expected": 4,
                    "ahead": [],
                    "neighbours": [],
                },
            ],
        }
        ws.write("retrieval-test", retrieval_data)

        import argparse

        args = argparse.Namespace(workspace=tmp)
        assert cmd_score(args) != 0


def test_compatibility_check_includes_retrieval_k() -> None:
    """retrieval_k, requested_rerank, and rerank_model are compared when present."""
    meta_a = {
        "corpus_hash": "abc123",
        "questions_hash": "qh1",
        "embedding_model": "e5",
        "embedding_dim": 384,
        "retrieval_k": 32,
    }
    meta_b = {**meta_a, "retrieval_k": 64}
    problems = _check_compatibility(meta_a, meta_b, allow_override=False)
    assert any("retrieval_k" in p for p in problems)
