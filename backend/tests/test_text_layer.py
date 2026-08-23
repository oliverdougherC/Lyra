"""The unusable-text-layer detector: what it flags, what it must never flag, and its score.

PLA-148 broadens the page-quality gate beyond "no text" and "a picture wearing a scrap of
text" to a third kind of page: a substantial text layer that is nonetheless junk - OCR
character soup, or a broken extraction that repeats one line or token down the page.

The detector is tuned for precision: a false positive re-recognizes a page that was already
fine, so a valid sparse title, a matrix of lone digits, a page of code, an equation page,
and a table of numbers must all stay readable. These tests pin that guarantee case by case,
pin the two catchable pathologies, and assert the corpus score so a later change cannot
quietly trade precision away. The corpus and its full report live in
scripts/eval_corpora/text_layer.json and scripts/eval_text_layer.py.
"""

import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.rag import parse  # noqa: E402
from backend.rag.parse import (  # noqa: E402
    CHARACTER_SOUP,
    PAGE_SKIP_REASONS,
    REPETITION,
    UNUSABLE_TEXT_FLAGS,
    classify_text_layer,
    page_skip_reason,
)
from scripts.eval_text_layer import Confusion, build_report  # noqa: E402

# --- Pages the detector must leave alone (precision) ----------------------------------

_USABLE = {
    "prose": (
        "The determinant of a triangular matrix is the product of the entries on its "
        "diagonal. It is invertible exactly when none of those entries is zero."
    ),
    "sparse_title": "Introduction to Real Analysis\nLecture Notes, Fall 2026\nProfessor A. Turing",
    "matrix": "1 0 0 2\n0 1 0 3\n0 0 1 4\n2 3 4 1\n5 6 7 8\n9 8 7 6",
    "equation": "f(x) = x^2 + 3x - 5\n∫ f(x) dx = x^3/3 + 3x^2/2 - 5x + C\nlim_{x->0} sin(x)/x = 1",
    "code": (
        "def binary_search(items, target):\n    low, high = 0, len(items) - 1\n"
        "    while low <= high:\n        mid = (low + high) // 2\n"
        "        if items[mid] == target:\n            return mid\n    return -1"
    ),
    "number_table": "Year Enrollment Passing\n2019 118 0.81\n2020 124 0.79\n2021 131 0.84",
}


@pytest.mark.parametrize("name", sorted(_USABLE))
def test_a_usable_page_is_never_flagged(name: str) -> None:
    assert classify_text_layer(_USABLE[name]) is None


# --- Pages the detector must catch (recall on the catchable pathologies) ---------------


def test_ocr_character_soup_is_flagged() -> None:
    soup = (
        "rn cl vv lll tt nn gg hh mn wr qxz bk dd ff kk pq zx cv bn mk lp rnm clm vln bkt "
        "qws zxc vbn mkl pqr ttn ggn hhr wrs"
    )
    assert classify_text_layer(soup) == CHARACTER_SOUP


def test_a_repeated_header_line_is_flagged() -> None:
    page = "\n".join(["CONFIDENTIAL DRAFT DO NOT DISTRIBUTE"] * 8)
    assert classify_text_layer(page) == REPETITION


def test_a_repeated_single_token_is_flagged() -> None:
    assert classify_text_layer("watermark " * 24) == REPETITION


def test_repetition_is_reported_before_soup_for_a_stable_code() -> None:
    # A page that is both repetitive and soup-like reports one deterministic reason.
    page = "\n".join(["qxz qxz qxz qxz qxz qxz"] * 8)
    assert classify_text_layer(page) == REPETITION


# --- The combined page-skip gate and its bounded, privacy-safe reasons -----------------


def test_page_skip_reason_covers_the_three_gates() -> None:
    assert page_skip_reason("", photographed=False) == parse.SPARSE_TEXT
    assert page_skip_reason("a real sentence of readable prose here", photographed=True) == (
        parse.PHOTOGRAPHED
    )
    assert page_skip_reason("watermark " * 24, photographed=False) == REPETITION
    assert page_skip_reason(_USABLE["prose"], photographed=False) is None


def test_every_reason_code_is_bounded_and_carries_no_text() -> None:
    # A reason code is safe to log or surface for diagnostics: it names why, never what.
    for reason in (*UNUSABLE_TEXT_FLAGS, parse.SPARSE_TEXT, parse.PHOTOGRAPHED):
        assert reason in PAGE_SKIP_REASONS
        assert reason.replace("_", "").isalpha()


def test_dropping_pages_logs_reasons_but_never_page_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "watermark " * 24
    pages = [
        (1, "A genuinely readable paragraph of prose about matrices and determinants.", False),
        (2, secret, False),
    ]
    with caplog.at_level(logging.INFO, logger="backend.rag.parse"):
        result = parse._drop_scanned_pages(pages, outline=[])

    assert result.pages_skipped == 1
    assert [page.page_number for page in result.pages] == [1]
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert REPETITION in logged and "page 2" in logged
    # The page's own text never appears in the log.
    assert "watermark" not in logged


# --- The corpus score: the shipping guarantee, guarded in CI ---------------------------


def test_detector_makes_no_false_positives_on_the_corpus() -> None:
    # The expensive error: flagging a usable page re-recognizes something that already read.
    report = build_report()
    assert report.false_positives == []
    assert report.detector.precision == 1.0


def test_detector_catches_every_pathology_it_claims_to() -> None:
    report = build_report()
    # Cases marked `detectable` in the corpus - character soup, repetition, image-only - are
    # all caught. The genuinely hard categories (reordered columns, coherent overlay,
    # ligature soup, scattered glyphs) are reported as known false negatives, not chased.
    assert report.detectable.recall == 1.0


def test_the_change_removes_junk_from_the_index_without_dropping_good_pages() -> None:
    report = build_report()
    # Good pages reach the index exactly as before; junk pages no longer do (except the
    # honest false negatives the report names).
    assert report.good_pages_indexed_after == report.good_pages_indexed_before
    assert report.junk_pages_indexed_after < report.junk_pages_indexed_before


def test_retrieval_keeps_good_content_and_loses_junk() -> None:
    report = build_report()
    before, after = report.retrieval["before"], report.retrieval["after"]
    # Every good probe stays findable; the junk that retrieval could surface shrinks.
    assert after["good_probes_found"] == before["good_probes_found"]
    assert after["junk_probes_found"] < before["junk_probes_found"]


def test_the_scoring_arithmetic_is_correct() -> None:
    # A harness that scores wrongly is worse than none: pin precision and recall against a
    # confusion matrix counted by hand.
    confusion = Confusion()
    for _ in range(6):
        confusion.record(actual_unusable=True, flagged=True)  # 6 true positives
    for _ in range(2):
        confusion.record(actual_unusable=True, flagged=False)  # 2 false negatives
    confusion.record(actual_unusable=False, flagged=True)  # 1 false positive
    for _ in range(3):
        confusion.record(actual_unusable=False, flagged=False)  # 3 true negatives

    assert confusion.precision == 6 / 7
    assert confusion.recall == 6 / 8
    # No positives predicted, and no actual positives, are both defined as perfect rather
    # than a divide-by-zero.
    assert Confusion().precision == 1.0
    assert Confusion().recall == 1.0
