"""Verify the exact downloadable DMG and read back its signed app before release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frozen_backend_smoke import run_smoke  # noqa: E402
from release.signing_evidence import inspect_bundle  # noqa: E402


def run(*args: str) -> str:
    return subprocess.run(  # noqa: S603 — fixed tools and argument arrays
        args, check=True, text=True, capture_output=True
    ).stdout


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def app_tree(app: Path) -> dict[str, tuple[str, int, str]]:
    """Hash regular bytes and preserve symlink targets, entry types, and permissions."""
    if app.is_symlink() or not app.is_dir():
        raise ValueError(f"Expected a real app directory: {app}")
    result = {}
    for path in [app, *sorted(app.rglob("*"))]:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            kind, payload = "symlink", os.readlink(path)
        elif stat.S_ISREG(mode):
            kind, payload = "file", file_digest(path)
        elif stat.S_ISDIR(mode):
            kind, payload = "directory", ""
        else:
            raise ValueError(f"Unsupported app entry: {path}")
        result[str(path.relative_to(app))] = (kind, stat.S_IMODE(mode), payload)
    return result


def verify_dmg(image: Path, source_app: Path) -> dict[str, object]:
    image = image.resolve(strict=True)
    source_app = source_app.resolve(strict=True)
    expected = app_tree(source_app)
    run("/usr/bin/hdiutil", "verify", str(image))
    mountpoint = Path(tempfile.mkdtemp(prefix="lyra-dmg-verify-"))
    attached = False
    try:
        run(
            "/usr/bin/hdiutil",
            "attach",
            "-readonly",
            "-nobrowse",
            "-noautoopen",
            "-mountpoint",
            str(mountpoint),
            str(image),
        )
        attached = True
        mounted_app = mountpoint / source_app.name
        actual = app_tree(mounted_app)
        if actual != expected:
            differences = sorted(
                name
                for name in expected.keys() | actual.keys()
                if expected.get(name) != actual.get(name)
            )
            raise ValueError(f"DMG app differs from signed source: {differences[:10]}")
        signing = inspect_bundle(mounted_app)
        backend = mounted_app / "Contents/Resources/resources/lyra-backend/lyra-backend"
        smoke = run_smoke(backend)
        if smoke.get("status") != "passed":
            raise ValueError("DMG frozen backend smoke did not pass")
        return {
            "status": "passed",
            "image_sha256": file_digest(image),
            "app_tree_sha256": hashlib.sha256(
                json.dumps(actual, sort_keys=True).encode()
            ).hexdigest(),
            "app_tree_entries": len(actual),
            "signing": signing,
            "frozen_smoke": smoke,
        }
    finally:
        # attach can fail after mounting. Never recursively remove this path: a failed
        # detach must leave the image and mountpoint intact for diagnosis/recovery.
        if attached or os.path.ismount(mountpoint):
            run("/usr/bin/hdiutil", "detach", str(mountpoint))
        if mountpoint.exists():
            mountpoint.rmdir()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("source_app", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify_dmg(args.image, args.source_app), sort_keys=True))


if __name__ == "__main__":
    main()
