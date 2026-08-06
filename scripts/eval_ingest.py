"""Evaluation of ingestion and retrieval against real documents at real size.

The backend test suite defends contracts against short fixtures. It cannot tell you what a
608-page textbook costs to ingest, whether the chunker understood how the book is
organized, or whether retrieval finds the one section that answers a question. Those are
the questions Phase 3 exists to answer, and this is what answers them.

It drives the real code path, in process: `core.ingestion` and `rag.retrieve`, against the
configured embedding server. Nothing here reimplements what the product does. The one thing
it reads on its own is the PDF outline, and that is deliberate: the outline is ground truth
about the file that the product does not read yet, so the harness has to know it in order
to say whether the product's reading of the same file was right.

**It never touches the student's own data.** Every run works in its own workspace directory
with its own database, set before anything opens a connection. The settings row is copied
from the real database so the embedding model and context window match what the app would
use.

Usage:

    python scripts/eval_ingest.py ingest --fresh /path/to/textbook.pdf
    python scripts/eval_ingest.py retrieve
    python scripts/eval_ingest.py retrieve --questions scripts/eval_questions/another-book.json
    python scripts/eval_ingest.py report

Each stage writes JSON into the workspace, so a later stage reads what an earlier one
produced rather than redoing it.
"""

import argparse
import asyncio
import json
import logging
import re
import shutil
import sqlite3
import statistics
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.api.routes_documents import MIME_BY_SUFFIX  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.core import ingestion, recognition  # noqa: E402
from backend.core.app_settings import resolve_tutor_config  # noqa: E402
from backend.llm import client  # noqa: E402
from backend.rag import render, transcribe  # noqa: E402
from backend.rag import retrieve as retrieval  # noqa: E402
from backend.storage.database import connect, migrate  # noqa: E402

logger = logging.getLogger("lyra.eval.ingest")

DEFAULT_WORKSPACE = ROOT / "data" / "eval-ingest"
DEFAULT_SOURCE_DB = ROOT / "data" / "lyra.db"
DEFAULT_QUESTIONS = ROOT / "scripts" / "eval_questions" / "kuttler-linear-algebra.json"

PDF_MIME = "application/pdf"

# The ingestion stages worth attributing time to, in the order they run. `store` is the
# database write and is derived rather than timed: it is whatever embedding was not.
TIMED_STAGES = ("parse", "chunk", "embed", "extract", "consolidate")

# Retrieval is run wider than the product's `k` so that the rank of the right chunk can be
# read off directly. Hit rate at any smaller k is then arithmetic rather than another run,
# which is what makes this able to say whether k = 8 is the right number.
WIDE_K = 32
REPORTED_K = (1, 4, 8, 16, 32)

# Large enough that `_fit_to_budget` never drops anything. Rank is the measurement here,
# and a budget trim would silently truncate the ranking being measured.
UNLIMITED_BUDGET = 10_000_000

# How many neighbours to keep in the record for reading by eye afterwards.
_KEPT_NEIGHBOURS = 5

_PATH_SEPARATOR = " / "


@dataclass(frozen=True)
class Workspace:
    """Where one evaluation run keeps its database, its uploads, and its reports."""

    root: Path

    @property
    def db_path(self) -> Path:
        return self.root / "lyra.db"

    def report(self, name: str) -> Path:
        return self.root / f"{name}.json"

    def read(self, name: str) -> dict[str, object]:
        path = self.report(name)
        if not path.exists():
            raise SystemExit(f"{path.name} is missing. Run the earlier stage first.")
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, name: str, payload: dict[str, object]) -> None:
        self.report(name).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def existing(self, name: str) -> dict[str, object]:
        path = self.report(name)
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}


@dataclass
class StageClock:
    """Wall-clock seconds spent inside each ingestion stage of one document."""

    totals: dict[str, float] = field(default_factory=dict)

    def add(self, stage: str, seconds: float) -> None:
        self.totals[stage] = self.totals.get(stage, 0.0) + seconds

    def rounded(self) -> dict[str, float]:
        return {stage: round(self.totals.get(stage, 0.0), 2) for stage in TIMED_STAGES}


def prepare(workspace: Workspace, source_db: Path) -> sqlite3.Connection:
    """Point the whole backend at the workspace, then open and migrate its database.

    The settings object is mutated rather than re-read from the environment because every
    module already holds a reference to it. Done before the first `connect`, so nothing
    can have opened the real database by the time this returns.
    """
    settings.data_dir = workspace.root
    settings.db_path = workspace.db_path
    workspace.root.mkdir(parents=True, exist_ok=True)
    _link_models(workspace)
    settings.ensure_directories()

    conn = connect()
    migrate(conn)
    _copy_settings(conn, source_db)
    return conn


def _link_models(workspace: Workspace) -> None:
    """Share the real installation's model directory rather than downloading a second one.

    Everything else about the workspace is its own, but the embedding binary and its
    weights are hundreds of megabytes and are identical by construction. Without this the
    workspace has no embedding server, every document fails at the embedding stage, and the
    harness reports a fault it created itself.
    """
    link = workspace.root / "models"
    real = ROOT / "data" / "models"
    if link.exists() or not real.is_dir():
        return
    link.symlink_to(real, target_is_directory=True)


def _copy_settings(conn: sqlite3.Connection, source_db: Path) -> None:
    """Copy the endpoint configuration from the real database into the workspace.

    Unlike the solver harness, profile extraction is left as configured rather than forced
    off. What `extract_facts` costs on a 608-page document is one of the things this script
    exists to measure, and it is the only ingestion stage whose cost does not scale with
    chunk count. `ingest --no-extraction` turns it off when the run is about the other
    stages.
    """
    if not source_db.exists():
        raise SystemExit(f"No database at {source_db}. Configure Lyra first, or pass --source-db.")

    source = sqlite3.connect(source_db)
    source.row_factory = sqlite3.Row
    row = source.execute("select * from settings where id = 1").fetchone()
    source.close()
    if row is None:
        raise SystemExit("The source database has no settings row.")

    conn.execute(
        "update settings set endpoint_url = ?, model = ?, context_window = ?, "
        "remote_ack = ?, embedding_model = ?, embedding_dim = ? where id = 1",
        (
            row["endpoint_url"],
            row["model"],
            row["context_window"],
            row["remote_ack"],
            row["embedding_model"],
            row["embedding_dim"],
        ),
    )
    conn.commit()


def _class_id(conn: sqlite3.Connection, name: str) -> int:
    """The evaluation class, created once and reused."""
    row = conn.execute("select id from classes where name = ?", (name,)).fetchone()
    if row is not None:
        return int(row["id"])
    cursor = conn.execute("insert into classes (name) values (?)", (name,))
    conn.commit()
    return int(cursor.lastrowid or 0)


@contextmanager
def _timed_stages(clock: StageClock) -> Iterator[None]:
    """Time each ingestion stage by wrapping the functions the job already calls.

    Instrumentation rather than reimplementation: every wrapper calls straight through, so
    the run being measured is the run the product performs. `ingestion` imports these names
    into its own namespace, which is why they are patched there rather than at their source
    module.
    """
    wrapped = {
        "parse": "parse_document",
        "chunk": "chunk_document",
        "embed": "embed_documents",
        "extract": "extract_facts",
        "consolidate": "consolidate_class",
    }
    originals = {stage: getattr(ingestion, name) for stage, name in wrapped.items()}

    def timer(stage: str, inner: Callable[..., object]) -> Callable[..., object]:
        def call(*args: object, **kwargs: object) -> object:
            started = time.monotonic()
            try:
                return inner(*args, **kwargs)
            finally:
                clock.add(stage, time.monotonic() - started)

        return call

    try:
        for stage, name in wrapped.items():
            setattr(ingestion, name, timer(stage, originals[stage]))
        yield
    finally:
        for stage, name in wrapped.items():
            setattr(ingestion, name, originals[stage])


def read_outline(path: Path) -> dict[str, object]:
    """The PDF's own outline, as ground truth about how the book is organized.

    This is the one thing the harness reads that the product does not. The product gains
    `get_toc()` in Phase 3, and until it does there is no other way to say whether its
    reading of a book's structure was right, because there would be nothing to be right
    about.

    Returns:
        `entries` as `(depth, title, page)` triples, plus the count and the maximum depth.
        An outline is absent on plenty of legitimate documents, so an empty one is a fact
        rather than a failure.
    """
    try:
        with pymupdf.open(path) as document:
            toc = document.get_toc()
            page_count = document.page_count
    except Exception as exc:  # noqa: BLE001 - a bad file is a datum, not a crash
        logger.warning("Could not read the outline of %s: %s", path.name, exc)
        return {"entries": [], "entry_count": 0, "max_depth": 0, "page_count": 0}

    entries = [[int(depth), str(title), int(page)] for depth, title, page in toc]
    return {
        "entries": entries,
        "entry_count": len(entries),
        "max_depth": max((entry[0] for entry in entries), default=0),
        "page_count": page_count,
    }


def section_ranges(outline: dict[str, object]) -> dict[str, tuple[int, int]]:
    """Every outline entry as a path, mapped to the pages it covers.

    A section runs from its own first page to the first page of the next entry at the same
    depth or shallower, inclusive at both ends. The end is inclusive because an outline
    destination lands where the heading is, which is often partway down a page that the
    previous section is still using: in the reference book the LU Factorization entry
    points at page 110 and its heading is on 111. Treating the boundary page as belonging
    to both is the honest reading and costs one page of precision.

    Returns:
        Paths of the form `Chapter / Section`, joined parent first, to `(first, last)`.
    """
    entries = [entry for entry in outline.get("entries", []) if isinstance(entry, list)]
    page_count = int(outline.get("page_count") or 0)
    ranges: dict[str, tuple[int, int]] = {}
    ancestry: list[str] = []

    for index, (depth, title, page) in enumerate(entries):
        del ancestry[depth - 1 :]
        ancestry.append(str(title).strip())
        path = _PATH_SEPARATOR.join(ancestry)

        end = page_count
        for later_depth, _, later_page in entries[index + 1 :]:
            if later_depth <= depth:
                end = later_page
                break
        ranges[path] = (int(page), int(end))

    return ranges


def cmd_ingest(args: argparse.Namespace) -> int:
    """Ingest documents into the workspace exactly as an upload would, and time it."""
    workspace = Workspace(Path(args.workspace).resolve())
    if args.fresh and workspace.root.exists():
        shutil.rmtree(workspace.root)
    conn = prepare(workspace, Path(args.source_db))
    if args.no_extraction:
        conn.execute("update settings set extraction_enabled = 0 where id = 1")
        conn.commit()
    class_id = _class_id(conn, args.class_name)

    documents = _mapping(workspace.existing("ingestion").get("documents", {}))
    for path in [Path(one).resolve() for one in args.documents]:
        if not path.is_file():
            raise SystemExit(f"Not a file: {path}")
        record = _ingest_one(conn, class_id, path)
        documents[path.stem] = record
        # Written after every document, so an interrupted run keeps what finished.
        workspace.write("ingestion", {"class_id": class_id, "documents": documents})
        print(
            f"{path.name}: {record['state']} in {record['seconds']}s, "
            f"{record['chunk_count']} chunks, doc_type {record['doc_type']}, "
            f"{record['chunks_with_section']} with a section title"
        )

    conn.close()
    return 0


def _ingest_one(conn: sqlite3.Connection, class_id: int, path: Path) -> dict[str, object]:
    """Upload, ingest, and measure one document."""
    document_id = _upload(conn, class_id, path)
    clock = StageClock()
    started = time.monotonic()
    with _timed_stages(clock):
        ingestion.run_ingestion(document_id)
    elapsed = time.monotonic() - started

    row = conn.execute(
        "select state, error_message, stage_detail, pages_total, pages_done, pages_skipped "
        "from documents where id = ?",
        (document_id,),
    ).fetchone()

    outline = read_outline(path) if path.suffix.lower() == ".pdf" else {}
    return {
        "document_id": document_id,
        "filename": path.name,
        "megabytes": round(path.stat().st_size / 1e6, 1),
        "state": row["state"],
        "error": row["error_message"],
        # From the chunks rather than from `documents.stage_detail`. Ingestion parks the
        # detected type there while embedding, and then `_mark_ready` overwrites it with
        # the profile-extraction outcome, so by the time a run finishes the column has
        # nothing to do with chunking.
        "doc_type": _detected_type(conn, document_id),
        "pages_total": row["pages_total"],
        "pages_done": row["pages_done"],
        "pages_skipped": row["pages_skipped"],
        "seconds": round(elapsed, 1),
        "stage_seconds": clock.rounded(),
        "outline": {
            "entry_count": outline.get("entry_count", 0),
            "max_depth": outline.get("max_depth", 0),
        },
        **_chunk_stats(conn, document_id),
    }


def _upload(conn: sqlite3.Connection, class_id: int, path: Path) -> int:
    """Store one file the way the upload route does, and return its document id."""
    payload = path.read_bytes()
    # The route's own map rather than a second copy of it, so an image the product accepts
    # is an image the harness accepts, and neither can drift from the other.
    mime = MIME_BY_SUFFIX.get(path.suffix.lower(), PDF_MIME)
    document_id = int(
        conn.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
            "values (?, ?, '', ?, ?, ?)",
            (class_id, path.name, mime, len(payload), ingestion.PENDING),
        ).lastrowid
        or 0
    )
    stored = settings.uploads_dir / str(class_id) / f"{document_id}-{path.name}"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(payload)
    conn.execute("update documents set stored_path = ? where id = ?", (str(stored), document_id))
    conn.commit()
    return document_id


def _detected_type(conn: sqlite3.Connection, document_id: int) -> str | None:
    """The document type the chunker actually used, as recorded on its chunks."""
    row = conn.execute(
        "select doc_type from chunks where document_id = ? limit 1", (document_id,)
    ).fetchone()
    return None if row is None else str(row["doc_type"])


def _chunk_stats(conn: sqlite3.Connection, document_id: int) -> dict[str, object]:
    """What the chunker produced, in the terms Phase 3 is trying to improve.

    `distinct_sections` is reported beside `chunks_with_section` because coverage on its
    own is not the measurement. A heading regex over flattened text labels almost every
    chunk and labels most of them wrongly, so a high coverage with an absurd number of
    distinct values is the signature of the fault rather than evidence against it.
    """
    rows = conn.execute(
        "select content, token_count, page_number, section_title, problem_number "
        "from chunks where document_id = ? order by id",
        (document_id,),
    ).fetchall()
    if not rows:
        return {
            "chunk_count": 0,
            "tokens": {},
            "chunks_with_section": 0,
            "distinct_sections": 0,
            "sample_sections": [],
            "chunks_with_problem_number": 0,
        }

    tokens = [int(row["token_count"]) for row in rows]
    titles = [row["section_title"] for row in rows if row["section_title"]]
    distinct: list[str] = []
    for title in titles:
        if title not in distinct:
            distinct.append(str(title))

    return {
        "chunk_count": len(rows),
        "tokens": {
            "total": sum(tokens),
            "mean": round(statistics.mean(tokens)),
            "median": round(statistics.median(tokens)),
            "min": min(tokens),
            "max": max(tokens),
        },
        "chunks_with_section": len(titles),
        "distinct_sections": len(distinct),
        "sample_sections": distinct[:12],
        "chunks_with_problem_number": sum(1 for row in rows if row["problem_number"]),
    }


@contextmanager
def _widened_k(k: int) -> Iterator[None]:
    """Run retrieval with a larger neighbour count than the product uses.

    Both the constant and the SQL are replaced, because sqlite-vec reads the KNN limit out
    of the query text rather than from a bound parameter, which is why `_KNN_SQL` inlines
    it in the first place.
    """
    original_k, original_sql = retrieval.K, retrieval._KNN_SQL
    retrieval.K = k
    retrieval._KNN_SQL = original_sql.replace(f"k = {original_k}", f"k = {k}")
    try:
        yield
    finally:
        retrieval.K, retrieval._KNN_SQL = original_k, original_sql


def cmd_retrieve(args: argparse.Namespace) -> int:
    """Ask the question set and record where the right chunk landed in the ranking."""
    workspace = Workspace(Path(args.workspace).resolve())
    conn = prepare(workspace, Path(args.source_db))
    ingested = workspace.read("ingestion")
    documents = _mapping(ingested.get("documents", {}))

    question_set = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    stem = str(question_set["document"])
    if stem not in documents:
        raise SystemExit(f"{stem} has not been ingested. Run the ingest stage on it first.")

    document = documents[stem]
    document_id = int(document["document_id"])
    stored = conn.execute(
        "select stored_path from documents where id = ?", (document_id,)
    ).fetchone()
    ranges = section_ranges(read_outline(Path(str(stored["stored_path"]))))

    results: list[dict[str, object]] = []
    with _widened_k(args.k):
        for question in question_set["questions"]:
            record = _ask(conn, int(ingested["class_id"]), document_id, question, ranges, args.k)
            results.append(record)
            print(
                f"{record['id']}: rank {record['rank'] if record['rank'] else '-'}, "
                f"top similarity {record['top_similarity']}"
            )

    workspace.write(
        "retrieval",
        {"document": stem, "k": args.k, "questions": results},
    )
    conn.close()
    return 0


def _ask(
    conn: sqlite3.Connection,
    class_id: int,
    document_id: int,
    question: dict[str, object],
    ranges: dict[str, tuple[int, int]],
    k: int,
) -> dict[str, object]:
    """Run one question and locate the expected section in the ranking."""
    expected = question.get("expect_section")
    pages: tuple[int, int] | None = None
    if expected is not None:
        pages = ranges.get(str(expected))
        if pages is None:
            raise SystemExit(
                f"{question['id']}: no outline entry at path {expected!r}. "
                "The question set and the book disagree."
            )

    result = retrieval.retrieve(conn, class_id, str(question["question"]), UNLIMITED_BUDGET)
    chunks = result.chunks[:k]

    rank: int | None = None
    if pages is not None:
        for position, chunk in enumerate(chunks, start=1):
            page = chunk.page_number
            if chunk.document_id != document_id or page is None:
                continue
            if pages[0] <= page <= pages[1]:
                rank = position
                break

    return {
        "id": question["id"],
        "question": question["question"],
        "expect_section": expected,
        "expect_pages": list(pages) if pages else None,
        "rank": rank,
        "returned": len(chunks),
        "top_similarity": round(chunks[0].similarity, 4) if chunks else None,
        "neighbours": [
            {
                "page": chunk.page_number,
                "similarity": round(chunk.similarity, 4),
                "section_title": chunk.section_title,
                "opening": " ".join(chunk.content.split())[:120],
            }
            for chunk in chunks[:_KEPT_NEIGHBOURS]
        ],
    }


def _mapping(value: object) -> dict[str, dict[str, object]]:
    """A JSON object read back from a report, or an empty one when it is not one."""
    return value if isinstance(value, dict) else {}


# Pages of the reference book chosen because the text layer is known to fail on them: each
# carries matrices, vectors, or a determinant written out in a grid. `13` is the control,
# a page of ordinary prose the text layer handles perfectly well, so a transcription that
# looked better everywhere would be suspected of flattering itself.
DEFAULT_TRANSCRIBE_PAGES = (13, 90, 111, 158, 194, 245)

# A line that is nothing but a number, which is what a matrix collapses into when the text
# layer flattens it. Counting these is the objective half of the comparison: the failure
# has a shape, and the shape is countable.
_LONE_NUMBER = re.compile(r"^\s*[-+−]?\s*\d+(?:\.\d+)?\s*$")

# Markup that says a grid survived as a grid.
_MATRIX_MARKUP = re.compile(r"\\begin\{(?:[bBpvV]?matrix|array)\}")


def _text_shape(text: str) -> dict[str, object]:
    """The countable signals of whether mathematics survived a reading of a page."""
    lines = [line for line in text.splitlines() if line.strip()]
    return {
        "characters": len(text),
        "lines": len(lines),
        "lone_number_lines": sum(1 for line in lines if _LONE_NUMBER.match(line)),
        "matrix_markup": len(_MATRIX_MARKUP.findall(text)),
        "math_delimiters": text.count("$"),
    }


def cmd_transcribe(args: argparse.Namespace) -> int:
    """Read chosen pages with the vision model and compare against the text layer.

    The question this answers is not "can it read a scan". It is whether a vision pass
    beats PyMuPDF on pages that already have a perfectly good text layer, which is what
    decides whether transcription is a scanned-document feature or a quality feature for
    every document.
    """
    workspace = Workspace(Path(args.workspace).resolve())
    conn = prepare(workspace, Path(args.source_db))
    ingested = workspace.read("ingestion")
    documents = _mapping(ingested.get("documents", {}))
    if not documents:
        raise SystemExit("Nothing ingested. Run the ingest stage first.")

    name = args.document or next(iter(documents))
    document = documents[name]
    document_id = int(document["document_id"])
    row = conn.execute(
        "select stored_path, mime from documents where id = ?", (document_id,)
    ).fetchone()
    source = Path(str(row["stored_path"]))

    config = resolve_tutor_config(conn)
    support = asyncio.run(
        client.probe_vision_support(config.endpoint_url, config.api_key, config.model)
    )
    print(f"vision probe: {'ok' if support.ok else 'unavailable'} - {support.message}\n")
    if not support.ok:
        raise SystemExit("The configured endpoint cannot read images, so there is nothing to time.")

    with pymupdf.open(source) as book:
        layers = {number: book[number - 1].get_text() for number in args.pages}

    results: list[dict[str, object]] = []
    for number in args.pages:
        image = render.render_page(
            document_id, source, str(row["mime"]), number, render.RECOGNITION_DPI
        )
        started = time.monotonic()
        try:
            read = asyncio.run(
                transcribe.transcribe_page(
                    config.endpoint_url,
                    config.api_key,
                    config.model,
                    image.read_bytes(),
                    remote_ack=True,
                )
            )
            error = None
        except Exception as exc:  # noqa: BLE001 - a failed page is a datum, not a crash
            read, error = "", str(exc)[:200]
        elapsed = round(time.monotonic() - started, 1)

        layer = layers[number]
        results.append(
            {
                "page": number,
                "seconds": elapsed,
                "error": error,
                "text_layer": layer,
                "transcription": read,
                "text_layer_shape": _text_shape(layer),
                "transcription_shape": _text_shape(read),
            }
        )
        shape = results[-1]["transcription_shape"]
        before = results[-1]["text_layer_shape"]
        print(
            f"page {number}: {elapsed}s, lone-number lines "
            f"{before['lone_number_lines']} -> {shape['lone_number_lines']}, "
            f"matrices {shape['matrix_markup']}" + (f", FAILED {error}" if error else "")
        )
        workspace.write("transcription", {"document": name, "pages": results})

    _write_comparison(workspace, name, results)
    print(f"\nBoth readings of every page: {workspace.root / 'transcription.md'}")
    conn.close()
    return 0


def _write_comparison(workspace: Workspace, name: str, results: list[dict[str, object]]) -> None:
    """Write both readings of every page out to be read by a person.

    The counts below are evidence and not a verdict. Whether a transcription is *right* is
    a question about mathematics that no automated signal here answers, and the standard
    this project holds itself to on that is the one Phase 2 used: read it.
    """
    lines = [f"# Two readings of {name}", ""]
    for result in results:
        lines += [
            f"## Page {result['page']}",
            "",
            f"Transcribed in {result['seconds']}s. "
            f"Text layer: {result['text_layer_shape']}. "
            f"Transcription: {result['transcription_shape']}.",
            "",
            "### PyMuPDF text layer",
            "",
            "```",
            str(result["text_layer"]).strip(),
            "```",
            "",
            "### Vision transcription",
            "",
            "```",
            str(result["transcription"]).strip() or "(nothing returned)",
            "```",
            "",
        ]
    (workspace.root / "transcription.md").write_text("\n".join(lines), encoding="utf-8")


@contextmanager
def _timed_pages(records: list[dict[str, object]]) -> Iterator[None]:
    """Time each page of a recognition run, from inside the run the product performs.

    Wrapped at `_read_one_page` rather than at `transcribe_page` because that is the layer
    that still knows which page it is holding: the transcription interface takes an image
    and nothing else, deliberately, so the page number is not recoverable below it.
    """
    original = recognition._read_one_page  # noqa: SLF001

    def call(
        config: object,
        remote_ack: bool,
        document_id: int,
        source: Path,
        mime: str,
        page_number: int,
    ) -> str:
        started = time.monotonic()
        try:
            text = original(config, remote_ack, document_id, source, mime, page_number)
        except Exception as exc:
            records.append(
                {
                    "page": page_number,
                    "seconds": round(time.monotonic() - started, 1),
                    "error": str(exc)[:200],
                    "text": "",
                    "shape": _text_shape(""),
                }
            )
            raise
        records.append(
            {
                "page": page_number,
                "seconds": round(time.monotonic() - started, 1),
                "error": None,
                "text": text,
                "shape": _text_shape(text),
            }
        )
        return text

    try:
        recognition._read_one_page = call  # type: ignore[assignment]  # noqa: SLF001
        yield
    finally:
        recognition._read_one_page = original  # type: ignore[assignment]  # noqa: SLF001


def cmd_recognize(args: argparse.Namespace) -> int:
    """Ingest a scanned document with recognition on, and time it page by page.

    This is the end-to-end run rather than the interface in isolation, because what is
    being measured is a document becoming searchable rather than a page becoming text. What
    it answers is whether the general vision path is fast enough to be the shipped default
    for a document a student would actually upload, and what the same rate would cost on a
    book.
    """
    workspace = Workspace(Path(args.workspace).resolve())
    conn = prepare(workspace, Path(args.source_db))
    if args.no_extraction:
        conn.execute("update settings set extraction_enabled = 0 where id = 1")
        conn.commit()
    class_id = _class_id(conn, args.class_name)

    path = Path(args.document).resolve()
    if not path.is_file():
        raise SystemExit(f"Not a file: {path}")

    config = resolve_tutor_config(conn)
    support = asyncio.run(
        client.probe_vision_support(config.endpoint_url, config.api_key, config.model)
    )
    print(f"vision probe: {'ok' if support.ok else 'unavailable'} - {support.message}\n")
    if not support.ok:
        raise SystemExit("The configured endpoint cannot read images, so there is nothing to time.")

    document_id = _upload(conn, class_id, path)
    # The one thing the student's explicit action does, done here explicitly for the same
    # reason: nothing transcribes without being asked.
    conn.execute("update documents set recognize = 1 where id = ?", (document_id,))
    conn.commit()

    pages: list[dict[str, object]] = []
    clock = StageClock()
    started = time.monotonic()
    with _timed_pages(pages), _timed_stages(clock):
        ingestion.run_ingestion(document_id)
    elapsed = time.monotonic() - started

    row = conn.execute(
        "select state, error_message, pages_total, pages_done, pages_skipped "
        "from documents where id = ?",
        (document_id,),
    ).fetchone()
    states = {
        str(state): int(count)
        for state, count in conn.execute(
            "select state, count(*) from document_pages where document_id = ? group by state",
            (document_id,),
        )
    }
    read = [entry for entry in pages if entry["error"] is None]
    seconds = [float(entry["seconds"]) for entry in read]

    record = {
        "document_id": document_id,
        "filename": path.name,
        "state": row["state"],
        "error": row["error_message"],
        "pages_total": row["pages_total"],
        "pages_done": row["pages_done"],
        "pages_skipped": row["pages_skipped"],
        "page_states": states,
        "seconds": round(elapsed, 1),
        "stage_seconds": clock.rounded(),
        "seconds_per_page": round(statistics.mean(seconds), 1) if seconds else None,
        "median_seconds_per_page": round(statistics.median(seconds), 1) if seconds else None,
        "pages": pages,
        **_chunk_stats(conn, document_id),
    }
    workspace.write("recognition", record)
    _write_recognition(workspace, path.stem, record)

    for entry in pages:
        note = f" FAILED {entry['error']}" if entry["error"] else ""
        shape = entry["shape"]
        print(f"page {entry['page']}: {entry['seconds']}s, {shape['characters']} chars{note}")
    print(
        f"\n{path.name}: {row['state']} in {record['seconds']}s, "
        f"{record['chunk_count']} chunks, {states}"
    )
    rate, median = record["seconds_per_page"], record["median_seconds_per_page"]
    if rate is not None:
        print(f"{rate}s a page (median {median}s)")
    print(f"\nWhat every page was read as: {workspace.root / 'recognition.md'}")
    conn.close()
    return 0


def _write_recognition(workspace: Workspace, name: str, record: dict[str, object]) -> None:
    """Write every recognized page out to be read by a person.

    Same standard as the transcription comparison: the counts are evidence and not a
    verdict, and whether a page was read correctly is a question only reading it answers.
    """
    lines = [f"# {name} as recognition read it", ""]
    for entry in record["pages"]:  # type: ignore[union-attr]
        lines += [
            f"## Page {entry['page']}",
            "",
            f"Read in {entry['seconds']}s. {entry['shape']}."
            + (f" FAILED: {entry['error']}" if entry["error"] else ""),
            "",
            "```",
            str(entry["text"]).strip() or "(nothing returned)",
            "```",
            "",
        ]
    (workspace.root / "recognition.md").write_text("\n".join(lines), encoding="utf-8")


def cmd_report(args: argparse.Namespace) -> int:
    """Print what the recorded runs say, which is the only output anyone reads twice."""
    workspace = Workspace(Path(args.workspace).resolve())
    ingested = _mapping(workspace.existing("ingestion").get("documents", {}))
    retrieved = workspace.existing("retrieval")

    if ingested:
        _report_ingestion(ingested)
    if retrieved:
        _report_retrieval(retrieved)
    if not ingested and not retrieved:
        print("Nothing recorded yet. Run the ingest stage first.")
    return 0


def _report_ingestion(documents: dict[str, dict[str, object]]) -> None:
    print("Ingestion")
    for name, record in sorted(documents.items()):
        outline = _mapping(record.get("outline"))
        tokens = _mapping(record.get("tokens"))
        print(f"\n  {name}")
        print(
            f"    {record['state']}, {record['pages_total']} pages, "
            f"{record['megabytes']} MB, doc_type {record['doc_type']}"
        )
        print(
            f"    outline: {outline.get('entry_count', 0)} entries, "
            f"depth {outline.get('max_depth', 0)}"
        )
        print(
            f"    chunks: {record['chunk_count']}, "
            f"mean {tokens.get('mean', 0)} tokens, max {tokens.get('max', 0)}"
        )
        print(
            f"    section titles: {record['chunks_with_section']} chunks, "
            f"{record['distinct_sections']} distinct"
        )
        stages = _mapping(record.get("stage_seconds"))
        timed = ", ".join(f"{stage} {stages.get(stage, 0)}s" for stage in TIMED_STAGES)
        print(f"    {record['seconds']}s total: {timed}")


def _report_retrieval(retrieved: dict[str, object]) -> None:
    questions = [one for one in retrieved.get("questions", []) if isinstance(one, dict)]
    targeted = [one for one in questions if one["expect_section"] is not None]
    controls = [one for one in questions if one["expect_section"] is None]
    if not questions:
        return

    print(f"\nRetrieval over {retrieved.get('document')}, k = {retrieved.get('k')}")
    ranks = [one["rank"] for one in targeted if one["rank"]]
    for k in REPORTED_K:
        if k > int(retrieved.get("k") or 0):
            continue
        hits = sum(1 for rank in ranks if rank <= k)
        print(f"  hit rate at k={k:>2}: {hits}/{len(targeted)}")
    if ranks:
        print(f"  median rank of a hit: {statistics.median(ranks)}")

    missed = [one["id"] for one in targeted if not one["rank"]]
    if missed:
        print(f"  never found: {', '.join(missed)}")

    if controls:
        best = [one["top_similarity"] for one in targeted if one["top_similarity"] is not None]
        print("\n  Controls, which should look worse than the questions above")
        for one in controls:
            print(f"    {one['id']}: top similarity {one['top_similarity']}")
        if best:
            print(f"    real questions, median top similarity: {round(statistics.median(best), 4)}")

    print("\n  Per question")
    for one in questions:
        target = one["expect_section"] or "not in this book"
        print(f"    {one['id']}: rank {one['rank'] or '-'} of {one['returned']}, {target}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    parser.add_argument("--source-db", default=str(DEFAULT_SOURCE_DB))
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="Ingest documents and time every stage")
    ingest.add_argument("documents", nargs="+", help="Paths to the files to ingest")
    ingest.add_argument("--class-name", default="Evaluation")
    ingest.add_argument("--fresh", action="store_true", help="Delete the workspace first")
    ingest.add_argument(
        "--no-extraction", action="store_true", help="Skip the profile extraction stage"
    )
    ingest.set_defaults(func=cmd_ingest)

    retrieve = subparsers.add_parser("retrieve", help="Ask the question set and rank the answers")
    retrieve.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    retrieve.add_argument("--k", type=int, default=WIDE_K)
    retrieve.set_defaults(func=cmd_retrieve)

    reading = subparsers.add_parser(
        "transcribe", help="Read pages with the vision model and compare against the text layer"
    )
    reading.add_argument("--document", help="Which ingested document, by stem")
    reading.add_argument("--pages", nargs="+", type=int, default=list(DEFAULT_TRANSCRIBE_PAGES))
    reading.set_defaults(func=cmd_transcribe)

    recognize = subparsers.add_parser(
        "recognize", help="Ingest a scanned document with recognition on, timed page by page"
    )
    recognize.add_argument("document", help="Path to the scanned file")
    recognize.add_argument("--class-name", default="Evaluation")
    recognize.add_argument(
        "--no-extraction", action="store_true", help="Skip the profile extraction stage"
    )
    recognize.set_defaults(func=cmd_recognize)

    report = subparsers.add_parser("report", help="Summarise the recorded runs")
    report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
