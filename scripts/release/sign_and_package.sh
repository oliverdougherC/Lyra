#!/usr/bin/env bash
# Run only on the trusted, protected release-signing runner after preflight.
set -euo pipefail
set +x
app='src-tauri/target/release/bundle/macos/Lyra.app'
identity="$APPLE_SIGNING_IDENTITY"
[[ "$identity" == 'Developer ID Application:'* ]] || { echo 'Developer ID Application required'; exit 1; }
security find-identity -v -p codesigning "$RUNNER_TEMP/lyra-release.keychain-db" | grep -Fq "\"$identity\""
mkdir -p release-assets
python3 scripts/release_metadata.py --bundle "$app" --source "$(git rev-parse HEAD)"
# Sign real nested Mach-O files inside-out, without changing symlink topology.
python3 - "$app" "$identity" <<'PY'
import pathlib, subprocess, sys
sys.path.insert(0, 'scripts')
from sign_local_app import signing_targets, verify_stable_requirement
app = pathlib.Path(sys.argv[1])
backend = app / 'Contents/Resources/resources/lyra-backend/lyra-backend'
targets = signing_targets(app)
for path in targets:
    args = ['codesign', '--force', '--options', 'runtime', '--timestamp', '--sign', sys.argv[2]]
    if path == backend:
        args += ['--identifier', 'com.lyra.desktop.backend']
    elif path == app:
        args += ['--identifier', 'com.lyra.desktop']
    subprocess.run([*args, str(path)], check=True)
for path in targets:
    subprocess.run(['codesign', '--verify', '--strict', str(path)], check=True)
verify_stable_requirement(backend, 'com.lyra.desktop.backend')
verify_stable_requirement(app, 'com.lyra.desktop')
PY
codesign --verify --deep --strict --verbose=2 "$app"
codesign -dv --verbose=4 "$app" 2> release-assets/app-signing.txt
ditto -c -k --keepParent "$app" "$RUNNER_TEMP/Lyra-notary.zip"
xcrun notarytool submit "$RUNNER_TEMP/Lyra-notary.zip" --apple-id "$APPLE_ID" --password "$APPLE_PASSWORD" --team-id "$APPLE_TEAM_ID" --wait --output-format json > release-assets/app-notarization.json
python3 -c 'import json; assert json.load(open("release-assets/app-notarization.json"))["status"] == "Accepted"'
xcrun stapler staple "$app"
xcrun stapler validate "$app"
python3 scripts/verify_macos_bundle.py "$app" > release-assets/native-inventory.json
codesign --verify --deep --strict "$app"
spctl --assess --type execute --verbose=4 "$app" 2> release-assets/app-gatekeeper.txt
backend="$(find "$app/Contents/Resources" -type f -name lyra-backend -print -quit)"
[[ -n "$backend" ]]
uv run --locked python scripts/frozen_backend_smoke.py "$backend" > release-assets/frozen-smoke.json
version="$(cat version.txt)"
dmg="release-assets/Lyra_${version}_aarch64.dmg"
bash scripts/build_dmg.sh "$app" "$dmg"
codesign --force --timestamp --sign "$identity" "$dmg"
xcrun notarytool submit "$dmg" --apple-id "$APPLE_ID" --password "$APPLE_PASSWORD" --team-id "$APPLE_TEAM_ID" --wait --output-format json > release-assets/dmg-notarization.json
python3 -c 'import json; assert json.load(open("release-assets/dmg-notarization.json"))["status"] == "Accepted"'
xcrun stapler staple "$dmg"
xcrun stapler validate "$dmg"
spctl --assess --type open --context context:primary-signature --verbose=4 "$dmg" 2> release-assets/dmg-gatekeeper.txt
# Updater bytes are created only after final app signing, notarization and stapling.
tar -czf release-assets/Lyra.app.tar.gz -C "$(dirname "$app")" Lyra.app
frontend/node_modules/.bin/tauri signer sign release-assets/Lyra.app.tar.gz
