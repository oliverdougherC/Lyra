"""Study tools: flashcard decks and quizzes generated from a class's own documents.

Both kinds are artifacts (migration 016), and both are produced by the same background
shape as ingestion and solving: an in-memory queue, one worker thread, state on the
artifact row, and a status endpoint the panel polls. A deck is generated in two phases -
one call maps the source material to a handful of topics, then each topic gets a
retrieval pass of its own and one constrained-JSON call writes its cards - because a
single whole-course call writes cards about whatever the model remembers rather than
what the course says. A quiz is one constrained-JSON call over the gathered material,
with the per-type rules enforced in code when the reply is parsed: the model's output is
a proposal, never trusted by construction.

Generated content is proposed, never asserted: cards and questions land as ordinary
parts, so the existing revision, provenance, and correction machinery applies to them
unchanged.
"""

import asyncio
import json
import logging
import queue
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

from backend.core import artifacts, scheduler
from backend.core.app_settings import (
    NO_ENDPOINT,
    REMOTE_UNACKNOWLEDGED,
    TutorConfig,
    document_text_allowed,
    resolve_tutor_config,
)
from backend.core.errors import LyraError, NotFoundError
from backend.core.scheduler import new_card_state
from backend.llm import client, prompts
from backend.rag.retrieve import retrieve
from backend.rag.tokens import estimate_tokens
from backend.storage.database import connect

logger = logging.getLogger(__name__)

# Per-document and whole-class reading caps for the topic-mapping pass, in estimated
# tokens. The per-document cap is what stops a 600-page textbook from crowding the
# syllabus out of the mapping; the total keeps the prompt inside the context window.
DOCUMENT_TOKEN_CAP = 6_000
TOTAL_TOKEN_CAP = 12_000

# Retrieval budget per topic, in estimated tokens. A topic's cards are written against
# what the course says about that topic, not against the whole gathered input.
TOPIC_RETRIEVAL_BUDGET = 2_500

INTERRUPTED_MESSAGE = "Interrupted, please retry"

# Why generation may not send course text, said in the words the student needs to act
# on. The rule itself lives in `app_settings.document_text_allowed`.
BLOCKED_MESSAGES = {
    NO_ENDPOINT: "No tutor endpoint is configured. Add one in Settings, then generate.",
    REMOTE_UNACKNOWLEDGED: (
        "Your tutor endpoint is not on this machine, and generation has to send it your "
        "course material. Allow that in Settings, then generate."
    ),
}

NO_TOPICS_MESSAGE = "The course material could not be mapped into study topics."
NO_CARDS_MESSAGE = "No flashcards could be generated from this material."
NO_QUESTIONS_MESSAGE = "Fewer than three valid questions survived validation."

STUDY_KINDS: tuple[str, ...] = (artifacts.KIND_FLASHCARD_DECK, artifacts.KIND_QUIZ)


class _GenerationCancelledError(Exception):
    """The artifact was cancelled while the worker was running and should be left alone."""


@dataclass(frozen=True)
class _Job:
    """What a queued generation needs. Ids and options only, never rows or handles."""

    artifact_id: int
    cards_per_topic: int = 4
    count: int = 10
    difficulty: str = "intermediate"
    types: tuple[str, ...] = field(default=prompts.QUIZ_QUESTION_TYPES)


_queue: queue.Queue[_Job] = queue.Queue()
_worker_lock = threading.Lock()
_worker_started = False


def enqueue(job: _Job) -> None:
    """Queue a deck or quiz for generation."""
    _queue.put(job)


def start_worker() -> None:
    """Start the single generation worker, once per process.

    One worker deliberately: a study generation is a run of model calls, and two decks
    generating at once would interleave those calls on one endpoint and double the time
    either takes. Waiting turns are short and rare.
    """
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_drain_queue, name="lyra-study", daemon=True).start()


def _drain_queue() -> None:
    """Run jobs until the process exits. The worker must never die."""
    while True:
        job = _queue.get()
        try:
            run_generation(job)
        except Exception:
            logger.exception("Study generation failed for artifact %s", job.artifact_id)
        finally:
            _queue.task_done()


def reconcile_interrupted(conn: sqlite3.Connection) -> int:
    """Fail every study artifact the last shutdown caught. Returns how many.

    Unlike the solver, a study job cannot be requeued: its options (card counts, quiz
    shape) live only in the in-memory job, so a `pending` row the process never started
    cannot be resumed either. Both pending and generating are failed with the same
    wording the solver's reconcile uses; a retry is the student asking again.
    """
    cursor = conn.execute(
        # `stage_detail` reads the pre-update row, so it keeps the lost stage.
        "update artifacts set stage_detail = state, state = ?, error_message = ?, "
        "updated_at = datetime('now') where state in (?, ?) and kind in (?, ?)",
        (
            artifacts.FAILED,
            INTERRUPTED_MESSAGE,
            artifacts.PENDING,
            artifacts.GENERATING,
            *STUDY_KINDS,
        ),
    )
    conn.commit()
    return cursor.rowcount


def run_generation(job: _Job) -> None:
    """Generate one deck or quiz. The worker calls this; tests call it directly."""
    conn = connect()
    try:
        artifact = artifacts.get_artifact(conn, job.artifact_id)
        if _cancelled(conn, job.artifact_id):
            return
        if artifact["kind"] == artifacts.KIND_FLASHCARD_DECK:
            _generate_deck(conn, job)
        else:
            _generate_quiz(conn, job)
    except NotFoundError:
        # Deleted between enqueue and run: the de-facto cancel, as in ingestion.
        logger.info("Study artifact %s vanished before generation", job.artifact_id)
    except _GenerationCancelledError:
        logger.info("Study artifact %s stopped: cancelled", job.artifact_id)
    except Exception as exc:
        conn.rollback()
        _mark_failed(conn, job.artifact_id, exc)
    finally:
        conn.close()


def _mark_failed(conn: sqlite3.Connection, artifact_id: int, exc: Exception) -> None:
    """Record the failure on the artifact row, keeping the stage it died in."""
    row = conn.execute("select state from artifacts where id = ?", (artifact_id,)).fetchone()
    if row is None:
        return
    message = exc.message if isinstance(exc, LyraError) else str(exc)
    artifacts.mark_artifact_failed(conn, artifact_id, str(row["state"]), message)


def _generate_deck(conn: sqlite3.Connection, job: _Job) -> None:
    """Map the sources to topics, then write each topic's cards against retrieval."""
    artifact = artifacts.get_artifact(conn, job.artifact_id)
    class_id = int(artifact["class_id"])
    blocked = document_text_allowed(conn)
    if blocked is not None:
        raise LyraError(BLOCKED_MESSAGES.get(blocked, BLOCKED_MESSAGES[NO_ENDPOINT]))
    config = resolve_tutor_config(conn)

    _raise_if_cancelled(conn, job.artifact_id)
    artifacts.set_artifact_state(
        conn, job.artifact_id, artifacts.GENERATING, "Reading the material"
    )
    gathered = _gather_source_text(conn, job.artifact_id)
    if not gathered:
        raise LyraError(NO_TOPICS_MESSAGE)

    _raise_if_cancelled(conn, job.artifact_id)
    artifacts.set_artifact_state(
        conn, job.artifact_id, artifacts.GENERATING, "Mapping study topics"
    )
    topics = _call_json(config, prompts.build_topics_prompt(gathered), prompts.TOPICS_SCHEMA)
    _raise_if_cancelled(conn, job.artifact_id)
    topic_names = [t.strip() for t in _json_list(topics, "topics") if str(t).strip()]
    if not topic_names:
        raise LyraError(NO_TOPICS_MESSAGE)

    artifacts.set_problems_total(conn, job.artifact_id, len(topic_names))
    artifacts.set_problems_done(conn, job.artifact_id, 0)

    cards_written = 0
    failed: list[str] = []
    ordinal = 0
    for topic in topic_names:
        _raise_if_cancelled(conn, job.artifact_id)
        artifacts.set_artifact_state(conn, job.artifact_id, artifacts.GENERATING, topic)
        try:
            written = _write_topic_cards(conn, job, config, class_id, topic, ordinal)
        except Exception:
            # One topic's call failing must not sink the deck: the student keeps what
            # the other topics produced, and the failure count says what was lost.
            logger.exception("Card generation failed for topic %r", topic)
            failed.append(topic)
        else:
            ordinal += written
            cards_written += written
        artifacts.increment_problems_done(conn, job.artifact_id)

    if cards_written == 0:
        raise LyraError(NO_CARDS_MESSAGE)

    detail = f"{len(failed)} of {len(topic_names)} topics failed" if failed else None
    _raise_if_cancelled(conn, job.artifact_id)
    artifacts.set_artifact_state(conn, job.artifact_id, artifacts.READY, detail)


def _write_topic_cards(
    conn: sqlite3.Connection,
    job: _Job,
    config: TutorConfig,
    class_id: int,
    topic: str,
    first_ordinal: int,
) -> int:
    """One topic's retrieval, one call, and the cards it produced. Returns the count."""
    result = retrieve(conn, class_id, topic, TOPIC_RETRIEVAL_BUDGET)
    context_block = prompts.format_context_block([vars(chunk) for chunk in result.chunks])
    reply = _call_json(
        config,
        prompts.build_flashcards_prompt(topic, context_block, job.cards_per_topic),
        prompts.FLASHCARDS_SCHEMA,
    )
    _raise_if_cancelled(conn, job.artifact_id)

    written = 0
    for card in _json_list(reply, "cards"):
        if not isinstance(card, dict):
            continue
        front = str(card.get("front") or "").strip()
        back = str(card.get("back") or "").strip()
        if not front or not back:
            continue
        payload = json.dumps({"front": front, "back": back, "topic": topic})
        part_id = artifacts.create_part(
            conn,
            job.artifact_id,
            artifacts.CARD,
            first_ordinal + written + 1,
            label=topic,
            content=payload,
            content_type=artifacts.JSON,
            status=artifacts.PART_COMPLETE,
        )
        _insert_card_state(conn, part_id)
        _record_card_provenance(conn, part_id, topic, result.chunks)
        written += 1
    return written


def _insert_card_state(conn: sqlite3.Connection, part_id: int) -> None:
    """Every card starts scheduling life as new, due immediately."""
    state = new_card_state(datetime.now(UTC))
    conn.execute(
        "insert into card_states "
        "(part_id, due_at, stability, difficulty, reps, lapses, state, last_review_at) "
        "values (?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            part_id,
            scheduler.to_storage(state.due_at),
            state.stability,
            state.difficulty,
            state.reps,
            state.lapses,
            state.state,
        ),
    )
    conn.commit()


def _record_card_provenance(
    conn: sqlite3.Connection, part_id: int, topic: str, chunks: list[object]
) -> None:
    """The three chunks the topic's context was led by, cited on every card it wrote."""
    entries = [
        artifacts.ProvenanceEntry(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            page_number=chunk.page_number,
            label=topic,
        )
        for chunk in chunks[:3]
    ]
    if entries:
        artifacts.set_provenance(conn, part_id, entries)


def _generate_quiz(conn: sqlite3.Connection, job: _Job) -> None:
    """One call for the whole quiz, then code-enforced validation of every question."""
    blocked = document_text_allowed(conn)
    if blocked is not None:
        raise LyraError(BLOCKED_MESSAGES.get(blocked, BLOCKED_MESSAGES[NO_ENDPOINT]))
    config = resolve_tutor_config(conn)

    _raise_if_cancelled(conn, job.artifact_id)
    artifacts.set_artifact_state(
        conn, job.artifact_id, artifacts.GENERATING, "Reading the material"
    )
    gathered = _gather_source_text(conn, job.artifact_id)
    if not gathered:
        raise LyraError(NO_QUESTIONS_MESSAGE)

    _raise_if_cancelled(conn, job.artifact_id)
    artifacts.set_artifact_state(conn, job.artifact_id, artifacts.GENERATING, "Writing questions")
    asked = list(job.types)
    reply = _call_json(
        config,
        prompts.build_quiz_prompt(gathered, job.count, job.difficulty, asked),
        prompts.QUIZ_SCHEMA,
    )
    _raise_if_cancelled(conn, job.artifact_id)
    questions, failures = _validate_questions(_json_list(reply, "questions"))

    # Fewer than half surviving means the reply misunderstood the shape badly enough
    # that one retry, told exactly what was wrong, is worth a second call.
    if len(questions) * 2 < job.count and failures:
        retry_note = "\n\nThe previous reply had these problems: " + "; ".join(failures)
        retry = _call_json(
            config,
            prompts.build_quiz_prompt(gathered + retry_note, job.count, job.difficulty, asked),
            prompts.QUIZ_SCHEMA,
        )
        _raise_if_cancelled(conn, job.artifact_id)
        retried, _ = _validate_questions(_json_list(retry, "questions"))
        if len(retried) > len(questions):
            questions = retried

    if len(questions) < 3:
        raise LyraError(NO_QUESTIONS_MESSAGE)

    artifacts.set_problems_total(conn, job.artifact_id, job.count)
    artifacts.set_problems_done(conn, job.artifact_id, len(questions))
    for ordinal, question in enumerate(questions, start=1):
        artifacts.create_part(
            conn,
            job.artifact_id,
            artifacts.QUIZ_QUESTION,
            ordinal,
            label=str(question["topic"]),
            content=json.dumps(question),
            content_type=artifacts.JSON,
            status=artifacts.PART_COMPLETE,
        )
        # No provenance is recorded on quiz questions: this pipeline grounds through one
        # whole-material call rather than per-question retrieval, so there is no small
        # set of chunks a question honestly traces to. Deck cards get theirs from the
        # per-topic retrieval; quizzes get this comment instead.
    _raise_if_cancelled(conn, job.artifact_id)
    artifacts.set_artifact_state(conn, job.artifact_id, artifacts.READY)


def _cancelled(conn: sqlite3.Connection, artifact_id: int) -> bool:
    """Whether the artifact was cancelled and the worker should stop without writing more."""
    return artifacts.get_artifact(conn, artifact_id)["state"] == artifacts.CANCELLED


def _raise_if_cancelled(conn: sqlite3.Connection, artifact_id: int) -> None:
    """Turn a cancelled artifact into the control flow that leaves it unchanged."""
    if _cancelled(conn, artifact_id):
        raise _GenerationCancelledError


def _validate_questions(
    items: list[object],
) -> tuple[list[dict[str, object]], list[str]]:
    """Split a reply's questions into the valid ones and the reasons the rest failed.

    The per-type rules from the prompt, enforced in code: the model's output is a
    proposal, and a question that breaks its type's rule is dropped, never repaired.
    """
    valid: list[dict[str, object]] = []
    failures: list[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            failures.append(f"question {index} is not an object")
            continue
        problem = _question_problem(item)
        if problem is None:
            valid.append(item)
        else:
            failures.append(f"question {index}: {problem}")
    return valid, failures


def _question_problem(item: dict[str, object]) -> str | None:
    """The one rule a question breaks, or None when it is fit to serve."""
    kind = item.get("type")
    question = str(item.get("question") or "").strip()
    options = item.get("options")
    explanation = str(item.get("explanation") or "").strip()
    topic = str(item.get("topic") or "").strip()
    correct_index = item.get("correct_index")

    if not question:
        return "empty question"
    if not explanation:
        return "empty explanation"
    if not topic:
        return "missing topic"
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        return "options must be a list of strings"
    if not isinstance(correct_index, int) or isinstance(correct_index, bool):
        return "correct_index must be an integer"

    if kind == "mcq":
        if len(options) != 4 or len(set(options)) != 4:
            return "mcq needs exactly four distinct options"
        if not 0 <= correct_index < 4:
            return "mcq correct_index out of range"
    elif kind == "true_false":
        if options != ["True", "False"]:
            return 'true_false options must be exactly ["True", "False"]'
        if correct_index not in (0, 1):
            return "true_false correct_index out of range"
    elif kind == "fill_blank":
        if len(options) != 1 or correct_index != 0:
            return "fill_blank holds exactly one option at index 0"
        if "___" not in question:
            return "fill_blank question needs a ___ blank"
    else:
        return f"unknown type {kind!r}"
    return None


def _gather_source_text(conn: sqlite3.Connection, artifact_id: int) -> str:
    """The source documents' chunk text, round-robined and capped.

    One document at a time in turns, so a long textbook cannot spend the whole budget
    before the syllabus is read once: each document gives at most DOCUMENT_TOKEN_CAP
    estimated tokens, and the gathering stops at TOTAL_TOKEN_CAP across all of them.
    """
    sources = artifacts.list_sources(conn, artifact_id, artifacts.STUDY_SOURCE)
    queues: list[tuple[int, list[sqlite3.Row]]] = []
    for source in sources:
        rows = conn.execute(
            "select content from chunks where document_id = ? order by id",
            (int(source["document_id"]),),
        ).fetchall()
        if rows:
            queues.append((int(source["document_id"]), list(rows)))

    gathered: list[str] = []
    per_document: dict[int, int] = {}
    total = 0
    while queues and total < TOTAL_TOKEN_CAP:
        document_id, rows = queues.pop(0)
        row = rows.pop(0)
        cost = estimate_tokens(str(row["content"]))
        spent = per_document.get(document_id, 0)
        if spent + cost <= DOCUMENT_TOKEN_CAP:
            gathered.append(str(row["content"]))
            per_document[document_id] = spent + cost
            total += cost
        if rows and per_document.get(document_id, 0) < DOCUMENT_TOKEN_CAP:
            queues.append((document_id, rows))
    return "\n\n".join(gathered)


def _call_json(
    config: TutorConfig, messages: list[dict[str, str]], schema: client.JsonSchema
) -> object:
    """One constrained-JSON call against the tutor endpoint, from a worker thread.

    Sync for the same reason the solver's are: the worker is a plain thread with no
    event loop, and owning one for the call keeps it free of async plumbing.
    """
    reply = asyncio.run(
        client.complete(
            config.endpoint_url,
            config.api_key,
            config.model,
            messages,
            temperature=client.DETERMINISTIC_TEMPERATURE,
            schema=schema,
            request_timeout=client.BACKGROUND_TIMEOUT,
        )
    )
    return json.loads(reply)


def _json_list(payload: object, key: str) -> list[object]:
    """The array field of a parsed reply, or nothing usable - never a raise."""
    if not isinstance(payload, dict):
        return []
    value = payload.get(key)
    return value if isinstance(value, list) else []
