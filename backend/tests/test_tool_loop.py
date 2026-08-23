"""Tool-loop contracts, driven through a stubbed transport.

The contract that matters most is the last one in this file: a loop that was cut off
never reports itself as complete. A caller reads `stopped` to decide whether a solution
was checked, and a loop that gave up quietly would be read as agreement.
"""

import asyncio
import json

import httpx
import pytest

from backend.core.errors import ToolsUnsupportedError
from backend.llm import client, tools
from backend.rag.tokens import estimate_tokens
from backend.tools.result import ToolResult, success

_ENDPOINT = "http://127.0.0.1:8080/v1"
_MESSAGES: list[dict[str, object]] = [{"role": "user", "content": "Check this."}]


def _tool_call(name: str, arguments: object, call_id: str = "call_1") -> dict[str, object]:
    """One tool call in the shape a server sends it."""
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        },
    }


def _reply(
    content: str = "",
    tool_calls: list[dict[str, object]] | None = None,
    finish_reason: str | None = None,
) -> dict[str, object]:
    """One chat-completions response body.

    `finish_reason` defaults to omitted (the server said nothing, read as a normal stop);
    pass `"length"` to model an endpoint cutting the reply off at the output-token ceiling.
    """
    message: dict[str, object] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    choice: dict[str, object] = {"message": message}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {"choices": [choice]}


def _scripted(*responses: dict[str, object]) -> tuple[httpx.MockTransport, list[dict[str, object]]]:
    """A transport that answers with each response in turn, recording what it was sent.

    The last response repeats, so a test that only cares about the first turn does not
    have to spell out an ending.
    """
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        index = min(len(sent) - 1, len(responses) - 1)
        return httpx.Response(200, json=responses[index])

    return httpx.MockTransport(handler), sent


async def _run(transport: httpx.MockTransport, **kwargs: object) -> tools.ToolLoopResult:
    """Run the loop against a stubbed transport."""
    return await tools.run_tool_loop(
        _ENDPOINT, None, "local-model", _MESSAGES, transport=transport, **kwargs
    )


async def test_a_turn_with_no_tool_calls_completes_immediately() -> None:
    transport, sent = _scripted(_reply(content="Looks right."))

    result = await _run(transport)

    assert result.stopped == tools.COMPLETED
    assert result.complete is True
    assert result.content == "Looks right."
    assert result.calls == ()
    # Tools are offered on every turn, so the model can reach for one at any point.
    assert [tool["function"]["name"] for tool in sent[0]["tools"]] == list(tools.REGISTRY)


async def test_a_tool_call_runs_and_its_result_is_replayed_to_the_model() -> None:
    transport, sent = _scripted(
        _reply(tool_calls=[_tool_call("cas_evaluate", {"expression": "2 + 2"})]),
        _reply(content="Confirmed."),
    )

    result = await _run(transport)

    assert result.stopped == tools.COMPLETED
    assert result.content == "Confirmed."
    assert [call.name for call in result.calls] == ["cas_evaluate"]
    assert result.calls[0].ok is True
    assert result.calls[0].result["simplified"] == "4"

    # The second request has to carry the assistant's own turn and the tool's answer, or
    # the model has no idea what came back.
    replayed = sent[1]["messages"]
    assert replayed[-2]["role"] == "assistant"
    assert replayed[-1]["role"] == "tool"
    assert replayed[-1]["tool_call_id"] == "call_1"
    assert json.loads(replayed[-1]["content"])["simplified"] == "4"


async def test_several_calls_in_one_turn_are_one_round() -> None:
    transport, _ = _scripted(
        _reply(
            tool_calls=[
                _tool_call("cas_evaluate", {"expression": "1 + 1"}, "a"),
                _tool_call("cas_differentiate", {"expression": "x**2", "variable": "x"}, "b"),
            ]
        ),
        _reply(content="Both check out."),
    )

    result = await _run(transport, max_depth=2)

    assert result.stopped == tools.COMPLETED
    assert [call.name for call in result.calls] == ["cas_evaluate", "cas_differentiate"]


async def test_a_call_to_a_tool_outside_the_registry_is_refused() -> None:
    transport, _ = _scripted(
        _reply(tool_calls=[_tool_call("read_file", {"path": "/etc/passwd"})]),
        _reply(content="Understood."),
    )

    result = await _run(transport)

    # The registry is the whole allowlist. Nothing outside it is reachable.
    assert result.calls[0].ok is False
    assert "read_file" in str(result.calls[0].result["error"])


async def test_unreadable_arguments_come_back_as_a_result_the_model_can_fix() -> None:
    transport, _ = _scripted(
        _reply(tool_calls=[_tool_call("cas_evaluate", "{not json")]),
        _reply(content="Retrying."),
    )

    result = await _run(transport)

    assert result.calls[0].ok is False
    assert result.calls[0].result["error"] == tools._BAD_ARGUMENTS
    # Kept so the transcript can show what was actually sent rather than just "invalid".
    assert result.calls[0].raw_arguments == "{not json"
    assert result.stopped == tools.COMPLETED


async def test_a_missing_required_argument_is_named() -> None:
    transport, _ = _scripted(
        _reply(tool_calls=[_tool_call("cas_integrate", {"expression": "x"})]),
        _reply(content="Right."),
    )

    result = await _run(transport)

    assert result.calls[0].ok is False
    assert "variable" in str(result.calls[0].result["error"])


async def test_an_argument_the_schema_does_not_declare_is_dropped() -> None:
    transport, _ = _scripted(
        _reply(
            tool_calls=[
                _tool_call("cas_evaluate", {"expression": "2 + 2", "precision": 12, "cwd": "/"})
            ]
        ),
        _reply(content="Fine."),
    )

    result = await _run(transport)

    # A model inventing a keyword must not become a TypeError it cannot see or fix.
    assert result.calls[0].ok is True
    assert result.calls[0].arguments == {"expression": "2 + 2"}


async def test_a_handler_that_raises_does_not_take_the_loop_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(**_: object) -> ToolResult:
        raise RuntimeError("boom")

    monkeypatch.setitem(
        tools.REGISTRY,
        "cas_evaluate",
        tools.ToolDefinition(
            name="cas_evaluate",
            description="",
            parameters={"type": "object", "properties": {"expression": {"type": "string"}}},
            handler=explode,
        ),
    )
    transport, _ = _scripted(
        _reply(tool_calls=[_tool_call("cas_evaluate", {"expression": "x"})]),
        _reply(content="Moving on."),
    )

    result = await _run(transport)

    assert result.calls[0].ok is False
    assert result.stopped == tools.COMPLETED


async def test_the_depth_ceiling_stops_a_model_that_never_finishes() -> None:
    # Always asks for another call, never answers.
    transport, _ = _scripted(_reply(tool_calls=[_tool_call("cas_evaluate", {"expression": "1"})]))

    result = await _run(transport, max_depth=3)

    assert result.stopped == tools.DEPTH
    assert result.complete is False
    assert len(result.calls) == 3
    assert "3 rounds" in result.detail


async def test_the_wall_clock_stops_a_loop_that_hangs() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, json=_reply(content="too late"))

    result = await _run(httpx.MockTransport(handler), timeout_seconds=0.2)

    # The ceiling holds while a single call is hanging, not only between rounds.
    assert result.stopped == tools.TIMEOUT
    assert result.detail == tools._TIMEOUT_DETAIL


async def test_calls_made_before_a_cut_are_still_reported() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if any(message["role"] == "tool" for message in payload["messages"]):
            await asyncio.sleep(30)
            return httpx.Response(200, json=_reply(content="too late"))
        return httpx.Response(
            200, json=_reply(tool_calls=[_tool_call("cas_evaluate", {"expression": "2 + 2"})])
        )

    result = await _run(httpx.MockTransport(handler), timeout_seconds=2.0)

    # Partial work is worth showing. It is just not worth trusting as an answer, which is
    # what `stopped` says.
    assert result.stopped == tools.TIMEOUT
    assert [call.name for call in result.calls] == ["cas_evaluate"]
    assert result.content == ""


async def test_an_endpoint_that_rejects_tools_degrades_rather_than_failing() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(400, json={"error": "no tools"}))

    result = await _run(transport)

    assert result.stopped == tools.NO_TOOL_SUPPORT
    assert result.complete is False


async def test_an_unreachable_endpoint_is_a_check_that_did_not_run() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    result = await _run(httpx.MockTransport(refuse))

    # Not raised: a verification pass that cannot reach the model is a check that did not
    # run, not an error the student needs to see.
    assert result.stopped == tools.UPSTREAM_FAILED
    assert result.detail


@pytest.mark.parametrize("reason", tools.INCOMPLETE_REASONS)
def test_no_incomplete_reason_reads_as_complete(reason: str) -> None:
    assert tools.ToolLoopResult(content="anything", stopped=reason).complete is False


async def test_ordinary_chat_never_carries_tool_definitions() -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')

    transport = httpx.MockTransport(handler)
    async for _ in client.stream_chat(
        _ENDPOINT, None, "local-model", [{"role": "user", "content": "hi"}], transport=transport
    ):
        pass

    # An endpoint that cannot accept tools still has to carry the whole conversation.
    assert "tools" not in sent[0]


async def test_probe_reports_three_outcomes_apart() -> None:
    calling = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json=_reply(tool_calls=[_tool_call("add", {"a": 2, "b": 3})])
        )
    )
    ignoring = httpx.MockTransport(lambda request: httpx.Response(200, json=_reply(content="5")))
    refusing = httpx.MockTransport(lambda request: httpx.Response(400, json={"error": "no"}))

    supported = await client.probe_tool_support(_ENDPOINT, None, "m", transport=calling)
    ignored = await client.probe_tool_support(_ENDPOINT, None, "m", transport=ignoring)
    refused = await client.probe_tool_support(_ENDPOINT, None, "m", transport=refusing)

    assert supported.ok is True
    # An endpoint that answers in prose is reported as unable, with a message saying so.
    # Guessing here would claim verification on an endpoint that never runs it.
    assert ignored.ok is False
    assert ignored.message != refused.message
    assert refused.ok is False


async def test_complete_with_tools_raises_only_outside_the_loop() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(400, json={"error": "no"}))

    with pytest.raises(ToolsUnsupportedError):
        await client.complete_with_tools(
            _ENDPOINT, None, "m", _MESSAGES, tools.tool_schemas(), transport=transport
        )


def test_every_registered_tool_declares_a_usable_schema() -> None:
    for name, definition in tools.REGISTRY.items():
        schema = definition.schema()
        assert schema["function"]["name"] == name
        assert definition.description
        # `optional` is Lyra's own marker for building the required list. It is not JSON
        # Schema, and a server given it would be within its rights to reject the request.
        for argument in definition.properties.values():
            assert "optional" not in argument
        for required in definition.required:
            assert required in definition.properties


def test_the_registry_holds_only_pure_computation() -> None:
    # Phase 2 tools compute and nothing else. Anything that reads, writes, or opens a
    # socket belongs to Phase 4 and its threat model, not here.
    assert set(tools.REGISTRY) == {
        "cas_evaluate",
        "cas_solve",
        "cas_integrate",
        "cas_differentiate",
        "cas_linalg",
        "check_units",
    }


def test_a_tool_result_never_reports_a_value_alongside_a_failure() -> None:
    assert success(answer="4").as_payload() == {"ok": True, "answer": "4"}
    payload = ToolResult(ok=False, error="nope").as_payload()
    assert payload == {"ok": False, "error": "nope"}


async def test_a_result_that_cannot_be_serialized_costs_one_check_and_not_the_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop writes every result out twice, and neither write is inside a guard.

    A payload `json.dumps` refuses therefore did not fail the tool that produced it: the
    raise travelled out of the loop, past the only handler above it, and was reported as
    the whole pass having been impossible to run, taking every check that had already
    succeeded with it. Proving the result here keeps a bad one to its own row.
    """

    def unserializable(**_: object) -> ToolResult:
        return success(value=object())

    monkeypatch.setitem(
        tools.REGISTRY,
        "cas_evaluate",
        tools.ToolDefinition(
            name="cas_evaluate",
            description="",
            parameters={"type": "object", "properties": {"expression": {"type": "string"}}},
            handler=unserializable,
        ),
    )
    transport, _ = _scripted(
        _reply(
            tool_calls=[
                _tool_call("cas_evaluate", {"expression": "x"}),
                _tool_call("check_units", {"expression": "9.8 m/s^2", "expected": "m/s^2"}),
            ]
        ),
        _reply(content='{"verdict": "agrees", "detail": "Checked."}'),
    )

    result = await _run(transport)

    assert result.stopped == tools.COMPLETED
    assert result.calls[0].ok is False
    # The check beside it ran, and a pass that reaches the model is a pass that can be read.
    assert result.calls[1].ok is True
    json.dumps([call.result for call in result.calls])


# ---------------------------------------------------------------------------------
# A caller's own registry, and the narration callback. The writer runs the same loop
# with different tools; the contract is that the passed registry is the whole
# allowlist and that narration can never cost the pass.
# ---------------------------------------------------------------------------------


def _echo_registry() -> dict[str, tools.ToolDefinition]:
    """One tool that reports what it was asked."""

    def echo(text: str) -> ToolResult:
        return success(echoed=text)

    return {
        "echo": tools.ToolDefinition(
            name="echo",
            description="Echo the text back.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            handler=echo,
        )
    }


async def test_a_passed_registry_is_what_the_model_is_offered_and_dispatched_to() -> None:
    transport, sent = _scripted(
        _reply(tool_calls=[_tool_call("echo", {"text": "hello"})]),
        _reply(content="Done."),
    )

    result = await tools.run_tool_loop(
        _ENDPOINT, None, "local-model", _MESSAGES, registry=_echo_registry(), transport=transport
    )

    assert result.stopped == tools.COMPLETED
    assert [tool["function"]["name"] for tool in sent[0]["tools"]] == ["echo"]
    assert result.calls[0].ok is True
    assert result.calls[0].result == {"ok": True, "echoed": "hello"}


async def test_a_passed_registry_is_the_whole_allowlist() -> None:
    # cas_evaluate exists in the default registry, but this loop was not given it.
    transport, _ = _scripted(
        _reply(tool_calls=[_tool_call("cas_evaluate", {"expression": "2 + 2"})]),
        _reply(content="Done."),
    )

    result = await tools.run_tool_loop(
        _ENDPOINT, None, "local-model", _MESSAGES, registry=_echo_registry(), transport=transport
    )

    assert result.calls[0].ok is False
    assert "no tool called" in str(result.calls[0].result["error"])


async def test_on_call_narrates_each_call_in_order() -> None:
    transport, _ = _scripted(
        _reply(
            tool_calls=[
                _tool_call("echo", {"text": "one"}, call_id="call_1"),
                _tool_call("echo", {"text": "two"}, call_id="call_2"),
            ]
        ),
        _reply(content="Done."),
    )
    narrated: list[tools.RecordedCall] = []

    result = await tools.run_tool_loop(
        _ENDPOINT,
        None,
        "local-model",
        _MESSAGES,
        registry=_echo_registry(),
        on_call=narrated.append,
        transport=transport,
    )

    assert result.stopped == tools.COMPLETED
    assert [call.arguments["text"] for call in narrated] == ["one", "two"]
    assert narrated == list(result.calls)


async def test_a_raising_on_call_is_ignored_and_the_pass_completes() -> None:
    transport, _ = _scripted(
        _reply(tool_calls=[_tool_call("echo", {"text": "one"})]),
        _reply(content="Done."),
    )

    def broken(_: tools.RecordedCall) -> None:
        raise RuntimeError("narration bug")

    result = await tools.run_tool_loop(
        _ENDPOINT,
        None,
        "local-model",
        _MESSAGES,
        registry=_echo_registry(),
        on_call=broken,
        transport=transport,
    )

    assert result.stopped == tools.COMPLETED
    assert result.content == "Done."
    assert result.calls[0].ok is True


# --- The per-round context guard (PLA-290) -------------------------------------------
#
# An agent turn's first request is proved to fit by the route's preflight, but each round
# appends the model's tool calls and their results, so a later request can outgrow the
# window even though the first one fit. Given a `ContextBudget`, the loop re-measures the
# conversation before every request after the first and stops with `CONTEXT_OVERFLOW`
# rather than sending one the transcript has grown too large for.


def _big_result_registry(chars: int) -> dict[str, tools.ToolDefinition]:
    """One tool whose result is large enough to grow the transcript quickly."""

    def bulky(**_: object) -> ToolResult:
        return success(payload="z" * chars)

    return {
        "bulky": tools.ToolDefinition(
            name="bulky",
            description="Return a large payload.",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=bulky,
        )
    }


async def test_a_growing_transcript_is_stopped_before_it_overflows() -> None:
    # The model keeps asking for a tool that returns a large payload. The first request fits
    # and is sent; once the accumulated results push the next request past the ceiling, the
    # loop stops before sending it rather than letting the endpoint reject it.
    transport, sent = _scripted(_reply(tool_calls=[_tool_call("bulky", {})]))
    budget = tools.ContextBudget(context_window=4096, generation_reserve=1024, tool_tokens=50)

    result = await tools.run_tool_loop(
        _ENDPOINT,
        None,
        "local-model",
        _MESSAGES,
        registry=_big_result_registry(chars=8000),  # ~2000 tokens per tool result
        context_budget=budget,
        transport=transport,
    )

    assert result.stopped == tools.CONTEXT_OVERFLOW
    assert result.complete is False
    assert result.content == ""
    # Bounded and privacy-safe: it names no endpoint, argument, or transcript.
    assert result.detail == tools._OVERFLOW_DETAIL
    # At least one round ran (partial work is real), but the loop stopped short of the
    # depth ceiling - it was the context guard, not exhaustion, that ended it.
    assert 0 < len(result.calls) < tools.MAX_DEPTH
    # The overflowing request was never sent: the guard fires before `complete_with_tools`.
    assert len(sent) == len(result.calls)


async def test_the_first_request_is_never_refused_by_the_context_guard() -> None:
    # Round zero is the caller's preflight to prove; the guard does not re-check it, so a
    # budget too small for even the opening request still lets that request go (and here the
    # model simply answers). Re-checking round zero could only disagree with the preflight.
    transport, sent = _scripted(_reply(content="Answered on the first try."))
    budget = tools.ContextBudget(context_window=8, generation_reserve=4, tool_tokens=4)

    result = await tools.run_tool_loop(
        _ENDPOINT, None, "local-model", _MESSAGES, context_budget=budget, transport=transport
    )

    assert result.stopped == tools.COMPLETED
    assert result.content == "Answered on the first try."
    assert len(sent) == 1


async def test_without_a_context_budget_the_loop_does_not_guard() -> None:
    # The writer chat and the solver's verification pass bound their transcripts by depth and
    # wall clock, not by a window, so a loop given no budget behaves exactly as before.
    transport, _ = _scripted(
        _reply(tool_calls=[_tool_call("bulky", {})]),
        _reply(content="Done."),
    )

    result = await tools.run_tool_loop(
        _ENDPOINT,
        None,
        "local-model",
        _MESSAGES,
        registry=_big_result_registry(chars=40_000),
        transport=transport,
    )

    assert result.stopped == tools.COMPLETED
    assert result.content == "Done."


# --- The intra-round context guard (PLA-290 blocker 1) -------------------------------
#
# A model's single response can ask for several tools at once, and re-checking the budget
# only between rounds let the whole set execute even once the transcript could no longer be
# continued: the assistant tool-call payload, or an early tool result, could make the next
# request impossible while later tools still ran. The guard is re-evaluated at every growth
# boundary now - after the assistant turn is appended, and after each individual result -
# so no tool runs merely because it was part of the same response, and no request is sent
# once overflow is known.


def _counting_registry(chars: int, runs: list[str]) -> dict[str, tools.ToolDefinition]:
    """One tool that records each execution and returns a payload of a chosen size."""

    def work(tag: str = "") -> ToolResult:
        runs.append(tag)
        return success(payload="z" * chars)

    return {
        "work": tools.ToolDefinition(
            name="work",
            description="Do a unit of work.",
            parameters={
                "type": "object",
                "properties": {"tag": {"type": "string"}},
                "required": [],
            },
            handler=work,
        )
    }


async def test_several_tool_calls_in_one_response_all_run_when_they_fit() -> None:
    # The baseline the guard must not disturb: one assistant turn asking for three tools,
    # comfortably under the window, runs all three in order and continues.
    runs: list[str] = []
    transport, _sent = _scripted(
        _reply(
            tool_calls=[
                _tool_call("work", {"tag": "a"}, call_id="c1"),
                _tool_call("work", {"tag": "b"}, call_id="c2"),
                _tool_call("work", {"tag": "c"}, call_id="c3"),
            ]
        ),
        _reply(content="All three done."),
    )
    budget = tools.ContextBudget(context_window=8192, generation_reserve=1024, tool_tokens=50)

    result = await tools.run_tool_loop(
        _ENDPOINT,
        None,
        "local-model",
        _MESSAGES,
        registry=_counting_registry(chars=10, runs=runs),
        context_budget=budget,
        transport=transport,
    )

    assert result.stopped == tools.COMPLETED
    assert runs == ["a", "b", "c"]
    assert [call.arguments.get("tag") for call in result.calls] == ["a", "b", "c"]


async def test_an_oversized_assistant_tool_call_payload_runs_zero_tools() -> None:
    # The assistant's tool-call payload itself can be the append that makes continuation
    # impossible - a model can emit a huge arguments blob. The turn is stopped after that
    # payload re-enters the transcript and before a single requested tool is dispatched, so
    # no tool runs only to have its result discarded with a transcript that cannot be sent.
    runs: list[str] = []
    huge_arguments = {"tag": "q" * 8000}  # ~2000 tokens of arguments on the assistant turn
    transport, sent = _scripted(
        _reply(
            tool_calls=[
                _tool_call("work", huge_arguments, call_id="c1"),
                _tool_call("work", {"tag": "b"}, call_id="c2"),
            ]
        )
    )
    budget = tools.ContextBudget(context_window=2048, generation_reserve=512, tool_tokens=50)

    result = await tools.run_tool_loop(
        _ENDPOINT,
        None,
        "local-model",
        _MESSAGES,
        registry=_counting_registry(chars=10, runs=runs),
        context_budget=budget,
        transport=transport,
    )

    assert result.stopped == tools.CONTEXT_OVERFLOW
    assert result.content == ""
    # Not one of the requested tools ran, and none was recorded.
    assert runs == []
    assert result.calls == ()
    # The opening request was sent (the preflight's to prove); no request followed it.
    assert len(sent) == 1


async def test_a_first_result_that_overflows_stops_the_rest_of_the_same_response() -> None:
    # One assistant response asks for three tools. The first result is large enough to fill
    # the window; the loop stops the moment it is appended, so the second and third tools of
    # that same response never run - exactly the calls that ran, and no more, are recorded.
    runs: list[str] = []
    transport, sent = _scripted(
        _reply(
            tool_calls=[
                _tool_call("work", {"tag": "a"}, call_id="c1"),
                _tool_call("work", {"tag": "b"}, call_id="c2"),
                _tool_call("work", {"tag": "c"}, call_id="c3"),
            ]
        )
    )
    budget = tools.ContextBudget(context_window=4096, generation_reserve=1024, tool_tokens=50)

    result = await tools.run_tool_loop(
        _ENDPOINT,
        None,
        "local-model",
        _MESSAGES,
        registry=_counting_registry(chars=12000, runs=runs),  # ~3000 tokens per result
        context_budget=budget,
        transport=transport,
    )

    assert result.stopped == tools.CONTEXT_OVERFLOW
    # Only the first of the three requested tools ran; the later two were abandoned.
    assert runs == ["a"]
    assert [call.arguments.get("tag") for call in result.calls] == ["a"]
    # The transcript stopped there rather than sending a request missing tool results.
    assert len(sent) == 1


# --- The reserved output ceiling and truncation (PLA-290 blocker 1) ------------------
#
# A guarded loop reserves output room in its context arithmetic, so it must also tell the
# endpoint to cap the reply at that reserve - otherwise the reservation is a fiction the
# request never enforces. And a reply the endpoint cut off at that ceiling
# (`finish_reason: "length"`) is not a finished turn: its prose is a fragment, not an
# answer, and its tool calls may be half-written, so neither may be trusted.


async def test_a_guarded_loop_sends_the_generation_reserve_as_the_output_ceiling() -> None:
    # The reserve the budget holds back is the `max_tokens` the endpoint is told to stay
    # inside, on the first round and on every later round alike.
    transport, sent = _scripted(
        _reply(tool_calls=[_tool_call("cas_evaluate", {"expression": "2 + 2"})]),
        _reply(content="Confirmed."),
    )
    budget = tools.ContextBudget(context_window=8192, generation_reserve=1024, tool_tokens=50)

    result = await _run(transport, context_budget=budget)

    assert result.stopped == tools.COMPLETED
    # Two requests were sent (round zero, then the round that read the tool result); both
    # carried the reserve as their output ceiling.
    assert len(sent) == 2
    assert sent[0]["max_tokens"] == 1024
    assert sent[1]["max_tokens"] == 1024


async def test_an_unguarded_loop_leaves_the_output_ceiling_unset() -> None:
    # No budget, no reserve, no ceiling: the writer chat and the solver's verification pass
    # send exactly what they sent before this existed.
    transport, sent = _scripted(
        _reply(tool_calls=[_tool_call("cas_evaluate", {"expression": "2 + 2"})]),
        _reply(content="Confirmed."),
    )

    result = await _run(transport)

    assert result.stopped == tools.COMPLETED
    assert all("max_tokens" not in body for body in sent)


async def test_a_truncated_final_prose_reply_is_not_reported_as_completed() -> None:
    # The model answered in prose but the endpoint cut it off at the reserve. That fragment
    # must not be stored as a finished answer, so the loop settles on OUTPUT_LIMIT instead of
    # COMPLETED and returns no content to store.
    transport, _ = _scripted(
        _reply(content="The answer is four hundred and", finish_reason="length")
    )
    budget = tools.ContextBudget(context_window=8192, generation_reserve=1024, tool_tokens=50)

    result = await _run(transport, context_budget=budget)

    assert result.stopped == tools.OUTPUT_LIMIT
    assert result.complete is False
    assert result.content == ""
    # Bounded and privacy-safe: it names no endpoint, argument, or transcript.
    assert result.detail == tools._OUTPUT_LIMIT_DETAIL


async def test_a_truncated_tool_call_round_dispatches_no_tool() -> None:
    # The would-be tool-call round was cut off at the ceiling, so its arguments may be
    # half-written. Not one tool runs - the loop settles before dispatch rather than
    # speculating on an incomplete request.
    runs: list[str] = []
    transport, sent = _scripted(
        _reply(
            tool_calls=[_tool_call("work", {"tag": "a"})],
            content="",
            finish_reason="length",
        )
    )
    budget = tools.ContextBudget(context_window=8192, generation_reserve=1024, tool_tokens=50)

    result = await tools.run_tool_loop(
        _ENDPOINT,
        None,
        "local-model",
        _MESSAGES,
        registry=_counting_registry(chars=10, runs=runs),
        context_budget=budget,
        transport=transport,
    )

    assert result.stopped == tools.OUTPUT_LIMIT
    assert result.content == ""
    assert runs == []
    assert result.calls == ()
    # The opening request was sent; no request and no dispatch followed the truncation.
    assert len(sent) == 1


async def test_an_unguarded_loop_ignores_truncation_and_keeps_prior_behaviour() -> None:
    # Without a budget the loop imposes no ceiling and does not reinterpret a server's own
    # truncation: a prose reply flagged `length` still flows through as it did before, so the
    # writer/solver loops are untouched. (The endpoint here truncated of its own accord.)
    transport, _ = _scripted(_reply(content="Partial but usable.", finish_reason="length"))

    result = await _run(transport)

    assert result.stopped == tools.COMPLETED
    assert result.content == "Partial but usable."


async def test_a_normal_stop_still_completes_under_a_budget() -> None:
    # The control case: a reply with no `length` finish reason completes unchanged even with
    # a budget in force, so the truncation handling costs the happy path nothing.
    transport, _ = _scripted(_reply(content="All done.", finish_reason="stop"))
    budget = tools.ContextBudget(context_window=8192, generation_reserve=1024, tool_tokens=50)

    result = await _run(transport, context_budget=budget)

    assert result.stopped == tools.COMPLETED
    assert result.content == "All done."


# --- First-round context rejection vs genuine tools-unsupported (PLA-290 blocker 2) ---
#
# The client reads a 400 on a tools-carrying request as "no tool support" - the best
# reading for a genuine refusal. But an unknown endpoint tokenizer can reject a first
# request the local estimate admitted, and that 400 is a context-window complaint, not a
# capability verdict. The client classifies the body (and drops it), so a context 400
# settles as an upstream failure while a genuine tools-unsupported 400 still degrades.


async def test_a_first_round_context_rejection_is_an_upstream_failure_not_no_tool_support() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            400, json={"error": {"message": "the request exceeds the available context size"}}
        )
    )
    budget = tools.ContextBudget(context_window=8192, generation_reserve=1024, tool_tokens=50)

    result = await _run(transport, context_budget=budget)

    # Not NO_TOOL_SUPPORT: the endpoint plainly processed the tools field, it just could not
    # fit the prompt. The settings screen must not claim tools are unsupported over this.
    assert result.stopped == tools.UPSTREAM_FAILED
    assert result.stopped != tools.NO_TOOL_SUPPORT
    assert result.complete is False
    assert result.detail


async def test_a_first_round_genuine_tools_rejection_still_reports_no_tool_support() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            400, json={"error": {"message": "this model does not support the tools parameter"}}
        )
    )

    result = await _run(transport)

    assert result.stopped == tools.NO_TOOL_SUPPORT
    assert result.complete is False


async def test_a_context_rejection_body_never_reaches_the_result_or_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A compromised or noisy endpoint can pack its error body with private-looking text. The
    # client classifies which kind of failure it is and drops the prose, so the marker must
    # not surface in the result detail or in any log line.
    marker = "SECRET_/Users/student/thesis.pdf_sk-lyra-key"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            400,
            json={"error": {"message": f"n_ctx too small; {marker}"}},
        )
    )
    budget = tools.ContextBudget(context_window=8192, generation_reserve=1024, tool_tokens=50)

    with caplog.at_level("INFO"):
        result = await _run(transport, context_budget=budget)

    assert result.stopped == tools.UPSTREAM_FAILED
    assert marker not in result.detail
    log = "\n".join(record.getMessage() for record in caplog.records)
    assert marker not in log


async def test_a_mid_loop_400_remains_an_upstream_failure() -> None:
    # The endpoint answered a tool round, then rejected the next request. Whatever the body,
    # this is the transcript going bad mid-loop, never a capability verdict - a 400 after a
    # demonstrated tool call must not read as "no tool support".
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if any(message["role"] == "tool" for message in payload["messages"]):
            return httpx.Response(400, json={"error": {"message": "the context window is full"}})
        return httpx.Response(
            200, json=_reply(tool_calls=[_tool_call("cas_evaluate", {"expression": "2 + 2"})])
        )

    budget = tools.ContextBudget(context_window=8192, generation_reserve=1024, tool_tokens=50)
    result = await _run(httpx.MockTransport(handler), context_budget=budget)

    assert result.stopped == tools.UPSTREAM_FAILED
    assert result.stopped != tools.NO_TOOL_SUPPORT
    # The one tool round that ran before the rejection is still reported.
    assert [call.name for call in result.calls] == ["cas_evaluate"]


# --- Canonical wire-shape accounting (PLA-290 blocker 3) -----------------------------
#
# `message_tokens` measures the exact compact JSON the client places in the request, not a
# hand-picked subset of fields. An earlier version summed only content, tool_calls, and
# name, so role, tool_call_id, and the object/array/key/string framing went uncharged and
# accumulated across many messages and long ids into a real undercount. The preflight and
# the loop both route through this one path so the request one measures is the request the
# other measures.


def test_message_tokens_counts_role_and_framing_not_only_content() -> None:
    message = {"role": "user", "content": "hi"}
    # The framing (role, keys, quotes, braces) dwarfs a two-character body, so the whole
    # serialized shape costs strictly more than the content estimate alone.
    assert tools.message_tokens(message) > estimate_tokens("hi")
    assert tools.message_tokens(message) == estimate_tokens(
        json.dumps(message, separators=(",", ":"))
    )


def test_conversation_tokens_is_the_canonical_array_serialization() -> None:
    # `conversation_tokens` measures the compact JSON of the whole `messages` array in one
    # operation, so it charges the `[` `]` and the commas joining messages - the wire framing
    # a per-message sum silently drops. With many tiny messages that separator/framing cost is
    # measurable, and the helper equals the one true serialization exactly.
    conversation = [{"role": "user", "content": "x"} for _ in range(50)]
    assert tools.conversation_tokens(conversation) == estimate_tokens(
        json.dumps(conversation, separators=(",", ":"))
    )
    # And it is strictly more than summing the messages one at a time: that sum is what the
    # array framing was missing.
    per_message_sum = sum(tools.message_tokens(message) for message in conversation)
    assert tools.conversation_tokens(conversation) > per_message_sum


def test_framing_dominates_across_many_short_messages() -> None:
    # Twenty one-character messages: the content is twenty tokens, but the request also
    # carries twenty role/framing envelopes. Charging only content would undercount the
    # request severalfold; the canonical path does not.
    conversation = [{"role": "user", "content": "x"} for _ in range(20)]
    content_only = sum(estimate_tokens("x") for _ in range(20))
    assert tools.conversation_tokens(conversation) > content_only * 3


def test_a_long_tool_call_id_is_charged() -> None:
    # A tool turn carries its tool_call_id back to the model, and real ids are long. The old
    # accounting ignored the field entirely; the canonical path charges it, so a long id
    # costs more than a short one.
    short = {"role": "tool", "tool_call_id": "c1", "name": "x", "content": "{}"}
    long = {"role": "tool", "tool_call_id": "call_" + "x" * 40, "name": "x", "content": "{}"}
    assert tools.message_tokens(long) > tools.message_tokens(short)


def test_multiple_tool_calls_and_json_heavy_payloads_are_charged() -> None:
    # An assistant turn asking for several tools, and a tool turn returning nested JSON, both
    # re-enter the model context; both are measured from their full serialized shape.
    assistant = tools._assistant_turn(
        client.AssistantMessage(
            content="",
            tool_calls=[
                client.ToolCall(id="c1", name="cas_evaluate", arguments='{"expression":"2+2"}'),
                client.ToolCall(id="c2", name="cas_solve", arguments='{"equations":["x=1"]}'),
            ],
        )
    )
    one_call = tools._assistant_turn(
        client.AssistantMessage(
            content="",
            tool_calls=[
                client.ToolCall(id="c1", name="cas_evaluate", arguments='{"expression":"2+2"}')
            ],
        )
    )
    assert tools.message_tokens(assistant) > tools.message_tokens(one_call)

    nested = {
        "role": "tool",
        "tool_call_id": "c1",
        "name": "x",
        "content": json.dumps({"ok": True, "value": {"rows": [[1, 2], [3, 4]], "note": "n" * 200}}),
    }
    assert tools.message_tokens(nested) == estimate_tokens(
        json.dumps(nested, separators=(",", ":"))
    )


def test_the_ceiling_boundary_is_exact_under_and_over() -> None:
    # The guard compares canonical conversation tokens against the ceiling with a strict
    # greater-than, so a conversation whose cost equals the ceiling still fits and one token
    # more does not. Both the preflight and the loop read the boundary this way.
    conversation = [{"role": "user", "content": "x"} for _ in range(20)]
    cost = tools.conversation_tokens(conversation)

    at_ceiling = tools.ContextBudget(
        context_window=cost + 100, generation_reserve=100, tool_tokens=0
    )
    assert at_ceiling.message_ceiling == cost
    assert not cost > at_ceiling.message_ceiling  # exactly under: fits

    one_smaller = tools.ContextBudget(
        context_window=cost + 99, generation_reserve=100, tool_tokens=0
    )
    assert cost > one_smaller.message_ceiling  # one token over: refused


# --- Estimator uncertainty and the context safety margin (PLA-290 blocker 4) ---------
#
# `estimate_tokens` is four characters per token against an unknown endpoint tokenizer.
# The canonical accounting above measures the request's shape exactly (in characters); the
# safety margin covers the residual - the characters-per-token ratio running denser than
# four for JSON, code, or non-ASCII text. The margin is charged on the input estimate and
# is explicitly NOT the generation reserve (which is room for output). It makes the guard
# conservative: it may refuse a turn that would have fit, and it does not accept one whose
# estimated input already exceeds the margin-reduced room.


def test_the_margin_shrinks_input_room_and_is_not_the_generation_reserve() -> None:
    from backend.llm import turn_budget

    window, generation = 8192, 2048
    # With no margin the input room is the whole window less the generation reserve.
    assert turn_budget.input_ceiling(window, generation, margin=0.0) == window - generation
    # The margin shrinks the input room strictly, without touching the generation reserve:
    # the reserve is subtracted first and is the same number either way.
    with_margin = turn_budget.input_ceiling(
        window, generation, margin=turn_budget.CONTEXT_SAFETY_MARGIN
    )
    assert with_margin < window - generation


def test_the_margin_is_genuinely_conservative() -> None:
    from backend.llm import turn_budget

    window, generation = 8192, 2048
    margin = turn_budget.CONTEXT_SAFETY_MARGIN
    ceiling = turn_budget.input_ceiling(window, generation, margin=margin)
    # A request filling the margin-reduced ceiling still leaves the margin's worth of slack
    # against the real input room, so a denser-than-estimated tokenizer has headroom.
    assert ceiling * (1 + margin) <= window - generation


def test_the_margin_keeps_a_normal_window_usable() -> None:
    from backend.llm import turn_budget

    # The measured cost of an ordinary turn: the ~1,055-token compute schema plus a few
    # short messages leaves most of an 8,192 window free even after the margin, so a normal
    # turn is not made unusable by the conservative accounting.
    ceiling = turn_budget.input_ceiling(8192, 2048, margin=turn_budget.CONTEXT_SAFETY_MARGIN)
    assert ceiling - 1055 > 3000


def test_non_ascii_content_is_measured_from_its_escaped_transport_shape() -> None:
    # `json.dumps` (default `ensure_ascii=True`) escapes non-ASCII to `\uXXXX`, so the shape
    # this charges for a run of CJK is several transport characters per source character. That
    # is a fact about the JSON on the wire, NOT a claim about the endpoint's tokenizer: the
    # escaping does not, on its own, guarantee the estimate stays above the model's real token
    # count for such text. The actual safeguards are the 10% input margin (a pragmatic bounded
    # approximation) and, as the fallback for a tokenizer that still runs denser, the
    # endpoint's own context rejection handled truthfully as an upstream failure. All this test
    # fixes is that the measurement reflects the escaped wire shape rather than the raw length.
    message = {"role": "user", "content": "日本語のテキスト"}
    assert tools.message_tokens(message) > len(message["content"])


def test_code_and_json_heavy_content_is_charged_in_full() -> None:
    # Punctuation-dense source and JSON are measured from their whole serialized length; no
    # field is dropped and nothing is optimistically discounted.
    code = "def f(x):\n    return {'a': [1, 2, 3], 'b': (x ** 2) % 7}\n"
    message = {"role": "assistant", "content": code}
    assert tools.message_tokens(message) == estimate_tokens(
        json.dumps(message, separators=(",", ":"))
    )
