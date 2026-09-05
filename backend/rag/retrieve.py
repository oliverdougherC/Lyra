"""Retrieval: exact KNN over one class partition, reranking, recency weighting, budgeting.

Stage 6 of docs/rag-pipeline.md. The search is exact brute-force over the class
partition rather than approximate, because sqlite-vec has no ANN index and at a few
thousand chunks per class it does not need one.

Where a reranker is installed the KNN is run wider than the served `k` and a cross-encoder
picks the `k` that are returned. See `rag/rerank.py` for why the second model exists; in
short, the embedder has to summarise a passage before it has seen the question, and a
class is full of documents that only differ once you have.

The search is hybrid. Beside the vectors, an FTS5 index over the same chunks (migration
015) ranks by BM25, and reciprocal rank fusion merges the two rankings before any
reranking. The case that forced it: a problem set and its answer key restate every
question verbatim, so the embedder cannot tell them apart and the key sat outside the top
128 neighbours, beyond any reordering. The words being identical is the textbook case for
lexical matching. Measured, the key's question goes from absent in the top 128 to rank 4
reranked; docs/rag-pipeline.md, stage 6 records the run.

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
from backend.rag.chunk import SOLUTIONS
from backend.rag.embed import embed_query
from backend.rag.rerank import RerankStatus, rerank
from backend.rag.tokens import estimate_tokens

K = 8

# How many neighbours the KNN returns. The served width stays `K`; this is how much
# material the fusion, and the cross-encoder where one is installed, gets to choose from.
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

# How many chunk ids the lexical pass returns, in BM25 order. The same width the reranker
# already reads: the lexical ranking is the fusion's second input, and a narrower list
# would decide the fused ranking before it was computed.
LEXICAL_FETCH_K = 64

# The reciprocal-rank-fusion constant. 60 is the value the original RRF paper
# (Cormack, Clarke, Buettcher 2009) measured as robust across collections, and it has
# been the default everywhere since: large enough that a mid-list rank still contributes,
# small enough that the head of a list dominates its tail.
RRF_K = 60

# Half of a single list's top-rank contribution, so it breaks ties between comparable fused
# scores without promoting a weak match: the same philosophy as RECENCY_COEFFICIENT. It was
# provisional until measured on the answer-key case, and the measurement kept it: with the
# bonus zeroed, `hw5-two-sided-exponential-answer` falls out of the candidate sixty-four and
# no other rank moves, so the bonus is exactly what carries an answer key past its problem
# set. Recorded in docs/rag-pipeline.md, stage 6.
SOLUTIONS_RRF_BONUS = 1.0 / (2 * (RRF_K + 1))

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


_CHUNK_COLUMNS = """c.id as chunk_id, c.document_id, c.content, c.token_count, c.page_number,
       c.section_title, c.section_path, c.section_number, c.problem_number, c.part_index,
       d.filename, d.created_at"""

_CHUNK_SELECT = f"""
select {_CHUNK_COLUMNS}
from chunks c
join documents d on d.id = c.document_id
"""  # noqa: S608 - interpolates only the module's own column list

# Every retrieval path filters on `d.state = 'ready'`, and none may drop it. Ingestion
# commits chunk batches as it goes, so a failure partway through embedding leaves a
# document whose chunks are real rows but whose text is only half indexed. Its state says
# so - `ready` is the one terminal state that means "searchable" (see the state machine in
# `backend/core/ingestion.py`) - and serving anything else answers a question from half a
# document with no symptom anywhere. Ingestion also cleans those chunks up on failure;
# this filter is the belt to that suspender, and both are intended.
_READY_ONLY = "d.state = 'ready'"

# The same join with the query's cosine distance selected as a column, for the paths that
# score rows with SQL rather than through the vec0 KNN. The first placeholder is always
# the serialized query vector. Selecting the distance here is what lets those paths read
# each row's similarity off the row instead of re-asking per chunk, which used to be an
# N+1 against the embeddings table.
_SCORED_CHUNK_SELECT = f"""
select {_CHUNK_COLUMNS},
       vec_distance_cosine(e.embedding, ?) as distance
from chunks c
join documents d on d.id = c.document_id
join chunk_embeddings e on e.chunk_id = c.id
"""  # noqa: S608 - interpolates only the module's own column list

# Chunks of one numbered section, and of everything nested under it, so asking for section
# 2.2 also reaches 2.2.1. Scored against the query rather than taken in document order:
# a section reference says where to look, and within a long section the part that answers
# the question should still outrank the part that does not.
_SECTION_SQL = (
    _SCORED_CHUNK_SELECT + f"where c.class_id = ? and {_READY_ONLY}\n"
    "  and (c.section_number = ? or c.section_number like ?)\n"
)

# Ordered by id, which is document order, because that is what makes one problem's parts
# contiguous and so lets `_sibling_run` tell two problems carrying the same number apart.
# The run is put back into part order before it is emitted.
_PROBLEM_SQL = (
    _CHUNK_SELECT
    + f"where c.document_id = ? and c.problem_number = ? and {_READY_ONLY}\norder by c.id"
)

# The lexical pass. The same ready-only rule as every other path (see `_READY_ONLY`), and
# the same class partition. Ordering and limiting are appended by the caller, because a
# document pin slots between the filters and them.
_LEXICAL_SQL = f"""
select c.id
from chunks_fts
join chunks c on c.id = chunks_fts.rowid
join documents d on d.id = c.document_id
where chunks_fts match ? and c.class_id = ? and {_READY_ONLY}
"""  # noqa: S608 - interpolates only the module's own ready-only filter


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
        omitted_document_ids: The same documents by id, retained so a caller performing
            another exact-fit pass can union omissions without double-counting them.
        rerank_status: What actually happened at the rerank boundary. ``APPLIED``
            when the cross-encoder ran and its scores were used; ``NOT_REQUESTED``
            when the caller did not ask for reranking; one of the failure statuses
            when reranking was attempted but fell back to the embedding order.
    """

    chunks: list[RetrievedChunk]
    trimmed: bool
    omitted_document_count: int
    omitted_document_ids: frozenset[int] = frozenset()
    rerank_status: RerankStatus = RerankStatus.NOT_REQUESTED


def retrieve(
    conn: sqlite3.Connection,
    class_id: int,
    query: str,
    budget_tokens: int,
    document_id: int | None = None,
    *,
    document_ids: tuple[int, ...] | None = None,
) -> RetrievalResult:
    """Find the chunks of one class that best answer `query` and fit the budget.

    Args:
        conn: Open database connection.
        class_id: Class partition to search. Retrieval never crosses classes.
        query: The student's question, embedded here with the query task prefix.
        budget_tokens: Retrieval share of the context window, measured with
            `estimate_tokens`.
        document_id: Restrict the result to one document. The vector search then runs
            over that document's own chunks, so the document's best answers come back
            rather than whatever slice of it survived a class-wide search.
        document_ids: Restrict every candidate path to this selected set before ranking.
            An empty set returns no context; with document_id, the intersection is used.

    Returns:
        The ranked chunks that fit, and the trim reporting for the ones that did not.
        An empty result is normal, not an error.
    """
    if document_ids is not None:
        document_ids = tuple(sorted(set(document_ids)))
        if document_id is not None:
            document_ids = tuple(value for value in document_ids if value == document_id)
        if not document_ids:
            return RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)
    elif document_id is not None:
        document_ids = (document_id,)
    vector = embed_query(query)
    resolved = _resolve_sections(
        conn, class_id, query, vector, budget_tokens, document_id, document_ids
    )

    # The KNN always fetches wide now: under fusion the surplus is the material the fused
    # ranking is computed over, and only the fused top-`K` is served, so none of it falls
    # into the budget unranked. The old no-reranker narrow fetch existed to keep the
    # surplus out of the budget, and the fused cut keeps it out instead.
    reranking = rerank_server.available
    lexical = _lexical_ranks(conn, class_id, query, LEXICAL_FETCH_K, document_id, document_ids)
    if document_ids is not None:
        vector_chunks = _document_candidates(conn, class_id, document_ids, vector, RERANK_FETCH_K)
    else:
        distances = _knn(conn, class_id, vector, RERANK_FETCH_K)
        vector_chunks = _load_candidates(conn, distances) if distances else []
    candidates = _fuse(conn, vector_chunks, lexical, vector)
    if reranking:
        candidates, rerank_status = _reranked(query, candidates[:RERANK_FETCH_K])
    else:
        candidates = candidates[:K]
        rerank_status = RerankStatus.NOT_REQUESTED
    if not candidates and not resolved:
        return RetrievalResult(
            chunks=[],
            trimmed=False,
            omitted_document_count=0,
            rerank_status=rerank_status,
        )

    # A section that was asked for by name goes in front of the similarity ranking rather
    # than competing with it, because naming a section is a fact about where the answer is
    # and cosine distance is a guess at it. What the KNN found still fills the rest.
    already = {chunk.chunk_id for chunk in resolved}
    remaining = budget_tokens - sum(estimate_tokens(chunk.content) for chunk in resolved)
    kept, dropped = _fit_to_budget(
        [chunk for chunk in candidates if chunk.chunk_id not in already], remaining
    )

    omitted_document_ids = frozenset(chunk.document_id for chunk in dropped)
    return RetrievalResult(
        chunks=resolved + _expand_problem_parts(conn, kept, remaining),
        # Reported over the similarity ranking alone. A chunk that did not fit beside a
        # section the student asked for by name was not omitted for lack of room in the
        # sense this flag means, and counting it would raise the notice on every turn that
        # cites a section. More than half gone means the answer is missing enough material
        # that the user deserves to be told, per Stage 7 of docs/rag-pipeline.md.
        trimmed=bool(candidates) and len(dropped) * 2 > len(candidates),
        omitted_document_count=len(omitted_document_ids),
        omitted_document_ids=omitted_document_ids,
        rerank_status=rerank_status,
    )


def _resolve_sections(
    conn: sqlite3.Connection,
    class_id: int,
    query: str,
    vector: list[float],
    budget_tokens: int,
    document_id: int | None,
    document_ids: tuple[int, ...] | None = None,
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
    # Uppercased before the SQL, because the two sides of the lookup disagree about case
    # and split the result down the middle: stored section numbers are always uppercase
    # (the heading regexes only accept `[A-Z]`), `SECTION_REFERENCE` matches
    # case-insensitively, and SQLite's `=` is case-sensitive where its `like` is not. A
    # student who typed `section a.2` therefore got A.2's subsections (via the `like`)
    # and not the section's own chunks (via the `=`), which is a stranger failure than
    # either everything or nothing.
    numbers = {match.group(1).upper() for match in SECTION_REFERENCE.finditer(query)}
    if not numbers:
        return []

    share = int(budget_tokens * STRUCTURAL_BUDGET_SHARE)
    if share <= 0:
        return []

    now = datetime.now(UTC)
    found: dict[int, RetrievedChunk] = {}
    serialized = sqlite_vec.serialize_float32(vector)
    for number in sorted(numbers):
        sql = _SECTION_SQL
        parameters: list[object] = [serialized, class_id, number, f"{number}.%"]
        if document_ids is not None:
            sql += " and c.document_id in (" + ",".join("?" for _ in document_ids) + ")"
            parameters.extend(document_ids)
        elif document_id is not None:
            sql += " and c.document_id = ?"
            parameters.append(document_id)
        # Ordered by distance so the part of a long section that answers the question
        # survives the budget, then put back into reading order below.
        sql += " order by distance, c.id"

        for row in conn.execute(sql, parameters):
            chunk_id = int(row["chunk_id"])
            if chunk_id in found:
                continue
            # The distance came back as a column of this very row, so the similarity is
            # read off it rather than re-asked of the embeddings table one chunk at a
            # time, which is what this loop used to do.
            similarity = 1.0 - float(row["distance"])
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


def _knn(
    conn: sqlite3.Connection, class_id: int, vector: list[float], limit: int
) -> dict[int, float]:
    """Run the partitioned KNN and return distance by chunk id."""
    rows = conn.execute(
        _knn_sql(limit), (sqlite_vec.serialize_float32(vector), class_id)
    ).fetchall()
    return {int(row["chunk_id"]): float(row["distance"]) for row in rows}


def _document_candidates(
    conn: sqlite3.Connection,
    class_id: int,
    document_ids: tuple[int, ...],
    vector: list[float],
    limit: int,
) -> list[RetrievedChunk]:
    """The pinned document's own nearest chunks, scored the way `_load_candidates` scores.

    A manual `vec_distance_cosine` scan rather than the vec0 KNN, because the KNN
    partitions by class and cannot be told about a document; scanning one document's
    chunks is far smaller work than the class-wide search anyway. The distance arrives as
    a column, so similarity is read straight off each row.
    """
    now = datetime.now(UTC)
    chunks: list[RetrievedChunk] = []
    placeholders = ",".join("?" for _ in document_ids)
    sql = (
        _SCORED_CHUNK_SELECT
        + f"where c.class_id = ? and c.document_id in ({placeholders}) and {_READY_ONLY} "
        + "order by distance, c.id limit ?"
    )  # noqa: S608 - placeholders and static clauses only
    rows = conn.execute(sql, (sqlite_vec.serialize_float32(vector), class_id, *document_ids, limit))
    for row in rows:
        similarity = 1.0 - float(row["distance"])
        score = similarity + RECENCY_COEFFICIENT * _recency_factor(row["created_at"], now)
        chunks.append(_chunk_from_row(row, similarity, score))

    # The SQL ordered by distance to apply `limit`; the served order still includes the
    # recency bonus, exactly as the class-wide path ranks its candidates.
    chunks.sort(key=lambda chunk: (-chunk.score, chunk.chunk_id))
    return chunks


def _load_candidates(conn: sqlite3.Connection, distances: dict[int, float]) -> list[RetrievedChunk]:
    """Join the neighbours to their content and document metadata, then rank them."""
    placeholders = ", ".join("?" * len(distances))
    sql = f"{_CHUNK_SELECT}where c.id in ({placeholders}) and {_READY_ONLY}"  # noqa: S608
    parameters: list[object] = list(distances)

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


def _fts_terms(query: str) -> list[str]:
    """The query as quoted FTS5 terms, one per whitespace-separated word.

    Quoting every word makes FTS5's operator words (`AND`, `OR`, `NEAR`) and its
    parenthesis syntax literals, so a student who types one gets a search for the word
    they typed rather than a syntax error or a silently different query. Double quotes
    inside a word are stripped first, because one would close the quoting early.
    """
    terms = []
    for word in query.split():
        stripped = word.replace('"', "")
        if stripped:
            terms.append(f'"{stripped}"')
    return terms


def _lexical_ranks(
    conn: sqlite3.Connection,
    class_id: int,
    query: str,
    limit: int,
    document_id: int | None = None,
    document_ids: tuple[int, ...] | None = None,
) -> list[int]:
    """Chunk ids in BM25 order: the lexical ranking that fusion merges with the KNN's.

    All terms must match at first; when a multi-term query has no chunk containing every
    term, the retry joins them with OR, so one absent word does not zero the result. BM25
    still ranks a chunk matching more terms above one matching fewer, so the retry widens
    the candidate set without flattening its order.
    """
    terms = _fts_terms(query)
    if not terms:
        return []
    rows = _lexical_query(conn, class_id, " ".join(terms), limit, document_id, document_ids)
    if not rows and len(terms) >= 2:
        rows = _lexical_query(conn, class_id, " OR ".join(terms), limit, document_id, document_ids)
    return rows


def _lexical_query(
    conn: sqlite3.Connection,
    class_id: int,
    match: str,
    limit: int,
    document_id: int | None,
    document_ids: tuple[int, ...] | None = None,
) -> list[int]:
    """One FTS5 match against the class partition, best (most negative) BM25 first."""
    sql = _LEXICAL_SQL
    parameters: list[object] = [match, class_id]
    if document_ids is not None:
        sql += " and c.document_id in (" + ",".join("?" for _ in document_ids) + ")"
        parameters.extend(document_ids)
    elif document_id is not None:
        sql += " and c.document_id = ?"
        parameters.append(document_id)
    sql += " order by bm25(chunks_fts), c.id limit ?"
    parameters.append(limit)
    return [int(row["id"]) for row in conn.execute(sql, parameters)]


def _fuse(
    conn: sqlite3.Connection,
    vector_chunks: list[RetrievedChunk],
    lexical_ids: list[int],
    vector: list[float],
) -> list[RetrievedChunk]:
    """Reciprocal rank fusion of the vector ranking with the lexical one.

    Each list contributes `1 / (RRF_K + rank)` per chunk, so a chunk near the top of both
    lists outranks a chunk at the top of only one, and neither list's score scale matters
    because only ranks are read. The vector list arrives already ranked by `score`
    (similarity plus recency), so recency keeps its tie-breaking effect through the
    fusion.

    Chunks the lexical pass found beyond the vector over-fetch are loaded through the
    scored select, so every fused candidate carries the embedder's real cosine in
    `similarity`, as the `RetrievedChunk` contract requires; the fused value lives in
    `score`, which is only ever a ranking key. `doc_type = 'solutions'` chunks then
    receive `SOLUTIONS_RRF_BONUS`, the nudge toward answer keys that the roadmap's
    hybrid-retrieval item names; §1.4 of the handoff decides whether it stays.
    """
    chunks = {chunk.chunk_id: chunk for chunk in vector_chunks}
    missing = [chunk_id for chunk_id in lexical_ids if chunk_id not in chunks]
    if missing:
        placeholders = ", ".join("?" * len(missing))
        sql = f"{_SCORED_CHUNK_SELECT}where c.id in ({placeholders}) and {_READY_ONLY}"  # noqa: S608
        now = datetime.now(UTC)
        rows = conn.execute(sql, (sqlite_vec.serialize_float32(vector), *missing))
        for row in rows:
            similarity = 1.0 - float(row["distance"])
            # The recency bonus is computed here for parity with the vector candidates,
            # then discarded: every candidate's `score` is set to its fused value below.
            chunks[int(row["chunk_id"])] = _chunk_from_row(
                row,
                similarity,
                similarity + RECENCY_COEFFICIENT * _recency_factor(row["created_at"], now),
            )

    fused: dict[int, float] = {}
    for rank, chunk in enumerate(vector_chunks, start=1):
        fused[chunk.chunk_id] = fused.get(chunk.chunk_id, 0.0) + 1.0 / (RRF_K + rank)
    for rank, chunk_id in enumerate(lexical_ids, start=1):
        fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)

    if fused:
        placeholders = ", ".join("?" * len(fused))
        rows = conn.execute(
            f"select id from chunks where id in ({placeholders}) and doc_type = ?",  # noqa: S608
            [*fused, SOLUTIONS],
        )
        for row in rows:
            fused[int(row["id"])] += SOLUTIONS_RRF_BONUS

    # Ties break by vector rank, then chunk id. The handoff specified chunk id alone; the
    # vector rank goes first because an exact fused tie between an older and a newer chunk
    # would otherwise discard the recency ordering the vector list carries (chunk id
    # ascending is oldest-first, the anti-recency order). Determinism is unaffected:
    # vector ranks are unique, and chunk id remains the final tiebreak for chunks only the
    # lexical pass found.
    vector_rank = {chunk.chunk_id: rank for rank, chunk in enumerate(vector_chunks, start=1)}
    fallback = len(vector_chunks) + 1
    ranked = sorted(
        chunks.values(),
        key=lambda chunk: (
            -fused[chunk.chunk_id],
            vector_rank.get(chunk.chunk_id, fallback),
            chunk.chunk_id,
        ),
    )
    return [RetrievedChunk(**{**vars(chunk), "score": fused[chunk.chunk_id]}) for chunk in ranked]


def _reranked(
    query: str, candidates: list[RetrievedChunk]
) -> tuple[list[RetrievedChunk], RerankStatus]:
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

    Returns:
        The ranked chunks and the rerank status that says what actually happened.
    """
    outcome = rerank(query, [chunk.content for chunk in candidates])
    if outcome.scores is None:
        return candidates[:K], outcome.status

    rescored = [
        RetrievedChunk(**{**vars(chunk), "score": score})
        for chunk, score in zip(candidates, outcome.scores, strict=True)
    ]
    rescored.sort(key=lambda chunk: (-chunk.score, chunk.chunk_id))
    return rescored[:K], outcome.status


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
