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
import sqlite3
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field, field_validator

from backend.core import artifacts, scheduler, study
from backend.core.classes import get_class
from backend.core.errors import ConflictError, NotFoundError
from backend.llm.prompts import QUIZ_QUESTION_TYPES
from backend.storage.database import get_db

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

    Any index other than the stored `correct_index` grades incorrect, which is also how
    a fill_blank answer that matched nothing arrives: the runner sends -1.
    """

    part_id: int
    selected_index: int


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
    return created, ready, real_job


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
    assert job is not None
    study.enqueue(job)
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
    assert job is not None
    study.enqueue(job)
    return created


@router.get("/classes/{class_id}/study", response_model=None)
def list_study(class_id: int, conn: DbConn) -> dict[str, object]:
    """Decks and quizzes for the hub panel; decks carry their bucket and due counts."""
    get_class(conn, class_id)
    rows = conn.execute(
        "select id, kind, title, state, stage_detail, problems_total, problems_done, "
        "error_message, created_at, updated_at from artifacts "
        "where class_id = ? and kind in (?, ?) order by updated_at desc",
        (class_id, artifacts.KIND_FLASHCARD_DECK, artifacts.KIND_QUIZ),
    ).fetchall()

    decks: list[dict[str, object]] = []
    quizzes: list[dict[str, object]] = []
    now = scheduler.to_storage(datetime.now(UTC))
    for row in rows:
        entry = dict(row)
        if row["kind"] == artifacts.KIND_FLASHCARD_DECK:
            entry.update(_deck_counts(conn, int(row["id"]), now))
            decks.append(entry)
        else:
            quizzes.append(entry)
    return {"decks": decks, "quizzes": quizzes}


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
            "select result_state from card_review_log where part_id = ? and op_id = ?",
            (part_id, payload.operation_id),
        ).fetchone()
        if prior is not None:
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
    answer is revealed.
    """
    return {
        "part_id": part["id"],
        "ordinal": part["ordinal"],
        "label": part["label"],
        "question": json.loads(str(part["content"])),
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


def _attempt_answers(conn: sqlite3.Connection, attempt_id: int) -> list[dict[str, object]]:
    """The answers recorded for an attempt: which option was chosen and whether it was right.

    Deliberately not the answer key: a question the student has not answered leaks nothing
    here, so resuming an attempt cannot reveal an answer they have not yet earned (PLA-277).
    """
    rows = conn.execute(
        "select part_id, selected_index, correct from quiz_answers "
        "where attempt_id = ? order by part_id",
        (attempt_id,),
    ).fetchall()
    return [
        {
            "part_id": int(row["part_id"]),
            "selected_index": int(row["selected_index"]),
            "correct": bool(row["correct"]),
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


@router.post("/attempts/{attempt_id}/answers", response_model=None)
def answer_question(attempt_id: int, payload: AnswerCreate, conn: DbConn) -> dict[str, object]:
    """Grade one answer against the stored payload and record it, transactionally (PLA-277).

    The read of the attempt, the membership check, and the write share one `begin
    immediate` transaction, so a just-submitted answer cannot race the attempt's finish:
    an answer to a finished attempt is refused, and an answer that lands commits before a
    finish can read the answers. The question must belong to this attempt's fixed set, so
    an answer can never attach to a question from a regenerated quiz.
    """
    try:
        conn.execute("begin immediate")
        attempt = conn.execute("select * from quiz_attempts where id = ?", (attempt_id,)).fetchone()
        if attempt is None:
            raise NotFoundError(NOT_AN_ATTEMPT_MESSAGE)
        if attempt["finished_at"] is not None:
            raise ConflictError(ATTEMPT_FINISHED_MESSAGE)
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
        correct = payload.selected_index == int(question["correct_index"])
        conn.execute(
            "insert into quiz_answers (attempt_id, part_id, selected_index, correct) "
            "values (?, ?, ?, ?) "
            "on conflict (attempt_id, part_id) do update set "
            "selected_index = excluded.selected_index, correct = excluded.correct, "
            "answered_at = datetime('now')",
            (attempt_id, payload.part_id, payload.selected_index, int(correct)),
        )
        conn.commit()
        return {
            "correct": correct,
            "correct_index": question["correct_index"],
            "explanation": question["explanation"],
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _score_attempt(conn: sqlite3.Connection, attempt: sqlite3.Row) -> dict[str, object]:
    """Score an attempt over its fixed question set, per topic: the weakness surface.

    The denominator is the quiz's true question count (PLA-277), so an incomplete
    submission is represented honestly - unanswered questions count toward the total but
    not the score - rather than a smaller quiz reported as complete. A legacy attempt with
    no stored snapshot is scored exactly as before, over the answers it holds, so its
    result shape does not change.
    """
    attempt_id = int(attempt["id"])
    answers = {
        int(row["part_id"]): row
        for row in conn.execute(
            "select part_id, correct from quiz_answers where attempt_id = ?", (attempt_id,)
        )
    }
    score = sum(int(row["correct"]) for row in answers.values())
    raw_snapshot = attempt["question_part_ids"]
    by_topic: dict[str, dict[str, int]] = {}
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
            entry = by_topic.setdefault(topic, {"topic": topic, "correct": 0, "total": 0})
            entry["total"] += 1
            answer = answers.get(part_id)
            if answer is not None:
                entry["correct"] += int(answer["correct"])
    else:
        rows = conn.execute(
            "select qa.correct, p.content from quiz_answers qa "
            "join artifact_parts p on p.id = qa.part_id where qa.attempt_id = ?",
            (attempt_id,),
        ).fetchall()
        total = len(rows)
        for row in rows:
            topic = str(json.loads(str(row["content"])).get("topic") or "General")
            entry = by_topic.setdefault(topic, {"topic": topic, "correct": 0, "total": 0})
            entry["correct"] += int(row["correct"])
            entry["total"] += 1
    return {
        "score": score,
        "total": total,
        "answered": len(answers),
        "by_topic": sorted(by_topic.values(), key=lambda entry: str(entry["topic"])),
    }


@router.post("/attempts/{attempt_id}/finish", response_model=None)
def finish_attempt(attempt_id: int, conn: DbConn) -> dict[str, object]:
    """Close an attempt and score it, idempotently (PLA-277).

    Scoring and the finish write share one `begin immediate` transaction, so an answer
    that committed before this cannot be omitted from the result. The result is stored on
    the attempt, so a finish whose HTTP response was lost can be retried after a reload or
    restart and returns the same stored score without double-counting weakness or
    scheduling data.
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
    """Stop a queued or running study generation, keeping anything already written."""
    if artifact["state"] not in (artifacts.PENDING, artifacts.GENERATING):
        raise ConflictError(NOT_RUNNING_MESSAGE)
    artifacts.set_artifact_state(conn, int(artifact["id"]), artifacts.CANCELLED)
    return artifacts.get_artifact(conn, int(artifact["id"]))


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
