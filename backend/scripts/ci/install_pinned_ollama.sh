#!/usr/bin/env bash
# Install the exact Ollama build used by the real-local acceptance workflows.
#
# Supply-chain boundary: never execute a network-fetched installer. The release URL contains
# an immutable version and the complete archive is authenticated against the SHA-256 digest
# published by the ollama/ollama GitHub release before any byte is extracted or executed.

set -euo pipefail

readonly OLLAMA_VERSION="v0.40.15"
readonly OLLAMA_ARCHIVE="ollama-linux-amd64.tar.zst"
readonly OLLAMA_ARCHIVE_SHA256="628c018404a4206493efd38e46251c154ede5141b8b8dc85c659b3b027c47379"
readonly OLLAMA_RELEASE_URL="https://github.com/ollama/ollama/releases/download/${OLLAMA_VERSION}/${OLLAMA_ARCHIVE}"

test "$(uname -s)" = "Linux" || { echo "pinned Ollama installer supports Linux only" >&2; exit 1; }
test "$(uname -m)" = "x86_64" || { echo "pinned Ollama artifact is amd64-only" >&2; exit 1; }
command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }
command -v sha256sum >/dev/null || { echo "sha256sum is required" >&2; exit 1; }
command -v unzstd >/dev/null || { echo "unzstd is required" >&2; exit 1; }

download_dir="$(mktemp -d)"
trap 'rm -rf "$download_dir"' EXIT
archive_path="${download_dir}/${OLLAMA_ARCHIVE}"

curl --fail --show-error --location \
  --proto '=https' --tlsv1.2 \
  --output "$archive_path" \
  "$OLLAMA_RELEASE_URL"

printf '%s  %s\n' "$OLLAMA_ARCHIVE_SHA256" "$archive_path" | sha256sum --check --strict
sudo tar --use-compress-program=unzstd -xf "$archive_path" -C /usr/local

command -v ollama >/dev/null || { echo "verified archive did not install ollama" >&2; exit 1; }
ollama --version
