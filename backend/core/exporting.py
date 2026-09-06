"""PDF export: markdown through Pandoc into Typst, compiled to a typeset page.

Kuhn's renderer, ported: Pandoc converts the draft's markdown to Typst markup (which is
what makes LaTeX math survive the trip - a hand-rolled converter would mangle exactly
the content an engineering draft is made of), and the `typst` binary compiles it.
Both stages run as short-lived subprocesses in a throwaway directory, the same posture
as the solver's CAS runner: rendering someone's markdown is arbitrary code execution by
another name, so the child gets a temp dir for a working directory, a wall-clock
ceiling, and an argument vector no part of which comes from the document - the document
travels as a file.

Both binaries are the user's own installs. When either is missing the export honestly
says which, and the workspace keeps its Print button instead - a stopgap the settings
of a local-first app should not paper over by downloading executables.
"""

import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

from backend.core import mathnorm
from backend.core.errors import LyraError

# Wall-clock ceiling per stage. Compiling a long draft is seconds; a minute means a
# hung child, and a hung child must cost one export rather than the worker thread.
STAGE_TIMEOUT_SECONDS = 60

PANDOC_MISSING = (
    "PDF export needs pandoc, which was not found. Install it (brew install pandoc), or use Print."
)
TYPST_MISSING = (
    "PDF export needs typst, which was not found. Install it (brew install typst), or use Print."
)
_STAGE_TIMEOUT_ERROR = "The export took too long and was stopped."
_STAGE_FAILED_ERROR = "The export failed while {stage}: {detail}"


def export_available() -> str | None:
    """None when both binaries exist, else the message naming the missing one."""
    if shutil.which("pandoc") is None:
        return PANDOC_MISSING
    if shutil.which("typst") is None:
        return TYPST_MISSING
    return None


# ``[source:N]`` is accepted as a compatibility spelling for drafts produced during
# the writer-overhaul transition; new prompts emit the Pandoc-like ``[@lyra:N]`` form.
_CITATION_MARKER = re.compile(r"(?:\[@lyra(?::|-)|\[source:)(\d+)\]")


def render_citations(body: str, sources: Sequence[Mapping[str, object]] | None = None) -> str:
    """Turn stable Lyra source markers into readable references for Pandoc.

    The editor stores a source identity, not a display number: display numbers are
    assigned by first use at export time, so deleting an unused ledger row never
    renumbers the prose on disk. Unknown ids remain visible instead of silently citing
    an unrelated source.
    """
    if not sources:
        return body
    by_id = {int(source["id"]): source for source in sources if source.get("id") is not None}
    order: list[int] = []

    def replace(match: re.Match[str]) -> str:
        source_id = int(match.group(1))
        if source_id not in by_id:
            return match.group(0)
        if source_id not in order:
            order.append(source_id)
        return f"[{order.index(source_id) + 1}]"

    rendered = _CITATION_MARKER.sub(replace, body)
    if not order:
        return rendered
    entries = ["## Sources", ""]
    for number, source_id in enumerate(order, start=1):
        source = by_id[source_id]
        title = str(source.get("title") or "Untitled source").strip()
        url = str(source.get("url") or "").strip()
        accessed = str(source.get("accessed_at") or source.get("access_date") or "").strip()
        detail = title
        if url:
            detail += f". {url}"
        revisions = {
            (excerpt.get("supporting_revision"), excerpt.get("supporting_accessed_at"))
            for excerpt in source.get("excerpts", [])
            if isinstance(excerpt, Mapping) and excerpt.get("supporting_revision") is not None
        }
        unavailable = any(
            isinstance(excerpt, Mapping) and excerpt.get("evidence_unavailable")
            for excerpt in source.get("excerpts", [])
        )
        if unavailable:
            detail += ". Some historical supporting snapshots are unavailable"
        if revisions:
            references = [
                f"revision {revision}" + (f" (saved {date})" if date else "")
                for revision, date in sorted(revisions, key=lambda value: int(value[0]))
            ]
            detail += ". Supporting saved " + ", ".join(references)
        elif accessed and not unavailable:
            detail += f". Accessed {accessed}"
        entries.append(f"{number}. {detail}.")
    return rendered.rstrip() + "\n\n" + "\n".join(entries) + "\n"


def render_pdf(
    body: str,
    title: str,
    class_name: str,
    sources: Sequence[Mapping[str, object]] | None = None,
) -> bytes:
    """The draft as a typeset PDF: title, class, and date over the document.

    Raises:
        LyraError: when a binary is missing, a stage fails, or a stage hangs. The
            message names the stage, because "export failed" sends the student
            debugging the wrong half.
    """
    blocked = export_available()
    if blocked is not None:
        raise LyraError(blocked)

    with tempfile.TemporaryDirectory(prefix="lyra-export-") as workdir:
        work = Path(workdir)
        normalized = mathnorm.normalize(render_citations(body, sources))
        (work / "draft.md").write_text(normalized, encoding="utf-8")
        # Pandoc's standalone Typst template renders the metadata as the title block,
        # which is the one clean default template this export ships with.
        _run(
            [
                "pandoc",
                "draft.md",
                "--standalone",
                "--to",
                "typst",
                "--output",
                "draft.typ",
                "--metadata",
                f"title={title}",
                "--metadata",
                f"author={class_name}",
                "--metadata",
                f"date={date.today().isoformat()}",
            ],
            work,
            stage="converting the document",
        )
        # `--root` pins file resolution inside the throwaway directory, so a document
        # cannot read outside it however its markdown is shaped.
        _run(
            ["typst", "compile", "--root", str(work), "draft.typ", "draft.pdf"],
            work,
            stage="typesetting the PDF",
        )
        return (work / "draft.pdf").read_bytes()


def _run(command: list[str], work: Path, *, stage: str) -> None:
    """One stage as a bounded child in the throwaway directory.

    Raises:
        LyraError: on a non-zero exit or a hang, with the child's own last words.
    """
    try:
        completed = subprocess.run(  # noqa: S603
            # The argument vector is module constants plus metadata Lyra itself wrote;
            # the document travels as a file in the working directory, where it cannot
            # become an argument.
            command,
            cwd=work,
            capture_output=True,
            text=True,
            timeout=STAGE_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise LyraError(_STAGE_TIMEOUT_ERROR) from exc
    except OSError as exc:
        raise LyraError(_STAGE_FAILED_ERROR.format(stage=stage, detail=exc)) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise LyraError(
            _STAGE_FAILED_ERROR.format(stage=stage, detail=detail[-1] if detail else "no output")
        )
