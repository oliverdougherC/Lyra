"""Fail closed before a protected runner touches any signing material."""

import base64
import json
import os
import subprocess
from pathlib import Path

REQUIRED = (
    "APPLE_CERTIFICATE",
    "APPLE_CERTIFICATE_PASSWORD",
    "APPLE_SIGNING_IDENTITY",
    "APPLE_ID",
    "APPLE_PASSWORD",
    "APPLE_TEAM_ID",
    "TAURI_SIGNING_PRIVATE_KEY",
)


def main() -> None:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        raise SystemExit("Missing protected release configuration: " + ", ".join(missing))
    identity = os.environ["APPLE_SIGNING_IDENTITY"]
    if not identity.startswith("Developer ID Application:"):
        raise SystemExit(
            "External distribution requires Developer ID Application, never Apple Development"
        )
    if not Path("src-tauri/updater-public-key.txt").read_text().strip():
        raise SystemExit("Persistent updater public key is missing")
    root = Path(os.environ["RUNNER_TEMP"])
    certificate = root / "lyra-certificate.p12"
    certificate.write_bytes(base64.b64decode(os.environ["APPLE_CERTIFICATE"], validate=True))
    certificate.chmod(0o600)
    password = os.urandom(32).hex()
    keychain = str(root / "lyra-release.keychain-db")

    def security(*args: str) -> None:
        subprocess.run(["security", *args], check=True, capture_output=True)  # noqa: S603,S607

    try:
        security("create-keychain", "-p", password, keychain)
        security("set-keychain-settings", "-lut", "21600", keychain)
        security("unlock-keychain", "-p", password, keychain)
        security(
            "import",
            str(certificate),
            "-k",
            keychain,
            "-P",
            os.environ["APPLE_CERTIFICATE_PASSWORD"],
            "-T",
            "/usr/bin/codesign",
        )
        security(
            "set-key-partition-list",
            "-S",
            "apple-tool:,apple:,codesign:",
            "-s",
            "-k",
            password,
            keychain,
        )
        security("list-keychains", "-d", "user", "-s", keychain)
        identities = subprocess.check_output(  # noqa: S603,S607
            ["/usr/bin/security", "find-identity", "-v", "-p", "codesigning", keychain], text=True
        )
        if f'"{identity}"' not in identities:
            raise SystemExit("Imported certificate has no usable matching Developer ID private key")
    except subprocess.CalledProcessError:
        raise SystemExit(
            "Protected signing keychain import failed; inspect credential configuration"
        ) from None
    finally:
        certificate.unlink(missing_ok=True)
    print(json.dumps({"developer_id_identity": "verified", "private_key": "available"}))


if __name__ == "__main__":
    main()
