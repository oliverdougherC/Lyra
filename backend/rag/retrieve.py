"""Retrieval: exact KNN over one class partition, recency weighting, and budgeting.

Stage 6 of docs/rag-pipeline.md. The search is exact brute-force over the class
partition rather than approximate, because sqlite-vec has no ANN index and at a few
thousand chunks per class it does not need one.

Zero chunks is a valid result. A class with nothing indexed, or a question no chunk
answers, produces an empty `RetrievalResult` and the turn is built with no context
block. It is not an error and must not be reported as one.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlite_vec

from backend.rag.embed import embed_query
from backend.rag.tokens import estimate_tokens

K = 8

# The bonus is deliberately smaller than any meaningful similarity gap: it breaks ties
# between comparable matches, it does not promote a weak match over a strong one.
RECENCY_COEFFICIENT = 0.05
RECENCY_HORIZON_DAYS = 120

_SECONDS_PER_DAY = 86400.0

# `k` is inlined from the module constant rather than bound, because sqlite-vec reads the
# KNN limit out of the query text.
_KNN_SQL = f"""
select chunk_id, distance
from chunk_embeddings
where embedding match ? and k = {K} and class_id = ?
"""  # noqa: S608

_CHUNK_SELECT = """
select c.id as chunk_id, c.document_id, c.content, c.token_count, c.page_number,
       c.section_title, c.problem_number, c.part_index, d.filename, d.created_at
from chunks c
join documents d on d.id = c.document_id
"""

# Ordered by id, which is document order, because that is what makes one problem's parts
# contiguous and so lets `_sibling_run` tell two problems carrying the same number apart.
# The run is put back into part order before it is emitted.
_PROBLEM_SQL = _CHUNK_SELECT + "where c.document_id = ? and c.problem_number = ?\norder by c.id"


@dataclass(frozen=True)
class RetrievedChunk:
    """One chunk the KNN returned, joined to its document and scored.

    Attributes:
        similarity: Cosine similarity to the query, in [0, 1].
        score: `similarity` plus the recency bonus. This is the ranking key.
    """

    chunk_id: int
    document_id: int
    content: str
    token_count: int
    page_number: int | None
    section_title: str | None
    problem_number: str | None
    part_index: int | None
    filename: str
    similarity: float
    score: float


@dataclass(frozen=True)
class RetrievalResult:
    """Ranked chunks, plus what the interface has to be told about what did not fit.

    Attributes:
        chunks: Chunks in prompt order, already inside the budget.
        trimmed: True when the budget dropped more than half of what was retrieved.
        omitted_document_count: Distinct documents among the dropped chunks.
    """

    chunks: list[RetrievedChunk]
    trimmed: bool
    omitted_document_count: int


def retrieve(
    conn: sqlite3.Connection,
    class_id: int,
    query: str,
    budget_tokens: int,
    document_id: int | None = None,
) -> RetrievalResult:
    """Find the chunks of one class that best answer `query` and fit the budget.

    Args:
        conn: Open database connection.
        class_id: Class partition to search. Retrieval never crosses classes.
        query: The student's question, embedded here with the query task prefix.
        budget_tokens: Retrieval share of the context window, measured with
            `estimate_tokens`.
        document_id: Restrict the result to one document. Applied after the KNN, so the
            neighbours are still chosen from the whole class.

    Returns:
        The ranked chunks that fit, and the trim reporting for the ones that did not.
        An empty result is normal, not an error.
    """
    distances = _knn(conn, class_id, embed_query(query))
    if not distances:
        return RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)

    candidates = _load_candidates(conn, distances, document_id)
    if not candidates:
        return RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)

    kept, dropped = _fit_to_budget(candidates, budget_tokens)
    return RetrievalResult(
        chunks=_expand_problem_parts(conn, kept, budget_tokens),
        # More than half gone means the answer is missing enough material that the user
        # deserves to be told, per Stage 7 of docs/rag-pipeline.md.
        trimmed=len(dropped) * 2 > len(candidates),
        omitted_document_count=len({chunk.document_id for chunk in dropped}),
    )


def _knn(conn: sqlite3.Connection, class_id: int, vector: list[float]) -> dict[int, float]:
    """Run the partitioned KNN and return distance by chunk id."""
    rows = conn.execute(_KNN_SQL, (sqlite_vec.serialize_float32(vector), class_id)).fetchall()
    return {int(row["chunk_id"]): float(row["distance"]) for row in rows}


def _load_candidates(
    conn: sqlite3.Connection, distances: dict[int, float], document_id: int | None
) -> list[RetrievedChunk]:
    """Join the neighbours to their content and document metadata, then rank them."""
    placeholders = ", ".join("?" * len(distances))
    sql = f"{_CHUNK_SELECT}where c.id in ({placeholders})"  # noqa: S608
    parameters: list[object] = list(distances)
    if document_id is not None:
        sql += " and c.document_id = ?"
        parameters.append(document_id)

    now = datetime.now(UTC)
    chunks: list[RetrievedChunk] = []
    for row in conn.execute(sql, parameters):
        # The metric is cosine and the vectors arrive L2-normalized from llama.cpp, so
        # the distance is already 1 - cosine similarity. Do not add a normalization step
        # here: it would silently change every score in the product.
        similarity = 1.0 - distances[int(row["chunk_id"])]
        score = similarity + RECENCY_COEFFICIENT * _recency_factor(row["created_at"], now)
        chunks.append(_chunk_from_row(row, similarity, score))

    # Chunk id is the tiebreak, so an exact tie orders the same way on every run.
    chunks.sort(key=lambda chunk: (-chunk.score, chunk.chunk_id))
    return chunks


def _chunk_from_row(row: sqlite3.Row, similarity: float, score: float) -> RetrievedChunk:
    """Build a chunk from a `_CHUNK_SELECT` row and its already-computed ranking."""
    return RetrievedChunk(
        chunk_id=int(row["chunk_id"]),
        document_id=int(row["document_id"]),
        content=str(row["content"]),
        token_count=int(row["token_count"]),
        page_number=row["page_number"],
        section_title=row["section_title"],
        problem_number=row["problem_number"],
        part_index=row["part_index"],
        filename=str(row["filename"]),
        similarity=similarity,
        score=score,
    )


def _recency_factor(created_at: object, now: datetime) -> float:
    """Decay linearly from 1.0 for a document uploaded today to 0.0 at the horizon."""
    created = _parse_timestamp(created_at)
    if created is None:
        return 0.0
    age_days = (now - created).total_seconds() / _SECONDS_PER_DAY
    return max(0.0, min(1.0, 1.0 - age_days / RECENCY_HORIZON_DAYS))


def _parse_timestamp(value: object) -> datetime | None:
    """Read a `datetime('now')` timestamp, which SQLite writes as naive UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _fit_to_budget(
    chunks: list[RetrievedChunk], budget_tokens: int
) -> tuple[list[RetrievedChunk], list[RetrievedChunk]]:
    """Split the ranked chunks into what fits and what the budget drops.

    Dropping is lowest-score-first, so what survives is the highest-scoring run from the
    top of the ranking.
    """
    used = 0
    for index, chunk in enumerate(chunks):
        cost = estimate_tokens(chunk.content)
        if used + cost > budget_tokens:
            return chunks[:index], chunks[index:]
        used += cost
    return chunks, []


def _expand_problem_parts(
    conn: sqlite3.Connection, kept: list[RetrievedChunk], budget_tokens: int
) -> list[RetrievedChunk]:
    """Reassemble split homework problems around the parts that matched.

    An oversized problem is chunked into parts sharing one `problem_number`, so a match
    on part (b) alone answers half a question. The remaining parts are pulled in where
    the budget still has room, and the problem is emitted in part order. A sibling
    inherits the matched part's score: it arrives by reassembly, not by the KNN.

    Two things bound what counts as a sibling, and neither used to.

    A chunk whose `part_index` is null is a whole problem. `_number_parts` in `rag/chunk.py`
    leaves the index null exactly when a problem fit in one chunk, so there is nothing to
    reassemble and no query worth running.

    A problem number is not unique within a document. The chunker has grouped parts by
    consecutive run since Phase 2, precisely because a sheet that restarts numbering under
    each section heading has several problem 1s, and this did not: it took every chunk in
    the document carrying the same number. On a 608-page textbook read as homework, which
    is what `detect_doc_type` does with one today, `problem_number = '2'` covers 120 chunks
    spread over the whole book, and one KNN hit on any of them emitted all 120 ahead of the
    second-ranked result, every one carrying the first one's score. The ranking was gone.
    """
    if not any(chunk.part_index is not None for chunk in kept):
        return kept

    used = sum(estimate_tokens(chunk.content) for chunk in kept)
    budgeted = {chunk.chunk_id: chunk for chunk in kept}
    expanded: list[RetrievedChunk] = []
    emitted: set[int] = set()

    for chunk in kept:
        if chunk.chunk_id in emitted:
            continue
        if chunk.part_index is None:
            expanded.append(chunk)
            emitted.add(chunk.chunk_id)
            continue

        rows = conn.execute(_PROBLEM_SQL, (chunk.document_id, chunk.problem_number)).fetchall()
        for row in _sibling_run(rows, chunk.chunk_id):
            part_id = int(row["chunk_id"])
            if part_id in emitted:
                continue
            part = budgeted.get(part_id)
            if part is None:
                cost = estimate_tokens(str(row["content"]))
                if used + cost > budget_tokens:
                    continue
                used += cost
                part = _chunk_from_row(row, chunk.similarity, chunk.score)
            expanded.append(part)
            emitted.add(part_id)

    return expanded


def _sibling_run(rows: list[sqlite3.Row], chunk_id: int) -> list[sqlite3.Row]:
    """The one run of parts the matched chunk belongs to.

    `_number_parts` numbers every run from zero, so a repeated `part_index` is where one
    problem's parts end and the next problem carrying the same number begins. That is the
    only reliable split: the rows are ordered by part index rather than by id, and a run's
    ids are contiguous in a real ingest but not in a fixture that inserts them out of order.

    Returns:
        The run containing `chunk_id`, in part order, or an empty list when no row carries
        that id.
    """
    runs: list[list[sqlite3.Row]] = []
    current: list[sqlite3.Row] = []
    seen: set[int] = set()
    for row in rows:
        index = int(row["part_index"] or 0)
        if index in seen:
            runs.append(current)
            current, seen = [], set()
        current.append(row)
        seen.add(index)
    if current:
        runs.append(current)

    run = next((run for run in runs if any(int(r["chunk_id"]) == chunk_id for r in run)), [])
    # Back into part order, so a problem still reads (a), (b), (c) however its rows were
    # written. The rows arrived in document order because that is what separates the runs.
    return sorted(run, key=lambda row: (int(row["part_index"] or 0), int(row["chunk_id"])))
