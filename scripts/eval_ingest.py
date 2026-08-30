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
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
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
from backend.rag import (  # noqa: E402
    embed,  # noqa: E402
    render,
    transcribe,
)
from backend.rag import retrieve as retrieval  # noqa: E402
from backend.rag.chunk import MAX_CHUNK_TOKENS  # noqa: E402
from backend.rag.rerank import RerankStatus  # noqa: E402
from backend.storage.database import connect, migrate  # noqa: E402

logger = logging.getLogger("lyra.eval.ingest")

DEFAULT_WORKSPACE = ROOT / "data" / "eval-ingest"
DEFAULT_SOURCE_DB = ROOT / "data" / "lyra.db"
DEFAULT_QUESTIONS = ROOT / "scripts" / "eval_questions" / "kuttler-linear-algebra.json"
DEFAULT_CORPUS = ROOT / "scripts" / "eval_corpora" / "retrieval_class.json"
DEFAULT_CLASS_WORKSPACE = ROOT / "data" / "eval-class"

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


def _model_files(source_root: Path) -> list[Path]:
    """Every shareable file under the installation's models directory, hidden entries skipped."""
    files: list[Path] = []
    for entry in sorted(source_root.rglob("*")):
        if entry.name.startswith(".") or not entry.is_file():
            continue
        files.append(entry)
    return files


def _link_models(workspace: Workspace) -> None:
    """Share the real installation's model directory rather than downloading a second one.

    Everything else about the workspace is its own, but the embedding binary and its
    weights are identical by construction. Without this the
    workspace has no embedding server, every document fails at the embedding stage, and the
    harness reports a fault it created itself.

    The share is per-file hard links into a real directory, not a directory symlink: the
    privacy contract (``storage.private.secure_mkdir``) refuses a symlink component below
    the data root, and ``ensure_directories`` runs right after this. A hard link is the
    same inode as the canonical weight, so nothing is copied and the workspace cannot
    drift from the installation it measures.
    """
    link = workspace.root / "models"
    real = ROOT / "data" / "models"
    if link.exists() or not real.is_dir():
        return
    for source in _model_files(real):
        destination = link / source.relative_to(real)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, destination)
        except OSError as exc:
            raise SystemExit(
                f"Cannot share {source.name} into {link} by hard link ({exc}). The "
                f"installation's model directory must live on the same volume as the "
                f"workspace."
            ) from exc


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
        filename: The actual stored filename, used for cross-format document identity.
        ranges: Outline paths to page spans, empty for a document that names no sections.
        passage_anchors: Mapping from passage ID to anchor text that must appear in the
            chunk for a passage-level hit. Empty for documents with no section metadata.
    """

    document_id: int
    filename: str
    ranges: dict[str, tuple[int, int]]
    passage_anchors: dict[str, str] = field(default_factory=dict)


NO_TARGET = Target(document_id=0, filename="", ranges={})


def _load_corpus_sections(question_set: dict[str, object]) -> dict[str, dict[str, str]]:
    """Load passage anchors from the corpus file, keyed by stem then passage ID."""
    corpus_name = question_set.get("corpus")
    if not corpus_name:
        return {}
    corpus_path = ROOT / "scripts" / "eval_corpora" / f"{corpus_name}.json"
    if not corpus_path.exists():
        return {}
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    result: dict[str, dict[str, str]] = {}
    for doc in corpus.get("documents", []):
        stem = doc.get("stem", "")
        anchors: dict[str, str] = {}
        for sec in doc.get("sections", []):
            if sec.get("id") and sec.get("anchor"):
                anchors[sec["id"]] = sec["anchor"]
        if anchors:
            result[stem] = anchors
    return result


def _resolve_targets(
    workspace: Workspace,
    conn: sqlite3.Connection,
    question_set: dict[str, object],
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
    corpus_sections = _load_corpus_sections(question_set)

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
        wanted[str(stem)] = wanted.get(str(stem), False) or (
            question.get("expect_section") is not None
        )

    classes: set[int] = set()
    targets: dict[str, Target] = {}
    for stem, wants_sections in wanted.items():
        class_id, document_id, stored = _find_document(workspace, conn, stem)
        classes.add(class_id)
        ranges = section_ranges(read_outline(stored)) if wants_sections else {}
        row = conn.execute("select filename from documents where id = ?", (document_id,)).fetchone()
        filename = str(row["filename"]) if row else ""
        anchors = corpus_sections.get(stem, {})
        targets[stem] = Target(
            document_id=document_id,
            filename=filename,
            ranges=ranges,
            passage_anchors=anchors,
        )

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
    return (
        question.get("expect_section") is not None
        or question.get("expect_pages") is not None
        or question.get("expect_passage_id") is not None
    )


def _classify_rerank_validity(
    requested: bool, results: list[dict[str, object]]
) -> tuple[bool, bool, bool, set[str]]:
    """Per-query rerank applicability driven by whether candidates existed.

    Only ``EMPTY_INPUT`` is N/A for a requested-rerank run: zero candidates
    means there was nothing to rerank.  ``NOT_REQUESTED`` on a query with
    candidates is evidence that rerank did not execute despite being asked for,
    and must invalidate the reranked measurement.

    When *every* query is N/A (all ``EMPTY_INPUT``), reranking was never
    exercised. The run itself may be diagnostically valid, but it is not
    proof of a reranked baseline and must not be labelled ``observed_path =
    'reranked'``.

    Returns:
        ``(rerank_applied, rerank_degraded, rerank_unexercised, failure_reasons)``
    """
    if not requested:
        return False, False, False, set()

    applicable = [
        str(r["rerank_status"])
        for r in results
        if str(r["rerank_status"]) != RerankStatus.EMPTY_INPUT.value
    ]
    if not applicable:
        return False, False, True, set()

    failures = {s for s in applicable if s != RerankStatus.APPLIED.value}
    if failures:
        return False, True, False, failures
    return True, False, False, set()


def _question_line(record: dict[str, object]) -> str:
    """One progress line per question, over the current record shape."""
    return (
        f"{record['id']}: doc {record.get('document_rank') or '-'}, "
        f"passage {record.get('passage_rank') or '-'}, "
        f"top similarity {record.get('top_similarity')}"
    )


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
            print(_question_line(record))

    elapsed = time.monotonic() - started

    rerank_applied, rerank_degraded, rerank_unexercised, degradation_reasons = (
        _classify_rerank_validity(args.rerank, results)
    )

    if rerank_degraded:
        reason_str = ", ".join(sorted(degradation_reasons))
        suffix = "-rerank-INVALID"
    elif rerank_unexercised:
        suffix = "-rerank-UNEXERCISED"
    elif rerank_applied:
        suffix = "-reranked"
    else:
        suffix = ""

    if rerank_applied:
        observed_path = "reranked"
    elif rerank_unexercised:
        observed_path = "unexercised"
    else:
        observed_path = "embedding_order"
    questions_path = Path(args.questions)
    corpus_path = None
    if questions_path.exists():
        qdata = json.loads(questions_path.read_text(encoding="utf-8"))
        corpus_name = qdata.get("corpus")
        if corpus_name:
            corpus_path = ROOT / "scripts" / "eval_corpora" / f"{corpus_name}.json"

    workspace.write(
        f"retrieval-{Path(args.questions).stem}{suffix}",
        {
            "document": default or "several",
            "questions_file": questions_path.name,
            "requested_rerank": bool(args.rerank),
            "reranked": rerank_applied,
            "observed_path": observed_path,
            "degraded": rerank_degraded,
            "degradation_reasons": sorted(degradation_reasons) if rerank_degraded else [],
            "valid": not rerank_degraded,
            "seconds_per_question": round(elapsed / max(1, len(results)), 2),
            "k": args.k,
            "class_chunks": int(scale["chunks"]),
            "class_documents": int(scale["documents"]),
            "questions": results,
            "metadata": _reproducibility_metadata(args, corpus_path),
        },
    )
    conn.close()

    if rerank_degraded:
        print(
            f"\nINVALID: --rerank was requested but reranking did not run. "
            f"Observed: {reason_str}. "
            f"The report is marked invalid and must not be used as a reranked baseline."
        )
        return 1
    if rerank_unexercised:
        print(
            "\nUNEXERCISED: --rerank was requested but every query had zero candidates. "
            "Reranking was never exercised. The run is not proof of a reranked baseline."
        )
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
    """Run one question and locate the expected chunk in the ranking.

    Two independent dimensions are recorded:

    ``document_rank``
        Position of the first chunk belonging to the expected document, regardless
        of whether it is the expected passage.  Document-level hit/MRR are derived
        from this.

    ``passage_rank``
        Position of the first chunk satisfying the passage/section/page locator.
        Passage-level hit/MRR are derived from this.

    The locator that determines ``passage_rank`` is chosen by the annotation:
    1. ``expect_section``: outline path resolved to page range (PDF books).
    2. ``expect_pages``: explicit page span (scanned handouts, page-ground-truth).
    3. ``expect_passage_id``: passage anchor text must appear in chunk content.

    A chunk from the wrong document is always a miss on both dimensions.
    """
    expected = question.get("expect_section")
    stated = question.get("expect_pages")
    passage_id = question.get("expect_passage_id")
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

    passage_anchor: str | None = None
    if passage_id is not None:
        passage_anchor = target.passage_anchors.get(str(passage_id))
        if passage_anchor is None:
            raise SystemExit(
                f"{question['id']}: expect_passage_id {passage_id!r} cannot be resolved "
                f"for {stem!r}. The corpus file is missing, has no section map for this "
                f"document, or the passage ID is absent."
            )

    targeted = pages is not None or passage_anchor is not None
    result = retrieval.retrieve(conn, class_id, str(question["question"]), UNLIMITED_BUDGET)
    chunks = result.chunks[:k]

    document_rank: int | None = None
    passage_rank: int | None = None

    for position, chunk in enumerate(chunks, start=1):
        if chunk.document_id != target.document_id:
            continue

        if document_rank is None:
            document_rank = position

        if passage_rank is None:
            if passage_anchor is not None:
                if passage_anchor in chunk.content:
                    passage_rank = position
            elif pages is not None:
                page = chunk.page_number
                if page is not None and pages[0] <= page <= pages[1]:
                    passage_rank = position

    document_hit = document_rank is not None if targeted else None
    passage_hit = passage_rank is not None if targeted else None

    ref_rank = passage_rank or document_rank
    ahead = chunks[: ref_rank - 1] if ref_rank else chunks[:PRODUCT_K]
    record: dict[str, object] = {
        "id": question["id"],
        "question": question["question"],
        "expect_document": stem,
        "expect_filename": target.filename if targeted else None,
        "expect_section": expected,
        "expect_pages": list(pages) if pages else None,
        "expect_passage_id": passage_id,
        "targeted": targeted,
        "document_rank": document_rank,
        "document_hit": document_hit,
        "passage_rank": passage_rank,
        "passage_hit": passage_hit,
        "returned": len(chunks),
        "top_similarity": round(chunks[0].similarity, 4) if chunks else None,
        "rerank_status": result.rerank_status.value,
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
    return record


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
    how = retrieved.get("observed_path") or (
        "reranked" if retrieved.get("reranked") else "embedding order"
    )
    rate = retrieved.get("seconds_per_question")
    cost = f", {rate}s a question" if rate else ""
    valid = retrieved.get("valid", True)
    validity = "" if valid else " ** INVALID **"
    print(
        f"\nRetrieval over {retrieved.get('document')} ({scale}), "
        f"k = {retrieved.get('k')}, {how}{cost}{validity}"
    )
    if retrieved.get("degraded"):
        reasons = retrieved.get("degradation_reasons", [])
        print(f"  DEGRADED: requested rerank, got {', '.join(str(r) for r in reasons)}")
    if retrieved.get("requested_rerank") is not None:
        print(
            f"  requested: {'rerank' if retrieved.get('requested_rerank') else 'embedding order'}, "
            f"observed: {how}"
        )
    doc_ranks = [one["document_rank"] for one in targeted if one.get("document_rank")]
    pass_ranks = [one["passage_rank"] for one in targeted if one.get("passage_rank")]
    for k in REPORTED_K:
        if k > int(retrieved.get("k") or 0):
            continue
        d_hits = sum(1 for r in doc_ranks if r <= k)
        p_hits = sum(1 for r in pass_ranks if r <= k)
        n = len(targeted)
        print(f"  hit rate at k={k:>2}: doc {d_hits}/{n}, passage {p_hits}/{n}")
    if doc_ranks:
        print(f"  median document rank of a hit: {statistics.median(doc_ranks)}")
    if pass_ranks:
        print(f"  median passage rank of a hit: {statistics.median(pass_ranks)}")

    missed = [one["id"] for one in targeted if not one.get("document_rank")]
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
        dr = one.get("document_rank") or "-"
        pr = one.get("passage_rank") or "-"
        print(f"    {one['id']}: doc {dr} / passage {pr} of {one['returned']}, {target}")


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


def _git_revision() -> str:
    """The current git commit, or 'unknown' outside a repository."""
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],  # noqa: S607
                cwd=ROOT,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _corpus_hash(path: Path) -> str:
    """SHA-256 of the corpus file, short enough to compare by eye."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _corpus_path_from_retrieval(data: dict[str, object]) -> Path | None:
    """Recover the corpus path from a retrieval result's metadata."""
    meta = data.get("metadata")
    if isinstance(meta, dict):
        qfile = meta.get("questions_file")
        if qfile:
            qpath = ROOT / "scripts" / "eval_questions" / str(qfile)
            if qpath.exists():
                qdata = json.loads(qpath.read_text(encoding="utf-8"))
                corpus_name = qdata.get("corpus")
                if corpus_name:
                    cpath = ROOT / "scripts" / "eval_corpora" / f"{corpus_name}.json"
                    if cpath.exists():
                        return cpath
    return None


def _reproducibility_metadata(
    args: argparse.Namespace, corpus_path: Path | None = None
) -> dict[str, object]:
    """State sufficient to reproduce or compare a measurement."""
    meta: dict[str, object] = {
        "git_revision": _git_revision(),
        "embedding_model": embed.EMBEDDING_MODEL,
        "embedding_dim": embed.EMBEDDING_DIM,
        "rerank_model": (
            str(settings.rerank_model_path.name) if settings.rerank_installed else None
        ),
        "chunk_max_tokens": MAX_CHUNK_TOKENS,
        "chunk_overlap_tokens": getattr(settings, "chunk_overlap_tokens", None),
    }
    if corpus_path and corpus_path.exists():
        corpus_data = json.loads(corpus_path.read_text(encoding="utf-8"))
        meta["corpus_version"] = corpus_data.get("corpus_version", "unknown")
        meta["corpus_hash"] = _corpus_hash(corpus_path)
    if hasattr(args, "questions"):
        qpath = Path(args.questions)
        if qpath.exists():
            qdata = json.loads(qpath.read_text(encoding="utf-8"))
            meta["questions_version"] = qdata.get("corpus_version", "unknown")
            meta["questions_file"] = qpath.name
            meta["questions_hash"] = hashlib.sha256(qpath.read_bytes()).hexdigest()[:16]
    if hasattr(args, "k"):
        meta["retrieval_k"] = args.k
    if hasattr(args, "rerank"):
        meta["requested_rerank"] = bool(args.rerank)
    return meta


def cmd_build_corpus(args: argparse.Namespace) -> int:
    """Build a workspace from the committed evaluation corpus and ingest every document."""
    corpus_path = Path(args.corpus).resolve()
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    workspace = Workspace(Path(args.workspace).resolve())

    if args.fresh and workspace.root.exists():
        shutil.rmtree(workspace.root)

    conn = prepare(workspace, Path(args.source_db))
    if args.no_extraction:
        conn.execute("update settings set extraction_enabled = 0 where id = 1")
        conn.commit()
    class_name = corpus.get("class_name", "Evaluation")
    class_id = _class_id(conn, class_name)

    doc_dir = workspace.root / "corpus_sources"
    doc_dir.mkdir(parents=True, exist_ok=True)

    documents: dict[str, dict[str, object]] = {}
    for doc_spec in corpus["documents"]:
        stem = doc_spec["stem"]
        filename = doc_spec["filename"]
        text = doc_spec["text"]

        source_file = doc_dir / filename
        source_file.write_text(text, encoding="utf-8")

        duplicate = _already_ingested(conn, class_id, source_file)
        if duplicate is not None:
            print(f"{filename}: already in this workspace as document {duplicate}; skipped.")
            continue

        record = _ingest_one(conn, class_id, source_file)
        documents[stem] = record
        workspace.write(
            "ingestion",
            {
                "class_id": class_id,
                "corpus_version": corpus.get("corpus_version", "unknown"),
                "documents": {
                    **_mapping(workspace.existing("ingestion").get("documents", {})),
                    **documents,
                },
            },
        )
        print(
            f"{filename}: {record['state']} in {record['seconds']}s, {record['chunk_count']} chunks"
        )

    conn.close()
    print(f"\nCorpus built: {len(documents)} documents in {workspace.root}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    """Score a retrieval run with document-aware metrics and write a graded report.

    Returns nonzero when any scored retrieval result is invalid, making it
    impossible to mistake the stage for a passing baseline.  Diagnostic JSON
    is still written for inspection.
    """
    workspace = Workspace(Path(args.workspace).resolve())
    retrieved = sorted(workspace.root.glob("retrieval*.json"))
    if not retrieved:
        print("No retrieval results found. Run the retrieve stage first.")
        return 1

    exit_code = 0
    for path in retrieved:
        data = json.loads(path.read_text(encoding="utf-8"))
        questions = [q for q in data.get("questions", []) if isinstance(q, dict)]
        if not questions:
            continue

        valid = data.get("valid", True)
        if not valid:
            exit_code = 1
        observed = data.get(
            "observed_path", "reranked" if data.get("reranked") else "embedding_order"
        )

        targeted = [q for q in questions if q.get("targeted")]
        controls = [q for q in questions if q not in targeted]

        scores: dict[str, object] = {
            "report_file": path.name,
            "observed_path": observed,
            "valid": valid,
            "question_count": len(questions),
            "targeted_count": len(targeted),
            "control_count": len(controls),
        }

        if targeted:
            doc_ranks = [q["document_rank"] for q in targeted if q.get("document_rank")]
            doc_misses = [q["id"] for q in targeted if not q.get("document_rank")]
            passage_ranks = [q["passage_rank"] for q in targeted if q.get("passage_rank")]
            passage_misses = [q["id"] for q in targeted if not q.get("passage_rank")]

            scores["document_hit_rates"] = {}
            scores["passage_hit_rates"] = {}
            for k_val in REPORTED_K:
                if k_val > int(data.get("k") or 0):
                    continue
                d_hits = sum(1 for r in doc_ranks if r <= k_val)
                scores["document_hit_rates"][f"k={k_val}"] = {
                    "hits": d_hits,
                    "total": len(targeted),
                    "rate": round(d_hits / len(targeted), 4),
                }
                p_hits = sum(1 for r in passage_ranks if r <= k_val)
                scores["passage_hit_rates"][f"k={k_val}"] = {
                    "hits": p_hits,
                    "total": len(targeted),
                    "rate": round(p_hits / len(targeted), 4),
                }

            scores["document_mrr"] = round(sum(1.0 / r for r in doc_ranks) / len(targeted), 4)
            passage_rr_sum = sum(1.0 / r for r in passage_ranks)
            scores["passage_mrr"] = round(passage_rr_sum / len(targeted), 4)
            # Median rank of hits: operates only on found ranks, not over
            # all targeted queries.  A miss has no rank to take a median of.
            scores["passage_median_rank"] = (
                statistics.median(passage_ranks) if passage_ranks else None
            )

            scores["document_misses"] = doc_misses
            scores["passage_misses"] = passage_misses

            by_document: dict[str, list[dict[str, object]]] = {}
            for q in targeted:
                doc = str(q.get("expect_document") or "unknown")
                by_document.setdefault(doc, []).append(q)

            per_doc: dict[str, object] = {}
            for doc, qs in sorted(by_document.items()):
                dr = [q["document_rank"] for q in qs if q.get("document_rank")]
                pr = [q["passage_rank"] for q in qs if q.get("passage_rank")]
                per_doc[doc] = {
                    "targeted": len(qs),
                    "document_found": len(dr),
                    "passage_found": len(pr),
                    "document_mrr": round(sum(1.0 / r for r in dr) / len(qs), 4),
                    "passage_mrr": round(sum(1.0 / r for r in pr) / len(qs), 4),
                }
            scores["by_document"] = per_doc

            wrong_doc = 0
            for q in targeted:
                nbs = q.get("neighbours")
                if not isinstance(nbs, list) or not nbs:
                    continue
                top_filename = str(nbs[0].get("document", ""))
                expected_filename = str(q.get("expect_filename") or "")
                if not expected_filename:
                    continue
                if top_filename != expected_filename:
                    wrong_doc += 1
            scores["wrong_document_top1"] = wrong_doc

            by_category: dict[str, list[dict[str, object]]] = {}
            for q in questions:
                cat = str(q.get("category", "uncategorized"))
                by_category.setdefault(cat, []).append(q)
            cat_scores: dict[str, object] = {}
            for cat, qs in sorted(by_category.items()):
                targ = [q for q in qs if q.get("targeted")]
                cat_pr = [q["passage_rank"] for q in targ if q.get("passage_rank")]
                cat_scores[cat] = {
                    "count": len(qs),
                    "targeted": len(targ),
                    "passage_found": len(cat_pr),
                    "passage_mrr": round(sum(1.0 / r for r in cat_pr) / len(targ), 4)
                    if targ
                    else 0.0,
                }
            scores["by_category"] = cat_scores

        if controls:
            control_sims = [
                q["top_similarity"] for q in controls if q.get("top_similarity") is not None
            ]
            targeted_sims = [
                q["top_similarity"] for q in targeted if q.get("top_similarity") is not None
            ]
            scores["control_median_similarity"] = (
                round(statistics.median(control_sims), 4) if control_sims else None
            )
            scores["targeted_median_similarity"] = (
                round(statistics.median(targeted_sims), 4) if targeted_sims else None
            )

        retrieval_meta = data.get("metadata")
        scores["metadata"] = dict(retrieval_meta) if isinstance(retrieval_meta, dict) else {}

        score_name = path.stem.replace("retrieval", "scores")
        workspace.write(score_name, scores)
        _print_scores(scores)

    return exit_code


def _print_scores(scores: dict[str, object]) -> None:
    """Print a scored report to stdout."""
    valid = scores.get("valid", True)
    path = scores.get("observed_path", "unknown")
    validity = "" if valid else " ** INVALID **"
    print(f"\nScored: {scores.get('report_file')} ({path}){validity}")

    for label, key in [("Document", "document_hit_rates"), ("Passage", "passage_hit_rates")]:
        hit_rates = scores.get(key)
        if isinstance(hit_rates, dict):
            for k_label, data in hit_rates.items():
                if isinstance(data, dict):
                    rate = data.get("rate", 0)
                    print(
                        f"  {label} {k_label}: {data.get('hits')}/{data.get('total')} ({rate:.1%})"
                    )

    d_mrr = scores.get("document_mrr")
    if d_mrr is not None:
        print(f"  Document MRR: {d_mrr}")
    p_mrr = scores.get("passage_mrr")
    if p_mrr is not None:
        print(f"  Passage MRR: {p_mrr}")
    p_median = scores.get("passage_median_rank")
    if p_median is not None:
        print(f"  Passage median rank: {p_median}")

    for label, key in [("Document", "document_misses"), ("Passage", "passage_misses")]:
        misses = scores.get(key)
        if misses:
            print(f"  {label} missed: {', '.join(str(m) for m in misses)}")

    by_doc = scores.get("by_document")
    if isinstance(by_doc, dict) and by_doc:
        print("\n  Per document:")
        for doc, data in by_doc.items():
            if isinstance(data, dict):
                print(
                    f"    {doc}: doc {data.get('document_found')}/{data.get('targeted')}"
                    f" MRR {data.get('document_mrr', 0)},"
                    f" passage {data.get('passage_found')}/{data.get('targeted')}"
                    f" MRR {data.get('passage_mrr', 0)}"
                )

    by_cat = scores.get("by_category")
    if isinstance(by_cat, dict) and by_cat:
        print("\n  Per category:")
        for cat, data in by_cat.items():
            if isinstance(data, dict):
                print(
                    f"    {cat}: {data.get('passage_found')}/{data.get('targeted')},"
                    f" passage MRR {data.get('passage_mrr', 0)}"
                )

    wrong = scores.get("wrong_document_top1")
    if wrong:
        print(f"\n  Wrong-document top-1: {wrong}")


# Regression thresholds for the comparison gate.  A drop larger than these
# from baseline to candidate exits nonzero.  The same threshold applies to
# both document and passage dimensions: a 5 pp MRR drop is a regression
# regardless of which dimension it appears in, and a 5 pp hit-rate drop at
# any shared k is likewise.
MRR_REGRESSION_THRESHOLD = -0.05
HIT_RATE_REGRESSION_THRESHOLD = -0.05

_REQUIRED_COMPATIBILITY_KEYS = (
    "corpus_hash",
    "questions_hash",
    "embedding_model",
    "embedding_dim",
    "retrieval_k",
    "requested_rerank",
)
_OPTIONAL_COMPATIBILITY_KEYS = (
    "corpus_version",
    "chunk_max_tokens",
    "chunk_overlap_tokens",
    "rerank_model",
)


def _check_compatibility(
    meta_a: dict[str, object],
    meta_b: dict[str, object],
    allow_override: bool,
) -> list[str]:
    """Return a list of incompatible fields. Empty means comparable.

    Required keys must be present on both sides; a missing required key is
    INCOMPATIBLE (fail closed), not silently skipped.
    """
    problems: list[str] = []
    for key in _REQUIRED_COMPATIBILITY_KEYS:
        va, vb = meta_a.get(key), meta_b.get(key)
        if va is None or vb is None:
            label = f"{key}: missing ({'baseline' if va is None else 'candidate'})"
            if allow_override:
                problems.append(f"WARNING: {label} (override: comparing anyway)")
            else:
                problems.append(f"INCOMPATIBLE: {label}")
        elif va != vb:
            label = f"{key}: {va} vs {vb}"
            if allow_override:
                problems.append(f"WARNING: {label} (override: comparing anyway)")
            else:
                problems.append(f"INCOMPATIBLE: {label}")
    for key in _OPTIONAL_COMPATIBILITY_KEYS:
        va, vb = meta_a.get(key), meta_b.get(key)
        if va is not None and vb is not None and va != vb:
            label = f"{key}: {va} vs {vb}"
            if allow_override:
                problems.append(f"WARNING: {label} (override: comparing anyway)")
            else:
                problems.append(f"INCOMPATIBLE: {label}")
    if meta_a.get("requested_rerank") and meta_b.get("requested_rerank"):
        ra, rb = meta_a.get("rerank_model"), meta_b.get("rerank_model")
        if ra is None or rb is None:
            label = f"rerank_model: missing ({'baseline' if ra is None else 'candidate'})"
            if allow_override:
                problems.append(f"WARNING: {label} (override: comparing anyway)")
            else:
                problems.append(f"INCOMPATIBLE: {label}")
        elif ra != rb:
            label = f"rerank_model: {ra} vs {rb}"
            if allow_override:
                problems.append(f"WARNING: {label} (override: comparing anyway)")
            else:
                problems.append(f"INCOMPATIBLE: {label}")
    return problems


def _score_identity(scores: dict[str, object]) -> str:
    """A stable key for pairing scored runs by what they measured."""
    observed = str(scores.get("observed_path", ""))
    report = str(scores.get("report_file", ""))
    return f"{observed}::{report}"


def cmd_compare(args: argparse.Namespace) -> int:
    """Compare two scored retrieval runs and flag regressions.

    Pairing is by identity (observed_path + report_file), not by position. Runs
    that exist in one workspace but not the other are flagged as unmatched.
    Incompatible metadata (different corpus, embedding, chunking, retrieval k,
    or reranker identity) fails closed unless ``--force`` is passed; a forced
    comparison is exploratory and must not be used as a release gate.

    Both document-level and passage-level metrics are compared independently.
    Regression thresholds are ``MRR_REGRESSION_THRESHOLD`` and
    ``HIT_RATE_REGRESSION_THRESHOLD``, defined centrally above.
    """
    ws_a = Workspace(Path(args.baseline).resolve())
    ws_b = Workspace(Path(args.candidate).resolve())
    allow_override = getattr(args, "force", False)

    scores_a = sorted(ws_a.root.glob("scores*.json"))
    scores_b = sorted(ws_b.root.glob("scores*.json"))

    if not scores_a:
        print(f"No scored results in {args.baseline}. Run the score stage first.")
        return 1
    if not scores_b:
        print(f"No scored results in {args.candidate}. Run the score stage first.")
        return 1

    map_a: dict[str, dict[str, object]] = {}
    for path in scores_a:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_source_path"] = str(path)
        key = _score_identity(data)
        if key in map_a:
            print(f"Duplicate identity in baseline: {key}")
            return 1
        map_a[key] = data

    map_b: dict[str, dict[str, object]] = {}
    for path in scores_b:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_source_path"] = str(path)
        key = _score_identity(data)
        if key in map_b:
            print(f"Duplicate identity in candidate: {key}")
            return 1
        map_b[key] = data

    all_keys = sorted(set(map_a) | set(map_b))
    exit_code = 0

    for key in all_keys:
        a = map_a.get(key)
        b = map_b.get(key)

        if a is None:
            print(f"\nUNMATCHED in candidate only: {key}")
            exit_code = 1
            continue
        if b is None:
            print(f"\nUNMATCHED in baseline only: {key}")
            exit_code = 1
            continue

        src_a = Path(str(a.get("_source_path", "")))
        src_b = Path(str(b.get("_source_path", "")))
        print(f"\nComparison: {src_a.name} vs {src_b.name}")

        meta_a = a.get("metadata", {})
        meta_b = b.get("metadata", {})
        if isinstance(meta_a, dict) and isinstance(meta_b, dict):
            problems = _check_compatibility(meta_a, meta_b, allow_override)
            for p in problems:
                print(f"  {p}")
            if any("INCOMPATIBLE" in p for p in problems):
                exit_code = 1
                continue

        if not a.get("valid", True):
            print(f"  BASELINE INVALID: {src_a.name}")
            exit_code = 1
        if not b.get("valid", True):
            print(f"  CANDIDATE INVALID: {src_b.name}")
            exit_code = 1

        for dimension in ("document", "passage"):
            mrr_key = f"{dimension}_mrr"
            hr_key = f"{dimension}_hit_rates"

            mrr_a = a.get(mrr_key)
            mrr_b = b.get(mrr_key)
            if mrr_a is None and mrr_b is None:
                continue
            if mrr_a is None or mrr_b is None:
                side = "baseline" if mrr_a is None else "candidate"
                print(f"  {dimension} MRR: missing on {side}")
                if not allow_override:
                    exit_code = 1
                continue
            if isinstance(mrr_a, (int, float)) and isinstance(mrr_b, (int, float)):
                delta = mrr_b - mrr_a
                tag = "IMPROVEMENT" if delta > 0 else "REGRESSION" if delta < 0 else "UNCHANGED"
                print(f"  {dimension} MRR: {mrr_a} -> {mrr_b} ({tag}, delta {delta:+.4f})")
                if delta < MRR_REGRESSION_THRESHOLD:
                    exit_code = 1

            hit_a = a.get(hr_key)
            hit_b = b.get(hr_key)
            if hit_a is None and hit_b is None:
                pass
            elif hit_a is None or hit_b is None:
                side = "baseline" if hit_a is None else "candidate"
                print(f"  {dimension} hit rates: missing on {side}")
                if not allow_override:
                    exit_code = 1
            elif isinstance(hit_a, dict) and isinstance(hit_b, dict):
                for k_label in sorted(set(hit_a) | set(hit_b)):
                    ra = hit_a.get(k_label)
                    rb = hit_b.get(k_label)
                    if ra is None or rb is None:
                        side = "baseline" if ra is None else "candidate"
                        print(f"  {dimension} {k_label}: missing on {side}")
                        if not allow_override:
                            exit_code = 1
                    elif isinstance(ra, dict) and isinstance(rb, dict):
                        rate_a = ra.get("rate", 0)
                        rate_b = rb.get("rate", 0)
                        tag = (
                            "IMPROVEMENT"
                            if rate_b > rate_a
                            else "REGRESSION"
                            if rate_b < rate_a
                            else "UNCHANGED"
                        )
                        print(f"  {dimension} {k_label}: {rate_a:.1%} -> {rate_b:.1%} ({tag})")
                        if rate_b - rate_a < HIT_RATE_REGRESSION_THRESHOLD:
                            exit_code = 1

    return exit_code


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

    build = subparsers.add_parser(
        "build-corpus", help="Build a workspace from the committed evaluation corpus"
    )
    build.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    build.add_argument("--fresh", action="store_true", help="Delete the workspace first")
    build.add_argument(
        "--no-extraction", action="store_true", help="Skip the profile extraction stage"
    )
    build.set_defaults(func=cmd_build_corpus, workspace=str(DEFAULT_CLASS_WORKSPACE))

    score = subparsers.add_parser(
        "score", help="Score retrieval results with document-aware metrics"
    )
    score.set_defaults(func=cmd_score)

    compare = subparsers.add_parser(
        "compare", help="Compare two scored workspaces and flag regressions"
    )
    compare.add_argument("baseline", help="Path to the baseline workspace")
    compare.add_argument("candidate", help="Path to the candidate workspace")
    compare.add_argument(
        "--force",
        action="store_true",
        help="Compare even when metadata is incompatible (exploratory, non-gating)",
    )
    compare.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
