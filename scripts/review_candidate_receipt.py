"""Produce explicit unsigned CI review receipts; never release or updater credentials."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

try:
    from scripts.release_metadata import metadata, write_bundle_contract
except ModuleNotFoundError:
    from release_metadata import metadata, write_bundle_contract

ARTIFACT_NAME = "lyra-desktop-macos-unsigned-review"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_identity(
    checkout_sha: str, event_sha: str, pr_head_sha: str | None, pr_base_sha: str | None = None
) -> dict:
    for sha in (checkout_sha, event_sha, pr_head_sha, pr_base_sha):
        if sha is not None and re.fullmatch(r"[a-f0-9]{40}", sha) is None:
            raise ValueError("Expected exact 40-character source SHAs")
    if checkout_sha != (pr_head_sha or event_sha):
        raise ValueError("Checkout SHA differs from the exact candidate head")
    return {
        "checkout_sha": checkout_sha,
        "workflow_event_sha": event_sha,
        "pull_request_head_sha": pr_head_sha,
        "pull_request_base_sha": pr_base_sha,
        "checkout_kind": "pull-request-head" if pr_head_sha else "branch-commit",
    }


def receipt(directory: Path, dmg: Path, identity: dict, version: str, run_url: str) -> dict:
    if not dmg.is_file() or dmg.stat().st_size == 0 or dmg.parent != directory:
        raise ValueError("Expected a nonempty DMG inside the review artifact directory")
    contract = json.loads((directory / "lyra-release.json").read_text())
    if contract["source"] != identity["checkout_sha"] or contract["version"] != version:
        raise ValueError("Bundle contract does not describe the built checkout")
    info = {
        "format": 1,
        "status": "UNSIGNED_REVIEW_ONLY",
        "distribution_ready": False,
        "developer_id_signed": False,
        "notarized": False,
        "updater_artifact": False,
        **metadata(version),
        **identity,
        "workflow_run_url": run_url,
        "actions_artifact_name": ARTIFACT_NAME,
        "access": "GitHub Actions artifact; GitHub authentication may be required",
        "installer": {"filename": dmg.name, "bytes": dmg.stat().st_size, "sha256": sha256(dmg)},
    }
    (directory / "candidate-receipt.json").write_text(json.dumps(info, indent=2) + "\n")
    (directory / "README.txt").write_text(
        "UNSIGNED REVIEW CANDIDATE — NOT A DISTRIBUTABLE BETA\n\n"
        "This DMG has no Developer ID signing or notarization proof. Gatekeeper may block it.\n"
        "It is not an updater artifact and must not be published to a release channel.\n"
        "The app was built from checkout_sha in candidate-receipt.json. On pull requests,\n"
        "this is the exact PR head. workflow_event_sha records GitHub's separate CI test merge.\n"
        "The receipt also records the PR base SHA. No signing secrets were used.\n"
        "Verify the downloaded files using: shasum -a 256 -c SHA256SUMS\n"
        f"Workflow and artifact access: {run_url}\n"
    )
    paths = sorted(p for p in directory.iterdir() if p.is_file() and p.name != "SHA256SUMS")
    (directory / "SHA256SUMS").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in paths))
    return info


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "receipt"))
    parser.add_argument("--app", type=Path)
    parser.add_argument("--dmg", type=Path)
    parser.add_argument("--directory", type=Path, default=Path("review-artifact"))
    args = parser.parse_args()
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()  # noqa: S603,S607
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    head = event.get("pull_request", {}).get("head", {}).get("sha")
    base = event.get("pull_request", {}).get("base", {}).get("sha")
    identity = source_identity(checkout, os.environ["GITHUB_SHA"], head, base)
    version = Path("version.txt").read_text().strip()
    args.directory.mkdir(parents=True, exist_ok=True)
    if args.action == "prepare":
        if args.app is None:
            parser.error("prepare requires --app")
        write_bundle_contract(args.app, version, checkout)
        contract = args.app / "Contents/Resources/lyra-release.json"
        (args.directory / contract.name).write_bytes(contract.read_bytes())
        (args.app / "Contents/Resources/UNSIGNED-REVIEW.json").write_text(
            json.dumps({"status": "UNSIGNED_REVIEW_ONLY", **identity}, indent=2) + "\n"
        )
    else:
        if args.dmg is None:
            parser.error("receipt requires --dmg")
        run_url = (
            f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/"
            f"actions/runs/{os.environ['GITHUB_RUN_ID']}"
        )
        print(json.dumps(receipt(args.directory, args.dmg, identity, version, run_url)))


if __name__ == "__main__":
    main()
