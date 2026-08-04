"""The solve run: what lands, when, and what a failure costs.

`run_solve` is called directly rather than through the queue, so the tests stay
synchronous and deterministic. The tutor model is faked at the two seams the solver
reaches it through, so nothing here touches the network.

What this file defends, in the order it matters:

- Results are committed per problem, not at the end. A student who closes the laptop
  mid-solve comes back to finished work, and that is only true if each problem is written
  as it completes.
- A failed problem never fails the artifact.
- Document text never reaches an endpoint the student has not acknowledged, on this path
  as on every other.
- A verdict is never better than what actually happened.
"""

import json
import re
import sqlite3
from pathlib import Path

import pytest

from backend.config import settings
from backend.core import artifacts, solver, solving, verification
from backend.core.artifacts import SourceSpec
from backend.llm import tools
from backend.rag.retrieve import RetrievalResult, RetrievedChunk

_STATEMENT = "Find the Laplace transform of a unit ramp."

_SOLUTION = {
    "steps": [
        {"title": "Set up the integral", "content": "Apply the definition.", "sources": [1]},
        {"title": "Evaluate", "content": "Integrate by parts.", "sources": []},
    ],
    "answer": "1/s^2",
}


@pytest.fixture(autouse=True)
def local_endpoint(db: sqlite3.Connection) -> None:
    """A configured local endpoint, which is what lets solving run at all."""
    db.execute("update settings set endpoint_url = 'http://127.0.0.1:8080/v1' where id = 1")
    db.commit()


def _document(db: sqlite3.Connection, class_id: int, filename: str = "hw4.pdf") -> int:
    cursor = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, ?, '/tmp/x', 'application/pdf', 1, 'ready')",
        (class_id, filename),
    )
    db.commit()
    document_id = int(cursor.lastrowid or 0)
    settings.text_dir.mkdir(parents=True, exist_ok=True)
    Path(settings.text_dir / f"{document_id}.txt").write_text("Reference text.", encoding="utf-8")
    return document_id


def _set(
    db: sqlite3.Connection,
    class_id: int,
    statements: list[str],
    sources: list[SourceSpec] | None = None,
) -> int:
    """A solution set sitting at the review gate with a confirmed problem list."""
    document_id = _document(db, class_id)
    created = artifacts.create_artifact(
        db, class_id, "Problem set 4", sources or [SourceSpec(document_id)]
    )
    artifact_id = int(created["id"])
    for ordinal, statement in enumerate(statements):
        artifacts.create_part(
            db,
            artifact_id,
            artifacts.PROBLEM,
            ordinal,
            label=f"Problem {ordinal + 1}",
            content=statement,
        )
    artifacts.set_problems_total(db, artifact_id, len(statements))
    artifacts.set_artifact_state(db, artifact_id, artifacts.AWAITING_REVIEW)
    return artifact_id


def fake_solver(
    monkeypatch: pytest.MonkeyPatch,
    replies: object,
    *,
    tool_support: bool = False,
    on_reply: object = None,
    chunks: list[RetrievedChunk] | None = None,
) -> list[list[dict[str, str]]]:
    """Answer every solve call from `replies`, and stop the worker reaching the network.

    Args:
        replies: One reply for every problem, or a single reply reused for all of them.
            An `Exception` is raised instead of returned.
        tool_support: What the capability probe reports. Off by default, so a test that
            does not care about checking gets the honest `unchecked` verdict rather than
            an accidental pass.
        on_reply: Called after each reply, which is how the per-problem commit is observed
            from inside the run.
        chunks: What retrieval returns. Empty by default, so a test that does not set it
            up cannot accidentally assert on provenance that happens to be there.

    Returns:
        The prompts that were sent, in order, so a test can assert what the model saw.
    """
    sent: list[list[dict[str, str]]] = []
    queue = list(replies) if isinstance(replies, list) else None
    result = RetrievalResult(chunks=chunks or [], trimmed=False, omitted_document_count=0)

    monkeypatch.setattr(solver, "_tool_support", lambda conn, config: tool_support)
    monkeypatch.setattr(solving, "retrieve", lambda conn, class_id, query, budget: result)

    async def complete(
        endpoint: str, api_key: object, model: object, messages: list[dict[str, str]]
    ) -> str:
        sent.append(messages)
        reply = queue.pop(0) if queue else replies
        if callable(on_reply):
            on_reply()
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)

    monkeypatch.setattr(solver.client, "complete", complete)
    return sent


@pytest.fixture
def retrieved(db: sqlite3.Connection, class_id: int) -> RetrievedChunk:
    """A real chunk row, so provenance written against it satisfies the foreign key.

    Provenance points at chunks and documents that exist. Faking retrieval with invented
    ids would pass a test the product cannot pass.
    """
    document_id = _document(db, class_id, "lecture3.pdf")
    cursor = db.execute(
        "insert into chunks (document_id, class_id, content, token_count, page_number, "
        "doc_type, embedding_model, embedding_dim) "
        "values (?, ?, 'The Laplace transform is defined as...', 10, 12, 'lecture', 'nomic', 768)",
        (document_id, class_id),
    )
    db.commit()
    return RetrievedChunk(
        chunk_id=int(cursor.lastrowid or 0),
        document_id=document_id,
        content="The Laplace transform is defined as...",
        token_count=10,
        page_number=12,
        section_title="Transforms",
        problem_number=None,
        part_index=None,
        filename="lecture3.pdf",
        similarity=0.9,
        score=0.9,
    )


def _children(db: sqlite3.Connection, part_id: int, kind: str) -> list[dict[str, object]]:
    return [child for child in artifacts.list_child_parts(db, part_id) if child["kind"] == kind]


def _problems(db: sqlite3.Connection, artifact_id: int) -> list[dict[str, object]]:
    return [
        part
        for part in artifacts.list_parts(db, artifact_id)
        if part["parent_part_id"] is None and part["kind"] == artifacts.PROBLEM
    ]


def test_a_solved_problem_becomes_steps_and_an_answer(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id = _set(db, class_id, [_STATEMENT])
    fake_solver(monkeypatch, _SOLUTION)

    solver.run_solve(artifact_id)

    part_id = int(_problems(db, artifact_id)[0]["id"])
    steps = _children(db, part_id, artifacts.STEP)
    answers = _children(db, part_id, artifacts.ANSWER)
    assert [step["label"] for step in steps] == ["Set up the integral", "Evaluate"]
    assert [step["content"] for step in steps] == ["Apply the definition.", "Integrate by parts."]
    assert [answer["content"] for answer in answers] == ["1/s^2"]
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.READY


def test_a_cited_step_carries_its_source_and_an_uncited_one_carries_none(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
    retrieved: RetrievedChunk,
) -> None:
    # Grounding is provenance, not a score: a step that says it used retrieved material
    # gets a source line, and one that does not gets nothing at all.
    artifact_id = _set(db, class_id, [_STATEMENT])
    fake_solver(monkeypatch, _SOLUTION, chunks=[retrieved])

    solver.run_solve(artifact_id)

    steps = _children(db, int(_problems(db, artifact_id)[0]["id"]), artifacts.STEP)
    grounded = artifacts.list_provenance(db, int(steps[0]["id"]))
    assert [entry["chunk_id"] for entry in grounded] == [retrieved.chunk_id]
    assert [entry["page_number"] for entry in grounded] == [12]
    assert [entry["filename"] for entry in grounded] == ["lecture3.pdf"]
    assert artifacts.list_provenance(db, int(steps[1]["id"])) == []


def test_a_step_can_cite_a_reference_solution(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
    retrieved: RetrievedChunk,
) -> None:
    """A reference the student attached has to be citable, or it cannot be reported.

    References used to enter the prompt unnumbered, so a step that followed the answer key
    had no way to name it and the screen said every step was ungrounded. They now continue
    the retrieved chunks' numbering: one chunk means the reference is [2].
    """
    problem_document = _document(db, class_id)
    reference = _document(db, class_id, "hw4_solutions.pdf")
    created = artifacts.create_artifact(
        db,
        class_id,
        "Problem set 4",
        [SourceSpec(problem_document), SourceSpec(reference, artifacts.REFERENCE_SOLUTIONS)],
    )
    artifact_id = int(created["id"])
    artifacts.create_part(db, artifact_id, artifacts.PROBLEM, 0, label="Problem 1", content="Go.")
    artifacts.set_artifact_state(db, artifact_id, artifacts.AWAITING_REVIEW)
    fake_solver(
        monkeypatch,
        {
            "steps": [
                {"title": "Follow the key", "content": "As in the solutions.", "sources": [2]},
            ],
            "answer": "1/s^2",
        },
        chunks=[retrieved],
    )

    solver.run_solve(artifact_id)

    steps = _children(db, int(_problems(db, artifact_id)[0]["id"]), artifacts.STEP)
    entries = artifacts.list_provenance(db, int(steps[0]["id"]))
    assert [entry["filename"] for entry in entries] == ["hw4_solutions.pdf"]
    # No chunk and no page: a reference enters the prompt whole rather than as an indexed
    # passage, and a page number invented for it would be a citation nobody can follow.
    assert [entry["chunk_id"] for entry in entries] == [None]
    assert [entry["page_number"] for entry in entries] == [None]


def test_a_citation_written_into_the_prose_becomes_a_source(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
    retrieved: RetrievedChunk,
) -> None:
    """`[6]` on screen is a footnote marker pointing at a list the student cannot see.

    The prompt says the numbers belong in `sources` and nowhere else. Models write them
    into the step text anyway, so they are lifted rather than deleted: the citation is
    real, and it belongs where the provenance chip can turn it into a filename.
    """
    artifact_id = _set(db, class_id, [_STATEMENT])
    fake_solver(
        monkeypatch,
        {
            "steps": [
                {
                    "title": "Apply the definition",
                    "content": "By the definition [1], the sample $x[1]$ is unchanged.",
                    "sources": [],
                }
            ],
            "answer": "1/s^2",
        },
        chunks=[retrieved],
    )

    solver.run_solve(artifact_id)

    steps = _children(db, int(_problems(db, artifact_id)[0]["id"]), artifacts.STEP)
    # The marker is gone and the space it sat in went with it. A bracketed index after an
    # identifier is notation, not a citation, and this is a signals course.
    assert steps[0]["content"] == "By the definition, the sample $x[1]$ is unchanged."
    entries = artifacts.list_provenance(db, int(steps[0]["id"]))
    assert [entry["chunk_id"] for entry in entries] == [retrieved.chunk_id]


def test_a_citation_outside_the_context_block_is_dropped(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
    retrieved: RetrievedChunk,
) -> None:
    # One chunk was retrieved, so [4] resolves to nothing. A source line pointing at
    # nothing would render as grounding the step does not have.
    artifact_id = _set(db, class_id, [_STATEMENT])
    fake_solver(
        monkeypatch,
        {"steps": [{"title": "", "content": "Working.", "sources": [4]}], "answer": "1"},
        chunks=[retrieved],
    )

    solver.run_solve(artifact_id)

    steps = _children(db, int(_problems(db, artifact_id)[0]["id"]), artifacts.STEP)
    assert artifacts.list_provenance(db, int(steps[0]["id"])) == []


def test_each_problem_is_committed_as_it_completes(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole reason the job is shaped this way: work survives a closed laptop."""
    artifact_id = _set(db, class_id, ["First.", "Second."])
    seen: list[int] = []

    def observe() -> None:
        # A second connection, so what it reads is what is actually committed rather than
        # what the worker's transaction happens to be holding.
        from backend.storage.database import connect

        other = connect()
        try:
            seen.append(
                sum(
                    1
                    for part in artifacts.list_parts(other, artifact_id)
                    if part["kind"] == artifacts.STEP
                )
            )
        finally:
            other.close()

    fake_solver(monkeypatch, _SOLUTION, on_reply=observe)
    solver.run_solve(artifact_id)

    # Before the first reply nothing is written; before the second, problem one is
    # already on disk. Buffering to the end would make both readings zero.
    assert seen == [0, 2]


def test_a_failed_problem_does_not_fail_the_artifact(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.core.errors import UpstreamError

    artifact_id = _set(db, class_id, ["First.", "Second."])
    fake_solver(monkeypatch, [UpstreamError("The tutor endpoint is not reachable."), _SOLUTION])

    solver.run_solve(artifact_id)

    problems = _problems(db, artifact_id)
    assert problems[0]["status"] == artifacts.PART_FAILED
    assert problems[0]["error_message"] == "The tutor endpoint is not reachable."
    assert problems[1]["status"] == artifacts.PART_COMPLETE
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.READY


def test_a_failed_problem_is_never_reported_as_checked(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.core.errors import UpstreamError

    artifact_id = _set(db, class_id, ["First."])
    fake_solver(monkeypatch, UpstreamError("Down."), tool_support=True)

    solver.run_solve(artifact_id)

    problem = _problems(db, artifact_id)[0]
    assert problem["verdict"] == artifacts.UNCHECKED


def test_cancelling_mid_run_keeps_what_is_finished(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id = _set(db, class_id, ["First.", "Second."])

    def stop() -> None:
        from backend.storage.database import connect

        other = connect()
        try:
            artifacts.set_artifact_state(other, artifact_id, artifacts.CANCELLED)
        finally:
            other.close()

    # Cancelled while the first problem is in flight, so the second is never started.
    fake_solver(monkeypatch, _SOLUTION, on_reply=stop)
    solver.run_solve(artifact_id)

    problems = _problems(db, artifact_id)
    assert problems[0]["status"] == artifacts.PART_COMPLETE
    assert problems[1]["status"] == artifacts.PART_PENDING
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.CANCELLED


def test_starting_again_skips_problems_that_are_already_solved(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Solve the rest` is the same call as `Solve`, and it does not redo finished work."""
    artifact_id = _set(db, class_id, ["First.", "Second."])
    first = int(_problems(db, artifact_id)[0]["id"])
    artifacts.set_part_status(db, first, artifacts.PART_COMPLETE)

    sent = fake_solver(monkeypatch, _SOLUTION)
    solver.run_solve(artifact_id)

    assert len(sent) == 1
    assert "Second." in sent[0][1]["content"]
    assert artifacts.get_artifact(db, artifact_id)["problems_done"] == 2


def test_solving_refuses_to_send_statements_to_an_unacknowledged_remote_endpoint(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Inference Posture rule, on the path that cannot degrade around it.

    Segmentation falls back to the chunker when this is blocked. Solving cannot: without
    the statement there is nothing to solve, so the run fails and says why.
    """
    db.execute(
        "update settings set endpoint_url = 'https://api.example.com/v1', remote_ack = 0 "
        "where id = 1"
    )
    db.commit()
    artifact_id = _set(db, class_id, [_STATEMENT])
    sent = fake_solver(monkeypatch, _SOLUTION)

    solver.run_solve(artifact_id)

    assert sent == []
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert "Settings" in str(artifact["error_message"])


def test_reference_solutions_reach_the_prompt_labelled_as_examples(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    problem_document = _document(db, class_id)
    reference = _document(db, class_id, "last_term_solutions.pdf")
    created = artifacts.create_artifact(
        db,
        class_id,
        "Problem set 4",
        [
            SourceSpec(problem_document),
            SourceSpec(reference, artifacts.REFERENCE_SOLUTIONS),
        ],
    )
    artifact_id = int(created["id"])
    artifacts.create_part(db, artifact_id, artifacts.PROBLEM, 0, label="Problem 1", content="Go.")
    artifacts.set_artifact_state(db, artifact_id, artifacts.AWAITING_REVIEW)

    sent = fake_solver(monkeypatch, _SOLUTION)
    solver.run_solve(artifact_id)

    # Collapsed: the prompt is hard-wrapped and which words share a line is not a contract.
    prompt = re.sub(r"\s+", " ", sent[0][1]["content"])
    assert "last_term_solutions.pdf" in prompt
    assert "Reference text." in prompt
    # The heading has to distinguish the two cases the picker now allows. Solutions to an
    # earlier set are a method to follow and not content to copy. Solutions to the set
    # being solved are the authority on the answer, and a student who deliberately
    # attached them is owed a solve that reads them rather than one told to look away.
    assert "take the method and not the content" in prompt
    assert "it is the authority on the answer" in prompt


def test_a_prose_reply_becomes_one_step_rather_than_a_failure(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id = _set(db, class_id, [_STATEMENT])
    fake_solver(monkeypatch, "The transform is $1/s^2$ by the definition.")

    solver.run_solve(artifact_id)

    part_id = int(_problems(db, artifact_id)[0]["id"])
    steps = _children(db, part_id, artifacts.STEP)
    assert len(steps) == 1
    assert steps[0]["content"] == "The transform is $1/s^2$ by the definition."
    assert _problems(db, artifact_id)[0]["status"] == artifacts.PART_COMPLETE


def test_an_empty_reply_fails_only_that_problem(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id = _set(db, class_id, [_STATEMENT])
    fake_solver(monkeypatch, "   ")

    solver.run_solve(artifact_id)

    assert _problems(db, artifact_id)[0]["status"] == artifacts.PART_FAILED
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.READY


def test_a_set_with_no_problems_is_refused_rather_than_left_running(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id = _document(db, class_id)
    created = artifacts.create_artifact(db, class_id, "Empty", [SourceSpec(document_id)])
    artifact_id = int(created["id"])
    artifacts.set_artifact_state(db, artifact_id, artifacts.AWAITING_REVIEW)
    fake_solver(monkeypatch, _SOLUTION)

    solver.run_solve(artifact_id)

    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.FAILED


def test_re_solving_rewrites_a_step_in_place_so_its_history_survives(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-solved step keeps its id and its past, which is what History exists to show."""
    artifact_id = _set(db, class_id, [_STATEMENT])
    fake_solver(monkeypatch, _SOLUTION)
    solver.run_solve(artifact_id)

    part_id = int(_problems(db, artifact_id)[0]["id"])
    first_step = int(_children(db, part_id, artifacts.STEP)[0]["id"])

    fake_solver(
        monkeypatch,
        {"steps": [{"title": "Different", "content": "A better first step."}], "answer": "1/s^2"},
    )
    solver.run_regeneration(artifact_id, part_id, "Step 1 uses the wrong definition.")

    steps = _children(db, part_id, artifacts.STEP)
    assert [step["id"] for step in steps] == [first_step]
    revisions = artifacts.list_revisions(db, first_step)
    assert [revision["content"] for revision in revisions] == [
        "A better first step.",
        "Apply the definition.",
    ]
    assert revisions[0]["note"] == "Step 1 uses the wrong definition."
    assert revisions[0]["origin"] == artifacts.REGENERATED


def test_a_correction_reaches_the_model_as_the_last_thing_it_reads(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id = _set(db, class_id, [_STATEMENT])
    fake_solver(monkeypatch, _SOLUTION)
    solver.run_solve(artifact_id)
    part_id = int(_problems(db, artifact_id)[0]["id"])

    sent = fake_solver(monkeypatch, _SOLUTION)
    solver.run_regeneration(artifact_id, part_id, "You dropped a factor of two.")

    prompt = sent[0][1]["content"]
    assert "You dropped a factor of two." in prompt
    assert prompt.rstrip().endswith("You dropped a factor of two.")


def test_a_regeneration_that_fails_leaves_the_previous_solution_in_place(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chat retry rule, applied here: a failed retry costs the student nothing."""
    from backend.core.errors import UpstreamError

    artifact_id = _set(db, class_id, [_STATEMENT])
    fake_solver(monkeypatch, _SOLUTION)
    solver.run_solve(artifact_id)
    part_id = int(_problems(db, artifact_id)[0]["id"])

    fake_solver(monkeypatch, UpstreamError("Down."))
    solver.run_regeneration(artifact_id, part_id, "")

    steps = _children(db, part_id, artifacts.STEP)
    assert [step["content"] for step in steps] == ["Apply the definition.", "Integrate by parts."]
    assert _problems(db, artifact_id)[0]["status"] == artifacts.PART_FAILED


def test_a_shorter_re_solve_drops_the_steps_it_no_longer_has(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id = _set(db, class_id, [_STATEMENT])
    fake_solver(monkeypatch, _SOLUTION)
    solver.run_solve(artifact_id)
    part_id = int(_problems(db, artifact_id)[0]["id"])

    fake_solver(monkeypatch, {"steps": [{"title": "Only", "content": "One step."}], "answer": ""})
    solver.run_regeneration(artifact_id, part_id, "")

    assert [step["content"] for step in _children(db, part_id, artifacts.STEP)] == ["One step."]
    # An answer this run did not reach must not keep the previous one, which would read as
    # this run's result.
    assert _children(db, part_id, artifacts.ANSWER) == []


def test_an_interrupted_part_is_pending_again_rather_than_failed(
    db: sqlite3.Connection, class_id: int
) -> None:
    artifact_id = _set(db, class_id, [_STATEMENT])
    part_id = int(_problems(db, artifact_id)[0]["id"])
    artifacts.set_artifact_state(db, artifact_id, artifacts.SOLVING)
    artifacts.set_part_status(db, part_id, artifacts.PART_SOLVING)

    solver.reconcile_interrupted(db)

    # Nothing is wrong with the problem: it simply never ran, and the retry should pick it
    # up as unsolved work rather than as something that failed.
    assert artifacts.get_part(db, part_id)["status"] == artifacts.PART_PENDING
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.FAILED


def test_a_waiting_set_survives_a_restart_untouched(db: sqlite3.Connection, class_id: int) -> None:
    artifact_id = _set(db, class_id, [_STATEMENT])

    solver.reconcile_interrupted(db)

    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.AWAITING_REVIEW


# --- Verification -----------------------------------------------------------------


def _loop(
    content: str, calls: tuple[tools.RecordedCall, ...] = (), stopped: str = tools.COMPLETED
) -> tools.ToolLoopResult:
    return tools.ToolLoopResult(content=content, calls=calls, stopped=stopped, detail="Gave up.")


def _call(ok: bool = True) -> tools.RecordedCall:
    return tools.RecordedCall(
        name="cas_integrate",
        arguments={"expression": "t"},
        raw_arguments='{"expression":"t"}',
        ok=ok,
        result={"ok": ok, "value": "t**2/2"},
    )


def test_agreement_without_a_single_tool_call_is_not_a_pass() -> None:
    """The rule the whole module exists for: a model ratifying itself is not a check."""
    outcome = verification.judge(_loop('{"verdict": "agrees", "detail": "Looks right."}'))

    assert outcome.verdict == artifacts.UNCHECKABLE


def test_agreement_backed_by_a_tool_call_is_verified() -> None:
    outcome = verification.judge(
        _loop('{"verdict": "agrees", "detail": "The integral matches."}', (_call(),))
    )

    assert outcome.verdict == artifacts.VERIFIED
    assert outcome.detail == "The integral matches."


def test_a_disagreement_is_refuted_and_names_what_disagreed() -> None:
    outcome = verification.judge(
        _loop(
            '{"verdict": "disagrees", "detail": "Step 3 integrates to t^2/2, not t^2."}',
            (_call(),),
        )
    )

    assert outcome.verdict == artifacts.REFUTED
    assert "t^2/2" in outcome.detail


@pytest.mark.parametrize(
    "stopped", [tools.DEPTH, tools.TIMEOUT, tools.NO_TOOL_SUPPORT, tools.UPSTREAM_FAILED]
)
def test_every_incomplete_stop_is_unchecked_with_a_reason(stopped: str) -> None:
    outcome = verification.judge(_loop("", (_call(),), stopped=stopped))

    assert outcome.verdict == artifacts.UNCHECKED
    assert outcome.detail
    # The calls that did run are still shown: partial work is worth seeing, it is just not
    # worth reading as an answer.
    assert len(outcome.checks) == 1


def test_a_reply_nobody_can_read_is_unchecked_rather_than_agreement() -> None:
    outcome = verification.judge(_loop("I ran some checks and it is fine, mostly.", (_call(),)))

    assert outcome.verdict == artifacts.UNCHECKED


def test_a_disagreement_stated_in_prose_is_still_read() -> None:
    # Reading agreement out of prose would let a reply that checked nothing become a pass.
    # Reading disagreement out of it only ever costs a re-derive.
    outcome = verification.judge(_loop("The verdict is: disagrees, step 2 is wrong.", (_call(),)))

    assert outcome.verdict == artifacts.REFUTED


def test_an_endpoint_without_tool_support_solves_and_reports_honestly(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id = _set(db, class_id, [_STATEMENT])
    fake_solver(monkeypatch, _SOLUTION, tool_support=False)

    solver.run_solve(artifact_id)

    problem = _problems(db, artifact_id)[0]
    assert problem["status"] == artifacts.PART_COMPLETE
    assert problem["verdict"] == artifacts.UNCHECKED
    assert "cannot run the checks" in str(problem["verdict_detail"])
    assert artifacts.list_checks(db, int(problem["id"])) == []


def test_a_refuted_problem_is_re_derived_once_and_no_more(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running until it passes would be a problem nobody checked."""
    artifact_id = _set(db, class_id, [_STATEMENT])
    sent = fake_solver(monkeypatch, _SOLUTION, tool_support=True)

    verdicts = iter(
        [
            verification.VerificationOutcome(
                artifacts.REFUTED,
                "Step 2 is wrong.",
                (),
            ),
            verification.VerificationOutcome(artifacts.REFUTED, "Still wrong.", ()),
        ]
    )
    monkeypatch.setattr(solver.verification, "verify", lambda *a, **k: next(verdicts))

    solver.run_solve(artifact_id)

    # One solve, one re-derive. A third would mean the loop is fishing for a pass.
    assert len(sent) == 2
    problem = _problems(db, artifact_id)[0]
    assert problem["verdict"] == artifacts.REFUTED
    assert problem["verdict_detail"] == "Still wrong."


def test_the_tool_calls_behind_a_verdict_are_stored(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_id = _set(db, class_id, [_STATEMENT])
    fake_solver(monkeypatch, _SOLUTION, tool_support=True)
    monkeypatch.setattr(
        solver.verification,
        "verify",
        lambda *a, **k: verification.judge(_loop('{"verdict": "agrees"}', (_call(),))),
    )

    solver.run_solve(artifact_id)

    problem = _problems(db, artifact_id)[0]
    checks = artifacts.list_checks(db, int(problem["id"]))
    assert [check["tool"] for check in checks] == ["cas_integrate"]
    assert json.loads(str(checks[0]["result"]))["value"] == "t**2/2"
    assert problem["verdict"] == artifacts.VERIFIED
