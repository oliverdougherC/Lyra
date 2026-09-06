"""Reuse retained signed bytes on retries; never rebuild over uploaded assets."""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_artifacts import gh, validate  # noqa: E402


def main() -> None:
    repo, sha = os.environ["GITHUB_REPOSITORY"], os.environ["SOURCE_SHA"]
    version = Path("version.txt").read_text().strip()
    release = json.loads(gh("api", f"repos/{repo}/releases/tags/v{version}"))
    if not release["draft"]:
        raise SystemExit("Already published; use promote retry to repair the channel only")
    candidates = json.loads(
        gh(
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repo}/actions/artifacts?per_page=100",
        )
    )
    for page in candidates:
        for artifact in page["artifacts"]:
            if (
                not artifact["name"].startswith("lyra-signed-candidate-")
                or artifact["expired"]
                or artifact["workflow_run"]["head_sha"] != sha
            ):
                continue
            with tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                gh(
                    "run",
                    "download",
                    str(artifact["workflow_run"]["id"]),
                    "--name",
                    artifact["name"],
                    "--dir",
                    temp,
                )
                try:
                    info = validate(directory)
                except (ValueError, OSError, KeyError):
                    continue
                if info["source"] != sha or info["version"] != version:
                    continue
                shutil.copytree(directory, Path("release-assets"))
                with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
                    stream.write("restored=true\n")
                return
    if release["assets"]:
        raise SystemExit(
            "Draft contains bytes but the matching retained candidate is unavailable; "
            "do not rebuild or overwrite. Recover the original candidate artifact."
        )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
        stream.write("restored=false\n")


if __name__ == "__main__":
    main()
