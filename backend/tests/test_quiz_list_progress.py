"""The study hub reports actual resumable answers with bounded database reads."""

import json
import sqlite3

from backend.api import routes_study
from backend.core import artifacts


def _quiz(db: sqlite3.Connection, class_id: int) -> tuple[int, list[int]]:
    document_id = int(
        db.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
            "values (?, 'synthetic.txt', '/tmp/synthetic', 'text/plain', 1, 'ready')",
            (class_id,),
        ).lastrowid
        or 0
    )
    db.commit()
    quiz = artifacts.create_artifact(
        db,
        class_id,
        "Synthetic quiz",
        [artifacts.SourceSpec(document_id=document_id, role=artifacts.STUDY_SOURCE)],
        kind=artifacts.KIND_QUIZ,
    )
    quiz_id = int(quiz["id"])
    parts = [
        artifacts.create_part(
            db,
            quiz_id,
            artifacts.QUIZ_QUESTION,
            ordinal,
            content="{}",
            content_type=artifacts.JSON,
            status=artifacts.PART_COMPLETE,
        )
        for ordinal in range(1, 4)
    ]
    artifacts.set_artifact_state(db, quiz_id, artifacts.READY)
    db.execute(
        "update artifacts set problems_done = 3, problems_total = 3 where id = ?", (quiz_id,)
    )
    db.commit()
    return quiz_id, parts


def _attempt(db: sqlite3.Connection, quiz_id: int, parts: list[int]) -> int:
    attempt_id = int(
        db.execute(
            "insert into quiz_attempts (artifact_id, question_part_ids, question_count) "
            "values (?, ?, ?)",
            (quiz_id, json.dumps(parts), len(parts)),
        ).lastrowid
        or 0
    )
    db.commit()
    return attempt_id


def _answer(db: sqlite3.Connection, attempt_id: int, part_id: int) -> None:
    db.execute(
        "insert into quiz_answers (attempt_id, part_id, selected_index, correct) "
        "values (?, ?, 0, 1)",
        (attempt_id, part_id),
    )
    db.commit()


def test_list_counts_actual_active_answers_not_generated_questions(
    db: sqlite3.Connection,
    class_id: int,
) -> None:
    quiz_id, parts = _quiz(db, class_id)
    finished = _attempt(db, quiz_id, parts)
    _answer(db, finished, parts[0])
    db.execute("update quiz_attempts set finished_at = datetime('now') where id = ?", (finished,))
    db.commit()
    active = _attempt(db, quiz_id, parts)
    _answer(db, active, parts[0])
    _answer(db, active, parts[1])

    quiz = routes_study.list_study(class_id, db)["quizzes"][0]
    assert quiz["problems_done"] == 3
    assert quiz["active_attempt_id"] == active
    assert quiz["answered_count"] == 2


def test_list_excludes_finished_abandoned_stale_unready_and_other_class_attempts(
    db: sqlite3.Connection,
    class_id: int,
) -> None:
    for state in ("finished", "abandoned", "stale", "legacy", "unready", "none"):
        quiz_id, parts = _quiz(db, class_id)
        if state == "none":
            continue
        attempt_id = _attempt(db, quiz_id, parts)
        _answer(db, attempt_id, parts[0])
        if state == "finished":
            db.execute(
                "update quiz_attempts set finished_at = datetime('now') where id = ?", (attempt_id,)
            )
        elif state == "abandoned":
            db.execute("update quiz_attempts set abandoned = 1 where id = ?", (attempt_id,))
        elif state == "stale":
            db.execute("update artifact_parts set ordinal = 4 where id = ?", (parts[0],))
        elif state == "legacy":
            db.execute(
                "update quiz_attempts set question_part_ids = null where id = ?", (attempt_id,)
            )
        else:
            db.execute("update artifacts set state = 'generating' where id = ?", (quiz_id,))
        db.commit()
    other_class = int(
        db.execute("insert into classes (name) values ('Other class')").lastrowid or 0
    )
    db.commit()
    other_quiz, parts = _quiz(db, other_class)
    _answer(db, _attempt(db, other_quiz, parts), parts[0])

    quizzes = routes_study.list_study(class_id, db)["quizzes"]
    assert len(quizzes) == 6
    assert all(quiz["id"] != other_quiz for quiz in quizzes)
    assert all(
        quiz["active_attempt_id"] is None and quiz["answered_count"] == 0 for quiz in quizzes
    )


def test_hundred_quizzes_use_same_query_count_as_one(
    db: sqlite3.Connection,
    class_id: int,
) -> None:
    counts = []
    for total in (1, 100):
        for _ in range(total - (1 if counts else 0)):
            quiz_id, parts = _quiz(db, class_id)
            _attempt(db, quiz_id, parts)
        statements: list[str] = []
        db.set_trace_callback(statements.append)
        try:
            quizzes = routes_study.list_study(class_id, db)["quizzes"]
        finally:
            db.set_trace_callback(None)
        counts.append(len(statements))
        assert len(quizzes) == total
        assert all(
            quiz["active_attempt_id"] is not None and quiz["answered_count"] == 0
            for quiz in quizzes
        )
    assert counts[0] == counts[1]
    assert counts[1] <= 4
