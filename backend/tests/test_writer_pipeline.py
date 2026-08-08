"""Contract tests for the draft pass: structure first, sections serially, landings safe.

The model is never called: `_complete` is stubbed with scripted repliers, the locality
gate is stubbed open, and retrieval is replaced so no embedding server runs. What is
under test is the machinery around the calls - who gets drafted, where the text lands,
what parks, and what a failure costs (never the student's words).
"""

import json
import sqlite3
import threading
import time
from collections.abc import Callable

import httpx
import pytest

from backend.core import (
    artifacts,
    briefs,
    comments,
    live_drafts,
    query_guard,
    source_ledger,
    suggestions,
    writer_pipeline,
    writer_plans,
    writer_runs,
)
from backend.core.app_settings import TutorConfig
from backend.rag.retrieve import RetrievalResult, RetrievedChunk

SKELETON = (
    "# Pendulum Lab\n"
    "\n"
    "## Introduction\n"
    "\n"
    "[TODO: say what the lab measures and why it matters]\n"
    "\n"
    "## Methods\n"
    "\n"
    "[TODO: describe the rig and the procedure]\n"
    "\n"
    "## Results\n"
    "\n"
    "[TODO: report the period-vs-length data]\n"
)

# A structured document with two occupied sections and one still-empty one.
STRUCTURED = (
    "# Pendulum Lab\n"
    "\n"
    "## Introduction\n"
    "\n"
    "We measured the pendulum period.\n"
    "\n"
    "## Methods\n"
    "\n"
    "[TODO: describe the rig and the procedure]\n"
    "\n"
    "## Results\n"
    "\n"
    "The period grew with length.\n"
)

Replier = Callable[[TutorConfig, list[dict[str, str]]], str]


class _StubModel:
    """Scripted `_complete`: each call pops the next replier (or plain string).

    `targets` records the per-call word target the pipeline computed, and `truncated` is
    the sink the real client appends to - a step that appends to it is a step standing in
    for a reply the endpoint cut off at its ceiling.
    """

    def __init__(self) -> None:
        self.script: list[object] = []
        self.prompts: list[list[dict[str, str]]] = []
        self.targets: list[int | None] = []
        self.evaluations = 0

    def __call__(
        self,
        config: TutorConfig,
        messages: list[dict[str, str]],
        target_words: int | None = None,
        truncated: list[bool] | None = None,
        schema: object | None = None,
    ) -> str:
        self.prompts.append(messages)
        self.targets.append(target_words)
        if schema is not None:
            self.evaluations += 1
        if not self.script and schema is not None:
            # A full pass ends by reading itself back. Unless a test scripts findings,
            # that read finds nothing - which is what a test about the drafting stages
            # means by not scripting it.
            return '{"sections": []}'
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return str(step(config, messages, truncated))
        return str(step)


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> _StubModel:
    stub = _StubModel()
    monkeypatch.setattr(writer_pipeline, "_complete", stub)
    return stub


@pytest.fixture(autouse=True)
def _open_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(writer_pipeline, "document_text_allowed", lambda conn: None)
    monkeypatch.setattr(
        writer_pipeline,
        "resolve_tutor_config",
        lambda conn: TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192),
    )
    monkeypatch.setattr(
        writer_pipeline,
        "retrieve",
        lambda conn, class_id, query, budget: RetrievalResult(
            chunks=[], trimmed=False, omitted_document_count=0
        ),
    )


def _draft(db: sqlite3.Connection, class_id: int, content: str = "") -> tuple[int, int]:
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


def _body(db: sqlite3.Connection, part_id: int) -> str:
    return str(artifacts.get_part(db, part_id)["content"])


def _section_reply(heading: str, prose: str) -> str:
    return f"## {heading}\n\n{prose}\n"


def test_a_durable_full_pass_uses_fixed_paragraph_stages_and_never_writes_the_document(
    db: sqlite3.Connection,
    class_id: int,
    model: _StubModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, part_id = _draft(db, class_id, content="My notes stay editable.\n")
    briefs.save_brief(
        db,
        artifact_id,
        summary="Explain why pendulum period depends on length.",
        length_target="700 words",
    )
    writer_plans.create_plan(
        db,
        artifact_id,
        brief_analysis='{"assignment_type":"essay","task":"explain","success_criteria":[]}',
        thesis="A pendulum's period is controlled by its length.",
        argument_map=[{"id": "c1", "claim": "Length changes period", "supports": []}],
        sections=[
            {
                "section_ref": "1.1",
                "ordinal": 0,
                "title": "Introduction",
                "job": "Frame the question and thesis.",
                "claim": "Length is the key variable.",
                "evidence": [],
                "source_ids": [],
                "word_budget": 320,
            },
            {
                "section_ref": "1.2",
                "ordinal": 1,
                "title": "Explanation",
                "job": "Explain the physical relationship.",
                "claim": "A longer pendulum has a longer period.",
                "evidence": [],
                "source_ids": [],
                "word_budget": 380,
            },
        ],
    )
    run = writer_runs.create_run(
        db,
        artifact_id,
        writer_runs.PASS,
        "quick",
        request={},
        started_at="2026-08-07T00:00:00+00:00",
    )
    db.commit()
    monkeypatch.setattr(writer_pipeline, "_prepare_research_batch", lambda *args, **kwargs: [])
    streamed_jobs: list[tuple[str, str, str | None]] = []

    def stream_paragraph(
        conn: sqlite3.Connection,
        job: writer_pipeline.PassJob,
        config: TutorConfig,
        suggestion_id: int,
        block: dict[str, object],
        messages: list[dict[str, str]],
    ) -> dict[str, object]:
        rendered = "\n".join(message["content"] for message in messages)
        previous = next(
            (
                line.split("The preceding paragraph:\n", 1)[1]
                for line in (message["content"] for message in messages)
                if "The preceding paragraph:\n" in line
            ),
            None,
        )
        streamed_jobs.append((str(block["stable_key"]), rendered, previous))
        return live_drafts.append_block_text(
            conn,
            suggestion_id,
            str(block["stable_key"]),
            f"Paragraph {block['paragraph_ordinal']} prose.",
            status="complete",
        )

    monkeypatch.setattr(writer_pipeline, "_stream_live_paragraph", stream_paragraph)
    model.script = [
        json.dumps(
            {
                "paragraphs": [
                    {
                        "key": "intro-1",
                        "purpose": "Open with the question.",
                        "claim": "Length matters.",
                        "evidence": [],
                        "target_words": 150,
                        "transition_in": "Open the paper.",
                        "transition_out": "Move to the mechanism.",
                    },
                    {
                        "key": "intro-2",
                        "purpose": "State the thesis.",
                        "claim": "Length controls period.",
                        "evidence": [],
                        "target_words": 170,
                        "transition_in": "Narrow the question.",
                        "transition_out": "Set up the explanation.",
                    },
                ]
            }
        ),
        json.dumps(
            {
                "paragraphs": [
                    {
                        "key": "explain-1",
                        "purpose": "Explain the mechanism.",
                        "claim": "Longer length increases period.",
                        "evidence": [],
                        "target_words": 380,
                        "transition_in": "Develop the thesis.",
                        "transition_out": "Close the explanation.",
                    }
                ]
            }
        ),
        '{"needs_change":false,"rationale":"clear","revised_next_paragraph":"Paragraph 2 prose."}',
        '{"needs_change":false,"rationale":"clear","revised_next_paragraph":"Paragraph 3 prose."}',
        '{"summary":"The chunk is coherent.","issues":[]}',
    ]

    writer_pipeline.run_pass(
        writer_pipeline.PassJob(artifact_id, depth="quick", run_id=int(run["id"]))
    )

    assert _body(db, part_id) == "My notes stay editable.\n"
    live = live_drafts.get_live_suggestion_for_run(db, int(run["id"]))
    assert live is not None
    assert live["stage"] == "completed"
    assert live["status"] == "ready"
    assert [block["stable_key"] for block in live["blocks"]] == [
        "1.1:p1",
        "1.1:p2",
        "1.2:p1",
    ]
    assert all(block["status"] == "complete" for block in live["blocks"])
    assert len(streamed_jobs) == 3
    assert "Global document map" in streamed_jobs[0][1]
    assert "Paragraph 1 prose." in streamed_jobs[1][1]
    pending = suggestions.pending_for_part(db, part_id)
    assert pending is not None
    assert "Paragraph 3 prose." in str(pending["proposed_content"])


def test_an_empty_draft_becomes_a_skeleton_and_then_a_full_document(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    briefs.save_brief(db, artifact_id, summary="Pendulum period vs length.")
    model.script = [
        SKELETON,
        _section_reply("Introduction", "We measure how period depends on length."),
        _section_reply("Methods", "A string, a bob, a stopwatch."),
        _section_reply("Results", "Period grew with the square root of length."),
    ]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id))

    body = _body(db, part_id)
    assert "We measure how period depends on length." in body
    assert "A string, a bob, a stopwatch." in body
    assert "Period grew with the square root of length." in body
    assert "[TODO:" not in body
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert artifact["stage_detail"] is None
    assert artifact["problems_total"] == 3
    assert artifact["problems_done"] == 3
    # No proposal: every section was empty at land time, so everything landed direct.
    assert suggestions.pending_for_part(db, part_id) is None
    # History reads as the pass proceeded: one revision for structure, one per section.
    revisions = db.execute(
        "select note from artifact_part_revisions where part_id = ? order by id",
        (part_id,),
    ).fetchall()
    assert [str(row["note"]) for row in revisions] == [
        "structured the document",
        "drafted 1.1 Introduction",
        "drafted 1.2 Methods",
        "drafted 1.3 Results",
    ]


def test_unheaded_prose_parks_on_a_structure_proposal(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    prose = "I measured pendulums all afternoon and here is what I found.\n"
    artifact_id, part_id = _draft(db, class_id, content=prose)
    model.script = [SKELETON]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id))

    # The student's words were not touched; the skeleton is a proposal to review.
    assert _body(db, part_id) == prose
    pending = suggestions.pending_for_part(db, part_id)
    assert pending is not None
    assert pending["note"] == "structure the document"
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert artifact["stage_detail"] == writer_pipeline.PARKED_DETAIL
    # Parked means parked: the section stage did not run.
    assert len(model.prompts) == 1


def test_an_instruction_pass_coalesces_occupied_sections_into_one_edit(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    artifact_id, part_id = _draft(db, class_id, content=STRUCTURED)
    model.script = [
        _section_reply("Introduction", "Tightened introduction."),
        _section_reply("Methods", "The rig, finally described."),
        _section_reply("Results", "Tightened results."),
    ]

    writer_pipeline.run_pass(
        writer_pipeline.PassJob(artifact_id, instruction="tighten every claim")
    )

    body = _body(db, part_id)
    # The empty section landed direct; the occupied ones did not move.
    assert "The rig, finally described." in body
    assert "We measured the pendulum period." in body
    assert "Tightened introduction." not in body
    pending = suggestions.pending_for_part(db, part_id)
    assert pending is not None
    assert pending["note"] == "tighten every claim"
    proposed = str(pending["proposed_content"])
    assert "Tightened introduction." in proposed
    assert "Tightened results." in proposed
    # One edit, both sections in it.
    assert int(db.execute("select count(*) as n from pending_edits").fetchone()["n"]) == 1


def test_a_section_filter_drafts_only_what_was_named(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    artifact_id, part_id = _draft(db, class_id, content=STRUCTURED)
    model.script = [_section_reply("Methods", "Only the methods.")]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id, section_refs=("methods",)))

    assert "Only the methods." in _body(db, part_id)
    assert len(model.prompts) == 1
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["problems_total"] == 1


def test_an_unknown_section_ref_fails_the_pass_cleanly(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    artifact_id, part_id = _draft(db, class_id, content=STRUCTURED)

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id, section_refs=("Discussion",)))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert 'No section matches "Discussion".' in str(artifact["error_message"])
    assert _body(db, part_id) == STRUCTURED
    assert model.prompts == []


def test_a_section_the_student_filled_mid_pass_is_proposed_not_overwritten(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    artifact_id, part_id = _draft(db, class_id, content=STRUCTURED)
    typed = STRUCTURED.replace(
        "[TODO: describe the rig and the procedure]", "My own methods, typed mid-pass."
    )

    def types_then_replies(
        config: TutorConfig, messages: list[dict[str, str]], truncated: list[bool] | None
    ) -> str:
        # The student types into the empty section while the model is thinking. The
        # landing must re-check and propose rather than overwrite.
        artifacts.set_part_content(
            db, part_id, typed, origin=artifacts.USER_CORRECTED, record_revision=False
        )
        return _section_reply("Methods", "Model prose that must not land directly.")

    model.script = [types_then_replies]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id, section_refs=("Methods",)))

    body = _body(db, part_id)
    assert "My own methods, typed mid-pass." in body
    assert "Model prose that must not land directly." not in body
    pending = suggestions.pending_for_part(db, part_id)
    assert pending is not None
    assert "Model prose that must not land directly." in str(pending["proposed_content"])


def test_replies_that_change_nothing_settle_as_no_changes(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    artifact_id, part_id = _draft(db, class_id, content=STRUCTURED)
    model.script = [
        "",  # Introduction: nothing came back.
        "## Methods\n\n[TODO: describe the rig and the procedure]",  # Methods: unchanged.
        "## Results\n\nThe period grew with length.",  # Results: unchanged.
    ]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id, instruction="polish"))

    assert _body(db, part_id) == STRUCTURED
    assert suggestions.pending_for_part(db, part_id) is None
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["stage_detail"] == writer_pipeline.NO_CHANGES_DETAIL
    assert artifact["problems_done"] == 3


def test_a_failed_pass_keeps_what_landed_and_reports_the_reason(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    artifact_id, part_id = _draft(db, class_id, content=STRUCTURED)
    model.script = [
        _section_reply("Introduction", "Tightened introduction."),
        RuntimeError("endpoint fell over"),
    ]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id, instruction="tighten"))

    artifact = artifacts.get_artifact(db, artifact_id)
    # `failed`, not `ready`: the workspace only renders `error_message` beside that
    # state, so settling ready wrote the reason somewhere nothing reads it.
    assert artifact["state"] == artifacts.FAILED
    assert "endpoint fell over" in str(artifact["error_message"])
    # The section that landed before the failure stays landed.
    pending = suggestions.pending_for_part(db, part_id)
    assert pending is not None
    assert "Tightened introduction." in str(pending["proposed_content"])


def test_a_draft_deleted_before_its_pass_is_the_cancel(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    artifact_id, _ = _draft(db, class_id)
    artifacts.delete_artifact(db, artifact_id)

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id))  # Must not raise.

    assert model.prompts == []


def test_a_blocked_gate_settles_with_its_message(
    db: sqlite3.Connection, class_id: int, model: _StubModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.core.app_settings import NO_ENDPOINT

    artifact_id, _ = _draft(db, class_id, content=STRUCTURED)
    monkeypatch.setattr(writer_pipeline, "document_text_allowed", lambda conn: NO_ENDPOINT)

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert "No tutor endpoint" in str(artifact["error_message"])


def test_a_section_prompt_carries_the_neighbours_and_the_lens(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    artifact_id, _ = _draft(db, class_id, content=STRUCTURED)
    model.script = [_section_reply("Methods", "The rig.")]

    writer_pipeline.run_pass(
        writer_pipeline.PassJob(artifact_id, instruction="be terse", section_refs=("Methods",))
    )

    turn = model.prompts[0][1]["content"]
    assert "We measured the pendulum period." in turn  # the preceding tail
    assert "## Results" in turn  # the next heading
    assert "Instruction for this pass: be terse" in turn
    assert "The section to write" in turn


def test_the_length_target_reaches_the_structure_and_the_sections(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    """The whole of "I asked for five pages and got three paragraphs".

    Nothing used to carry the brief's length past one labelled line: the structure stage
    was never told how many sections five pages needs, and a section run could not tell
    whether its own share was 200 words or 900.
    """
    artifact_id, _ = _draft(db, class_id)
    briefs.save_brief(
        db, artifact_id, summary="Pendulum period vs length.", length_target="5 pages"
    )
    model.script = [
        SKELETON,
        _section_reply("Introduction", "Words. " * 700),
        _section_reply("Methods", "Words. " * 700),
        _section_reply("Results", "Words. " * 700),
    ]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id))

    structure_prompt = model.prompts[0][-1]["content"]
    assert "2,000 words" in structure_prompt
    assert "sections" in structure_prompt
    # 2000 words over the skeleton's three leaf sections. The trailing call is the
    # revise stage's read-back, which writes nothing and so carries no target.
    assert model.targets[1:4] == [666, 666, 666]
    assert "666 words" in model.prompts[1][-1]["content"]


def test_a_length_the_student_typed_beats_the_brief(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    """The dialog is the more recent and more specific statement of what they want."""
    artifact_id, _ = _draft(db, class_id)
    briefs.save_brief(db, artifact_id, summary="Pendulum.", length_target="2 pages")
    model.script = [
        SKELETON,
        *[_section_reply(name, "Words. " * 900) for name in ("Introduction", "Methods", "Results")],
    ]

    writer_pipeline.run_pass(
        writer_pipeline.PassJob(artifact_id, instruction="draft this at about 6 pages")
    )

    # 6 pages, not the brief's 2, and the instruction itself reached the planner.
    assert "2,400 words" in model.prompts[0][-1]["content"]
    assert "draft this at about 6 pages" in model.prompts[0][-1]["content"]


def test_an_instruction_with_no_length_leaves_the_targets_alone(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    """A section number in the instruction is not a page count."""
    artifact_id, _ = _draft(db, class_id, content=STRUCTURED)
    model.script = [_section_reply("Methods", "The rig, described.")]

    writer_pipeline.run_pass(
        writer_pipeline.PassJob(
            artifact_id, instruction="tighten section 2", section_refs=("Methods",)
        )
    )

    assert model.targets == [None]


def test_a_section_that_comes_back_short_is_completed_in_an_append_only_follow_up(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    """A short first chunk is kept and Python asks only for what remains.

    Models under-write against a word target far more often than they over-write, and a
    section arriving at a third of its length is the difference between a draft and a
    sketch.
    """
    artifact_id, part_id = _draft(db, class_id, content=STRUCTURED)
    briefs.save_brief(db, artifact_id, summary="Pendulum.", length_target="1200 words")
    model.script = [
        _section_reply("Methods", "Far too short."),
        "A properly developed account. " * 300,
    ]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id, section_refs=("Methods",)))

    assert len(model.prompts) == 2
    # The follow-up carries only a bounded tail and asks for new prose, not a rewrite.
    retry = model.prompts[1]
    assert retry[-1]["role"] == "user"
    assert "Far too short." in retry[-1]["content"]
    assert "Do not repeat it" in retry[-1]["content"]
    assert "Far too short." in _body(db, part_id)
    assert "A properly developed account." in _body(db, part_id)


def test_a_section_long_enough_is_not_retried(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    artifact_id, _ = _draft(db, class_id, content=STRUCTURED)
    briefs.save_brief(db, artifact_id, summary="Pendulum.", length_target="900 words")
    # A filtered pass has one target, so the document's whole 900 is this section's share.
    model.script = [_section_reply("Methods", "Words. " * 860)]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id, section_refs=("Methods",)))

    assert model.targets == [900]
    assert len(model.prompts) == 1


def test_a_section_cut_off_at_the_ceiling_is_continued_before_it_lands(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    """A truncated chunk is real prose and the next model call appends to it."""
    artifact_id, part_id = _draft(db, class_id, content=STRUCTURED)

    def cut_off(
        config: TutorConfig, messages: list[dict[str, str]], truncated: list[bool] | None
    ) -> str:
        assert truncated is not None
        truncated.append(True)
        return _section_reply("Methods", "It begins well and then stops mid-")

    model.script = [cut_off, "the complete ending."]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id, section_refs=("Methods",)))

    assert "It begins well and then stops mid-" in _body(db, part_id)
    assert "the complete ending." in _body(db, part_id)
    assert len(model.prompts) == 2
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert artifact["stage_detail"] is None


def test_a_long_section_uses_as_many_output_chunks_as_its_small_window_requires(
    db: sqlite3.Connection,
    class_id: int,
    model: _StubModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A five-page target is not silently reduced to one endpoint-sized reply."""
    artifact_id, part_id = _draft(db, class_id, content=STRUCTURED)
    briefs.save_brief(db, artifact_id, summary="Pendulum.", length_target="5 pages")
    monkeypatch.setattr(
        writer_pipeline,
        "resolve_tutor_config",
        lambda conn: TutorConfig("http://127.0.0.1:9/v1", None, "m", 4096),
    )

    def chunk(
        config: TutorConfig, messages: list[dict[str, str]], truncated: list[bool] | None
    ) -> str:
        assert truncated is not None
        truncated.append(True)
        prose = "Evidence and analysis. " * 170
        return (
            _section_reply("Methods", prose)
            if "Continue section" not in messages[-1]["content"]
            else prose
        )

    model.script = [chunk, chunk, chunk, "A final developed paragraph. " * 170]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id, section_refs=("Methods",)))

    method = writer_pipeline.sections.extract(_body(db, part_id), "Methods")
    assert method is not None
    assert len(writer_pipeline.sections.prose(method).split()) >= 1_900
    assert len(model.prompts) == 4
    assert artifacts.get_artifact(db, artifact_id)["stage_detail"] is None


def test_the_generation_ceiling_is_computed_and_capped(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling is sent, and never exceeds the window's generation reserve.

    Left unset it was whatever the endpoint was launched with - a number Lyra does not
    know and the student did not choose.
    """
    from backend.llm import budget

    sent: list[object] = []
    timeouts: list[object] = []

    async def fake_complete(endpoint, api_key, model, messages, **kwargs):  # noqa: ANN001
        sent.append(kwargs.get("max_tokens"))
        timeouts.append(kwargs.get("request_timeout"))
        return "text"

    monkeypatch.setattr(writer_pipeline.client, "complete", fake_complete)
    monkeypatch.setattr(
        writer_pipeline._writer_local,
        "deadline",
        time.monotonic() + 30,
        raising=False,
    )
    config = TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192)

    writer_pipeline._complete(config, [{"role": "user", "content": "x"}], 300)
    writer_pipeline._complete(config, [{"role": "user", "content": "x"}], 100_000)
    writer_pipeline._complete(config, [{"role": "user", "content": "x"}], None)

    reserve = budget.generation_reserve(8192)
    assert sent[0] == budget.tokens_for_words(300)
    assert sent[1] == reserve  # A target larger than the window is clamped to it.
    assert sent[2] == reserve  # No target at all still sends the reserve.
    assert all(isinstance(timeout, httpx.Timeout) for timeout in timeouts)
    assert all(0 < timeout.read <= 30 for timeout in timeouts if isinstance(timeout, httpx.Timeout))


def test_live_paragraph_retries_when_the_model_spends_the_first_call_only_reasoning(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id, _ = _draft(db, class_id)
    suggestion = live_drafts.create_live_suggestion(db, artifact_id, run_id=91)
    block = live_drafts.model_update_block(
        db,
        int(suggestion["id"]),
        "1:p1",
        paragraph_ordinal=1,
        target_words=180,
    )
    calls: list[list[dict[str, str]]] = []

    async def fake_stream_chat(endpoint, api_key, model, messages, **kwargs):  # noqa: ANN001
        calls.append(messages)
        if len(calls) == 1:
            yield writer_pipeline.client.StreamDelta("reasoning", "I should plan this paragraph.")
            return
        yield writer_pipeline.client.StreamDelta("answer", "The paragraph begins immediately.")

    monkeypatch.setattr(writer_pipeline.client, "stream_chat", fake_stream_chat)

    completed = writer_pipeline._stream_live_paragraph(
        db,
        writer_pipeline.PassJob(artifact_id, _deadline=time.monotonic() + 60),
        TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192),
        int(suggestion["id"]),
        block,
        [{"role": "user", "content": "/no_think\n\nWrite the paragraph."}],
    )

    assert len(calls) == 2
    assert "/no_think" in calls[1][-1]["content"]
    assert completed["content"] == "The paragraph begins immediately."


# --------------------------------------------------------------------------------
# The revise stage: the pass reads back what it wrote before handing it over.
# --------------------------------------------------------------------------------


def _finds(*pairs: tuple[str, str]) -> str:
    """A scripted revise evaluation naming sections and what is wrong with them."""
    import json

    return json.dumps({"sections": [{"section": n, "problem": p} for n, p in pairs]})


def test_a_full_pass_reads_itself_back_and_rewrites_what_it_finds(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    """The difference between a pipeline that emits sections and one that drafts.

    The evaluator's sentence becomes the rewrite's instruction, so the second attempt is
    aimed at what was wrong rather than being an undirected retry.
    """
    artifact_id, part_id = _draft(db, class_id)
    model.script = [
        SKELETON,
        _section_reply("Introduction", "A first attempt at the introduction."),
        _section_reply("Methods", "The rig, described."),
        _section_reply("Results", "The results, reported."),
        _finds(("1.1", "It never says what the lab was for.")),
        _section_reply("Introduction", "A properly motivated introduction."),
        _finds(),  # Round two: clean, so the loop stops.
    ]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id))

    body = _body(db, part_id)
    assert "A properly motivated introduction." in body
    assert "A first attempt at the introduction." not in body
    # The rewrite was told what was wrong with it.
    rewrite = model.prompts[5][-1]["content"]
    assert "It never says what the lab was for." in rewrite
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    # The extra work is counted, so the progress strip stays honest.
    assert artifact["problems_total"] == 4
    assert artifact["problems_done"] == 4


def test_a_draft_that_holds_up_is_left_alone(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    """An empty finding list is a good answer, and costs exactly one call."""
    artifact_id, _ = _draft(db, class_id)
    model.script = [
        SKELETON,
        _section_reply("Introduction", "Motivated and complete."),
        _section_reply("Methods", "The rig, described."),
        _section_reply("Results", "The results, reported."),
        _finds(),
    ]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id))

    assert model.evaluations == 1
    assert len(model.prompts) == 5
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.READY


def test_a_leftover_todo_is_flagged_without_asking_the_model(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    """Code can see this one for free, so it does not wait to be told.

    A section still holding its intent marker is unwritten however confidently the
    evaluator reports the draft is fine.
    """
    artifact_id, part_id = _draft(db, class_id)
    model.script = [
        SKELETON,
        _section_reply("Introduction", "Written properly."),
        _section_reply("Methods", "[TODO: describe the rig and the procedure]"),
        _section_reply("Results", "Written properly."),
        _finds(),  # The evaluator sees nothing wrong; the counting does.
        _section_reply("Methods", "The rig, finally described."),
        _finds(),
    ]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id))

    body = _body(db, part_id)
    assert "The rig, finally described." in body
    assert "[TODO:" not in body


def test_a_thin_section_is_flagged_against_its_share_of_the_length(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    briefs.save_brief(db, artifact_id, summary="Pendulum.", length_target="1500 words")
    long_enough = "Words. " * 500
    model.script = [
        SKELETON,
        _section_reply("Introduction", long_enough),
        _section_reply("Methods", long_enough),
        # Short, and short again on the drafting retry, so it reaches revise still thin.
        _section_reply("Results", "Barely anything."),
        _section_reply("Results", "Barely anything at all."),
        _finds(),
        _section_reply("Results", long_enough),
        _finds(),
    ]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id))

    revision = model.prompts[6][-1]["content"]
    assert "full length" in revision or "Develop it" in revision
    assert "Words." in _body(db, part_id)


def test_the_revise_stage_stops_after_its_second_round(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    """Bounded, because the endpoint is serial and a satisfied loop never arrives."""
    artifact_id, _ = _draft(db, class_id)
    model.script = [
        SKELETON,
        _section_reply("Introduction", "One."),
        _section_reply("Methods", "Two."),
        _section_reply("Results", "Three."),
    ]
    # Every evaluation keeps finding the same fault; every rewrite keeps landing.
    counter = {"n": 0}

    def always_finds(config: TutorConfig, messages: list[dict[str, str]], truncated: object) -> str:
        counter["n"] += 1
        return _finds(("1.1", "Still not right."))

    def rewrite(config: TutorConfig, messages: list[dict[str, str]], truncated: object) -> str:
        return _section_reply("Introduction", f"Attempt {counter['n']}.")

    model.script.extend([always_finds, rewrite, always_finds, rewrite, always_finds, rewrite])

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id))

    # The shared quick budget permits one whole-document evaluation pass.
    assert model.evaluations == writer_pipeline.writer_budgets.BUDGETS["quick"].evaluation_passes
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.READY


def test_an_unreadable_evaluation_costs_the_polish_not_the_draft(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    """The revise stage improves a draft that already landed; it may never cost one."""
    artifact_id, part_id = _draft(db, class_id)
    model.script = [
        SKELETON,
        _section_reply("Introduction", "The introduction."),
        _section_reply("Methods", "The methods."),
        _section_reply("Results", "The results."),
        "not json at all",
    ]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert "The introduction." in _body(db, part_id)


def test_a_filtered_pass_does_not_revise(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    """A lens pass over named sections is the student steering; it does not second-guess."""
    artifact_id, _ = _draft(db, class_id, content=STRUCTURED)
    model.script = [_section_reply("Methods", "The rig, described.")]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id, section_refs=("Methods",)))

    assert model.evaluations == 0


def test_revising_a_section_the_student_typed_into_proposes_rather_than_overwrites(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    """The revise stage may finish its own work; it may never overwrite the student's.

    A section the pass wrote and still owns is rewritten in place - that is one run
    finishing what it started. The moment the student's own words are in it, the same
    rewrite becomes a proposal, because the text is compared at land time rather than
    assumed from who wrote it a minute ago.
    """
    artifact_id, part_id = _draft(db, class_id)

    def student_types(
        config: TutorConfig, messages: list[dict[str, str]], truncated: object
    ) -> str:
        # The student edits the Introduction while the revise evaluation is running.
        body = _body(db, part_id)
        section = writer_pipeline.sections.extract(body, "1.1")
        assert section is not None
        artifacts.set_part_content(
            db,
            part_id,
            writer_pipeline.sections.splice(
                body, section, "## Introduction\n\nMy own words, thank you.\n\n"
            ),
            origin=artifacts.USER_CORRECTED,
            record_revision=False,
        )
        return _finds(("1.1", "It never says what the lab was for."))

    model.script = [
        SKELETON,
        _section_reply("Introduction", "A first attempt."),
        _section_reply("Methods", "The rig."),
        _section_reply("Results", "The results."),
        student_types,
        _section_reply("Introduction", "A rewrite that must not land directly."),
        _finds(),
    ]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id))

    body = _body(db, part_id)
    assert "My own words, thank you." in body
    assert "A rewrite that must not land directly." not in body
    pending = suggestions.pending_for_part(db, part_id)
    assert pending is not None
    assert "A rewrite that must not land directly." in str(pending["proposed_content"])


def test_planning_is_persisted_and_pause_at_plan_stops_before_drafting(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    model.script = [
        json.dumps(
            {
                "assignment_type": "argument",
                "task": "Explain why period depends on length.",
                "success_criteria": ["Use measured evidence"],
            }
        ),
        json.dumps(
            {
                "candidates": ["Length controls period", "Mass controls period"],
                "selected": "Length controls period",
                "rationale": "The measurements support it.",
            }
        ),
        json.dumps([{"id": "c1", "claim": "Longer pendulums swing slower", "supports": []}]),
        json.dumps(
            {
                "sections": [
                    {
                        "ref": "1.1",
                        "title": "Argument",
                        "job": "Establish the measured relationship",
                        "claim": "Period rises with length",
                        "evidence": ["Measured periods"],
                        "source_ids": [],
                        "word_budget": 400,
                    }
                ]
            }
        ),
    ]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id, depth="deep", pause_at_plan=True))

    plan = writer_plans.get_active_plan(db, artifact_id)
    assert plan is not None
    assert plan["thesis"] == "Length controls period"
    assert plan["sections"][0]["job"] == "Establish the measured relationship"
    assert "[TODO: Establish the measured relationship" in _body(db, part_id)
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["stage_detail"] == writer_pipeline.PLAN_PARKED_DETAIL
    metadata = db.execute(
        "select writer_job_depth, writer_job_completed_at from artifacts where id = ?",
        (artifact_id,),
    ).fetchone()
    assert metadata["writer_job_depth"] == "deep"
    assert metadata["writer_job_completed_at"] is not None


def test_python_reconciles_planned_section_budgets_to_the_requested_document_length(
    db: sqlite3.Connection,
    class_id: int,
    model: _StubModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A small model's undersized plan cannot shrink a five-page assignment."""
    body = "# Essay\n\n## First\n\n[TODO: first]\n\n## Second\n\n[TODO: second]\n"
    artifact_id, _ = _draft(db, class_id, content=body)
    briefs.save_brief(db, artifact_id, summary="An argument.", length_target="5 pages")
    writer_plans.create_plan(
        db,
        artifact_id,
        thesis="A thesis",
        argument_map={"claims": []},
        sections=[
            {
                "section_ref": "1.1",
                "ordinal": 0,
                "title": "First",
                "job": "First job",
                "claim": "First claim",
                "evidence": [],
                "source_ids": [],
                "word_budget": 200,
            },
            {
                "section_ref": "1.2",
                "ordinal": 1,
                "title": "Second",
                "job": "Second job",
                "claim": "Second claim",
                "evidence": [],
                "source_ids": [],
                "word_budget": 200,
            },
        ],
    )
    monkeypatch.setattr(writer_pipeline, "_prepare_research_batch", lambda *args: [])
    monkeypatch.setattr(writer_pipeline, "_converge_section", lambda *args: False)
    monkeypatch.setattr(writer_pipeline, "_weave_stage", lambda *args: False)
    model.script = [
        _section_reply("First", "First evidence. " * 475),
        _section_reply("Second", "Second evidence. " * 475),
    ]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id))

    assert model.targets == [1_000, 1_000]


def test_a_filtered_planned_pass_keeps_the_sections_share_of_the_document_length(
    db: sqlite3.Connection,
    class_id: int,
    model: _StubModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editing one section of a five-page paper must not make that section five pages."""
    body = "# Essay\n\n## First\n\n[TODO: first]\n\n## Second\n\n[TODO: second]\n"
    artifact_id, _ = _draft(db, class_id, content=body)
    briefs.save_brief(db, artifact_id, summary="An argument.", length_target="5 pages")
    writer_plans.create_plan(
        db,
        artifact_id,
        thesis="A thesis",
        argument_map=[],
        sections=[
            {
                "section_ref": "1.1",
                "ordinal": 0,
                "title": "First",
                "job": "First job",
                "claim": "First claim",
                "evidence": [],
                "source_ids": [],
                "word_budget": 200,
            },
            {
                "section_ref": "1.2",
                "ordinal": 1,
                "title": "Second",
                "job": "Second job",
                "claim": "Second claim",
                "evidence": [],
                "source_ids": [],
                "word_budget": 200,
            },
        ],
    )
    monkeypatch.setattr(writer_pipeline, "_prepare_research_batch", lambda *args: [])
    monkeypatch.setattr(writer_pipeline, "_converge_section", lambda *args: False)
    model.script = [_section_reply("First", "Focused evidence. " * 475)]

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id, section_refs=("First",)))

    assert model.targets == [1_000]


def test_parallel_planned_drafts_run_model_calls_together_but_land_in_plan_order(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "# Essay\n\n## First\n\n[TODO: first]\n\n## Second\n\n[TODO: second]\n"
    artifact_id, part_id = _draft(db, class_id, content=body)
    writer_plans.create_plan(
        db,
        artifact_id,
        thesis="A thesis",
        argument_map={"claims": []},
        sections=[
            {
                "section_ref": "1.1",
                "ordinal": 0,
                "title": "First",
                "job": "First job",
                "claim": "First claim",
                "evidence": [],
                "source_ids": [],
                "word_budget": 200,
            },
            {
                "section_ref": "1.2",
                "ordinal": 1,
                "title": "Second",
                "job": "Second job",
                "claim": "Second claim",
                "evidence": [],
                "source_ids": [],
                "word_budget": 200,
            },
        ],
    )
    db.execute("update settings set parallel_requests = 1, parallel_concurrency = 2 where id = 1")
    db.commit()
    monkeypatch.setattr(writer_pipeline, "_prepare_research_section", lambda *args: None)
    monkeypatch.setattr(writer_pipeline, "_converge_section", lambda *args: False)
    monkeypatch.setattr(writer_pipeline, "_weave_stage", lambda *args: False)
    barrier = threading.Barrier(2)
    active = 0
    peak = 0
    lock = threading.Lock()

    def complete(
        config: TutorConfig,
        messages: list[dict[str, str]],
        target_words: int | None = None,
        truncated: list[bool] | None = None,
        schema: object | None = None,
    ) -> str:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=2)
        text = messages[-1]["content"]
        reply = (
            _section_reply("First", "First prose. " * 100)
            if "## First" in text
            else _section_reply("Second", "Second prose. " * 100)
        )
        with lock:
            active -= 1
        return reply

    monkeypatch.setattr(writer_pipeline, "_complete", complete)
    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id))

    landed = _body(db, part_id)
    assert peak == 2
    assert landed.index("First prose.") < landed.index("Second prose.")


def test_parallel_research_and_drafting_use_the_same_inputs_and_land_as_serial(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "# Essay\n\n## First\n\n[TODO: first]\n\n## Second\n\n[TODO: second]\n"
    cursor = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, 'shared.pdf', '/tmp/shared.pdf', 'application/pdf', 1, 'ready')",
        (class_id,),
    )
    document_id = int(cursor.lastrowid or 0)
    db.execute(
        "insert into chunks (document_id, class_id, content, token_count, doc_type, "
        "embedding_model, embedding_dim) values (?, ?, ?, 6, 'reading', 'nomic', 768)",
        (document_id, class_id, "First evidence. Second evidence."),
    )
    db.commit()
    source = source_ledger.upsert_source(
        db,
        class_id,
        source_type=source_ledger.COURSE,
        title="shared.pdf",
        document_id=document_id,
    )
    source_id = int(source["id"])
    chunk = RetrievedChunk(
        chunk_id=1,
        document_id=document_id,
        content="First evidence. Second evidence.",
        token_count=6,
        page_number=1,
        section_title="Evidence",
        section_path=None,
        section_number=None,
        problem_number=None,
        part_index=0,
        filename="shared.pdf",
        similarity=0.9,
        score=0.9,
    )
    monkeypatch.setattr(
        writer_pipeline,
        "retrieve",
        lambda *args: RetrievalResult([chunk], trimmed=False, omitted_document_count=0),
    )

    def planned_draft() -> tuple[int, int]:
        artifact_id, part_id = _draft(db, class_id, content=body)
        writer_plans.create_plan(
            db,
            artifact_id,
            thesis="A thesis",
            argument_map=[],
            sections=[
                {
                    "section_ref": "1.1",
                    "ordinal": 0,
                    "title": "First",
                    "job": "First job",
                    "claim": "First claim",
                    "evidence": [],
                    "source_ids": [source_id],
                    "word_budget": 200,
                },
                {
                    "section_ref": "1.2",
                    "ordinal": 1,
                    "title": "Second",
                    "job": "Second job",
                    "claim": "Second claim",
                    "evidence": [],
                    "source_ids": [source_id],
                    "word_budget": 200,
                },
            ],
        )
        return artifact_id, part_id

    captured: dict[str, dict[tuple[str, str], str]] = {"serial": {}, "parallel": {}}
    mode = "serial"

    def complete(
        config: TutorConfig,
        messages: list[dict[str, str]],
        target_words: int | None = None,
        truncated: list[bool] | None = None,
        schema: object | None = None,
    ) -> str:
        user = messages[-1]["content"]
        section = "first" if "First job" in user else "second"
        schema_name = getattr(schema, "name", "")
        kind = "research" if schema_name == "writer_section_research_notes" else "draft"
        captured[mode][(kind, section)] = user
        if kind == "research":
            excerpt = "First evidence." if section == "first" else "Second evidence."
            return json.dumps(
                {
                    "notes": [f"{section} note"],
                    "source_ids": [str(source_id)],
                    "gaps": [],
                    "relied_on": [{"source_id": source_id, "excerpt": excerpt}],
                }
            )
        return _section_reply(section.title(), f"{section.title()} prose. " * 100)

    monkeypatch.setattr(writer_pipeline, "_complete", complete)
    monkeypatch.setattr(writer_pipeline, "_converge_section", lambda *args: False)
    monkeypatch.setattr(writer_pipeline, "_weave_stage", lambda *args: False)

    serial_id, serial_part = planned_draft()
    writer_pipeline.run_pass(writer_pipeline.PassJob(serial_id))

    db.execute("delete from writer_source_excerpts where source_id = ?", (source_id,))
    db.commit()
    mode = "parallel"
    db.execute("update settings set parallel_requests = 1, parallel_concurrency = 2 where id = 1")
    db.commit()
    parallel_id, parallel_part = planned_draft()
    writer_pipeline.run_pass(writer_pipeline.PassJob(parallel_id))

    assert captured["parallel"] == captured["serial"]
    assert _body(db, parallel_part) == _body(db, serial_part)
    first_draft = captured["serial"][("draft", "first")]
    second_draft = captured["serial"][("draft", "second")]
    assert '"excerpt": "First evidence."' in first_draft
    assert '"excerpt": "Second evidence."' not in first_draft
    assert '"excerpt": "Second evidence."' in second_draft
    assert '"excerpt": "First evidence."' not in second_draft


def test_planned_research_reports_when_web_research_degrades_to_course_only(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "# Essay\n\n## First\n\n[TODO: first]\n"
    artifact_id, part_id = _draft(db, class_id, content=body)
    writer_plans.create_plan(
        db,
        artifact_id,
        thesis="A thesis",
        argument_map=[],
        sections=[
            {
                "section_ref": "1.1",
                "ordinal": 0,
                "title": "First",
                "job": "weighted residual method proof for nonlinear boundary conditions",
                "claim": "First claim",
                "evidence": [],
                "source_ids": [],
                "word_budget": 200,
            }
        ],
    )
    db.execute("update settings set allow_web_research = 1 where id = 1")
    db.commit()

    captured: dict[str, object] = {}

    def guarded_search(
        query: str, *, allowed: bool, firecrawl_base_url: str, private_context: tuple[str, ...]
    ) -> list[dict[str, str]]:
        captured["query"] = query
        captured["private_context"] = private_context
        guarded = query_guard.guard_web_query(query, private_context=private_context)
        if isinstance(guarded, query_guard.QueryRefusal):
            raise ValueError(guarded.message)
        return []

    def complete(
        config: TutorConfig,
        messages: list[dict[str, str]],
        target_words: int | None = None,
        truncated: list[bool] | None = None,
        schema: object | None = None,
    ) -> str:
        if getattr(schema, "name", "") == "writer_section_research_notes":
            return json.dumps(
                {
                    "notes": ["course-only note"],
                    "source_ids": [],
                    "gaps": [],
                    "relied_on": [],
                }
            )
        return _section_reply("First", "First prose.")

    monkeypatch.setattr(writer_pipeline.web_research, "search_web", guarded_search)
    monkeypatch.setattr(writer_pipeline, "_complete", complete)
    monkeypatch.setattr(writer_pipeline, "_converge_section", lambda *args: False)
    monkeypatch.setattr(writer_pipeline, "_weave_stage", lambda *args: False)

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert captured["query"] == "weighted residual method proof for nonlinear boundary conditions"
    assert (
        "weighted residual method proof for nonlinear boundary conditions"
        not in captured["private_context"]
    )
    assert "Web research was unavailable" not in str(artifact["stage_detail"])
    assert "First prose." in _body(db, part_id)
    plan = writer_plans.get_active_plan(db, artifact_id)
    assert plan is not None
    notes = json.loads(str(plan["sections"][0]["research_notes"]))
    assert "warning" not in notes


def _planning_replies(*titles: str) -> list[str]:
    return [
        json.dumps(
            {
                "assignment_type": "argument",
                "task": "Defend the draft's thesis.",
                "success_criteria": ["Use evidence"],
            }
        ),
        json.dumps(
            {
                "candidates": ["The selected thesis"],
                "selected": "The selected thesis",
                "rationale": "It fits the evidence.",
            }
        ),
        json.dumps([{"id": "c1", "claim": "The central claim", "supports": []}]),
        json.dumps(
            {
                "sections": [
                    {
                        "ref": f"1.{index}",
                        "title": title,
                        "job": f"Do the {title.lower()} job",
                        "claim": f"{title} claim",
                        "evidence": [],
                        "source_ids": [],
                        "word_budget": 300,
                    }
                    for index, title in enumerate(titles, 1)
                ]
            }
        ),
    ]


def test_structured_draft_without_a_plan_plans_then_researches_and_converges(
    db: sqlite3.Connection,
    class_id: int,
    model: _StubModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = (
        "# Essay\n\n## Introduction\n\nExisting introduction.\n\n"
        "## Evidence\n\nExisting evidence.\n"
    )
    artifact_id, part_id = _draft(db, class_id, content=body)
    model.script = [
        *_planning_replies("Introduction", "Evidence"),
        _section_reply("Introduction", "Reworked introduction. " * 150),
        _section_reply("Evidence", "Reworked evidence. " * 150),
    ]
    researched: list[str] = []
    converged: list[str] = []

    def prepare_research(*args: object) -> list[writer_pipeline._ResearchWork]:
        targets = args[5]
        assert isinstance(targets, list)
        researched.extend(str(number) for number, _ in targets)
        return []

    monkeypatch.setattr(writer_pipeline, "_prepare_research_batch", prepare_research)

    def converge(*args: object, **kwargs: object) -> bool:
        converged.append(str(args[6]))
        return False

    monkeypatch.setattr(writer_pipeline, "_converge_section", converge)
    monkeypatch.setattr(writer_pipeline, "_weave_stage", lambda *args, **kwargs: False)

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id))

    plan = writer_plans.get_active_plan(db, artifact_id)
    assert plan is not None
    assert plan["argument_map"] == [{"id": "c1", "claim": "The central claim", "supports": []}]
    assert researched == ["1.1", "1.2"]
    assert converged == ["1.1", "1.2"]
    assert _body(db, part_id) == body
    pending = suggestions.pending_for_part(db, part_id)
    assert pending is not None
    assert "## Introduction" in str(pending["proposed_content"])
    assert "## Evidence" in str(pending["proposed_content"])


def test_addressed_comment_waits_for_an_occupied_section_proposal_to_be_accepted(
    db: sqlite3.Connection, class_id: int, model: _StubModel
) -> None:
    artifact_id, part_id = _draft(db, class_id, content=STRUCTURED)
    root = comments.add_comment(
        db,
        part_id,
        comments.REVIEWER,
        "Tighten this claim.",
        severity="major",
        quote="The period grew with length.",
    )
    model.script = [_section_reply("Results", "A better supported result.")]

    writer_pipeline.run_pass(
        writer_pipeline.PassJob(
            artifact_id,
            instruction="Address the finding",
            section_refs=("Results",),
            address_comment_id=int(root["id"]),
        )
    )

    unresolved = comments.unresolved_threads(db, part_id, _body(db, part_id))
    assert [thread["id"] for thread in unresolved] == [root["id"]]
    pending = suggestions.pending_for_part(db, part_id)
    assert pending is not None
    link = db.execute(
        "select comment_id from pending_edit_comment_links where edit_id = ?",
        (pending["id"],),
    ).fetchone()
    assert link is not None and int(link["comment_id"]) == int(root["id"])


def test_writer_stops_before_a_model_call_when_the_depth_wall_clock_is_spent(
    db: sqlite3.Connection,
    class_id: int,
    model: _StubModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id, _ = _draft(db, class_id)
    ticks = iter((0.0, 601.0, 601.0))
    monkeypatch.setattr(writer_pipeline.time, "monotonic", lambda: next(ticks, 601.0))

    writer_pipeline.run_pass(writer_pipeline.PassJob(artifact_id, depth="quick"))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert "time budget was exhausted" in str(artifact["error_message"])
    assert model.prompts == []
