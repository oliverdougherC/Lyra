"""Contract tests for lexical retrieval and its fusion with the vector ranking.

The embedding server is never started, exactly as in test_retrieve.py: `embed_query` is
replaced with a fixed vector and stored vectors are crafted angles, so every cosine in
these tests is one the test chose. The lexical side needs no stand-in: FTS5 runs in
process against the temporary database, so the tests drive the real index, the real
triggers, and the real BM25.
"""

import math
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import sqlite_vec

from backend.rag import retrieve as retrieve_module
from backend.rag.retrieve import _fts_terms, _lexical_ranks, retrieve
from backend.rag.tokens import estimate_tokens

DIMENSIONS = 768
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
    """A document with an explicit upload time and state."""
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
    doc_type: str = "generic",
) -> int:
    """One chunk and its embedding, placed at a chosen angle from the query."""
    cursor = db.execute(
        "insert into chunks "
        "(document_id, class_id, content, token_count, page_number, section_title, "
        "problem_number, part_index, doc_type, embedding_model, embedding_dim) "
        "values (?, ?, ?, ?, 2, 'Derivatives', NULL, NULL, ?, 'test-embed', ?)",
        (document_id, class_id, content, estimate_tokens(content), doc_type, DIMENSIONS),
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


def test_fts5_is_available_in_the_bundled_sqlite(db: sqlite3.Connection) -> None:
    """A build without FTS5 must fail here with the diagnosis, not deep in retrieval.

    Probed by creating a table rather than with `select fts5_version()`: that function is
    not exposed by every build that ships FTS5 (the bundled 3.53 is one), while table
    creation is the capability retrieval actually needs.
    """
    db.execute("create virtual table probe_fts using fts5(content)")
    db.execute("insert into probe_fts(content) values ('a probe sentence')")
    found = db.execute("select rowid from probe_fts where probe_fts match 'probe'").fetchone()
    assert found is not None


def test_inserted_chunks_are_findable_by_fts(db: sqlite3.Connection, class_id: int) -> None:
    document = _insert_document(db, class_id, "notes.pdf", _days_ago(1))
    chunk = _insert_chunk(db, class_id, document, "The eigenvalue decomposition.", 0.0)

    assert _lexical_ranks(db, class_id, "eigenvalue", 10) == [chunk]


def test_deleting_a_document_removes_its_chunks_from_the_index(
    db: sqlite3.Connection, class_id: int
) -> None:
    """The app deletes the document row and relies on the FK cascade to reach the chunks,
    so this is the cascade-delete -> trigger interaction, not the trigger alone. If a
    SQLite build stops firing triggers for cascaded rows, this test is the one that says
    so, and the delete path in routes_documents.py gains an explicit chunk delete.
    """
    document = _insert_document(db, class_id, "notes.pdf", _days_ago(1))
    _insert_chunk(db, class_id, document, "The eigenvalue decomposition.", 0.0)

    db.execute("delete from documents where id = ?", (document,))
    db.commit()

    assert _lexical_ranks(db, class_id, "eigenvalue", 10) == []
    assert db.execute("select count(*) from chunks_fts").fetchone()[0] == 0


def test_reingesting_a_document_leaves_no_orphaned_fts_rows(
    db: sqlite3.Connection, class_id: int
) -> None:
    """Re-ingestion deletes a document's chunks and writes new ones; the index must hold
    exactly the new generation, no union of the two."""
    document = _insert_document(db, class_id, "notes.pdf", _days_ago(1))
    _insert_chunk(db, class_id, document, "Old text about eigenvalues.", 0.0)
    _insert_chunk(db, class_id, document, "More old text about eigenvalues.", 0.1)

    db.execute("delete from chunks where document_id = ?", (document,))
    _insert_chunk(db, class_id, document, "New text about factorizations.", 0.0)
    db.commit()

    assert db.execute("select count(*) from chunks_fts").fetchone()[0] == 1
    assert _lexical_ranks(db, class_id, "eigenvalues", 10) == []
    assert len(_lexical_ranks(db, class_id, "factorizations", 10)) == 1


def test_query_terms_are_quoted_so_operator_words_are_literals() -> None:
    assert _fts_terms('AND OR NEAR "section"') == ['"AND"', '"OR"', '"NEAR"', '"section"']
    assert _fts_terms("") == []
    assert _fts_terms("   ") == []
    # A quote inside a word would close the quoting early, so it is stripped.
    assert _fts_terms('sec"tion') == ['"section"']


def test_an_operator_word_query_searches_the_word(db: sqlite3.Connection, class_id: int) -> None:
    """Unquoted, `AND` is FTS5 syntax and the query would raise; quoted, it is the word."""
    document = _insert_document(db, class_id, "notes.pdf", _days_ago(1))
    chunk = _insert_chunk(db, class_id, document, "AND gates and OR gates.", 0.0)

    assert _lexical_ranks(db, class_id, "AND", 10) == [chunk]


def test_all_terms_must_match_before_the_or_retry(db: sqlite3.Connection, class_id: int) -> None:
    document = _insert_document(db, class_id, "notes.pdf", _days_ago(1))
    both = _insert_chunk(db, class_id, document, "The eigenvalue decomposition works.", 0.0)
    one = _insert_chunk(db, class_id, document, "The eigenvalue alone.", 0.1)

    assert _lexical_ranks(db, class_id, "eigenvalue decomposition", 10) == [both]
    assert one not in _lexical_ranks(db, class_id, "eigenvalue decomposition", 10)


def test_the_or_retry_fires_only_when_no_chunk_has_every_term(
    db: sqlite3.Connection, class_id: int
) -> None:
    """One absent word must not zero the result, and BM25 still ranks a chunk matching
    more terms above one matching fewer."""
    document = _insert_document(db, class_id, "notes.pdf", _days_ago(1))
    both = _insert_chunk(db, class_id, document, "The eigenvalue decomposition works.", 0.0)
    one = _insert_chunk(db, class_id, document, "The eigenvalue alone.", 0.1)

    # No chunk carries all three words, so the AND pass returns nothing and the OR retry
    # runs; the chunk carrying two of them must outrank the one carrying one.
    ranks = _lexical_ranks(db, class_id, "eigenvalue decomposition nonexistentword", 10)

    assert ranks == [both, one]


def test_a_single_term_query_has_no_or_retry(db: sqlite3.Connection, class_id: int) -> None:
    document = _insert_document(db, class_id, "notes.pdf", _days_ago(1))
    _insert_chunk(db, class_id, document, "The eigenvalue decomposition.", 0.0)

    assert _lexical_ranks(db, class_id, "nonexistentword", 10) == []


def test_lexical_search_never_crosses_classes(db: sqlite3.Connection, class_id: int) -> None:
    other_class = db.execute("insert into classes (name) values ('Physics I')").lastrowid
    db.commit()
    document = _insert_document(db, int(other_class or 0), "notes.pdf", _days_ago(1))
    _insert_chunk(db, int(other_class or 0), document, "The eigenvalue decomposition.", 0.0)

    assert _lexical_ranks(db, class_id, "eigenvalue", 10) == []


def test_lexical_search_reads_only_ready_documents(db: sqlite3.Connection, class_id: int) -> None:
    """The ready-only rule holds for every retrieval path, including this one: a
    half-indexed document must not answer questions however well its words match."""
    broken = _insert_document(db, class_id, "book.pdf", _days_ago(1), state="embedding")
    _insert_chunk(db, class_id, broken, "The eigenvalue decomposition.", 0.0)

    assert _lexical_ranks(db, class_id, "eigenvalue", 10) == []


def test_lexical_search_pins_to_one_document(db: sqlite3.Connection, class_id: int) -> None:
    first = _insert_document(db, class_id, "week-1.pdf", _days_ago(2))
    second = _insert_document(db, class_id, "week-2.pdf", _days_ago(1))
    own = _insert_chunk(db, class_id, first, "The eigenvalue decomposition.", 0.0)
    _insert_chunk(db, class_id, second, "The eigenvalue decomposition again.", 0.0)

    assert _lexical_ranks(db, class_id, "eigenvalue", 10, first) == [own]


def test_a_chunk_in_both_rankings_outranks_the_top_of_one(
    db: sqlite3.Connection, class_id: int
) -> None:
    """The RRF property the fusion exists for: vector rank 3 plus lexical rank 1 beats
    vector rank 1 with no lexical match, because 1/63 + 1/61 > 1/61."""
    document = _insert_document(db, class_id, "notes.pdf", _days_ago(1))
    vector_top = _insert_chunk(db, class_id, document, "Unrelated material one.", 0.0)
    _insert_chunk(db, class_id, document, "Unrelated material two.", 0.1)
    _insert_chunk(db, class_id, document, "Unrelated material three.", 0.2)
    both = _insert_chunk(db, class_id, document, "The zyxplot quaternion defined.", 0.3)

    result = retrieve(db, class_id, "zyxplot", 1000)

    assert result.chunks[0].chunk_id == both
    assert result.chunks[0].score > result.chunks[1].score
    assert vector_top in [chunk.chunk_id for chunk in result.chunks]


def test_a_fused_tie_breaks_by_vector_rank_not_insertion_order(
    db: sqlite3.Connection, class_id: int
) -> None:
    """X is vector rank 1 and lexical rank 2; Y is the mirror, so the fused scores tie
    exactly. Y was inserted first, so a chunk-id tiebreak would serve it; the vector rank
    serves X, because that is the ranking recency and similarity already voted on."""
    document = _insert_document(db, class_id, "notes.pdf", _days_ago(1))
    # Inserted first on purpose: a chunk-id tiebreak would rank it above X.
    mirror = _insert_chunk(db, class_id, document, "eigenplot eigenplot brief.", 0.1)
    vector_first = _insert_chunk(
        db, class_id, document, "eigenplot explained at somewhat greater length here.", 0.0
    )

    result = retrieve(db, class_id, "eigenplot", 1000)

    assert result.chunks[0].chunk_id == vector_first
    assert result.chunks[1].chunk_id == mirror
    assert result.chunks[0].score == pytest.approx(result.chunks[1].score)


def test_the_solutions_bonus_breaks_a_fused_tie(db: sqlite3.Connection, class_id: int) -> None:
    """The same exact tie as above, but the mirror is an answer key: the bonus exists to
    put reference solutions ahead of the problem statement they restate."""
    document = _insert_document(db, class_id, "notes.pdf", _days_ago(1))
    key = _insert_chunk(db, class_id, document, "eigenplot eigenplot brief.", 0.1, "solutions")
    statement = _insert_chunk(
        db, class_id, document, "eigenplot explained at somewhat greater length here.", 0.0
    )

    result = retrieve(db, class_id, "eigenplot", 1000)

    assert result.chunks[0].chunk_id == key
    assert result.chunks[1].chunk_id == statement
    assert result.chunks[0].score > result.chunks[1].score


def test_a_lexical_only_candidate_carries_the_embedders_similarity(
    db: sqlite3.Connection, class_id: int
) -> None:
    """A chunk the KNN never returned still enters the fused ranking through the lexical
    pass, and its `similarity` is the embedder's real cosine, not a fabricated stand-in:
    that is the RetrievedChunk contract, and it is what the interface reports."""
    document = _insert_document(db, class_id, "notes.pdf", _days_ago(1))
    # Sixty-four chunks at the same angle fill the vector over-fetch exactly, so the
    # target, inserted last at the worst angle, is outside it and arrives by FTS alone.
    for index in range(retrieve_module.RERANK_FETCH_K):
        _insert_chunk(db, class_id, document, f"Unrelated material {index}.", 0.5)
    target = _insert_chunk(db, class_id, document, "The zyxplot quaternion defined.", 1.5)

    result = retrieve(db, class_id, "zyxplot", 100_000)

    served = {chunk.chunk_id: chunk for chunk in result.chunks}
    assert target in served
    assert served[target].similarity == pytest.approx(math.cos(1.5), abs=1e-5)


def test_a_pinned_document_fuses_within_its_own_scope(
    db: sqlite3.Connection, class_id: int
) -> None:
    """Scoping changes where the candidates come from, not what happens to them next: the
    lexical pass lifts the verbatim match above the closer embedding inside the pin."""
    document = _insert_document(db, class_id, "notes.pdf", _days_ago(1))
    other = _insert_document(db, class_id, "other.pdf", _days_ago(1))
    _insert_chunk(db, class_id, document, "Unrelated material.", 0.0)
    verbatim = _insert_chunk(db, class_id, document, "The zyxplot quaternion defined.", 0.5)
    _insert_chunk(db, class_id, other, "The zyxplot quaternion elsewhere.", 0.0)

    result = retrieve(db, class_id, "zyxplot", 1000, document_id=document)

    assert result.chunks[0].chunk_id == verbatim
