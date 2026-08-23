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

Three reliability contracts hold here, each with its own Linear issue:

- **Durable intent (PLA-169).** A queued generation persists everything the worker needs
  to reconstruct its job (`study_jobs`, migration 035) before it is enqueued, so a
  restart requeues pending work and safely restarts interrupted work instead of failing
  a deck that another job was merely ahead of in the single-worker queue.
- **Exact sources (PLA-291).** The document set is an exact contract, revalidated at the
  worker boundary against the accepted snapshot, so a source deleted or made unusable
  after the request fails generation visibly rather than silently shrinking it.
- **Budgeted prompts (PLA-298) and truthful completion (PLA-299).** Every model call is
  budgeted against the configured tutor context window with an explicit output reserve,
  and an artifact reaches `ready` only when generation fulfilled the requested contract
  after bounded recovery; incomplete output fails visibly and leaves no partial rows.
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
    resolve_tutor_access,
)
from backend.core.errors import LyraError, NotFoundError
from backend.core.scheduler import new_card_state
from backend.llm import client, prompts
from backend.llm.budget import generation_reserve
from backend.llm.turn_budget import input_ceiling
from backend.rag.retrieve import retrieve
from backend.rag.tokens import estimate_tokens
from backend.storage.database import connect

logger = logging.getLogger(__name__)

# Per-document and whole-class reading caps for the source-gathering pass, in estimated
# tokens. The per-document cap is what stops a 600-page textbook from crowding the
# syllabus out of the mapping; the total is the material ceiling before the context-window
# budget (below) trims it further. The context budget, not this constant, is what keeps a
# prompt inside the endpoint's window.
DOCUMENT_TOKEN_CAP = 6_000
TOTAL_TOKEN_CAP = 12_000

# Retrieval budget per topic, in estimated tokens. A topic's cards are written against
# what the course says about that topic, not against the whole gathered input.
TOPIC_RETRIEVAL_BUDGET = 2_500

# Room held back from a call's source budget so the bounded retry, which appends a short
# corrective hint to the prompt, still fits the window without a second budgeting pass.
# Also the ceiling the hint itself is truncated to, so the reserve is never overspent.
_RETRY_HINT_RESERVE = 256

# Each topic and each quiz gets at most one extra attempt. Retries vary the prompt with a
# corrective hint (deterministic sampling would otherwise return the same reply), and the
# count is fixed so a bad endpoint cannot multiply model calls without bound.
_MAX_ATTEMPTS = 2

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
CONTEXT_TOO_SMALL_MESSAGE = (
    "The configured tutor context window is too small to generate this from the chosen "
    "material. Raise the context window in Settings, or choose less material, then try again."
)

STUDY_KINDS: tuple[str, ...] = (artifacts.KIND_FLASHCARD_DECK, artifacts.KIND_QUIZ)


class _GenerationCancelledError(Exception):
    """The artifact was cancelled while the worker was running and should be left alone."""


@dataclass(frozen=True)
class _Job:
    """What a queued generation needs. Ids and options only, never rows or handles.

    `source_ids` is the exact accepted document set, in reading order, so the worker can
    revalidate it against the live document table before spending a model call (PLA-291).
    """

    artifact_id: int
    source_ids: tuple[int, ...] = ()
    cards_per_topic: int = 4
    count: int = 10
    difficulty: str = "intermediate"
    types: tuple[str, ...] = field(default=prompts.QUIZ_QUESTION_TYPES)


@dataclass(frozen=True)
class _ProposedCard:
    """One validated card and the exact retrieval context that supported it."""

    front: str
    back: str
    chunks: tuple[object, ...]


_queue: queue.Queue[_Job] = queue.Queue()
_worker_lock = threading.Lock()
_worker_started = False


def persist_job(conn: sqlite3.Connection, job: _Job, kind: str) -> None:
    """Record a generation's full intent so a restart can reconstruct it (PLA-169).

    Written in the request, before the in-memory job is enqueued, so a process that dies
    with the row present can requeue the exact same job. A study artifact found queued or
    interrupted with no row here cannot be reconstructed and is failed rather than guessed.
    """
    conn.execute(
        "insert into study_jobs "
        "(artifact_id, kind, cards_per_topic, count, difficulty, types, source_ids) "
        "values (?, ?, ?, ?, ?, ?, ?) "
        "on conflict (artifact_id) do update set "
        "kind = excluded.kind, cards_per_topic = excluded.cards_per_topic, "
        "count = excluded.count, difficulty = excluded.difficulty, "
        "types = excluded.types, source_ids = excluded.source_ids",
        (
            job.artifact_id,
            kind,
            job.cards_per_topic,
            job.count,
            job.difficulty,
            json.dumps(list(job.types)),
            json.dumps(list(job.source_ids)),
        ),
    )
    conn.commit()


def _job_from_row(row: sqlite3.Row) -> _Job:
    """Reconstruct a job from its persisted row. Raises on metadata that will not parse."""
    types = json.loads(str(row["types"]))
    source_ids = json.loads(str(row["source_ids"]))
    if not isinstance(types, list) or not all(isinstance(item, str) for item in types):
        raise ValueError("persisted job types are malformed")
    if not isinstance(source_ids, list) or not all(isinstance(item, int) for item in source_ids):
        raise ValueError("persisted job source ids are malformed")
    return _Job(
        artifact_id=int(row["artifact_id"]),
        source_ids=tuple(source_ids),
        cards_per_topic=int(row["cards_per_topic"]),
        count=int(row["count"]),
        difficulty=str(row["difficulty"]),
        types=tuple(types),
    )


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


def reconcile_interrupted(conn: sqlite3.Connection) -> tuple[int, int]:
    """Recover every study artifact the last shutdown caught (PLA-169).

    Returns how many were requeued and how many were failed. The policy mirrors ingestion:

    - A `pending` artifact was queued and never started model work. With its intent
      persisted (`study_jobs`), it is requeued with the exact original options rather than
      failed for being behind another job in the single-worker queue.
    - A `generating` artifact had started. Its partial cards/questions are deleted and it
      is reset to `pending` and requeued, so recovery restarts cleanly and never appends
      duplicates to half-written output. There is no durable per-stage progress to resume
      from, so a safe restart is the deterministic choice.
    - An artifact whose intent cannot be reconstructed - no `study_jobs` row, or metadata
      that will not parse - is failed with the interrupted message, because guessing its
      options would silently change what the student asked for.

    A `cancelled` artifact is never touched here, so a cancelled job is never resurrected.
    A deleted artifact took its `study_jobs` row with it (cascade) and is not seen.

    Requeue order is by artifact id ascending - creation order - so the queue after a
    restart is stable and independent of row-scan order.
    """
    placeholders = ", ".join("?" for _ in STUDY_KINDS)
    rows = conn.execute(
        f"select id, state from artifacts "  # noqa: S608
        f"where state in (?, ?) and kind in ({placeholders}) order by id",
        (artifacts.PENDING, artifacts.GENERATING, *STUDY_KINDS),
    ).fetchall()

    requeued: list[_Job] = []
    failed = 0
    for row in rows:
        artifact_id = int(row["id"])
        job_row = conn.execute(
            "select * from study_jobs where artifact_id = ?", (artifact_id,)
        ).fetchone()
        job = None
        if job_row is not None:
            try:
                job = _job_from_row(job_row)
            except (ValueError, TypeError, json.JSONDecodeError):
                logger.warning("Study artifact %s has unparseable job metadata", artifact_id)
        if job is None:
            # This is terminal: without reconstructable intent there is no safe replay.
            # Use the ordinary failure path so an interrupted artifact cannot retain
            # stale partial cards/questions while reporting `failed`.
            _mark_failed(conn, artifact_id, LyraError(INTERRUPTED_MESSAGE))
            failed += 1
            continue
        if str(row["state"]) == artifacts.GENERATING:
            # Discard whatever the interrupted run half-wrote before restarting it, so the
            # fresh run cannot land duplicates beside the old partial output.
            artifacts.delete_parts(conn, artifact_id)
            artifacts.set_artifact_state(conn, artifact_id, artifacts.PENDING, None)
        requeued.append(job)

    for job in requeued:
        enqueue(job)
    return len(requeued), failed


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
    """Record the failure on the artifact row, keeping the stage it died in.

    A failed generation must leave nothing that reads as a finished artifact: any parts a
    partial run wrote are deleted here (PLA-299), so a failed deck never shows cards and a
    failed quiz never shows questions. The delete shares the failure commit path below.
    """
    row = conn.execute("select state from artifacts where id = ?", (artifact_id,)).fetchone()
    if row is None:
        return
    # A cancellation that landed between the worker's cancel checkpoints and this failure
    # write wins: cancelling keeps whatever was already written, so a late failure must not
    # overwrite `cancelled` with `failed` or delete the cards the cancel meant to keep.
    if str(row["state"]) == artifacts.CANCELLED:
        return
    # Deleting the parts of an artifact that is being failed is safe across retry/restart:
    # a re-run regenerates from scratch, and reconcile deletes parts before restarting too.
    try:
        artifacts.delete_parts(conn, artifact_id)
    except NotFoundError:
        return
    message = exc.message if isinstance(exc, LyraError) else str(exc)
    artifacts.mark_artifact_failed(conn, artifact_id, str(row["state"]), message)


def _resolve_config(conn: sqlite3.Connection) -> TutorConfig:
    """The endpoint to generate against, from one snapshot, or a blocked-reason raise.

    One read resolves the endpoint and its document-text permission together, so the
    endpoint checked for consent is provably the endpoint generation is sent to.
    """
    access = resolve_tutor_access(conn)
    if access.document_block is not None:
        raise LyraError(BLOCKED_MESSAGES.get(access.document_block, BLOCKED_MESSAGES[NO_ENDPOINT]))
    if access.config is None:
        raise LyraError(BLOCKED_MESSAGES[NO_ENDPOINT])
    return access.config


def _generate_deck(conn: sqlite3.Connection, job: _Job) -> None:
    """Map the sources to topics, then write each topic's cards against retrieval."""
    artifact = artifacts.get_artifact(conn, job.artifact_id)
    class_id = int(artifact["class_id"])
    config = _resolve_config(conn)
    _validate_sources(conn, job)

    _raise_if_cancelled(conn, job.artifact_id)
    artifacts.set_artifact_state(
        conn, job.artifact_id, artifacts.GENERATING, "Reading the material"
    )
    topics_prompt = prompts.build_topics_prompt("")
    fixed = _prompt_tokens(topics_prompt)
    gathered, _ = _gather_source_text(conn, job.source_ids, _source_cap(config, fixed))
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

    failed: list[str] = []
    complete_topics: list[tuple[str, list[_ProposedCard]]] = []
    # Deck-wide, so a card is dropped whether one topic's call repeated itself or two
    # topics converged on the same front. The set outlives any single topic's attempts.
    seen_fronts: set[str] = set()
    for topic in topic_names:
        _raise_if_cancelled(conn, job.artifact_id)
        artifacts.set_artifact_state(conn, job.artifact_id, artifacts.GENERATING, topic)
        cards = _collect_topic_cards_bounded(conn, job, config, class_id, topic, seen_fronts)
        if len(cards) != job.cards_per_topic:
            failed.append(topic)
        else:
            complete_topics.append((topic, cards))
        artifacts.increment_problems_done(conn, job.artifact_id)

    # Truthful completion (PLA-299): every mapped topic must reach the exact requested
    # count after bounded recovery. Nothing has been persisted yet, so an undershoot in
    # even one topic cannot leave an apparently successful partial deck behind.
    if failed:
        raise LyraError(
            _deck_incomplete_message(len(failed), len(topic_names), job.cards_per_topic)
        )

    ordinal = 0
    for topic, cards in complete_topics:
        _raise_if_cancelled(conn, job.artifact_id)
        _persist_topic_cards(conn, job, topic, ordinal, cards)
        ordinal += len(cards)

    _raise_if_cancelled(conn, job.artifact_id)
    artifacts.set_artifact_state(conn, job.artifact_id, artifacts.READY, None)


def _collect_topic_cards_bounded(
    conn: sqlite3.Connection,
    job: _Job,
    config: TutorConfig,
    class_id: int,
    topic: str,
    seen_fronts: set[str],
) -> list[_ProposedCard]:
    """Collect exactly one topic's requested cards across at most two model attempts.

    Invalid cards, repeats within either reply, and fronts already retained for another
    topic do not count. The first distinct cards in proposal order win, so overproduction
    is truncated deterministically. Nothing is persisted here: if bounded recovery still
    undershoots, the caller can fail the whole deck without cleaning up proposal rows.
    """
    collected: list[_ProposedCard] = []
    candidate_fronts = set(seen_fronts)
    for attempt in range(_MAX_ATTEMPTS):
        try:
            proposed, kept_chunks = _propose_topic_cards(
                conn,
                job,
                config,
                class_id,
                topic,
                retained=len(collected) if attempt > 0 else None,
            )
        except _GenerationCancelledError:
            raise
        except LyraError as exc:
            # No retrieval chunk can fit this deterministic context budget. Repeating the
            # same retrieval cannot change that, so preserve the actionable local error.
            if exc.message == CONTEXT_TOO_SMALL_MESSAGE:
                raise
            logger.exception("Card generation failed for topic %r (attempt %d)", topic, attempt + 1)
            continue
        except Exception:
            logger.exception("Card generation failed for topic %r (attempt %d)", topic, attempt + 1)
            continue
        for card in proposed:
            key = _dedupe_key(card["front"])
            if not key or key in candidate_fronts:
                continue
            candidate_fronts.add(key)
            collected.append(
                _ProposedCard(front=card["front"], back=card["back"], chunks=tuple(kept_chunks))
            )
            if len(collected) == job.cards_per_topic:
                # A topic only claims its fronts deck-wide once its exact contract is met.
                # A failed partial topic therefore cannot make later accounting ambiguous.
                seen_fronts.update(_dedupe_key(card.front) for card in collected)
                return collected
    return collected


def _propose_topic_cards(
    conn: sqlite3.Connection,
    job: _Job,
    config: TutorConfig,
    class_id: int,
    topic: str,
    *,
    retained: int | None,
) -> tuple[list[dict[str, str]], list[object]]:
    """One topic's retrieval and one model call, returning the cards it proposed.

    Writes nothing. Separating the proposal (the flaky model call) from persistence is what
    makes the bounded retry safe: a retry re-runs only this, so no card is committed twice
    and no ordinal is reused. Returns the validated front/back pairs and the chunks that
    fed them, for the caller to persist with provenance.
    """
    system_tokens = _prompt_tokens(prompts.build_flashcards_prompt(topic, "", job.cards_per_topic))
    retry = retained is not None
    hint_reserve = _RETRY_HINT_RESERVE if retry else 0
    context_budget = max(0, _source_cap(config, system_tokens) - hint_reserve)
    result = retrieve(conn, class_id, topic, min(TOPIC_RETRIEVAL_BUDGET, context_budget))
    kept_chunks = _trim_chunks(list(result.chunks), context_budget)
    if not kept_chunks:
        raise LyraError(CONTEXT_TOO_SMALL_MESSAGE)
    context_block = prompts.format_context_block([vars(chunk) for chunk in kept_chunks])
    messages = prompts.build_flashcards_prompt(topic, context_block, job.cards_per_topic)
    if retry:
        messages = _with_retry_hint(
            messages, _flashcard_retry_hint(retained or 0, job.cards_per_topic)
        )
    reply = _call_json(config, messages, prompts.FLASHCARDS_SCHEMA)
    _raise_if_cancelled(conn, job.artifact_id)

    proposed: list[dict[str, str]] = []
    for card in _json_list(reply, "cards"):
        if not isinstance(card, dict):
            continue
        front = str(card.get("front") or "").strip()
        back = str(card.get("back") or "").strip()
        if front and back:
            proposed.append({"front": front, "back": back})
    return proposed, kept_chunks


def _persist_topic_cards(
    conn: sqlite3.Connection,
    job: _Job,
    topic: str,
    first_ordinal: int,
    cards: list[_ProposedCard],
) -> None:
    """Persist one already exact, deduplicated topic with per-attempt provenance."""
    if len(cards) != job.cards_per_topic:
        raise ValueError("A topic must be complete before it is persisted.")
    for offset, card in enumerate(cards, start=1):
        payload = json.dumps({"front": card.front, "back": card.back, "topic": topic})
        part_id = artifacts.create_part(
            conn,
            job.artifact_id,
            artifacts.CARD,
            first_ordinal + offset,
            label=topic,
            content=payload,
            content_type=artifacts.JSON,
            status=artifacts.PART_COMPLETE,
        )
        _insert_card_state(conn, part_id)
        _record_card_provenance(conn, part_id, topic, list(card.chunks))


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
    config = _resolve_config(conn)
    _validate_sources(conn, job)

    _raise_if_cancelled(conn, job.artifact_id)
    artifacts.set_artifact_state(
        conn, job.artifact_id, artifacts.GENERATING, "Reading the material"
    )
    asked = list(job.types)
    # The source budget leaves room for the retry hint, so the second call fits the window
    # without re-gathering. The quiz prompt's fixed material is its system instruction.
    fixed = _prompt_tokens(prompts.build_quiz_prompt("", job.count, job.difficulty, asked))
    source_cap = max(0, _source_cap(config, fixed) - _RETRY_HINT_RESERVE)
    gathered, source_ids = _gather_source_text(conn, job.source_ids, source_cap)
    if not gathered:
        raise LyraError(NO_QUESTIONS_MESSAGE)

    _raise_if_cancelled(conn, job.artifact_id)
    artifacts.set_artifact_state(conn, job.artifact_id, artifacts.GENERATING, "Writing questions")
    reply = _call_json(
        config,
        prompts.build_quiz_prompt(gathered, job.count, job.difficulty, asked),
        prompts.QUIZ_SCHEMA,
    )
    _raise_if_cancelled(conn, job.artifact_id)
    questions, failures = _validate_questions(_json_list(reply, "questions"))
    questions = _dedupe_questions(questions)

    # Bounded recovery (PLA-299): one retry when the reply undershoots the requested count,
    # told exactly what was wrong so the deterministic re-send is not identical.
    if len(questions) < job.count:
        hint = _quiz_retry_hint(failures)
        retry = _call_json(
            config,
            _with_retry_hint(
                prompts.build_quiz_prompt(gathered, job.count, job.difficulty, asked), hint
            ),
            prompts.QUIZ_SCHEMA,
        )
        _raise_if_cancelled(conn, job.artifact_id)
        retried, _ = _validate_questions(_json_list(retry, "questions"))
        retried = _dedupe_questions(retried)
        if len(retried) > len(questions):
            questions = retried

    # Truthful completion (PLA-299): the quiz is `ready` only when it holds the number of
    # questions the student asked for. A short reply is a failure with an actionable count,
    # not a smaller quiz quietly presented as the requested one. No parts are written until
    # the contract is met, so a failed quiz leaves nothing behind.
    if len(questions) < job.count:
        raise LyraError(_quiz_incomplete_message(len(questions), job.count))
    questions = questions[: job.count]

    # Every question is grounded at the document level, not the chunk level. The quiz is
    # written by one call over the whole gathered material, so no question honestly traces
    # to a small set of chunks the way a deck card traces to its topic's retrieval. What is
    # honestly true is which documents fed that material, and provenance degrades to exactly
    # that: document_id with no chunk or page, which list_provenance still resolves to a
    # filename the student can open.
    source_entries = [
        artifacts.ProvenanceEntry(document_id=document_id) for document_id in source_ids
    ]
    artifacts.set_problems_total(conn, job.artifact_id, job.count)
    artifacts.set_problems_done(conn, job.artifact_id, len(questions))
    for ordinal, question in enumerate(questions, start=1):
        part_id = artifacts.create_part(
            conn,
            job.artifact_id,
            artifacts.QUIZ_QUESTION,
            ordinal,
            label=str(question["topic"]),
            content=json.dumps(question),
            content_type=artifacts.JSON,
            status=artifacts.PART_COMPLETE,
        )
        if source_entries:
            artifacts.set_provenance(conn, part_id, source_entries)
    _raise_if_cancelled(conn, job.artifact_id)
    artifacts.set_artifact_state(conn, job.artifact_id, artifacts.READY)


def _cancelled(conn: sqlite3.Connection, artifact_id: int) -> bool:
    """Whether the artifact was cancelled and the worker should stop without writing more."""
    return artifacts.get_artifact(conn, artifact_id)["state"] == artifacts.CANCELLED


def _raise_if_cancelled(conn: sqlite3.Connection, artifact_id: int) -> None:
    """Turn a cancelled artifact into the control flow that leaves it unchanged."""
    if _cancelled(conn, artifact_id):
        raise _GenerationCancelledError


# ---------------------------------------------------------------------------
# Source revalidation (PLA-291)
# ---------------------------------------------------------------------------

# The student-facing reason a source cannot be used, by document state. Filesystem paths,
# document text, and internal stage names never appear; the filename and a plain reason do.
_UNREADY_REASONS: dict[str, str] = {
    "failed": "failed to process",
    "unsupported": "could not be read",
}
_UNREADY_DEFAULT = "is still processing"


def _validate_sources(conn: sqlite3.Connection, job: _Job) -> None:
    """Refuse to generate unless every accepted source still exists and is ready (PLA-291).

    The worker boundary re-check, against the exact snapshot the request accepted. A
    source deleted, moved out of the class, or knocked out of `ready` (a reingest, a
    failure) after the HTTP request would otherwise be silently skipped, producing an
    artifact whose title and source choice imply material it never used. Instead
    generation fails visibly, naming the affected files and what is wrong with them.
    """
    if not job.source_ids:
        return
    placeholders = ", ".join("?" for _ in job.source_ids)
    rows = conn.execute(
        f"select id, filename, state from documents where id in ({placeholders})",  # noqa: S608
        tuple(job.source_ids),
    ).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    problems: list[str] = []
    for document_id in job.source_ids:
        row = by_id.get(document_id)
        if row is None:
            problems.append("a chosen document was removed")
        elif str(row["state"]) != "ready":
            reason = _UNREADY_REASONS.get(str(row["state"]), _UNREADY_DEFAULT)
            problems.append(f"{row['filename']} {reason}")
    if problems:
        raise LyraError("This can no longer be generated: " + "; ".join(problems) + ".")


# ---------------------------------------------------------------------------
# Context-window budgeting (PLA-298)
# ---------------------------------------------------------------------------


def _prompt_tokens(messages: list[dict[str, str]]) -> int:
    """The estimated token cost of a prompt, measured as the budget elsewhere measures."""
    return sum(estimate_tokens(str(message["content"])) for message in messages)


def _input_ceiling(config: TutorConfig) -> int:
    """The most prompt material one call may carry: the window less the output reserve.

    The same reserve the chat turn and the background pipelines take (`generation_reserve`)
    is set aside for the model's reply and never lent to the prompt; the shared context
    safety margin then applies to what remains, so the estimate's density error cannot
    push an accepted prompt past the real window.
    """
    return input_ceiling(config.context_window, generation_reserve(config.context_window))


def _source_cap(config: TutorConfig, fixed_tokens: int) -> int:
    """Tokens left for trimmable source material once fixed prompt material is charged.

    Raises when the fixed material (system instruction and the like) does not itself fit
    the window: with no room even for zero source text, there is nothing to trim toward a
    fit, so generation fails locally with a bounded message rather than knowingly sending
    an oversized request (PLA-298).
    """
    room = _input_ceiling(config) - fixed_tokens
    if room <= 0:
        raise LyraError(CONTEXT_TOO_SMALL_MESSAGE)
    return room


def _gather_source_text(
    conn: sqlite3.Connection, source_ids: tuple[int, ...], total_cap: int
) -> tuple[str, list[int]]:
    """The source documents' chunk text, round-robined and capped to fit the budget.

    One document at a time in turns, so a long textbook cannot spend the whole budget
    before the syllabus is read once: each document gives at most DOCUMENT_TOKEN_CAP
    estimated tokens, and the gathering stops once adding the next chunk would exceed
    `total_cap`. A chunk is added only when it fits both the per-document cap and the total
    cap (`spent + cost <= cap`), so the ceiling can never be overshot by one chunk - the
    boundary bug PLA-298 calls out. `total_cap` is the context-window-derived budget, so
    gathering trims deterministically rather than relying on the endpoint to reject an
    oversized prompt.

    Returns the joined text and the ids of the documents that actually contributed to it,
    in source order. The latter is what a quiz question honestly traces to: the whole-
    material call read this text, so the documents behind it are its provenance.
    """
    document_cap = min(DOCUMENT_TOKEN_CAP, total_cap)
    queues: list[tuple[int, list[sqlite3.Row]]] = []
    for document_id in source_ids:
        rows = conn.execute(
            "select content from chunks where document_id = ? order by id",
            (document_id,),
        ).fetchall()
        if rows:
            queues.append((document_id, list(rows)))

    gathered: list[str] = []
    per_document: dict[int, int] = {}
    total = 0
    while queues and total < total_cap:
        document_id, rows = queues.pop(0)
        row = rows.pop(0)
        cost = estimate_tokens(str(row["content"]))
        spent = per_document.get(document_id, 0)
        if spent + cost <= document_cap and total + cost <= total_cap:
            gathered.append(str(row["content"]))
            per_document[document_id] = spent + cost
            total += cost
        if rows and per_document.get(document_id, 0) < document_cap:
            queues.append((document_id, rows))
    contributing = [document_id for document_id in source_ids if per_document.get(document_id, 0)]
    return "\n\n".join(gathered), contributing


def _trim_chunks(chunks: list[object], budget_tokens: int) -> list[object]:
    """Keep the best retrieved chunks that fit `budget_tokens`, in retrieval order.

    Retrieval is already budgeted, but formatting adds labels and a heading, so the block
    can run a little over. A chunk that cannot fit is skipped rather than retained or used
    as a stopping point: a later, smaller chunk may still provide grounded context. Thus
    the retained raw content never exceeds the budget and an oversized top-ranked chunk
    cannot force an oversized upstream request.
    """
    kept: list[object] = []
    used = 0
    for chunk in chunks:
        cost = estimate_tokens(str(getattr(chunk, "content", "")))
        if cost <= 0 or used + cost > budget_tokens:
            continue
        used += cost
        kept.append(chunk)
    return kept


def _with_retry_hint(messages: list[dict[str, str]], hint: str) -> list[dict[str, str]]:
    """A copy of the prompt with a corrective hint appended to its system instruction.

    A deterministic re-send of the same prompt returns the same reply, so the bounded
    retry has to vary the prompt to have any recovery value. The hint is capped so it can
    never overrun the reserve held back for it when the source budget was computed.
    """
    capped = hint[: _RETRY_HINT_RESERVE * 4]
    updated = [dict(message) for message in messages]
    for message in updated:
        if message.get("role") == "system":
            message["content"] = f"{message['content']}\n\n{capped}"
            return updated
    updated.insert(0, {"role": "system", "content": capped})
    return updated


def _flashcard_retry_hint(retained: int, requested: int) -> str:
    """Tell the bounded retry its exact distinct-card deficit."""
    remaining = max(0, requested - retained)
    return (
        f"The previous attempt produced only {retained} of {requested} required distinct, "
        f"usable cards. Write at least {remaining} new cards with fronts not repeated from "
        "the previous attempt. Every card must have a non-empty front and back grounded in "
        "the material above."
    )


def _quiz_retry_hint(failures: list[str]) -> str:
    """The corrective hint for a quiz retry, naming what the last reply got wrong."""
    base = "The previous reply did not produce enough valid questions."
    if not failures:
        return base
    return base + " Problems: " + "; ".join(failures[:5])


def _deck_incomplete_message(failed: int, total: int, cards_per_topic: int) -> str:
    """Why a deck was failed rather than shown as an incomplete `ready`."""
    return (
        f"{failed} of {total} topics did not reach the required {cards_per_topic} distinct "
        "cards, so this deck was not finished. Please try again."
    )


def _quiz_incomplete_message(produced: int, requested: int) -> str:
    """Why a quiz was failed rather than shown as a smaller `ready` quiz."""
    return (
        f"Only {produced} of the {requested} requested questions could be generated from "
        "this material. Try fewer questions, or add more material, then generate again."
    )


def _call_json(
    config: TutorConfig, messages: list[dict[str, str]], schema: client.JsonSchema
) -> object:
    """One constrained-JSON call against the tutor endpoint, from a worker thread.

    Every study call funnels through here, which is where the context-window invariant is
    enforced rather than merely calculated (PLA-298): the output reserve is sent as
    `max_tokens`, and the prompt is refused locally if it exceeds the input ceiling. The
    callers trim toward this ceiling first, so the refusal is a backstop against a prompt
    that slipped past the budget, never the normal path. A reply the endpoint truncates at
    the ceiling is fatal, because a half-written JSON reply is not a smaller valid one.

    Sync for the same reason the solver's are: the worker is a plain thread with no
    event loop, and owning one for the call keeps it free of async plumbing.
    """
    if _prompt_tokens(messages) > _input_ceiling(config):
        raise LyraError(CONTEXT_TOO_SMALL_MESSAGE)
    reply = asyncio.run(
        client.complete(
            config.endpoint_url,
            config.api_key,
            config.model,
            messages,
            temperature=client.DETERMINISTIC_TEMPERATURE,
            schema=schema,
            max_tokens=generation_reserve(config.context_window),
            request_timeout=client.BACKGROUND_TIMEOUT,
            fail_on_truncation=True,
        )
    )
    return json.loads(reply)


def _dedupe_key(text: str) -> str:
    """A comparison key for duplicate detection.

    Case, surrounding whitespace, and trailing sentence punctuation are folded away so two
    cards or questions that differ only cosmetically - "What is a delta?" against "What is
    a delta" - collide, while anything that differs in wording stays distinct. Deliberately
    conservative: it drops a near-identical repeat without risking a merge of two prompts
    the student would recognise as different.
    """
    collapsed = " ".join(text.casefold().split())
    return collapsed.rstrip(".?!:;, ")


def _dedupe_questions(questions: list[dict[str, object]]) -> list[dict[str, object]]:
    """Drop questions whose stem repeats one already kept, preserving order.

    The model is asked for distinct questions but is not held to it, so a quiz could
    otherwise store the same stem twice - wasting a slot and letting one attempt count the
    same knowledge twice in the weakness report. Distinctness is by stem alone: two
    questions with the same wording are the same question whatever their options say.
    """
    seen: set[str] = set()
    unique: list[dict[str, object]] = []
    for question in questions:
        key = _dedupe_key(str(question.get("question") or ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(question)
    return unique


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


def _json_list(payload: object, key: str) -> list[object]:
    """The array field of a parsed reply, or nothing usable - never a raise."""
    if not isinstance(payload, dict):
        return []
    value = payload.get(key)
    return value if isinstance(value, list) else []
