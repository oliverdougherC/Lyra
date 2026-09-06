"""Collect installed dependency license metadata and notices for review and bundling.

The inventory conservatively includes build dependencies; inclusion is not a claim
that every listed package executes in Lyra. It never chooses Lyra's project license.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import subprocess
from pathlib import Path


def license_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.iterdir()
        if path.is_file()
        and path.name.lower().startswith(("license", "licence", "copying", "notice", "copyright"))
    )


def collect(output: Path, frontend_inventory: Path, rust_inventory: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "LYRA-LICENSE.txt").write_bytes(Path("LICENSE").read_bytes())
    for notice in Path("packaging/notices").glob("*.txt"):
        (output / notice.name).write_bytes(notice.read_bytes())
    inventory = []
    notices = ["THIRD-PARTY NOTICES\n\nConservative installed build-input inventory.\n"]

    def add(ecosystem: str, name: str, version: str, license_name: str, files: list[Path]) -> None:
        inventory.append(
            {"ecosystem": ecosystem, "name": name, "version": version, "license": license_name}
        )
        notices.append(f"\n{'=' * 72}\n{ecosystem}: {name} {version}\n{license_name}\n")
        for file in files:
            notices.append(f"\n--- {file.name} ---\n{file.read_text(errors='replace')}\n")

    for dist in sorted(importlib.metadata.distributions(), key=lambda d: d.name.lower()):
        if dist.name.lower() == "lyra":
            continue
        files = [
            Path(dist.locate_file(file))
            for file in dist.files or []
            if Path(file)
            .name.lower()
            .startswith(("license", "licence", "copying", "notice", "copyright"))
            and Path(dist.locate_file(file)).is_file()
        ]
        add(
            "python",
            dist.name,
            dist.version,
            dist.metadata.get("License-Expression") or dist.metadata.get("License") or "UNDECLARED",
            files,
        )
    frontend = json.loads(frontend_inventory.read_text())
    for packages in frontend.values():
        for package in packages:
            add(
                "frontend",
                package["name"],
                ", ".join(package["versions"]),
                package.get("license", "UNDECLARED"),
                [file for root in package["paths"] for file in license_files(Path(root))],
            )
    rust = json.loads(rust_inventory.read_text())
    for package in rust["packages"]:
        if package["name"] == "lyra-desktop":
            continue
        root = Path(package["manifest_path"]).parent
        files = license_files(root)
        if package.get("license_file"):
            declared = root / package["license_file"]
            if declared.is_file() and declared not in files:
                files.append(declared)
        add(
            "rust",
            package["name"],
            package["version"],
            package.get("license") or "UNDECLARED",
            files,
        )
    (output / "dependency-licenses.json").write_text(json.dumps(inventory, indent=2) + "\n")
    (output / "THIRD-PARTY-NOTICES.txt").write_text("".join(notices))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("src-tauri/resources/notices"))
    parser.add_argument("--frontend-inventory", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rust_inventory = args.output / "cargo-metadata.json"
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607 — developer tool from the build environment
            "cargo",
            "metadata",
            "--locked",
            "--format-version",
            "1",
            "--manifest-path",
            "src-tauri/Cargo.toml",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    # Source locations are only intermediate inputs, never bundled diagnostics.
    rust_inventory.write_text(result.stdout)
    try:
        collect(args.output, args.frontend_inventory, rust_inventory)
    finally:
        rust_inventory.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
