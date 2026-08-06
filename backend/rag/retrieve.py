"""Retrieval: exact KNN over one class partition, reranking, recency weighting, budgeting.

Stage 6 of docs/rag-pipeline.md. The search is exact brute-force over the class
partition rather than approximate, because sqlite-vec has no ANN index and at a few
thousand chunks per class it does not need one.

Where a reranker is installed the KNN is run wider than the served `k` and a cross-encoder
picks the `k` that are returned. See `rag/rerank.py` for why the second model exists; in
short, the embedder has to summarise a passage before it has seen the question, and a
class is full of documents that only differ once you have.

Zero chunks is a valid result. A class with nothing indexed, or a question no chunk
answers, produces an empty `RetrievalResult` and the turn is built with no context
block. It is not an error and must not be reported as one.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlite_vec

from backend.llm.rerank_server import rerank_server
from backend.rag.embed import embed_query
from backend.rag.rerank import rerank
from backend.rag.tokens import estimate_tokens

K = 8

# How many neighbours the KNN returns when a reranker is going to read them. The served
# width stays `K`; this is only how much material the cross-encoder gets to choose from.
#
# Eight times `K`, chosen by measurement rather than by taste. On a real 36-document course
# (scripts/eval_questions/ece203-class.json), of sixteen questions whose answer is a known
# page of a known document:
#
#   fetch 8, no reranker    9/16 first, 14/16 in the served eight, 0.02s a question
#   fetch 32, reranked     12/16 first, 14/16 in the served eight, 0.84s
#   fetch 64, reranked     12/16 first, 15/16 in the served eight, 1.58s
#
# Thirty-two was not enough because a real answer sat at rank 35 in the embedding order:
# a reranker can only reorder what it is given. Doubling again is where the cost stops
# buying anything, and 1.6 seconds is small beside the model turn it feeds.
RERANK_FETCH_K = 64

# A query naming a part of a document: `section 4.11`, `Chapter 7`, `§5.2`, `section A.2`.
# The number is the thing being looked up, so a reference with no number is not one.
SECTION_REFERENCE = re.compile(
    r"\b(?:section|chapter|part|§)\s*\.?\s*([A-Za-z]?\.?\d+(?:\.\d+)*)\b",
    re.IGNORECASE,
)

# How much of the retrieval budget one resolved section may take. A section reference says
# where to look, not what is wanted from it, so the KNN keeps at least half the room to
# answer the second question. Without the cap a forty-chunk chapter would fill the context
# on its own and the student's actual question would arrive with nothing else beside it.
STRUCTURAL_BUDGET_SHARE = 0.5

# The bonus is deliberately smaller than any meaningful similarity gap: it breaks ties
# between comparable matches, it does not promote a weak match over a strong one.
RECENCY_COEFFICIENT = 0.05
RECENCY_HORIZON_DAYS = 120

_SECONDS_PER_DAY = 86400.0


# `k` is inlined rather than bound, because sqlite-vec reads the KNN limit out of the query
# text. Built per call rather than once, because the limit is `K` or `RERANK_FETCH_K`
# depending on whether a reranker is going to read the result.
def _knn_sql(limit: int) -> str:
    return f"""
select chunk_id, distance
from chunk_embeddings
where embedding match ? and k = {int(limit)} and class_id = ?
"""  # noqa: S608


_CHUNK_SELECT = """
select c.id as chunk_id, c.document_id, c.content, c.token_count, c.page_number,
       c.section_title, c.section_path, c.section_number, c.problem_number, c.part_index,
       d.filename, d.created_at
from chunks c
join documents d on d.id = c.document_id
"""

# Chunks of one numbered section, and of everything nested under it, so asking for section
# 2.2 also reaches 2.2.1. Scored against the query rather than taken in document order:
# a section reference says where to look, and within a long section the part that answers
# the question should still outrank the part that does not.
_SECTION_SQL = (
    _CHUNK_SELECT + "join chunk_embeddings e on e.chunk_id = c.id\n"
    "where c.class_id = ? and (c.section_number = ? or c.section_number like ?)\n"
)

# Ordered by id, which is document order, because that is what makes one problem's parts
# contiguous and so lets `_sibling_run` tell two problems carrying the same number apart.
# The run is put back into part order before it is emitted.
_PROBLEM_SQL = _CHUNK_SELECT + "where c.document_id = ? and c.problem_number = ?\norder by c.id"


@dataclass(frozen=True)
class RetrievedChunk:
    """One chunk the KNN returned, joined to its document and scored.

    Attributes:
        similarity: Cosine similarity to the query, in [0, 1]. Always the embedder's
            measurement, whether or not a reranker ran.
        score: The ranking key, and only that. `similarity` plus the recency bonus
            ordinarily; the cross-encoder's logit where reranking ran, which is unbounded,
            routinely negative, and comparable only against other scores from the same
            query. Never show it, and never compare it against `similarity`.
    """

    chunk_id: int
    document_id: int
    content: str
    token_count: int
    page_number: int | None
    section_title: str | None
    section_path: str | None
    section_number: str | None
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
    vector = embed_query(query)
    resolved = _resolve_sections(conn, class_id, query, vector, budget_tokens, document_id)

    # Fetch wide only when something is going to read the extra. Without a reranker the
    # surplus would go straight into `_fit_to_budget`, which is a different product: the
    # turn would be built from thirty-two chunks the search was never confident about.
    reranking = rerank_server.available
    distances = _knn(conn, class_id, vector, RERANK_FETCH_K if reranking else K)
    candidates = _load_candidates(conn, distances, document_id) if distances else []
    if reranking:
        candidates = _reranked(query, candidates)
    if not candidates and not resolved:
        return RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)

    # A section that was asked for by name goes in front of the similarity ranking rather
    # than competing with it, because naming a section is a fact about where the answer is
    # and cosine distance is a guess at it. What the KNN found still fills the rest.
    already = {chunk.chunk_id for chunk in resolved}
    remaining = budget_tokens - sum(estimate_tokens(chunk.content) for chunk in resolved)
    kept, dropped = _fit_to_budget(
        [chunk for chunk in candidates if chunk.chunk_id not in already], remaining
    )

    return RetrievalResult(
        chunks=resolved + _expand_problem_parts(conn, kept, remaining),
        # Reported over the similarity ranking alone. A chunk that did not fit beside a
        # section the student asked for by name was not omitted for lack of room in the
        # sense this flag means, and counting it would raise the notice on every turn that
        # cites a section. More than half gone means the answer is missing enough material
        # that the user deserves to be told, per Stage 7 of docs/rag-pipeline.md.
        trimmed=bool(candidates) and len(dropped) * 2 > len(candidates),
        omitted_document_count=len({chunk.document_id for chunk in dropped}),
    )


def _resolve_sections(
    conn: sqlite3.Connection,
    class_id: int,
    query: str,
    vector: list[float],
    budget_tokens: int,
    document_id: int | None,
) -> list[RetrievedChunk]:
    """Chunks of any section the query names outright, in reading order.

    A question that says "use the result from section 4.11" is not a similarity problem,
    and treating it as one is what the measurement said it costs: asked bare, `What does
    section 2.2 cover?` came back at rank 12 and `Summarize what section 4.9 is about` at
    rank 23, both scoring below questions about material the book does not contain at all.
    A section number is a fact printed on the page, so it is looked up.

    Silent on a miss, deliberately. A student may cite a section of a book they never
    uploaded, or a course may number its weeks, and the similarity search is a perfectly
    good answer to both. A reference that resolves to nothing costs one query.

    Returns:
        The best of the named section within its share of the budget, ordered by page so
        the section still reads forwards. Empty when the query names nothing, when nothing
        matches, or when no chunk has been indexed with a section number.
    """
    numbers = {match.group(1) for match in SECTION_REFERENCE.finditer(query)}
    if not numbers:
        return []

    share = int(budget_tokens * STRUCTURAL_BUDGET_SHARE)
    if share <= 0:
        return []

    now = datetime.now(UTC)
    found: dict[int, RetrievedChunk] = {}
    for number in sorted(numbers):
        sql = _SECTION_SQL
        parameters: list[object] = [class_id, number, f"{number}.%"]
        if document_id is not None:
            sql += " and c.document_id = ?"
            parameters.append(document_id)
        # Ordered by distance so the part of a long section that answers the question
        # survives the budget, then put back into reading order below.
        sql += " order by vec_distance_cosine(e.embedding, ?), c.id"
        parameters.append(sqlite_vec.serialize_float32(vector))

        for row in conn.execute(sql, parameters):
            chunk_id = int(row["chunk_id"])
            if chunk_id in found:
                continue
            similarity = _similarity_to(conn, chunk_id, vector)
            found[chunk_id] = _chunk_from_row(
                row,
                similarity,
                similarity + RECENCY_COEFFICIENT * _recency_factor(row["created_at"], now),
            )

    ranked = sorted(found.values(), key=lambda chunk: (-chunk.score, chunk.chunk_id))
    kept, _ = _fit_to_budget(ranked, share)
    # Reading order for the prompt: a section quoted out of order is harder to follow than
    # one quoted short.
    return sorted(kept, key=lambda chunk: (chunk.page_number or 0, chunk.chunk_id))


def _similarity_to(conn: sqlite3.Connection, chunk_id: int, vector: list[float]) -> float:
    """Cosine similarity between one stored chunk and the query.

    Read back rather than carried out of the ordering query, because sqlite-vec exposes
    the distance to `order by` but not as a column of the row it returns.
    """
    row = conn.execute(
        "select vec_distance_cosine(embedding, ?) as distance "
        "from chunk_embeddings where chunk_id = ?",
        (sqlite_vec.serialize_float32(vector), chunk_id),
    ).fetchone()
    return 1.0 - float(row["distance"]) if row is not None else 0.0


def _knn(
    conn: sqlite3.Connection, class_id: int, vector: list[float], limit: int
) -> dict[int, float]:
    """Run the partitioned KNN and return distance by chunk id."""
    rows = conn.execute(
        _knn_sql(limit), (sqlite_vec.serialize_float32(vector), class_id)
    ).fetchall()
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


def _reranked(query: str, candidates: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Reorder the over-fetch by cross-encoder score and cut it back to `K`.

    `similarity` is left exactly as the embedder measured it, because that is what it
    means and what the interface reports. Only `score` - the ranking key - is replaced,
    and the recency bonus is not carried over: it exists to break ties between matches the
    embedder could not separate, and a model that has read both passages has separated
    them. Adding it back would let a document uploaded this week outrank a better answer
    the reranker was confident about.

    Cutting to `K` here rather than leaving it to the budget is deliberate. The over-fetch
    is a shortlist, not more context: everything past `K` is material the search was not
    confident about and the reranker did not rescue.
    """
    scores = rerank(query, [chunk.content for chunk in candidates])
    if scores is None:
        return candidates[:K]

    rescored = [
        RetrievedChunk(**{**vars(chunk), "score": score})
        for chunk, score in zip(candidates, scores, strict=True)
    ]
    rescored.sort(key=lambda chunk: (-chunk.score, chunk.chunk_id))
    return rescored[:K]


def _chunk_from_row(row: sqlite3.Row, similarity: float, score: float) -> RetrievedChunk:
    """Build a chunk from a `_CHUNK_SELECT` row and its already-computed ranking."""
    return RetrievedChunk(
        chunk_id=int(row["chunk_id"]),
        document_id=int(row["document_id"]),
        content=str(row["content"]),
        token_count=int(row["token_count"]),
        page_number=row["page_number"],
        section_title=row["section_title"],
        section_path=row["section_path"],
        section_number=row["section_number"],
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
