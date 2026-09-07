"""Contract tests for the review pass: four lenses, comments filed, document untouched.

The model is never called: `_review_run` is stubbed with scripted steps that may drive
the real reviewer registry (so filing goes through the real `add_comment` tool), and
the locality gate is stubbed open. Under test is the machinery: which lenses run over
what, how progress counts, what an interruption costs (never a filed finding), and how
the review closes into the writer conversation.
"""

import sqlite3
import threading
from collections.abc import Callable

import pytest

from backend.core import (
    artifacts,
    comments,
    drafting,
    review_pipeline,
    sessions,
    source_ledger,
    writer_plans,
    writer_runs,
)
from backend.core.app_settings import TutorAccess, TutorConfig
from backend.core.errors import LyraError
from backend.llm.tools import COMPLETED, DEPTH, UPSTREAM_FAILED, ToolDefinition, ToolLoopResult
from backend.rag.retrieve import RetrievalResult, RetrievedChunk

# Two occupied sections, one empty: the empty one must never get a lens of its own.
BODY = (
    "# Pendulum Lab\n"
    "\n"
    "## Introduction\n"
    "\n"
    "We measured the pendulum period, and it was clearly very interesting.\n"
    "\n"
    "## Methods\n"
    "\n"
    "[TODO: describe the rig and the procedure]\n"
    "\n"
    "## Results\n"
    "\n"
    "The period grew with length.\n"
)

_DONE = ToolLoopResult(content="Filed.", stopped=COMPLETED)

Step = Callable[[dict[str, ToolDefinition]], ToolLoopResult | None]


class _StubReviewer:
    """Scripted `_review_run`: each call pops the next step.

    A step may be a `ToolLoopResult`, an exception to raise, or a callable handed the
    real registry - which is how a test files comments through the real tool.
    """

    def __init__(self) -> None:
        self.script: list[object] = []
        self.runs: list[tuple[list[dict[str, str]], int]] = []

    def __call__(
        self,
        config: TutorConfig,
        messages: list[dict[str, str]],
        registry: dict[str, ToolDefinition],
        max_depth: int,
    ) -> ToolLoopResult:
        self.runs.append((messages, max_depth))
        step = self.script.pop(0) if self.script else _DONE
        if isinstance(step, Exception):
            raise step
        if isinstance(step, ToolLoopResult):
            return step
        assert callable(step)
        return step(registry) or _DONE


@pytest.fixture
def reviewer(monkeypatch: pytest.MonkeyPatch) -> _StubReviewer:
    stub = _StubReviewer()
    monkeypatch.setattr(review_pipeline, "_review_run", stub)
    return stub


@pytest.fixture(autouse=True)
def _open_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        review_pipeline,
        "resolve_tutor_access",
        lambda conn, **_kwargs: TutorAccess(
            config=TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192),
            document_block=None,
            remote_ack=True,
        ),
    )


def _draft(db: sqlite3.Connection, class_id: int, content: str = BODY) -> tuple[int, int]:
    created = artifacts.create_artifact(db, class_id, "Pendulum Lab", [], kind=artifacts.KIND_DRAFT)
    part_id = artifacts.create_part(
        db,
        int(created["id"]),
        artifacts.DRAFT_BODY,
        1,
        content=content,
        status=artifacts.PART_COMPLETE,
    )
    artifacts.set_artifact_state(db, int(created["id"]), artifacts.READY)
    return int(created["id"]), part_id


def _files(body: str, severity: str, quote: str | None = None) -> Step:
    """A step that files one comment through the registry's real tool."""

    def step(registry: dict[str, ToolDefinition]) -> None:
        arguments: dict[str, object] = {"body": body, "severity": severity}
        if quote is not None:
            arguments["quote"] = quote
        result = registry["add_comment"].handler(**arguments)
        assert result.ok, result.error

    return step


def test_a_review_runs_every_lens_over_the_prose_and_files_as_it_goes(
    db: sqlite3.Connection, class_id: int, reviewer: _StubReviewer
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    reviewer.script = [
        _files("The intro does not state the assignment's question.", "major", "## Introduction"),
        _DONE,  # argument: nothing at the seams
        _files('"clearly very interesting" does the reader\'s judging.', "minor", "clearly very"),
        _DONE,  # prose: Results holds up
        _DONE,  # claims: Introduction
        _files("No source gives the period-length relation.", "critical", "grew with length"),
    ]

    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))

    # Structure, argument, then prose and claims over the two occupied sections only.
    assert len(reviewer.runs) == 6
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert artifact["problems_total"] == review_pipeline.LENS_COUNT
    assert artifact["problems_done"] == review_pipeline.LENS_COUNT
    assert artifact["stage_detail"] == "Review complete: 1 critical, 1 major, 1 minor."
    # The document was never touched; the findings are rows beside it.
    assert str(artifacts.get_part(db, part_id)["content"]) == BODY
    threads = comments.list_threads(db, part_id, BODY)
    assert [thread["severity"] for thread in threads] == ["major", "minor", "critical"]
    assert all(thread["author"] == comments.REVIEWER for thread in threads)


def test_the_close_lands_in_the_writer_conversation_with_the_worst_first(
    db: sqlite3.Connection, class_id: int, reviewer: _StubReviewer
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    existing = sessions.create_session(db, class_id, artifact_part_id=part_id, mode=sessions.WRITER)
    reviewer.script = [
        _files("A note-level thought.", "note"),
        _files("The critical one.", "critical", "grew with length"),
    ]

    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))

    # No new session: the close joins the conversation the student already has.
    listed = sessions.writer_sessions_for_part(db, part_id)
    assert [int(row["id"]) for row in listed] == [int(existing["id"])]
    messages = sessions.list_messages(db, int(existing["id"]))
    assert len(messages) == 1
    text = str(messages[0]["content"])
    assert "1 critical, 1 note" in text
    # Severity order, not filing order: the critical finding leads.
    assert text.index("The critical one.") < text.index("A note-level thought.")


def test_a_findingless_review_says_so_without_inventing_one(
    db: sqlite3.Connection, class_id: int, reviewer: _StubReviewer
) -> None:
    artifact_id, part_id = _draft(db, class_id)

    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["stage_detail"] == "Review complete: no findings."
    [session] = sessions.writer_sessions_for_part(db, part_id)
    [message] = sessions.list_messages(db, int(session["id"]))
    assert "filed no comments" in str(message["content"])


def test_a_draft_with_no_prose_has_nothing_to_review(
    db: sqlite3.Connection, class_id: int, reviewer: _StubReviewer
) -> None:
    skeleton = "# Lab\n\n## Introduction\n\n[TODO: say what this measures]\n"
    artifact_id, _ = _draft(db, class_id, content=skeleton)

    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))

    assert reviewer.runs == []
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert artifact["stage_detail"] == review_pipeline.NOTHING_TO_REVIEW_DETAIL


def test_a_single_section_skips_the_argument_lens_but_counts_all_four(
    db: sqlite3.Connection, class_id: int, reviewer: _StubReviewer
) -> None:
    one = "# Essay\n\nJust one run of prose, no headings.\n"
    artifact_id, _ = _draft(db, class_id, content=one)

    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))

    # Structure, then prose and claims on the one section. No seams to judge.
    assert len(reviewer.runs) == 3
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["problems_done"] == review_pipeline.LENS_COUNT


def test_one_upstream_failure_stops_review_without_certifying_skipped_lenses(
    db: sqlite3.Connection, class_id: int, reviewer: _StubReviewer
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    reviewer.script = [
        ToolLoopResult(content="", stopped=UPSTREAM_FAILED, detail="A one-off 500."),
        _files("Must not run after the incomplete lens.", "major", "## Introduction"),
    ]
    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))
    assert len(reviewer.runs) == 1
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert artifact["error_message"] == "A one-off 500."
    assert comments.list_threads(db, part_id, BODY) == []


def test_upstream_failure_stops_the_review_but_keeps_what_it_filed(
    db: sqlite3.Connection, class_id: int, reviewer: _StubReviewer
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    reviewer.script = [
        _files("Filed before the endpoint died.", "major", "## Introduction"),
        ToolLoopResult(content="", stopped=UPSTREAM_FAILED, detail="The endpoint fell over."),
        ToolLoopResult(content="", stopped=UPSTREAM_FAILED, detail="The endpoint fell over."),
    ]

    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))

    assert len(reviewer.runs) == 2
    artifact = artifacts.get_artifact(db, artifact_id)
    # `failed`, not `ready`: the alert that carries `error_message` renders on that state
    # alone, so an aborted review used to settle looking exactly like a finished one.
    assert artifact["state"] == artifacts.FAILED
    assert "fell over" in str(artifact["error_message"])
    [thread] = comments.list_threads(db, part_id, BODY)
    assert thread["body"] == "Filed before the endpoint died."


def test_a_lens_hitting_its_ceiling_cannot_certify_the_review(
    db: sqlite3.Connection, class_id: int, reviewer: _StubReviewer
) -> None:
    artifact_id, _ = _draft(db, class_id)
    reviewer.script = [
        ToolLoopResult(content="", stopped=DEPTH, detail="Stopped after 12 rounds."),
    ]

    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))

    assert len(reviewer.runs) == 1
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert artifact["error_message"] == "Stopped after 12 rounds."


def test_a_deep_review_adds_full_skeptic_runs_and_carries_plan_jobs(
    db: sqlite3.Connection, class_id: int, reviewer: _StubReviewer
) -> None:
    artifact_id, _ = _draft(db, class_id)
    writer_plans.create_plan(
        db,
        artifact_id,
        thesis="Period depends on length.",
        argument_map={"claims": []},
        sections=[
            {
                "section_ref": "1.1",
                "ordinal": 0,
                "title": "Introduction",
                "job": "Frame the causal question",
                "claim": "Length matters",
                "evidence": [],
                "source_ids": [],
                "word_budget": 250,
            },
            {
                "section_ref": "1.3",
                "ordinal": 1,
                "title": "Results",
                "job": "Establish the empirical relationship",
                "claim": "Period rose with length",
                "evidence": [],
                "source_ids": [],
                "word_budget": 250,
            },
        ],
    )

    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id, depth="deep"))

    # Six ordinary runs plus one full skeptic run for each occupied leaf section.
    assert len(reviewer.runs) == 8
    skeptic_turns = [run[0][1]["content"] for run in reviewer.runs[-2:]]
    assert "Frame the causal question" in skeptic_turns[0]
    assert "Establish the empirical relationship" in skeptic_turns[1]
    assert all(depth == 24 for _, depth in reviewer.runs)


def test_parallel_section_reviews_are_bounded_and_replayed_in_lens_section_order(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    db.execute("update settings set parallel_requests = 1, parallel_concurrency = 2 where id = 1")
    db.commit()
    lock = threading.Lock()
    barriers = {"prose": threading.Barrier(2), "claims": threading.Barrier(2)}
    active = 0
    peak = 0

    def run(
        config: TutorConfig,
        messages: list[dict[str, str]],
        registry: dict[str, ToolDefinition],
        max_depth: int,
    ) -> ToolLoopResult:
        nonlocal active, peak
        user = messages[-1]["content"]
        if "The section under review" not in user:
            return _DONE
        system = messages[0]["content"]
        lens_name = "prose" if "calibrates one section" in system else "claims"
        section_name = "intro" if "## Introduction" in user else "results"
        with lock:
            active += 1
            peak = max(peak, active)
        barriers[lens_name].wait(timeout=2)
        result = registry["add_comment"].handler(
            body=f"{lens_name}-{section_name}", severity="minor"
        )
        assert result.ok
        with lock:
            active -= 1
        return _DONE

    replay_threads: list[int] = []
    real_add = comments.add_comment

    def record_replay(*args: object, **kwargs: object) -> dict[str, object]:
        replay_threads.append(threading.get_ident())
        return real_add(*args, **kwargs)

    monkeypatch.setattr(review_pipeline, "_review_run", run)
    monkeypatch.setattr(comments, "add_comment", record_replay)
    owner_thread = threading.get_ident()

    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))

    assert peak == 2
    assert replay_threads == [owner_thread] * 4
    threads = comments.list_threads(db, part_id, BODY)
    assert [thread["body"] for thread in threads] == [
        "prose-intro",
        "prose-results",
        "claims-intro",
        "claims-results",
    ]


def test_parallel_capture_canonicalizes_and_confirms_an_existing_finding(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    exact = "We measured the pendulum period, and it was clearly very interesting."
    existing = comments.add_comment(
        db,
        part_id,
        comments.REVIEWER,
        "Already filed.",
        severity="major",
        quote=exact,
        hint=BODY.index(exact),
        section_ref="1.1",
    )
    db.execute("update settings set parallel_requests = 1, parallel_concurrency = 2 where id = 1")
    db.commit()
    attempts = []

    def run(
        config: TutorConfig,
        messages: list[dict[str, str]],
        registry: dict[str, ToolDefinition],
        max_depth: int,
    ) -> ToolLoopResult:
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "calibrates one section" in system and "## Introduction" in user:
            attempts.append(
                registry["add_comment"].handler(
                    body="A repeated finding.",
                    severity="major",
                    quote=(
                        "We   measured the pendulum period, and it was clearly very interesting."
                    ),
                    section_ref="1.1",
                )
            )
        return _DONE

    monkeypatch.setattr(review_pipeline, "_review_run", run)
    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))

    assert len(attempts) == 1
    assert not attempts[0].ok
    assert [thread["id"] for thread in comments.list_threads(db, part_id, BODY)] == [existing["id"]]
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["stage_detail"] == "Review complete: 1 major (1 already filed)."


def test_parallel_course_search_uses_owner_registered_source_ids(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, 'reading.pdf', '/tmp/reading.pdf', 'application/pdf', 1, 'ready')",
        (class_id,),
    )
    document_id = int(cursor.lastrowid or 0)
    db.commit()
    artifact_id, _ = _draft(db, class_id)
    db.execute("update settings set parallel_requests = 1, parallel_concurrency = 2 where id = 1")
    db.commit()
    chunk = RetrievedChunk(
        chunk_id=1,
        document_id=document_id,
        content="The period rises with pendulum length.",
        token_count=7,
        page_number=3,
        section_title="Pendulums",
        section_path=None,
        section_number=None,
        problem_number=None,
        part_index=0,
        filename="reading.pdf",
        similarity=0.9,
        score=0.9,
    )
    retrieved = lambda *args: RetrievalResult(  # noqa: E731 - shared test double.
        [chunk], trimmed=False, omitted_document_count=0
    )
    monkeypatch.setattr(review_pipeline, "retrieve", retrieved)
    monkeypatch.setattr(review_pipeline.writer_tools, "retrieve", retrieved)
    seen_ids: list[int] = []

    def run(
        config: TutorConfig,
        messages: list[dict[str, str]],
        registry: dict[str, ToolDefinition],
        max_depth: int,
    ) -> ToolLoopResult:
        if "The section under review" in messages[-1]["content"]:
            result = registry["search_course_material"].handler(query="period length")
            assert result.ok
            [item] = result.value["results"]
            seen_ids.append(int(item["source_id"]))
        return _DONE

    monkeypatch.setattr(review_pipeline, "_review_run", run)
    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))

    [registered] = source_ledger.list_sources(db, class_id)
    assert registered["document_id"] == document_id
    assert seen_ids == [registered["id"]] * 4


def test_parallel_review_matches_serial_prompts_search_and_comment_landing(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, 'lab.pdf', '/tmp/lab.pdf', 'application/pdf', 1, 'ready')",
        (class_id,),
    )
    document_id = int(cursor.lastrowid or 0)
    db.commit()
    chunk = RetrievedChunk(
        chunk_id=2,
        document_id=document_id,
        content="Longer pendulums have longer periods.",
        token_count=6,
        page_number=4,
        section_title="Period",
        section_path=None,
        section_number=None,
        problem_number=None,
        part_index=0,
        filename="lab.pdf",
        similarity=0.95,
        score=0.95,
    )

    def retrieved(*args: object) -> RetrievalResult:
        return RetrievalResult([chunk], trimmed=False, omitted_document_count=0)

    monkeypatch.setattr(review_pipeline, "retrieve", retrieved)
    monkeypatch.setattr(review_pipeline.writer_tools, "retrieve", retrieved)
    captured: dict[str, dict[tuple[str, str], object]] = {
        "serial": {},
        "parallel": {},
    }
    mode = "serial"

    def run(
        config: TutorConfig,
        messages: list[dict[str, str]],
        registry: dict[str, ToolDefinition],
        max_depth: int,
    ) -> ToolLoopResult:
        user = messages[-1]["content"]
        if "The section under review" not in user:
            return _DONE
        system = messages[0]["content"]
        lens_name = "prose" if "calibrates one section" in system else "claims"
        section_name = "intro" if "## Introduction" in user else "results"
        key = (lens_name, section_name)
        captured[mode][key] = user
        if lens_name == "claims":
            searched = registry["search_course_material"].handler(query="pendulum period")
            assert searched.ok
            captured[mode][("search", section_name)] = searched.as_payload()
        quotes = {
            ("prose", "intro"): "clearly   very interesting",
            ("prose", "results"): "The period grew with length.",
            ("claims", "intro"): "pendulum period",
            ("claims", "results"): "grew with length",
        }
        result = registry["add_comment"].handler(
            body=f"{lens_name}-{section_name}",
            severity="major" if lens_name == "claims" else "minor",
            quote=quotes[key],
            section_ref="1.1" if section_name == "intro" else "1.3",
        )
        assert result.ok
        return _DONE

    monkeypatch.setattr(review_pipeline, "_review_run", run)
    serial_id, serial_part = _draft(db, class_id)
    review_pipeline.run_review(review_pipeline.ReviewJob(serial_id))
    serial_artifact = artifacts.get_artifact(db, serial_id)
    assert serial_artifact["state"] == artifacts.READY, serial_artifact["error_message"]

    mode = "parallel"
    db.execute("update settings set parallel_requests = 1, parallel_concurrency = 2 where id = 1")
    db.commit()
    parallel_id, parallel_part = _draft(db, class_id)
    review_pipeline.run_review(review_pipeline.ReviewJob(parallel_id))

    assert captured["parallel"] == captured["serial"]

    def landed(part_id: int) -> list[tuple[object, ...]]:
        return [
            (
                thread["body"],
                thread["severity"],
                thread["quote"],
                thread["hint"],
                thread["section_ref"],
                thread["orphaned"],
            )
            for thread in comments.list_threads(db, part_id, BODY)
        ]

    assert landed(parallel_part) == landed(serial_part)


def test_parallel_peer_duplicates_see_the_exact_serial_tool_results(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visible: dict[str, dict[str, dict[str, object]]] = {"serial": {}, "parallel": {}}
    mode = "serial"

    def run(
        config: TutorConfig,
        messages: list[dict[str, str]],
        registry: dict[str, ToolDefinition],
        max_depth: int,
    ) -> ToolLoopResult:
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "calibrates one section" not in system:
            return _DONE
        section_name = "intro" if "## Introduction" in user else "results"
        result = registry["add_comment"].handler(
            body=f"duplicate from {section_name}",
            severity="major",
            quote="pendulum period",
        )
        visible[mode][section_name] = result.as_payload()
        return _DONE

    monkeypatch.setattr(review_pipeline, "_review_run", run)
    serial_id, serial_part = _draft(db, class_id)
    review_pipeline.run_review(review_pipeline.ReviewJob(serial_id))

    mode = "parallel"
    db.execute("update settings set parallel_requests = 1, parallel_concurrency = 2 where id = 1")
    db.commit()
    parallel_id, parallel_part = _draft(db, class_id)
    review_pipeline.run_review(review_pipeline.ReviewJob(parallel_id))

    assert (
        visible["parallel"]
        == visible["serial"]
        == {
            "intro": {"ok": True, "filed": True, "anchored": True},
            "results": {
                "ok": False,
                "error": (
                    "A comment at this severity is already open on that exact passage. "
                    "Do not file the same finding twice; move on to the next one."
                ),
            },
        }
    )
    assert len(comments.list_threads(db, serial_part, BODY)) == 1
    assert len(comments.list_threads(db, parallel_part, BODY)) == 1


def test_serial_default_never_enters_the_parallel_worker_path(
    db: sqlite3.Connection,
    class_id: int,
    reviewer: _StubReviewer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, _ = _draft(db, class_id)

    def forbidden(*args: object, **kwargs: object) -> review_pipeline._CapturedLens:
        raise AssertionError("parallel review worker used while capability was disabled")

    monkeypatch.setattr(review_pipeline, "_parallel_review_run", forbidden)
    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))

    assert len(reviewer.runs) == 6


def test_parallel_review_replays_successful_findings_when_a_peer_fails(
    db: sqlite3.Connection,
    class_id: int,
    reviewer: _StubReviewer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    db.execute("update settings set parallel_requests = 1, parallel_concurrency = 2 where id = 1")
    db.commit()

    def captured(
        config: TutorConfig,
        target_artifact_id: int,
        target_class_id: int,
        stage: str,
        messages: list[dict[str, str]],
        max_depth: int,
        deadline: float | None = None,
        coordinator: review_pipeline._CaptureCoordinator | None = None,
        worker_index: int = 0,
        run_id: int | None = None,
    ) -> review_pipeline._CapturedLens:
        if "Introduction" in stage:
            return review_pipeline._CapturedLens(
                stage,
                ToolLoopResult(content="", stopped=UPSTREAM_FAILED, detail="one worker failed"),
                (),
            )
        return review_pipeline._CapturedLens(
            stage,
            _DONE,
            ({"body": f"survived: {stage}", "severity": "major"},),
        )

    monkeypatch.setattr(review_pipeline, "_parallel_review_run", captured)
    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    threads = comments.list_threads(db, part_id, BODY)
    assert [thread["body"] for thread in threads] == [
        "survived: Reviewing prose: 1.3 Results",
    ]


def test_parallel_review_checkpoints_only_validated_replayed_outcomes(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "# Pendulum Lab\n\n## Introduction\n\nFirst claim.\n\n## Results\n\nSecond claim.\n"
    artifact_id, part_id = _draft(db, class_id, content=body)
    db.execute("update settings set parallel_requests = 1, parallel_concurrency = 2 where id = 1")
    run = writer_runs.create_run(
        db,
        artifact_id,
        writer_runs.REVIEW,
        "quick",
        request={},
        started_at="2026-08-07T12:00:00+00:00",
    )
    writer_runs.mark_running(db, int(run["id"]))
    writer_runs.checkpoint(
        db,
        int(run["id"]),
        stage="prose",
        index=0,
        data={
            "filed_comment_ids": [],
            "confirmed_comment_ids": [],
            "completed_lenses": 2,
        },
    )

    def captured(
        config: TutorConfig,
        target_artifact_id: int,
        target_class_id: int,
        stage: str,
        messages: list[dict[str, str]],
        max_depth: int,
        deadline: float | None = None,
        coordinator: review_pipeline._CaptureCoordinator | None = None,
        worker_index: int = 0,
        run_id: int | None = None,
    ) -> review_pipeline._CapturedLens:
        if "Introduction" in stage:
            return review_pipeline._CapturedLens(
                stage,
                _DONE,
                ({"body": "survived intro", "severity": "major"},),
            )
        return review_pipeline._CapturedLens(
            stage,
            ToolLoopResult(content="", stopped=review_pipeline.NO_TOOL_SUPPORT, detail="no tools"),
            (),
        )

    monkeypatch.setattr(review_pipeline, "_parallel_review_run", captured)

    with pytest.raises(LyraError, match="no tools"):
        review_pipeline._run(db, review_pipeline.ReviewJob(artifact_id, run_id=int(run["id"])))

    stored = writer_runs.get_run(db, int(run["id"]))
    checkpoint = stored["checkpoint"]
    assert checkpoint["stage"] == "prose"
    assert checkpoint["index"] == 1
    assert checkpoint["data"]["filed_comment_ids"]
    assert [thread["body"] for thread in comments.list_threads(db, part_id, body)] == [
        "survived intro"
    ]


def test_deep_review_progress_includes_the_full_skeptic_stage(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    observed: list[tuple[int, int]] = []

    def run(
        config: TutorConfig,
        messages: list[dict[str, str]],
        registry: dict[str, ToolDefinition],
        max_depth: int,
    ) -> ToolLoopResult:
        if "full skeptical read" in messages[0]["content"]:
            row = db.execute(
                "select problems_total, problems_done from artifacts where id = ?",
                (artifact_id,),
            ).fetchone()
            observed.append((int(row["problems_total"]), int(row["problems_done"])))
        return _DONE

    monkeypatch.setattr(review_pipeline, "_review_run", run)
    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id, depth="deep"))

    assert observed == [(5, 4), (5, 4)]
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["problems_total"] == 5
    assert artifact["problems_done"] == 5


def test_review_wall_clock_stops_without_reporting_unrun_lenses_complete(
    db: sqlite3.Connection,
    class_id: int,
    reviewer: _StubReviewer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    ticks = iter((0.0, 601.0))
    monkeypatch.setattr(review_pipeline.time, "monotonic", lambda: next(ticks, 601.0))

    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id, depth="quick"))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert artifact["problems_total"] == 4
    assert artifact["problems_done"] == 0
    assert artifact["stage_detail"] == "The review did not finish."
    assert "time budget was exhausted" in artifact["error_message"]
    assert reviewer.runs == []


def test_the_argument_lens_sees_the_seams_and_the_section_lenses_see_their_text(
    db: sqlite3.Connection, class_id: int, reviewer: _StubReviewer
) -> None:
    artifact_id, _ = _draft(db, class_id)

    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))

    argument_turn = reviewer.runs[1][0][1]["content"]
    assert "1.1 Introduction ends:" in argument_turn
    assert "1.3 Results begins:" in argument_turn
    # The empty Methods section is not a seam party: 1.1 hands off to 1.3.
    assert "1.2 Methods ends:" not in argument_turn
    assert "1.2 Methods begins:" not in argument_turn
    prose_turn = reviewer.runs[2][0][1]["content"]
    assert "We measured the pendulum period" in prose_turn
    # Every lens follows the one shared depth budget; no legacy fixed ceiling remains.
    quick_depth = review_pipeline.writer_budgets.BUDGETS["quick"].tool_loop_depth
    assert reviewer.runs[0][1] == quick_depth
    assert reviewer.runs[2][1] == quick_depth


def test_a_deleted_draft_is_the_cancel_and_a_blocked_gate_reports(
    db: sqlite3.Connection, class_id: int, reviewer: _StubReviewer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.core.app_settings import NO_ENDPOINT

    gone, _ = _draft(db, class_id)
    artifacts.delete_artifact(db, gone)
    review_pipeline.run_review(review_pipeline.ReviewJob(gone))  # Must not raise.
    assert reviewer.runs == []

    artifact_id, _ = _draft(db, class_id)
    monkeypatch.setattr(
        review_pipeline,
        "resolve_tutor_access",
        lambda conn, **_kwargs: TutorAccess(
            config=None, document_block=NO_ENDPOINT, remote_ack=False
        ),
    )
    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert "No tutor endpoint" in str(artifact["error_message"])


def test_a_section_emptied_mid_review_loses_its_turn_not_the_review(
    db: sqlite3.Connection, class_id: int, reviewer: _StubReviewer
) -> None:
    artifact_id, part_id = _draft(db, class_id)

    def empties_results(registry: dict[str, ToolDefinition]) -> None:
        # The student deletes the Results prose while the structure lens runs. The
        # per-section lenses re-read, so Results must not get prose or claims runs.
        artifacts.set_part_content(
            db,
            part_id,
            BODY.replace("The period grew with length.\n", ""),
            origin=artifacts.USER_CORRECTED,
            record_revision=False,
        )

    reviewer.script = [empties_results]

    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))

    # Structure ran on the old shape; after the edit only Introduction has prose, and
    # with one section left the argument lens has no seams: 1 + 1 + 1 runs.
    assert len(reviewer.runs) == 3
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert artifact["problems_done"] == review_pipeline.LENS_COUNT


def test_every_review_stage_detail_keeps_the_reviewing_prefix(
    db: sqlite3.Connection, class_id: int, reviewer: _StubReviewer
) -> None:
    """The contract the workspace reads to know a review will not write the document.

    The editor stays live under a review and goes inert under a draft pass, and the only
    thing telling them apart is this prefix. It is asserted here rather than left to
    convention because breaking it silently hands the pen to the wrong side.
    """
    artifact_id, _ = _draft(db, class_id)
    seen: list[str] = []

    def watch(registry: dict[str, ToolDefinition]) -> None:
        seen.append(str(artifacts.get_artifact(db, artifact_id)["stage_detail"]))

    reviewer.script = [watch] * 6

    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))

    assert len(seen) == 6
    assert all(detail.startswith("Reviewing") for detail in seen), seen


def test_the_structure_lens_is_given_the_headings_verbatim(
    db: sqlite3.Connection, class_id: int, reviewer: _StubReviewer
) -> None:
    """Its findings have to quote something that exists in the document.

    The outline renders `1.1 Introduction (12 words)`, which appears nowhere in the text,
    so a lens quoting from it wrote quotes `resolve_quote` refused - the first live review
    filed a finding whose quote was the single character "T".
    """
    artifact_id, _ = _draft(db, class_id)

    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))

    structure_prompt = reviewer.runs[0][0][-1]["content"]
    assert "## Introduction" in structure_prompt
    assert "## Results" in structure_prompt


def test_a_re_review_reports_findings_it_reached_again_rather_than_none(
    db: sqlite3.Connection, class_id: int, reviewer: _StubReviewer
) -> None:
    """The second review of an unresolved draft files nothing, and must not say so.

    `add_comment` refuses a finding already open on the same passage at the same
    severity, which is right - it is the same finding, not a new one. Reporting the run
    as "no findings" told the student their draft was clean when every comment on it was
    still waiting for them.
    """
    artifact_id, part_id = _draft(db, class_id)
    finding = _files("Say what you measured.", "major", "The period grew with length.")
    reviewer.script = [finding]
    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))

    def refiles(registry: dict[str, ToolDefinition]) -> None:
        result = registry["add_comment"].handler(
            body="Say what you measured.",
            severity="major",
            quote="The period grew with length.",
        )
        assert not result.ok  # Deduped, exactly as intended.

    reviewer.script = [refiles]
    review_pipeline.run_review(review_pipeline.ReviewJob(artifact_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    detail = str(artifact["stage_detail"])
    assert detail != review_pipeline._COMPLETE_EMPTY_DETAIL
    assert "1 major" in detail
    assert "already filed" in detail
    # And still exactly one comment on the draft: confirmed, not duplicated.
    assert len(comments.list_threads(db, part_id, BODY)) == 1


def test_recovered_review_from_done_closes_from_persisted_findings_without_model_calls(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    root = comments.add_comment(
        db,
        part_id,
        comments.REVIEWER,
        "Tighten the claim.",
        severity="major",
        quote="The period grew with length.",
    )
    run = writer_runs.create_run(
        db,
        artifact_id,
        writer_runs.REVIEW,
        "quick",
        request={},
        started_at="2026-08-07T12:00:00+00:00",
    )
    writer_runs.checkpoint(
        db,
        int(run["id"]),
        stage="done",
        data={
            "filed_comment_ids": [int(root["id"])],
            "confirmed_comment_ids": [],
            "completed_lenses": review_pipeline.LENS_COUNT,
        },
    )
    db.execute(
        "update writer_runs set status = ? where id = ?", (writer_runs.RUNNING, int(run["id"]))
    )
    db.execute(
        "update artifacts set state = ?, stage_detail = ?, problems_total = ?, problems_done = ? "
        "where id = ?",
        (
            artifacts.GENERATING,
            "Landing findings: Reviewing claims",
            review_pipeline.LENS_COUNT,
            review_pipeline.LENS_COUNT,
            artifact_id,
        ),
    )
    db.commit()
    queued: list[object] = []
    monkeypatch.setattr(drafting, "enqueue", queued.append)
    monkeypatch.setattr(
        review_pipeline,
        "_review_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not rerun")),
    )

    requeued, recovered = drafting.reconcile_interrupted(db)

    assert requeued == 1
    assert recovered == 0
    review_pipeline.run_review(queued[0])
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert "1 major" in str(artifact["stage_detail"])


def test_recovered_review_resumes_at_the_next_completed_section_and_keeps_prior_findings(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "# Pendulum Lab\n\n## Introduction\n\nFirst claim.\n\n## Results\n\nSecond claim.\n"
    artifact_id, part_id = _draft(db, class_id, content=body)
    first = comments.add_comment(
        db,
        part_id,
        comments.REVIEWER,
        "Clarify the first claim.",
        severity="major",
        quote="First claim.",
    )
    run = writer_runs.create_run(
        db,
        artifact_id,
        writer_runs.REVIEW,
        "quick",
        request={},
        started_at="2026-08-07T12:00:00+00:00",
    )
    prose_targets = (
        "Reviewing prose: 1.1 Introduction",
        "Reviewing prose: 1.2 Results",
    )
    writer_runs.checkpoint(
        db,
        int(run["id"]),
        stage="prose",
        index=1,
        targets=prose_targets,
        data={
            "filed_comment_ids": [int(first["id"])],
            "confirmed_comment_ids": [],
            "completed_lenses": 2,
        },
    )
    db.execute(
        "update writer_runs set status = ? where id = ?", (writer_runs.RUNNING, int(run["id"]))
    )
    db.execute(
        "update artifacts set state = ?, stage_detail = ?, problems_total = 4, problems_done = 2 "
        "where id = ?",
        (artifacts.GENERATING, "Reviewing prose: 1 Introduction", artifact_id),
    )
    db.commit()
    queued: list[object] = []
    monkeypatch.setattr(drafting, "enqueue", queued.append)

    seen: list[str] = []
    filed_second = {"ok": False}

    def resumed_run(config, messages, registry, max_depth):  # noqa: ANN001, ANN003
        seen.append(str(artifacts.get_artifact(db, artifact_id)["stage_detail"]))
        stage = seen[-1]
        if stage == "Reviewing prose: 1.2 Results":
            result = registry["add_comment"].handler(
                body="Clarify the second claim.",
                severity="major",
                quote="Second claim.",
            )
            assert result.ok, result.error
            filed_second["ok"] = True
        return _DONE

    monkeypatch.setattr(review_pipeline, "_review_run", resumed_run)

    requeued, recovered = drafting.reconcile_interrupted(db)

    assert requeued == 1
    assert recovered == 0
    review_pipeline.run_review(queued[0])
    assert seen[0] == "Reviewing prose: 1.2 Results"
    assert any(stage.startswith("Reviewing claims:") for stage in seen)
    assert filed_second["ok"] is True
    assert "Reviewing structure" not in seen
    artifact = artifacts.get_artifact(db, artifact_id)
    assert "2 major" in str(artifact["stage_detail"])
