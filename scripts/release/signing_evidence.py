"""Inspect native signatures and require ad-hoc signing with hardened runtime."""

import argparse
import json
import plistlib
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sign_local_app import signing_targets  # noqa: E402


def inspect_details(details: str) -> dict:
    flags = re.search(r"^CodeDirectory .* flags=0x([0-9a-fA-F]+)\(", details, re.M)
    if not flags or not int(flags[1], 16) & 0x10000:
        raise ValueError("Native signature is missing hardened runtime")
    if not re.search(r"^Signature=adhoc$", details, re.M) or re.search(
        r"^Authority=", details, re.M
    ):
        raise ValueError("Native signature is not ad-hoc")
    return {
        "mode": "ad-hoc",
        "developer_id_signed": False,
        "notarized": False,
        "hardened_runtime": True,
    }


def validate_evidence(receipt: dict) -> None:
    if not isinstance(receipt, dict):
        raise ValueError("Invalid distribution signing evidence")
    objects = receipt.get("objects")
    if not isinstance(objects, list) or not objects:
        raise ValueError("Native signing observations are missing")
    paths = set()
    for item in objects:
        if not isinstance(item, dict) or not isinstance(item.get("details"), str):
            raise ValueError("Invalid native signing observation")
        path = item.get("path")
        if not isinstance(path, str) or path in paths:
            raise ValueError("Invalid or duplicate native signing path")
        paths.add(path)
        allowed = (
            {"com.apple.security.cs.disable-library-validation": True}
            if path == "Contents/Resources/resources/lyra-backend/lyra-backend"
            or path.endswith("/llama-server")
            else {}
        )
        if item.get("entitlements") != allowed:
            raise ValueError("Native entitlements differ from the scoped helper exception")
        observed = inspect_details(item["details"])
        if any(type(receipt.get(k)) is not type(v) or receipt[k] != v for k, v in observed.items()):
            raise ValueError("Distribution signing declaration differs from native observations")
    if not {".", "Contents/Resources/resources/lyra-backend/lyra-backend"} <= paths:
        raise ValueError("App and backend signing observations are required")


def inspect_bundle(app: Path) -> dict:
    objects = []
    for path in signing_targets(app):
        subprocess.run(  # noqa: S603
            ["/usr/bin/codesign", "--verify", "--strict", str(path)],
            check=True,
            capture_output=True,
        )
        result = subprocess.run(  # noqa: S603
            ["/usr/bin/codesign", "-dv", "--verbose=4", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        details = result.stdout + result.stderr
        inspect_details(details)
        entitlements = subprocess.run(  # noqa: S603
            ["/usr/bin/codesign", "-d", "--xml", "--entitlements", "-", str(path)],
            check=True,
            capture_output=True,
        ).stdout
        objects.append(
            {
                "path": str(path.relative_to(app)),
                "details": details,
                "entitlements": plistlib.loads(entitlements) if entitlements.strip() else {},
            }
        )
    subprocess.run(  # noqa: S603
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        check=True,
        capture_output=True,
    )
    receipt = {**inspect_details(objects[-1]["details"]), "objects": objects}
    validate_evidence(receipt)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect_bundle(args.app), indent=2))


if __name__ == "__main__":
    main()
