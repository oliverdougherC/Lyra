"""Verify every bundled native object's architecture and declared deployment floor."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from sign_local_app import MACHO_MAGICS


def inspect_bundle(app: Path, minimum: str) -> dict[str, object]:
    objects = []
    floor = (tuple(int(part) for part in minimum.split(".")) + (0, 0, 0))[:3]
    for path in sorted(app.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(4) not in MACHO_MAGICS:
                continue
        architectures = subprocess.check_output(  # noqa: S603
            ["/usr/bin/lipo", "-archs", str(path)], text=True
        ).split()
        if "arm64" not in architectures:
            raise ValueError(f"Native object lacks arm64: {path.relative_to(app)}")
        commands = subprocess.check_output(  # noqa: S603
            ["/usr/bin/otool", "-l", str(path)], text=True
        )
        minima = re.findall(r"\bminos\s+([\d.]+)", commands)
        minima += re.findall(
            r"LC_VERSION_MIN_MACOSX\s+cmdsize\s+\d+\s+version\s+([\d.]+)", commands
        )
        if not minima:
            raise ValueError(
                f"Native object has no macOS deployment floor: {path.relative_to(app)}"
            )
        if any(
            (tuple(int(part) for part in value.split(".")) + (0, 0, 0))[:3] > floor
            for value in minima
        ):
            raise ValueError(f"Native dependency exceeds macOS {minimum}: {path.relative_to(app)}")
        subprocess.run(  # noqa: S603
            ["/usr/bin/codesign", "--verify", "--strict", str(path)],
            check=True,
            capture_output=True,
        )
        objects.append(
            {"path": str(path.relative_to(app)), "architectures": architectures, "minimum": minima}
        )
    if not objects:
        raise ValueError("Bundle contains no native code")
    return {
        "status": "passed",
        "architecture": "arm64",
        "minimumSystemVersion": minimum,
        "objects": objects,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("--minimum", default="14.0")
    args = parser.parse_args()
    print(json.dumps(inspect_bundle(args.app, args.minimum), indent=2))


if __name__ == "__main__":
    main()
