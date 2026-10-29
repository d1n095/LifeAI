#!/usr/bin/env bash
#
# Build the macOS desktop bundle (.app + .dmg) for Apple Silicon.
# Prereqs: Node (have), Rust stable (rustup), Xcode Command Line Tools (have).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # desktop/
cd "$HERE"

# Make the rustup toolchain visible to this shell if it was installed without a profile reload.
[ -f "$HOME/.cargo/env" ] && source "$HOME/.cargo/env"

# 1. Tauri CLI (prebuilt binary via npm — faster than `cargo install tauri-cli`).
if [ ! -x node_modules/.bin/tauri ]; then
  echo "[build] installing @tauri-apps/cli…"
  npm install
fi
TAURI="node_modules/.bin/tauri"

# 2. Generate the icon set from the committed SVG into src-tauri/icons/ (git-ignored).
echo "[build] generating icons…"
"$TAURI" icon src-tauri/app-icon.svg

# 2b. Clean up any stale read-write DMG image left mounted by a previous interrupted build.
#     tauri's bundle_dmg.sh fails with "couldn't unmount ... Resource busy" if one is still
#     attached, so detach it and delete the temp image before building.
echo "[build] clearing any stale DMG mounts…"
for dev in $(hdiutil info 2>/dev/null | awk '/image-path.*MainAI/{p=1} p&&/^\/dev\/disk[0-9]+\t/{print $1; p=0}'); do
  echo "[build]   detaching $dev"
  hdiutil detach "$dev" -force >/dev/null 2>&1 || true
done
find src-tauri/target/release/bundle -name 'rw.*.dmg' -delete 2>/dev/null || true

# 3. Build. tauri.conf.json's beforeBuildCommand runs the static frontend export first.
echo "[build] building macOS bundle (release)…"
MAINAI_BACKEND_URL="${MAINAI_BACKEND_URL:-http://localhost:8000}" "$TAURI" build

echo "[build] artifacts:"
find src-tauri/target/release/bundle -maxdepth 2 -name '*.app' -o -name '*.dmg' 2>/dev/null || true
