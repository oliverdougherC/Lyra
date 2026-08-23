"""Score the unusable-text-layer detector against the labelled corpus (PLA-148).

The backend tests defend the detector's contract on individual fixtures. This harness
answers the question that decides whether the heuristics are worth shipping: across a
labelled corpus, how often does the page-quality gate flag a page that was fine (a false
positive, the expensive kind - it re-recognizes a page that already read), and how often
does it miss a page that was junk (a false negative). It reports precision and recall,
names every false positive and false negative by category, and measures the before/after
effect on what actually reaches the index and what retrieval can find.

It drives the real code path - `parse.page_skip_reason` and `parse.chunk_document` - against
the committed corpus in `scripts/eval_corpora/text_layer.json`. Nothing here reaches an
endpoint, opens a PDF, or touches the student's own data. `photographed` pages are scored
by a rendered fixture in the backend tests instead, because that gate needs the page image.

Usage:

    python scripts/eval_text_layer.py            # print the report
    python scripts/eval_text_layer.py --json      # emit the report as JSON
"""

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.rag.chunk import chunk_document, detect_doc_type  # noqa: E402
from backend.rag.parse import (  # noqa: E402
    ParsedDocument,
    ParsedPage,
    is_scanned_page,
    page_skip_reason,
)

CORPUS_PATH = ROOT / "scripts" / "eval_corpora" / "text_layer.json"

# Retrieval-quality probes: a term the reader would search for, and whether the page it
# belongs to is one Lyra should index. A `good` probe must still be findable after the
# change (retrieval recall preserved); a `junk` probe must stop being findable, because the
# page it came from is now dropped (retrieval precision improved).
_GOOD_PROBES = ("determinant", "epsilon", "chain rule", "binary_search", "enrollment")
_JUNK_PROBES = ("watermark", "CONFIDENTIAL", "tliat", "qxz", "sample sample")


@dataclass
class Confusion:
    """The four outcomes of a binary flag, and the metrics they imply."""

    true_positive: int = 0
    false_positive: int = 0
    true_negative: int = 0
    false_negative: int = 0

    def record(self, *, actual_unusable: bool, flagged: bool) -> None:
        if actual_unusable and flagged:
            self.true_positive += 1
        elif actual_unusable and not flagged:
            self.false_negative += 1
        elif not actual_unusable and flagged:
            self.false_positive += 1
        else:
            self.true_negative += 1

    @property
    def precision(self) -> float:
        flagged = self.true_positive + self.false_positive
        return self.true_positive / flagged if flagged else 1.0

    @property
    def recall(self) -> float:
        actual = self.true_positive + self.false_negative
        return self.true_positive / actual if actual else 1.0


@dataclass
class Report:
    """Everything the harness measured, ready to print or serialize."""

    version: int
    total: int
    detector: Confusion
    detectable: Confusion
    false_positives: list[str] = field(default_factory=list)
    false_negatives: list[dict[str, str]] = field(default_factory=list)
    per_category_recall: dict[str, float] = field(default_factory=dict)
    indexed_pages_before: int = 0
    indexed_pages_after: int = 0
    junk_pages_indexed_before: int = 0
    junk_pages_indexed_after: int = 0
    good_pages_indexed_before: int = 0
    good_pages_indexed_after: int = 0
    retrieval: dict[str, dict[str, int]] = field(default_factory=dict)


def load_corpus(path: Path = CORPUS_PATH) -> tuple[int, list[dict[str, object]]]:
    """The versioned corpus: its schema version, and its list of labelled cases."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return int(data["version"]), list(data["cases"])


def _old_flag(text: str) -> bool:
    """The pre-PLA-148 gate on text alone: only a sparse page was dropped.

    The photographed half of the old gate needs the page image, which a text corpus does
    not carry, so on this corpus the old behavior is exactly the scanned-page rule - which
    is the point: a substantial junk text layer sailed through it.
    """
    return is_scanned_page(text)


def _new_flag(text: str) -> bool:
    """The current gate on text alone: sparse, plus the unusable-text-layer signals."""
    return page_skip_reason(text, photographed=False) is not None


def evaluate(cases: list[dict[str, object]], version: int) -> Report:
    """Score every case and assemble the report."""
    report = Report(version=version, total=len(cases), detector=Confusion(), detectable=Confusion())
    per_category_hits: dict[str, list[bool]] = defaultdict(list)

    kept_before: list[str] = []
    kept_after: list[str] = []

    for index, case in enumerate(cases, start=1):
        text = str(case["text"])
        category = str(case["category"])
        actual_unusable = case["label"] == "unusable"
        flagged = _new_flag(text)

        report.detector.record(actual_unusable=actual_unusable, flagged=flagged)
        if case.get("detectable", True):
            report.detectable.record(actual_unusable=actual_unusable, flagged=flagged)

        if not actual_unusable and flagged:
            report.false_positives.append(f"{case['name']} ({category})")
        if actual_unusable and not flagged:
            report.false_negatives.append({"name": str(case["name"]), "category": category})
        if actual_unusable:
            per_category_hits[category].append(flagged)

        # A page is "indexed" (its text reaches chunking) when the gate keeps it. Page
        # numbers are the case index, which is enough to build a document to chunk.
        page = ParsedPage(page_number=index, text=text)
        if not _old_flag(text):
            kept_before.append(text)
            report.indexed_pages_before += 1
            if actual_unusable:
                report.junk_pages_indexed_before += 1
            else:
                report.good_pages_indexed_before += 1
        if not flagged:
            kept_after.append(text)
            report.indexed_pages_after += 1
            if actual_unusable:
                report.junk_pages_indexed_after += 1
            else:
                report.good_pages_indexed_after += 1
        del page

    report.per_category_recall = {
        category: sum(hits) / len(hits) for category, hits in sorted(per_category_hits.items())
    }
    report.retrieval = {
        "before": _retrieval_hits(kept_before),
        "after": _retrieval_hits(kept_after),
    }
    return report


def _retrieval_hits(pages: list[str]) -> dict[str, int]:
    """Run the real chunker over the kept pages and probe what retrieval could find.

    A lightweight lexical proxy: retrieval can only ever return text that was chunked, so a
    probe term found in some chunk is a term retrieval could surface. Good probes must stay
    findable (recall) and junk probes must disappear (precision) once the junk pages are
    dropped. The chunker is the real one, so this measures the actual indexable surface.
    """
    parsed = ParsedDocument(
        pages=[ParsedPage(page_number=i, text=text) for i, text in enumerate(pages, start=1)],
        pages_total=len(pages),
        pages_skipped=0,
    )
    chunks = chunk_document(parsed, detect_doc_type("corpus.pdf", parsed.full_text, parsed))
    haystack = "\n".join(chunk.content for chunk in chunks).lower()
    return {
        "good_probes_found": sum(1 for probe in _GOOD_PROBES if probe.lower() in haystack),
        "junk_probes_found": sum(1 for probe in _JUNK_PROBES if probe.lower() in haystack),
    }


def _format(report: Report) -> str:
    """The human-readable report."""
    lines = [
        f"Text-layer detector eval - corpus v{report.version}, {report.total} cases",
        "=" * 64,
        "",
        "Detector (all cases):",
        f"  precision {report.detector.precision:.3f}   recall {report.detector.recall:.3f}"
        f"   (TP {report.detector.true_positive}  FP {report.detector.false_positive}"
        f"  FN {report.detector.false_negative}  TN {report.detector.true_negative})",
        "",
        "Detector (cases the signals claim to catch):",
        f"  precision {report.detectable.precision:.3f}   recall {report.detectable.recall:.3f}"
        f"   (TP {report.detectable.true_positive}  FP {report.detectable.false_positive}"
        f"  FN {report.detectable.false_negative}  TN {report.detectable.true_negative})",
        "",
        "Recall by category (unusable cases):",
    ]
    for category, recall in report.per_category_recall.items():
        lines.append(f"  {category:<22} {recall:.3f}")
    lines += ["", "False positives (a usable page wrongly flagged - must be none):"]
    lines += [f"  {name}" for name in report.false_positives] or ["  none"]
    lines += ["", "False negatives (an unusable page missed), by category:"]
    lines += [f"  {fn['name']} ({fn['category']})" for fn in report.false_negatives] or ["  none"]
    lines += [
        "",
        "Extraction quality (pages reaching the index):",
        f"  before PLA-148: {report.indexed_pages_before} pages"
        f"  ({report.junk_pages_indexed_before} junk, {report.good_pages_indexed_before} good)",
        f"  after  PLA-148: {report.indexed_pages_after} pages"
        f"  ({report.junk_pages_indexed_after} junk, {report.good_pages_indexed_after} good)",
        "",
        "Retrieval quality (probe terms findable in the chunked index):",
        f"  before: good {report.retrieval['before']['good_probes_found']}/{len(_GOOD_PROBES)}"
        f"  junk {report.retrieval['before']['junk_probes_found']}/{len(_JUNK_PROBES)}",
        f"  after:  good {report.retrieval['after']['good_probes_found']}/{len(_GOOD_PROBES)}"
        f"  junk {report.retrieval['after']['junk_probes_found']}/{len(_JUNK_PROBES)}",
    ]
    return "\n".join(lines)


def _as_dict(report: Report) -> dict[str, object]:
    return {
        "version": report.version,
        "total": report.total,
        "detector": vars(report.detector),
        "detectable": vars(report.detectable),
        "precision": report.detector.precision,
        "recall": report.detector.recall,
        "detectable_precision": report.detectable.precision,
        "detectable_recall": report.detectable.recall,
        "false_positives": report.false_positives,
        "false_negatives": report.false_negatives,
        "per_category_recall": report.per_category_recall,
        "extraction": {
            "before": {
                "pages": report.indexed_pages_before,
                "junk": report.junk_pages_indexed_before,
                "good": report.good_pages_indexed_before,
            },
            "after": {
                "pages": report.indexed_pages_after,
                "junk": report.junk_pages_indexed_after,
                "good": report.good_pages_indexed_after,
            },
        },
        "retrieval": report.retrieval,
    }


def build_report() -> Report:
    """Load the corpus and score it. The one entry point the tests share with the CLI."""
    version, cases = load_corpus()
    return evaluate(cases, version)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args()
    report = build_report()
    if args.json:
        print(json.dumps(_as_dict(report), indent=2))
    else:
        print(_format(report))


if __name__ == "__main__":
    main()
