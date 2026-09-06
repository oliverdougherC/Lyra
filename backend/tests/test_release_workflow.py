"""Execute release preflight diagnostics without GitHub credentials or network."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/release.yml"


@pytest.mark.parametrize(
    ("app_id", "app_key", "missing"),
    [
        ("", "", ("RELEASE_APP_ID", "RELEASE_APP_PRIVATE_KEY")),
        ("fixture-app-id", "", ("RELEASE_APP_PRIVATE_KEY",)),
        ("", "fixture-private-key", ("RELEASE_APP_ID",)),
        ("fixture-app-id", "fixture-private-key", ()),
    ],
)
def test_release_app_preflight_names_missing_configuration_without_exposing_values(
    app_id, app_key, missing
):
    workflow = yaml.safe_load(WORKFLOW.read_text())
    step = next(
        step
        for step in workflow["jobs"]["prepare"]["steps"]
        if step.get("name") == "Require release App configuration"
    )
    result = subprocess.run(  # noqa: S603 — run the repository-owned preflight verbatim.
        ["/bin/bash", "-e", "-c", step["run"]],
        env={**os.environ, "APP_ID": app_id, "APP_KEY": app_key},
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == (1 if missing else 0)
    assert output.count("::error::") == len(missing)
    for name in missing:
        assert name in output
    assert "fixture-app-id" not in output
    assert "fixture-private-key" not in output
    if missing:
        assert "docs/releasing.md" in output
