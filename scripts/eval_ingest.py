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

A question set is asked against a *class*, because that is what the product searches. Run
one against a workspace holding a single document and the answer has nothing to compete
with, which is why a set names the document each answer is in: a chunk from the right pages
of the wrong document is a miss, and the report says which documents did the crowding.

Usage:

    python scripts/eval_ingest.py ingest --fresh /path/to/textbook.pdf
    python scripts/eval_ingest.py --workspace data/eval-class ingest --fresh course/*.pdf
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
from backend.llm.locality import is_local_endpoint  # noqa: E402
from backend.rag import chunk as chunking  # noqa: E402
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
#
# The default width is the product's own rerank fetch width, `RERANK_FETCH_K` in
# `backend/rag/retrieve.py`, deliberately by reference rather than by a copy of the number:
# the recorded class-scale measurements were taken at that width, so a run that passes no
# `--k` reproduces them instead of silently measuring a narrower fetch.
WIDE_K = retrieval.RERANK_FETCH_K
REPORTED_K = (1, 4, 8, 16, 32, 64)

# Large enough that `_fit_to_budget` never drops anything. Rank is the measurement here,
# and a budget trim would silently truncate the ranking being measured.
UNLIMITED_BUDGET = 10_000_000

# How many neighbours to keep in the record for reading by eye afterwards.
_KEPT_NEIGHBOURS = 5

# The `k` the product actually serves. Cross-document crowding is reported at this width
# rather than at `WIDE_K`, because what a student loses is the chunk that did not make the
# eight, not the one that did not make the thirty-two.
PRODUCT_K = 8

_PATH_SEPARATOR = " / "


@dataclass(frozen=True)
class _RerankSwitch:
    """The rerank server with `available` answered by the harness rather than by the disk."""

    inner: object
    available: bool


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
        duplicate = _already_ingested(conn, class_id, path)
        if duplicate is not None:
            # `_upload` inserts a new row every time it is called, so ingesting the same
            # file into an existing workspace would put two copies of it in the class and
            # every later retrieval run would be measured against the inflated haystack.
            print(
                f"{path.name}: already in this workspace as document {duplicate}; skipped. "
                "Use --fresh to rebuild the workspace, or `reindex` to re-chunk it."
            )
            continue
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


def _already_ingested(conn: sqlite3.Connection, class_id: int, path: Path) -> int | None:
    """The id of a document in the class with this file's stem, or None.

    Matched by stem rather than by full filename because the stem is how everything else
    here names a document: the ingestion report keys on it and `_find_document` searches
    by it, so two files that would collide there are duplicates here.
    """
    for row in conn.execute(
        "select id, filename from documents where class_id = ? order by id", (class_id,)
    ):
        if Path(str(row["filename"])).stem == path.stem:
            return int(row["id"])
    return None


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
def _widened_k(k: int, *, reranking: bool) -> Iterator[None]:
    """Run retrieval with a larger neighbour count than the product uses.

    Both widths are set, because the product has two: `K` is what it serves and
    `RERANK_FETCH_K` is what it fetches for a reranker to choose from. Measuring rank needs
    the whole ranking, so both become `k`.

    Reranking is forced rather than left as configured. Whether the weights happen to be on
    the machine running the harness is not a property of the product, and a run whose
    numbers silently depended on that would not be comparable with the run before it.
    Setting it explicitly in both directions is what makes a pair of runs a measurement.
    """
    original_k = (retrieval.K, retrieval.RERANK_FETCH_K)
    original_server = retrieval.rerank_server
    retrieval.K = retrieval.RERANK_FETCH_K = k
    # `available` is a read-only property of the shared instance, so the object is stood in
    # for rather than mutated. Only `retrieve` reads it; `rag/rerank.py` keeps its own
    # reference and so still talks to the real server, which is what a `--rerank` run wants.
    retrieval.rerank_server = _RerankSwitch(original_server, available=reranking)
    try:
        yield
    finally:
        retrieval.K, retrieval.RERANK_FETCH_K = original_k
        retrieval.rerank_server = original_server


def _find_document(
    workspace: Workspace, conn: sqlite3.Connection, stem: str
) -> tuple[int, int, Path]:
    """The document a question set is about, as `(class_id, document_id, stored_path)`.

    The ingestion report is preferred, because that is what the ingest stage wrote and it
    names the run being asked about. The workspace database is the fallback, and it is what
    makes a question set answerable about a document that arrived by another route: the
    scanned handout is read by the `recognize` stage, which costs minutes of model time and
    writes no ingestion report, and re-uploading it to get one would spend those minutes
    again for nothing.
    """
    documents = _mapping(workspace.existing("ingestion").get("documents", {}))
    if stem in documents:
        record = documents[stem]
        document_id = int(record["document_id"])
    else:
        rows = conn.execute(
            "select id, filename from documents where filename like ? order by id",
            (f"%{stem}%",),
        ).fetchall()
        if not rows:
            raise SystemExit(
                f"{stem} is not in this workspace. Run the ingest or recognize stage on it first."
            )
        # A fragment that matches two documents is a question set that cannot say where its
        # answer is, and at class scale that is a live hazard rather than a theoretical one:
        # `homework_1` and `homework_1_solution` are both in the same class.
        if len(rows) > 1:
            names = ", ".join(str(row["filename"]) for row in rows)
            raise SystemExit(f"{stem} matches {len(rows)} documents in this workspace: {names}")
        document_id = int(rows[0]["id"])

    found = conn.execute(
        "select class_id, stored_path from documents where id = ?", (document_id,)
    ).fetchone()
    if found is None:
        raise SystemExit(f"Document {document_id} is recorded but not in the database.")
    return int(found["class_id"]), document_id, Path(str(found["stored_path"]))


@dataclass(frozen=True)
class Target:
    """The document a question's answer is in, and how that document names places in itself.

    Attributes:
        document_id: The row the answer has to come from for a hit to count.
        ranges: Outline paths to page spans, empty for a document that names no sections.
    """

    document_id: int
    ranges: dict[str, tuple[int, int]]


# A control names no document because it has no answer to find. Its document id matches
# nothing, which is exactly the reading a control wants: whatever came back is the wrong
# document, because every document is.
NO_TARGET = Target(document_id=0, ranges={})


def _resolve_targets(
    workspace: Workspace, conn: sqlite3.Connection, question_set: dict[str, object]
) -> tuple[int, dict[str, Target]]:
    """Every document the question set refers to, resolved once each.

    A set names one document at the top level and a question may override it with
    `expect_document`. That is what lets a set be asked against a class rather than against
    a document: the class is the search space either way, and naming the document per
    question is what makes a hit in the *wrong* document countable as a miss.

    Raises:
        SystemExit: If the named documents are not all in one class. Retrieval is
            class-scoped, so a set spanning two classes could never be answered in one run.
    """
    default = question_set.get("document")
    questions = [one for one in question_set["questions"] if isinstance(one, dict)]

    wanted: dict[str, bool] = {}
    for question in questions:
        if not _states_a_location(question):
            continue
        stem = question.get("expect_document") or default
        if stem is None:
            raise SystemExit(
                f"{question['id']} names no document, and the set has no default. "
                "Give the set a `document`, or the question an `expect_document`."
            )
        # A document needs its outline read only if some question addresses it by section.
        # Every scan has no outline, and reading one it does not have would fail the run
        # before a question was asked.
        wanted[str(stem)] = wanted.get(str(stem), False) or (
            question.get("expect_section") is not None
        )

    classes: set[int] = set()
    targets: dict[str, Target] = {}
    for stem, wants_sections in wanted.items():
        class_id, document_id, stored = _find_document(workspace, conn, stem)
        classes.add(class_id)
        ranges = section_ranges(read_outline(stored)) if wants_sections else {}
        targets[stem] = Target(document_id=document_id, ranges=ranges)

    if len(classes) > 1:
        raise SystemExit(
            f"The question set spans {len(classes)} classes. Retrieval never crosses a class, "
            "so no single run can answer it."
        )
    if not classes:
        raise SystemExit(
            "No question in this set says where its answer is, so nothing is measured."
        )
    return classes.pop(), targets


def _states_a_location(question: dict[str, object]) -> bool:
    """Whether a question has an answer to find, however it says where.

    False for a control, which is the point of a control: there is no document to name
    because there is nothing in the class to find.
    """
    return question.get("expect_section") is not None or question.get("expect_pages") is not None


def cmd_retrieve(args: argparse.Namespace) -> int:
    """Ask the question set and record where the right chunk landed in the ranking."""
    workspace = Workspace(Path(args.workspace).resolve())
    conn = prepare(workspace, Path(args.source_db))

    question_set = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    default = question_set.get("document")
    class_id, targets = _resolve_targets(workspace, conn, question_set)
    scale = conn.execute(
        "select (select count(*) from chunks where class_id = ?) as chunks, "
        "(select count(*) from documents where class_id = ?) as documents",
        (class_id, class_id),
    ).fetchone()

    if args.rerank and not settings.rerank_installed:
        raise SystemExit(
            "--rerank was asked for and the weights are not on this machine. "
            "Run `python scripts/fetch_models.py`."
        )

    results: list[dict[str, object]] = []
    started = time.monotonic()
    with _widened_k(args.k, reranking=args.rerank):
        for question in question_set["questions"]:
            stem = (
                str(question.get("expect_document") or default)
                if _states_a_location(question)
                else None
            )
            target = targets[stem] if stem is not None else NO_TARGET
            record = _ask(conn, class_id, stem, target, question, args.k)
            results.append(record)
            print(
                f"{record['id']}: rank {record['rank'] if record['rank'] else '-'}, "
                f"top similarity {record['top_similarity']}"
            )

    elapsed = time.monotonic() - started
    # Named after the question set rather than fixed, because a class is asked more than one
    # set and a single `retrieval.json` would mean each run destroyed the one before it.
    # The reranked run is named apart from the plain one for the same reason: the pair is
    # the measurement, so neither may overwrite the other.
    suffix = "-reranked" if args.rerank else ""
    workspace.write(
        f"retrieval-{Path(args.questions).stem}{suffix}",
        {
            "document": default or "several",
            "questions_file": Path(args.questions).name,
            "reranked": bool(args.rerank),
            # Per question, and the whole turn's retrieval rather than the rerank call
            # alone, because what a student waits for is the whole turn.
            "seconds_per_question": round(elapsed / max(1, len(results)), 2),
            "k": args.k,
            # The size of the haystack, recorded because a rank means nothing without it.
            # One document's rank 1 and a class's rank 1 are not the same measurement.
            "class_chunks": int(scale["chunks"]),
            "class_documents": int(scale["documents"]),
            "questions": results,
        },
    )
    conn.close()
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    """Chunk and embed a document already in the workspace again, and report what changed.

    What the app's Reindex action does, in process, which is what makes a change to the
    chunker measurable against a document that was expensive to read. A transcription lives
    on its page row and outlives a re-parse, so a scanned document re-indexes without
    spending the vision model again - and this is also the check that it really does.
    """
    workspace = Workspace(Path(args.workspace).resolve())
    conn = prepare(workspace, Path(args.source_db))
    if args.no_extraction:
        conn.execute("update settings set extraction_enabled = 0 where id = 1")
        conn.commit()

    _, document_id, _ = _find_document(workspace, conn, args.document)
    if args.recognize:
        # The student's explicit action, done explicitly here for the same reason: nothing
        # transcribes without being asked. This is what turns a scan already sitting in a
        # class from `unsupported` into something retrieval can reach, and doing it here
        # rather than through `recognize` is what keeps it one document rather than two.
        conn.execute("update documents set recognize = 1 where id = ?", (document_id,))
        conn.commit()
    before = _chunk_stats(conn, document_id)

    clock = StageClock()
    started = time.monotonic()
    with _timed_stages(clock):
        ingestion.run_ingestion(document_id)
    elapsed = round(time.monotonic() - started, 1)

    after = _chunk_stats(conn, document_id)
    row = conn.execute(
        "select state, error_message from documents where id = ?", (document_id,)
    ).fetchone()
    print(f"{args.document}: {row['state']} in {elapsed}s, {clock.rounded()}")
    for name, stats in (("before", before), ("after", after)):
        tokens = _mapping(stats.get("tokens"))
        print(
            f"  {name}: {stats['chunk_count']} chunks, mean {tokens.get('mean', 0)} tokens, "
            f"max {tokens.get('max', 0)}, {stats['chunks_with_section']} with a section title"
        )
    if row["error_message"]:
        print(f"  error: {row['error_message']}")
    conn.close()
    return 0


def _ask(
    conn: sqlite3.Connection,
    class_id: int,
    stem: str | None,
    target: Target,
    question: dict[str, object],
    k: int,
) -> dict[str, object]:
    """Run one question and locate the expected pages in the ranking.

    A question says where its answer is either by naming a section of the book's outline,
    which is the ground truth a book carries about itself, or by giving the pages outright.
    The second is not a lesser form of the first: a scanned handout has no outline to name,
    and the pages of an eight-page appendix are as checkable by eye as an outline entry.

    Either way the search covers the whole class, and a chunk from the wrong document is
    counted as a miss however well it scored. That is the point of running this against a
    class: the same page ranks first among eleven chunks and eleventh among a thousand, and
    only the second number describes what a student experiences.
    """
    expected = question.get("expect_section")
    stated = question.get("expect_pages")
    pages: tuple[int, int] | None = None
    if expected is not None:
        pages = target.ranges.get(str(expected))
        if pages is None:
            raise SystemExit(
                f"{question['id']}: no outline entry at path {expected!r}. "
                "The question set and the book disagree."
            )
    elif isinstance(stated, list) and len(stated) == 2:
        pages = (int(stated[0]), int(stated[1]))

    result = retrieval.retrieve(conn, class_id, str(question["question"]), UNLIMITED_BUDGET)
    chunks = result.chunks[:k]

    rank: int | None = None
    if pages is not None:
        for position, chunk in enumerate(chunks, start=1):
            page = chunk.page_number
            if chunk.document_id != target.document_id or page is None:
                continue
            if pages[0] <= page <= pages[1]:
                rank = position
                break

    # What outranked the answer, by document. Everything above the hit when there was one,
    # and the whole of the product's `k` when there was not, because a question that missed
    # was crowded out by all of it.
    ahead = chunks[: rank - 1] if rank else chunks[:PRODUCT_K]
    return {
        "id": question["id"],
        "question": question["question"],
        "expect_document": stem,
        "expect_section": expected,
        "expect_pages": list(pages) if pages else None,
        # A question is targeted when it has an answer to find, however it says where.
        "targeted": pages is not None,
        "rank": rank,
        "returned": len(chunks),
        "top_similarity": round(chunks[0].similarity, 4) if chunks else None,
        # Of the k the product would actually serve, how many came from the right document.
        "from_expected": sum(
            1 for chunk in chunks[:PRODUCT_K] if chunk.document_id == target.document_id
        ),
        "ahead": sorted(
            {chunk.filename for chunk in ahead if chunk.document_id != target.document_id}
        ),
        "neighbours": [
            {
                "document": chunk.filename,
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
    """The countable signals of whether a page's mathematics and structure survived.

    `notations` is the one to read across a whole document rather than per page. A model
    asked only to transcribe picks its table notation afresh on every page - the reference
    appendix came back in four of them - and a document whose tables are written four ways
    is one nothing downstream can read as tables at all. One notation is the target; the
    count is what says whether asking for it worked.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    return {
        "characters": len(text),
        "lines": len(lines),
        "lone_number_lines": sum(1 for line in lines if _LONE_NUMBER.match(line)),
        "matrix_markup": len(_MATRIX_MARKUP.findall(text)),
        "math_delimiters": text.count("$"),
        "notations": sorted(_notations(text)),
        # Headings the chunker can actually see, which is what decides whether a section
        # gets a name. A page of a numbered appendix that reports zero here has headings
        # the reader can see and the product cannot.
        "headings": len(chunking.SECTION_HEADING.findall(text)),
        "bold_headings": len(_BOLD_HEADING.findall(text)),
    }


# How a page wrote its tables. Named rather than counted, because the useful report is
# which notations a document used, not how many rows each one had.
_NOTATION_PATTERNS = (
    ("markdown-table", re.compile(r"^\s*\|.*\|\s*$\n\s*\|[\s:|-]+\|\s*$", re.MULTILINE)),
    ("bare-pipes", re.compile(r"^\s*[^|\n]+\|[^|\n]+$", re.MULTILINE)),
    ("latex-tabular", re.compile(r"\\begin\{tabular\}")),
)

# A heading the model emphasised instead of marking up: `**C.6 Discrete-Time Fourier**`.
# The chunker does not see one, and the reference appendix returned several.
_BOLD_HEADING = re.compile(r"^\s*\*\*[^*\n]+\*\*\s*$", re.MULTILINE)


def _notations(text: str) -> set[str]:
    """Which table notations appear on a page.

    `bare-pipes` is only reported where no Markdown table was found, because every
    Markdown table row also matches it. Reporting both would make the pinned notation look
    like two notations and the measurement would say the opposite of the truth.
    """
    found = {name for name, pattern in _NOTATION_PATTERNS if pattern.search(text)}
    if "markdown-table" in found:
        found.discard("bare-pipes")
    return found


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
    if not is_local_endpoint(config.endpoint_url):
        # The product asks before a page image leaves the machine; this stage answers yes
        # on the operator's behalf, and the operator should know it did.
        print(
            f"note: overriding the remote acknowledgement - page images will be sent "
            f"to {config.endpoint_url}\n"
        )

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
    pages = list(record["pages"])  # type: ignore[call-overload]
    lines = [f"# {name} as recognition read it", "", _consistency(pages), ""]
    for entry in pages:
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


def _consistency(pages: list[dict[str, object]]) -> str:
    """One sentence about whether the document was written one way or several.

    Across the document rather than per page, because that is the failure: every page is
    individually plausible and the eight of them together are four documents. It is a
    sentence rather than a verdict for the same reason the rest of this file is - whether
    a page was read correctly is a question only reading it answers.
    """
    notations: set[str] = set()
    headings = bold = unmarked = 0
    for entry in pages:
        shape = entry.get("shape")
        if not isinstance(shape, dict):
            continue
        found = {str(one) for one in shape.get("notations", [])}
        notations.update(found)
        unmarked += not found
        headings += int(shape.get("headings") or 0)
        bold += int(shape.get("bold_headings") or 0)

    written = ", ".join(sorted(notations)) or "none"
    return (
        f"Table notations used: {written}. "
        # Counted separately because on a document of tables it is not the absence of a
        # table, it is a table written as plain alternating lines - which is a notation
        # too, and the one nothing downstream can recognise.
        f"{unmarked} of {len(pages)} page(s) marked up no table at all. "
        f"{headings} heading(s) the chunker can see, {bold} written in bold instead."
    )


def cmd_report(args: argparse.Namespace) -> int:
    """Print what the recorded runs say, which is the only output anyone reads twice."""
    workspace = Workspace(Path(args.workspace).resolve())
    ingested = _mapping(workspace.existing("ingestion").get("documents", {}))
    # Every question set asked of this workspace, not one of them. A class is asked several,
    # and the interesting comparison is usually between two of them rather than inside one.
    retrieved = sorted(workspace.root.glob("retrieval*.json"))

    if ingested:
        _report_ingestion(ingested)
    for path in retrieved:
        _report_retrieval(json.loads(path.read_text(encoding="utf-8")))
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
    # `.get` throughout, because a workspace may hold reports written before Phase 3 added
    # `targeted` and `expect_section`, and `report` reads every retrieval file it finds.
    targeted = [
        one for one in questions if one.get("targeted", one.get("expect_section") is not None)
    ]
    controls = [one for one in questions if one not in targeted]
    if not questions:
        return

    scale = (
        f"{retrieved['class_documents']} documents, {retrieved['class_chunks']} chunks"
        if retrieved.get("class_chunks")
        else "scale not recorded"
    )
    how = "reranked" if retrieved.get("reranked") else "embedding order"
    rate = retrieved.get("seconds_per_question")
    cost = f", {rate}s a question" if rate else ""
    print(
        f"\nRetrieval over {retrieved.get('document')} ({scale}), "
        f"k = {retrieved.get('k')}, {how}{cost}"
    )
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

    _report_crowding(targeted)

    if controls:
        best = [one["top_similarity"] for one in targeted if one["top_similarity"] is not None]
        print("\n  Controls, which should look worse than the questions above")
        for one in controls:
            print(f"    {one['id']}: top similarity {one['top_similarity']}")
        if best:
            print(f"    real questions, median top similarity: {round(statistics.median(best), 4)}")

    print("\n  Per question")
    for one in questions:
        pages = one.get("expect_pages")
        target = one.get("expect_section") or (
            f"pages {pages[0]}-{pages[1]}" if pages else "not here"
        )
        print(f"    {one['id']}: rank {one['rank'] or '-'} of {one['returned']}, {target}")


def _report_crowding(targeted: list[dict[str, object]]) -> None:
    """What the class costs a question: how much of `k` the wrong documents took.

    A single-document workspace cannot produce this number at all, which is the reason it
    flatters retrieval. Here every one of a class's documents is competing for the same
    eight places, and a question can be answered by the right passage of the wrong term's
    answer key without any of these counts noticing - so this says how crowded the result
    was, not whether the answer was good.
    """
    measured = [one for one in targeted if "from_expected" in one]
    if not measured:
        return

    served = [int(one["from_expected"]) for one in measured]
    print(
        f"\n  Of the {PRODUCT_K} chunks the product would serve, "
        f"{sum(served)}/{PRODUCT_K * len(measured)} came from the expected document "
        f"(median {statistics.median(served)} per question)"
    )

    blocking: dict[str, int] = {}
    for one in measured:
        for name in one.get("ahead", []):  # type: ignore[union-attr]
            blocking[str(name)] = blocking.get(str(name), 0) + 1
    if not blocking:
        print("    nothing from another document outranked an answer")
        return

    ranked = sorted(blocking.items(), key=lambda pair: (-pair[1], pair[0]))
    print("    documents that outranked an answer, by how many questions they did it to:")
    for name, count in ranked[:_KEPT_NEIGHBOURS]:
        print(f"      {name}: {count}")


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
    retrieve.add_argument(
        "--rerank", action="store_true", help="Reorder the neighbours with the cross-encoder"
    )
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

    reindex = subparsers.add_parser(
        "reindex", help="Chunk and embed a document already in the workspace again"
    )
    reindex.add_argument("document", help="Which document, by a fragment of its filename")
    reindex.add_argument(
        "--no-extraction", action="store_true", help="Skip the profile extraction stage"
    )
    reindex.add_argument(
        "--recognize", action="store_true", help="Read it with the vision model first"
    )
    reindex.set_defaults(func=cmd_reindex)

    report = subparsers.add_parser("report", help="Summarise the recorded runs")
    report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
