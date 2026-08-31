"""Smoke-check the Python modules and package resources a packaged desktop build needs."""

from __future__ import annotations

import argparse
import importlib
from collections.abc import Sequence
from importlib import resources
from pathlib import Path

MODULES: tuple[str, ...] = (
    "backend.config",
    "backend.main",
    "backend.core.diagnostics",
    "backend.core.exa",
    "backend.core.web_research",
    "backend.storage.database",
    "backend.storage.secrets",
)


def _materialize(resource: object) -> Path:
    path = Path(str(resource))
    if not path.exists():
        raise FileNotFoundError(f"missing packaged resource: {path}")
    return path


def run_smoke() -> dict[str, object]:
    imported: list[str] = []
    for module_name in MODULES:
        importlib.import_module(module_name)
        imported.append(module_name)

    migrations_root = _materialize(resources.files("backend.storage").joinpath("migrations"))
    migration_names = sorted(
        path.name for path in migrations_root.iterdir() if path.suffix == ".sql"
    )
    if not migration_names:
        raise FileNotFoundError("no SQLite migrations were found")

    prompts_module = _materialize(resources.files("backend.llm").joinpath("prompts.py"))

    return {
        "modules": imported,
        "migration_count": len(migration_names),
        "latest_migration": migration_names[-1],
        "prompts_module": prompts_module.name,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    result = run_smoke()
    print("Packaged Python smoke passed.")
    print(f"Imported modules: {', '.join(result['modules'])}")
    print(
        "Resources: "
        f"{result['migration_count']} migrations present, latest {result['latest_migration']}, "
        f"{result['prompts_module']} available"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
