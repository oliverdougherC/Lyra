"""Generate a privacy-safe, deterministic packaged desktop resource inventory.

This is for packaged-runtime evidence, not for user-profile inspection. The report never
prints absolute paths: every entry is relative to the package root passed on the command
line, and absolute symlink targets are redacted.

Example:

    python scripts/desktop_resource_report.py \
        --root app=/Applications/Lyra.app \
        --output desktop-resource-report.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path

SCHEMA_VERSION = 1
_HASH_CHUNK_BYTES = 1 << 20
_BINARY_SUFFIXES = {".dylib", ".so", ".dll"}
_FRONTEND_SUFFIXES = {".css", ".html", ".js", ".json", ".mjs", ".cjs", ".map"}
_FONT_SUFFIXES = {".ttf", ".otf", ".woff", ".woff2"}
_IMAGE_SUFFIXES = {".icns", ".png", ".jpg", ".jpeg", ".svg", ".webp"}
_MANIFEST_SUFFIXES = {".json", ".plist", ".toml", ".yaml", ".yml"}
_PYTHON_SUFFIXES = {".py", ".pyc", ".pyd", ".so", ".zip"}


def parse_root_spec(spec: str) -> tuple[str, Path]:
    if "=" not in spec:
        raise argparse.ArgumentTypeError("root spec must be LABEL=PATH")
    label, raw_path = spec.split("=", 1)
    label = label.strip()
    if not label:
        raise argparse.ArgumentTypeError("root label must not be empty")
    path = Path(raw_path).expanduser()
    return label, path


def digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def classify_entry(relative_path: str, mode: int) -> str:
    suffix = Path(relative_path).suffix.lower()
    parts = tuple(Path(relative_path).parts)
    name = parts[-1].lower() if parts else relative_path.lower()
    if stat.S_ISLNK(mode):
        if suffix in _BINARY_SUFFIXES:
            return "shared-library"
        if suffix in _PYTHON_SUFFIXES:
            return "python-runtime"
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if "migrations" in parts and suffix == ".sql":
        return "sqlite-migration"
    if suffix in _BINARY_SUFFIXES:
        return "shared-library"
    if suffix in _FONT_SUFFIXES:
        return "font"
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _MANIFEST_SUFFIXES:
        return "manifest"
    if suffix in _PYTHON_SUFFIXES or name.startswith("python"):
        return "python-runtime"
    if suffix in _FRONTEND_SUFFIXES or "assets" in parts:
        return "frontend-asset"
    if stat.S_ISREG(mode) and mode & stat.S_IXUSR:
        return "executable"
    return "other"


def describe_symlink(path: Path) -> tuple[str, bool]:
    target = os.readlink(path)
    if os.path.isabs(target):
        return "<absolute-target-redacted>", True
    return target, False


def collect_root(label: str, root: Path) -> dict[str, object]:
    resolved = root.expanduser()
    report: dict[str, object] = {
        "label": label,
        "root_name": resolved.name or str(resolved),
        "exists": resolved.exists(),
        "file_count": 0,
        "dir_count": 0,
        "symlink_count": 0,
        "total_bytes": 0,
        "categories": {},
        "entries": [],
    }
    if not resolved.exists():
        return report

    categories: dict[str, int] = {}
    entries: list[dict[str, object]] = []
    entries_on_disk = sorted(
        resolved.rglob("*"), key=lambda item: item.relative_to(resolved).as_posix()
    )
    for current in entries_on_disk:
        relative = current.relative_to(resolved).as_posix()
        info = current.lstat()
        category = classify_entry(relative, info.st_mode)
        categories[category] = categories.get(category, 0) + 1
        mode_text = oct(stat.S_IMODE(info.st_mode))
        if stat.S_ISDIR(info.st_mode):
            report["dir_count"] = int(report["dir_count"]) + 1
            continue
        if stat.S_ISLNK(info.st_mode):
            report["symlink_count"] = int(report["symlink_count"]) + 1
            target, redacted = describe_symlink(current)
            entries.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "category": category,
                    "mode": mode_text,
                    "target": target,
                    "target_redacted": redacted,
                }
            )
            continue
        if not stat.S_ISREG(info.st_mode):
            entries.append(
                {
                    "path": relative,
                    "kind": "special",
                    "category": category,
                    "mode": mode_text,
                }
            )
            continue
        report["file_count"] = int(report["file_count"]) + 1
        report["total_bytes"] = int(report["total_bytes"]) + int(info.st_size)
        entries.append(
            {
                "path": relative,
                "kind": "file",
                "category": category,
                "mode": mode_text,
                "size_bytes": info.st_size,
                "sha256": digest_file(current),
            }
        )

    report["categories"] = dict(sorted(categories.items()))
    report["entries"] = entries
    return report


def build_report(roots: list[tuple[str, Path]]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "desktop_resource_report",
        "roots": [collect_root(label, root) for label, root in roots],
        "privacy": {
            "absolute_paths_emitted": False,
            "profile_contents_scanned": False,
            "absolute_symlink_targets_redacted": True,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        action="append",
        dest="roots",
        metavar="LABEL=PATH",
        required=True,
        help="package root to inventory; may be repeated",
    )
    parser.add_argument("--output", type=Path, help="write JSON to this file instead of stdout")
    args = parser.parse_args(argv)

    roots = [parse_root_spec(spec) for spec in args.roots]
    payload = build_report(roots)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
