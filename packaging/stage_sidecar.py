"""Validate and stage the PyInstaller onedir backend for the Tauri bundle."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def stage_sidecar(source: Path, destination: Path) -> Path:
    executable = source / "lyra-backend"
    runtime = source / "_internal"
    if not executable.is_file() or not runtime.is_dir():
        raise ValueError("PyInstaller onedir output must contain lyra-backend and _internal/")
    destination.mkdir(parents=True, exist_ok=True)
    for child in destination.iterdir():
        if child.name == ".gitkeep":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
    return destination / "lyra-backend"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("dist/lyra-backend"))
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("src-tauri/resources/lyra-backend"),
    )
    args = parser.parse_args(argv)
    print(stage_sidecar(args.source, args.destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
