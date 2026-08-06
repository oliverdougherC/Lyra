"""How the tool loop classifies a failure partway through a run.

Companion to `test_tool_loop.py`. The contract under test: what a 400 means depends on
when it arrives. On the first tools-carrying request it is the best available signal that
the endpoint does not implement tool calling. After the endpoint has answered tool rounds
in this very loop, that reading is impossible - the capability was just demonstrated -
and the honest classification is an endpoint failure, commonly a transcript grown past
the context window.
"""

import json
import logging

import httpx
import pytest

from backend.llm import tools

_ENDPOINT = "http://127.0.0.1:8080/v1"
_MESSAGES: list[dict[str, object]] = [{"role": "user", "content": "Check this."}]

_TOOL_CALL_REPLY = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "cas_evaluate",
                            "arguments": json.dumps({"expression": "2 + 2"}),
                        },
                    }
                ],
            }
        }
    ]
}


async def test_a_400_after_a_successful_tool_round_is_an_endpoint_failure_not_no_tools() -> None:
    """Ten good rounds and then a 400 is the request going bad, not the capability.

    Reported as `no_tool_support`, this told the settings screen and the verdict two
    contradictory stories about an endpoint that had just been calling tools; the real
    cause - the tool transcript outgrowing the context window - was never named anywhere.
    """
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            return httpx.Response(200, json=_TOOL_CALL_REPLY)
        return httpx.Response(400, json={"error": "the request exceeds the context window"})

    result = await tools.run_tool_loop(
        _ENDPOINT, None, "local-model", _MESSAGES, transport=httpx.MockTransport(handler)
    )

    assert result.stopped == tools.UPSTREAM_FAILED
    assert result.complete is False
    # The round that ran is still worth showing - it just is not worth trusting.
    assert [call.name for call in result.calls] == ["cas_evaluate"]
    assert "context window" in result.detail


async def test_a_400_on_the_first_request_still_reads_as_no_tool_support() -> None:
    """The first request is the only one with nothing to contradict the refusal."""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(400, json={"error": "tools not supported"})
    )

    result = await tools.run_tool_loop(
        _ENDPOINT, None, "local-model", _MESSAGES, transport=transport
    )

    assert result.stopped == tools.NO_TOOL_SUPPORT


async def test_an_unexpected_failure_is_logged_with_its_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The catch-all guard swallows real bugs alongside network weather.

    Both are tolerable only because the log keeps the evidence: a class name with no
    traceback made a coding error in the loop indistinguishable from a dropped socket.
    """

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with caplog.at_level(logging.ERROR, logger="backend.llm.tools"):
        result = await tools.run_tool_loop(
            _ENDPOINT, None, "local-model", _MESSAGES, transport=httpx.MockTransport(refuse)
        )

    assert result.stopped == tools.UPSTREAM_FAILED
    record = next(record for record in caplog.records if "tutor endpoint" in record.getMessage())
    assert record.exc_info is not None
