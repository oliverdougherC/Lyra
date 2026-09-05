"""Bounded source gathering and quiz output contracts (PLA-469/470/474)."""

import sqlite3
import tracemalloc

import pytest

from backend.core import study
from backend.rag import retrieve as retrieval
from backend.tests.test_retrieve import _days_ago, _insert_chunk, _insert_document, _vector


def _question(kind: str = "mcq") -> dict[str, object]:
    return {
        "type": kind,
        "question": "The value is ___?",
        "topic": "Values",
        "explanation": "The source states the value.",
        "correct_index": 0,
        "options": ["A", "B", "C", "D"] if kind == "mcq" else ["True", "False"],
    }


@pytest.mark.parametrize("options", [["", "B", "C", "D"], ["A", " a ", "C", "D"]])
def test_mcq_rejects_unusable_choices(options: list[str]) -> None:
    question = {**_question(), "options": options}
    assert study._validate_questions([question], frozenset({"mcq"}))[0] == []


@pytest.mark.parametrize("answer", ["", " ", "\n\t"])
def test_fill_blank_rejects_empty_answers(answer: str) -> None:
    question = {**_question("fill_blank"), "options": [answer]}
    assert study._validate_questions([question], frozenset({"fill_blank"}))[0] == []


def test_requested_formats_and_answer_order_survive_validation() -> None:
    mcq = {**_question(), "options": [" B ", "A", "C", "D"], "correct_index": 1}
    tf = _question("true_false")
    assert study._validate_questions([tf], frozenset({"mcq"}))[0] == []
    valid, failures = study._validate_questions([mcq, tf], frozenset({"mcq", "true_false"}))
    assert valid == [mcq, tf]
    assert not failures
    assert valid[0]["options"][valid[0]["correct_index"]] == "A"


def _source_db(count: int, text: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("create table chunks (id integer primary key, document_id integer, content text)")
    conn.execute("create index chunks_document on chunks(document_id, id)")
    conn.executemany(
        "insert into chunks (document_id, content) values (1, ?)", ((text,) for _ in range(count))
    )
    return conn


def test_gathering_memory_does_not_scale_with_corpus() -> None:
    peaks = []
    for count in (2000, 16000):
        conn = _source_db(count, "x" * 2048)
        tracemalloc.start()
        text, ids = study._gather_source_text(conn, (1,), 2048)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        conn.close()
        assert len(text) == 8198
        assert ids == [1]
        peaks.append(peak)
    assert max(peaks) < 150_000
    assert peaks[1] < peaks[0] * 2


def test_oversized_no_fit_scan_is_bounded_and_does_not_materialize_text() -> None:
    conn = _source_db(1000, "x" * 100_000)
    queries = []
    conn.set_trace_callback(queries.append)
    tracemalloc.start()
    assert study._gather_source_text(conn, (1,), 1) == ("", [])
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(queries) == study._DOCUMENT_SCAN_LIMIT
    assert all("select content" not in query for query in queries)
    assert peak < 150_000
    conn.close()


def test_many_documents_preserve_round_robin_and_exact_selection() -> None:
    conn = _source_db(3, "1111")
    for doc in range(2, 102):
        conn.executemany(
            "insert into chunks(document_id, content) values (?, ?)", [(doc, str(doc) * 4)] * 3
        )
    selected = tuple(range(1, 101))
    text, ids = study._gather_source_text(conn, selected, 1000)
    assert ids == list(selected)
    assert text.split("\n\n")[:100] == [str(doc) * 4 for doc in selected]
    assert "101101101101" not in text
    conn.close()


@pytest.mark.parametrize("query", ["section 2.2", "selected missingword"])
def test_selected_set_filters_every_retrieval_branch_before_limits(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    monkeypatch.setattr(retrieval, "embed_query", lambda _: _vector(0))
    monkeypatch.setattr(type(retrieval.rerank_server), "available", property(lambda _: False))
    selected = [_insert_document(db, class_id, f"selected-{i}.pdf", _days_ago(0)) for i in range(2)]
    excluded = _insert_document(db, class_id, "answer-key.pdf", _days_ago(0))
    for doc in selected:
        chunk = _insert_chunk(db, class_id, doc, "selected section 2.2 lecture", 1.2)
        db.execute("update chunks set section_number = '2.2' where id = ?", (chunk,))
    for _ in range(80):
        chunk = _insert_chunk(db, class_id, excluded, query, 0)
        db.execute(
            "update chunks set section_number = '2.2', doc_type = 'solutions' where id = ?",
            (chunk,),
        )
    db.commit()
    result = retrieval.retrieve(db, class_id, query, 4096, document_ids=tuple(selected))
    assert {chunk.document_id for chunk in result.chunks} == set(selected)
    db.execute("update documents set state = 'embedding' where id = ?", (selected[0],))
    result = retrieval.retrieve(db, class_id, query, 4096, document_ids=tuple(selected))
    assert {chunk.document_id for chunk in result.chunks} == {selected[1]}
    db.execute("delete from documents where id = ?", (selected[1],))
    assert retrieval.retrieve(db, class_id, query, 4096, document_ids=tuple(selected)).chunks == []
    assert retrieval.retrieve(db, class_id, query, 4096, document_ids=()).chunks == []


@pytest.mark.parametrize("field", ["question", "explanation", "topic"])
@pytest.mark.parametrize(
    "value", [123, {"text": "pretend string"}, ["pretend string"], None, "   "]
)
def test_question_text_fields_require_usable_strings(field: str, value: object) -> None:
    assert study._validate_questions([{**_question(), field: value}], frozenset({"mcq"}))[0] == []


def test_no_fit_global_scan_ceiling_and_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _source_db(0, "")
    conn.executemany(
        "insert into chunks(document_id, content) values (?, 'oversized')",
        ((doc,) for doc in range(1, 41) for _ in range(256)),
    )
    queries = []
    conn.set_trace_callback(queries.append)
    assert study._gather_source_text(conn, tuple(range(1, 41)), 1) == ("", [])
    assert len(queries) == study._SOURCE_SCAN_LIMIT
    checks = []

    def cancelled(_conn: sqlite3.Connection, artifact_id: int) -> None:
        checks.append(artifact_id)
        if len(checks) == 3:
            raise study._GenerationCancelledError()

    monkeypatch.setattr(study, "_raise_if_cancelled", cancelled)
    with pytest.raises(study._GenerationCancelledError):
        study._gather_source_text(conn, (1,), 1, artifact_id=42)
    assert checks == [42, 42, 42]
    conn.close()


def test_embedded_nul_cannot_bypass_source_memory_or_token_bound() -> None:
    conn = _source_db(1, "\0" + "x" * 1_000_000)
    queries = []
    conn.set_trace_callback(queries.append)
    assert study._gather_source_text(conn, (1,), 1) == ("", [])
    assert all("select content" not in query for query in queries)
    conn.close()
