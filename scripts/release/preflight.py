"""Check updater configuration without requiring an Apple developer account."""

import os
from pathlib import Path


def main() -> None:
    if not os.environ.get("TAURI_SIGNING_PRIVATE_KEY"):
        raise SystemExit("Missing protected release configuration: TAURI_SIGNING_PRIVATE_KEY")
    if not Path("src-tauri/updater-public-key.txt").read_text().strip():
        raise SystemExit("Persistent updater public key is missing")
    print("Updater signing configured; Apple signing and notarization are disabled.")


if __name__ == "__main__":
    main()
