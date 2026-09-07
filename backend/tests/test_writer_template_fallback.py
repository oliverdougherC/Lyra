"""A refused optional template field must not demote JSON constraints."""

import json

import httpx
import pytest

from backend.core.errors import UpstreamError
from backend.llm import client

ENDPOINT = "http://127.0.0.1:19991/v1"
SCHEMA = client.JsonSchema(name="fixture", schema={"type": "object"})
MESSAGES = [{"role": "user", "content": "Synthetic source-only assessment."}]


@pytest.fixture(autouse=True)
def reset_formats():
    client.reset_json_support()
    yield
    client.reset_json_support()


def answer():
    return httpx.Response(
        200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    )


async def run(handler, **kwargs):
    return await client.complete(
        ENDPOINT,
        "synthetic-key",
        "captured-model",
        MESSAGES,
        transport=httpx.MockTransport(handler),
        max_tokens=200,
        temperature=0,
        enable_thinking=False,
        **kwargs,
    )


@pytest.mark.parametrize("field", ["chat_template_kwargs", "enable_thinking"])
async def test_field_refusal_retries_only_template_field_on_same_json_rung(field):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(400, json={"error": f"request schema rejects unknown {field}"})
        return answer()

    assert await run(handler, schema=SCHEMA) == "{}"
    assert len(requests) == 2
    first, second = [json.loads(r.content) for r in requests]
    assert first.pop("chat_template_kwargs") == {"enable_thinking": False}
    assert first == second
    assert second["response_format"]["type"] == "json_schema"
    assert requests[0].url == requests[1].url
    assert (
        requests[0].headers["authorization"]
        == requests[1].headers["authorization"]
        == "Bearer synthetic-key"
    )
    assert client._json_levels(ENDPOINT, "captured-model", SCHEMA)[0] == client.JSON_SCHEMA


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
async def test_other_failures_do_not_trigger_template_retry(status):
    sent = []

    def handler(request):
        sent.append(request)
        message = "context window exceeded" if status == 400 else "chat_template_kwargs refused"
        return httpx.Response(status, json={"error": message})

    with pytest.raises(UpstreamError):
        await run(handler)
    assert len(sent) == 1


@pytest.mark.parametrize(
    "message", ["chat_template_kwargs unsupported", "schema rejects enable_thinking"]
)
async def test_template_rejection_is_retried_at_most_once(message):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(400, json={"error": message})

    with pytest.raises(UpstreamError):
        await run(handler, schema=SCHEMA)
    assert len(sent) == 2
    assert "chat_template_kwargs" not in json.loads(sent[1].content)


async def test_format_ladder_still_applies_after_template_fallback():
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        if "chat_template_kwargs" in body:
            return httpx.Response(400, json={"error": "unknown enable_thinking"})
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": "response_format json_schema unsupported"})
        return answer()

    assert await run(handler, schema=SCHEMA) == "{}"
    assert len(sent) == 3
    assert [b["response_format"]["type"] for b in sent] == [
        "json_schema",
        "json_schema",
        "json_object",
    ]
    assert all("chat_template_kwargs" not in b for b in sent[1:])


@pytest.mark.parametrize("failure", ["cutoff", "malformed"])
async def test_successful_http_with_bad_content_is_not_retried(failure):
    sent = []

    def handler(request):
        sent.append(request)
        if failure == "malformed":
            return httpx.Response(200, text="not JSON chat_template_kwargs")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}
        )

    with pytest.raises(UpstreamError):
        await run(handler, fail_on_truncation=True)
    assert len(sent) == 1


async def test_no_template_sent_means_no_template_retry():
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(400, json={"error": "unknown enable_thinking"})

    with pytest.raises(UpstreamError):
        await client.complete(
            ENDPOINT, None, "captured-model", MESSAGES, transport=httpx.MockTransport(handler)
        )
    assert len(sent) == 1


async def test_template_retry_does_not_apply_to_streams():
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(400, json={"error": "chat_template_kwargs unsupported"})

    with pytest.raises(UpstreamError):
        async for _ in client.stream_chat(
            ENDPOINT,
            "synthetic-key",
            "captured-model",
            MESSAGES,
            transport=httpx.MockTransport(handler),
            enable_thinking=False,
        ):
            pytest.fail("Refused stream yielded output")
    assert len(sent) == 1
