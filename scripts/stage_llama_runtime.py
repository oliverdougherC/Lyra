"""Stage the pinned llama.cpp runtime into the Tauri resource tree for the app bundle.

The packaged product ships its own runtime: a clean install must be able to send the
first message with an empty application-support models directory, and the runtime is
the half of that promise the weights alone do not cover. The build downloads the same
pinned release `scripts/fetch_models.py` installs into a checkout - one pin, one
download, one build verification - and stages it next to the frozen backend, where the
backend resolves it at first use without starting it.

Because the runtime stays inside the .app bundle, it is signed and notarized with the
rest of the application rather than fetched and executed from the user's data directory
at first use.

    python scripts/stage_llama_runtime.py
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

try:
    from scripts import fetch_models
except ModuleNotFoundError:
    # Run as a plain script: the script's own directory is on the path, not the repo root.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import fetch_models


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("src-tauri/resources/llama"),
        help="the Tauri resource directory the runtime is staged into",
    )
    args = parser.parse_args(argv)
    binary = fetch_models.fetch_llama_server(target_dir=args.destination)
    print(f"llama-server: {binary}  ({fetch_models.installed_build(binary)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
