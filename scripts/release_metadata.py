"""Derive every packaged version from version.txt; no release credentials required."""

import argparse
import json
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def version_parts(version: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(
        r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-beta\.(0|[1-9]\d*))?", version
    )
    if not match:
        raise ValueError("Expected MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH-beta.N")
    major, minor, patch = (int(match[i]) for i in (1, 2, 3))
    beta = int(match[4]) if match[4] is not None else 98
    if major > 98 or minor > 98 or patch > 99 or beta > 98:
        raise ValueError("Version exceeds the documented macOS build-number bounds")
    if match[4] is not None and beta >= 98:
        raise ValueError("Beta sequence exhausted; advance patch before beta.98")
    return major, minor, patch, beta


def build_number(version: str) -> str:
    major, minor, patch, beta = version_parts(version)
    return f"{major * 100 + minor + 1}.{patch}.{beta + 1}"


def metadata(version: str) -> dict:
    version_parts(version)
    return {
        "version": version,
        "build": build_number(version),
        "channel": "beta" if "-beta." in version else "stable",
        "tag": f"v{version}",
    }


def synchronize(root: Path, version: str) -> None:
    info = metadata(version)
    for relative in ("src-tauri/tauri.conf.json", "frontend/package.json"):
        path = root / relative
        value = json.loads(path.read_text())
        value["version"] = version
        if relative.startswith("src-tauri"):
            value["bundle"]["macOS"]["bundleVersion"] = info["build"]
            value["bundle"]["macOS"]["minimumSystemVersion"] = "14.0"
        path.write_text(json.dumps(value, indent=2) + "\n")
    backend_version = root / "backend/version.py"
    backend_version.parent.mkdir(parents=True, exist_ok=True)
    backend_version.write_text(
        "# Generated from version.txt by scripts/release_metadata.py.\n"
        f'VERSION = "{version}"\nBUILD = "{info["build"]}"\n'
    )
    python_version = version.replace("-beta.", "b")
    for relative, name, new_version in (
        ("src-tauri/Cargo.toml", "lyra-desktop", version),
        ("src-tauri/Cargo.lock", "lyra-desktop", version),
        ("pyproject.toml", "lyra", python_version),
        ("uv.lock", "lyra", python_version),
    ):
        path = root / relative
        content, count = re.subn(
            rf'(name = "{name}"\nversion = ")[^"]+(" )?',
            lambda match, replacement=new_version: match[1] + replacement + (match[2] or ""),
            path.read_text(),
            count=1,
        )
        if count != 1:
            raise ValueError(f"Missing package metadata: {relative}")
        path.write_text(content)


def write_bundle_contract(app: Path, version: str, source: str, root: Path = ROOT) -> None:
    if not re.fullmatch(r"[a-f0-9]{40}", source):
        raise ValueError("A signed release contract requires the exact source commit SHA")
    info = metadata(version)
    schema = max(
        int(path.name.split("_", 1)[0])
        for path in (root / "backend/storage/migrations").glob("*.sql")
    )
    contract = {
        "version": info["version"],
        "build": info["build"],
        "source": source,
        "bundleIdentifier": "com.lyra.desktop",
        "architecture": "aarch64",
        "schemaMin": 0,
        "schemaMax": schema,
    }
    destination = app / "Contents/Resources/lyra-release.json"
    if not destination.parent.is_dir():
        raise ValueError("Expected a completed Lyra.app before writing its release contract")
    destination.write_text(json.dumps(contract, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--source")
    args = parser.parse_args()
    version = (ROOT / "version.txt").read_text().strip()
    if args.sync:
        synchronize(ROOT, version)
    if args.check:
        paths = (
            "src-tauri/tauri.conf.json",
            "frontend/package.json",
            "src-tauri/Cargo.toml",
            "src-tauri/Cargo.lock",
            "pyproject.toml",
            "uv.lock",
            "backend/version.py",
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in paths:
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((ROOT / relative).read_bytes())
            synchronize(root, version)
            if any((root / p).read_bytes() != (ROOT / p).read_bytes() for p in paths):
                raise SystemExit(
                    "Generated versions differ from version.txt; run release_metadata.py --sync"
                )
    if args.bundle:
        write_bundle_contract(args.bundle, version, args.source or "")
    print(json.dumps(metadata(version)))


if __name__ == "__main__":
    main()
