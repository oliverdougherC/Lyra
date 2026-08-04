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
    content: str = "", tool_calls: list[dict[str, object]] | None = None
) -> dict[str, object]:
    """One chat-completions response body."""
    message: dict[str, object] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}


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
