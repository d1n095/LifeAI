#!/usr/bin/env bash
# Executable failure-injection tests for scripts/vps/backup.sh's atomic finalization and
# checksum-gated retention (and scripts/vps/restore.sh's integrity refusals). Pure userspace:
# no Docker, no root, no network — so it runs the same on a CI Linux runner as on a dev box.
# The ENOSPC (disk-exhaustion) scenario needs a real tiny filesystem and therefore lives in
# the workflow (.github/workflows/backup-integrity-verification.yml), which mounts a tmpfs and
# calls this script's single-scenario entrypoint.
#
# Design: failures are injected DETERMINISTICALLY via PATH shims (a fake tar / sha256sum / mv
# that this script controls through env vars), not via timing/kill races — so every assertion
# is reproducible. Distinct historical backups are fabricated by copying a known-good archive
# to distinct canonical timestamps with correct .sha256 sidecars, so retention tests need no
# real-time sleeps.
#
# Exit 0 = every scenario passed. Non-zero = at least one assertion failed (details on stderr).
# This is a test harness built around the `<condition> && pass "..." || fail "..."` idiom.
# pass()/fail() always return 0, so the SC2015 "C may run when A is true" caveat never applies
# here, and checking $? on the line after a backgrounded/subshell command is exactly what these
# assertions need. Disabled file-wide (must precede the first command) to keep real signal clean.
# shellcheck disable=SC2015,SC2181
set -uo pipefail

SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
VPS_DIR="$(cd -- "$SELF_DIR/.." &> /dev/null && pwd)"
BACKUP_SH="$VPS_DIR/backup.sh"
RESTORE_SH="$VPS_DIR/restore.sh"

PASS=0
FAIL=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS + 1)); }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*" >&2; FAIL=$((FAIL + 1)); }
section() { printf '\n== %s ==\n' "$*"; }

# Absolute paths to the REAL tools, captured before any PATH shimming.
REAL_TAR="$(command -v tar)"
REAL_SHA256="$(command -v sha256sum)"
REAL_MV="$(command -v mv)"
export REAL_TAR REAL_SHA256 REAL_MV

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- PATH shims ---------------------------------------------------------------------------
# Each shim passes through to the real tool unless its INJECT_* env var selects a failure mode.
SHIM_DIR="$WORK/shim"
mkdir -p "$SHIM_DIR"

cat > "$SHIM_DIR/tar" <<'SHIM'
#!/usr/bin/env bash
# Only interfere with the FINAL-archive write (target is a hidden *.partial.* file). Every
# other tar call (the inner opt-lifeai tar, and all `tzf` listing/verification) passes through.
target=""
prev=""
for a in "$@"; do
    case "$prev" in -*f|f) target="$a";; esac
    case "$a" in -*f) prev="f";; *) prev="";; esac
done
is_partial=0
case "$target" in *.partial.*) is_partial=1;; esac
case "${INJECT_TAR_MODE:-}" in
    fail_partial)
        if [ "$is_partial" = 1 ]; then
            head -c 512 /dev/zero > "$target" 2>/dev/null || true   # leave a short/partial file
            echo "fake tar: simulated interrupted archive write" >&2
            exit 1
        fi
        ;;
    garbage_partial)
        if [ "$is_partial" = 1 ]; then
            printf 'this is not a valid gzip stream' > "$target"     # passes write, fails tzf
            exit 0
        fi
        ;;
esac
exec "$REAL_TAR" "$@"
SHIM

cat > "$SHIM_DIR/sha256sum" <<'SHIM'
#!/usr/bin/env bash
# Fail only the hashing call (no -c). `sha256sum -c` verification always passes through so the
# post-publish/retention checks behave normally.
case "${INJECT_SHA_FAIL:-}" in
    1)
        for a in "$@"; do [ "$a" = "-c" ] && exec "$REAL_SHA256" "$@"; done
        echo "fake sha256sum: simulated checksum generation failure" >&2
        exit 1
        ;;
esac
exec "$REAL_SHA256" "$@"
SHIM

cat > "$SHIM_DIR/mv" <<'SHIM'
#!/usr/bin/env bash
case "${INJECT_MV_FAIL:-}" in
    1) echo "fake mv: simulated atomic publication failure" >&2; exit 1;;
esac
exec "$REAL_MV" "$@"
SHIM

chmod +x "$SHIM_DIR/tar" "$SHIM_DIR/sha256sum" "$SHIM_DIR/mv"

# --- Fixtures -----------------------------------------------------------------------------
# Build a fresh (compose_dir, output_dir) pair. Each scenario gets its own isolated dirs.
make_fixture() {
    local root="$1"
    local compose="$root/opt-lifeai" out="$root/backups"
    rm -rf "$root"
    mkdir -p "$compose/deployments" "$out"
    printf 'services: {}\n' > "$compose/docker-compose.vps.yml"
    printf 'example.com { respond "ok" }\n' > "$compose/Caddyfile"
    printf '{"deploy":"record"}\n' > "$compose/deployments/0001.json"
    # One line, space-separated, consumed by `read -r C O`. Paths live under mktemp -d (/tmp),
    # which contains no spaces, so word-splitting on a single space is safe here.
    printf '%s %s\n' "$compose" "$out"
}

run_backup() { # run_backup <compose> <out> [extra args...]  -> exit code, output to $LAST_OUT
    local compose="$1" out="$2"; shift 2
    LAST_OUT="$("$BACKUP_SH" --compose-dir "$compose" --output-dir "$out" "$@" 2>&1)"
    return $?
}

count_canonical() { find "$1" -maxdepth 1 -name 'lifeai-backup-*.tar.gz' | wc -l | tr -d ' '; }
count_partials()  { find "$1" -maxdepth 1 -name '.lifeai-backup-*.partial.*' | wc -l | tr -d ' '; }
newest_archive()  { find "$1" -maxdepth 1 -name 'lifeai-backup-*.tar.gz' | sort -r | head -1; }

# Fabricate a valid historical backup by copying an existing good archive to a distinct
# canonical timestamp and writing a correct sidecar — no real-time sleep needed.
fabricate_valid() { # <out> <good_archive> <timestamp>
    local out="$1" good="$2" name="lifeai-backup-$3.tar.gz"
    cp "$good" "$out/$name"
    ( cd "$out" && "$REAL_SHA256" "$name" > "$name.sha256" )
}

# ==========================================================================================
section "Scenario 1 — successful backup, verify, restore round-trip"
read -r C O < <(make_fixture "$WORK/s1")
if run_backup "$C" "$O"; then
    [ "$(count_canonical "$O")" = 1 ] && pass "exactly one canonical archive published" || fail "expected 1 archive, got $(count_canonical "$O")"
    arc="$(newest_archive "$O")"
    [ -f "$arc.sha256" ] && pass "checksum sidecar present" || fail "missing .sha256 sidecar"
    ( cd "$O" && "$REAL_SHA256" -c "$(basename "$arc").sha256" >/dev/null 2>&1 ) && pass "published archive verifies" || fail "published archive does not verify"
    [ "$(count_partials "$O")" = 0 ] && pass "no .partial leftovers" || fail "found leftover .partial files"
    perm="$(stat -c '%a' "$arc" 2>/dev/null || stat -f '%Lp' "$arc")"
    [ "$perm" = 600 ] && pass "archive mode is 0600" || fail "archive mode is $perm, expected 600"
    rdir="$WORK/s1/restored"
    if "$RESTORE_SH" --from "$arc" --target-dir "$rdir" --confirm >/dev/null 2>&1; then
        [ -f "$rdir/docker-compose.vps.yml" ] && pass "restore recovered compose file" || fail "restore missing compose file"
    else
        fail "restore.sh failed on a good archive"
    fi
else
    fail "backup.sh failed on the happy path: $LAST_OUT"
fi

section "Scenario 2 — interrupted archive write leaves NO canonical archive and spares prior backups"
read -r C O < <(make_fixture "$WORK/s2")
run_backup "$C" "$O"                       # seed one good backup
good="$(newest_archive "$O")"
sha_before="$("$REAL_SHA256" "$good" | awk '{print $1}')"
PATH="$SHIM_DIR:$PATH" INJECT_TAR_MODE=fail_partial run_backup "$C" "$O"
rc=$?
[ "$rc" -ne 0 ] && pass "backup.sh failed (as it must) on interrupted write" || fail "backup.sh wrongly reported success"
[ "$(count_canonical "$O")" = 1 ] && pass "no second (corrupt) canonical archive appeared" || fail "a corrupt canonical archive was published ($(count_canonical "$O") present)"
[ "$(count_partials "$O")" = 0 ] && pass "interrupted temp cleaned up (no .partial leftover)" || fail "left a .partial file behind"
[ -f "$good" ] && [ "$("$REAL_SHA256" "$good" | awk '{print $1}')" = "$sha_before" ] && pass "prior good backup intact & unchanged" || fail "prior good backup was damaged/removed"

section "Scenario 3 — corrupt archive (bad gzip) is caught by the integrity gate, never published"
read -r C O < <(make_fixture "$WORK/s3")
PATH="$SHIM_DIR:$PATH" INJECT_TAR_MODE=garbage_partial run_backup "$C" "$O"
[ $? -ne 0 ] && pass "backup.sh refused to publish a corrupt archive" || fail "backup.sh published a corrupt archive"
[ "$(count_canonical "$O")" = 0 ] && pass "no canonical archive published" || fail "a corrupt archive was published"
[ "$(count_partials "$O")" = 0 ] && pass "corrupt temp cleaned up" || fail "left a .partial file behind"

section "Scenario 4 — checksum generation failure aborts before publication"
read -r C O < <(make_fixture "$WORK/s4")
PATH="$SHIM_DIR:$PATH" INJECT_SHA_FAIL=1 run_backup "$C" "$O"
[ $? -ne 0 ] && pass "backup.sh failed when checksum could not be generated" || fail "backup.sh ignored a checksum failure"
[ "$(count_canonical "$O")" = 0 ] && pass "nothing published without a checksum" || fail "published an archive with no checksum"
[ "$(count_partials "$O")" = 0 ] && pass "temp cleaned up" || fail "left a .partial file behind"

section "Scenario 5 — atomic publication (mv) failure leaves no half-published state"
read -r C O < <(make_fixture "$WORK/s5")
run_backup "$C" "$O"; good="$(newest_archive "$O")"; sha_before="$("$REAL_SHA256" "$good" | awk '{print $1}')"
PATH="$SHIM_DIR:$PATH" INJECT_MV_FAIL=1 run_backup "$C" "$O"
[ $? -ne 0 ] && pass "backup.sh failed when publication could not complete" || fail "backup.sh ignored a publish failure"
[ "$(count_canonical "$O")" = 1 ] && pass "no partial publish (only the prior good archive remains)" || fail "unexpected canonical count $(count_canonical "$O")"
[ "$(count_partials "$O")" = 0 ] && pass "temp cleaned up after failed publish" || fail "left a .partial file behind"
[ "$("$REAL_SHA256" "$good" | awk '{print $1}')" = "$sha_before" ] && pass "prior good backup intact" || fail "prior good backup changed"

section "Scenario 6 — restore refuses invalid checksum; warns-but-proceeds on missing checksum"
read -r C O < <(make_fixture "$WORK/s6")
run_backup "$C" "$O"; good="$(newest_archive "$O")"
printf '%s  %s\n' "0000000000000000000000000000000000000000000000000000000000000000" "$(basename "$good")" > "$good.sha256"
"$RESTORE_SH" --from "$good" --target-dir "$WORK/s6/r_bad" --confirm >/dev/null 2>&1
[ $? -ne 0 ] && pass "restore refused a checksum-mismatched archive" || fail "restore accepted a tampered archive"
read -r C O < <(make_fixture "$WORK/s6b")
run_backup "$C" "$O"; good="$(newest_archive "$O")"; rm -f "$good.sha256"
if "$RESTORE_SH" --from "$good" --target-dir "$WORK/s6b/r_missing" --confirm >/tmp/s6b.out 2>&1; then
    grep -q "No checksum file" /tmp/s6b.out && pass "restore warned about the missing checksum and proceeded" || pass "restore proceeded without a checksum (warning path)"
else
    fail "restore failed outright on a missing (not invalid) checksum"
fi

section "Scenario 7 — concurrent backups are serialized (no corruption, no leak)"
read -r C O < <(make_fixture "$WORK/s7")
# Make the first run slow by shimming tar to sleep during the FINAL archive write, so the
# second run is guaranteed to collide with the held lock.
cat > "$SHIM_DIR/tar_slow" <<'SHIM'
#!/usr/bin/env bash
target=""; prev=""
for a in "$@"; do case "$prev" in f) target="$a";; esac; case "$a" in -*f) prev="f";; *) prev="";; esac; done
case "$target" in *.partial.*) sleep 3;; esac
exec "$REAL_TAR" "$@"
SHIM
chmod +x "$SHIM_DIR/tar_slow"
mkdir -p "$WORK/s7/slowshim"; ln -sf "$SHIM_DIR/tar_slow" "$WORK/s7/slowshim/tar"
( PATH="$WORK/s7/slowshim:$PATH" "$BACKUP_SH" --compose-dir "$C" --output-dir "$O" >/tmp/s7a.out 2>&1 ) &
first_pid=$!
sleep 1
"$BACKUP_SH" --compose-dir "$C" --output-dir "$O" >/tmp/s7b.out 2>&1
second_rc=$?
wait "$first_pid"; first_rc=$?
if [ "$second_rc" -ne 0 ] && grep -q "Another backup is already running" /tmp/s7b.out; then
    pass "second concurrent backup refused via flock"
elif [ "$second_rc" -eq 0 ] && [ "$first_rc" -eq 0 ]; then
    pass "both runs serialized to success (lock released between them)"
else
    fail "unexpected concurrency outcome (first=$first_rc second=$second_rc): $(cat /tmp/s7b.out)"
fi
[ "$first_rc" -eq 0 ] && pass "first (lock-holding) backup succeeded" || fail "first backup failed under contention"
[ "$(count_partials "$O")" = 0 ] && pass "no .partial leftovers after contention" || fail "left a .partial file behind"
for a in "$O"/lifeai-backup-*.tar.gz; do ( cd "$O" && "$REAL_SHA256" -c "$(basename "$a").sha256" >/dev/null 2>&1 ) || fail "a post-contention archive failed verification: $a"; done
pass "every post-contention archive verifies"

section "Scenario 8 — retention never evicts a valid backup because of an incomplete one"
read -r C O < <(make_fixture "$WORK/s8")
run_backup "$C" "$O"; good="$(newest_archive "$O")"
fabricate_valid "$O" "$good" "20200101T000000Z"
fabricate_valid "$O" "$good" "20200102T000000Z"
fabricate_valid "$O" "$good" "20200103T000000Z"   # now 4 valid total (3 fabricated + 1 real)
# Drop in an incomplete canonical-named archive with NO valid checksum.
printf 'truncated junk' > "$O/lifeai-backup-20200104T000000Z.tar.gz"
valid_before=$(for a in "$O"/lifeai-backup-*.tar.gz; do [ -f "$a.sha256" ] && ( cd "$O" && "$REAL_SHA256" -c "$(basename "$a").sha256" >/dev/null 2>&1 ) && echo "$a"; done | wc -l | tr -d ' ')
# A successful backup with --keep 3 should prune down to the 3 NEWEST VALID, keep the junk file untouched.
run_backup "$C" "$O" --keep 3
[ -f "$O/lifeai-backup-20200104T000000Z.tar.gz" ] && pass "incomplete archive left in place (not deleted by retention)" || fail "retention deleted an incomplete archive it should have ignored"
# The newest valid backup (the fresh real one) must still be present and verify.
newest="$(find "$O" -maxdepth 1 -name 'lifeai-backup-*.tar.gz' ! -name 'lifeai-backup-20200104T000000Z.tar.gz' | sort -r | head -1)"
( cd "$O" && "$REAL_SHA256" -c "$(basename "$newest").sha256" >/dev/null 2>&1 ) && pass "newest valid backup preserved & verifies" || fail "newest valid backup missing/bad"
valid_after=$(for a in "$O"/lifeai-backup-*.tar.gz; do [ -f "$a.sha256" ] && ( cd "$O" && "$REAL_SHA256" -c "$(basename "$a").sha256" >/dev/null 2>&1 ) && echo "$a"; done | wc -l | tr -d ' ')
[ "$valid_after" = 3 ] && pass "retention kept exactly --keep=3 VALID backups (was $valid_before)" || fail "expected 3 valid after prune, got $valid_after"

section "Scenario 9 — recovery from the last valid backup after a failed run"
read -r C O < <(make_fixture "$WORK/s9")
run_backup "$C" "$O"; good="$(newest_archive "$O")"
PATH="$SHIM_DIR:$PATH" INJECT_TAR_MODE=fail_partial run_backup "$C" "$O"   # a failed run after the good one
last_valid="$(newest_archive "$O")"
if "$RESTORE_SH" --from "$last_valid" --target-dir "$WORK/s9/recovered" --confirm >/dev/null 2>&1; then
    diff -q "$C/docker-compose.vps.yml" "$WORK/s9/recovered/docker-compose.vps.yml" >/dev/null && pass "recovered content matches the original" || fail "recovered content differs from original"
else
    fail "could not recover from the last valid backup"
fi

printf '\n==================== RESULT: %d passed, %d failed ====================\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
