"""Evaluation of profile extraction: what a model finds, what it invents, what it misses.

The one process the backend test suite cannot defend. Every other stage of ingestion has a
right answer a fixture can assert - a chunk is under the ceiling or it is not, a citation
resolves or it does not - and extraction has a *judgment*, made by whatever model the
student happens to be pointing at. A contract test can prove the prompt asks a homework
sheet for three fields. Only a run against a real model can tell you whether a 7B model
answers it with three facts or with the professor's name off a practice exam.

That gap is the whole reason this exists, and it is why the metrics are shaped the way
they are. Recall alone would reward a model that returns everything; precision alone would
reward one that returns nothing. So a run reports four numbers per case:

- `recall`: of the facts the case says are really in the document, how many were found.
- `contamination`: facts matching something the case says must never be recorded. This is
  the number the document-type work exists to drive to zero - an instructor's name taken
  off a reused answer key, a course code proposed as a discovery.
- `verified`: the share of facts whose `quote` was found in the document. A model that is
  inventing shows up here first, and it needs no labelling to measure, so it is the one
  metric that works on a corpus nobody has annotated.
- `volume`: facts per document. Noise is mostly a quantity problem.

**Run it against more than one model.** The point of the prompt work is that it holds up on
a small local model, and a number from one model tells you nothing about that. `compare`
takes several runs and puts them beside each other.

**It never touches the student's own data.** Like `eval_ingest.py`, every run works in its
own workspace with its own database, set before anything opens a connection. The endpoint
configuration is copied from the real database so the model matches what the app would use.

Usage:

    python scripts/eval_extract.py run --corpus scripts/eval_corpora/extraction.json
    python scripts/eval_extract.py run --corpus ... --model qwen2.5-7b --label small
    python scripts/eval_extract.py report
    python scripts/eval_extract.py compare small large
"""

import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import settings  # noqa: E402
from backend.core import profiles  # noqa: E402
from backend.core.app_settings import update_settings_row  # noqa: E402
from backend.rag.chunk import detect_doc_type  # noqa: E402
from backend.storage.database import connect, migrate  # noqa: E402

logger = logging.getLogger("lyra.eval.extract")

DEFAULT_WORKSPACE = ROOT / "data" / "eval-extract"
DEFAULT_CORPUS = ROOT / "scripts" / "eval_corpora" / "extraction.json"


@dataclass(frozen=True)
class Case:
    """One labelled document.

    Attributes:
        name: What the case is called in the report.
        filename: The name the document is uploaded under. Load-bearing rather than
            cosmetic: `detect_doc_type` reads it first, so it is what decides which
            extraction profile the case exercises.
        text: The document's text.
        expect_doc_type: What the classifier should call it, or None to skip that check.
        expect: Subjects that really are stated in the document. Matched case-insensitively
            as substrings against each extracted fact, so `fourier series` matches a fact
            named `Fourier Series`.
        forbid: Patterns that must never appear in any extracted fact. This is where a case
            records the trap it was built around - the professor who teaches a different
            course, the term the answer key was written for.
    """

    name: str
    filename: str
    text: str
    expect_doc_type: str | None = None
    expect: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()


@dataclass
class CaseResult:
    """What one case produced. Every count is over the facts extraction actually stored."""

    name: str
    filename: str
    doc_type: str
    doc_type_ok: bool
    facts: list[dict[str, str]] = field(default_factory=list)
    found: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    contaminants: list[str] = field(default_factory=list)
    verified: int = 0
    seconds: float = 0.0
    error: str = ""

    @property
    def recall(self) -> float | None:
        """None rather than 1.0 for a case that labelled nothing, so it averages honestly."""
        total = len(self.found) + len(self.missed)
        return None if total == 0 else len(self.found) / total

    @property
    def verified_rate(self) -> float | None:
        return None if not self.facts else self.verified / len(self.facts)

    def as_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "filename": self.filename,
            "doc_type": self.doc_type,
            "doc_type_ok": self.doc_type_ok,
            "fact_count": len(self.facts),
            "verified": self.verified,
            "verified_rate": _rounded(self.verified_rate),
            "recall": _rounded(self.recall),
            "found": self.found,
            "missed": self.missed,
            "contaminants": self.contaminants,
            "seconds": round(self.seconds, 2),
            "error": self.error,
            "facts": self.facts,
        }


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


class Workspace:
    """Where one evaluation run keeps its database and its reports."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def db_path(self) -> Path:
        return self.root / "lyra.db"

    def report(self, name: str) -> Path:
        return self.root / f"{name}.json"

    def read(self, name: str) -> dict[str, object]:
        path = self.report(name)
        if not path.exists():
            raise SystemExit(f"{path.name} is missing. Run `eval_extract.py run --label {name}`.")
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, name: str, payload: dict[str, object]) -> None:
        self.report(name).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def prepare(workspace: Workspace, source_db: Path, model: str | None) -> sqlite3.Connection:
    """Point the backend at the workspace, then open and migrate its database.

    The settings object is mutated rather than re-read from the environment because every
    module already holds a reference to it, and it happens before the first `connect` so
    nothing can have opened the real database by the time this returns.
    """
    settings.data_dir = workspace.root
    settings.db_path = workspace.db_path
    workspace.root.mkdir(parents=True, exist_ok=True)
    settings.ensure_directories()

    conn = connect()
    migrate(conn)
    _copy_settings(conn, source_db, model)
    return conn


def _copy_settings(conn: sqlite3.Connection, source_db: Path, model: str | None) -> None:
    """Copy the endpoint configuration from the real database, optionally overriding the model.

    The override is what makes a run comparable across model sizes without the student
    having to reconfigure the app between runs.
    """
    if not source_db.exists():
        raise SystemExit(f"No database at {source_db}. Configure Lyra first, or pass --source-db.")

    source = connect(source_db)
    try:
        row = source.execute(
            "select endpoint_url, model, context_window from settings where id = 1"
        ).fetchone()
    finally:
        source.close()
    if row is None or not str(row["endpoint_url"] or "").strip():
        raise SystemExit("The source database has no endpoint configured. Set one in Settings.")

    update_settings_row(
        conn,
        {
            "endpoint_url": str(row["endpoint_url"]),
            "model": model if model is not None else row["model"],
            "context_window": int(row["context_window"]),
            "extraction_enabled": 1,
            # Every case is fed in as text this script already holds, so nothing is read
            # off disk and no upload is parsed. The acknowledgement matters because the
            # configured endpoint may be remote, and extraction is a path that sends
            # document text: a run that silently skipped would report zero facts and look
            # like a model failure.
            "remote_ack": 1,
        },
    )


def load_corpus(path: Path) -> list[Case]:
    """Read a corpus file into cases, resolving any that point at a document on disk."""
    if not path.exists():
        raise SystemExit(f"No corpus at {path}.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(raw, list) or not raw:
        raise SystemExit(f"{path.name} carries no cases.")

    cases: list[Case] = []
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise SystemExit(f"Case {index} in {path.name} is not an object.")
        text = entry.get("text")
        if text is None and (relative := entry.get("path")):
            text = (path.parent / str(relative)).read_text(encoding="utf-8")
        if not isinstance(text, str) or not text.strip():
            raise SystemExit(f"Case {index} in {path.name} has no text.")
        cases.append(
            Case(
                name=str(entry.get("name") or f"case-{index}"),
                filename=str(entry.get("filename") or "document.pdf"),
                text=text,
                expect_doc_type=(
                    str(entry["expect_doc_type"]) if entry.get("expect_doc_type") else None
                ),
                expect=tuple(str(item) for item in entry.get("expect", ())),
                forbid=tuple(str(item) for item in entry.get("forbid", ())),
            )
        )
    return cases


def _insert_document(conn: sqlite3.Connection, class_id: int, filename: str) -> int:
    """A document row for one case. Nothing is written to disk; extraction takes text."""
    cursor = conn.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, ?, ?, 'application/pdf', 0, 'ready')",
        (class_id, filename, f"{class_id}/{filename}"),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def _fresh_class(conn: sqlite3.Connection, case: Case) -> int:
    """One class per case, so no case can inherit another's facts.

    Extraction merges into a class profile by design, which is right for the product and
    wrong for a measurement: two cases sharing a class would have the second one's
    duplicates silently absorbed, and its recall would be scored against facts the first
    one found.
    """
    cursor = conn.execute(
        "insert into classes (name, code, semester) values (?, 'ECE 301', 'Spring 2026')",
        (case.name,),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def _subject(fact: sqlite3.Row) -> str:
    """A fact as one searchable string, so a label and a value are both matched."""
    label = str(fact["label"] or "").strip()
    value = str(fact["value"] or "").strip()
    return f"{label}: {value}" if label and value else label or value


def run_case(conn: sqlite3.Connection, case: Case) -> CaseResult:
    """Extract one case through the real product path and score what came back."""
    doc_type = detect_doc_type(case.filename, case.text)
    result = CaseResult(
        name=case.name,
        filename=case.filename,
        doc_type=doc_type,
        doc_type_ok=case.expect_doc_type is None or case.expect_doc_type == doc_type,
    )

    class_id = _fresh_class(conn, case)
    document_id = _insert_document(conn, class_id, case.filename)

    started = time.monotonic()
    try:
        skipped = profiles.extract_facts(conn, document_id, case.text, doc_type)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.seconds = time.monotonic() - started
        return result
    result.seconds = time.monotonic() - started
    if skipped is not None:
        result.error = f"extraction skipped: {skipped}"
        return result

    rows = list(
        conn.execute(
            "select kind, label, value, confidence from profile_facts where class_id = ?",
            (class_id,),
        )
    )
    result.facts = [
        {
            "kind": str(row["kind"]),
            "subject": _subject(row),
            "confidence": str(row["confidence"]),
        }
        for row in rows
    ]
    result.verified = sum(1 for row in rows if str(row["confidence"]) == "high")

    haystack = " | ".join(fact["subject"] for fact in result.facts).casefold()
    for wanted in case.expect:
        (result.found if wanted.casefold() in haystack else result.missed).append(wanted)
    for pattern in case.forbid:
        hits = [
            fact["subject"]
            for fact in result.facts
            if re.search(pattern, fact["subject"], re.IGNORECASE)
        ]
        result.contaminants.extend(hits)
    return result


def _summary(results: list[CaseResult]) -> dict[str, object]:
    """Aggregate the per-case numbers into the four a run is judged on."""
    scored = [result for result in results if not result.error]
    recalls = [result.recall for result in scored if result.recall is not None]
    rates = [result.verified_rate for result in scored if result.verified_rate is not None]
    facts = sum(len(result.facts) for result in scored)
    return {
        "cases": len(results),
        "errors": sum(1 for result in results if result.error),
        "doc_type_correct": sum(1 for result in results if result.doc_type_ok),
        "facts_total": facts,
        "facts_per_document": round(facts / len(scored), 2) if scored else 0.0,
        "recall": _rounded(sum(recalls) / len(recalls)) if recalls else None,
        "verified_rate": _rounded(sum(rates) / len(rates)) if rates else None,
        "contamination": sum(len(result.contaminants) for result in results),
        "seconds": round(sum(result.seconds for result in results), 1),
    }


def command_run(args: argparse.Namespace) -> None:
    """Extract every case in the corpus and write the report."""
    workspace = Workspace(args.workspace)
    cases = load_corpus(args.corpus)
    conn = prepare(workspace, args.source_db, args.model)
    try:
        results = []
        for index, case in enumerate(cases, start=1):
            logger.info("[%s/%s] %s (%s)", index, len(cases), case.name, case.filename)
            result = run_case(conn, case)
            if result.error:
                logger.warning("  %s", result.error)
            else:
                logger.info(
                    "  %s facts, %s verified, recall %s, contamination %s",
                    len(result.facts),
                    result.verified,
                    _rounded(result.recall),
                    len(result.contaminants),
                )
            results.append(result)
    finally:
        conn.close()

    payload = {
        "label": args.label,
        "model": args.model,
        "corpus": str(args.corpus),
        "summary": _summary(results),
        "cases": [result.as_payload() for result in results],
    }
    workspace.write(args.label, payload)
    _print_summary(args.label, payload["summary"])


def command_report(args: argparse.Namespace) -> None:
    """Print one run's summary and every case that lost a point."""
    workspace = Workspace(args.workspace)
    payload = workspace.read(args.label)
    _print_summary(args.label, payload.get("summary", {}))

    print("\nCases needing attention:")
    clean = True
    for case in payload.get("cases", []):
        problems = []
        if case.get("error"):
            problems.append(case["error"])
        if not case.get("doc_type_ok"):
            problems.append(f"classified as {case['doc_type']}")
        if case.get("contaminants"):
            problems.append(f"recorded {len(case['contaminants'])}: {case['contaminants']}")
        if case.get("missed"):
            problems.append(f"missed {case['missed']}")
        if problems:
            clean = False
            print(f"  {case['name']}")
            for problem in problems:
                print(f"    - {problem}")
    if clean:
        print("  none")


def command_compare(args: argparse.Namespace) -> None:
    """Put several runs beside each other, which is the only way to read these numbers.

    A verified rate of 0.7 means nothing on its own. The same corpus scoring 0.9 on a large
    model and 0.7 on a small one means the prompt is leaning on capability the target
    deployment does not have, which is the finding this whole harness exists to surface.
    """
    workspace = Workspace(args.workspace)
    runs = [(label, workspace.read(label).get("summary", {})) for label in args.labels]

    columns = [
        ("facts/doc", "facts_per_document"),
        ("recall", "recall"),
        ("verified", "verified_rate"),
        ("contam.", "contamination"),
        ("doctype", "doc_type_correct"),
        ("errors", "errors"),
    ]
    width = max(len(label) for label, _ in runs) + 2
    print("run".ljust(width) + "".join(name.rjust(11) for name, _ in columns))
    for label, summary in runs:
        cells = "".join(str(summary.get(key, "-")).rjust(11) for _, key in columns)
        print(label.ljust(width) + cells)


def _print_summary(label: str, summary: object) -> None:
    if not isinstance(summary, dict):
        return
    print(f"\n{label}:")
    for key, value in summary.items():
        print(f"  {key}: {value}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="extract every case and score it")
    run.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    run.add_argument("--source-db", type=Path, default=ROOT / "data" / "lyra.db")
    run.add_argument("--model", default=None, help="override the configured model")
    run.add_argument("--label", default="latest", help="names the report this run writes")
    run.set_defaults(func=command_run)

    report = subparsers.add_parser("report", help="print one run")
    report.add_argument("--label", default="latest")
    report.set_defaults(func=command_report)

    compare = subparsers.add_parser("compare", help="put several runs beside each other")
    compare.add_argument("labels", nargs="+")
    compare.set_defaults(func=command_compare)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
