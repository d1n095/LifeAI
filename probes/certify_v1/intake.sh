#!/bin/bash
# Exact-SHA intake + full combined certification run for an integration candidate.
# Usage: intake.sh <expected-40-char-sha> [<git-bundle-path>]
#  - with a bundle: `git bundle verify`, fetch it into the examiner object store (no network);
#  - without: anonymous public fetch (isolated git config, no credential helper, no lazy fetch).
# Then: exact SHA check, ancestry report, frozen worktree, frontend build, `combined` profile, GO/NO-GO matrix.
# Never pushes, never uses credentials, never modifies the candidate.
set -u
SHA="${1:?expected sha}"; BUNDLE="${2:-}"; HERE="$(cd "$(dirname "$0")" && pwd)"; PROBES="$(cd "$HERE/.." && pwd)"; REPO="$(cd "$PROBES/.." && pwd)"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "need a full 40-char sha"; exit 2; }
ISO="$(mktemp -d)"; GITENV=(env -i PATH="$PATH" HOME="$ISO" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 GIT_NO_LAZY_FETCH=1)
if [ -n "$BUNDLE" ]; then
  "${GITENV[@]}" git -C "$REPO" bundle verify "$BUNDLE" || { echo "INTAKE FAIL: bundle does not verify"; exit 3; }
  "${GITENV[@]}" git -C "$REPO" -c credential.helper= fetch -q "$BUNDLE" "+refs/*:refs/examiner-intake/*" || exit 3
else
  "${GITENV[@]}" git -C "$REPO" -c credential.helper= -c core.askPass= fetch -q https://github.com/d1n095/LifeAI.git "$SHA" \
    || { echo "INTAKE BLOCKED: $SHA is not publicly fetchable"; exit 4; }
fi
[ "$(git -C "$REPO" rev-parse --verify --quiet "$SHA^{commit}")" = "$SHA" ] || { echo "INTAKE FAIL: exact sha $SHA not present after intake"; exit 3; }
WT="$REPO/../wtcert-${SHA:0:7}"; [ -d "$WT" ] || git -C "$REPO" worktree add -q --detach "$WT" "$SHA"
[ "$(git -C "$WT" rev-parse HEAD)" = "$SHA" ] || { echo "INTAKE FAIL: worktree HEAD mismatch"; exit 3; }
OUTD="$PROBES/cert-${SHA:0:7}"; mkdir -p "$OUTD"
{ echo "sha=$SHA"; echo "source=${BUNDLE:-public-fetch}"
  for ref in ee132bb8e87fcc99f2f3607cfc2b46a5364d23c7:#293 5e3cd01ac3a2cded69ef19bfa074d15196d3afaa:#292 0b1871bf63e4c9b6d0d32633af097b281e644679:#291 37bca08:#280; do
    r="${ref%%:*}"; n="${ref##*:}"; if git -C "$REPO" merge-base --is-ancestor "$r" "$SHA" 2>/dev/null; then echo "contains $n ($r)"; else echo "does NOT contain $n ($r)"; fi; done
  git -C "$WT" log --oneline -15; } > "$OUTD/intake.txt"
cat "$OUTD/intake.txt"
FE="$("$PROBES/proxy_v1/build_frontend.sh" "$WT" "$(mktemp -d)/fe")" || echo "frontend build failed (proxy/ui suites will report FAILED)"
[ -d "${PW_BROWSERS:-/nonexistent}" ] && ln -sfn "$PW_BROWSERS" "$FE/../pw-browsers"
FE_BUILT="$FE" REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6390/0}" "$PROBES/first_cycle_acceptance_v1/run_acceptance.sh" combined "$WT" | tee "$OUTD/run.txt"
OUT="$PROBES/first_cycle_acceptance_v1/out/combined-${SHA:0:7}"
python3 "$HERE/gonogo.py" "$OUT/summary.json" "$OUT/ci_exact_sha.json" "$SHA" > "$OUTD/GO_NO_GO.md"; cat "$OUTD/GO_NO_GO.md"
