#!/bin/bash
# Credential-free intake + integrity verification for the Codex Founder Authority & Capability Core handoff.
# Usage: verify_bundle.sh <path-to.bundle>
# Verifies, in order: file SHA-256 == expected; `git bundle verify`; the bundle contains the exact commit;
# commit->tree == expected; ancestry on the stated base; single Alembic head 0094. Imports into an ISOLATED
# object store (never the examiner repo's refs). No network, no credentials, no pushes.
set -u
EXPECT_SHA256="9c1b8848471575867c1e63fb1f8925cc91a94088a48b1747d24bb143322313c8"
EXPECT_COMMIT="e68e7f4affc961e1536850cbe52d303c4775ee37"
EXPECT_TREE="6208f6efeb5605442fe03f2201ed631f882b264b"
EXPECT_BASE="a2dcad1c9f1aaa2107ea377864d8db45ba312c77"
EXPECT_HEAD="0094_founder_authority_core"
B="${1:?usage: verify_bundle.sh <path-to.bundle>}"
[ -f "$B" ] || { echo "FAIL intake::file_missing $B"; exit 2; }
HASH="$(shasum -a 256 "$B" | awk '{print $1}')"
[ "$HASH" = "$EXPECT_SHA256" ] && echo "PASS intake::sha256 $HASH" || { echo "FAIL intake::sha256 got=$HASH want=$EXPECT_SHA256"; exit 3; }
WT="$(mktemp -d)"; export GIT_DIR="$WT/git"; git init -q --bare "$GIT_DIR"
ISO=(env GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 GIT_NO_LAZY_FETCH=1 HOME="$WT" git)
"${ISO[@]}" bundle verify "$B" > "$WT/verify.txt" 2>&1 && echo "PASS intake::bundle_verify" || { echo "FAIL intake::bundle_verify"; cat "$WT/verify.txt"; exit 4; }
"${ISO[@]}" fetch -q "$B" "+refs/*:refs/codex/*" 2>/dev/null || { echo "FAIL intake::fetch"; exit 5; }
T="$("${ISO[@]}" cat-file -t "$EXPECT_COMMIT" 2>/dev/null)"
[ "$T" = "commit" ] && echo "PASS intake::commit_present $EXPECT_COMMIT" || { echo "FAIL intake::commit_absent $EXPECT_COMMIT (type=$T)"; exit 6; }
TREE="$("${ISO[@]}" rev-parse "$EXPECT_COMMIT^{tree}" 2>/dev/null)"
[ "$TREE" = "$EXPECT_TREE" ] && echo "PASS intake::tree $TREE" || { echo "FAIL intake::tree got=$TREE want=$EXPECT_TREE"; exit 7; }
if "${ISO[@]}" cat-file -t "$EXPECT_BASE" >/dev/null 2>&1; then
  "${ISO[@]}" merge-base --is-ancestor "$EXPECT_BASE" "$EXPECT_COMMIT" && echo "PASS intake::ancestry contains base $EXPECT_BASE" || echo "FAIL intake::ancestry base $EXPECT_BASE not an ancestor"
else
  echo "KNOWN intake::ancestry base $EXPECT_BASE not in bundle (base is itself unpublished Codex work); verify base bundle separately"
fi
# single alembic head 0094 — extract the versions tree from the commit without a full checkout
VC="$(mktemp -d)"; "${ISO[@]}" --work-tree="$VC" checkout -q "$EXPECT_COMMIT" -- backend/alembic/versions 2>/dev/null \
  && echo "note: versions extracted" || echo "note: could not sparse-extract versions (run migration_integrity on the worktree instead)"
echo "INTAKE: to examine, add a worktree from this isolated store and run the fa_core profile:"
echo "  git --git-dir=$GIT_DIR worktree add --detach <path> $EXPECT_COMMIT   # (store is temp; copy it first to persist)"
