"""Contract tests for retrieval: ranking, recency weighting, scoping, and budgeting.

The embedding server is never started. `embed_query` is replaced with a fixed vector and
the stored vectors are crafted so every cosine distance in these tests is one the test
chose, which is what makes the ordering assertions meaningful rather than incidental.
"""

import math
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import sqlite_vec

from backend.rag import retrieve as retrieve_module
from backend.rag.retrieve import RetrievalResult, retrieve
from backend.rag.tokens import estimate_tokens

DIMENSIONS = 768

# Every vector lies in the plane of the first two axes, so a chunk stored at `angle`
# radians has cosine similarity cos(angle) to the query at angle zero.
_QUERY_ANGLE = 0.0


def _vector(angle: float) -> list[float]:
    """A unit vector `angle` radians away from the query direction."""
    values = [0.0] * DIMENSIONS
    values[0] = math.cos(angle)
    values[1] = math.sin(angle)
    return values


def _days_ago(days: float) -> str:
    """A timestamp in the format SQLite's `datetime('now')` writes."""
    moment = datetime.now(UTC) - timedelta(days=days)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _insert_document(
    db: sqlite3.Connection,
    class_id: int,
    filename: str,
    created_at: str,
    state: str = "ready",
) -> int:
    """A document with an explicit upload time, so recency is under test control.

    `ready` unless a test says otherwise: only a ready document may serve chunks, and the
    tests for that rule are the ones that pass another state.
    """
    cursor = db.execute(
        "insert into documents "
        "(class_id, filename, stored_path, mime, byte_size, state, created_at) "
        "values (?, ?, ?, 'application/pdf', 2048, ?, ?)",
        (class_id, filename, f"uploads/{class_id}/{filename}", state, created_at),
    )
    db.commit()
    return int(cursor.lastrowid or 0)


def _insert_chunk(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    content: str,
    angle: float,
    problem_number: str | None = None,
    part_index: int | None = None,
) -> int:
    """One chunk and its embedding, placed at a chosen angle from the query."""
    cursor = db.execute(
        "insert into chunks "
        "(document_id, class_id, content, token_count, page_number, section_title, "
        "problem_number, part_index, doc_type, embedding_model, embedding_dim) "
        "values (?, ?, ?, ?, 2, 'Derivatives', ?, ?, 'generic', 'nomic-embed-text-v1.5.Q8_0', ?)",
        (
            document_id,
            class_id,
            content,
            estimate_tokens(content),
            problem_number,
            part_index,
            DIMENSIONS,
        ),
    )
    chunk_id = int(cursor.lastrowid or 0)
    db.execute(
        "insert into chunk_embeddings (chunk_id, class_id, embedding) values (?, ?, ?)",
        (chunk_id, class_id, sqlite_vec.serialize_float32(_vector(angle))),
    )
    db.commit()
    return chunk_id


@pytest.fixture(autouse=True)
def fixed_query_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the local embedding server: the query is always the same direction."""
    monkeypatch.setattr(retrieve_module, "embed_query", lambda text: _vector(_QUERY_ANGLE))


def test_equal_similarity_ranks_the_newer_document_first(
    db: sqlite3.Connection, class_id: int
) -> None:
    older = _insert_document(db, class_id, "week-1-notes.pdf", _days_ago(200))
    newer = _insert_document(db, class_id, "week-9-notes.pdf", _days_ago(0))
    # Inserted oldest first, so an implementation that ignored recency would rank it first.
    _insert_chunk(db, class_id, older, "The chain rule, as covered in week one.", 0.0)
    _insert_chunk(db, class_id, newer, "The chain rule, as covered in week nine.", 0.0)

    result = retrieve(db, class_id, "explain the chain rule", 1000)

    assert [chunk.document_id for chunk in result.chunks] == [newer, older]
    assert result.chunks[0].similarity == pytest.approx(result.chunks[1].similarity)
    assert result.chunks[0].score > result.chunks[1].score


def test_recency_does_not_outrank_a_clearly_better_match(
    db: sqlite3.Connection, class_id: int
) -> None:
    stale = _insert_document(db, class_id, "textbook.pdf", _days_ago(200))
    fresh = _insert_document(db, class_id, "today.pdf", _days_ago(0))
    strong = _insert_chunk(db, class_id, stale, "A direct statement of the chain rule.", 0.0)
    _insert_chunk(db, class_id, fresh, "A passing mention of derivatives.", 0.6)

    result = retrieve(db, class_id, "explain the chain rule", 1000)

    # The bonus is 0.05 and the similarity gap here is roughly 0.17, so the older but far
    # better match still wins. That is the whole point of the coefficient being small.
    assert result.chunks[0].chunk_id == strong


def test_a_budget_smaller_than_the_matches_trims_and_counts_omitted_documents(
    db: sqlite3.Connection, class_id: int
) -> None:
    first = _insert_document(db, class_id, "lecture-1.pdf", _days_ago(3))
    second = _insert_document(db, class_id, "lecture-2.pdf", _days_ago(3))
    third = _insert_document(db, class_id, "lecture-3.pdf", _days_ago(3))
    best = "The chain rule differentiates a composition."
    _insert_chunk(db, class_id, first, best, 0.0)
    _insert_chunk(db, class_id, second, "Limits and continuity, with proofs.", 0.3)
    _insert_chunk(db, class_id, third, "Epsilon and delta, worked slowly.", 0.6)
    _insert_chunk(db, class_id, third, "A second pass over epsilon and delta.", 0.9)

    # Exactly one chunk wide, so three of the four retrieved chunks have to go.
    result = retrieve(db, class_id, "explain the chain rule", estimate_tokens(best))

    assert [chunk.document_id for chunk in result.chunks] == [first]
    assert result.trimmed is True
    assert result.omitted_document_count == 2


def test_a_document_scope_returns_only_that_document(db: sqlite3.Connection, class_id: int) -> None:
    syllabus = _insert_document(db, class_id, "syllabus.pdf", _days_ago(30))
    homework = _insert_document(db, class_id, "homework-3.pdf", _days_ago(2))
    _insert_chunk(db, class_id, syllabus, "Grading is 40 percent homework.", 0.0)
    scoped = _insert_chunk(db, class_id, homework, "Problem 3 asks for the derivative.", 0.3)

    result = retrieve(db, class_id, "what is problem 3", 1000, document_id=homework)

    assert [chunk.chunk_id for chunk in result.chunks] == [scoped]
    # A chunk the user scoped out was never omitted for lack of room, so it is not a trim.
    assert result.trimmed is False
    assert result.omitted_document_count == 0


def test_a_scoped_document_serves_its_own_best_chunks_not_the_class_leftovers(
    db: sqlite3.Connection, class_id: int
) -> None:
    """The scope used to be a post-filter on a class-wide KNN of K = 8, so in a class
    where eight chunks of other documents sat closer, "chat about this document" returned
    nothing from the document at all. The vector search has to run over the pinned
    document's own chunks."""
    pinned = _insert_document(db, class_id, "reader.pdf", _days_ago(1))
    noise = _insert_document(db, class_id, "lecture.pdf", _days_ago(1))
    # A full K of closer chunks from another document: the class-wide top eight holds
    # nothing of the pinned document, so a post-filter would come back empty.
    for index in range(retrieve_module.K):
        _insert_chunk(db, class_id, noise, f"Closer class material {index}.", 0.01 * (index + 1))
    second = _insert_document(db, class_id, "other.pdf", _days_ago(1))
    _insert_chunk(db, class_id, second, "Material from a third document.", 0.02)
    wanted = _insert_chunk(db, class_id, pinned, "The pinned document's own answer.", 1.0)
    also = _insert_chunk(db, class_id, pinned, "More of the pinned document.", 1.2)

    result = retrieve(db, class_id, "chat about this document", 100_000, document_id=pinned)

    # The document's own top chunks, in its own similarity order, and nothing else.
    assert [chunk.chunk_id for chunk in result.chunks] == [wanted, also]


@pytest.mark.parametrize("state", ["embedding", "failed"])
def test_chunks_of_a_document_that_is_not_ready_are_never_served(
    db: sqlite3.Connection, class_id: int, state: str
) -> None:
    """A half-indexed document must not answer questions.

    Ingestion commits chunk batches as it goes, so a failure partway through embedding
    leaves real chunk rows behind a document whose state says it is not searchable.
    Serving them answers from half a document with no symptom anywhere. Ingestion also
    cleans those chunks up on failure; this is the retrieval side of the same rule.
    """
    ready = _insert_document(db, class_id, "notes.pdf", _days_ago(1))
    broken = _insert_document(db, class_id, "book.pdf", _days_ago(1), state=state)
    served = _insert_chunk(db, class_id, ready, "The chain rule, properly indexed.", 0.3)
    # The best match in the class, and it must still not be served.
    _insert_chunk(db, class_id, broken, "The chain rule, from a half-indexed book.", 0.0)

    result = retrieve(db, class_id, "explain the chain rule", 1000)

    assert [chunk.chunk_id for chunk in result.chunks] == [served]


def test_a_section_of_a_document_that_is_not_ready_is_not_looked_up(
    db: sqlite3.Connection, class_id: int
) -> None:
    """The section lookup bypasses the KNN, so it needs the state filter of its own."""
    broken = _insert_document(db, class_id, "book.pdf", _days_ago(1), state="failed")
    _insert_section_chunk(db, class_id, broken, "An LU factorization is...", 0.0, "2.2", 110)

    result = retrieve(db, class_id, "What does section 2.2 cover?", 1000)

    assert result.chunks == []


def test_no_matches_is_an_empty_result_rather_than_an_error(
    db: sqlite3.Connection, class_id: int
) -> None:
    result = retrieve(db, class_id, "anything at all", 1000)

    assert result == RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)


def test_a_matched_problem_part_pulls_in_the_rest_of_the_problem(
    db: sqlite3.Connection, class_id: int
) -> None:
    homework = _insert_document(db, class_id, "homework-4.pdf", _days_ago(1))
    noise = _insert_document(db, class_id, "lecture.pdf", _days_ago(1))
    matched = _insert_chunk(
        db,
        class_id,
        homework,
        "Problem 7, part (b): prove the bound you stated.",
        0.0,
        problem_number="7",
        part_index=1,
    )
    # Seven closer chunks push the sibling past k = 8, so it can only arrive by expansion.
    for index in range(7):
        _insert_chunk(db, class_id, noise, f"Related lecture material {index}.", 0.1 * (index + 1))
    sibling = _insert_chunk(
        db,
        class_id,
        homework,
        "Problem 7, part (a): state the bound.",
        1.2,
        problem_number="7",
        part_index=0,
    )

    result = retrieve(db, class_id, "how do I prove the bound", 1000)

    # More chunks than one KNN can return, so the extra one arrived by expansion.
    assert len(result.chunks) == retrieve_module.K + 1
    # The problem is emitted whole, in part order, at the rank its matched part earned.
    assert [chunk.chunk_id for chunk in result.chunks[:2]] == [sibling, matched]
    # The sibling inherits the match's score rather than posing as a match of its own.
    assert result.chunks[0].score == result.chunks[1].score


def test_a_whole_problem_pulls_in_nothing(db: sqlite3.Connection, class_id: int) -> None:
    """A problem that fit in one chunk has no parts, so it has no siblings.

    `_number_parts` leaves `part_index` null exactly when a problem was not split, and
    expansion used to key on `problem_number` alone. Two unrelated whole problems that
    happen to share a number are not one problem.
    """
    homework = _insert_document(db, class_id, "homework-4.pdf", _days_ago(1))
    noise = _insert_document(db, class_id, "lecture.pdf", _days_ago(1))
    _insert_chunk(
        db, class_id, homework, "2. Differentiate the following.", 0.0, problem_number="2"
    )
    # Seven closer chunks fill the rest of k, so the other problem 2 can only arrive by
    # expansion. It must not.
    for index in range(7):
        _insert_chunk(db, class_id, noise, f"Related lecture material {index}.", 0.1 * (index + 1))
    unrelated = _insert_chunk(
        db, class_id, homework, "2. An unrelated second question.", 1.3, problem_number="2"
    )

    result = retrieve(db, class_id, "how do I differentiate", 1000)

    assert len(result.chunks) == retrieve_module.K
    assert unrelated not in [chunk.chunk_id for chunk in result.chunks]


def test_numbering_that_restarts_does_not_merge_two_problems(
    db: sqlite3.Connection, class_id: int
) -> None:
    """The fault a 608-page textbook found, in miniature.

    A sheet that restarts numbering under each section heading has several problem 1s, and
    the chunker has grouped parts by consecutive run since Phase 2 for exactly that reason.
    Retrieval did not: it took every chunk in the document carrying the same number. Read
    as homework, the textbook had 120 chunks numbered `2` spread across the whole book, and
    one hit emitted all 120 ahead of the second-ranked result at the first one's score.
    """
    homework = _insert_document(db, class_id, "homework-4.pdf", _days_ago(1))
    noise = _insert_document(db, class_id, "lecture.pdf", _days_ago(1))
    _insert_chunk(
        db, class_id, homework, "1(a) State the theorem.", 0.0, problem_number="1", part_index=0
    )
    # Seven closer chunks fill the rest of k, so everything below arrives only by expansion.
    for index in range(7):
        _insert_chunk(db, class_id, noise, f"Related lecture material {index}.", 0.1 * (index + 1))
    first_b = _insert_chunk(
        db, class_id, homework, "1(b) Prove it.", 1.4, problem_number="1", part_index=1
    )
    # A second section, numbered from one again. Same number, different problem.
    second_a = _insert_chunk(
        db, class_id, homework, "1(a) An unrelated question.", 1.5, problem_number="1", part_index=0
    )
    second_b = _insert_chunk(
        db, class_id, homework, "1(b) Also unrelated.", 1.6, problem_number="1", part_index=1
    )

    result = retrieve(db, class_id, "state the theorem", 1000)

    returned = [chunk.chunk_id for chunk in result.chunks]
    assert first_b in returned, "the matched problem's own other part still arrives"
    assert second_a not in returned
    assert second_b not in returned


def _insert_section_chunk(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    content: str,
    angle: float,
    section_number: str,
    page_number: int,
) -> int:
    """A chunk of a numbered section, at a chosen angle from the query."""
    cursor = db.execute(
        "insert into chunks "
        "(document_id, class_id, content, token_count, page_number, section_title, "
        "section_path, section_number, doc_type, embedding_model, embedding_dim) "
        "values (?, ?, ?, ?, ?, 'A Section', 'Chapter / A Section', ?, 'textbook', "
        "'nomic-embed-text-v1.5.Q8_0', ?)",
        (
            document_id,
            class_id,
            content,
            estimate_tokens(content),
            page_number,
            section_number,
            DIMENSIONS,
        ),
    )
    chunk_id = int(cursor.lastrowid or 0)
    db.execute(
        "insert into chunk_embeddings (chunk_id, class_id, embedding) values (?, ?, ?)",
        (chunk_id, class_id, sqlite_vec.serialize_float32(_vector(angle))),
    )
    db.commit()
    return chunk_id


def test_a_named_section_is_looked_up_rather_than_searched_for(
    db: sqlite3.Connection, class_id: int
) -> None:
    """The measured failure this exists to fix.

    Asked bare, `What does section 2.2 cover?` came back at rank 12 and
    `Summarize what section 4.9 is about` at rank 23, both scoring below questions about
    material the book does not contain at all. A section number is a fact printed on the
    page, not a similarity, so no embedding improvement was ever going to reach them.
    """
    book = _insert_document(db, class_id, "textbook.pdf", _days_ago(1))
    # Deliberately the worst match in the class: only the lookup can surface it.
    wanted = _insert_section_chunk(db, class_id, book, "An LU factorization is...", 1.5, "2.2", 110)
    for index in range(8):
        _insert_chunk(db, class_id, book, f"Unrelated but closer material {index}.", 0.1 * index)

    result = retrieve(db, class_id, "What does section 2.2 cover?", 1000)

    assert result.chunks[0].chunk_id == wanted, "the named section leads the ranking"


def test_a_section_lookup_reaches_everything_nested_under_it(
    db: sqlite3.Connection, class_id: int
) -> None:
    """Asking for section 2.2 asks for 2.2.1 as well, because that is inside it."""
    book = _insert_document(db, class_id, "textbook.pdf", _days_ago(1))
    parent = _insert_section_chunk(db, class_id, book, "LU factorization.", 1.4, "2.2", 110)
    child = _insert_section_chunk(
        db, class_id, book, "Finding one by inspection.", 1.5, "2.2.1", 111
    )
    # A neighbouring section that must not come along: 2.20 is not underneath 2.2.
    other = _insert_section_chunk(db, class_id, book, "Something else entirely.", 1.45, "2.20", 130)
    # Closer chunks fill k, so anything from a section arrives by lookup rather than by
    # having been one of the neighbours anyway.
    for index in range(8):
        _insert_chunk(db, class_id, book, f"Unrelated but closer material {index}.", 0.1 * index)

    result = retrieve(db, class_id, "use the result from section 2.2", 1000)

    returned = [chunk.chunk_id for chunk in result.chunks]
    assert parent in returned
    assert child in returned
    assert other not in returned


def test_a_lowercase_section_reference_reaches_the_sections_own_chunks(
    db: sqlite3.Connection, class_id: int
) -> None:
    """`section a.2` used to resolve A.2's subsections and not A.2 itself.

    Stored section numbers are always uppercase (the heading regexes only accept
    `[A-Z]`), `SECTION_REFERENCE` matches case-insensitively, and SQLite's `=` is
    case-sensitive where its `like` is not - so the lookup's two arms disagreed and the
    student got the halves of the section without its opening. The captured number is
    normalized before the SQL.
    """
    book = _insert_document(db, class_id, "textbook.pdf", _days_ago(1))
    own = _insert_section_chunk(db, class_id, book, "Well ordering, stated.", 1.5, "A.2", 300)
    nested = _insert_section_chunk(db, class_id, book, "A nested lemma.", 1.4, "A.2.1", 301)
    # Closer chunks fill k, so the section's chunks can only arrive by lookup.
    for index in range(8):
        _insert_chunk(db, class_id, book, f"Unrelated but closer material {index}.", 0.1 * index)

    result = retrieve(db, class_id, "what does section a.2 cover", 1000)

    returned = [chunk.chunk_id for chunk in result.chunks]
    assert own in returned, "the section's own chunks resolve, not only its subsections"
    assert nested in returned


def test_a_looked_up_section_reads_its_distances_off_the_lookup_itself(
    db: sqlite3.Connection, class_id: int
) -> None:
    """One query per reference, not one per row.

    The lookup used to re-ask the embeddings table for each row's distance one chunk at a
    time, an N+1 the outer query can answer as a column. The similarity must still be the
    embedder's measurement, so both facts are asserted: no per-chunk query ran, and the
    reported similarity is the cosine the stored angle implies.
    """
    book = _insert_document(db, class_id, "textbook.pdf", _days_ago(1))
    for index in range(5):
        _insert_section_chunk(db, class_id, book, f"Part {index} of 2.2.", 1.0, "2.2", 110 + index)

    statements: list[str] = []
    db.set_trace_callback(statements.append)
    result = retrieve(db, class_id, "what does section 2.2 cover", 10_000)
    db.set_trace_callback(None)

    assert [s for s in statements if "where chunk_id" in s] == []
    assert result.chunks[0].similarity == pytest.approx(math.cos(1.0), abs=1e-5)


def test_a_reference_that_resolves_to_nothing_falls_through_quietly(
    db: sqlite3.Connection, class_id: int
) -> None:
    """A student may cite a section of a book they never uploaded, or number their weeks.

    The similarity search is a perfectly good answer to both, so a miss costs one query
    and nothing else. Failing here would be a regression on every course that says `week 3`.
    """
    notes = _insert_document(db, class_id, "lecture.pdf", _days_ago(1))
    match = _insert_chunk(db, class_id, notes, "Convolution, as covered in week three.", 0.0)

    result = retrieve(db, class_id, "what did we do in section 9.4", 1000)

    assert [chunk.chunk_id for chunk in result.chunks] == [match]


def test_a_looked_up_section_leaves_room_for_the_question(
    db: sqlite3.Connection, class_id: int
) -> None:
    """A section reference says where to look, not what is wanted from it.

    Without the cap a long chapter fills the context on its own and the student's actual
    question arrives with nothing beside it.
    """
    book = _insert_document(db, class_id, "textbook.pdf", _days_ago(1))
    body = "A long passage about factorization that fills the budget. " * 4
    for index in range(6):
        _insert_section_chunk(db, class_id, book, f"{body} {index}", 1.5, "2.2", 110 + index)
    nearest = _insert_chunk(db, class_id, book, "The closest match in the class.", 0.0)

    result = retrieve(db, class_id, "section 2.2 and the method it uses", estimate_tokens(body) * 4)

    returned = [chunk.chunk_id for chunk in result.chunks]
    assert nearest in returned, "the similarity search still gets room"
    assert any(chunk.section_number == "2.2" for chunk in result.chunks)


def test_a_looked_up_section_is_quoted_in_reading_order(
    db: sqlite3.Connection, class_id: int
) -> None:
    """A section quoted out of order is harder to follow than one quoted short."""
    book = _insert_document(db, class_id, "textbook.pdf", _days_ago(1))
    # Inserted with the later page as the better match, so document order is not score order.
    second = _insert_section_chunk(db, class_id, book, "Then this follows.", 0.2, "2.2", 112)
    first = _insert_section_chunk(db, class_id, book, "First, define the terms.", 0.9, "2.2", 111)

    result = retrieve(db, class_id, "explain section 2.2", 1000)

    assert [chunk.chunk_id for chunk in result.chunks] == [first, second]


@pytest.fixture
def reranker(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Stand in for the cross-encoder: it exactly reverses whatever it is given.

    Reversal rather than a fixed order, because it inverts the embedding ranking without
    depending on it. A test can then tell "the reranker decided this" from "the KNN did",
    which is the only thing worth asserting about a model whose weights are not here.

    Returns:
        The passage lists it was asked to score, so a test can check what it received.
    """
    asked: list[list[str]] = []

    def reversed_scores(query: str, passages: list[str]) -> list[float]:
        asked.append(list(passages))
        return [float(index) for index in range(len(passages))]

    monkeypatch.setattr(retrieve_module, "rerank", reversed_scores)
    monkeypatch.setattr(retrieve_module, "rerank_server", type("Stub", (), {"available": True})())
    return asked


def test_reranking_decides_the_order_the_similarity_search_proposed(
    db: sqlite3.Connection, class_id: int, reranker: list[list[str]]
) -> None:
    """The whole point of the second model: it is allowed to disagree with the first."""
    notes = _insert_document(db, class_id, "lecture.pdf", _days_ago(1))
    nearest = _insert_chunk(db, class_id, notes, "The closest match by cosine.", 0.0)
    furthest = _insert_chunk(db, class_id, notes, "A worse match by cosine.", 1.0)

    result = retrieve(db, class_id, "anything", 1000)

    assert [chunk.chunk_id for chunk in result.chunks] == [furthest, nearest]


def test_a_scoped_document_still_reranks_its_own_candidates(
    db: sqlite3.Connection, class_id: int, reranker: list[list[str]]
) -> None:
    """Scoping changes where the candidates come from, not what happens to them next.

    The cross-encoder reads the pinned document's chunks and only those; its verdict
    still decides the order.
    """
    pinned = _insert_document(db, class_id, "reader.pdf", _days_ago(1))
    other = _insert_document(db, class_id, "lecture.pdf", _days_ago(1))
    nearest = _insert_chunk(db, class_id, pinned, "Nearest by cosine.", 0.0)
    furthest = _insert_chunk(db, class_id, pinned, "Further by cosine.", 1.0)
    _insert_chunk(db, class_id, other, "Another document's material.", 0.1)

    result = retrieve(db, class_id, "anything", 1000, document_id=pinned)

    # The stub reverses what it reads, so the reranker demonstrably decided this order.
    assert [chunk.chunk_id for chunk in result.chunks] == [furthest, nearest]
    assert reranker[0] == ["Nearest by cosine.", "Further by cosine."]


def test_reranking_reports_the_similarity_the_embedder_measured(
    db: sqlite3.Connection, class_id: int, reranker: list[list[str]]
) -> None:
    """`score` is the ranking key and may be a logit; `similarity` is a cosine and is what
    the interface shows. Overwriting the second with the first would put an unbounded,
    routinely negative number in front of a student."""
    notes = _insert_document(db, class_id, "lecture.pdf", _days_ago(1))
    _insert_chunk(db, class_id, notes, "The closest match by cosine.", 0.0)

    result = retrieve(db, class_id, "anything", 1000)

    assert result.chunks[0].similarity == pytest.approx(1.0)


def test_reranking_serves_k_however_many_it_was_given(
    db: sqlite3.Connection, class_id: int, reranker: list[list[str]]
) -> None:
    """The over-fetch is a shortlist, not more context. Everything past `k` is material the
    search was not confident about and the reranker did not rescue, and letting it through
    would quietly change how many chunks a turn is built from."""
    notes = _insert_document(db, class_id, "lecture.pdf", _days_ago(1))
    for index in range(retrieve_module.K + 6):
        _insert_chunk(db, class_id, notes, f"Material {index}.", 0.01 * index)

    result = retrieve(db, class_id, "anything", 100_000)

    assert len(reranker[0]) == retrieve_module.K + 6, "it reads more than it serves"
    assert len(result.chunks) == retrieve_module.K


def test_a_reranker_that_fails_leaves_the_search_order_intact(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reranking improves an ordering that already works, so its failure must cost the
    improvement and nothing else - including not serving the whole over-fetch."""
    monkeypatch.setattr(retrieve_module, "rerank", lambda query, passages: None)
    monkeypatch.setattr(retrieve_module, "rerank_server", type("Stub", (), {"available": True})())
    notes = _insert_document(db, class_id, "lecture.pdf", _days_ago(1))
    nearest = _insert_chunk(db, class_id, notes, "The closest match by cosine.", 0.0)
    for index in range(retrieve_module.K + 4):
        _insert_chunk(db, class_id, notes, f"Worse material {index}.", 0.1 + 0.01 * index)

    result = retrieve(db, class_id, "anything", 100_000)

    assert result.chunks[0].chunk_id == nearest
    assert len(result.chunks) == retrieve_module.K


def test_without_a_reranker_the_search_is_not_widened(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The surplus would go straight into the budget, and the turn would be built from
    chunks the search was never confident about. That is a different product."""
    monkeypatch.setattr(retrieve_module, "rerank_server", type("Stub", (), {"available": False})())
    monkeypatch.setattr(
        retrieve_module,
        "rerank",
        lambda query, passages: pytest.fail("reranking ran without a reranker"),
    )
    notes = _insert_document(db, class_id, "lecture.pdf", _days_ago(1))
    for index in range(retrieve_module.RERANK_FETCH_K):
        _insert_chunk(db, class_id, notes, f"Material {index}.", 0.01 * index)

    result = retrieve(db, class_id, "anything", 100_000)

    assert len(result.chunks) == retrieve_module.K
