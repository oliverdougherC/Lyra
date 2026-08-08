"""The draft pass: structure first, then sections, serially, against one endpoint.

A long document is not drafted in one shot, and a good draft goes through once for
structure before any prose. The pass is deterministic code dispatching one model call
per stage - never an agent improvising control flow - in two stages:

**Structure.** Runs only when the document has no headings. On an empty document the
skeleton lands directly (each landing is a revision, so history holds the before). On
a document holding unheaded prose the skeleton is a *proposal* - reorganizing the
student's words is exactly what a pending edit is for - and the pass parks: back to
`ready` with `stage_detail` saying the outline awaits review. The student resolves the
edit and runs the pass again; resuming is pressing the same button, not a second
concept. A document that already has headings skips this stage entirely - the pass
respects structure the student made, and redesigning it is the chat's `propose_revision`
conversation, not a background job's opinion.

**Sections.** Each leaf section in outline order, one model call each, scoped tight:
the brief, the cheap outline, the tail of the preceding text for the transition in,
the next heading for the transition out, and a per-section retrieval. What comes back
lands under the same rule the writer's tools enforce - *directly* into a section that
is empty at land time (re-checked then, not at pass start, because the student may
have typed meanwhile), and as a proposal into one that is not. Every proposal in a
pass coalesces into the one pending edit, so the student reviews one diff however many
sections it touches.

An instruction turns the same stage into an iteration pass ("tighten the argument"),
optionally filtered to named sections. That is how a draft gets polished over and over
without any new machinery.

Progress is the artifact row: `problems_total`/`problems_done` count sections and
`stage_detail` names the one in flight. A failed or interrupted pass costs the rest of
the pass, never what already landed, and never the student's own words.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import math
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
    mathnorm,
    sections,
    source_ledger,
    suggestions,
    web_research,
    writer_budgets,
    writer_plans,
    writer_runs,
)
from backend.core.app_settings import (
    NO_ENDPOINT,
    REMOTE_UNACKNOWLEDGED,
    TutorConfig,
    document_text_allowed,
    resolve_tutor_config,
)
from backend.core.errors import LyraError, NotFoundError
from backend.core.profiles import select_active_facts
from backend.llm import budget, client, prompts, replies
from backend.rag.retrieve import retrieve
from backend.storage.database import connect

logger = logging.getLogger(__name__)
_writer_local = threading.local()

# Retrieval budgets, in estimated tokens. The structure stage asks one broad question
# (what is this assignment about); a section stage asks a narrow one, at the budget
# the caret's /write proved out.
STRUCTURE_RETRIEVAL_BUDGET = 2_500
SECTION_RETRIEVAL_BUDGET = 2_000

# How much of the preceding text a section run sees, for the transition in.
PREVIOUS_TAIL_WORDS = 300

BLOCKED_MESSAGES = {
    NO_ENDPOINT: "No tutor endpoint is configured. Add one in Settings, then draft.",
    REMOTE_UNACKNOWLEDGED: (
        "Your tutor endpoint is not on this machine, and a draft pass has to send it "
        "your draft. Allow that in Settings, then draft."
    ),
}

PARKED_DETAIL = "The proposed outline is waiting for review. Accept or reject it, then draft again."
NO_CHANGES_DETAIL = "The pass suggested no changes."
_FAILED_DETAIL = "The pass did not finish."
_INCOMPLETE_DETAIL = (
    "Done, but {count} section{plural} did not reach the assigned length within the "
    "bounded continuation passes. The prose completed so far was kept; a deeper pass "
    "gives the writer more attempts."
)
_WEB_RESEARCH_DEGRADED_DETAIL = (
    "Web research was unavailable for {count} section{plural}; the pass continued with "
    "course material only."
)
_WEB_RESEARCH_WARNING = "Web research was unavailable; continued with course material only."

# No section is worth writing to a target below this: dividing a short brief across many
# sections otherwise asks for two-sentence sections, which read as stubs however well
# they are written.
MINIMUM_SECTION_WORDS = 120

# A requested document length is a completion condition. Page-to-word conversion is
# necessarily approximate, so a section within five percent is complete; anything
# shorter gets another bounded append-only generation call instead of being accepted as
# a mysteriously short final answer.
SECTION_COMPLETION_RATIO = 0.95
CONTINUATION_TAIL_WORDS = 240

# Under this fraction of its target a section is underdeveloped rather than merely brief.
# Looser than the drafting retry's threshold: by now the section has already had its
# chance to be asked again, and rewriting a section that is nearly there costs more than
# it gains.
THIN_SECTION_RATIO = 0.55

# The revise stage judges joins on more text than the reviewer does: it is deciding
# whether to rewrite a section, not filing a comment on one sentence.
REVISE_SEAM_TAIL_WORDS = 80
REVISE_SEAM_HEAD_WORDS = 50
_EMPTY_STRUCTURE_ERROR = "The model returned nothing for the document's structure."
_NO_SECTIONS_DETAIL = "There are no sections to draft."
_NO_SECTION_MESSAGE = 'No section matches "{ref}".'
PLAN_PARKED_DETAIL = "The writing plan is ready. Review or edit it, then draft again."


@dataclass(frozen=True)
class PassJob:
    """One queued draft pass.

    `instruction` turns the section stage into a lens ("tighten the argument");
    `section_refs` filters it to named sections. Both empty is the full draft pass.
    """

    artifact_id: int
    instruction: str | None = None
    section_refs: tuple[str, ...] = ()
    depth: str = "quick"
    pause_at_plan: bool = False
    address_comment_id: int | None = None
    run_id: int | None = None
    _deadline: float | None = None


@dataclass(frozen=True)
class _ResearchWork:
    section_ref: str
    plan_entry: dict[str, object] | None
    messages: list[dict[str, str]]
    candidate_source_ids: tuple[int, ...]
    title: str
    plan_block: str
    context_block: str
    research_warning: str | None = None


def _time_remaining(job: PassJob) -> bool:
    return job._deadline is None or time.monotonic() < job._deadline


def _require_time(job: PassJob) -> None:
    if not _time_remaining(job):
        raise LyraError(f"The {job.depth} writing time budget was exhausted.")


def enqueue(job: PassJob) -> None:
    """Queue a draft pass on the shared drafting worker."""
    drafting.enqueue(job)


def run_pass(job: PassJob) -> None:
    """Run one pass. The worker calls this; tests call it directly."""
    conn = connect()
    try:
        if job.run_id is not None:
            writer_runs.mark_running(conn, job.run_id)
        _run(conn, job)
        if job.run_id is not None and not _cancel_requested(conn, job):
            writer_runs.mark_completed(conn, job.run_id)
        conn.execute(
            "update artifacts set writer_job_completed_at = datetime('now') where id = ?",
            (job.artifact_id,),
        )
        conn.commit()
    except NotFoundError:
        # Deleted between enqueue and run: the de-facto cancel, as everywhere.
        logger.info("Draft %s vanished before its pass ran", job.artifact_id)
    except Exception as exc:
        conn.rollback()
        _settle_failed(conn, job, exc)
    finally:
        if hasattr(_writer_local, "deadline"):
            del _writer_local.deadline
        conn.close()


# The worker dispatches by type; registering at import time is what makes
# `writer_pipeline.enqueue` legal to call.
drafting.register_runner(PassJob, run_pass)

_PASS_CANCELLED_DETAIL = "The pass was cancelled. Finished sections were kept."  # noqa: S105


@dataclass(frozen=True)
class _PassResumeState:
    stage: str | None
    start_index: int
    research_index: int
    changed: bool
    cut_off: int
    degraded_web_research: int
    owned: dict[str, str]
    direct_landings: set[str]


def _checkpoint(
    conn: sqlite3.Connection,
    job: PassJob,
    stage: str,
    *,
    index: int = 0,
    targets: tuple[str, ...] = (),
    data: dict[str, object] | None = None,
) -> None:
    if job.run_id is None:
        return
    writer_runs.checkpoint(conn, job.run_id, stage=stage, index=index, targets=targets, data=data)


def _cancel_requested(conn: sqlite3.Connection, job: PassJob) -> bool:
    if not writer_runs.cancel_requested(conn, job.run_id):
        return False
    if job.run_id is not None:
        writer_runs.mark_cancelled(conn, job.run_id)
    artifacts.set_artifact_state(conn, job.artifact_id, artifacts.CANCELLED, _PASS_CANCELLED_DETAIL)
    conn.execute(
        "update artifacts set writer_job_completed_at = datetime('now') where id = ?",
        (job.artifact_id,),
    )
    conn.commit()
    return True


def _resume_state(
    conn: sqlite3.Connection, job: PassJob, targets: list[tuple[str, str]]
) -> _PassResumeState:
    if job.run_id is None:
        return _PassResumeState(None, 0, 0, False, 0, 0, {}, set())
    run = writer_runs.get_run(conn, job.run_id)
    checkpoint = run.get("checkpoint")
    if not isinstance(checkpoint, dict):
        return _PassResumeState(None, 0, 0, False, 0, 0, {}, set())
    data = checkpoint.get("data")
    stored = dict(data) if isinstance(data, dict) else {}
    stage = str(checkpoint.get("stage") or "") or None
    claimed_section_index = (
        int(checkpoint.get("index") or 0) if stage in {"sections", "revise", "weave", "done"} else 0
    )
    claimed_research_index = int(checkpoint.get("index") or 0) if stage == "research" else 0
    processed = stored.get("processed_sections")
    entries = processed if isinstance(processed, list) else []
    owned: dict[str, str] = {}
    direct_landings: set[str] = set()
    validated = 0
    mismatch = False
    body = str(artifacts.get_part(conn, int(_body_part(conn, job.artifact_id)["id"]))["content"])
    for entry in entries[:claimed_section_index]:
        if not isinstance(entry, dict):
            mismatch = True
            break
        ref = str(entry.get("ref") or "").strip()
        if validated >= len(targets) or ref != targets[validated][0]:
            mismatch = True
            break
        current = sections.extract(body, ref)
        if current is None:
            mismatch = True
            break
        owned_hash = str(entry.get("owned_hash") or "")
        direct = bool(entry.get("direct_landed", False))
        if owned_hash:
            current_hash = _section_hash(current.text)
            if current_hash != owned_hash:
                mismatch = True
                break
            owned[ref] = current.text
            if direct:
                direct_landings.add(ref)
        validated += 1
    if mismatch:
        writer_runs.add_warning(
            conn,
            job.run_id,
            code=writer_runs.CHECKPOINT_MISMATCH_WARNING,
            message=(
                "The draft changed after restart, so this pass resumed from the last "
                "validated section boundary."
            ),
            replace=True,
        )
    start_index = validated if stage in {"sections", "revise", "weave", "done"} else 0
    if stage in {"revise", "weave", "done"} and start_index < len(targets):
        stage = "sections"
    if stage == "research":
        claimed_targets = checkpoint.get("targets")
        current_targets = [f"{number} {title}".strip() for number, title in targets]
        if (
            not isinstance(claimed_targets, list)
            or claimed_targets[: len(current_targets)] != current_targets[: len(claimed_targets)]
        ):
            writer_runs.add_warning(
                conn,
                job.run_id,
                code=writer_runs.CHECKPOINT_MISMATCH_WARNING,
                message=(
                    "The draft changed after restart, so research resumed from the "
                    "last stable boundary."
                ),
                replace=True,
            )
            claimed_research_index = 0
    return _PassResumeState(
        stage,
        start_index,
        claimed_research_index,
        bool(stored.get("changed", False)),
        int(stored.get("cut_off", 0) or 0),
        int(stored.get("degraded_web_research", 0) or 0),
        owned,
        direct_landings,
    )


def _checkpoint_data(
    job: PassJob,
    *,
    processed_sections: list[dict[str, object]],
    changed: bool,
    cut_off: int,
    degraded_web_research: int,
) -> dict[str, object]:
    return {
        "processed_sections": processed_sections,
        "changed": changed,
        "cut_off": cut_off,
        "degraded_web_research": degraded_web_research,
        "address_comment_id": job.address_comment_id,
    }


def _section_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _settle_failed(conn: sqlite3.Connection, job: PassJob, exc: Exception) -> None:
    """Failed, with the reason: the pass failed, what landed stays landed.

    `failed` rather than `ready` for the same reason the review settles that way - the
    workspace only shows `error_message` beside that state, so a pass that died to a
    misnamed section or an unreachable endpoint used to settle silently, looking like a
    pass that simply had nothing to suggest.
    """
    row = conn.execute("select id from artifacts where id = ?", (job.artifact_id,)).fetchone()
    if row is None:
        return
    message = exc.message if isinstance(exc, LyraError) else str(exc)
    artifacts.mark_artifact_failed(conn, job.artifact_id, _FAILED_DETAIL, message)
    if job.run_id is not None:
        writer_runs.mark_failed(conn, job.run_id, message)
    conn.execute(
        "update artifacts set writer_job_completed_at = datetime('now') where id = ?",
        (job.artifact_id,),
    )
    conn.commit()


def _complete(
    config: TutorConfig,
    messages: list[dict[str, str]],
    target_words: int | None = None,
    truncated: list[bool] | None = None,
    schema: client.JsonSchema | None = None,
) -> str:
    """One model call, stripped. The test seam for every stage.

    The ceiling is sent explicitly. Left unset, the reply is bounded by whatever the
    endpoint happened to be launched with - a number Lyra does not know and the student
    did not choose - so a section could come back cut off with nothing to say it had
    been. A cut-off section is still real prose and still lands; `truncated` is how the
    stage learns to say so.
    """
    reserve = budget.generation_reserve(config.context_window)
    max_tokens = min(budget.tokens_for_words(target_words), reserve) if target_words else reserve
    deadline = getattr(_writer_local, "deadline", None)
    timeout = client.BACKGROUND_TIMEOUT
    if isinstance(deadline, float):
        timeout = max(0.001, min(timeout, deadline - time.monotonic()))
    return asyncio.run(
        client.complete(
            config.endpoint_url,
            config.api_key,
            config.model,
            messages,
            request_timeout=timeout,
            max_tokens=max_tokens,
            truncated=truncated,
            schema=schema,
            temperature=client.DETERMINISTIC_TEMPERATURE if schema else None,
        )
    ).strip()


def _run(conn: sqlite3.Connection, job: PassJob) -> None:
    writer_budgets.validate_depth(job.depth)
    if job._deadline is None:
        job = replace(
            job,
            _deadline=time.monotonic() + writer_budgets.get_budget(job.depth).wall_clock_seconds,
        )
    _writer_local.deadline = job._deadline
    artifact = artifacts.get_artifact(conn, job.artifact_id)
    if artifact["kind"] != artifacts.KIND_DRAFT:
        raise NotFoundError("That draft does not exist.")
    class_id = int(artifact["class_id"])
    part = _body_part(conn, job.artifact_id)
    blocked = document_text_allowed(conn)
    if blocked is not None:
        raise LyraError(BLOCKED_MESSAGES.get(blocked, BLOCKED_MESSAGES[NO_ENDPOINT]))
    config = resolve_tutor_config(conn)

    conn.execute(
        "update artifacts set writer_job_kind = 'pass', writer_job_depth = ?, "
        "writer_job_started_at = datetime('now'), writer_job_completed_at = null where id = ?",
        (job.depth, job.artifact_id),
    )
    conn.commit()
    run = writer_runs.get_run(conn, job.run_id) if job.run_id is not None else None
    checkpoint = run.get("checkpoint") if run is not None else None
    if not isinstance(checkpoint, dict):
        _checkpoint(conn, job, "start")
    if _cancel_requested(conn, job):
        return

    artifacts.set_artifact_state(
        conn, job.artifact_id, artifacts.GENERATING, "Reading the document"
    )
    conn.execute("update artifacts set error_message = null where id = ?", (job.artifact_id,))
    conn.commit()

    body = str(artifacts.get_part(conn, int(part["id"]))["content"])
    structured = any(section.level > 0 for section in sections.parse(body))
    plan = _active_plan(conn, job.artifact_id)
    prefetched_reply: str | None = None
    resume_stage = str(checkpoint.get("stage") or "") if isinstance(checkpoint, dict) else ""
    if not job.section_refs and plan is None and resume_stage in {"", "start", "planning"}:
        _checkpoint(conn, job, "planning")
        parked, plan, prefetched_reply = _planning_stage(
            conn, job, artifact, config, class_id, int(part["id"]), body, structured
        )
        if parked:
            return
    elif not structured and not job.section_refs and resume_stage in {"", "start", "structure"}:
        _checkpoint(conn, job, "structure")
        parked = _structure_stage(conn, job, artifact, config, class_id, int(part["id"]))
        if parked:
            return
    _section_stage(
        conn,
        job,
        artifact,
        config,
        class_id,
        int(part["id"]),
        plan,
        prefetched_reply=prefetched_reply,
    )


def _planning_stage(
    conn: sqlite3.Connection,
    job: PassJob,
    artifact: dict[str, object],
    config: TutorConfig,
    class_id: int,
    part_id: int,
    body: str,
    structured: bool,
) -> tuple[bool, dict[str, object] | None, str | None]:
    """Build and persist a narrow-call plan, with a legacy-outline escape hatch.

    The escape hatch matters for old endpoints and queued jobs: if the first constrained
    planning answer is instead a markdown skeleton, it is handled exactly as the former
    structure stage handled it. That preserves direct-empty/proposal safety while new
    endpoints get the persistent process.
    """
    artifacts.set_artifact_state(
        conn, job.artifact_id, artifacts.GENERATING, "Analyzing the assignment brief"
    )
    _require_time(job)
    brief = briefs.get_brief(conn, job.artifact_id)
    brief_block = prompts.format_brief_block(brief)
    first = _complete(
        config,
        prompts.build_plan_brief_prompt(
            str(artifact["title"]),
            body,
            brief_block,
            job.instruction,
            prompts.format_length_block(_target_words(conn, job)),
        ),
    )
    analysis = replies.loads_object(first)
    if analysis is None:
        if structured:
            # Legacy endpoints answer the first planning question as if drafting had
            # begun (including an empty answer). Preserve that already-paid-for first
            # section result and continue through the established no-plan path.
            return False, None, first
        if first.lstrip().startswith("#"):
            return _land_skeleton(conn, job, part_id, body, first), None, None
        # An old/small endpoint may ignore constrained planning without returning the
        # former skeleton. Give the established structure prompt its one normal chance.
        return _structure_stage(conn, job, artifact, config, class_id, part_id), None, None

    query = str(analysis.get("task") or "").strip() or str(artifact["title"])
    result = retrieve(conn, class_id, query, STRUCTURE_RETRIEVAL_BUDGET)
    context_block = prompts.format_context_block([vars(chunk) for chunk in result.chunks])

    artifacts.set_artifact_state(conn, job.artifact_id, artifacts.GENERATING, "Selecting a thesis")
    _require_time(job)
    thesis_reply = _complete(
        config,
        prompts.build_plan_thesis_prompt(str(artifact["title"]), analysis, context_block),
        schema=prompts.PLAN_THESIS_SCHEMA,
    )
    thesis_payload = replies.loads_object(thesis_reply)
    if thesis_payload is None or not str(thesis_payload.get("selected") or "").strip():
        raise LyraError("The model did not return a usable thesis plan.")
    thesis = str(thesis_payload["selected"]).strip()

    artifacts.set_artifact_state(
        conn, job.artifact_id, artifacts.GENERATING, "Mapping the argument"
    )
    _require_time(job)
    map_reply = _complete(
        config,
        prompts.build_plan_argument_prompt(thesis, analysis, context_block),
        schema=prompts.PLAN_ARGUMENT_SCHEMA,
    )
    argument_map = replies.loads(map_reply)
    if not isinstance(argument_map, list):
        raise LyraError("The model did not return a usable argument map.")

    artifacts.set_artifact_state(
        conn, job.artifact_id, artifacts.GENERATING, "Planning sections and evidence"
    )
    _require_time(job)
    section_reply = _complete(
        config,
        prompts.build_plan_sections_prompt(
            str(artifact["title"]),
            thesis,
            argument_map,
            _target_words(conn, job),
            context_block,
            sections.outline(body) if structured else "",
        ),
        schema=prompts.PLAN_SECTIONS_SCHEMA,
    )
    section_payload = replies.loads_object(section_reply)
    planned_sections = section_payload.get("sections") if section_payload else None
    if not isinstance(planned_sections, list) or not planned_sections:
        raise LyraError("The model did not return a usable annotated section plan.")

    normalized_sections: list[dict[str, object]] = []
    for ordinal, raw in enumerate(planned_sections):
        if not isinstance(raw, dict):
            continue
        normalized_sections.append(
            {
                **raw,
                "section_ref": str(raw.get("ref") or f"1.{ordinal + 1}"),
                "ordinal": ordinal,
            }
        )
    normalized_sections = _normalize_plan_word_budgets(
        normalized_sections, _target_words(conn, job)
    )
    plan: dict[str, object] = {
        "brief_analysis": json.dumps(
            {
                **analysis,
                "thesis_candidates": thesis_payload.get("candidates", []),
                "thesis_rationale": thesis_payload.get("rationale", ""),
            },
            ensure_ascii=False,
        ),
        "thesis": thesis,
        "argument_map": argument_map,
        "sections": normalized_sections,
    }
    stored = _save_plan(conn, job.artifact_id, plan)
    plan = stored or plan
    if structured:
        if job.pause_at_plan:
            artifacts.set_artifact_state(conn, job.artifact_id, artifacts.READY, PLAN_PARKED_DETAIL)
            return True, plan, None
        return False, plan, None
    skeleton = _plan_skeleton(str(artifact["title"]), normalized_sections)
    parked = _land_skeleton(conn, job, part_id, body, skeleton)
    if parked:
        return True, plan, None
    if job.pause_at_plan:
        artifacts.set_artifact_state(conn, job.artifact_id, artifacts.READY, PLAN_PARKED_DETAIL)
        return True, plan, None
    return False, plan, None


def _land_skeleton(
    conn: sqlite3.Connection, job: PassJob, part_id: int, body: str, skeleton: str
) -> bool:
    """Land/propose a generated skeleton under the existing safety rule."""
    skeleton = mathnorm.normalize(skeleton.strip())
    if not skeleton:
        raise LyraError(_EMPTY_STRUCTURE_ERROR)
    if body.strip():
        suggestions.propose(conn, part_id, skeleton, job.instruction or "structure the document")
        artifacts.set_artifact_state(conn, job.artifact_id, artifacts.READY, PARKED_DETAIL)
        return True
    artifacts.set_part_content(
        conn,
        part_id,
        skeleton + "\n",
        origin=artifacts.GENERATED,
        note="structured the document",
        record_revision=True,
    )
    return False


def _plan_skeleton(title: str, entries: list[object]) -> str:
    """Generate markdown deterministically from the annotated plan."""
    lines = [f"# {title}", ""]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        heading = str(entry.get("title") or entry.get("ref") or "Section").strip()
        job = str(entry.get("job") or entry.get("claim") or "develop this section").strip()
        words = entry.get("word_budget")
        size = (
            f" About {int(words):,} words." if isinstance(words, (int, float)) and words > 0 else ""
        )
        lines.extend([f"## {heading}", "", f"[TODO: {job}{size}]", ""])
    if len(lines) <= 2:
        raise LyraError("The annotated plan had no usable sections.")
    return "\n".join(lines).rstrip()


def _structure_stage(
    conn: sqlite3.Connection,
    job: PassJob,
    artifact: dict[str, object],
    config: TutorConfig,
    class_id: int,
    part_id: int,
) -> bool:
    """Lay the skeleton. Returns True when the pass parked on a proposal."""
    artifacts.set_artifact_state(
        conn, job.artifact_id, artifacts.GENERATING, "Structuring the document"
    )
    body = str(artifacts.get_part(conn, part_id)["content"])
    brief = briefs.get_brief(conn, job.artifact_id)
    summary = str(brief["summary"]).strip() if brief else ""
    query = summary or str(artifact["title"])
    result = retrieve(conn, class_id, query, STRUCTURE_RETRIEVAL_BUDGET)
    skeleton = _complete(
        config,
        prompts.build_structure_prompt(
            str(artifact["title"]),
            body,
            prompts.format_brief_block(brief),
            prompts.format_context_block([vars(chunk) for chunk in result.chunks]),
            prompts.format_facts_block(select_active_facts(conn, class_id)),
            instruction=job.instruction,
            length_block=prompts.format_length_block(_target_words(conn, job)),
        ),
    )
    if not skeleton:
        raise LyraError(_EMPTY_STRUCTURE_ERROR)
    skeleton = mathnorm.normalize(skeleton)

    if body.strip():
        # The skeleton rearranges the student's prose, so it is only ever a proposal,
        # and the pass parks until the student has ruled on it. No hidden continuation:
        # resuming is running the pass again once the document has its structure.
        suggestions.propose(conn, part_id, skeleton, job.instruction or "structure the document")
        artifacts.set_artifact_state(conn, job.artifact_id, artifacts.READY, PARKED_DETAIL)
        return True

    artifacts.set_part_content(
        conn,
        part_id,
        skeleton,
        origin=artifacts.GENERATED,
        note="structured the document",
        record_revision=True,
    )
    return False


def _section_stage(
    conn: sqlite3.Connection,
    job: PassJob,
    artifact: dict[str, object],
    config: TutorConfig,
    class_id: int,
    part_id: int,
    plan: dict[str, object] | None = None,
    prefetched_reply: str | None = None,
) -> None:
    """Draft or revise the target sections, serially, landing each as it completes."""
    body = str(artifacts.get_part(conn, part_id)["content"])
    targets = _targets(body, job.section_refs)
    if not targets:
        artifacts.set_artifact_state(conn, job.artifact_id, artifacts.READY, _NO_SECTIONS_DETAIL)
        return
    resume = _resume_state(conn, job, targets)
    if resume.stage == "done":
        artifacts.set_problems_total(conn, job.artifact_id, len(targets))
        artifacts.set_problems_done(conn, job.artifact_id, len(targets))
        if job.run_id is not None and (degraded_web_research := resume.degraded_web_research):
            writer_runs.add_warning(
                conn,
                job.run_id,
                code=writer_runs.WEB_RESEARCH_DEGRADED_WARNING,
                message=_WEB_RESEARCH_DEGRADED_DETAIL.format(
                    count=degraded_web_research,
                    plural="" if degraded_web_research == 1 else "s",
                ),
                replace=True,
            )
        artifacts.set_artifact_state(
            conn,
            job.artifact_id,
            artifacts.READY,
            _settled_detail(resume.changed, resume.cut_off, resume.degraded_web_research),
        )
        _settle_addressed_comment(conn, job, part_id, resume.direct_landings)
        return
    start_index = resume.start_index
    artifacts.set_problems_total(conn, job.artifact_id, len(targets))
    artifacts.set_problems_done(conn, job.artifact_id, min(start_index, len(targets)))
    processed_sections = [
        {
            "ref": number,
            "owned_hash": _section_hash(text) if number in resume.owned else "",
            "direct_landed": number in resume.direct_landings,
        }
        for number, title in targets[:start_index]
        if (text := resume.owned.get(number, "")) or number not in resume.owned
    ]
    _checkpoint(
        conn,
        job,
        resume.stage or "sections",
        index=start_index,
        targets=tuple(f"{number} {title}".strip() for number, title in targets),
        data=_checkpoint_data(
            job,
            processed_sections=processed_sections,
            changed=resume.changed,
            cut_off=resume.cut_off,
            degraded_web_research=resume.degraded_web_research,
        ),
    )

    # The document's target divided by the sections that will carry it. A section run
    # cannot infer its own share from "5 pages" in the brief, and a model left to guess
    # guesses low every time - which is the whole of "I asked for five pages and got
    # three paragraphs".
    total_words = _target_words(conn, job)
    if plan is not None and total_words:
        instruction_words = briefs.length_target_words(job.instruction, require_unit=True)
        _normalize_runtime_plan_word_budgets(
            plan,
            targets,
            total_words,
            selected_sections_own_total=bool(job.section_refs and instruction_words),
        )
    per_section = max(MINIMUM_SECTION_WORDS, total_words // len(targets)) if total_words else None

    # Sections this pass wrote into itself, and the text it wrote, so the revise stage
    # can tell its own prose from the student's. See `_land`.
    owned: dict[str, str] = dict(resume.owned)

    changed = resume.changed
    cut_off = resume.cut_off
    degraded_web_research = resume.degraded_web_research
    direct_landings: set[str] = set(resume.direct_landings)
    fixed_plan_outline = sections.outline(body) if plan is not None else None
    capabilities = writer_budgets.get_writer_capabilities(conn, class_id)
    if (
        plan is not None
        and capabilities.parallel_requests
        and capabilities.parallel_concurrency > 1
        and len(targets) > 1
        and resume.start_index == 0
        and resume.stage not in {"research", "sections", "revise", "weave", "done"}
    ):
        changed, cut_off, degraded_web_research = _parallel_initial_sections(
            conn,
            job,
            artifact,
            config,
            class_id,
            part_id,
            targets,
            per_section,
            owned,
            plan,
            capabilities.parallel_concurrency,
            direct_landings,
            processed_sections,
        )
        if not job.section_refs and changed:
            _checkpoint(
                conn,
                job,
                "weave",
                index=len(targets),
                targets=tuple(f"{number} {title}".strip() for number, title in targets),
                data=_checkpoint_data(
                    job,
                    processed_sections=processed_sections,
                    changed=changed,
                    cut_off=cut_off,
                    degraded_web_research=degraded_web_research,
                ),
            )
            changed = (
                _weave_stage(
                    conn, job, artifact, config, class_id, part_id, per_section, owned, plan
                )
                or changed
            )
        _checkpoint(
            conn,
            job,
            "done",
            index=len(targets),
            targets=tuple(f"{number} {title}".strip() for number, title in targets),
            data=_checkpoint_data(
                job,
                processed_sections=processed_sections,
                changed=changed,
                cut_off=cut_off,
                degraded_web_research=degraded_web_research,
            ),
        )
        if _cancel_requested(conn, job):
            return
        artifacts.set_artifact_state(
            conn,
            job.artifact_id,
            artifacts.READY,
            _settled_detail(changed, cut_off, degraded_web_research),
        )
        _settle_addressed_comment(conn, job, part_id, direct_landings)
        return

    if plan is not None and resume.stage in {None, "start", "planning", "structure", "research"}:
        research_work = _prepare_research_batch(
            conn, job, artifact, config, class_id, targets, plan
        )
        degraded_web_research = max(
            degraded_web_research,
            sum(1 for work in research_work if work.research_warning),
        )
        research_start = resume.research_index if resume.stage == "research" else 0
        for index, work in enumerate(research_work[research_start:], start=research_start):
            if not _time_remaining(job):
                break
            if _cancel_requested(conn, job):
                return
            _checkpoint(
                conn,
                job,
                "research",
                index=index,
                targets=tuple(f"{number} {title}".strip() for number, title in targets),
                data=_checkpoint_data(
                    job,
                    processed_sections=processed_sections,
                    changed=changed,
                    cut_off=cut_off,
                    degraded_web_research=degraded_web_research,
                ),
            )
            reply = _complete(config, work.messages, schema=prompts.RESEARCH_NOTES_SCHEMA)
            _finish_research_section(conn, job, class_id, plan, work, reply)

    if resume.stage == "done":
        section_iterable = []
    elif resume.stage == "revise" and start_index == len(targets):
        section_iterable: list[tuple[int, tuple[str, str]]] = []
    elif resume.stage == "weave" and start_index == len(targets):
        section_iterable = []
    else:
        section_iterable = list(enumerate(targets[start_index:], start=start_index))

    for target_index, (number, title) in section_iterable:
        if not _time_remaining(job):
            break
        if _cancel_requested(conn, job):
            return
        _checkpoint(
            conn,
            job,
            "sections",
            index=target_index,
            targets=tuple(f"{ref} {heading}".strip() for ref, heading in targets),
            data=_checkpoint_data(
                job,
                processed_sections=processed_sections,
                changed=changed,
                cut_off=cut_off,
                degraded_web_research=degraded_web_research,
            ),
        )
        plan_entry = _section_plan(plan, number, title)
        artifacts.set_artifact_state(
            conn,
            job.artifact_id,
            artifacts.GENERATING,
            f"Drafting {number} {title}".strip(),
        )
        landed, truncated = _run_section(
            conn,
            job,
            artifact,
            config,
            class_id,
            part_id,
            number,
            title,
            int(plan_entry.get("word_budget"))
            if plan_entry and isinstance(plan_entry.get("word_budget"), int)
            else per_section,
            owned,
            plan,
            direct_landings,
            prefetched_reply if target_index == 0 else None,
            fixed_plan_outline,
        )
        changed = landed or changed
        cut_off += 1 if truncated else 0
        if plan is not None and landed:
            changed = (
                _converge_section(
                    conn,
                    job,
                    artifact,
                    config,
                    class_id,
                    part_id,
                    number,
                    title,
                    per_section,
                    owned,
                    plan,
                    direct_landings,
                )
                or changed
            )
        artifacts.increment_problems_done(conn, job.artifact_id)
        processed_sections.append(
            {
                "ref": number,
                "owned_hash": _section_hash(owned[number]) if number in owned else "",
                "direct_landed": number in direct_landings,
            }
        )
        _checkpoint(
            conn,
            job,
            "sections",
            index=target_index + 1,
            targets=tuple(f"{ref} {heading}".strip() for ref, heading in targets),
            data=_checkpoint_data(
                job,
                processed_sections=processed_sections,
                changed=changed,
                cut_off=cut_off,
                degraded_web_research=degraded_web_research,
            ),
        )

    # A first draft is not a draft. A full pass now reads back what it wrote and fixes
    # what it can see wrong with it, which is the difference between a pipeline that
    # emits sections and one that produces a document.
    if not job.section_refs and changed and plan is None:
        # `or changed`: a revise round that finds nothing to fix does not un-write the
        # sections the pass just landed.
        _checkpoint(
            conn,
            job,
            "revise",
            index=len(targets),
            targets=tuple(f"{number} {title}".strip() for number, title in targets),
            data=_checkpoint_data(
                job,
                processed_sections=processed_sections,
                changed=changed,
                cut_off=cut_off,
                degraded_web_research=degraded_web_research,
            ),
        )
        changed = (
            _revise_stage(conn, job, artifact, config, class_id, part_id, per_section, owned)
            or changed
        )
        _checkpoint(
            conn,
            job,
            "done",
            index=len(targets),
            targets=tuple(f"{number} {title}".strip() for number, title in targets),
            data=_checkpoint_data(
                job,
                processed_sections=processed_sections,
                changed=changed,
                cut_off=cut_off,
                degraded_web_research=degraded_web_research,
            ),
        )
    elif not job.section_refs and changed and plan is not None:
        _checkpoint(
            conn,
            job,
            "weave",
            index=len(targets),
            targets=tuple(f"{number} {title}".strip() for number, title in targets),
            data=_checkpoint_data(
                job,
                processed_sections=processed_sections,
                changed=changed,
                cut_off=cut_off,
                degraded_web_research=degraded_web_research,
            ),
        )
        changed = (
            _weave_stage(conn, job, artifact, config, class_id, part_id, per_section, owned, plan)
            or changed
        )
        _checkpoint(
            conn,
            job,
            "done",
            index=len(targets),
            targets=tuple(f"{number} {title}".strip() for number, title in targets),
            data=_checkpoint_data(
                job,
                processed_sections=processed_sections,
                changed=changed,
                cut_off=cut_off,
                degraded_web_research=degraded_web_research,
            ),
        )
    if _cancel_requested(conn, job):
        return

    if degraded_web_research and job.run_id is not None:
        writer_runs.add_warning(
            conn,
            job.run_id,
            code=writer_runs.WEB_RESEARCH_DEGRADED_WARNING,
            message=_WEB_RESEARCH_DEGRADED_DETAIL.format(
                count=degraded_web_research,
                plural="" if degraded_web_research == 1 else "s",
            ),
            replace=True,
        )

    artifacts.set_artifact_state(
        conn,
        job.artifact_id,
        artifacts.READY,
        _settled_detail(changed, cut_off, degraded_web_research),
    )
    _settle_addressed_comment(conn, job, part_id, direct_landings)


def _parallel_initial_sections(
    conn: sqlite3.Connection,
    job: PassJob,
    artifact: dict[str, object],
    config: TutorConfig,
    class_id: int,
    part_id: int,
    targets: list[tuple[str, str]],
    per_section: int | None,
    owned: dict[str, str],
    plan: dict[str, object],
    concurrency: int,
    direct_landings: set[str],
    processed_sections: list[dict[str, object]],
) -> tuple[bool, int, int]:
    """Fan out research/draft model calls; prepare and land deterministically."""
    body = str(artifacts.get_part(conn, part_id)["content"])
    research_work = _prepare_research_batch(conn, job, artifact, config, class_id, targets, plan)
    degraded_web_research = sum(1 for work in research_work if work.research_warning)

    def research(work: _ResearchWork) -> str:
        _writer_local.deadline = job._deadline
        return _complete(config, work.messages, schema=prompts.RESEARCH_NOTES_SCHEMA)

    if research_work:
        with ThreadPoolExecutor(max_workers=min(concurrency, len(research_work))) as pool:
            research_replies = list(pool.map(research, research_work))
        for work, reply in zip(research_work, research_replies, strict=True):
            _finish_research_section(conn, job, class_id, plan, work, reply)

    prepared: list[tuple[str, str, int | None, list[dict[str, str]]]] = []
    for number, title in targets:
        entry = _section_plan(plan, number, title)
        target = sections.extract(body, number) or sections.extract(body, title)
        if target is None:
            continue
        target_words = (
            int(entry["word_budget"])
            if entry and isinstance(entry.get("word_budget"), int)
            else per_section
        )
        result = retrieve(
            conn, class_id, str(entry.get("job") if entry else title), SECTION_RETRIEVAL_BUDGET
        )
        prepared.append(
            (
                number,
                title,
                target_words,
                prompts.build_section_prompt(
                    str(artifact["title"]),
                    sections.outline(body),
                    target.text,
                    None,  # planned sections are woven after all deterministic landings
                    _next_heading(body, target),
                    job.instruction,
                    prompts.format_brief_block(briefs.get_brief(conn, job.artifact_id)),
                    prompts.format_context_block([vars(chunk) for chunk in result.chunks]),
                    prompts.format_facts_block(select_active_facts(conn, class_id)),
                    target_words=target_words,
                    plan_block=prompts.format_plan_block(plan, number),
                    ledger_block=prompts.format_ledger_block(
                        _ledger_entries(conn, class_id, entry, number)
                    ),
                ),
            )
        )

    def generate(item: tuple[str, str, int | None, list[dict[str, str]]]) -> tuple[str, list[bool]]:
        _, _, target_words, messages = item
        truncated: list[bool] = []
        _writer_local.deadline = job._deadline
        return _complete(config, messages, target_words, truncated), truncated

    # executor.map preserves input order even when calls finish out of order. The owner
    # thread then lands and critiques each section in document order.
    with ThreadPoolExecutor(max_workers=min(concurrency, len(prepared))) as pool:
        generated = list(pool.map(generate, prepared))

    changed = False
    cut_off = 0
    target_keys = tuple(f"{number} {title}".strip() for number, title in targets)
    for item, (reply, truncated) in zip(prepared, generated, strict=True):
        number, title, target_words, messages = item
        reply, incomplete = _finish_section_generation(
            conn,
            job,
            config,
            messages,
            reply,
            bool(truncated),
            target_words,
            number,
            title,
        )
        reply = mathnorm.normalize(reply)
        if reply:
            replacement = reply + ("\n" if not reply.endswith("\n") else "")
            current_body = str(artifacts.get_part(conn, part_id)["content"])
            current_target = sections.extract(current_body, number) or sections.extract(
                current_body, title
            )
            if (
                current_target is not None
                and current_target.end < len(current_body)
                and not replacement.endswith("\n\n")
            ):
                replacement += "\n"
            if _land(
                conn,
                job,
                part_id,
                number,
                title,
                replacement,
                owned,
                direct_landings,
            ):
                changed = True
                changed = (
                    _converge_section(
                        conn,
                        job,
                        artifact,
                        config,
                        class_id,
                        part_id,
                        number,
                        title,
                        per_section,
                        owned,
                        plan,
                        direct_landings,
                    )
                    or changed
                )
        cut_off += 1 if incomplete else 0
        artifacts.increment_problems_done(conn, job.artifact_id)
        processed_sections.append(
            {
                "ref": number,
                "owned_hash": _section_hash(owned[number]) if number in owned else "",
                "direct_landed": number in direct_landings,
            }
        )
        completed = len(processed_sections)
        _checkpoint(
            conn,
            job,
            "sections",
            index=completed,
            targets=target_keys,
            data=_checkpoint_data(
                job,
                processed_sections=processed_sections,
                changed=changed,
                cut_off=cut_off,
                degraded_web_research=degraded_web_research,
            ),
        )
    return changed, cut_off, degraded_web_research


def _settle_addressed_comment(
    conn: sqlite3.Connection,
    job: PassJob,
    part_id: int,
    direct_landings: set[str],
) -> None:
    """Resolve only a direct landing; proposals resolve when their edit is accepted."""
    if job.address_comment_id is None:
        return
    body = str(artifacts.get_part(conn, part_id)["content"])
    addressed_sections = {
        target.number
        for ref in job.section_refs
        if (target := sections.extract(body, ref)) is not None
    }
    if addressed_sections.intersection(direct_landings):
        comments.set_resolved(conn, job.address_comment_id, True)
        return
    pending = suggestions.pending_for_part(conn, part_id)
    if pending is None:
        return
    try:
        conn.execute(
            "insert or ignore into pending_edit_comment_links (edit_id, comment_id) values (?, ?)",
            (int(pending["id"]), job.address_comment_id),
        )
        conn.commit()
    except sqlite3.OperationalError as exc:
        # Rolling-upgrade compatibility: the proposal remains pending and, critically,
        # the comment remains unresolved even before the linkage migration is present.
        if "pending_edit_comment_links" not in str(exc):
            raise


def _prepare_research_section(
    conn: sqlite3.Connection,
    job: PassJob,
    artifact: dict[str, object],
    config: TutorConfig,
    class_id: int,
    number: str,
    title: str,
    plan: dict[str, object],
    plan_entry: dict[str, object] | None,
) -> _ResearchWork | None:
    """Gather fixed research inputs without making the distillation model call."""
    if not _time_remaining(job):
        return None
    artifacts.set_artifact_state(
        conn,
        job.artifact_id,
        artifacts.GENERATING,
        f"Researching {number} {title}".strip(),
    )
    job_text = str((plan_entry or {}).get("job") or title)
    result = retrieve(conn, class_id, job_text, SECTION_RETRIEVAL_BUDGET)
    artifact_title = str(artifact["title"])
    plan_block = prompts.format_plan_block(plan, number)
    course_context_block = prompts.format_context_block([vars(chunk) for chunk in result.chunks])
    part = _body_part(conn, job.artifact_id)
    target = sections.extract(str(artifacts.get_part(conn, int(part["id"]))["content"]), number)
    search_private_context = tuple(
        value
        for value in (
            artifact_title,
            f"{number} {title}".strip(),
            target.text if target is not None else "",
            prompts.format_brief_block(briefs.get_brief(conn, job.artifact_id)),
        )
        if value
    )
    course_source_ids: list[int] = []
    for chunk in result.chunks:
        source = source_ledger.upsert_source(
            conn,
            class_id,
            source_type=source_ledger.COURSE,
            title=chunk.filename,
            document_id=chunk.document_id,
        )
        source_id = int(source["id"])
        course_source_ids.append(source_id)

    # Web research is an optional enhancement. The source module owns its consent and
    # availability checks; an absent provider or disabled toggle leaves course retrieval
    # untouched rather than failing the pass.
    web_context: list[str] = []
    research_warning: str | None = None
    try:
        capabilities = writer_budgets.get_writer_capabilities(conn, class_id)
        if capabilities.allow_web_research:
            results = web_research.search_web(
                job_text,
                allowed=True,
                firecrawl_base_url=capabilities.firecrawl_base_url,
                private_context=search_private_context,
            )
            fetch_limit = writer_budgets.get_budget(job.depth).section_retries
            for candidate in results[:fetch_limit]:
                if not _time_remaining(job):
                    break
                fetched = web_research.fetch_source(
                    str(candidate["url"]),
                    allowed=True,
                    firecrawl_base_url=capabilities.firecrawl_base_url,
                    scrape_enabled=capabilities.firecrawl_scrape_enabled,
                )
                source = source_ledger.upsert_source(
                    conn,
                    class_id,
                    source_type=source_ledger.WEB,
                    title=str(fetched["title"]),
                    url=str(fetched["url"]),
                    accessed_at=str(fetched["accessed_at"]),
                    snapshot=str(fetched["snapshot"]),
                    final_url=str(fetched["final_url"]),
                    content_type=(
                        str(fetched["content_type"]) if fetched["content_type"] else None
                    ),
                    truncated=bool(fetched["truncated"]),
                )
                source_id = int(source["id"])
                web_context.append(
                    f"[@lyra:{source_id}] {source['title']}\n{str(fetched['snapshot'])[:8_000]}"
                )
    except (web_research.WebResearchError, LyraError, OSError, TypeError, ValueError) as exc:
        logger.info(
            "Web research unavailable for section %s; continuing course-only: %s", number, exc
        )
        research_warning = _WEB_RESEARCH_WARNING

    ledger = source_ledger.list_sources(conn, class_id)
    context_block = course_context_block
    if web_context:
        context_block = "\n\n".join(
            block
            for block in (context_block, "Web research candidates:\n" + "\n\n".join(web_context))
            if block
        )
    messages = prompts.build_research_notes_prompt(
        artifact_title,
        plan_block,
        context_block,
        prompts.format_ledger_block(ledger),
    )
    return _ResearchWork(
        number,
        plan_entry,
        messages,
        tuple(dict.fromkeys([*course_source_ids])),
        artifact_title,
        plan_block,
        context_block,
        research_warning,
    )


def _prepare_research_batch(
    conn: sqlite3.Connection,
    job: PassJob,
    artifact: dict[str, object],
    config: TutorConfig,
    class_id: int,
    targets: list[tuple[str, str]],
    plan: dict[str, object],
) -> list[_ResearchWork]:
    """Prepare every section, then freeze one ledger snapshot for every call.

    Research completion persists relied-on excerpts. Preparing all calls before any
    completion prevents serial section order from leaking one section's new excerpt
    into the next section's model input. It also gives both serial and parallel modes
    the same union of course/web candidate metadata.
    """
    work: list[_ResearchWork] = []
    for number, title in targets:
        prepared = _prepare_research_section(
            conn,
            job,
            artifact,
            config,
            class_id,
            number,
            title,
            plan,
            _section_plan(plan, number, title),
        )
        if prepared is not None:
            work.append(prepared)
    ledger_block = prompts.format_ledger_block(source_ledger.list_sources(conn, class_id))
    return [
        replace(
            prepared,
            messages=prompts.build_research_notes_prompt(
                prepared.title,
                prepared.plan_block,
                prepared.context_block,
                ledger_block,
            ),
        )
        for prepared in work
    ]


def _finish_research_section(
    conn: sqlite3.Connection,
    job: PassJob,
    class_id: int,
    plan: dict[str, object],
    work: _ResearchWork,
    reply: str,
) -> None:
    """Validate one research reply, then persist selected excerpts and plan notes."""
    notes = replies.loads_object(reply)
    if notes is None:
        logger.warning("Research notes for section %s were not valid JSON", work.section_ref)
        return
    if work.research_warning:
        notes["warning"] = work.research_warning
    validated_source_ids: list[int] = []
    relied_on = notes.get("relied_on")
    if isinstance(relied_on, list):
        for item in relied_on:
            if not isinstance(item, dict):
                continue
            source_id = item.get("source_id")
            excerpt = str(item.get("excerpt") or "").strip()
            if not isinstance(source_id, int) or not excerpt:
                continue
            try:
                source_ledger.get_source(conn, source_id, class_id=class_id)
                source_ledger.add_relied_on_excerpt(
                    conn,
                    source_id,
                    excerpt,
                    section_ref=work.section_ref,
                )
                validated_source_ids.append(source_id)
            except (ValueError, LyraError) as exc:
                logger.warning(
                    "Researcher selected an invalid excerpt for source %s: %s",
                    source_id,
                    exc,
                )
    if work.plan_entry is not None:
        current_ids = work.plan_entry.get("source_ids")
        bound = (
            [value for value in current_ids if isinstance(value, int)]
            if isinstance(current_ids, list)
            else []
        )
        work.plan_entry["source_ids"] = list(dict.fromkeys([*bound, *validated_source_ids]))
    research = plan.setdefault("research_notes", {})
    if isinstance(research, dict):
        research[work.section_ref] = notes
    _save_research_notes(
        conn,
        job.artifact_id,
        work.section_ref,
        notes,
        list(work.plan_entry.get("source_ids", []))
        if work.plan_entry is not None
        else list(work.candidate_source_ids),
    )


def _research_section(
    conn: sqlite3.Connection,
    job: PassJob,
    artifact: dict[str, object],
    config: TutorConfig,
    class_id: int,
    number: str,
    title: str,
    plan: dict[str, object],
    plan_entry: dict[str, object] | None,
) -> None:
    """Serial reference path for one section's research."""
    work = _prepare_research_section(
        conn, job, artifact, config, class_id, number, title, plan, plan_entry
    )
    if work is None:
        return
    reply = _complete(config, work.messages, schema=prompts.RESEARCH_NOTES_SCHEMA)
    _finish_research_section(conn, job, class_id, plan, work, reply)


def _converge_section(
    conn: sqlite3.Connection,
    job: PassJob,
    artifact: dict[str, object],
    config: TutorConfig,
    class_id: int,
    part_id: int,
    number: str,
    title: str,
    per_section: int | None,
    owned: dict[str, str],
    plan: dict[str, object],
    direct_landings: set[str] | None = None,
) -> bool:
    """Draft -> structured skeptic -> targeted rewrite until pass or depth budget."""
    changed = False
    rounds = writer_budgets.get_budget(job.depth).max_critique_rounds
    for round_number in range(1, rounds + 1):
        if not _time_remaining(job):
            return changed
        body = str(artifacts.get_part(conn, part_id)["content"])
        target = sections.extract(body, number) or sections.extract(body, title)
        if target is None or target.is_empty:
            return changed
        artifacts.set_artifact_state(
            conn,
            job.artifact_id,
            artifacts.GENERATING,
            f"Attacking the argument in {number} (round {round_number})",
        )
        plan_entry = _section_plan(plan, number, title)
        reply = _complete(
            config,
            prompts.build_skeptic_prompt(
                str(artifact["title"]),
                target.text,
                prompts.format_plan_block(plan, number),
                prompts.format_ledger_block(_ledger_entries(conn, class_id, plan_entry, number)),
                _previous_tail(body, target),
                _next_heading(body, target),
            ),
            schema=prompts.SKEPTIC_SCHEMA,
        )
        verdict = replies.loads_object(reply)
        # An endpoint that cannot honor the schema costs this quality check, not the
        # section that already landed.
        if verdict is None or verdict.get("passes") is True:
            return changed
        instruction = str(verdict.get("rewrite_instruction") or "").strip()
        if not instruction:
            faults = verdict.get("faults")
            instruction = (
                "; ".join(str(fault) for fault in faults) if isinstance(faults, list) else ""
            )
        if not instruction or round_number == rounds:
            return changed
        artifacts.set_artifact_state(
            conn,
            job.artifact_id,
            artifacts.GENERATING,
            f"Rewriting {number} after skeptic round {round_number}",
        )
        landed, _ = _run_section(
            conn,
            PassJob(
                job.artifact_id,
                instruction=instruction,
                depth=job.depth,
                address_comment_id=job.address_comment_id,
            ),
            artifact,
            config,
            class_id,
            part_id,
            number,
            title,
            int(plan_entry.get("word_budget"))
            if plan_entry and isinstance(plan_entry.get("word_budget"), int)
            else per_section,
            owned,
            plan,
            direct_landings,
        )
        changed = landed or changed
    return changed


def _weave_stage(
    conn: sqlite3.Connection,
    job: PassJob,
    artifact: dict[str, object],
    config: TutorConfig,
    class_id: int,
    part_id: int,
    per_section: int | None,
    owned: dict[str, str],
    plan: dict[str, object],
) -> bool:
    """Rewrite joins, then perform one continuity read and targeted fixes."""
    changed = False
    body = str(artifacts.get_part(conn, part_id)["content"])
    leaves = [section for section in _leaf_sections(body) if section.level > 0]
    for following in leaves[1:]:
        if not _time_remaining(job):
            return changed
        artifacts.set_artifact_state(
            conn,
            job.artifact_id,
            artifacts.GENERATING,
            f"Rewriting the transition into {following.number}",
        )
        landed, _ = _run_section(
            conn,
            PassJob(
                job.artifact_id,
                instruction=(
                    "Preserve this section's argument and evidence. Rewrite only as much "
                    "of its opening as needed to make the transition from the preceding "
                    "section explicit, natural, and logically earned."
                ),
                depth=job.depth,
            ),
            artifact,
            config,
            class_id,
            part_id,
            following.number,
            following.title,
            int((_section_plan(plan, following.number, following.title) or {}).get("word_budget"))
            if isinstance(
                (_section_plan(plan, following.number, following.title) or {}).get("word_budget"),
                int,
            )
            else per_section,
            owned,
            plan,
        )
        changed = landed or changed

    artifacts.set_artifact_state(
        conn, job.artifact_id, artifacts.GENERATING, "Reading the whole document for continuity"
    )
    body = str(artifacts.get_part(conn, part_id)["content"])
    if not _time_remaining(job):
        return changed
    leaves = [section for section in _leaf_sections(body) if section.level > 0]
    fixes = _evaluate(conn, job, artifact, config, class_id, body, leaves, per_section, plan=plan)[
        : writer_budgets.get_budget(job.depth).max_findings
    ]
    for number, problem in fixes:
        target = sections.extract(str(artifacts.get_part(conn, part_id)["content"]), number)
        if target is None:
            continue
        artifacts.set_artifact_state(
            conn,
            job.artifact_id,
            artifacts.GENERATING,
            f"Applying continuity fix to {target.number}",
        )
        landed, _ = _run_section(
            conn,
            PassJob(job.artifact_id, instruction=problem, depth=job.depth),
            artifact,
            config,
            class_id,
            part_id,
            target.number,
            target.title,
            per_section,
            owned,
            plan,
        )
        changed = landed or changed
    return changed


def _revise_stage(
    conn: sqlite3.Connection,
    job: PassJob,
    artifact: dict[str, object],
    config: TutorConfig,
    class_id: int,
    part_id: int,
    per_section: int | None,
    owned: dict[str, str],
) -> bool:
    """Read the finished draft whole, then rewrite what it got wrong. Returns changed.

    Two sources of work, and they see different things. Code counts words and finds the
    TODO markers the drafter left behind - cheap, certain, and no model needed. One
    evaluation call reads the document as a reader would, for the faults that only show
    up whole: a section that never does what its heading promised, an argument that skips
    a step, a join that does not carry.

    Bounded at two rounds. The endpoint is serial and each round costs a call per flagged
    section, so a loop that ran until it was satisfied would run until the student gave
    up. Best-effort throughout: this stage improves a draft that already landed, so an
    upstream failure here costs the polish, never the draft.
    """
    changed = False
    tolerance = drafting.UpstreamTolerance()
    pass_budget = writer_budgets.get_budget(job.depth)
    rounds = min(pass_budget.max_critique_rounds, pass_budget.evaluation_passes)
    for round_number in range(1, rounds + 1):
        if not _time_remaining(job):
            return changed
        body = str(artifacts.get_part(conn, part_id)["content"])
        flagged = _revision_targets(conn, job, artifact, config, class_id, body, per_section)
        if not flagged:
            return changed
        # Read live: `artifact` is the snapshot taken at pass start, before the section
        # stage published its own count.
        running = artifacts.get_artifact(conn, job.artifact_id)
        artifacts.set_problems_total(
            conn, job.artifact_id, int(running["problems_total"] or 0) + len(flagged)
        )
        for number, problem in flagged:
            section = sections.extract(str(artifacts.get_part(conn, part_id)["content"]), number)
            if section is None:
                continue
            artifacts.set_artifact_state(
                conn,
                job.artifact_id,
                artifacts.GENERATING,
                f"Revising {section.number} {section.title} (round {round_number})".strip(),
            )
            try:
                landed, _ = _run_section(
                    conn,
                    # The deficiency *is* the instruction, so the rewrite is aimed at
                    # what was wrong rather than being a second undirected attempt.
                    PassJob(job.artifact_id, instruction=problem),
                    artifact,
                    config,
                    class_id,
                    part_id,
                    section.number,
                    section.title,
                    per_section,
                    owned,
                )
            except LyraError as exc:
                if tolerance.failed():
                    logger.warning("Revise stage gave up after repeated failures: %s", exc)
                    return changed
                logger.info("Revising %s failed; continuing: %s", section.number, exc)
                continue
            tolerance.succeeded()
            changed = landed or changed
            artifacts.increment_problems_done(conn, job.artifact_id)
    return changed


def _revision_targets(
    conn: sqlite3.Connection,
    job: PassJob,
    artifact: dict[str, object],
    config: TutorConfig,
    class_id: int,
    body: str,
    per_section: int | None,
) -> list[tuple[str, str]]:
    """What this round should rewrite, as (section number, what is wrong with it).

    Deterministic faults first, because they are certain and free; the reader's faults
    second, and only for sections the counting did not already claim.
    """
    leaves = [section for section in _leaf_sections(body) if section.level > 0]
    flagged: dict[str, str] = {}
    for section in leaves:
        if section.is_empty:
            flagged[section.number] = "This section is still empty. Write it."
        elif sections.has_todo(section.text):
            flagged[section.number] = (
                "This section still holds a [TODO:] marker. Write what it asks for from "
                "the course material, or say plainly in the prose what is missing."
            )
        elif (
            per_section and _word_count(sections.prose(section)) < per_section * THIN_SECTION_RATIO
        ):
            flagged[section.number] = (
                f"This section runs {_word_count(sections.prose(section))} words against "
                f"about {per_section}. Develop it to its full length."
            )

    for number, problem in _evaluate(
        conn, job, artifact, config, class_id, body, leaves, per_section
    ):
        flagged.setdefault(number, problem)

    order = [section.number for section in leaves]
    return sorted(
        ((number, problem) for number, problem in flagged.items() if number in order),
        key=lambda pair: order.index(pair[0]),
    )


def _evaluate(
    conn: sqlite3.Connection,
    job: PassJob,
    artifact: dict[str, object],
    config: TutorConfig,
    class_id: int,
    body: str,
    leaves: list[sections.Section],
    per_section: int | None,
    plan: dict[str, object] | None = None,
) -> list[tuple[str, str]]:
    """One model read of the whole document. Never fatal: bad JSON means no findings."""
    if not leaves:
        return []
    counts = "\n".join(
        f"{section.number} {section.title}: {_word_count(sections.prose(section))} words"
        for section in leaves
    )
    reply = _complete(
        config,
        prompts.build_revise_eval_prompt(
            str(artifact["title"]),
            sections.outline(body),
            counts,
            sections.seams(leaves, REVISE_SEAM_TAIL_WORDS, REVISE_SEAM_HEAD_WORDS),
            prompts.format_brief_block(briefs.get_brief(conn, job.artifact_id)),
            prompts.format_length_block(_target_words(conn, job)),
            plan_block=prompts.format_plan_block(plan),
            ledger_block=prompts.format_ledger_block(_ledger_entries(conn, class_id)),
        ),
        schema=prompts.REVISE_SCHEMA,
    )
    payload = replies.loads_object(reply)
    if payload is None:
        logger.warning("Revise evaluation returned a reply that is not a JSON object")
        return []
    listed = payload.get("sections")
    if not isinstance(listed, list):
        return []
    found: list[tuple[str, str]] = []
    for entry in listed[: writer_budgets.get_budget(job.depth).max_findings]:
        if not isinstance(entry, dict):
            continue
        number = str(entry.get("section") or "").strip()
        problem = str(entry.get("problem") or "").strip()
        if number and problem:
            found.append((number, problem))
    return found


def _leaf_sections(body: str) -> list[sections.Section]:
    """Every section drafted in its own right: leaves, preamble excluded."""
    parsed = sections.parse(body)
    leaves = []
    for index, section in enumerate(parsed):
        following = parsed[index + 1] if index + 1 < len(parsed) else None
        if following is None or following.start >= section.end:
            leaves.append(section)
    return leaves


def _settled_detail(changed: bool, cut_off: int, degraded_web_research: int = 0) -> str | None:
    """What a finished pass has left to say, if anything."""
    details: list[str] = []
    if cut_off:
        # Silence here would hand back a document with a section stopping mid-sentence
        # and no account of why. The ceiling is the endpoint's, so the fix is the
        # student's to make in Settings.
        details.append(
            _INCOMPLETE_DETAIL.format(
                count=cut_off,
                plural="" if cut_off == 1 else "s",
                verb="was" if cut_off == 1 else "were",
            )
        )
    if degraded_web_research:
        details.append(
            _WEB_RESEARCH_DEGRADED_DETAIL.format(
                count=degraded_web_research,
                plural="" if degraded_web_research == 1 else "s",
            )
        )
    if not changed:
        details.insert(0, NO_CHANGES_DETAIL)
    return " ".join(details) if details else None


def _run_section(
    conn: sqlite3.Connection,
    job: PassJob,
    artifact: dict[str, object],
    config: TutorConfig,
    class_id: int,
    part_id: int,
    number: str,
    title: str,
    target_words: int | None = None,
    owned: dict[str, str] | None = None,
    plan: dict[str, object] | None = None,
    direct_landings: set[str] | None = None,
    reply_override: str | None = None,
    outline_override: str | None = None,
) -> tuple[bool, bool]:
    """One section's model call and landing. Returns (changed, incomplete).

    The body is re-read and the section re-extracted here, not at pass start: earlier
    landings moved the text, and the student may have moved it too. A section that
    vanished since the pass was planned is skipped - the student deleted it, and the
    pass has no business resurrecting it.
    """
    body = str(artifacts.get_part(conn, part_id)["content"])
    target = sections.extract(body, number) or sections.extract(body, title)
    if target is None:
        logger.info("Section %s %r vanished mid-pass; skipped", number, title)
        return False, False

    # Planned drafts are independent by design and receive their joins in the weave.
    # Keeping the initial calls seam-free makes serial and parallel execution identical.
    tail = None if plan is not None else _previous_tail(body, target)
    following = _next_heading(body, target)
    query = f"{title} {job.instruction or ''}".strip() or str(artifact["title"])
    result = retrieve(conn, class_id, query, SECTION_RETRIEVAL_BUDGET)
    plan_entry = _section_plan(plan, number, title)
    ledger = _ledger_entries(conn, class_id, plan_entry, number)
    messages = prompts.build_section_prompt(
        str(artifact["title"]),
        outline_override or sections.outline(body),
        target.text,
        tail,
        following,
        job.instruction,
        prompts.format_brief_block(briefs.get_brief(conn, job.artifact_id)),
        prompts.format_context_block([vars(chunk) for chunk in result.chunks]),
        prompts.format_facts_block(select_active_facts(conn, class_id)),
        target_words=target_words,
        plan_block=prompts.format_plan_block(plan, number),
        ledger_block=prompts.format_ledger_block(ledger),
    )
    truncated: list[bool] = []
    reply = (
        reply_override
        if reply_override is not None
        else _complete(config, messages, target_words, truncated)
    )
    reply, incomplete = _finish_section_generation(
        conn,
        job,
        config,
        messages,
        reply,
        bool(truncated),
        target_words,
        number,
        title,
    )
    # Normalized where it lands, so the stored body converges on the `$` delimiters the
    # editor, Pandoc, and the chat renderer all read. A model writing `\(x\)` otherwise
    # ships a section that renders as literal backslashes in the editor and in the PDF.
    reply = mathnorm.normalize(reply)
    if not reply or reply == target.text.strip():
        return False, incomplete
    # Sections carry their trailing separation; a stripped model reply must not glue
    # the next heading onto its last paragraph.
    replacement = reply + ("\n" if not reply.endswith("\n") else "")
    if target.end < len(body) and not replacement.endswith("\n\n"):
        replacement += "\n"

    return (
        _land(
            conn,
            job,
            part_id,
            number,
            title,
            replacement,
            owned,
            direct_landings,
        ),
        incomplete,
    )


def _finish_section_generation(
    conn: sqlite3.Connection,
    job: PassJob,
    config: TutorConfig,
    messages: list[dict[str, str]],
    reply: str,
    was_truncated: bool,
    target_words: int | None,
    number: str,
    title: str,
) -> tuple[str, bool]:
    """Finish a section through bounded append-only chunks.

    A small endpoint may only be able to produce a few hundred words per reply. Python
    therefore owns the loop: it counts what arrived, computes the missing amount, sends
    a bounded tail for continuity, and appends new prose. The model only decides what the
    next prose should say.
    """
    combined = reply.strip()
    truncated = was_truncated
    retries = writer_budgets.get_budget(job.depth).section_retries
    capacity = max(
        1,
        int(budget.generation_reserve(config.context_window) / budget.TOKENS_PER_WORD),
    )
    expected_chunks = math.ceil(target_words / capacity) if target_words else 1
    continuation_limit = max(retries, expected_chunks - 1 + retries)

    for attempt in range(continuation_limit):
        words = _word_count(combined)
        short = bool(target_words and words < target_words * SECTION_COMPLETION_RATIO)
        if not truncated and not short:
            return combined, False
        if not _time_remaining(job):
            break
        artifacts.set_artifact_state(
            conn,
            job.artifact_id,
            artifacts.GENERATING,
            f"Continuing {number} {title} (chunk {attempt + 2})".strip(),
        )
        remaining = max(1, target_words - words) if target_words else None
        tail = " ".join(combined.split()[-CONTINUATION_TAIL_WORDS:])
        continuation_truncated: list[bool] = []
        addition = _complete(
            config,
            prompts.build_section_continuation_prompt(
                messages,
                number,
                title,
                tail,
                words,
                remaining,
            ),
            remaining,
            continuation_truncated,
        )
        merged = _append_continuation(combined, addition, title)
        if _word_count(merged) <= words:
            break
        combined = merged
        truncated = bool(continuation_truncated)

    incomplete = truncated or bool(
        target_words and _word_count(combined) < target_words * SECTION_COMPLETION_RATIO
    )
    if incomplete:
        logger.warning(
            "Section %s %r stopped at %s words against target %s after continuations",
            number,
            title,
            _word_count(combined),
            target_words,
        )
    return combined, incomplete


def _append_continuation(existing: str, addition: str, title: str) -> str:
    """Append new prose while removing a repeated heading or overlapping tail."""
    base = existing.rstrip()
    new = addition.strip()
    if not new:
        return base
    lines = new.splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        repeated = lines[0].lstrip("# ").strip().casefold()
        if repeated == title.strip().casefold():
            new = "\n".join(lines[1:]).lstrip()
    if not new:
        return base
    maximum = min(len(base), len(new), 1_000)
    for overlap in range(maximum, 15, -1):
        if base[-overlap:] == new[:overlap]:
            return base + new[overlap:]
    if base.endswith("-"):
        separator = ""
    else:
        separator = "\n\n" if base.endswith((".", "?", "!", ":", ";")) else " "
    return base + separator + new


def _normalize_plan_word_budgets(
    entries: list[dict[str, object]], total_words: int | None
) -> list[dict[str, object]]:
    """Make model-proposed section weights add up to Python's document target."""
    if not total_words or not entries:
        return entries
    weights = [
        int(entry["word_budget"])
        if isinstance(entry.get("word_budget"), int) and int(entry["word_budget"]) > 0
        else 1
        for entry in entries
    ]
    allocations = _allocate_words(total_words, weights)
    return [
        {**entry, "word_budget": words}
        for entry, words in zip(entries, allocations, strict=True)
    ]


def _normalize_runtime_plan_word_budgets(
    plan: dict[str, object],
    targets: list[tuple[str, str]],
    total_words: int,
    *,
    selected_sections_own_total: bool,
) -> None:
    """Normalize a loaded plan without making filtered rewrites paper-sized.

    A full pass allocates the whole target across the sections still in the document.
    A filtered pass normally keeps each selected section's share of the full plan; only
    an explicit length in that filtered instruction makes the selected set own a new
    total of its own.
    """
    raw_entries = plan.get("sections")
    if not isinstance(raw_entries, list):
        return
    chosen = [entry for entry in raw_entries if isinstance(entry, dict)]
    if selected_sections_own_total or not chosen:
        chosen = []
        for number, title in targets:
            entry = _section_plan(plan, number, title)
            if entry is not None and entry not in chosen:
                chosen.append(entry)
    normalized = _normalize_plan_word_budgets(chosen, total_words)
    for entry, replacement in zip(chosen, normalized, strict=True):
        entry["word_budget"] = replacement["word_budget"]


def _allocate_words(total_words: int, weights: list[int]) -> list[int]:
    """Allocate an exact total deterministically, with useful minimum section sizes."""
    count = len(weights)
    if count == 0:
        return []
    floor = min(MINIMUM_SECTION_WORDS, total_words // count)
    remaining = max(0, total_words - floor * count)
    weight_total = sum(weights) or count
    shares = [remaining * weight / weight_total for weight in weights]
    allocations = [floor + int(share) for share in shares]
    leftover = total_words - sum(allocations)
    order = sorted(range(count), key=lambda index: (-(shares[index] % 1), index))
    for index in order[:leftover]:
        allocations[index] += 1
    return allocations


def _word_count(text: str) -> int:
    """Words of prose, heading and TODO markers included. Close enough to steer by."""
    return len(text.split())


def _land(
    conn: sqlite3.Connection,
    job: PassJob,
    part_id: int,
    number: str,
    title: str,
    replacement: str,
    owned: dict[str, str] | None = None,
    direct_landings: set[str] | None = None,
) -> bool:
    """Land one section's text under the direct-on-empty rule, re-checked now.

    A section this pass wrote itself, and that still holds exactly what the pass wrote,
    counts as empty for this purpose: the revise stage improving the pass's own first
    attempt is one run finishing its work, not an assistant editing a student. The text
    is compared rather than trusted, so a section the student typed into between the two
    stages goes back to being a proposal - which is the whole point of the rule.
    """
    body = str(artifacts.get_part(conn, part_id)["content"])
    fresh = sections.extract(body, number) or sections.extract(body, title)
    if fresh is None:
        logger.info("Section %s %r vanished before landing; skipped", number, title)
        return False

    untouched = owned is not None and fresh.text.strip() == (owned.get(number) or "").strip()
    if fresh.is_empty or untouched:
        artifacts.set_part_content(
            conn,
            part_id,
            sections.splice(body, fresh, replacement),
            origin=artifacts.GENERATED,
            note=f"{'revised' if untouched else 'drafted'} {number} {title}".strip(),
            record_revision=True,
        )
        if owned is not None:
            owned[number] = replacement
        if direct_landings is not None:
            direct_landings.add(number)
        return True

    # Occupied: into the one coalesced proposal, spliced against what is already
    # proposed so every section of the pass arrives as a single reviewable diff.
    pending = suggestions.pending_for_part(conn, part_id)
    base = str(pending["proposed_content"]) if pending else body
    in_base = sections.extract(base, number) or sections.extract(base, title)
    if in_base is None:
        logger.info("Section %s %r is not in the proposal base; skipped", number, title)
        return False
    proposed = sections.splice(base, in_base, replacement)
    note = job.instruction or "draft pass"
    return suggestions.propose(conn, part_id, proposed, note) is not None


def _body_part(conn: sqlite3.Connection, artifact_id: int) -> dict[str, object]:
    """The draft's one body part."""
    for part in artifacts.list_parts(conn, artifact_id):
        if part["kind"] == artifacts.DRAFT_BODY:
            return part
    raise NotFoundError("That draft has no body.")


def _target_words(conn: sqlite3.Connection, job: PassJob) -> int | None:
    """How long the finished document should be, in words, or None when nobody said.

    The pass instruction wins over the brief: "write me five pages on X" typed into the
    dialog is the more recent and more specific statement of what the student wants, and
    a brief written weeks ago should not quietly override it.
    """
    from_instruction = briefs.length_target_words(job.instruction, require_unit=True)
    if from_instruction:
        return from_instruction
    brief = briefs.get_brief(conn, job.artifact_id)
    return briefs.length_target_words(str(brief["length_target"])) if brief else None


def _targets(body: str, refs: tuple[str, ...]) -> list[tuple[str, str]]:
    """The sections a pass will run over, as (number, title), in document order.

    Unfiltered, that is every leaf section - parents are drafted through their
    children - skipping the preamble, which is not a section anyone assigned. A filter
    resolves each ref against the document and refuses the ones that miss, because a
    pass over sections that do not exist is a misunderstanding to surface, not to
    half-run.

    Raises:
        LyraError: when a requested ref matches nothing.
    """
    parsed = sections.parse(body)
    if refs:
        chosen: dict[str, tuple[str, str]] = {}
        for ref in refs:
            section = sections.extract(body, ref)
            if section is None:
                raise LyraError(_NO_SECTION_MESSAGE.format(ref=ref))
            chosen[section.number] = (section.number, section.title)
        ordered = [s.number for s in parsed]
        return sorted(chosen.values(), key=lambda pair: ordered.index(pair[0]))

    leaves = []
    for index, section in enumerate(parsed):
        if section.level == 0:
            continue
        following = parsed[index + 1] if index + 1 < len(parsed) else None
        is_parent = following is not None and following.start < section.end
        if not is_parent:
            leaves.append((section.number, section.title))
    return leaves


def _previous_tail(body: str, target: sections.Section) -> str | None:
    """The last words before the section, for the transition in."""
    before = body[: target.start].strip()
    if not before:
        return None
    words = before.split()
    return " ".join(words[-PREVIOUS_TAIL_WORDS:])


def _next_heading(body: str, target: sections.Section) -> str | None:
    """The heading and intent line that follow the section, for the transition out."""
    after = body[target.end :]
    lines = [line.strip() for line in after.splitlines() if line.strip()]
    if not lines:
        return None
    return " - ".join(lines[:2]) if len(lines) > 1 else lines[0]


def _as_plan(value: object) -> dict[str, object] | None:
    """Normalize storage rows/dataclasses/mappings into the prompt-facing plan."""
    if value is None:
        return None
    if isinstance(value, (dict, sqlite3.Row)):
        payload = dict(value)
    elif hasattr(value, "__dict__"):
        payload = dict(vars(value))
    else:
        return None
    for key in (
        "argument_map",
        "sections",
        "research_notes",
        "brief_analysis",
        "thesis_candidates",
    ):
        raw = payload.get(key)
        if isinstance(raw, str):
            with contextlib.suppress(json.JSONDecodeError):
                payload[key] = json.loads(raw)
    nested = payload.get("plan") or payload.get("content")
    if isinstance(nested, str):
        try:
            nested = json.loads(nested)
        except json.JSONDecodeError:
            nested = None
    if isinstance(nested, dict):
        payload = {**payload, **nested}
    return payload


def _active_plan(conn: sqlite3.Connection, artifact_id: int) -> dict[str, object] | None:
    return _as_plan(writer_plans.get_active_plan(conn, artifact_id))


def _save_plan(
    conn: sqlite3.Connection, artifact_id: int, plan: dict[str, object]
) -> dict[str, object] | None:
    stored = writer_plans.create_plan(
        conn,
        artifact_id,
        brief_analysis=str(plan.get("brief_analysis") or ""),
        thesis=str(plan.get("thesis") or ""),
        argument_map=plan.get("argument_map")
        if isinstance(plan.get("argument_map"), (dict, list))
        else {},
        sections=plan.get("sections") if isinstance(plan.get("sections"), list) else (),
    )
    return _as_plan(stored)


def _save_research_notes(
    conn: sqlite3.Connection,
    artifact_id: int,
    section_ref: str,
    notes: dict[str, object],
    source_ids: list[object],
) -> None:
    plan = writer_plans.get_active_plan(conn, artifact_id)
    if plan is not None:
        writer_plans.update_plan_section(
            conn,
            int(plan["id"]),
            section_ref,
            research_notes=json.dumps(notes, ensure_ascii=False),
            source_ids=[value for value in source_ids if isinstance(value, int)],
        )


def _section_plan(
    plan: dict[str, object] | None, number: str, title: str
) -> dict[str, object] | None:
    if not plan:
        return None
    entries = plan.get("sections")
    if not isinstance(entries, list):
        return None
    normalized_title = title.casefold().strip()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ref = str(entry.get("section_ref") or entry.get("ref") or "").strip()
        entry_title = str(entry.get("title") or "").casefold().strip()
        if ref == number or (entry_title and entry_title == normalized_title):
            return entry
    return None


def _ledger_entries(
    conn: sqlite3.Connection,
    class_id: int,
    plan_entry: dict[str, object] | None = None,
    section_ref: str | None = None,
) -> list[dict[str, object]]:
    """Read ledger rows, narrowing relied-on passages to the current section."""
    rows = source_ledger.list_sources(conn, class_id)
    source_ids = (plan_entry or {}).get("source_ids")
    if isinstance(source_ids, list):
        selected = {int(value) for value in source_ids if isinstance(value, int)}
        rows = [row for row in rows if int(row["id"]) in selected]
    if section_ref is not None:
        scoped: list[dict[str, object]] = []
        for row in rows:
            entry = dict(row)
            excerpts = entry.get("excerpts")
            if isinstance(excerpts, list):
                entry["excerpts"] = [
                    {
                        "section_ref": excerpt.get("section_ref"),
                        "excerpt": excerpt.get("excerpt"),
                    }
                    for excerpt in excerpts
                    if isinstance(excerpt, dict)
                    and excerpt.get("section_ref") in (None, "", section_ref)
                ]
            scoped.append(entry)
        rows = scoped
    return rows
