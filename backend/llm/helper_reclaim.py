"""Trusted CLI for reclaiming Lyra-owned llama helpers after backend shutdown fallback."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import TextIO

from backend.core.errors import ConfigurationError
from backend.llm.embed_server import embedding_server
from backend.llm.llama_server import _read_server_record, _token_matches_pid
from backend.llm.ocr_server import ocr_server
from backend.llm.rerank_server import rerank_server

_HELPERS = {
    "embedding": embedding_server,
    "text-recognition": ocr_server,
    "reranking": rerank_server,
}


def _record_state(service: str) -> str:
    record = _read_server_record(service)
    if record is None:
        return "absent"
    pid = record.get("pid")
    token = record.get("start_token")
    if isinstance(pid, int) and isinstance(token, str) and _token_matches_pid(pid, token):
        return "live"
    return "stale"


def reclaim_owned_helpers(*, services: Sequence[str] | None = None) -> dict[str, object]:
    selected = list(services or _HELPERS)
    results: list[dict[str, object]] = []
    failures = 0

    for service in selected:
        helper = _HELPERS[service]
        try:
            before = _record_state(service)
            helper.stop_for_app_quit()
            after = _record_state(service)
            results.append(
                {
                    "service": service,
                    "before": before,
                    "after": after,
                    "ok": True,
                }
            )
        except ConfigurationError as exc:
            failures += 1
            results.append(
                {
                    "service": service,
                    "ok": False,
                    "error": exc.__class__.__name__,
                }
            )
        except Exception as exc:
            failures += 1
            results.append(
                {
                    "service": service,
                    "ok": False,
                    "error": exc.__class__.__name__,
                }
            )

    return {
        "status": "ok" if failures == 0 else "error",
        "services": results,
    }


def main(argv: Sequence[str] | None = None, *, stream: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.llm.helper_reclaim",
        description="Reclaim Lyra-owned llama helpers from durable ownership records.",
    )
    parser.add_argument(
        "--service",
        action="append",
        choices=sorted(_HELPERS),
        dest="services",
        help="Reclaim only the named helper. Repeat to reclaim more than one.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    payload = reclaim_owned_helpers(services=args.services)
    output = stream or sys.stdout
    output.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
    output.flush()
    return 0 if payload["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
