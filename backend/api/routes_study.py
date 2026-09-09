"""Study endpoints: flashcard decks and quizzes on the artifact substrate.

Creation answers `202` and does no work beyond writing the rows, because generation is a
run of model calls that takes minutes on local hardware. The interface polls `/status`
from there, exactly as it does for solutions and ingestion.

Handlers are sync `def`: `sqlite3` blocks, and FastAPI runs sync handlers in a
threadpool, which is where blocking work belongs.

The route prefixes are `/api/decks`, `/api/quizzes`, `/api/cards`, and `/api/attempts`
while the table is `artifacts`, for the same reason solutions are: the model is general
and these are the study tools' view of it.
"""

import json
import logging
import secrets
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field, field_validator

from backend.core import artifacts, grading, scheduler, study
from backend.core.app_settings import resolve_tutor_access
from backend.core.classes import get_class
from backend.core.deck_counts import deck_counts_for_class
from backend.core.errors import ConflictError, NotFoundError, UnprocessableError
from backend.llm.prompts import QUIZ_QUESTION_TYPES
from backend.storage.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["study"])

DbConn = Annotated[sqlite3.Connection, Depends(get_db)]

NOT_A_DECK_MESSAGE = "That deck does not exist."
NOT_A_QUIZ_MESSAGE = "That quiz does not exist."
NOT_A_CARD_MESSAGE = "That card does not exist."
NOT_AN_ATTEMPT_MESSAGE = "That attempt does not exist."
NOTHING_READY_MESSAGE = "There are no processed documents to study from yet."
NOT_RUNNING_MESSAGE = "This study run is not running."
DECK_NOT_READY_MESSAGE = "This deck is still being generated."
NOT_READY_MESSAGES: dict[str, str] = {
    artifacts.PENDING: "is still queued for generation.",
    artifacts.GENERATING: "is still being generated.",
    artifacts.FAILED: "failed to generate.",
    artifacts.CANCELLED: "was cancelled.",
}
DECK_CHANGED_MESSAGE = "This deck changed while you were reviewing. Reopen it and try again."
QUIZ_CHANGED_MESSAGE = "This quiz changed while you were answering. Reopen it and try again."
ATTEMPT_FINISHED_MESSAGE = "This attempt has already been finished."
NOT_THIS_QUIZ_MESSAGE = "That question does not belong to this quiz's attempt."
QUIZ_NOT_READY_MESSAGE = "This quiz is not ready to be taken yet."

QuizDifficulty = Literal["basic", "intermediate", "exam"]
QuizType = Literal["mcq", "true_false", "fill_blank"]
Rating = Literal["again", "hard", "good", "easy"]


class DeckCreate(BaseModel):
    """Body of `POST /api/classes/{class_id}/decks`."""

    title: str = Field(min_length=1)
    document_ids: list[int] | None = None
    cards_per_topic: int = Field(default=4, ge=2, le=6)

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("A deck name cannot be blank.")
        return cleaned


class QuizCreate(BaseModel):
    """Body of `POST /api/classes/{class_id}/quizzes`."""

    title: str = Field(min_length=1)
    document_ids: list[int] | None = None
    count: int = Field(default=10, ge=3, le=30)
    difficulty: QuizDifficulty = "intermediate"
    types: list[QuizType] | None = None

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("A quiz name cannot be blank.")
        return cleaned


class StudyRename(BaseModel):
    """Body of `PATCH /api/decks/{artifact_id}` and the quiz equivalent."""

    title: str = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("A name cannot be blank.")
        return cleaned


class CardUpdate(BaseModel):
    """Body of `PATCH /api/cards/{part_id}`. Both faces and the topic, every time."""

    front: str = Field(min_length=1)
    back: str = Field(min_length=1)
    topic: str = Field(min_length=1)

    @field_validator("front", "back", "topic")
    @classmethod
    def _check_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("A card's front, back, and topic cannot be blank.")
        return cleaned


class CardReview(BaseModel):
    """Body of `POST /api/cards/{part_id}/review`.

    `operation_id` is the client-generated idempotency key (PLA-296): one per revealed-card
    rating action, reused on a transport retry. Repeating it returns the original stored
    result and never applies the review twice.
    """

    rating: Rating
    operation_id: str = Field(min_length=1, max_length=200)

    @field_validator("operation_id")
    @classmethod
    def _check_operation_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("An operation id cannot be blank.")
        return cleaned


class AnswerCreate(BaseModel):
    """Body of `POST /api/attempts/{attempt_id}/answers`.

    For multiple choice and true/false, `selected_index` is the whole answer: any index
    other than the stored `correct_index` grades incorrect. A legacy choice client that
    predates `response_text` omits it, and the route grades the index as before, recording
    the chosen option's text where it can. For a fill_blank question the index carries no
    meaning (the runner sends -1) and `response_text` is the student's actual words - the
    layered grader evaluates them against the question's reference answer and hidden
    grading contract, and the route stores them beside the verdict (PLA-496), so a reload,
    a retry, and the attempt history all carry the original response.
    """

    part_id: int
    selected_index: int
    response_text: str | None = Field(default=None, max_length=grading.MAX_RESPONSE_CHARS)


class StudyStatusRead(BaseModel):
    """The polled generation state of one deck or quiz."""

    state: str
    stage_detail: str | None
    problems_total: int | None
    problems_done: int
    error_message: str | None


def _require_deck(conn: sqlite3.Connection, artifact_id: int) -> dict[str, object]:
    """The artifact, when it is a flashcard deck. 404 either way otherwise."""
    artifact = artifacts.get_artifact(conn, artifact_id)
    if artifact["kind"] != artifacts.KIND_FLASHCARD_DECK:
        raise NotFoundError(NOT_A_DECK_MESSAGE)
    return artifact


def _require_quiz(conn: sqlite3.Connection, artifact_id: int) -> dict[str, object]:
    """The artifact, when it is a quiz. 404 either way otherwise."""
    artifact = artifacts.get_artifact(conn, artifact_id)
    if artifact["kind"] != artifacts.KIND_QUIZ:
        raise NotFoundError(NOT_A_QUIZ_MESSAGE)
    return artifact


def _require_card(
    conn: sqlite3.Connection, part_id: int
) -> tuple[dict[str, object], dict[str, object]]:
    """The card part and its deck. A card only exists inside a ready-kind deck."""
    part = artifacts.get_part(conn, part_id)
    artifact = artifacts.get_artifact(conn, int(part["artifact_id"]))
    if artifact["kind"] != artifacts.KIND_FLASHCARD_DECK or part["kind"] != artifacts.CARD:
        raise NotFoundError(NOT_A_CARD_MESSAGE)
    return part, artifact


# The student-facing reason a chosen document cannot be used, by document state. No
# filesystem path, document text, or internal stage name ever appears; the filename and a
# plain reason do.
_UNREADY_REASONS: dict[str, str] = {
    "failed": "failed to process",
    "unsupported": "could not be read",
}
_UNREADY_DEFAULT = "is still processing"


def _study_sources(
    conn: sqlite3.Connection, class_id: int, document_ids: list[int] | None
) -> list[int]:
    """The document ids to generate from: the named ones exactly, or the whole class.

    An explicit `document_ids` list is an exact contract (PLA-291): every unique selected
    document must exist in this class and be `ready`. Duplicate ids are normalized away
    deterministically, order preserved. If any selected document is missing or belongs to
    another class it is a 404; if any exists but is not ready the whole request is refused
    with a 409 that names the affected files and what is wrong with them - never a silent
    generation from only the ready subset, and never a partial artifact.
    """
    rows = conn.execute(
        "select id, filename, state from documents where class_id = ?", (class_id,)
    ).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    if document_ids is not None:
        # Normalize duplicates to one, preserving first-seen order, so artifact source
        # ordinals stay stable and truthful whatever the client sent.
        unique: list[int] = []
        seen: set[int] = set()
        for document_id in document_ids:
            if document_id not in seen:
                seen.add(document_id)
                unique.append(document_id)
        missing = [document_id for document_id in unique if document_id not in by_id]
        if missing:
            raise NotFoundError("That document does not exist in this class.")
        not_ready = [
            by_id[document_id] for document_id in unique if by_id[document_id]["state"] != "ready"
        ]
        if not_ready:
            raise ConflictError(_unready_message(not_ready))
        return unique
    ready = [int(row["id"]) for row in rows if row["state"] == "ready"]
    if not ready:
        raise ConflictError(NOTHING_READY_MESSAGE)
    return ready


def _unready_message(rows: list[sqlite3.Row]) -> str:
    """A bounded 409 naming each not-ready document and why it cannot be used."""
    parts = [
        f"{row['filename']} {_UNREADY_REASONS.get(str(row['state']), _UNREADY_DEFAULT)}"
        for row in rows
    ]
    return "Some chosen documents are not ready: " + "; ".join(parts) + "."


def _study_source_specs(ready: list[int]) -> list[artifacts.SourceSpec]:
    """SourceSpec list from accepted document ids, in reading order."""
    return [
        artifacts.SourceSpec(document_id=document_id, role=artifacts.STUDY_SOURCE)
        for document_id in ready
    ]


def _create_study_artifact(
    conn: sqlite3.Connection,
    class_id: int,
    kind: str,
    title: str,
    document_ids: list[int] | None,
    *,
    job: study._Job | None = None,
) -> tuple[dict[str, object], list[int], study._Job | None]:
    """Artifact row + sources + optional job in one atomic commit (PLA-169).

    When ``job`` is provided the caller has already built a proto-job whose
    ``artifact_id`` and ``source_ids`` are placeholders (0 and ()). This function
    fills them in from the newly created artifact and persists the job in the same
    transaction, so a crash can never leave an artifact whose generation intent is
    unrecoverable.

    Returns the artifact, the accepted source ids, and the real job (or None).
    """
    get_class(conn, class_id)
    ready = _study_sources(conn, class_id, document_ids)
    try:
        created = artifacts.create_artifact(
            conn,
            class_id,
            title,
            _study_source_specs(ready),
            kind=kind,
            commit=False,
        )
        conn.execute(
            "update classes set last_active_at = datetime('now') where id = ?",
            (class_id,),
        )
        if job is not None:
            real_job = study._Job(
                int(created["id"]),
                source_ids=tuple(ready),
                cards_per_topic=job.cards_per_topic,
                count=job.count,
                difficulty=job.difficulty,
                types=job.types,
            )
            study.persist_job(conn, real_job, kind, commit=False)
        else:
            real_job = None
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return created, ready, real_job


def _enqueue_after_commit(job: study._Job) -> None:
    """Best-effort in-memory enqueue after the durable commit (PLA-169).

    The durable intent is already committed, so the reconciler will pick up the
    job on next startup if this enqueue fails. Swallowing the exception here
    means the route still returns the created artifact to the client, preventing
    duplicate creation on a client retry.
    """
    try:
        study.enqueue(job)
    except Exception:
        logger.warning(
            "In-memory enqueue failed for artifact %s; the reconciler will "
            "recover the durable intent on next startup",
            job.artifact_id,
        )


@router.post(
    "/classes/{class_id}/decks",
    response_model=None,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_deck(class_id: int, payload: DeckCreate, conn: DbConn) -> dict[str, object]:
    proto = study._Job(0, source_ids=(), cards_per_topic=payload.cards_per_topic)
    created, _source_ids, job = _create_study_artifact(
        conn,
        class_id,
        artifacts.KIND_FLASHCARD_DECK,
        payload.title,
        payload.document_ids,
        job=proto,
    )
    if job is not None:
        _enqueue_after_commit(job)
    return created


@router.post(
    "/classes/{class_id}/quizzes",
    response_model=None,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_quiz(class_id: int, payload: QuizCreate, conn: DbConn) -> dict[str, object]:
    proto = study._Job(
        0,
        source_ids=(),
        count=payload.count,
        difficulty=payload.difficulty,
        types=tuple(payload.types) if payload.types else QUIZ_QUESTION_TYPES,
    )
    created, _source_ids, job = _create_study_artifact(
        conn,
        class_id,
        artifacts.KIND_QUIZ,
        payload.title,
        payload.document_ids,
        job=proto,
    )
    if job is not None:
        _enqueue_after_commit(job)
    return created


@router.get("/classes/{class_id}/study", response_model=None)
def list_study(class_id: int, conn: DbConn) -> dict[str, object]:
    """Study hub entries with deck due counts and resumable quiz answer progress."""
    get_class(conn, class_id)
    rows = conn.execute(
        "select id, kind, title, state, stage_detail, problems_total, problems_done, "
        "error_message, created_at, updated_at from artifacts "
        "where class_id = ? and kind in (?, ?) order by updated_at desc",
        (class_id, artifacts.KIND_FLASHCARD_DECK, artifacts.KIND_QUIZ),
    ).fetchall()

    decks: list[dict[str, object]] = []
    quizzes: list[dict[str, object]] = []
    quiz_progress = _quiz_list_progress(conn, class_id)
    now = scheduler.to_storage(datetime.now(UTC))
    counts = (
        deck_counts_for_class(conn, class_id, now)
        if any(row["kind"] == artifacts.KIND_FLASHCARD_DECK for row in rows)
        else {}
    )
    for row in rows:
        entry = dict(row)
        if row["kind"] == artifacts.KIND_FLASHCARD_DECK:
            # A deck deleted after the inventory read has no remaining card states.
            entry.update(
                counts.get(
                    int(row["id"]),
                    {
                        "cards_total": 0,
                        "buckets": {"new": 0, "learning": 0, "mastered": 0},
                        "due_count": 0,
                    },
                )
            )
            decks.append(entry)
        else:
            attempt_id, answered_count = quiz_progress.get(int(row["id"]), (None, 0))
            entry.update(active_attempt_id=attempt_id, answered_count=answered_count)
            quizzes.append(entry)
    return {"decks": decks, "quizzes": quizzes}


def _quiz_list_progress(conn: sqlite3.Connection, class_id: int) -> dict[int, tuple[int, int]]:
    """Batch active progress without treating generated questions as student answers.

    Match current_attempt's question snapshot check so regenerated quizzes never advertise
    stale progress. Both queries are class-scoped, regardless of the number of quizzes.
    """
    attempts = conn.execute(
        "select t.id, t.artifact_id, t.question_part_ids, count(ans.part_id) as answered_count "
        "from quiz_attempts t join artifacts a on a.id = t.artifact_id "
        "left join quiz_answers ans on ans.attempt_id = t.id "
        "where a.class_id = ? and a.kind = ? and a.state = ? "
        "and t.finished_at is null and t.abandoned = 0 group by t.id",
        (class_id, artifacts.KIND_QUIZ, artifacts.READY),
    ).fetchall()
    if not attempts:
        return {}
    parts = conn.execute(
        "select p.artifact_id, p.id from artifact_parts p "
        "join artifacts a on a.id = p.artifact_id "
        "join quiz_attempts t on t.artifact_id = a.id "
        "where a.class_id = ? and a.kind = ? and a.state = ? and p.kind = ? "
        "and t.finished_at is null and t.abandoned = 0 order by p.ordinal, p.id",
        (class_id, artifacts.KIND_QUIZ, artifacts.READY, artifacts.QUIZ_QUESTION),
    ).fetchall()
    question_ids: dict[int, list[int]] = {}
    for part in parts:
        question_ids.setdefault(int(part["artifact_id"]), []).append(int(part["id"]))
    progress: dict[int, tuple[int, int]] = {}
    for attempt in attempts:
        artifact_id = int(attempt["artifact_id"])
        raw_snapshot = attempt["question_part_ids"]
        if raw_snapshot is None:
            continue
        snapshot = json.loads(str(raw_snapshot))
        if snapshot and snapshot == question_ids.get(artifact_id):
            progress[artifact_id] = (int(attempt["id"]), int(attempt["answered_count"]))
    return progress


def _deck_counts(conn: sqlite3.Connection, artifact_id: int, now: str) -> dict[str, object]:
    """Bucket counts and the due count over a deck's card states."""
    rows = conn.execute(
        "select cs.* from card_states cs "
        "join artifact_parts p on p.id = cs.part_id where p.artifact_id = ?",
        (artifact_id,),
    ).fetchall()
    buckets = {"new": 0, "learning": 0, "mastered": 0}
    due = 0
    for row in rows:
        buckets[scheduler.bucket(_state_from_row(row))] += 1
        if str(row["due_at"]) <= now:
            due += 1
    return {"cards_total": len(rows), "buckets": buckets, "due_count": due}


def _card_json(part: dict[str, object], state_row: sqlite3.Row | None) -> dict[str, object]:
    """One card for the interface: payload parsed, scheduling state beside it."""
    payload = json.loads(str(part["content"]))
    return {
        "part_id": part["id"],
        "ordinal": part["ordinal"],
        "label": part["label"],
        "card": payload,
        "card_state": _state_json(state_row) if state_row is not None else None,
    }


def _state_from_row(row: sqlite3.Row) -> scheduler.CardState:
    """A card_states row as the scheduler's value type, with real datetimes."""
    return scheduler.CardState(
        due_at=scheduler.from_storage(str(row["due_at"])),
        stability=float(row["stability"]),
        difficulty=float(row["difficulty"]),
        reps=int(row["reps"]),
        lapses=int(row["lapses"]),
        state=str(row["state"]),
        last_review_at=(
            scheduler.from_storage(str(row["last_review_at"])) if row["last_review_at"] else None
        ),
    )


def _state_json(row: sqlite3.Row) -> dict[str, object]:
    """The scheduling state as the interface reads it: storage strings plus the bucket."""
    return _state_json_from_state(_state_from_row(row))


def _state_json_from_state(state: scheduler.CardState) -> dict[str, object]:
    """The interface shape of a scheduling state, from the value rather than a row.

    Used to build the stored idempotency result of a review (PLA-296): the result a
    duplicate returns is the state the review produced, serialized here exactly as
    `_state_json` would serialize the row it wrote, so the two can never disagree.
    """
    return {
        "due_at": scheduler.to_storage(state.due_at),
        "stability": state.stability,
        "difficulty": state.difficulty,
        "reps": state.reps,
        "lapses": state.lapses,
        "state": state.state,
        "last_review_at": (
            scheduler.to_storage(state.last_review_at) if state.last_review_at else None
        ),
        "bucket": scheduler.bucket(state),
    }


def _require_ready(artifact: dict[str, object], label: str) -> None:
    """409 when the artifact is not ready for content reads (PLA-312)."""
    state = str(artifact["state"])
    if state != artifacts.READY:
        reason = NOT_READY_MESSAGES.get(state, "is not ready.")
        raise ConflictError(f"This {label} {reason}")


@router.get("/decks/{artifact_id}", response_model=None)
def read_deck(artifact_id: int, conn: DbConn) -> dict[str, object]:
    artifact = _require_deck(conn, artifact_id)
    _require_ready(artifact, "deck")
    parts = [
        part for part in artifacts.list_parts(conn, artifact_id) if part["kind"] == artifacts.CARD
    ]
    state_rows = {
        int(row["part_id"]): row
        for row in conn.execute(
            "select cs.* from card_states cs join artifact_parts p on p.id = cs.part_id "
            "where p.artifact_id = ?",
            (artifact_id,),
        )
    }
    return {
        **artifact,
        "cards": [_card_json(part, state_rows.get(int(part["id"]))) for part in parts],
    }


@router.get("/decks/{artifact_id}/session", response_model=None)
def read_deck_session(artifact_id: int, conn: DbConn, limit: int = 20) -> dict[str, object]:
    """Cards in study order, each flagged due, capped at `limit`."""
    artifact = _require_deck(conn, artifact_id)
    _require_ready(artifact, "deck")
    rows = conn.execute(
        "select p.id, p.label, p.content, cs.* from card_states cs "
        "join artifact_parts p on p.id = cs.part_id where p.artifact_id = ?",
        (artifact_id,),
    ).fetchall()
    now = datetime.now(UTC)
    states = {int(row["part_id"]): _state_from_row(row) for row in rows}
    ordered = scheduler.study_order(states, now)[:limit]
    by_id = {int(row["part_id"]): row for row in rows}
    return {
        "cards": [
            {
                "part_id": part_id,
                "label": by_id[part_id]["label"],
                "card": json.loads(str(by_id[part_id]["content"])),
                "due": states[part_id].due_at <= now,
                "card_state": _state_json(by_id[part_id]),
            }
            for part_id in ordered
        ]
    }


@router.post("/cards/{part_id}/review", response_model=None)
def review_card(part_id: int, payload: CardReview, conn: DbConn) -> dict[str, object]:
    """Apply one rating through the scheduler and log it, idempotently (PLA-296).

    The whole review - reading the latest card state, computing the next one, writing it,
    and appending the review-log row - happens inside one `begin immediate` transaction, so
    two requests can never both compute from the same starting state and leave the card
    state and the log disagreeing. The client's `operation_id` makes the operation
    idempotent: a repeat (a lost-response retry, a duplicate transport, a second tab)
    returns the original stored result and does not advance the schedule, touch reps or
    lapses, or append a second log row. A card deleted or a deck knocked out of `ready`
    while the review is in flight is a truthful conflict with no partial write.
    """
    # A 404 for a non-card id, read before the write lock; the state is re-checked inside.
    part, _ = _require_card(conn, part_id)
    try:
        conn.execute("begin immediate")
        prior = conn.execute(
            "select rating, result_state from card_review_log where part_id = ? and op_id = ?",
            (part_id, payload.operation_id),
        ).fetchone()
        if prior is not None:
            if prior["rating"] != payload.rating:
                raise ConflictError(
                    "This review operation already recorded a different rating. "
                    "Retry the original rating to confirm it."
                )
            conn.rollback()
            return json.loads(str(prior["result_state"]))
        # Re-read under the lock: the deck must still be ready and the card must still
        # exist for this review to mean anything.
        artifact = artifacts.get_artifact(conn, int(part["artifact_id"]))
        if artifact["state"] != artifacts.READY:
            raise ConflictError(DECK_CHANGED_MESSAGE)
        row = conn.execute("select * from card_states where part_id = ?", (part_id,)).fetchone()
        if row is None:
            raise NotFoundError(NOT_A_CARD_MESSAGE)

        updated = scheduler.review(_state_from_row(row), payload.rating, datetime.now(UTC))
        conn.execute(
            "update card_states set due_at = ?, stability = ?, difficulty = ?, reps = ?, "
            "lapses = ?, state = ?, last_review_at = ? where part_id = ?",
            (
                scheduler.to_storage(updated.due_at),
                updated.stability,
                updated.difficulty,
                updated.reps,
                updated.lapses,
                updated.state,
                scheduler.to_storage(updated.last_review_at) if updated.last_review_at else None,
                part_id,
            ),
        )
        result = _state_json_from_state(updated)
        conn.execute(
            "insert into card_review_log (part_id, rating, op_id, result_state) "
            "values (?, ?, ?, ?)",
            (part_id, payload.rating, payload.operation_id, json.dumps(result)),
        )
        conn.commit()
        return result
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


@router.patch("/cards/{part_id}", response_model=None)
def update_card(part_id: int, payload: CardUpdate, conn: DbConn) -> dict[str, object]:
    """Correct a card's faces. Scheduling state is deliberately untouched: editing what
    a card says does not reset how well the student knows it."""
    part, _ = _require_card(conn, part_id)
    content = json.dumps({"front": payload.front, "back": payload.back, "topic": payload.topic})
    artifacts.set_part_content(
        conn, part_id, content, origin=artifacts.USER_CORRECTED, note="card edited"
    )
    return {"part_id": part_id, "card": json.loads(content)}


@router.delete("/cards/{part_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_card(part_id: int, conn: DbConn) -> None:
    """Delete one card; the cascade removes its scheduling state and review log."""
    _require_card(conn, part_id)
    artifacts.delete_part(conn, part_id)


def _question_json(part: dict[str, object]) -> dict[str, object]:
    """One quiz question with its full payload, answers included.

    Lyra is local and trusts the user; the interface, not the API, controls when the
    answer is revealed. The hidden grading contract is an exception to that fullness: it
    is generation metadata (PLA-496) that grades the answer, so it never leaves the store
    through a read shape - a client cannot see the reference it will be held out or steer
    the grader by editing it.
    """
    question = json.loads(str(part["content"]))
    if isinstance(question, dict):
        question.pop("grading", None)
    return {
        "part_id": part["id"],
        "ordinal": part["ordinal"],
        "label": part["label"],
        "question": question,
    }


@router.get("/quizzes/{artifact_id}", response_model=None)
def read_quiz(artifact_id: int, conn: DbConn) -> dict[str, object]:
    artifact = _require_quiz(conn, artifact_id)
    _require_ready(artifact, "quiz")
    parts = [
        part
        for part in artifacts.list_parts(conn, artifact_id)
        if part["kind"] == artifacts.QUIZ_QUESTION
    ]
    return {**artifact, "questions": [_question_json(part) for part in parts]}


def _quiz_question_part_ids(conn: sqlite3.Connection, artifact_id: int) -> list[int]:
    """The quiz's question part ids in stable document order: the attempt's question set."""
    return [
        int(part["id"])
        for part in artifacts.list_parts(conn, artifact_id)
        if part["kind"] == artifacts.QUIZ_QUESTION
    ]


def _answer_outcome(row: sqlite3.Row) -> str:
    """`correct`, `incorrect`, or `unresolved` for one recorded answer.

    `uncertain` and an in-flight marker are unresolved, not wrong: the student's words
    are stored and the judgment may yet settle. A legacy choice row is settled - its
    stored correctness is deterministic - but a legacy fill-blank miss never carried
    recoverable words and cannot be graded now, so it stays unresolved.
    """
    verdict = row["verdict"]
    if verdict in ("correct", "incorrect"):
        return verdict
    if int(row["grading_version"] or 0) == 0 and int(row["selected_index"]) >= 0:
        return "correct" if int(row["correct"]) else "incorrect"
    return "unresolved"


def _attempt_answers(conn: sqlite3.Connection, attempt_id: int) -> list[dict[str, object]]:
    """The answers recorded for an attempt: the chosen option, the student's own words
    where a free response was graded, and each answer's outcome.

    Deliberately not the answer key: a question the student has not answered leaks nothing
    here, so resuming an attempt cannot reveal an answer they have not yet earned (PLA-277).
    `response_text` is None for answers recorded before the layered grader (PLA-496),
    including a legacy fill-blank miss, which never carried a recoverable text.
    `uncertain` is the read name for *unresolved* - an unsettled judgment renders as a
    neutral comparison the student can retry, never as a confident wrong.
    """
    rows = conn.execute(
        "select part_id, selected_index, correct, verdict, response_text, grading_version "
        "from quiz_answers where attempt_id = ? order by part_id",
        (attempt_id,),
    ).fetchall()
    return [
        {
            "part_id": int(row["part_id"]),
            "selected_index": int(row["selected_index"]),
            "correct": bool(row["correct"]),
            "uncertain": _answer_outcome(row) == "unresolved",
            "response_text": row["response_text"],
        }
        for row in rows
    ]


def _attempt_payload(conn: sqlite3.Connection, attempt: sqlite3.Row) -> dict[str, object]:
    """The interface shape of an attempt: its fixed question order and recorded answers."""
    raw_ids = attempt["question_part_ids"]
    part_ids = json.loads(str(raw_ids)) if raw_ids else []
    count = attempt["question_count"]
    return {
        "attempt_id": int(attempt["id"]),
        "question_part_ids": part_ids,
        "question_count": int(count) if count is not None else len(part_ids),
        "answers": _attempt_answers(conn, int(attempt["id"])),
        "finished": attempt["finished_at"] is not None,
    }


@router.post("/quizzes/{artifact_id}/attempts", response_model=None)
def start_attempt(artifact_id: int, conn: DbConn, restart: bool = False) -> dict[str, object]:
    """Start or resume the one active attempt for a quiz (PLA-277).

    A new attempt is permitted only when the quiz is `ready` and its full question set is
    present; a pending, generating, failed, or partially materialized quiz cannot be
    attempted, and no attempt row is created for one. Under concurrent starts exactly one
    active attempt exists: a second start returns the same resumable attempt idempotently
    rather than starting over, so a reload or a duplicate request never loses progress or
    forks the score. `restart=true` is the explicit start-over - the current attempt is
    retained but marked abandoned, and a fresh attempt is opened.
    """
    try:
        conn.execute("begin immediate")
        quiz = _require_quiz(conn, artifact_id)
        part_ids = _quiz_question_part_ids(conn, artifact_id)
        total = quiz["problems_total"]
        # Validate the live question set under the same write transaction that snapshots
        # it, so regeneration cannot land between the readiness check and attempt creation.
        if quiz["state"] != artifacts.READY or not part_ids:
            raise ConflictError(QUIZ_NOT_READY_MESSAGE)
        if total is not None and len(part_ids) != int(total):
            raise ConflictError(QUIZ_NOT_READY_MESSAGE)
        active = conn.execute(
            "select * from quiz_attempts where artifact_id = ? and finished_at is null",
            (artifact_id,),
        ).fetchone()
        snapshot = (
            json.loads(str(active["question_part_ids"]))
            if active is not None and active["question_part_ids"] is not None
            else None
        )
        # An explicit restart, a legacy attempt with no snapshot, or an attempt against a
        # previous question set is retired so POST cannot resume answers onto regenerated
        # questions. Exact list equality protects membership and question order.
        if active is not None and (restart or snapshot != part_ids):
            conn.execute(
                "update quiz_attempts set finished_at = datetime('now'), abandoned = 1 "
                "where id = ?",
                (int(active["id"]),),
            )
            active = None
        if active is not None:
            payload = _attempt_payload(conn, active)
            conn.rollback()
            return payload
        attempt_id = int(
            conn.execute(
                "insert into quiz_attempts (artifact_id, question_count, question_part_ids) "
                "values (?, ?, ?)",
                (artifact_id, len(part_ids), json.dumps(part_ids)),
            ).lastrowid
            or 0
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # The one-active-attempt index rejected a race we lost; return the winner instead.
        if conn.in_transaction:
            conn.rollback()
        active = conn.execute(
            "select * from quiz_attempts where artifact_id = ? and finished_at is null",
            (artifact_id,),
        ).fetchone()
        if active is None:
            raise
        return _attempt_payload(conn, active)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    started = conn.execute("select * from quiz_attempts where id = ?", (attempt_id,)).fetchone()
    return _attempt_payload(conn, started)


@router.get("/quizzes/{artifact_id}/attempts/current", response_model=None)
def current_attempt(artifact_id: int, conn: DbConn) -> dict[str, object]:
    """The resumable attempt for a quiz, or none (PLA-277).

    The smallest read surface the interface needs to re-enter an unfinished attempt after
    a reload, navigation, or backend restart: the attempt's fixed question order and the
    answers already recorded, but never the answer key of a question not yet answered. An
    attempt whose quiz changed underneath it - its snapshot no longer matches the quiz's
    questions - is not offered for resume, so answers are never attached to a different
    question set.
    """
    _require_quiz(conn, artifact_id)
    active = conn.execute(
        "select * from quiz_attempts where artifact_id = ? and finished_at is null",
        (artifact_id,),
    ).fetchone()
    if active is None or active["question_part_ids"] is None:
        return {"attempt": None}
    snapshot = json.loads(str(active["question_part_ids"]))
    current_ids = _quiz_question_part_ids(conn, artifact_id)
    if snapshot != current_ids:
        return {"attempt": None}
    return {"attempt": _attempt_payload(conn, active)}


def _judge_for(conn: sqlite3.Connection) -> Callable[..., grading.GradingResult | None] | None:
    """The semantic judge for one answer, or None when it cannot run.

    The judge sends the question - generated from the student's own documents - to the
    configured endpoint, so the same consent gate that guards document text applies: no
    endpoint, or a remote endpoint the student has not acknowledged, leaves the judge out,
    and the layered pass then ends on a recorded `uncertain` rather than a confident wrong.
    """
    access = resolve_tutor_access(conn)
    if access.config is None or access.document_block is not None:
        return None
    config = access.config

    def judge(
        *,
        question: str,
        rubric: dict[str, object] | None,
        reference: str,
        response: str,
    ) -> grading.GradingResult | None:
        return grading.judge_free_response(
            config, question=question, rubric=rubric, reference=reference, response=response
        )

    return judge


def _answer_read(question: dict[str, object], result: grading.GradingResult) -> dict[str, object]:
    """The response shape the runner renders: the simple correct/incorrect reveal.

    `uncertain` is the one addition, and it is what the interface needs to render the
    neutral comparison instead of a confident wrong; it is never grading machinery, and
    the internal detail that produced it is stored on the row, not returned.
    """
    return {
        "correct": result.correct,
        "uncertain": result.uncertain,
        "correct_index": question["correct_index"],
        "explanation": question["explanation"],
    }


def _stored_answer_read(question: dict[str, object], row: sqlite3.Row) -> dict[str, object]:
    return {
        "correct": bool(row["correct"]),
        "uncertain": _answer_outcome(row) == "unresolved",
        "correct_index": question["correct_index"],
        "explanation": question["explanation"],
    }


# A bounded wait for another request grading the same submission: longer than any
# judgment can run (the judge times out at 120s), short enough that a dead owner cannot
# hold a retry hostage.
_WAIT_FOR_VERDICT_SECONDS = 150.0
# An in-flight marker older than this has lost its owner - a restart, a crash - and is
# reclaimable rather than waitable.
_STALE_CLAIM_SECONDS = 180.0


def _row_digest(row: sqlite3.Row) -> str | None:
    """The question digest the row's grading ran against, or None when unknown."""
    if row["grade_detail"] is None:
        return None
    try:
        detail = json.loads(str(row["grade_detail"]))
    except (TypeError, ValueError):
        return None
    if not isinstance(detail, dict):
        return None
    digest = detail.get("question_digest")
    return digest if isinstance(digest, str) else None


def _same_submission(row: sqlite3.Row, response_text: str | None, selected_index: int) -> bool:
    """Whether the row is this exact submission under the current grading contract."""
    return (
        int(row["grading_version"]) == grading.GRADING_VERSION
        and row["response_text"] is not None
        and row["response_text"] == response_text
        and int(row["selected_index"]) == selected_index
    )


def _replayable(row: sqlite3.Row, question_content: str) -> bool:
    """Whether a stored result may be replayed for this question's current content.

    Only a settled, non-`uncertain` verdict under the current grading contract, graded
    against this question's current content (a regenerated question regrades even an
    identical submission). A stored `uncertain` never replays: the judgment may have
    been a transient failure, and the retry is how it gets a second chance.
    """
    return (
        row["verdict"] is not None
        and row["verdict"] != "uncertain"
        and _row_digest(row) == grading.question_digest(question_content)
    )


def _marker_seconds_ago(row: sqlite3.Row) -> float | None:
    """How old the row's marker is, in seconds, or None when it cannot be read."""
    raw = row["answered_at"]
    if not raw:
        return None
    try:
        stored = datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (datetime.now(UTC) - stored).total_seconds()


def _claim_in_flight(
    conn: sqlite3.Connection,
    attempt: sqlite3.Row,
    part_id: int,
    question_content: str,
    response_text: str | None,
    selected_index: int,
    *,
    supersede: bool,
    previously_seen: sqlite3.Row | None = None,
) -> str | None:
    """Record that this submission is being graded, or report that one already is.

    One short `begin immediate` transaction revalidates the attempt and the question,
    then upserts the in-flight marker - verdict NULL under the current contract, stamped
    with a unique claim token - or tells the caller to wait rather than charge a second
    judgment. It returns the claim token on success and None when the caller should wait.

    Waiting happens when the row already carries this submission's own result: a fresh
    in-flight claim of this exact question (its judgment is running), a settled result
    for the question's current content (replayed by the wait), or a fresh `uncertain`
    this request raced into - `previously_seen` is the row this request read before
    claiming, and if it saw no row, or one still in flight, this request is a duplicate
    of the judgment that just published, not a deliberate retry of it, so it shares the
    terminal result. A marker older than the longest judgment has lost its owner and is
    reclaimed, so a restart cannot strand an answer forever.

    `supersede` is the ordering rule: a freshly arrived submission always supersedes
    what the row holds (an older in-flight judgment loses, its publish will no-op); a
    reclaim after a timed-out wait never supersedes a *different* submission, so an
    older slower judgment can never overwrite a newer answer.
    """
    try:
        conn.execute("begin immediate")
        live = conn.execute(
            "select finished_at, abandoned from quiz_attempts where id = ?",
            (int(attempt["id"]),),
        ).fetchone()
        if live is None or live["finished_at"] is not None or int(live["abandoned"] or 0) == 1:
            raise ConflictError(QUIZ_CHANGED_MESSAGE)
        part_now = conn.execute(
            "select artifact_id, kind, content from artifact_parts where id = ?",
            (part_id,),
        ).fetchone()
        if (
            part_now is None
            or int(part_now["artifact_id"]) != int(attempt["artifact_id"])
            or part_now["kind"] != artifacts.QUIZ_QUESTION
            or str(part_now["content"]) != question_content
        ):
            raise ConflictError(QUIZ_CHANGED_MESSAGE)
        row = conn.execute(
            "select * from quiz_answers where attempt_id = ? and part_id = ?",
            (int(attempt["id"]), part_id),
        ).fetchone()
        current_digest = grading.question_digest(question_content)
        if row is not None:
            if _same_submission(row, response_text, selected_index):
                age = _marker_seconds_ago(row) or 0.0
                if row["verdict"] is None:
                    if age < _STALE_CLAIM_SECONDS and _row_digest(row) == current_digest:
                        # A fresh claim of this exact question owns the row: wait (or,
                        # on a reclaim, refuse - a live claim is never stolen).
                        conn.rollback()
                        return None
                    # A stale marker (the owner vanished) or a claim graded against
                    # different content: regrade.
                elif _replayable(row, question_content):
                    # Already settled against this question's current content: replay
                    # it. Wait returns the stored read; do not overwrite and regrade.
                    conn.rollback()
                    return None
                elif (
                    row["verdict"] == "uncertain"
                    and age < _STALE_CLAIM_SECONDS
                    and supersede
                    and (previously_seen is None or previously_seen["verdict"] is None)
                ):
                    # This request read no row, or one still in flight: it is a
                    # duplicate racing the judgment that just published its failure,
                    # not a deliberate retry. Wait shares the terminal result.
                    conn.rollback()
                    return None
                # A deliberate retry of an `uncertain` (the request saw it settled), a
                # stale result of any kind, or a settled result graded against different
                # content: regrade.
            elif not supersede and int(row["grading_version"]) == grading.GRADING_VERSION:
                # A different submission owns the row: this one is the older, slower
                # judgment, and it does not get to write over a newer answer.
                raise ConflictError(QUIZ_CHANGED_MESSAGE)
        token = secrets.token_hex(16)
        conn.execute(
            "insert into quiz_answers "
            "(attempt_id, part_id, selected_index, correct, response_text, verdict, "
            "grade_detail, grading_version) "
            "values (?, ?, ?, 0, ?, NULL, ?, ?) "
            "on conflict (attempt_id, part_id) do update set "
            "selected_index = excluded.selected_index, correct = 0, "
            "response_text = excluded.response_text, verdict = NULL, "
            "grade_detail = excluded.grade_detail, "
            "grading_version = excluded.grading_version, "
            "answered_at = datetime('now')",
            (
                int(attempt["id"]),
                part_id,
                selected_index,
                response_text,
                json.dumps(
                    {
                        "question_digest": grading.question_digest(question_content),
                        "claim_token": token,
                    },
                    ensure_ascii=False,
                ),
                grading.GRADING_VERSION,
            ),
        )
        conn.commit()
        return token
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _wait_for_verdict(
    conn: sqlite3.Connection,
    attempt: sqlite3.Row,
    part_id: int,
    response_text: str | None,
    selected_index: int,
    question: dict[str, object],
    question_content: str,
) -> dict[str, object] | None:
    """The bounded single-flight: the terminal result this submission's claim carries,
    or None when this claim can no longer own the row.

    Waiters for the same in-flight claim share its terminal result - a settled verdict
    or an `uncertain` one, which an explicit retry may later regrade. Every poll
    revalidates what it would otherwise return against: the attempt still live, the
    question still carrying the content it was claimed against, and the row still
    holding this exact submission under the current grading contract. A finish, a
    restart, a regeneration, or a newer distinct answer each stops the wait at once
    (within one poll), so a duplicate never sleeps out the full bound when the state
    that would have been returned is already known gone. None sends the route to the
    reclaim, which revalidates under lock and conflicts or regrades.
    """
    attempt_id = int(attempt["id"])
    current_digest = grading.question_digest(question_content)
    deadline = time.monotonic() + _WAIT_FOR_VERDICT_SECONDS
    while True:
        live = conn.execute(
            "select finished_at, abandoned from quiz_attempts where id = ?",
            (attempt_id,),
        ).fetchone()
        if live is None or live["finished_at"] is not None or int(live["abandoned"] or 0) == 1:
            return None  # The attempt is gone: stop, and let the reclaim conflict.
        part_now = conn.execute(
            "select artifact_id, kind, content from artifact_parts where id = ?",
            (part_id,),
        ).fetchone()
        if (
            part_now is None
            or int(part_now["artifact_id"]) != int(attempt["artifact_id"])
            or part_now["kind"] != artifacts.QUIZ_QUESTION
            or str(part_now["content"]) != question_content
        ):
            return None  # The question changed or left the quiz: stop.
        row = conn.execute(
            "select * from quiz_answers where attempt_id = ? and part_id = ?",
            (attempt_id, part_id),
        ).fetchone()
        if row is None or int(row["grading_version"]) != grading.GRADING_VERSION:
            return None
        if row["response_text"] != response_text or int(row["selected_index"]) != selected_index:
            return None  # A newer distinct answer took the row: superseded.
        if row["verdict"] is not None:
            if _row_digest(row) == current_digest:
                # Terminal for this question's current content - settled or uncertain:
                # share it. An explicit retry may grade again later.
                return _stored_answer_read(question, row)
            return None  # Settled against different content: stale, reclaim regrades.
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


@router.post("/attempts/{attempt_id}/answers", response_model=None)
def answer_question(attempt_id: int, payload: AnswerCreate, conn: DbConn) -> dict[str, object]:
    """Grade one answer and record it (PLA-277, PLA-496).

    Short transactions only, in order: a read that finds the attempt and the question;
    a claim that marks the submission in flight; the grading itself - a bounded algebra
    subprocess and, for answers no layer settles, one provider call - which runs with no
    transaction held; and a publish that revalidates everything it is about to commit.
    A result that lands late after a restart or a regeneration is refused, and a newer
    distinct submission that took the row in the meantime makes the older one a no-op, so
    nothing a judgment saw while in flight can contaminate what the attempt became.

    A resubmission already settled against the current grading contract - and the
    question's current content - replays its stored result: no regrade, no second
    semantic judgment. A stored `uncertain` never replays, so a transient failure can
    be retried without a new attempt. A second in-flight grading of the same submission
    waits for the first's published result - settled or `uncertain` - instead of
    charging the judge twice. The claim the first request took carries a unique token
    in its marker; a superseded or reclaimed judgment must present that token to
    publish, so an older slow judgment can never land in a newer claim's row.
    """
    # Read. The replay fast path below returns a stored result, so the attempt must be
    # validated live before any read is served - a finished or abandoned attempt has
    # no current answers to replay.
    attempt = conn.execute("select * from quiz_attempts where id = ?", (attempt_id,)).fetchone()
    if attempt is None:
        raise NotFoundError(NOT_AN_ATTEMPT_MESSAGE)
    if attempt["finished_at"] is not None:
        raise ConflictError(ATTEMPT_FINISHED_MESSAGE)
    if int(attempt["abandoned"] or 0) == 1:
        raise ConflictError(QUIZ_CHANGED_MESSAGE)
    snapshot = (
        json.loads(str(attempt["question_part_ids"])) if attempt["question_part_ids"] else None
    )
    if snapshot is not None and payload.part_id not in snapshot:
        raise NotFoundError(NOT_THIS_QUIZ_MESSAGE)
    part = artifacts.get_part(conn, payload.part_id)
    if (
        int(part["artifact_id"]) != int(attempt["artifact_id"])
        or part["kind"] != artifacts.QUIZ_QUESTION
    ):
        raise NotFoundError(NOT_THIS_QUIZ_MESSAGE)
    question = json.loads(str(part["content"]))
    content = str(part["content"])

    # What gets stored: the student's raw words, or the chosen option's text where a
    # legacy choice client does not send them.
    if str(question.get("type")) == "fill_blank":
        if payload.response_text is None:
            raise UnprocessableError("A typed answer needs the student's words.")
        stored_text = payload.response_text
        selected_index = -1
    else:
        stored_text = payload.response_text
        if stored_text is None:
            options = question.get("options")
            if isinstance(options, list) and 0 <= payload.selected_index < len(options):
                option = options[payload.selected_index]
                stored_text = option if isinstance(option, str) and option.strip() else None
        selected_index = payload.selected_index

    # A submission already settled against this question's current content replays its
    # stored result rather than grading again.
    existing = conn.execute(
        "select * from quiz_answers where attempt_id = ? and part_id = ?",
        (attempt_id, payload.part_id),
    ).fetchone()
    if (
        existing is not None
        and _same_submission(existing, stored_text, selected_index)
        and _replayable(existing, content)
    ):
        return _stored_answer_read(question, existing)

    # Single-flight: an identical submission that is already being graded owns the row;
    # a bounded wait returns its result instead of charging a second judgment. The
    # claim token the marker carries is what the publish below must present, so a
    # superseded judgment can never land in a newer claim's pending row.
    token = _claim_in_flight(
        conn,
        attempt,
        payload.part_id,
        content,
        stored_text,
        selected_index,
        supersede=True,
        previously_seen=existing,
    )
    if token is None:
        waited = _wait_for_verdict(
            conn, attempt, payload.part_id, stored_text, selected_index, question, content
        )
        if waited is not None:
            return waited
        # The wait ended without a terminal result: the owner died, or the state moved.
        # Reclaim and grade - but never over a submission that took the row in the
        # meantime (which conflicts, not regrades).
        token = _claim_in_flight(
            conn,
            attempt,
            payload.part_id,
            content,
            stored_text,
            selected_index,
            supersede=False,
            previously_seen=existing,
        )
        if token is None:
            raise ConflictError(QUIZ_CHANGED_MESSAGE)

    # Grade, outside any write transaction.
    if str(question.get("type")) == "fill_blank":
        result = grading.grade_free_response(question, stored_text, judge=_judge_for(conn))
    else:
        result = grading.grade_choice(question, selected_index)
    detail = dict(result.detail)
    detail["question_digest"] = grading.question_digest(content)

    # Publish: the result lands only while this claim still holds the row - the marker
    # still carries this claim's token - the attempt is still the same live attempt,
    # and the question still carries the content it was graded against.
    try:
        conn.execute("begin immediate")
        live = conn.execute(
            "select finished_at, abandoned from quiz_attempts where id = ?",
            (attempt_id,),
        ).fetchone()
        if live is None or live["finished_at"] is not None or int(live["abandoned"] or 0) == 1:
            raise ConflictError(QUIZ_CHANGED_MESSAGE)
        part_now = conn.execute(
            "select artifact_id, kind, content from artifact_parts where id = ?",
            (payload.part_id,),
        ).fetchone()
        if (
            part_now is None
            or int(part_now["artifact_id"]) != int(attempt["artifact_id"])
            or part_now["kind"] != artifacts.QUIZ_QUESTION
            or str(part_now["content"]) != content
        ):
            raise ConflictError(QUIZ_CHANGED_MESSAGE)
        updated = conn.execute(
            "update quiz_answers set selected_index = ?, correct = ?, verdict = ?, "
            "grade_detail = ?, grading_version = ?, answered_at = datetime('now') "
            "where attempt_id = ? and part_id = ? and grading_version = ? "
            "and verdict is null and response_text is ? and selected_index = ? "
            "and json_extract(grade_detail, '$.claim_token') = ?",
            (
                selected_index,
                int(result.correct),
                result.verdict,
                json.dumps(detail, ensure_ascii=False),
                grading.GRADING_VERSION,
                attempt_id,
                payload.part_id,
                grading.GRADING_VERSION,
                stored_text,
                selected_index,
                token,
            ),
        )
        if updated.rowcount != 1:
            # The claim moved on: a newer submission took the row, or the attempt no
            # longer matches. The result is discarded, not written.
            raise ConflictError(QUIZ_CHANGED_MESSAGE)
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return _answer_read(question, result)


def _score_attempt(conn: sqlite3.Connection, attempt: sqlite3.Row) -> dict[str, object]:
    """Score an attempt over its fixed question set, per topic: the weakness surface.

    Only settled answers count: `correct` into the score, `incorrect` into the totals,
    and unresolved answers - an `uncertain` verdict, an in-flight judgment, or a legacy
    fill-blank miss with no recoverable words - into a separate `unresolved` tally, so an
    unsettled judgment can never become a confident wrong or a confident weakness. The
    snapshot denominator is the quiz's true question count (PLA-277), so an incomplete
    submission is represented honestly rather than a smaller quiz reported as complete. A
    legacy attempt with no stored snapshot is scored over the answers it holds, so its
    result shape only gains the `unresolved` tally.
    """
    attempt_id = int(attempt["id"])
    answers = {
        int(row["part_id"]): row
        for row in conn.execute(
            "select part_id, correct, verdict, grading_version, selected_index "
            "from quiz_answers where attempt_id = ?",
            (attempt_id,),
        )
    }
    score = 0
    unresolved = 0
    raw_snapshot = attempt["question_part_ids"]
    by_topic: dict[str, dict[str, int]] = {}

    def _topic_entry(topic: str) -> dict[str, int]:
        return by_topic.setdefault(
            topic, {"topic": topic, "correct": 0, "total": 0, "unresolved": 0}
        )

    if raw_snapshot is not None:
        snapshot = json.loads(str(raw_snapshot))
        count = attempt["question_count"]
        total = int(count) if count is not None else len(snapshot)
        for part_id in snapshot:
            try:
                part = artifacts.get_part(conn, part_id)
            except NotFoundError:
                continue
            topic = str(json.loads(str(part["content"])).get("topic") or "General")
            entry = _topic_entry(topic)
            answer = answers.get(part_id)
            if answer is None:
                # An unanswered question keeps the honest denominator (PLA-277): it
                # counts toward the topic's total, never toward the score.
                entry["total"] += 1
                continue
            # An answered, settled question counts as before; an unsettled one is
            # reported separately and never becomes a wrong one or a confident weakness.
            outcome = _answer_outcome(answer)
            if outcome == "correct":
                entry["correct"] += 1
                entry["total"] += 1
                score += 1
            elif outcome == "incorrect":
                entry["total"] += 1
            else:
                entry["unresolved"] += 1
                unresolved += 1
    else:
        rows = conn.execute(
            "select qa.correct, qa.verdict, qa.grading_version, qa.selected_index, p.content "
            "from quiz_answers qa join artifact_parts p on p.id = qa.part_id "
            "where qa.attempt_id = ?",
            (attempt_id,),
        ).fetchall()
        total = 0
        for row in rows:
            topic = str(json.loads(str(row["content"])).get("topic") or "General")
            entry = _topic_entry(topic)
            outcome = _answer_outcome(row)
            if outcome == "correct":
                entry["correct"] += 1
                entry["total"] += 1
                score += 1
            elif outcome == "incorrect":
                entry["total"] += 1
            else:
                entry["unresolved"] += 1
                unresolved += 1
        total = len(rows)
    return {
        "score": score,
        "total": total,
        "unresolved": unresolved,
        "answered": len(answers),
        "by_topic": sorted(by_topic.values(), key=lambda entry: str(entry["topic"])),
    }


@router.post("/attempts/{attempt_id}/finish", response_model=None)
def finish_attempt(attempt_id: int, conn: DbConn) -> dict[str, object]:
    """Close an attempt and score it, idempotently (PLA-277).

    Scoring and the finish write share one `begin immediate` transaction, so an answer
    that committed before this cannot be omitted from the result. Unresolved answers are
    counted separately, never as wrong. The result is stored on the attempt, so a finish
    whose HTTP response was lost can be retried after a reload or restart and returns the
    same stored score without double-counting weakness or scheduling data.
    """
    try:
        conn.execute("begin immediate")
        attempt = conn.execute("select * from quiz_attempts where id = ?", (attempt_id,)).fetchone()
        if attempt is None:
            raise NotFoundError(NOT_AN_ATTEMPT_MESSAGE)
        if attempt["finished_at"] is not None:
            stored = attempt["result"]
            result = json.loads(str(stored)) if stored else _score_attempt(conn, attempt)
            conn.rollback()
            return result
        result = _score_attempt(conn, attempt)
        conn.execute(
            "update quiz_attempts set finished_at = datetime('now'), result = ? where id = ?",
            (json.dumps(result), attempt_id),
        )
        conn.commit()
        return result
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _cancel_study(artifact: dict[str, object], conn: sqlite3.Connection) -> dict[str, object]:
    """Cancel live work atomically; repeated cancellation returns its settled state."""
    artifact_id = int(artifact["id"])
    conn.execute("begin immediate")
    try:
        conn.execute(
            "update artifacts set state = ?, stage_detail = NULL, updated_at = datetime('now') "
            "where id = ? and state in (?, ?) and kind in (?, ?)",
            (
                artifacts.CANCELLED,
                artifact_id,
                artifacts.PENDING,
                artifacts.GENERATING,
                *study.STUDY_KINDS,
            ),
        )
        settled = artifacts.get_artifact(conn, artifact_id)
        if settled["state"] != artifacts.CANCELLED:
            raise ConflictError(f"{NOT_RUNNING_MESSAGE} Current state: {settled['state']}.")
        conn.commit()
        return settled
    except Exception:
        conn.rollback()
        raise


@router.patch("/decks/{artifact_id}", response_model=None)
def rename_deck(artifact_id: int, payload: StudyRename, conn: DbConn) -> dict[str, object]:
    _require_deck(conn, artifact_id)
    return artifacts.rename_artifact(conn, artifact_id, payload.title)


@router.post("/decks/{artifact_id}/cancel", response_model=None)
def cancel_deck(artifact_id: int, conn: DbConn) -> dict[str, object]:
    return _cancel_study(_require_deck(conn, artifact_id), conn)


@router.delete("/decks/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_deck(artifact_id: int, conn: DbConn) -> None:
    _require_deck(conn, artifact_id)
    artifacts.delete_artifact(conn, artifact_id)


@router.patch("/quizzes/{artifact_id}", response_model=None)
def rename_quiz(artifact_id: int, payload: StudyRename, conn: DbConn) -> dict[str, object]:
    _require_quiz(conn, artifact_id)
    return artifacts.rename_artifact(conn, artifact_id, payload.title)


@router.post("/quizzes/{artifact_id}/cancel", response_model=None)
def cancel_quiz(artifact_id: int, conn: DbConn) -> dict[str, object]:
    return _cancel_study(_require_quiz(conn, artifact_id), conn)


@router.delete("/quizzes/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_quiz(artifact_id: int, conn: DbConn) -> None:
    _require_quiz(conn, artifact_id)
    artifacts.delete_artifact(conn, artifact_id)


@router.get("/decks/{artifact_id}/status", response_model=StudyStatusRead)
def deck_status(artifact_id: int, conn: DbConn) -> dict[str, object]:
    """The polled generation state. Skinny on purpose: the panel polls it."""
    return _require_deck(conn, artifact_id)


@router.get("/quizzes/{artifact_id}/status", response_model=StudyStatusRead)
def quiz_status(artifact_id: int, conn: DbConn) -> dict[str, object]:
    return _require_quiz(conn, artifact_id)
