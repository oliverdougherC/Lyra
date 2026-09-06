"""Resolve a versioned draft only after exact main SHA CI succeeds."""

import json
import os
import subprocess
import time
from pathlib import Path


def gh(*args: str):
    return json.loads(subprocess.check_output(["gh", "api", *args], text=True))  # noqa: S603,S607


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    version = Path("version.txt").read_text().strip()
    triggering_sha = os.environ["GITHUB_SHA"]
    release = gh(f"repos/{repo}/releases/tags/v{version}")
    ref = gh(f"repos/{repo}/git/ref/tags/v{version}")
    sha = ref["object"]["sha"]
    if ref["object"]["type"] != "commit":
        raise SystemExit("Candidate tag must identify this exact trusted main commit")
    if os.environ["GITHUB_EVENT_NAME"] == "push" and sha != triggering_sha:
        raise SystemExit("New release must refer to its triggering main commit")
    subprocess.run(["git", "merge-base", "--is-ancestor", sha, "origin/main"], check=True)  # noqa: S603,S607
    if release["target_commitish"] != sha:
        raise SystemExit("Draft target must be the exact release commit SHA")
    # A bot-created tag need not trigger anything: this job is a direct continuation.
    for _ in range(120):
        runs = gh(
            f"repos/{repo}/actions/workflows/ci.yml/runs?head_sha={sha}&event=push&per_page=100"
        )["workflow_runs"]
        runs = [run for run in runs if run["head_branch"] == "main"]
        if runs and runs[0]["status"] == "completed":
            if runs[0]["conclusion"] != "success":
                raise SystemExit("Exact main revision CI failed")
            break
        time.sleep(10)
    else:
        raise SystemExit("Timed out awaiting exact main revision CI; retry workflow")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
        stream.write(f"ready=true\nversion={version}\nsha={sha}\n")


if __name__ == "__main__":
    main()
