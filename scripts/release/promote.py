"""Promote already signed draft bytes after release-promotion environment approval."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_artifacts import check_advance, gh, sha256, site, validate  # noqa: E402
from release_metadata import metadata, version_parts  # noqa: E402


def download(tag: str, directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    gh("release", "download", tag, "--dir", str(directory))
    return validate(directory)


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    info = metadata(Path("version.txt").read_text().strip())
    directory = Path("release-assets")
    candidate = download(info["tag"], directory)
    if candidate["source"] != os.environ["SOURCE_SHA"]:
        raise SystemExit("Candidate source differs from trusted revision")
    if json.loads((directory / "frozen-smoke.json").read_text())["status"] != "passed":
        raise SystemExit("Final frozen backend smoke did not pass")
    releases = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repo}/releases?per_page=100"))
    published = [release for page in releases for release in page if not release["draft"]]
    # Reconstruct both channels from immutable assets, including beta-only repositories.
    selected: dict[str, dict] = {}
    for release in published:
        try:
            release_info = metadata(release["tag_name"].removeprefix("v"))
        except ValueError:
            continue
        channel = release_info["channel"]
        current = selected.get(channel)
        if current is None or version_parts(release_info["version"]) > version_parts(
            current["version"]
        ):
            selected[channel] = release_info
    output = Path("public-release")
    for channel, current in selected.items():
        with tempfile.TemporaryDirectory() as temp:
            previous_dir = Path(temp)
            previous = download(current["tag"], previous_dir)
            if channel == candidate["channel"]:
                check_advance(previous, candidate)
            site(previous_dir, output)
    release = json.loads(gh("api", f"repos/{repo}/releases/tags/{info['tag']}"))
    if release["draft"]:
        gh("release", "edit", info["tag"], "--draft=false", "--latest=false")
    # No auth header or token goes to the public URLs. Verify every published byte
    # before the static channel can change. A retry uses these identical assets.
    for name, expected in candidate["sha256"].items():
        url = f"https://github.com/{repo}/releases/download/{info['tag']}/{name}"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / name
            subprocess.run(  # noqa: S603,S607
                [
                    "/usr/bin/curl",
                    "--fail",
                    "--location",
                    "--silent",
                    "--show-error",
                    "--retry",
                    "5",
                    "--retry-all-errors",
                    "--output",
                    str(path),
                    url,
                ],
                check=True,
            )
            if sha256(path) != expected:
                raise SystemExit("Anonymous released asset checksum mismatch")
    site(directory, output)


if __name__ == "__main__":
    main()
