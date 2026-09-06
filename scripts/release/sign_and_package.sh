#!/usr/bin/env bash
# Run only on the trusted, protected release-signing runner after preflight.
set -euo pipefail
set +x
app='src-tauri/target/release/bundle/macos/Lyra.app'
mkdir -p release-assets
python3 scripts/release_metadata.py --bundle "$app" --source "$(git rev-parse HEAD)"
# Sign real nested Mach-O files inside-out, without changing symlink topology.
python3 - "$app" <<'PY'
import pathlib, subprocess, sys
sys.path.insert(0, 'scripts')
from sign_local_app import signing_targets
app = pathlib.Path(sys.argv[1])
backend = app / 'Contents/Resources/resources/lyra-backend/lyra-backend'
targets = signing_targets(app)
for path in targets:
    args = ['codesign', '--force', '--timestamp=none', '--sign', '-', '--preserve-metadata=entitlements']
    if path == backend:
        args += ['--identifier', 'com.lyra.desktop.backend']
    elif path == app:
        args += ['--identifier', 'com.lyra.desktop']
    subprocess.run([*args, str(path)], check=True)
for path in targets:
    subprocess.run(['codesign', '--verify', '--strict', str(path)], check=True)
PY
codesign --verify --deep --strict --verbose=2 "$app"
codesign -dv --verbose=4 "$app" 2> release-assets/app-signing.txt
python3 - <<'PY_RECEIPT'
import json
from pathlib import Path
Path('release-assets/distribution-signing.json').write_text(json.dumps({
    'mode': 'ad-hoc', 'developer_id_signed': False, 'notarized': False,
}) + '\n')
PY_RECEIPT
python3 scripts/verify_macos_bundle.py "$app" > release-assets/native-inventory.json
codesign --verify --deep --strict "$app"
backend="$(find "$app/Contents/Resources" -type f -name lyra-backend -print -quit)"
[[ -n "$backend" ]]
uv run --locked python scripts/frozen_backend_smoke.py "$backend" > release-assets/frozen-smoke.json
version="$(cat version.txt)"
dmg="release-assets/Lyra_${version}_aarch64.dmg"
bash scripts/build_dmg.sh "$app" "$dmg"
# Authenticate updater bytes with the persistent Tauri key.
COPYFILE_DISABLE=1 tar -czf release-assets/Lyra.app.tar.gz -C "$(dirname "$app")" Lyra.app
frontend/node_modules/.bin/tauri signer sign release-assets/Lyra.app.tar.gz

# Confirm the updater representation retains valid ad-hoc code signatures.
archive_check="$(mktemp -d -t lyra-updater-check)"
trap 'rm -rf -- "$archive_check"' EXIT
tar -xzf release-assets/Lyra.app.tar.gz -C "$archive_check"
codesign --verify --deep --strict "$archive_check/Lyra.app"
