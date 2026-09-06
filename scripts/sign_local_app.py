"""Sign a local macOS review bundle with a stable identity, then verify every code object."""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import subprocess
from pathlib import Path

MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}


def run(*args: str) -> str:
    return subprocess.run(  # noqa: S603 — fixed system tools, argument arrays only
        args, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    ).stdout


def select_identity(output: str, requested: str | None) -> str:
    identities = dict(re.findall(r'^\s*\d+\) ([A-Fa-f0-9]{40}) "([^"]+)"', output, re.M))
    if requested:
        matches = [
            key
            for key, name in identities.items()
            if requested.upper() == key.upper() or requested == name
        ]
    else:
        matches = [key for key, name in identities.items() if name.startswith("Apple Development:")]
    if len(matches) != 1:
        raise ValueError(
            "Select one valid signing identity with LYRA_LOCAL_SIGNING_IDENTITY "
            "(exact name or SHA-1). Automatic selection requires exactly one Apple Development "
            "identity. Ad-hoc signing cannot preserve Keychain trust across rebuilds."
        )
    return matches[0]


def signing_targets(app: Path) -> list[Path]:
    targets = []
    for path in app.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_file():
            with path.open("rb") as stream:
                if stream.read(4) in MACHO_MAGICS:
                    targets.append(path)
        elif path.suffix in {".app", ".framework", ".xpc", ".appex"}:
            targets.append(path)
    return sorted(targets, key=lambda path: (-len(path.parts), str(path))) + [app]


def verify_stable_requirement(path: Path, identifier: str) -> str:
    output = run("/usr/bin/codesign", "--display", "--requirements", "-", str(path))
    requirement = next(
        (line for line in output.splitlines() if line.startswith("designated =>")), ""
    )
    if (
        not requirement
        or "cdhash" in requirement
        or f'identifier "{identifier}"' not in requirement
        or "certificate" not in requirement
    ):
        raise ValueError(f"Expected a certificate-backed, stable designated requirement: {path}")
    return requirement


def sign_app(app: Path, identity: str) -> None:
    app = app.resolve(strict=True)
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    identifier = info["CFBundleIdentifier"]
    backend = app / "Contents/Resources/resources/lyra-backend/lyra-backend"
    if not backend.is_file() or backend.is_symlink():
        raise ValueError("Expected the staged lyra-backend executable in the app bundle")
    targets = signing_targets(app)
    print(f"Signing {len(targets)} code objects in {app}…", flush=True)
    for path in targets:
        args = [
            "/usr/bin/codesign",
            "--force",
            "--sign",
            identity,
            "--timestamp=none",
            "--preserve-metadata=entitlements",
        ]
        if path == backend:
            args += ["--identifier", f"{identifier}.backend"]
        elif path == app:
            args += ["--identifier", identifier]
        run(*args, str(path))
    # Resource-located sidecars are not necessarily traversed by --deep verification.
    for path in targets:
        run("/usr/bin/codesign", "--verify", "--strict", str(path))
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(app))
    print(verify_stable_requirement(backend, f"{identifier}.backend"))
    print(verify_stable_requirement(app, identifier))
    print(f"Signed and verified {len(targets)} code objects in {app}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    args = parser.parse_args()
    try:
        identity = select_identity(
            run("/usr/bin/security", "find-identity", "-v", "-p", "codesigning"),
            os.environ.get("LYRA_LOCAL_SIGNING_IDENTITY"),
        )
        sign_app(args.app, identity)
    except subprocess.CalledProcessError as error:
        parser.exit(1, f"Signing/verification failed: {error.stdout}\n")
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
