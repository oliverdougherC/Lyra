"""Validate staged assets and advance static channels without replacing releases."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path

from release_metadata import metadata, version_parts


def gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True).strip()  # noqa: S603,S607


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_advance(previous: dict | None, candidate: dict) -> None:
    if previous is None:
        return
    if previous["channel"] != candidate["channel"]:
        raise ValueError("Channel mismatch")
    old, new = version_parts(previous["version"]), version_parts(candidate["version"])
    if new < old:
        raise ValueError("Refusing channel downgrade")
    if new == old and previous != candidate:
        raise ValueError("Version collision: immutable provenance differs")


def assemble(directory: Path, version: str, source: str, schema: int) -> dict:
    info = metadata(version)
    if len(source) != 40 or any(c not in "0123456789abcdef" for c in source):
        raise ValueError("Expected exact source SHA")
    dmg = directory / f"Lyra_{version}_aarch64.dmg"
    archive = directory / "Lyra.app.tar.gz"
    signature = directory / "Lyra.app.tar.gz.sig"
    for path in (dmg, archive, signature):
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Missing release asset: {path.name}")
    info.update(
        {
            "source": source,
            "schemaMin": 0,
            "schemaMax": schema,
            "workflow": os.environ.get("GITHUB_RUN_ID", "local-unverified"),
            "sha256": {p.name: sha256(p) for p in (dmg, archive, signature)},
        }
    )
    (directory / "provenance.json").write_text(json.dumps(info, indent=2) + "\n")
    base = f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/releases/download/{info['tag']}"
    feed = {
        "version": version,
        "notes": f"Lyra {version}; see versioned release notes.",
        "platforms": {
            "darwin-aarch64": {
                "url": f"{base}/{archive.name}",
                "signature": signature.read_text().strip(),
            }
        },
        "lyra": {
            "bundleIdentifier": "com.lyra.desktop",
            "architecture": "aarch64",
            "schemaMin": 0,
            "schemaMax": schema,
            "size": archive.stat().st_size,
        },
    }
    (directory / "latest.json").write_text(json.dumps(feed, indent=2) + "\n")
    return info


def required_assets(version: str) -> set[str]:
    return {
        f"Lyra_{version}_aarch64.dmg",
        "Lyra.app.tar.gz",
        "Lyra.app.tar.gz.sig",
        "provenance.json",
        "latest.json",
        "SHA256SUMS",
        "RELEASE_NOTES.md",
        "distribution-signing.json",
        "app-signing.txt",
        "frozen-smoke.json",
        "updater-signature-verification.txt",
        "native-inventory.json",
    }


def checksum_payload(directory: Path) -> None:
    paths = sorted(p for p in directory.iterdir() if p.is_file() and p.name != "SHA256SUMS")
    (directory / "SHA256SUMS").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in paths))


def validate(directory: Path) -> dict:
    info = json.loads((directory / "provenance.json").read_text())
    required = required_assets(info["version"])
    present = {path.name for path in directory.iterdir() if path.is_file()}
    if required != present:
        raise ValueError("Release payload has missing or unexpected assets")
    if any((directory / name).stat().st_size == 0 for name in required):
        raise ValueError("Release payload contains an empty asset")
    expected_metadata = metadata(info["version"])
    if any(info[key] != value for key, value in expected_metadata.items()):
        raise ValueError("Build identity mismatch")
    expected_binary_names = {
        f"Lyra_{info['version']}_aarch64.dmg",
        "Lyra.app.tar.gz",
        "Lyra.app.tar.gz.sig",
    }
    if set(info["sha256"]) != expected_binary_names:
        raise ValueError("Missing binary checksums")
    for name, digest in info["sha256"].items():
        if sha256(directory / name) != digest:
            raise ValueError("Artifact checksum mismatch")
    checked = set()
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        if name not in required - {"SHA256SUMS"} or name in checked:
            raise ValueError("Invalid payload checksum entries")
        if sha256(directory / name) != digest:
            raise ValueError("Staged payload checksum mismatch")
        checked.add(name)
    if checked != required - {"SHA256SUMS"}:
        raise ValueError("Incomplete payload checksums")
    feed = json.loads((directory / "latest.json").read_text())
    contract_keys = ("bundleIdentifier", "architecture", "schemaMin", "schemaMax")
    expected_contract = {
        "bundleIdentifier": "com.lyra.desktop",
        "architecture": "aarch64",
        "schemaMin": info["schemaMin"],
        "schemaMax": info["schemaMax"],
    }
    expected_url = (
        f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/releases/download/"
        f"{info['tag']}/Lyra.app.tar.gz"
    )
    if (
        feed["version"] != info["version"]
        or set(feed["platforms"]) != {"darwin-aarch64"}
        or feed["platforms"]["darwin-aarch64"]["url"] != expected_url
        or feed["platforms"]["darwin-aarch64"]["signature"]
        != (directory / "Lyra.app.tar.gz.sig").read_text().strip()
        or feed["lyra"]["size"] != (directory / "Lyra.app.tar.gz").stat().st_size
        or any(feed["lyra"][key] != expected_contract[key] for key in contract_keys)
    ):
        raise ValueError("Feed does not describe the staged signed archive")
    with tarfile.open(directory / "Lyra.app.tar.gz", "r:gz") as archive:
        matches = [
            member
            for member in archive.getmembers()
            if member.name == "Lyra.app/Contents/Resources/lyra-release.json"
        ]
        if len(matches) != 1 or not matches[0].isfile() or matches[0].size > 16384:
            raise ValueError("Signed inner release contract is missing or ambiguous")
        stream = archive.extractfile(matches[0])
        if stream is None:
            raise ValueError("Signed inner release contract cannot be read")
        inner = json.load(stream)
    expected_contract.update({key: info[key] for key in ("version", "build", "source")})
    if any(inner.get(key) != value for key, value in expected_contract.items()):
        raise ValueError("Signed inner contract differs from publication metadata")
    signing = json.loads((directory / "distribution-signing.json").read_text())
    if (
        not isinstance(signing, dict)
        or signing.get("mode") != "ad-hoc"
        or signing.get("developer_id_signed") is not False
        or signing.get("notarized") is not False
    ):
        raise ValueError("Distribution signing evidence must declare non-notarized ad-hoc signing")
    if json.loads((directory / "frozen-smoke.json").read_text()).get("status") != "passed":
        raise ValueError("Frozen backend smoke evidence did not pass")
    if json.loads((directory / "native-inventory.json").read_text()).get("status") != "passed":
        raise ValueError("Complete signed native inventory did not pass")
    if not re.fullmatch(
        r"Actual updater archive accepted by the installed parser \([1-9]\d* unpacked bytes\)\.\n"
        r"Updater archive signature verified against the retained Lyra public key\.",
        (directory / "updater-signature-verification.txt").read_text().strip(),
    ):
        raise ValueError(
            "Pinned updater signature and actual archive verification evidence is missing"
        )
    return info


def stage(directory: Path) -> None:
    info = validate(directory)
    release = json.loads(
        gh("api", f"repos/{os.environ['GITHUB_REPOSITORY']}/releases/tags/{info['tag']}")
    )
    if not release["draft"] or release["target_commitish"] != info["source"]:
        raise ValueError("Release must be a draft at the exact candidate SHA")
    existing = {item["name"]: item for item in release["assets"]}
    local = {p.name: p for p in directory.iterdir() if p.is_file()}
    if set(existing) - set(local):
        raise ValueError("Draft contains unexpected assets")
    for name, asset in existing.items():
        if asset.get("digest") != f"sha256:{sha256(local[name])}":
            raise ValueError("Existing draft asset differs; never overwrite an uploaded artifact")
    # Integrity manifest last makes partial uploads recoverable from retained candidate bytes.
    for name in sorted(set(local) - set(existing), key=lambda n: (n == "SHA256SUMS", n)):
        gh("release", "upload", info["tag"], str(local[name]))


def site(directory: Path, output: Path) -> None:
    info = validate(directory)
    channel = output / info["channel"]
    channel.mkdir(parents=True, exist_ok=True)
    previous = channel / "provenance.json"
    check_advance(json.loads(previous.read_text()) if previous.exists() else None, info)
    for name in ("latest.json", "provenance.json"):
        (channel / name).write_bytes((directory / name).read_bytes())
    url = (
        f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/releases/download/"
        f"{info['tag']}/Lyra_{info['version']}_aarch64.dmg"
    )
    (channel / "index.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Download Lyra</title>'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<h1>Lyra {info['channel']}</h1><p>Apple Silicon · macOS 14 or later</p>"
        f'<p><a href="{url}">Download Lyra {info["version"]}</a></p>'
        "<p>Open the DMG and drag Lyra to Applications. "
        "Tutor inference requires your own endpoint.</p>"
        "<p>Lyra is not signed with an Apple Developer ID and is not notarized. "
        "If macOS blocks the app, try opening Lyra, then go to System Settings &gt; "
        "Privacy &amp; Security and choose Open Anyway. Confirm Open when prompted.</p>"
        "</html>\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["assemble", "validate", "stage", "site"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--source")
    parser.add_argument("--schema", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "assemble":
        schema = args.schema
        if schema is None:
            migrations = Path(__file__).resolve().parents[1] / "backend/storage/migrations"
            schema = max(int(p.name.split("_", 1)[0]) for p in migrations.glob("*.sql"))
        assemble(args.directory, args.version, args.source, schema)
        checksum_payload(args.directory)
    elif args.action == "validate":
        print(json.dumps(validate(args.directory)))
    elif args.action == "stage":
        stage(args.directory)
    else:
        site(args.directory, args.output)


if __name__ == "__main__":
    main()
