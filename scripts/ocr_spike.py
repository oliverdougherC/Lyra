"""The serving spike the specialist OCR path is gated on.

docs/rag-pipeline.md records the question: can `llama-server` serve Unlimited-OCR with its
chat template applied, or must Lyra fall back to one-shot `llama-mtmd-cli` and pay a
multi-gigabyte model reload per page?

It matters because the reload is the whole cost. One-shot `llama-mtmd-cli` loads the
weights, reads one page, and exits. Over a forty-page document that is forty loads, which
is exactly the throughput problem the specialist path exists to solve.

The known hazard is specific: with `--chat-template deepseek-ocr`, llama-server has
answered `400` with `number of bitmaps (1) does not match number of markers (0)` for this
model family (ggml-org/llama.cpp#21022). Dropping the template made it run and degraded
quality, so "it returned text" is not a passing result on its own.

    python scripts/ocr_spike.py --document data/eval/uploads/1/23-Fourier_Tables.pdf

Reads one page both ways, times both, and writes the two readings out to be compared by a
person. It changes nothing and touches no database.
"""

import argparse
import base64
import difflib
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import settings  # noqa: E402
from backend.llm.ocr_server import (  # noqa: E402
    MAX_OUTPUT_TOKENS,
    OCR_PROMPT,
    REFERENCE_ARGS,
    ocr_server,
)
from backend.rag import render  # noqa: E402

DEFAULT_DOCUMENT = ROOT / "data" / "eval" / "uploads" / "1" / "23-Fourier_Tables.pdf"
DEFAULT_PAGE = 2

# One page through a several-gigabyte model on local hardware is minutes, not seconds.
CLI_TIMEOUT_SECONDS = 900.0
SERVER_TIMEOUT_SECONDS = 900.0


def _binary(name: str) -> Path:
    """Find one llama.cpp binary under the models directory."""
    for candidate in sorted(settings.llama_dir.rglob(name)):
        if candidate.is_file():
            return candidate
    raise SystemExit(f"{name} is not installed. Run: python scripts/fetch_models.py")


def read_one_shot(image: Path) -> tuple[str, float, str | None]:
    """Read a page with `llama-mtmd-cli`, which loads the model and exits.

    This is the fallback, and the baseline the server path has to match. It takes
    `REFERENCE_ARGS` and not `SERVER_ARGS`: the CLI rejects `--special` outright and needs
    no equivalent, because it prints special tokens by default. That asymmetry is the whole
    finding of this spike.
    """
    argv = [
        str(_binary("llama-mtmd-cli")),
        "-m",
        str(settings.ocr_model_path),
        "--mmproj",
        str(settings.ocr_mmproj_path),
        "--image",
        str(image),
        "-p",
        OCR_PROMPT,
        "-n",
        str(MAX_OUTPUT_TOKENS),
        *REFERENCE_ARGS,
    ]
    started = time.monotonic()
    try:
        # S603: every element comes from settings or this file, and it is a list.
        result = subprocess.run(  # noqa: S603
            argv, capture_output=True, timeout=CLI_TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired:
        return "", time.monotonic() - started, "timed out"
    elapsed = time.monotonic() - started
    if result.returncode != 0:
        return "", elapsed, result.stderr.decode("utf-8", "replace")[-400:]
    return result.stdout.decode("utf-8", "replace").strip(), elapsed, None


def read_through_server(image: Path) -> tuple[str, float, str | None]:
    """Read the same page through the persistent server, with the chat template on."""
    encoded = base64.b64encode(image.read_bytes()).decode("ascii")
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": OCR_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}"},
                    },
                ],
            }
        ],
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0,
    }
    started = time.monotonic()
    try:
        with httpx.Client(timeout=SERVER_TIMEOUT_SECONDS) as client:
            response = client.post(f"{ocr_server.base_url}/v1/chat/completions", json=payload)
    except httpx.HTTPError as exc:
        return "", time.monotonic() - started, f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - started

    if response.status_code != 200:
        # The documented failure mode is a 400 naming bitmaps and markers. Report the body
        # rather than the status, because the body is the whole finding.
        return "", elapsed, f"HTTP {response.status_code}: {response.text[:400]}"
    body = response.json()
    return str(body["choices"][0]["message"]["content"]).strip(), elapsed, None


def _report(what: str, seconds: float, text: str, error: str | None) -> None:
    """One line about one reading, saying plainly when it failed."""
    tail = f", FAILED {error}" if error else ""
    print(f"{what}: {seconds:.1f}s, {len(text)} chars{tail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document", default=str(DEFAULT_DOCUMENT))
    parser.add_argument("--page", type=int, default=DEFAULT_PAGE)
    parser.add_argument("--out", default=str(ROOT / "data" / "ocr-spike"))
    args = parser.parse_args(argv)

    source = Path(args.document).resolve()
    if not source.is_file():
        raise SystemExit(f"Not a file: {source}")
    if not settings.ocr_installed:
        raise SystemExit(
            "The OCR weights are not installed. Run: python scripts/fetch_models.py --ocr"
        )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # Rendered exactly as recognition renders, so the spike measures the model rather than
    # a resolution nothing else uses.
    image = render.render_page(0, source, "application/pdf", args.page, render.RECOGNITION_DPI)
    print(f"page {args.page} of {source.name}, rendered at {render.RECOGNITION_DPI} dpi\n")

    print("one-shot llama-mtmd-cli ...")
    cli_text, cli_seconds, cli_error = read_one_shot(image)
    _report("  one-shot", cli_seconds, cli_text, cli_error)

    print("persistent llama-server ...")
    server_error: str | None = None
    server_text, server_seconds = "", 0.0
    try:
        ocr_server.ensure_running()
    except Exception as exc:  # noqa: BLE001 - a failure to start is the spike's answer
        server_error = f"could not start: {exc}"
    else:
        server_text, server_seconds, server_error = read_through_server(image)
        # A second page, to show what the reload actually costs: the first request pays for
        # a warm-up the one-shot path pays on every page.
        warm_text, warm_seconds, warm_error = read_through_server(image)
        _report("  second request", warm_seconds, warm_text, warm_error)
        if warm_error is None:
            print(f"  identical to the first request: {warm_text == server_text}")
    _report("  server", server_seconds, server_text, server_error)
    ocr_server.stop()

    same = cli_text == server_text and not cli_error and not server_error
    ratio = (
        difflib.SequenceMatcher(None, cli_text, server_text).ratio()
        if cli_text and server_text
        else 0.0
    )
    verdict = {
        "document": source.name,
        "page": args.page,
        "one_shot": {
            "seconds": round(cli_seconds, 1),
            "characters": len(cli_text),
            "error": cli_error,
        },
        "server": {
            "seconds": round(server_seconds, 1),
            "characters": len(server_text),
            "error": server_error,
        },
        "byte_identical": same,
        "similarity": round(ratio, 4),
    }
    (out / "spike.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    (out / "one-shot.txt").write_text(cli_text, encoding="utf-8")
    (out / "server.txt").write_text(server_text, encoding="utf-8")

    print(f"\nbyte-identical: {same}   similarity: {ratio:.4f}")
    print(f"both readings: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
