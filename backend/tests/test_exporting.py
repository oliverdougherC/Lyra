"""The PDF export: two bounded subprocess stages, honest about what is missing.

The binaries are stubbed throughout - the suite must pass on a machine with neither
pandoc nor typst - so what is under test is the orchestration: availability naming the
missing binary, the argument vectors, the working directory, and every failure mode
arriving as a LyraError that names its stage.
"""

import subprocess
from pathlib import Path

import pytest

from backend.core import exporting
from backend.core.errors import LyraError


def _which(present: set[str]):
    return lambda name: f"/opt/homebrew/bin/{name}" if name in present else None


def test_availability_names_the_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exporting.shutil, "which", _which({"pandoc", "typst"}))
    assert exporting.export_available() is None

    monkeypatch.setattr(exporting.shutil, "which", _which({"typst"}))
    assert "pandoc" in str(exporting.export_available())

    monkeypatch.setattr(exporting.shutil, "which", _which({"pandoc"}))
    assert "typst" in str(exporting.export_available())


def test_render_runs_both_stages_in_a_throwaway_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(exporting.shutil, "which", _which({"pandoc", "typst"}))
    runs: list[tuple[list[str], Path]] = []

    def fake_run(command, cwd, **kwargs):
        runs.append((command, cwd))
        if command[0] == "typst":
            (cwd / "draft.pdf").write_bytes(b"%PDF-fake")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(exporting.subprocess, "run", fake_run)

    pdf = exporting.render_pdf("# Title\n\nProse.\n", "Lab 3", "ECE 203")

    assert pdf == b"%PDF-fake"
    (pandoc_command, pandoc_cwd), (typst_command, typst_cwd) = runs
    assert pandoc_command[0] == "pandoc" and "--standalone" in pandoc_command
    assert "title=Lab 3" in pandoc_command and "author=ECE 203" in pandoc_command
    assert typst_command[:2] == ["typst", "compile"]
    # Both stages share the throwaway directory, and typst is rooted inside it so the
    # document cannot read outside however its markdown is shaped.
    assert pandoc_cwd == typst_cwd
    assert str(typst_cwd) in typst_command
    # The source the stages read is the body as given.
    assert (pandoc_cwd / "draft.md").exists() is False  # the directory is gone after


def test_a_missing_binary_refuses_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(exporting.shutil, "which", _which(set()))

    def must_not_run(*args, **kwargs):
        raise AssertionError("no subprocess may start without the binaries")

    monkeypatch.setattr(exporting.subprocess, "run", must_not_run)
    with pytest.raises(LyraError, match="pandoc"):
        exporting.render_pdf("x", "T", "C")


def test_a_failed_stage_reports_its_stage_and_last_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(exporting.shutil, "which", _which({"pandoc", "typst"}))

    def failing_run(command, cwd, **kwargs):
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="error: unclosed delimiter\n"
        )

    monkeypatch.setattr(exporting.subprocess, "run", failing_run)

    with pytest.raises(LyraError, match="converting the document.*unclosed delimiter"):
        exporting.render_pdf("x", "T", "C")


def test_a_hung_stage_is_cut_and_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exporting.shutil, "which", _which({"pandoc", "typst"}))

    def hanging_run(command, cwd, **kwargs):
        raise subprocess.TimeoutExpired(command, exporting.STAGE_TIMEOUT_SECONDS)

    monkeypatch.setattr(exporting.subprocess, "run", hanging_run)

    with pytest.raises(LyraError, match="too long"):
        exporting.render_pdf("x", "T", "C")


def test_citation_markers_render_in_first_use_order_with_a_sources_section() -> None:
    body = "A claim [@lyra:8], then [@lyra-3], and source eight again [source:8]."
    rendered = exporting.render_citations(
        body,
        [
            {"id": 3, "title": "Course reader"},
            {
                "id": 8,
                "title": "Primary study",
                "url": "https://example.test/study",
                "accessed_at": "2026-08-07",
            },
        ],
    )

    assert "A claim [1], then [2], and source eight again [1]." in rendered
    assert "1. Primary study. https://example.test/study. Accessed 2026-08-07." in rendered
    assert "2. Course reader." in rendered


def test_unknown_citation_marker_stays_visible() -> None:
    rendered = exporting.render_citations("Claim [@lyra:99].", [{"id": 1, "title": "Known"}])
    assert rendered == "Claim [@lyra:99]."
