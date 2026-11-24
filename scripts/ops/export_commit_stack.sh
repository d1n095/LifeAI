#!/usr/bin/env bash
# Reproducible export + isolated verification of a local commit stack (Context Atlas WS-A).
#
# The agent has no GitHub write access, so publication is deliberately a human step. This
# script only produces and verifies a transfer artifact; it never pushes, merges or deploys.
#
#   export <repo> <base-sha> <out-dir> <branch> [branch...]
#   verify <bundle> <manifest.json>
#
# `export` refuses a dirty tracked tree, scans the new commits for secret-like strings, and
# writes commit-stack.bundle + manifest.json + SHA256SUMS. `verify` clones the bundle into a
# throwaway directory and checks every recorded commit/tree, then runs git fsck.
set -euo pipefail

fail() { echo "ERROR: $*" >&2; exit 1; }

cmd_export() {
  local repo="$1" base="$2" out="$3"; shift 3
  local branches=("$@")
  [ "${#branches[@]}" -gt 0 ] || fail "at least one branch is required"

  cd "$repo"
  [ -z "$(git status --porcelain --untracked-files=no)" ] || fail "tracked working tree is dirty"

  local base_full
  base_full="$(git rev-parse --verify "$base^{commit}")" || fail "unknown base: $base"

  mkdir -p "$out"
  local refs=()
  local branch
  for branch in "${branches[@]}"; do
    git rev-parse --verify "refs/heads/$branch" >/dev/null 2>&1 || fail "unknown branch: $branch"
    git merge-base --is-ancestor "$base_full" "refs/heads/$branch" \
      || fail "$branch does not descend from $base_full"
    refs+=("refs/heads/$branch")
  done

  # Secret scan over exactly the new commits, before anything is written to the artifact.
  # The GitHub/xox prefixes are assembled from parts so this detector's own source cannot
  # match itself when the branch that contains this script is exported.
  local gh_pat_prefix='github_'"pat_"
  local xox_prefix='xo'"x[baprs]-"
  local pattern="AKIA[0-9A-Z]{16}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|ghp_[A-Za-z0-9]{30,}|${gh_pat_prefix}|sk-[A-Za-z0-9]{20,}|${xox_prefix}"
  local scan_hits
  scan_hits="$(git log -p --no-color --format= "$base_full".."${refs[@]}" | grep -nE "$pattern" || true)"
  if [ -n "$scan_hits" ]; then
    echo "$scan_hits" | head -5 >&2
    fail "secret-like strings found in the new commits; refusing to export"
  fi

  local bundle="$out/commit-stack.bundle"
  git bundle create "$bundle" "$base_full" "${refs[@]}" >/dev/null

  python3 - "$base_full" "$bundle" "$out" "${branches[@]}" <<'PY'
import hashlib, json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

base, bundle, out, *branches = sys.argv[1:]
def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()

entries = []
for branch in branches:
    sha = git("rev-parse", f"refs/heads/{branch}")
    entries.append({
        "branch": branch,
        "sha": sha,
        "tree": git("rev-parse", f"{sha}^{{tree}}"),
        "commits_ahead": int(git("rev-list", "--count", f"{base}..{sha}")),
        "subjects": git("log", "--format=%h %s", f"{base}..{sha}").splitlines(),
    })

bundle_bytes = Path(bundle).read_bytes()
manifest = {
    "format": "lifeai-commit-stack-v1",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "git_version": git("--version"),
    "repo": git("config", "--get", "remote.origin.url") or "local",
    "base": base,
    "bundle": Path(bundle).name,
    "bundle_sha256": hashlib.sha256(bundle_bytes).hexdigest(),
    "bundle_bytes": len(bundle_bytes),
    "branches": entries,
}
manifest_path = Path(out) / "manifest.json"
manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
(Path(out) / "SHA256SUMS").write_text(
    f"{manifest['bundle_sha256']}  {manifest['bundle']}\n", encoding="utf-8"
)
print(f"exported {len(entries)} branches, {manifest['bundle_bytes']} bytes")
PY
}

cmd_verify() {
  local bundle="$1" manifest="$2"
  [ -f "$bundle" ] || fail "bundle not found: $bundle"
  [ -f "$manifest" ] || fail "manifest not found: $manifest"

  local expected actual
  expected="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["bundle_sha256"])' "$manifest")"
  actual="$(sha256sum "$bundle" | cut -d' ' -f1)"
  [ "$expected" = "$actual" ] || fail "bundle sha256 mismatch"

  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  git clone --quiet "$bundle" "$tmp/repo" || fail "bundle is not cloneable"
  git -C "$tmp/repo" fsck --no-dangling >/dev/null || fail "git fsck failed"

  python3 - "$tmp/repo" "$manifest" <<'PY'
import json, subprocess, sys
repo, manifest_path = sys.argv[1], sys.argv[2]
manifest = json.load(open(manifest_path))

def git(*args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout.strip()

missing = []
for entry in manifest["branches"]:
    try:
        sha = git("rev-parse", f"{entry['sha']}^{{commit}}")
        tree = git("rev-parse", f"{entry['sha']}^{{tree}}")
    except subprocess.CalledProcessError:
        missing.append(entry["branch"])
        continue
    if sha != entry["sha"] or tree != entry["tree"]:
        missing.append(entry["branch"])

if missing:
    print("MISSING/MISMATCHED: " + ", ".join(missing), file=sys.stderr)
    raise SystemExit(1)
print(f"verified {len(manifest['branches'])} branches in an isolated clone at {manifest['base'][:12]}")
PY
}

case "${1:-}" in
  export) shift; cmd_export "$@" ;;
  verify) shift; cmd_verify "$@" ;;
  *) echo "usage: $0 export <repo> <base-sha> <out-dir> <branch...> | verify <bundle> <manifest>" >&2; exit 2 ;;
esac
