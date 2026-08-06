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


def _insert_document(db: sqlite3.Connection, class_id: int, filename: str, created_at: str) -> int:
    """A ready document with an explicit upload time, so recency is under test control."""
    cursor = db.execute(
        "insert into documents "
        "(class_id, filename, stored_path, mime, byte_size, state, created_at) "
        "values (?, ?, ?, 'application/pdf', 2048, 'ready', ?)",
        (class_id, filename, f"uploads/{class_id}/{filename}", created_at),
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
