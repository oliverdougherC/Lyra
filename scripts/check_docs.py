"""Check local Markdown link destinations without network access or dependencies.

Checks tracked Markdown plus new root/docs Markdown files. Historical documents are
included: their links must still lead somewhere. URL availability and heading anchors
are deliberately outside this deterministic file-existence check.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)")
REFERENCE = re.compile(r"^\s*\[[^\]]+\]:\s*(<[^>]+>|\S+)", re.MULTILINE)


def broken_links(path: Path, root: Path) -> list[str]:
    text = re.sub(r"```.*?```|~~~.*?~~~", "", path.read_text(), flags=re.DOTALL)
    findings = []
    for pattern in (LINK, REFERENCE):
        for match in pattern.finditer(text):
            target = match.group(1).strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            destination = (root if parsed.path.startswith("/") else path.parent) / unquote(
                parsed.path.lstrip("/")
            )
            if not destination.exists():
                findings.append(f"{path.relative_to(root)}: missing link destination {target}")
    return findings


def main() -> int:
    tracked = (
        subprocess.check_output(  # noqa: S603,S607
            ["git", "ls-files", "-z", "--", "*.md"],  # noqa: S607
            cwd=ROOT,
        )
        .decode()
        .split("\0")
    )
    paths = {ROOT / name for name in tracked if name}
    paths.update(ROOT.glob("*.md"))
    paths.update((ROOT / "docs").rglob("*.md"))
    findings = [
        finding for path in sorted(paths) if path.is_file() for finding in broken_links(path, ROOT)
    ]
    if findings:
        print("\n".join(findings))
        return 1
    print(f"Local Markdown link destinations checked in {len(paths)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
