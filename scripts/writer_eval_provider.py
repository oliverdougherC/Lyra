"""Loopback-only deterministic fault provider; never a writing-quality judge."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def schema_value(schema: dict[str, Any]) -> Any:
    """Produce conservative scaffolding for the actual requested JSON schema."""
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        return {key: schema_value(value) for key, value in schema.get("properties", {}).items()}
    if kind == "array":
        return []
    if kind in ("integer", "number"):
        return 80
    if kind == "boolean":
        return False
    return "Synthetic fixture; assess the source limitations."


class FaultProvider:
    """Real HTTP/SSE transport with explicit, reproducible one-shot faults."""

    def __init__(self, delay: float = 0.08):
        self.delay = delay
        self.requests: list[dict[str, Any]] = []
        self.fault: str | None = None
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {"model_path": "nomic-embed-text-v1.5.Q8_0.gguf"}
                        if self.path == "/props"
                        else {"data": [{"id": "synthetic-writer-v1"}]}
                    ).encode()
                )

            def do_POST(self) -> None:  # noqa: N802
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path in ("/tokenize", "/v1/embeddings"):
                    if self.path == "/tokenize":
                        response = {
                            "tokens": list(range(max(1, len(str(payload.get("content", ""))) // 4)))
                        }
                    else:
                        inputs = payload.get("input", [])
                        response = {
                            "data": [
                                {"index": i, "embedding": [1.0] + [0.0] * 767}
                                for i, _ in enumerate(inputs)
                            ]
                        }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(response).encode())
                    return
                spec = payload.get("response_format", {}).get("json_schema", {})
                with owner.lock:
                    fault = owner.fault
                    if fault in ("partial_stream", "malformed_stream") and not payload.get(
                        "stream"
                    ):
                        fault = None
                    else:
                        owner.fault = None
                    owner.requests.append(
                        {
                            "index": len(owner.requests) + 1,
                            "schema": spec.get("name"),
                            "stream": bool(payload.get("stream")),
                            "fault": fault,
                        }
                    )
                time.sleep(owner.delay if fault != "slow" else max(owner.delay, 3))
                if fault in ("rate_limit", "transient"):
                    self.send_response(429 if fault == "rate_limit" else 503)
                    self.send_header("Retry-After", "0")
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"synthetic transient fault"}}')
                    return
                value = schema_value(spec.get("schema", {}))
                name = spec.get("name")
                if name == "writer_paragraph_outline":
                    item = schema_value(spec["schema"]["properties"]["paragraphs"]["items"])
                    item["key"] = "p1"
                    value = {"paragraphs": [item]}
                elif name == "writer_section_skeptic":
                    value["passes"] = True
                content = (
                    json.dumps(value)
                    if spec
                    else (
                        "The evidence supports a limited conclusion. The observation is useful, "
                        "but it does not establish causation. More measurements would help test "
                        "whether the pattern persists beyond this small sample."
                    )
                )
                if not spec:
                    # A continuation adds a final user message; the stable paragraph
                    # job remains earlier in the conversation and must not change identity.
                    prompt = next(
                        (
                            str(message.get("content", ""))
                            for message in payload.get("messages", [])
                            if "Section plan:\n" in str(message.get("content", ""))
                        ),
                        str(payload.get("messages", [{}])[-1].get("content", "")),
                    )
                    match = re.search(r"Write about ([0-9,]+) words", prompt)
                    target = int(match[1].replace(",", "")) if match else 100
                    section_context = prompt.split("Section plan:\n", 1)[-1].split("\n\n", 1)[0]
                    section_match = re.search(
                        r'"(?:section_ref|ref)"\s*:\s*"([^"]+)"', section_context
                    )
                    stable_section = section_match[1] if section_match else "fixture"
                    marker = hashlib.sha256(stable_section.encode()).hexdigest()[:8]
                    content = f"Synthetic passage {marker}. " + content
                    while len(content.split()) < min(target, 500):
                        content += (
                            " I would describe what was observed before deciding what it means."
                            " A careful account distinguishes a recorded event from an explanation"
                            " and leaves room for a different result in a later observation."
                        )
                if fault == "empty":
                    content = ""
                calls = []
                functions = [tool["function"] for tool in payload.get("tools", [])]
                has_tool_result = any(m.get("role") == "tool" for m in payload.get("messages", []))
                if functions and not has_tool_result and fault != "empty":
                    function = next(
                        (f for f in functions if f["name"] == "add_comment"), functions[0]
                    )
                    args = schema_value(function.get("parameters", {}))
                    if function["name"] == "add_comment":
                        args = {
                            "body": "Synthetic review: this claim needs supporting evidence.",
                            "severity": "major",
                            "quote": "",
                            "section_ref": "",
                        }
                    calls = [
                        {
                            "id": "call_fixture",
                            "type": "function",
                            "function": {"name": function["name"], "arguments": json.dumps(args)},
                        }
                    ]
                reason = "tool_calls" if calls else "stop"
                try:
                    self.send_response(200)
                    self.send_header(
                        "Content-Type",
                        "text/event-stream" if payload.get("stream") else "application/json",
                    )
                    self.end_headers()
                    if payload.get("stream"):
                        delta = (
                            {"tool_calls": [{"index": i, **c} for i, c in enumerate(calls)]}
                            if calls
                            else {"content": content}
                        )
                        self.wfile.write(
                            (
                                "data: "
                                + json.dumps(
                                    {
                                        "choices": [
                                            {"index": 0, "delta": delta, "finish_reason": None}
                                        ]
                                    }
                                )
                                + "\n\n"
                            ).encode()
                        )
                        self.wfile.flush()
                        if fault == "partial_stream":
                            return
                        if fault == "malformed_stream":
                            self.wfile.write(b"data: {broken\n\n")
                            return
                        self.wfile.write(
                            (
                                "data: "
                                + json.dumps(
                                    {
                                        "choices": [
                                            {"index": 0, "delta": {}, "finish_reason": reason}
                                        ]
                                    }
                                )
                                + "\n\ndata: [DONE]\n\n"
                            ).encode()
                        )
                    else:
                        message = {"role": "assistant", "content": content}
                        if calls:
                            message["tool_calls"] = calls
                        self.wfile.write(
                            json.dumps(
                                {"choices": [{"message": message, "finish_reason": reason}]}
                            ).encode()
                        )
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1"

    def arm(self, fault: str | None) -> None:
        with self.lock:
            self.fault = fault

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
