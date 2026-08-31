"""Declarative component inventory for the frozen desktop backend package."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

SCHEMA_VERSION = 1
ENTRY_MODULE = "backend.desktop_entry"
SPEC_PATH = "packaging/lyra_backend.spec"
DATA_GLOBS = ("backend/storage/migrations/*.sql",)
DYNAMIC_LIB_PACKAGES = ("pymupdf",)
EXTENSION_DATA_PACKAGES = ("sqlite_vec",)
HIDDENIMPORT_PACKAGES = (
    "keyring.backends",
    "uvicorn",
    "uvicorn.loops",
    "uvicorn.lifespan",
    "uvicorn.protocols",
    "sympy",
    "pint",
)


def build_inventory() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "entry_module": ENTRY_MODULE,
        "spec_path": SPEC_PATH,
        "data_globs": list(DATA_GLOBS),
        "dynamic_lib_packages": list(DYNAMIC_LIB_PACKAGES),
        "extension_data_packages": list(EXTENSION_DATA_PACKAGES),
        "hiddenimport_packages": list(HIDDENIMPORT_PACKAGES),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write JSON to this file instead of stdout")
    args = parser.parse_args(argv)
    encoded = json.dumps(build_inventory(), indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
