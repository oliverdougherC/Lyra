"""Fail when active docs or workflow files still mention retired runtime surfaces.

This scan is intentionally narrow: it covers only the files that define the current
desktop-migration contract. Historical records are excluded so they can retain accurately
labelled Firecrawl/Next.js references.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_FILES: tuple[Path, ...] = (
    ROOT / "README.md",
    ROOT / "docs" / "architecture.md",
    ROOT / "docs" / "local-deployment.md",
    ROOT / "docs" / "privacy-and-data-location.md",
    ROOT / "docs" / "troubleshooting.md",
    ROOT / "docs" / "feature-roadmap.md",
    ROOT / "docs" / "security-and-ci-gates.md",
    ROOT / "docs" / "macos-apple-silicon-release-checklist.md",
    ROOT / "docs" / "contributing-testing-migrations.md",
    ROOT / "docs" / "phase-4-threat-model.md",
    ROOT / "infra" / "README.md",
    ROOT / ".github" / "workflows" / "ci.yml",
    ROOT / ".github" / "workflows" / "webkit-acceptance.yml",
    ROOT / "scripts" / "run-acceptance.sh",
    ROOT / "scripts" / "lyra_launcher.py",
    ROOT / "frontend" / "package.json",
    ROOT / "frontend" / "src" / "lib" / "api.ts",
    ROOT / "frontend" / "src" / "components" / "settings" / "settings-form.tsx",
    ROOT / "backend" / "main.py",
    ROOT / "backend" / "core" / "exa.py",
    ROOT / "backend" / "core" / "web_research.py",
)


@dataclass(frozen=True)
class Rule:
    label: str
    pattern: re.Pattern[str]


RULES: tuple[Rule, ...] = (
    Rule("Firecrawl", re.compile(r"\bfirecrawl\b", re.IGNORECASE)),
    Rule("Docker", re.compile(r"\bdocker(?: desktop| engine)?\b", re.IGNORECASE)),
    Rule("Next.js", re.compile(r"\bnext\.js\b", re.IGNORECASE)),
    Rule("next build", re.compile(r"\bnext build\b", re.IGNORECASE)),
    Rule("next start", re.compile(r"\bnext start\b", re.IGNORECASE)),
    Rule("--skip-firecrawl", re.compile(r"--skip-firecrawl")),
)


def active_paths(raw_paths: Sequence[str] | None = None) -> list[Path]:
    if raw_paths:
        return [Path(path) for path in raw_paths]
    return list(ACTIVE_FILES)


def scan_text(path: Path, text: str, rules: Iterable[Rule] = RULES) -> list[str]:
    findings: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule in rules:
            if rule.pattern.search(line):
                findings.append(
                    f"{path}:{line_number}: stale {rule.label} reference: {line.strip()}"
                )
    return findings


def scan_paths(paths: Sequence[Path]) -> list[str]:
    findings: list[str] = []
    for path in paths:
        findings.extend(scan_text(path, path.read_text(encoding="utf-8")))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        help="optional explicit files to scan; defaults to the active desktop-migration surfaces",
    )
    args = parser.parse_args(argv)

    findings = scan_paths(active_paths(args.paths))
    if findings:
        print("\n".join(findings))
        return 1
    print("No stale active references found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
