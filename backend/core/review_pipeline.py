"""The review pass: four lenses over the draft, filing margin comments as they look.

The reviewer never touches the document. Each lens is one or more reviewer-profile
tool-loop runs against the same serial endpoint the rest of the workspace uses, and
every finding lands through the `add_comment` tool the moment the model files it - so
a slow model shows findings incrementally in the Comments tab, and an interrupted
review keeps everything it found. The lenses, in order (kuhn's review scope, translated
to student writing):

1. **Structure** - one run over the outline and the brief: missing moves for the
   assignment type, ordering, balance, sections that do not serve the brief.
2. **Argument and transitions** - one run over the seams: for each handoff between
   sections, the tail of one and the head of the next, judged as a chain.
3. **Prose calibration** - one run per section, the craft bar as a checklist.
4. **Claims and citations** - one run per section: factual claims checked against the
   course material through the search tool, and filed where the source does not say
   what the prose says.

Lenses 3 and 4 go section by section because nothing here assumes the document fits in
context - that is the workspace's ground rule, not a degraded mode. Progress counts
lenses (`problems_total`/`problems_done`); `stage_detail` always starts with
"Reviewing", which is how the workspace tells a review (editor stays live - nothing
will write the document) from a draft pass (editor follows the pen).

A run that hits its depth or time ceiling remains failed and retains its last complete
checkpoint, never losing what it filed. The close is a chat message in the draft's
writer conversation: counts by severity and the findings that matter most. The comments
are the review; the message is not a restatement.
"""

import asyncio
import hashlib
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from backend.core import (
    artifacts,
    briefs,
    comments,
    drafting,
    sections,
    sessions,
    source_ledger,
    writer_budgets,
    writer_plans,
    writer_runs,
    writer_tools,
)
from backend.core.app_settings import (
    NO_ENDPOINT,
    TutorConfig,
    resolve_tutor_access,
)
from backend.core.errors import LyraError, NotFoundError
from backend.core.profiles import select_active_facts
from backend.core.writer_pipeline import BLOCKED_MESSAGES, _bounded_inference
from backend.llm import budget, prompts
from backend.llm.tools import (
    NO_TOOL_SUPPORT,
    UPSTREAM_FAILED,
    ContextBudget,
    ToolDefinition,
    ToolLoopResult,
    conversation_tokens,
    run_tool_loop,
    schema_tokens,
    tool_schemas,
)
from backend.llm.turn_budget import CONTEXT_SAFETY_MARGIN
from backend.rag.retrieve import retrieve
from backend.storage.database import connect
from backend.tools.result import failure, success

logger = logging.getLogger(__name__)
_review_local = threading.local()

# Wall clock per run, the tool loop's own background ceiling.
LENS_TIMEOUT_SECONDS = 600.0

# How much of each side a seam shows: enough of the tail to know where the section
# lands, enough of the head to know where the next one starts.
SEAM_TAIL_WORDS = 120
SEAM_HEAD_WORDS = 60

LENS_COUNT = 4

NOTHING_TO_REVIEW_DETAIL = "There is nothing to review yet: no section has any prose."
_COMPLETE_EMPTY_DETAIL = "Review complete: no findings."
_FAILED_DETAIL = "The review did not finish."

# A re-review of a draft whose findings are still open re-derives the same findings, and
# `add_comment` dedups them: filing nothing. Reporting that as "no findings" told the
# student their draft was clean when it was the opposite, so a confirmed finding counts
# as found - it just does not become a second comment.
_CONFIRMED_SUFFIX = " ({count} already filed)"

# Severity rank for "what matters most", the scale's own order.
_SEVERITY_RANK = {severity: index for index, severity in enumerate(comments.SEVERITIES)}
_ALREADY_FILED = (
    "A comment at this severity is already open on that exact passage. Do not file "
    "the same finding twice; move on to the next one."
)
_NO_SECTION = (
    "No section matches {ref!r}. Address sections by their outline number or title; "
    "read_outline lists them."
)


@dataclass(frozen=True)
class ReviewJob:
    """One queued review pass over a draft."""

    artifact_id: int
    depth: str = "quick"
    run_id: int | None = None
    _deadline: float | None = None


def _time_remaining(job: ReviewJob) -> bool:
    return job._deadline is None or time.monotonic() < job._deadline


@dataclass(frozen=True)
class _CapturedLens:
    """One parallel lens result and its deferred write effects."""

    stage: str
    result: ToolLoopResult
    findings: tuple[dict[str, object], ...]
    confirmed_ids: tuple[int, ...] = ()


class _CaptureCoordinator:
    """Deterministic comment reservations shared by one parallel lens batch.

    A later section may run model inference concurrently, but its first comment tool
    call waits until every earlier section has finished its loop. That gives the tool
    the same duplicate state the serial reference would expose without permitting a
    worker connection to write.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._finished: set[int] = set()
        self._reservations: set[tuple[str | None, str]] = set()

    def wait_for_turn(self, worker_index: int) -> None:
        with self._condition:
            self._condition.wait_for(
                lambda: all(index in self._finished for index in range(worker_index))
            )

    def reserve(self, key: tuple[str | None, str]) -> bool:
        with self._condition:
            if key in self._reservations:
                return False
            self._reservations.add(key)
            return True

    def finish(self, worker_index: int) -> None:
        with self._condition:
            self._finished.add(worker_index)
            self._condition.notify_all()


def enqueue(job: ReviewJob) -> None:
    """Queue a review on the shared drafting worker - one thread, one endpoint caller."""
    drafting.enqueue(job)


def run_review(job: ReviewJob) -> None:
    """Run one review. The worker calls this; tests call it directly."""
    conn = connect()
    _review_local.run = (conn, job)
    try:
        if job.run_id is not None:
            run = writer_runs.mark_running(conn, job.run_id)
            if run["status"] not in writer_runs.ACTIVE_STATUSES:
                return
        _run(conn, job)
        if job.run_id is not None:
            if _cancel_requested(conn, job):
                return
            writer_runs.mark_completed(conn, job.run_id)
            if _cancel_requested(conn, job):
                return
        conn.execute(
            "update artifacts set writer_job_completed_at = datetime('now') where id = ? "
            "and (? is null or not exists (select 1 from writer_runs "
            "where artifact_id = ? and id > ?))",
            (job.artifact_id, job.run_id, job.artifact_id, job.run_id),
        )
        conn.commit()
    except NotFoundError:
        logger.info("Draft %s vanished before its review ran", job.artifact_id)
    except Exception as exc:
        conn.rollback()
        _settle_failed(conn, job, exc)
    finally:
        if hasattr(_review_local, "run"):
            del _review_local.run
        if hasattr(_review_local, "deadline"):
            del _review_local.deadline
        conn.close()


drafting.register_runner(ReviewJob, run_review)

_REVIEW_CANCELLED_DETAIL = "The review was cancelled. Filed comments were kept."


def _checkpoint(
    conn: sqlite3.Connection,
    job: ReviewJob,
    stage: str,
    *,
    index: int = 0,
    targets: tuple[str, ...] = (),
    data: dict[str, object] | None = None,
) -> None:
    if job.run_id is None:
        return
    writer_runs.checkpoint(conn, job.run_id, stage=stage, index=index, targets=targets, data=data)


def _cancel_requested(conn: sqlite3.Connection, job: ReviewJob) -> bool:
    return writer_runs.settle_cancellation(conn, job.run_id, _REVIEW_CANCELLED_DETAIL)


def _body_snapshot(part: dict[str, object]) -> dict[str, object]:
    return {
        "part_id": int(part["id"]),
        "content_version": int(part["content_version"]),
        "sha256": hashlib.sha256(str(part["content"]).encode()).hexdigest(),
    }


def _require_body_snapshot(
    conn: sqlite3.Connection, part_id: int, expected: dict[str, object]
) -> None:
    if _body_snapshot(artifacts.get_part(conn, part_id)) != expected:
        raise LyraError(
            "The writing changed during review. Comments were kept; review the new writing again."
        )


def _checkpoint_data(
    filed: list[int], confirmed: list[int], completed_lenses: int
) -> dict[str, object]:
    return {
        "filed_comment_ids": sorted(set(filed)),
        "confirmed_comment_ids": sorted(set(confirmed)),
        "completed_lenses": max(0, completed_lenses),
    }


def _settle_failed(conn: sqlite3.Connection, job: ReviewJob, exc: Exception) -> None:
    """Failed, with the reason. Comments already filed stay filed.

    `failed` rather than `ready`, because `error_message` is only ever shown next to that
    state: settling ready wrote the reason to a column no interface reads, so a review
    that died to an unreachable endpoint was indistinguishable from one that finished
    with nothing to say. The state is also the way back - the Review button is live at
    `failed`, and re-running clears it.
    """
    row = conn.execute("select id from artifacts where id = ?", (job.artifact_id,)).fetchone()
    if row is None:
        return
    if _cancel_requested(conn, job):
        return
    message = exc.message if isinstance(exc, LyraError) else str(exc)
    if job.run_id is not None:
        writer_runs.settle_failure(conn, job.run_id, _FAILED_DETAIL, message)
        # If cancellation won the writer lock, retain its terminal semantics.
        _cancel_requested(conn, job)
        return
    artifacts.mark_artifact_failed(conn, job.artifact_id, _FAILED_DETAIL, message)
    conn.execute(
        "update artifacts set writer_job_completed_at = datetime('now') where id = ?",
        (job.artifact_id,),
    )
    conn.commit()


def _review_run(
    config: TutorConfig,
    messages: list[dict[str, str]],
    registry: dict[str, ToolDefinition],
    max_depth: int,
) -> ToolLoopResult:
    """One reviewer tool-loop run, stripped. The test seam for every lens."""
    deadline = getattr(_review_local, "deadline", None)
    timeout = LENS_TIMEOUT_SECONDS
    if isinstance(deadline, float):
        timeout = max(0.001, min(timeout, deadline - time.monotonic()))
    messages = [
        *messages,
        {
            "role": "user",
            "content": (
                "Selected ledger excerpts are partial context, not the full source. Before "
                "calling a claim unsupported or unverifiable, use read_source with the source "
                "ID and supporting source_revision_id when available. Follow next_offset for "
                "omitted content needed to check the claim. Distinguish unavailable or partial "
                "evidence from a demonstrated contradiction; an uninspected passage is not absent."
                " A length target in the brief applies to the whole document, not every "
                "section; use a section budget only when the plan explicitly provides it. "
                "Application markers [@lyra:ID] are rendered on display/export, so do not "
                "criticize or replace that internal syntax. Distinguish the student's "
                "tentative interpretation and clearly attributed personal testimony from "
                "claims allegedly stated in a source. For textual analysis, complete inspected "
                "passages can support observations about what is not narrated; missing real-world "
                "measurements do not establish that an effect is absent. Prioritize consequential "
                "findings, read existing comments before filing overlapping advice, and group "
                "group the underlying problem instead of repeating it for each sentence. "
                "If an existing finding still applies, confirm its exact quote and severity "
                "through add_comment instead of paraphrasing it at a new anchor."
            ),
        },
    ]
    context_budget = ContextBudget(
        context_window=config.context_window,
        generation_reserve=budget.generation_reserve(config.context_window),
        tool_tokens=schema_tokens(tool_schemas(registry)),
        safety_margin=CONTEXT_SAFETY_MARGIN,
    )
    if conversation_tokens(messages) > context_budget.message_ceiling:
        raise LyraError(
            "The review's mandatory assignment, writing and source context exceed the "
            "configured context window. Saved writing and comments were kept."
        )
    return asyncio.run(
        _bounded_inference(
            run_tool_loop(
                config.endpoint_url,
                config.api_key,
                config.model,
                list(messages),
                max_depth=max_depth,
                timeout_seconds=timeout,
                registry=registry,
                context_budget=context_budget,
            ),
            deadline=deadline,
            run=getattr(_review_local, "run", None),
        )
    )


def _model_visible_reviewer_registry(
    registry: dict[str, ToolDefinition],
) -> dict[str, ToolDefinition]:
    """Hide replay-only comment ids and normalize duplicate errors from the model."""
    original = registry["add_comment"]

    def add_comment(**arguments: object):
        result = original.handler(**arguments)
        if result.ok:
            value = dict(result.value)
            value.pop("comment_id", None)
            value.pop("captured", None)
            return success(**value)
        if result.error.startswith("A comment at this severity is already open"):
            return failure(_ALREADY_FILED)
        return result

    registry["add_comment"] = ToolDefinition(
        original.name,
        original.description,
        original.parameters,
        add_comment,
    )
    return registry


def _capture_registry(
    conn: sqlite3.Connection,
    artifact_id: int,
    class_id: int,
    coordinator: _CaptureCoordinator | None = None,
    worker_index: int = 0,
) -> tuple[dict[str, ToolDefinition], list[dict[str, object]], list[int]]:
    """Build a reviewer registry whose handlers cannot write external state.

    Every worker owns ``conn``. Read handlers remain useful, while the three ledger/
    comment writers are removed or replaced. Comments are acknowledged to the model and
    captured for ordered replay by the owner connection.
    """
    registry, _ = writer_tools.build_registry(conn, artifact_id, writer_tools.REVIEWER)
    findings: list[dict[str, object]] = []
    confirmed_ids: list[int] = []
    coordinator = coordinator or _CaptureCoordinator()

    original_comment = registry["add_comment"]

    def capture_comment(
        body: str,
        severity: str,
        quote: str = "",
        section_ref: str = "",
    ):
        if severity not in comments.SEVERITIES:
            return failure(f"Severity must be one of: {', '.join(comments.SEVERITIES)}.")
        part = _body_part(conn, artifact_id)
        content = str(part["content"])
        cleaned = quote.strip()
        ref = section_ref.strip()
        hint: int | None = None
        canonical_quote: str | None = cleaned or None
        if cleaned:
            coordinator.wait_for_turn(worker_index)
            target = sections.extract(content, ref) if ref else None
            if ref and target is None:
                return failure(_NO_SECTION.format(ref=ref))
            anchor = comments.resolve_quote(
                content,
                cleaned,
                scope_start=target.start if target is not None else 0,
                scope_end=target.end if target is not None else None,
            )
            if anchor is not None:
                hint = anchor.start
                canonical_quote = content[anchor.start : anchor.end]
            duplicate = conn.execute(
                "select id from draft_comments where part_id = ? and parent_id is null "
                "and author = ? and quote = ? and severity = ? and resolved = 0 limit 1",
                (int(part["id"]), comments.REVIEWER, canonical_quote, severity),
            ).fetchone()
            if duplicate is not None:
                confirmed_ids.append(int(duplicate["id"]))
                return failure(_ALREADY_FILED)
            key = (canonical_quote, severity)
            if not coordinator.reserve(key):
                return failure(_ALREADY_FILED)
        findings.append(
            {
                "body": body,
                "severity": severity,
                "quote": canonical_quote or "",
                "section_ref": ref,
            }
        )
        return success(filed=True, anchored=hint is not None)

    registry["add_comment"] = ToolDefinition(
        original_comment.name,
        original_comment.description,
        original_comment.parameters,
        capture_comment,
    )

    # The ordinary search tool registers course documents in the ledger. Parallel
    # review needs the same evidence without that side effect; existing source IDs are
    # included where available, and missing ledger rows stay honest rather than being
    # created from worker threads.
    original_search = registry["search_course_material"]

    def search_course_material(query: str):
        result = retrieve(conn, class_id, query, writer_tools.SEARCH_BUDGET_TOKENS)
        ledger_by_document = {
            int(source["document_id"]): int(source["id"])
            for source in source_ledger.list_sources(conn, class_id)
            if source.get("document_id") is not None
        }
        return success(
            results=[
                {
                    "source_id": ledger_by_document.get(chunk.document_id),
                    "source": chunk.filename,
                    "section": chunk.section_title or chunk.section_path or "",
                    "page": chunk.page_number,
                    "excerpt": chunk.content[:700],
                }
                for chunk in result.chunks
            ],
            **({"note": "Nothing is indexed for this class yet."} if not result.chunks else {}),
        )

    registry["search_course_material"] = ToolDefinition(
        original_search.name,
        original_search.description,
        original_search.parameters,
        search_course_material,
    )

    # These tools persist fetched pages/excerpts. The section prompt already carries the
    # current ledger; omitting mutation-only tools is the safe parallel contract.
    registry.pop("fetch_source", None)
    registry.pop("record_source_excerpt", None)
    conn.execute("pragma query_only = on")
    return registry, findings, confirmed_ids


def _sync_course_sources(conn: sqlite3.Connection, class_id: int) -> None:
    """Give serial and captured search the same stable course-source IDs.

    The ordinary search handler registers a document when it first returns a chunk. A
    read-only worker cannot safely do that, so the owner registers searchable documents
    before it builds prompts or starts workers. Later serial upserts are idempotent.
    """
    rows = conn.execute(
        "select id, filename from documents where class_id = ? and state = 'ready' order by id",
        (class_id,),
    ).fetchall()
    for row in rows:
        source_ledger.upsert_source(
            conn,
            class_id,
            source_type=source_ledger.COURSE,
            document_id=int(row["id"]),
            title=str(row["filename"]),
        )


def _parallel_review_run(
    config: TutorConfig,
    artifact_id: int,
    class_id: int,
    stage: str,
    messages: list[dict[str, str]],
    max_depth: int,
    deadline: float | None = None,
    coordinator: _CaptureCoordinator | None = None,
    worker_index: int = 0,
    run_id: int | None = None,
) -> _CapturedLens:
    """Run one section lens on a private read-only connection."""
    worker_conn = connect()
    try:
        _review_local.deadline = deadline
        _review_local.run = (worker_conn, ReviewJob(artifact_id, run_id=run_id))
        registry, findings, confirmed_ids = _capture_registry(
            worker_conn,
            artifact_id,
            class_id,
            coordinator,
            worker_index,
        )
        try:
            result = _review_run(config, messages, registry, max_depth)
        except Exception as exc:
            logger.warning("Parallel review lens %r raised: %s", stage, exc)
            result = ToolLoopResult(content="", stopped=UPSTREAM_FAILED, detail=str(exc))
        return _CapturedLens(stage, result, tuple(findings), tuple(confirmed_ids))
    finally:
        if hasattr(_review_local, "run"):
            del _review_local.run
        if coordinator is not None:
            coordinator.finish(worker_index)
        if hasattr(_review_local, "deadline"):
            del _review_local.deadline
        worker_conn.close()


def _run(conn: sqlite3.Connection, job: ReviewJob) -> None:
    pass_budget = writer_budgets.get_budget(job.depth)
    if job._deadline is None:
        job = replace(
            job,
            _deadline=time.monotonic() + pass_budget.wall_clock_seconds,
        )
    _review_local.deadline = job._deadline
    artifact = artifacts.get_artifact(conn, job.artifact_id)
    if artifact["kind"] != artifacts.KIND_DRAFT:
        raise NotFoundError("That draft does not exist.")
    part = _body_part(conn, job.artifact_id)
    # One snapshot: the endpoint checked for consent is the endpoint the review is sent to.
    access = resolve_tutor_access(conn)
    if access.document_block is not None:
        raise LyraError(BLOCKED_MESSAGES.get(access.document_block, BLOCKED_MESSAGES[NO_ENDPOINT]))
    config = access.config

    conn.execute(
        "update artifacts set writer_job_kind = 'review', writer_job_depth = ?, "
        "writer_job_started_at = datetime('now'), writer_job_completed_at = null where id = ?",
        (job.depth, job.artifact_id),
    )
    conn.commit()
    run = writer_runs.get_run(conn, job.run_id) if job.run_id is not None else None
    checkpoint_payload = run.get("checkpoint") if run is not None else None
    if not isinstance(checkpoint_payload, dict):
        _checkpoint(conn, job, "start")
    if _cancel_requested(conn, job):
        return

    artifacts.set_artifact_state(
        conn, job.artifact_id, artifacts.GENERATING, "Reviewing the document"
    )
    conn.execute("update artifacts set error_message = null where id = ?", (job.artifact_id,))
    conn.commit()

    part = artifacts.get_part(conn, int(part["id"]))
    body = str(part["content"])
    body_snapshot = _body_snapshot(part)
    checkpoint_data = (
        dict(checkpoint_payload.get("data"))
        if isinstance(checkpoint_payload, dict) and isinstance(checkpoint_payload.get("data"), dict)
        else {}
    )
    nonfresh = isinstance(checkpoint_payload, dict) and (
        checkpoint_payload.get("stage") not in (None, "", "start")
        or checkpoint_payload.get("index", 0)
        or checkpoint_data.get("completed_lenses", 0)
    )
    if nonfresh and checkpoint_data.get("body_snapshot") != body_snapshot:
        checkpoint_data["previous_snapshot_comment_ids"] = sorted(
            set(
                checkpoint_data.get("previous_snapshot_comment_ids", [])
                + checkpoint_data.get("filed_comment_ids", [])
                + checkpoint_data.get("confirmed_comment_ids", [])
            )
        )
        checkpoint_data["filed_comment_ids"] = []
        checkpoint_data["confirmed_comment_ids"] = []
        checkpoint_data["completed_lenses"] = 0
        checkpoint_payload = {"stage": "start", "index": 0, "targets": []}
        if job.run_id is not None:
            writer_runs.add_warning(
                conn,
                job.run_id,
                code=writer_runs.CHECKPOINT_MISMATCH_WARNING,
                message=(
                    "The writing changed or its saved review revision could not be verified. "
                    "Review restarted from structure; earlier comments were kept."
                ),
                replace=True,
            )
    checkpoint_data["body_snapshot"] = body_snapshot
    if not nonfresh or checkpoint_payload.get("stage") == "start":
        _checkpoint(conn, job, "start", data=checkpoint_data)
    targets = _targets(body)
    if not targets:
        _close(
            conn,
            job.artifact_id,
            int(part["id"]),
            int(artifact["class_id"]),
            [],
            [],
            NOTHING_TO_REVIEW_DETAIL,
            run_id=job.run_id,
            expected_body=body_snapshot,
        )
        return

    total_lenses = LENS_COUNT + (1 if job.depth == "deep" else 0)
    completed_lenses = int(checkpoint_data.get("completed_lenses", 0) or 0)
    artifacts.set_problems_total(conn, job.artifact_id, total_lenses)
    artifacts.set_problems_done(conn, job.artifact_id, min(completed_lenses, total_lenses))

    title = str(artifact["title"])
    class_id = int(artifact["class_id"])
    part_id = int(part["id"])
    brief_block = prompts.format_brief_block(briefs.get_brief(conn, job.artifact_id))
    plan = writer_plans.get_active_plan(conn, job.artifact_id)
    plan_block = prompts.format_plan_block(plan)
    _sync_course_sources(conn, class_id)
    ledger = source_ledger.list_sources(conn, class_id)
    ledger_block = prompts.format_ledger_block(ledger)
    filed = [int(value) for value in checkpoint_data.get("filed_comment_ids", [])]
    confirmed = [int(value) for value in checkpoint_data.get("confirmed_comment_ids", [])]
    budget_exhausted = False

    def save_checkpoint(stage: str, *, index: int = 0, targets: tuple[str, ...] = ()) -> None:
        _require_body_snapshot(conn, part_id, body_snapshot)
        _checkpoint(
            conn,
            job,
            stage,
            index=index,
            targets=targets,
            data={
                **checkpoint_data,
                **_checkpoint_data(filed, confirmed, completed_lenses),
                "body_snapshot": body_snapshot,
            },
        )

    def observe_result(stage: str, result: ToolLoopResult) -> None:
        """Apply the serial pass's tolerance and logging to one completed lens."""
        if result.complete and not result.calls:
            logger.warning(
                "Review lens %r finished without calling a tool; its findings, if any, "
                "are in prose the loop discards",
                stage,
            )
        if result.stopped == NO_TOOL_SUPPORT:
            raise LyraError(result.detail or "The tutor endpoint does not support tools.")
        if not result.complete:
            # Checkpoints certify completed work; skipped lenses cannot become an
            # all-clear after restart. Already filed comments remain available.
            raise LyraError(
                result.detail
                or f"{stage} did not finish ({result.stopped}). Filed comments were kept."
            )

    def lens(display_stage: str, messages: list[dict[str, str]], max_depth: int) -> bool:
        """One reviewer run under one stage banner, findings accumulated as filed."""
        nonlocal budget_exhausted
        if not _time_remaining(job):
            budget_exhausted = True
            return False
        if _cancel_requested(conn, job):
            return False
        _require_body_snapshot(conn, part_id, body_snapshot)
        artifacts.set_artifact_state(conn, job.artifact_id, artifacts.GENERATING, display_stage)
        registry, effects = writer_tools.build_registry(
            conn, job.artifact_id, writer_tools.REVIEWER, run_id=job.run_id
        )
        _model_visible_reviewer_registry(registry)
        result = _review_run(config, messages, registry, max_depth)
        filed.extend(effects.filed_comment_ids or [])
        confirmed.extend(effects.confirmed_comment_ids or [])
        if _cancel_requested(conn, job):
            return False
        _require_body_snapshot(conn, part_id, body_snapshot)
        observe_result(display_stage, result)
        return True

    capabilities = writer_budgets.get_writer_capabilities(conn, class_id)

    def section_lenses(
        stage_key: str,
        work: list[tuple[str, list[dict[str, str]], int]],
    ) -> bool:
        """Run section work serially by default, or capture/replay in a bounded pool."""
        nonlocal budget_exhausted, completed_lenses
        if not _time_remaining(job):
            budget_exhausted = True
            return False
        run_local = writer_runs.get_run(conn, job.run_id) if job.run_id is not None else None
        targets = tuple(stage for stage, _, _ in work)
        resume_index = (
            writer_runs.compatible_index(run_local, stage=stage_key, targets=targets)
            if run_local is not None
            else 0
        )
        if resume_index < 0:
            if job.run_id is not None:
                writer_runs.add_warning(
                    conn,
                    job.run_id,
                    code=writer_runs.CHECKPOINT_MISMATCH_WARNING,
                    message=(
                        "The draft changed after restart, so this review resumed from "
                        "the last stable lens."
                    ),
                    replace=True,
                )
            resume_index = 0
        save_checkpoint(stage_key, index=resume_index, targets=targets)
        if not (
            capabilities.parallel_requests
            and capabilities.parallel_concurrency > 1
            and len(work) > 1
            and resume_index == 0
        ):
            for index, (stage, messages, max_depth) in enumerate(
                work[resume_index:], start=resume_index
            ):
                if not lens(stage, messages, max_depth):
                    return False
                save_checkpoint(stage_key, index=index + 1, targets=targets)
            return True

        artifacts.set_artifact_state(
            conn,
            job.artifact_id,
            artifacts.GENERATING,
            f"{work[0][0].split(':', 1)[0]} in parallel",
        )

        coordinator = _CaptureCoordinator()

        def run(
            indexed_item: tuple[int, tuple[str, list[dict[str, str]], int]],
        ) -> _CapturedLens:
            worker_index, item = indexed_item
            stage, messages, max_depth = item
            return _parallel_review_run(
                config,
                job.artifact_id,
                class_id,
                stage,
                messages,
                max_depth,
                job._deadline,
                coordinator,
                worker_index,
                job.run_id,
            )

        with ThreadPoolExecutor(
            max_workers=min(capabilities.parallel_concurrency, len(work))
        ) as pool:
            outcomes = list(pool.map(run, enumerate(work)))

        # `map` retains section order. All model work has finished; the owner connection
        # is now the only writer and replays findings lens-by-lens, call-by-call.
        first_error: LyraError | None = None
        completed_index = 0
        for outcome in outcomes:
            if _cancel_requested(conn, job):
                return False
            _require_body_snapshot(conn, part_id, body_snapshot)
            artifacts.set_artifact_state(
                conn,
                job.artifact_id,
                artifacts.GENERATING,
                f"Landing findings: {outcome.stage}",
            )
            registry, effects = writer_tools.build_registry(
                conn, job.artifact_id, writer_tools.REVIEWER, run_id=job.run_id
            )
            confirmed.extend(outcome.confirmed_ids)
            for finding in outcome.findings:
                registry["add_comment"].handler(**finding)
            filed.extend(effects.filed_comment_ids or [])
            confirmed.extend(effects.confirmed_comment_ids or [])
            try:
                observe_result(outcome.stage, outcome.result)
            except LyraError as exc:
                if first_error is None:
                    first_error = exc
            if first_error is None:
                completed_index += 1
            save_checkpoint(stage_key, index=completed_index, targets=targets)
        if first_error is not None:
            raise first_error
        return True

    stage_order = ["structure", "argument", "prose", "claims"]
    if job.depth == "deep":
        stage_order.append("skeptic")
    stage_key = (
        str(checkpoint_payload.get("stage") or "") if isinstance(checkpoint_payload, dict) else ""
    )
    if stage_key == "done":
        artifacts.set_problems_done(conn, job.artifact_id, total_lenses)
        _close(
            conn,
            job.artifact_id,
            part_id,
            class_id,
            filed,
            confirmed,
            run_id=job.run_id,
            expected_body=body_snapshot,
        )
        return
    stage_index = stage_order.index(stage_key) if stage_key in stage_order else 0

    structure_ran = stage_index <= 0 and lens(
        "Reviewing structure",
        prompts.build_review_structure_prompt(
            title,
            sections.outline(body),
            sections.heading_lines(body),
            brief_block,
            prompts.format_facts_block(select_active_facts(conn, class_id)),
            plan_block,
            ledger_block,
        ),
        pass_budget.tool_loop_depth,
    )
    if structure_ran:
        completed_lenses = max(completed_lenses, 1)
        artifacts.increment_problems_done(conn, job.artifact_id)
        save_checkpoint("argument")
    if _cancel_requested(conn, job):
        return

    # Lens 2: the argument, judged at the seams. One section has no handoffs to judge.
    body = str(artifacts.get_part(conn, part_id)["content"])
    targets = _targets(body)
    argument_ran = stage_index <= 1 and _time_remaining(job)
    if len(targets) > 1 and stage_index <= 1:
        argument_ran = lens(
            "Reviewing the argument",
            prompts.build_review_argument_prompt(
                title,
                sections.outline(body),
                _seams(targets),
                brief_block,
                plan_block,
                ledger_block,
            ),
            pass_budget.tool_loop_depth,
        )
    elif stage_index <= 1 and not argument_ran:
        budget_exhausted = True
    if argument_ran:
        completed_lenses = max(completed_lenses, 2)
        artifacts.increment_problems_done(conn, job.artifact_id)
        save_checkpoint("prose")
    if _cancel_requested(conn, job):
        return

    # Lenses 3 and 4: per section, re-resolved against the body as it stands now -
    # the student kept the pen through all of this, so every pass re-reads.
    for stage_position, (stage_key_name, display_name, build) in enumerate(
        (
            ("prose", "Reviewing prose", prompts.build_review_prose_prompt),
            ("claims", "Reviewing claims", prompts.build_review_claims_prompt),
        ),
        start=2,
    ):
        if stage_index > stage_position:
            continue
        body = str(artifacts.get_part(conn, part_id)["content"])
        work: list[tuple[str, list[dict[str, str]], int]] = []
        for target in _targets(body):
            fresh_body = str(artifacts.get_part(conn, part_id)["content"])
            fresh = sections.extract(fresh_body, target.number)
            if fresh is None or fresh.is_empty:
                continue
            work.append(
                (
                    f"{display_name}: {fresh.number} {fresh.title}".strip(),
                    build(
                        title,
                        fresh.text,
                        brief_block,
                        prompts.format_plan_block(plan, fresh.number),
                        ledger_block,
                    ),
                    pass_budget.tool_loop_depth,
                ),
            )
        if section_lenses(stage_key_name, work):
            completed_lenses = max(completed_lenses, stage_position + 1)
            artifacts.increment_problems_done(conn, job.artifact_id)
            if stage_key_name == "prose":
                next_stage = "claims"
            elif job.depth == "deep":
                next_stage = "skeptic"
            else:
                next_stage = "done"
            save_checkpoint(next_stage)
        if _cancel_requested(conn, job):
            return

    if job.depth == "deep" and stage_index <= 4:
        body = str(artifacts.get_part(conn, part_id)["content"])
        work = []
        for target in _targets(body):
            work.append(
                (
                    f"Reviewing section {target.number} with the full skeptic rubric",
                    prompts.build_review_skeptic_prompt(
                        title,
                        target.text,
                        brief_block,
                        prompts.format_plan_block(plan, target.number),
                        ledger_block,
                    ),
                    pass_budget.tool_loop_depth,
                ),
            )
        if section_lenses("skeptic", work):
            completed_lenses = max(completed_lenses, total_lenses)
            artifacts.increment_problems_done(conn, job.artifact_id)
            save_checkpoint("done")
        if _cancel_requested(conn, job):
            return

    interrupted = (
        f"Review stopped: the {job.depth} time budget was exhausted." if budget_exhausted else None
    )
    if interrupted is not None:
        raise LyraError(interrupted + " Filed comments were kept.")
    _close(
        conn,
        job.artifact_id,
        part_id,
        class_id,
        filed,
        confirmed,
        run_id=job.run_id,
        expected_body=body_snapshot,
    )


def _close(
    conn: sqlite3.Connection,
    artifact_id: int,
    part_id: int,
    class_id: int,
    filed: list[int],
    confirmed: list[int],
    interrupted_detail: str | None = None,
    *,
    run_id: int | None = None,
    expected_body: dict[str, object] | None = None,
) -> None:
    """Settle ready and say what the review found, in the writer conversation."""
    open_sessions = sessions.writer_sessions_for_part(conn, part_id)
    if open_sessions:
        session_id = int(open_sessions[0]["id"])
    else:
        session_id = int(
            sessions.create_session(conn, class_id, artifact_part_id=part_id, mode=sessions.WRITER)[
                "id"
            ]
        )
    try:
        conn.execute("begin immediate")
        if run_id is not None:
            current = writer_runs.latest_run(conn, artifact_id)
            if (
                current is None
                or int(current["id"]) != run_id
                or current["status"] not in (writer_runs.QUEUED, writer_runs.RUNNING)
            ):
                conn.rollback()
                return
            if expected_body is not None:
                _require_body_snapshot(conn, part_id, expected_body)
            # Commit summary, terminal run and artifact mirror together. A restart
            # cannot replay a summary whose completion marker was never persisted.
            conn.execute(
                "update writer_runs set status = ?, finished_at = datetime('now'), "
                "updated_at = datetime('now'), error_message = null where id = ?",
                (writer_runs.COMPLETED, run_id),
            )
        if expected_body is not None:
            _require_body_snapshot(conn, part_id, expected_body)
        detail = interrupted_detail or _severity_line(conn, filed, confirmed)
        message = interrupted_detail or _summary_message(conn, filed, confirmed)
        standing = [
            thread
            for thread in comments.unresolved_threads(
                conn, part_id, str(artifacts.get_part(conn, part_id)["content"])
            )
            if int(thread["id"]) not in {*filed, *confirmed}
        ]
        if standing and interrupted_detail is None:
            count = len(standing)
            suffix = f"{count} earlier comment{'s remain' if count != 1 else ' remains'} open."
            if not filed and not confirmed:
                detail = "Review complete: no new comments. " + suffix
                message = "No new margin comments were filed. " + suffix
            else:
                detail += " " + suffix
                message += "\n\n" + suffix
        sessions.insert_message(conn, session_id, "assistant", message)
        conn.execute(
            "update artifacts set state = ?, stage_detail = ?, updated_at = datetime('now'), "
            "writer_job_completed_at = datetime('now') where id = ?",
            (artifacts.READY, detail, artifact_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _severity_line(conn: sqlite3.Connection, filed: list[int], confirmed: list[int]) -> str:
    """The settled strip's one line: counts by severity, in the scale's order.

    Findings that were already open count toward the severities and are named separately:
    a second review of an unresolved draft files nothing, and saying "no findings" there
    would read as an all-clear on a draft whose comments are all still open.
    """
    standing = _unique_confirmed(filed, confirmed)
    if not filed and not standing:
        return _COMPLETE_EMPTY_DETAIL
    counts: dict[str, int] = {}
    for root in _filed_roots(conn, [*filed, *standing]):
        severity = str(root["severity"] or "note")
        counts[severity] = counts.get(severity, 0) + 1
    rendered = ", ".join(
        f"{counts[severity]} {severity}" for severity in comments.SEVERITIES if severity in counts
    )
    suffix = _CONFIRMED_SUFFIX.format(count=len(standing)) if standing else ""
    return f"Review complete: {rendered}{suffix}."


def _unique_confirmed(filed: list[int], confirmed: list[int]) -> list[int]:
    """Confirmed findings, deduplicated, and never double-counting one just filed."""
    seen = set(filed)
    unique: list[int] = []
    for comment_id in confirmed:
        if comment_id not in seen:
            seen.add(comment_id)
            unique.append(comment_id)
    return unique


def _summary_message(conn: sqlite3.Connection, filed: list[int], confirmed: list[int]) -> str:
    """The closing chat message: counts, then the findings that matter most.

    The comments are the review; this points at them rather than restating them.
    """
    standing = _unique_confirmed(filed, confirmed)
    if not filed and not standing:
        return (
            "I reviewed the draft and filed no comments: the structure, the argument, "
            "the prose, and the claims all held up under this read."
        )
    if not filed:
        return (
            f"I reviewed the draft again and reached the same {len(standing)} finding(s) "
            "already open in the Comments tab. Nothing new: this read found no findings "
            "beyond the ones still waiting on you."
        )
    roots = _filed_roots(conn, filed)
    line = _severity_line(conn, filed, confirmed).removeprefix("Review complete: ").rstrip(".")
    worst = sorted(
        roots, key=lambda root: (_SEVERITY_RANK.get(str(root["severity"]), 99), int(root["id"]))
    )[:2]
    matter = "\n".join(
        f"- {str(root['severity'] or 'note').capitalize()}: {root['body']}" for root in worst
    )
    return (
        f"I reviewed the draft and filed {line}. What matters most:\n\n{matter}\n\n"
        "The full list is in the Comments tab, anchored to the passages it is about."
    )


def _filed_roots(conn: sqlite3.Connection, filed: list[int]) -> list[dict[str, object]]:
    """The filed comments' rows, in filing order."""
    if not filed:
        return []
    placeholders = ", ".join("?" for _ in filed)
    rows = conn.execute(
        f"select id, severity, quote, body from draft_comments "  # noqa: S608
        f"where id in ({placeholders}) order by id",
        filed,
    ).fetchall()
    return [dict(row) for row in rows]


def _body_part(conn: sqlite3.Connection, artifact_id: int) -> dict[str, object]:
    """The draft's one body part."""
    for part in artifacts.list_parts(conn, artifact_id):
        if part["kind"] == artifacts.DRAFT_BODY:
            return part
    raise NotFoundError("That draft has no body.")


def _targets(body: str) -> list[sections.Section]:
    """The sections worth reviewing: every leaf with prose, the preamble included.

    Parents are reviewed through their children, exactly as they are drafted; empty
    sections have nothing to review, and their absence from the count is honest.
    """
    parsed = sections.parse(body)
    leaves: list[sections.Section] = []
    for index, section in enumerate(parsed):
        following = parsed[index + 1] if index + 1 < len(parsed) else None
        is_parent = following is not None and following.start < section.end
        if not is_parent and not section.is_empty:
            leaves.append(section)
    return leaves


def _seams(targets: list[sections.Section]) -> str:
    """Every handoff between consecutive reviewed sections: tail of one, head of the next."""
    return sections.seams(targets, SEAM_TAIL_WORDS, SEAM_HEAD_WORDS)
