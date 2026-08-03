"""Client for the user's OpenAI-compatible tutor endpoint.

The configured endpoint is assumed to already carry its API version suffix, for example
`http://127.0.0.1:8080/v1`. This module appends only `/chat/completions` and `/models` and
never probes alternative paths: a wrong path surfaces as a 404 the user can act on, which
beats silently guessing at another one.

Every failure becomes an `UpstreamError` with a message written for the user. Those messages,
and any log line this module writes, never contain the endpoint URL or the API key.
"""

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from backend.core.errors import UpstreamError

logger = logging.getLogger(__name__)

# A local model can spend minutes on the first token, so reads are patient while connects
# are not. The probe timeouts are short so a dead host cannot hold the settings screen.
CHAT_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
PROBE_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

_ERROR_UNREACHABLE = "The tutor endpoint is not reachable. Check that the server is running."
_ERROR_TIMEOUT = "The tutor endpoint did not respond in time."
_ERROR_UNAUTHORIZED = "The tutor endpoint rejected the API key."
_ERROR_NOT_FOUND = "The tutor endpoint path looks wrong. The URL should end in /v1."
_ERROR_UPSTREAM = "The tutor endpoint returned an error."
_ERROR_UNREADABLE = "The tutor endpoint returned a response that could not be read."


@dataclass(frozen=True)
class ConnectionResult:
    """Outcome of a connection test, shaped for the four states the settings screen renders."""

    ok: bool
    model_count: int
    message: str


def _base_url(endpoint: str) -> str:
    """Drop one trailing slash so path joining does not double it."""
    return endpoint[:-1] if endpoint.endswith("/") else endpoint


def _client(
    timeout: httpx.Timeout,
    api_key: str | None,
    transport: httpx.AsyncBaseTransport | None,
) -> httpx.AsyncClient:
    """Build the HTTP client. `transport` is the seam tests inject a stub through."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return httpx.AsyncClient(timeout=timeout, headers=headers, transport=transport)


def _mapped_error(exc: Exception) -> UpstreamError:
    """Turn a transport or status failure into a distinct user-facing error.

    The endpoint URL and the API key are deliberately absent from every branch: these
    messages reach the browser, and `httpx` puts the full request URL in its own strings.
    """
    if isinstance(exc, httpx.TimeoutException):
        return UpstreamError(_ERROR_TIMEOUT)
    if isinstance(exc, httpx.ConnectError):
        return UpstreamError(_ERROR_UNREACHABLE)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return UpstreamError(_ERROR_UNAUTHORIZED)
        if status == 404:
            return UpstreamError(_ERROR_NOT_FOUND)
        logger.warning("Tutor endpoint returned status %s", status)
        return UpstreamError(_ERROR_UPSTREAM)
    return UpstreamError(_ERROR_UNREACHABLE)


def _chat_body(
    model: str | None, messages: list[dict[str, str]], stream: bool
) -> dict[str, object]:
    """Assemble a chat-completions body, omitting `model` when the user has not picked one."""
    body: dict[str, object] = {"messages": messages, "stream": stream}
    if model is not None:
        body["model"] = model
    return body


def _delta_text(payload: str) -> str | None:
    """Pull the content delta out of one SSE data payload, or None if there is nothing to yield."""
    try:
        frame = json.loads(payload)
    except ValueError:
        # Keep-alive noise and half-written frames are normal on some servers, not fatal.
        return None
    if not isinstance(frame, dict):
        return None
    choices = frame.get("choices") or []
    if not choices:
        return None
    delta = choices[0].get("delta") or {}
    text = delta.get("content")
    return text if isinstance(text, str) and text else None


async def stream_chat(
    endpoint: str,
    api_key: str | None,
    model: str | None,
    messages: list[dict[str, str]],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[str]:
    """Stream assistant content deltas from the tutor endpoint.

    Args:
        endpoint: Endpoint base URL including its version suffix.
        api_key: Bearer token, or None when the endpoint needs no auth.
        model: Model identifier, omitted from the request when None.
        messages: OpenAI-shaped chat messages.
        transport: Test seam. Leave unset in production code.

    Yields:
        Non-empty content fragments in arrival order, ending at the upstream `[DONE]` frame.

    Raises:
        UpstreamError: The endpoint was unreachable, slow, or returned a non-2xx status.
    """
    url = f"{_base_url(endpoint)}/chat/completions"
    body = _chat_body(model, messages, stream=True)
    async with _client(CHAT_TIMEOUT, api_key, transport) as client:
        try:
            async with client.stream("POST", url, json=body) as response:
                if response.status_code >= 400:
                    await response.aread()
                    response.raise_for_status()
                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:") :].strip()
                    if payload == "[DONE]":
                        return
                    text = _delta_text(payload)
                    if text:
                        yield text
        except httpx.HTTPError as exc:
            raise _mapped_error(exc) from exc


async def complete(
    endpoint: str,
    api_key: str | None,
    model: str | None,
    messages: list[dict[str, str]],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Run a single non-streaming completion and return the assistant message content.

    Profile extraction uses this: it wants one whole JSON document, not a token stream.

    Raises:
        UpstreamError: The endpoint failed, or its reply had no readable message content.
    """
    url = f"{_base_url(endpoint)}/chat/completions"
    body = _chat_body(model, messages, stream=False)
    async with _client(CHAT_TIMEOUT, api_key, transport) as client:
        try:
            response = await client.post(url, json=body)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise _mapped_error(exc) from exc
        except ValueError as exc:
            raise UpstreamError(_ERROR_UNREADABLE) from exc

    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        raise UpstreamError(_ERROR_UNREADABLE)
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str):
        raise UpstreamError(_ERROR_UNREADABLE)
    return content


async def list_models(
    endpoint: str,
    api_key: str | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    """List the model identifiers the endpoint advertises.

    Raises:
        UpstreamError: The endpoint failed or returned something that is not a model list.
    """
    url = f"{_base_url(endpoint)}/models"
    async with _client(PROBE_TIMEOUT, api_key, transport) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise _mapped_error(exc) from exc
        except ValueError as exc:
            raise UpstreamError(_ERROR_UNREADABLE) from exc

    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise UpstreamError(_ERROR_UNREADABLE)
    return [str(entry["id"]) for entry in entries if isinstance(entry, dict) and entry.get("id")]


async def test_connection(
    endpoint: str,
    api_key: str | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ConnectionResult:
    """Probe the endpoint and report the outcome instead of raising.

    The settings screen renders four distinct outcomes, so a failure has to come back as
    data with its own message rather than as an exception the caller has to re-classify.
    A reachable endpoint advertising zero models is still `ok`; the UI reads `model_count`.
    """
    try:
        models = await list_models(endpoint, api_key, transport=transport)
    except UpstreamError as exc:
        return ConnectionResult(ok=False, model_count=0, message=exc.message)
    count = len(models)
    return ConnectionResult(
        ok=True,
        model_count=count,
        message=f"Connected. {count} models available.",
    )
