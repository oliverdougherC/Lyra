#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 /path/to/Lyra.app /path/to/Lyra.dmg" >&2
  exit 2
fi

app_bundle="$1"
output_dmg="$2"
if [[ ! -d "$app_bundle/Contents" ]]; then
  echo "not a macOS app bundle: $app_bundle" >&2
  exit 2
fi

stage_dir="$(mktemp -d -t lyra-dmg)"
cleanup() {
  rm -rf -- "$stage_dir"
}
trap cleanup EXIT

cp -R "$app_bundle" "$stage_dir/Lyra.app"
ln -s /Applications "$stage_dir/Applications"
mkdir -p "$(dirname "$output_dmg")"
hdiutil create \
  -volname Lyra \
  -srcfolder "$stage_dir" \
  -format UDZO \
  -ov \
  "$output_dmg"
