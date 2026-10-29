#!/usr/bin/env bash
#
# Build a MainAI Android debug APK (arm64 / Google Pixel) on a machine that already has the
# Android toolchain. This is the "dedicated box / CI" path — used instead of installing the
# multi-GB SDK on a disk-constrained laptop. The GitHub Actions workflow
# .github/workflows/mainai-android-apk.yml runs these same steps on CI.
#
# Prereqs (checked below):
#   * JDK 17 on PATH (java)
#   * ANDROID_HOME pointing at an Android SDK with platform + build-tools
#   * An NDK installed (ANDROID_NDK_ROOT / NDK_HOME, or $ANDROID_HOME/ndk/<ver>)
#   * Rust stable (rustup) + Node 20+
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # desktop/
cd "$HERE"
[ -f "$HOME/.cargo/env" ] && source "$HOME/.cargo/env"

# --- preconditions -----------------------------------------------------------------------
command -v java >/dev/null 2>&1 || { echo "[android] FATAL: JDK 17 not found on PATH" >&2; exit 1; }
: "${ANDROID_HOME:?[android] FATAL: set ANDROID_HOME to your Android SDK}"
if [ -z "${NDK_HOME:-}${ANDROID_NDK_ROOT:-}" ] && ! ls "$ANDROID_HOME"/ndk/* >/dev/null 2>&1; then
  echo "[android] FATAL: no NDK found (set ANDROID_NDK_ROOT/NDK_HOME or install \$ANDROID_HOME/ndk/<ver>)" >&2
  exit 1
fi

# The backend the phone will reach. localhost is NOT reachable from a device/emulator:
#   * physical Pixel  -> your backend's LAN or public HTTPS URL
#   * Android emulator-> http://10.0.2.2:8000 (host loopback alias)
# Baked into the client bundle at build time (not a secret). No API keys are ever embedded.
BACKEND_URL="${MAINAI_BACKEND_URL:-}"
if [ -z "$BACKEND_URL" ]; then
  echo "[android] WARNING: MAINAI_BACKEND_URL unset; defaulting to http://10.0.2.2:8000 (emulator only)."
  BACKEND_URL="http://10.0.2.2:8000"
fi
echo "[android] backend url: $BACKEND_URL"

# --- rust android targets ----------------------------------------------------------------
echo "[android] ensuring rust android targets…"
rustup target add aarch64-linux-android armv7-linux-androideabi i686-linux-android x86_64-linux-android

# --- tauri cli + icons -------------------------------------------------------------------
if [ ! -x node_modules/.bin/tauri ]; then
  echo "[android] installing @tauri-apps/cli…"
  npm install
fi
TAURI="node_modules/.bin/tauri"
echo "[android] generating icons…"
"$TAURI" icon src-tauri/app-icon.svg

# --- init the android gradle project (generated, git-ignored) ----------------------------
if [ ! -d src-tauri/gen/android ]; then
  echo "[android] tauri android init…"
  "$TAURI" android init
fi

# --- build the debug APK (arm64 first, for Pixel) ----------------------------------------
echo "[android] building debug APK (aarch64)…"
MAINAI_BACKEND_URL="$BACKEND_URL" "$TAURI" android build --apk --debug --target aarch64

echo "[android] APK(s):"
find src-tauri/gen/android -name '*.apk' -path '*debug*' -print -exec du -h {} \;
echo "[android] package identity:"
echo "  identifier (tauri.conf.json): ai.mainai.client"
echo "  (verify with: \$ANDROID_HOME/build-tools/<ver>/aapt dump badging <apk> | grep package)"
