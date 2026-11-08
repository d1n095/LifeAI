#!/usr/bin/env bash
# Backs up the VPS-hosted state that deploy.sh/rollback.sh can't reconstruct on their own:
#   - /opt/lifeai's non-secret files: docker-compose.vps.yml, Caddyfile, deployment records
#     (scripts/vps/deploy.sh's and rollback.sh's *.json history — digests/timestamps/results
#     only, never secrets, see deploy.sh's own record-writing code).
#   - the caddy_data, caddy_config, and lifeai_uploads named Docker volumes (TLS certificates
#     + Caddy's own autosaved config, plus — since the durable-worker package — every
#     original file the Life Library has ever accepted, content-addressed under
#     lifeai_uploads, see backend/app/storage/local_fs.py). This is the WHOLE reason
#     lifeai_uploads must be backed up: it's the only durable copy of an original file's
#     bytes outside Postgres, which stores the metadata (Document.storage_key/sha256/
#     size_bytes) but never the bytes themselves. Losing this volume without a backup means
#     every already-imported original is gone even though the database still looks intact.
#
# Deliberately does NOT back up:
#   - /etc/lifeai/lifeai.env (the secrets file). An unencrypted archive sitting on the same
#     disk is not real protection for secrets, and copying them into a tar file only
#     multiplies where a leak could come from. This script instead writes a NAMES-only
#     manifest entry (never values, see $LIFEAI_REQUIRED_ENV_VARS in lib.sh) so a from-
#     scratch restore has a checklist of what to re-provision from your own secret store —
#     the same step as the very first deploy (docs/STRATO_VPS_DEPLOY.md Steg 2). See
#     docs/VPS_BACKUP_RESTORE.md.
#   - The Postgres database: Supabase-hosted, never on this VPS at all — Supabase's own
#     backup responsibility, not something a VPS-local script could honestly claim to cover.
#   - Redis/Valkey: runs locally on this VPS now (docker-compose.vps.yml's `redis` service,
#     replacing the former external Upstash dependency), but on tmpfs with no disk
#     persistence (--save "") — ephemeral rate-limit/job-lock cache, nothing on disk to back
#     up in the first place, safe to lose entirely either way.
#   - Docker's own json-file container logs: ephemeral, already size/count-limited by
#     docker-compose.vps.yml's own logging driver config, not disaster-recovery-relevant.
#
# Usage: ./backup.sh [--dry-run] [--compose-dir /opt/lifeai] [--output-dir /var/backups/lifeai] [--keep N]
#
# Deliberately does NOT require root: /opt/lifeai and /var/backups/lifeai are owned by the
# deploy user (see 30_setup_directories.sh), and reading Docker volumes only needs that
# user's docker group membership — this script never touches /etc/lifeai at all.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

parse_common_flags "$@"

COMPOSE_DIR="/opt/lifeai"
OUTPUT_DIR="/var/backups/lifeai"
KEEP=7

while [ $# -gt 0 ]; do
    case "$1" in
        --compose-dir)
            COMPOSE_DIR="$2"
            shift 2
            ;;
        --output-dir)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --keep)
            KEEP="$2"
            shift 2
            ;;
        --dry-run | --yes | -y)
            shift
            ;;
        *)
            die "Unknown argument: $1"
            ;;
    esac
done

case "$KEEP" in
    '' | *[!0-9]*) die "--keep must be a non-negative integer, got: $KEEP" ;;
esac

[ -d "$COMPOSE_DIR" ] || die "Missing $COMPOSE_DIR."
[ -f "$COMPOSE_DIR/docker-compose.vps.yml" ] || die "Missing $COMPOSE_DIR/docker-compose.vps.yml."
[ -d "$OUTPUT_DIR" ] || die "Missing $OUTPUT_DIR — run 30_setup_directories.sh first."

# --- Concurrency guard --------------------------------------------------------------------
# Two backup runs writing into the same OUTPUT_DIR at once could collide on a same-second
# TIMESTAMP (identical ARCHIVE_NAME) and race each other's publish/prune. flock serializes
# them: a second run refuses immediately rather than corrupting the first's output. The lock
# fd is held for this process's whole lifetime and released automatically on exit. flock is
# util-linux, always present on the Linux VPS these scripts target; if it is somehow absent we
# warn rather than hard-fail, since the per-run mktemp suffixes below still prevent temp-file
# collisions (only a same-second final-name overwrite would remain, which flock is what fully
# closes).
LOCK_FILE="$OUTPUT_DIR/.backup.lock"
if [ "$DRY_RUN" != "1" ]; then
    if command -v flock > /dev/null 2>&1; then
        exec 9> "$LOCK_FILE" || die "Could not open backup lock file: $LOCK_FILE"
        if ! flock -n 9; then
            die "Another backup is already running (lock held on $LOCK_FILE) — refusing to run concurrently."
        fi
        chmod 0600 "$LOCK_FILE" 2> /dev/null || true
    else
        log_warn "flock not found — cannot guard against a concurrent backup run. Proceeding (per-run temp files are unique, but two same-second runs could still overwrite each other's final archive)."
    fi
fi

# Human-readable UTC second-timestamp, recorded in the manifest and used as the SORTABLE
# prefix of every canonical name so `sort` on the filename stays chronological.
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
# Collision-proof backup identifier.
#
# A second-resolution timestamp alone is NOT a unique identifier: two backups started in the
# same wall-clock second (two sequential runs, or an hourly timer that fires twice across a
# clock adjustment) would compute the SAME canonical name `lifeai-backup-$TIMESTAMP.tar.gz`.
# With a clobbering publish that lets a later run overwrite — or, if killed mid-publish, leave
# a mismatched sidecar on — a previously validated recovery point. flock only serializes
# *concurrent* runs; it does nothing for *sequential* same-second ones. So the identifier must
# be unique by construction.
#
# Construction: the sortable second-timestamp, then a high-resolution + random uniqueness
# token. `/dev/urandom` is the primary source (truly unique per run, independent of clock
# resolution and unaffected by a frozen/rewound clock); GNU `date +%N` nanoseconds, when
# available, are folded in so same-second backups also sort in creation order. $RANDOM+$$ are a
# last-resort fallback if /dev/urandom is somehow unreadable. The token only ever lengthens the
# name after the timestamp, so chronological lexical sort (restore discovery, retention) is
# preserved; restore.sh captures the whole id via `(.*)` and reuses it only as a Docker volume
# name, where '-' is valid — so this is discovery/restore compatible (covered by the tests).
_backup_nanos=$(date -u +%N 2>/dev/null)
case "$_backup_nanos" in ''|*[!0-9]*) _backup_nanos="000000000";; esac
_backup_rand=$(od -An -N6 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')
[ -n "$_backup_rand" ] || _backup_rand=$(printf '%08x%04x' "$$" "$RANDOM")
BACKUP_ID="${TIMESTAMP}-${_backup_nanos}-${_backup_rand}"

STAGING_DIR=$(mktemp -d)
# TMP_ARCHIVE / TMP_CHECKSUM hold the in-progress (hidden, non-canonical) archive + checksum
# paths so the EXIT trap removes them if the run dies before OR during atomic publication.
# They never use the canonical `lifeai-backup-*.tar.gz` name, so an interrupted run can never
# leave a file that restore discovery or retention pruning would mistake for a finished backup.
TMP_ARCHIVE=""
TMP_CHECKSUM=""
cleanup() {
    rm -rf "$STAGING_DIR"
    [ -n "$TMP_ARCHIVE" ] && rm -f "$TMP_ARCHIVE"
    [ -n "$TMP_CHECKSUM" ] && rm -f "$TMP_CHECKSUM"
    return 0
}
trap cleanup EXIT

# A previous run hard-killed (SIGKILL / power loss) before its EXIT trap could fire may have
# left a hidden *.partial.* staging file in OUTPUT_DIR. These are inert (never the canonical
# name) but should not accumulate. The flock above guarantees no OTHER backup is running right
# now, so anything matching is a dead run's leftover and safe to remove.
if [ "$DRY_RUN" != "1" ]; then
    shopt -s nullglob
    stale_partials=("$OUTPUT_DIR"/.lifeai-backup-*.partial.*)
    shopt -u nullglob
    for sp in "${stale_partials[@]}"; do
        log_warn "Removing stale incomplete-backup artifact from a previous interrupted run: $sp"
        rm -f "$sp"
    done

    # Orphaned checksum sidecar cleanup. Publication order is checksum-FIRST, archive-LAST (the
    # archive is the atomic commit marker — see the publication block below). A run hard-killed
    # in the window between the two therefore leaves a `lifeai-backup-<id>.tar.gz.sha256` with
    # NO matching archive. That orphan is inert for retention (which globs archives, not
    # sidecars) and for restore (which needs --from <archive>), but its archive id is unique and
    # will never be published, so the sidecar is dead and should not accumulate. flock above
    # guarantees no other backup is mid-publish right now, so a sidecar whose archive is absent
    # is unambiguously such an orphan. We remove ONLY sidecars with no archive — a valid pair is
    # never touched.
    shopt -s nullglob
    orphan_sidecars=("$OUTPUT_DIR"/lifeai-backup-*.tar.gz.sha256)
    shopt -u nullglob
    for sc in "${orphan_sidecars[@]}"; do
        if [ ! -e "${sc%.sha256}" ]; then
            log_warn "Removing orphaned checksum sidecar (its archive was never published — a run killed mid-publish): $sc"
            rm -f "$sc"
        fi
    done
fi

log_info "== 1/4: archiving $COMPOSE_DIR's non-secret files =="
run mkdir -p "$STAGING_DIR/opt-lifeai"
run cp "$COMPOSE_DIR/docker-compose.vps.yml" "$STAGING_DIR/opt-lifeai/"
[ -f "$COMPOSE_DIR/Caddyfile" ] && run cp "$COMPOSE_DIR/Caddyfile" "$STAGING_DIR/opt-lifeai/"
if [ -d "$COMPOSE_DIR/deployments" ]; then
    run mkdir -p "$STAGING_DIR/opt-lifeai/deployments"
    # A glob that matches nothing expands to itself under default (non-nullglob) bash
    # settings — guard so an empty deployments/ directory doesn't try to `cp` a literal
    # "*.json" and fail.
    shopt -s nullglob
    json_files=("$COMPOSE_DIR"/deployments/*.json)
    shopt -u nullglob
    if [ "${#json_files[@]}" -gt 0 ]; then
        run cp "${json_files[@]}" "$STAGING_DIR/opt-lifeai/deployments/"
    fi
fi
run tar czf "$STAGING_DIR/opt-lifeai.tar.gz" -C "$STAGING_DIR/opt-lifeai" .
run rm -rf "$STAGING_DIR/opt-lifeai"

log_info "== 2/4: archiving Docker volumes (caddy_data, caddy_config, lifeai_uploads) =="
BACKED_UP_VOLUMES="[]"
for vol in lifeai_caddy_data lifeai_caddy_config lifeai_uploads; do
    short_name="${vol#lifeai_}"
    if ! docker volume inspect "$vol" &> /dev/null; then
        log_warn "Volume $vol does not exist yet (no deploy has run) — skipping."
        continue
    fi
    if [ "$DRY_RUN" = "1" ]; then
        log_info "[dry-run] would run: docker run --rm -v $vol:/vol:ro -v \$STAGING_DIR:/backup alpine tar czf /backup/$short_name.tar.gz -C /vol ."
    else
        docker run --rm \
            -v "$vol:/vol:ro" \
            -v "$STAGING_DIR:/backup" \
            alpine:3 tar czf "/backup/$short_name.tar.gz" -C /vol .
    fi
    BACKED_UP_VOLUMES=$(echo "$BACKED_UP_VOLUMES" | jq --arg v "$short_name" '. + [$v]')
done

# lifeai_uploads holds content-addressed blobs (see backend/app/storage/local_fs.py) — its
# own checksum is already in the filename/path of every blob it contains
# ({sha256[:2]}/{sha256}), so a lightweight manifest of what's actually in the volume right
# now (not re-hashed here — that's restore.sh's job, against the RESTORED copy, to catch
# corruption introduced by the backup/restore round trip itself) lets a restore verify
# nothing was silently dropped.
if docker volume inspect lifeai_uploads &> /dev/null; then
    if [ "$DRY_RUN" = "1" ]; then
        log_info "[dry-run] would write uploads-manifest.txt (list of content-addressed keys currently in lifeai_uploads)"
    else
        docker run --rm -v lifeai_uploads:/vol:ro alpine:3 \
            sh -c "find /vol -type f -not -path '/vol/tmp/*' | sed 's#^/vol/##' | sort" \
            > "$STAGING_DIR/uploads-manifest.txt"
    fi
fi

log_info "== 3/4: writing manifest and combining into the final archive =="
HOSTNAME_VALUE=$(hostname)
MANIFEST=$(jq -n \
    --arg timestamp "$TIMESTAMP" \
    --arg hostname "$HOSTNAME_VALUE" \
    --arg compose_dir "$COMPOSE_DIR" \
    --argjson backed_up_volumes "$BACKED_UP_VOLUMES" \
    --arg required_env_vars "$LIFEAI_REQUIRED_ENV_VARS" \
    '{
        timestamp: $timestamp,
        hostname: $hostname,
        compose_dir: $compose_dir,
        backed_up_volumes: $backed_up_volumes,
        note: "opt-lifeai.tar.gz and any *_volume.tar.gz members contain no secrets. /etc/lifeai/lifeai.env is intentionally NOT included — see docs/VPS_BACKUP_RESTORE.md. uploads.tar.gz (from lifeai_uploads) holds Life Library original files as content-addressed blobs; uploads-manifest.txt lists every key present at backup time for restore.sh to verify against.",
        required_env_var_names: ($required_env_vars | split(" "))
    }')
if [ "$DRY_RUN" = "1" ]; then
    log_info "[dry-run] would write manifest.json:"
    echo "$MANIFEST"
else
    echo "$MANIFEST" > "$STAGING_DIR/manifest.json"
fi

ARCHIVE_NAME="lifeai-backup-$BACKUP_ID.tar.gz"
ARCHIVE_PATH="$OUTPUT_DIR/$ARCHIVE_NAME"

# --- Atomic, validated, durable, crash-consistent publication -----------------------------
# The final archive is NEVER written directly to its canonical path. A direct write that was
# interrupted (process killed, or ENOSPC mid-write) would leave a truncated
# lifeai-backup-*.tar.gz that retention pruning counts as a real backup — and could then evict
# a genuinely-good older one. Instead: build into a hidden, same-filesystem *.partial.* file,
# prove it is a complete, readable gzip/tar AND compute its checksum BEFORE publishing, then
# publish into the canonical names. A failure at any step leaves only hidden temps (removed by
# the EXIT trap) or, at worst, an orphan sidecar (cleaned on the next run) — never a corrupt or
# mismatched backup, and never damage to an existing one.
#
# Crash-consistency, two guarantees beyond plain atomicity:
#
#  1. No-overwrite publication. We publish with `ln TMP DEST` (a hardlink), NOT `mv`. link(2)
#     fails with EEXIST if DEST already exists, so publication is an atomic test-and-create that
#     can NEVER overwrite a finalized backup or its sidecar. `mv -f` would clobber; `mv -n` is
#     NOT a safe substitute (it is stat-then-rename — a TOCTOU window — and on some platforms a
#     silent no-op "success"), which is why we do not rely on it. Combined with the unique
#     BACKUP_ID this is belt-and-braces: names don't collide, and if they somehow did we fail
#     closed (die) instead of destroying the prior recovery point.
#
#  2. Commit-marker ordering. The checksum sidecar is published and synced FIRST; the archive
#     is published LAST and is the single atomic "this backup is complete" marker that both
#     retention (globs lifeai-backup-*.tar.gz) and restore (--from <archive>) key on. So a crash
#     between the two publishes can only leave an archive-less orphan sidecar (inert; cleaned
#     next run) — never an archive with a missing or mismatched sidecar. The reverse ordering
#     (archive first) is exactly what let a kill between the steps leave an unrestorable,
#     wrong-checksum archive; this ordering makes that state unreachable.
#
# Durability note: `ln`/rename only guarantee a consumer sees old-or-new, not that bytes
# survive power loss while pages/dir entries are dirty. `sync` forces them out before each
# publish and after. This bounds process-kill AND reduces the power-loss window, but a single
# global rename is not a full journaled-commit guarantee — broad power-loss safety is NOT
# claimed from the process-kill tests; see the test header and the DR package.
if [ "$DRY_RUN" = "1" ]; then
    log_info "[dry-run] would build the archive into a hidden .partial file, verify its tar+gzip integrity, write and re-verify its SHA-256, then publish checksum-first then archive-last via atomic no-overwrite links as: $ARCHIVE_PATH (+ .sha256)"
else
    # mktemp creates the file with O_EXCL and mode 0600 — no symlink/path-traversal race, and
    # the random suffix keeps two runs' temp files distinct even if flock were unavailable.
    TMP_ARCHIVE=$(mktemp "$OUTPUT_DIR/.lifeai-backup-$BACKUP_ID.tar.gz.partial.XXXXXX")
    tar czf "$TMP_ARCHIVE" -C "$STAGING_DIR" .
    chmod 0600 "$TMP_ARCHIVE"

    # Integrity gate: a truncated or short-written archive fails to list, so we never publish
    # (or even checksum) a corrupt file. This is the crash-consistency check for the bytes.
    if ! tar tzf "$TMP_ARCHIVE" > /dev/null 2>&1; then
        die "Freshly written archive failed its tar/gzip integrity check (truncated or corrupt — likely an interrupted write or a full disk). NOT publishing; the temp file is cleaned up on exit: $TMP_ARCHIVE"
    fi

    # Checksum is computed from the verified temp file but RECORDED under the final name, so the
    # published .sha256 is immediately valid against the published archive.
    archive_hash=$(sha256sum "$TMP_ARCHIVE" | awk '{print $1}')
    [ -n "$archive_hash" ] || die "Could not compute SHA-256 of the new archive — NOT publishing."
    TMP_CHECKSUM=$(mktemp "$OUTPUT_DIR/.lifeai-backup-$BACKUP_ID.tar.gz.sha256.partial.XXXXXX")
    printf '%s  %s\n' "$archive_hash" "$ARCHIVE_NAME" > "$TMP_CHECKSUM"
    chmod 0600 "$TMP_CHECKSUM"

    # Fail-closed pre-check: never publish onto an existing finalized backup. With a unique
    # BACKUP_ID this is unreachable in practice, so if either canonical name already exists it
    # signals an id collision or a stray file — abort rather than risk destroying a recovery
    # point. (The atomic `ln` below enforces this regardless; this gives a clear early error.)
    if [ -e "$ARCHIVE_PATH" ] || [ -e "$ARCHIVE_PATH.sha256" ]; then
        die "Refusing to publish: a backup already exists at $ARCHIVE_PATH(.sha256). Not overwriting a finalized backup."
    fi

    # Durability: flush the verified temp bytes before publishing any canonical name.
    sync

    # Publish CHECKSUM FIRST (atomic, no-overwrite), then fsync it to disk, so the sidecar is
    # durable before the archive — the commit marker — can appear. `ln` fails if the name
    # exists: no clobber, no TOCTOU.
    ln "$TMP_CHECKSUM" "$ARCHIVE_PATH.sha256" \
        || die "Could not publish checksum sidecar (name already exists or link failed): $ARCHIVE_PATH.sha256"
    rm -f "$TMP_CHECKSUM"
    TMP_CHECKSUM=""
    sync

    # Publish ARCHIVE LAST (atomic, no-overwrite) — this is the single step that makes the
    # backup visible to retention and restore. A crash before this leaves only the synced
    # orphan sidecar (cleaned next run); a crash after this leaves a complete, verifiable pair.
    ln "$TMP_ARCHIVE" "$ARCHIVE_PATH" \
        || die "Could not publish archive (name already exists or link failed): $ARCHIVE_PATH"
    rm -f "$TMP_ARCHIVE"
    TMP_ARCHIVE=""
    sync

    # Post-publish verification: prove the published pair verifies exactly as restore.sh will
    # check it. By construction this cannot fail, but a backup that silently didn't verify is
    # worse than one that loudly did, so we assert rather than assume.
    if ! (cd "$OUTPUT_DIR" && sha256sum -c "$ARCHIVE_NAME.sha256" > /dev/null 2>&1); then
        die "Published archive failed post-publish checksum verification: $ARCHIVE_PATH"
    fi
    log_info "Published and verified: $ARCHIVE_PATH (sha256 OK)"
fi

log_info "== 4/4: pruning old backups (keeping the most recent $KEEP complete ones) =="
if [ "$KEEP" -gt 0 ]; then
    # Retention counts ONLY complete, verifiable backups: a canonical archive that has a
    # matching .sha256 which actually verifies. An incomplete/corrupt/partial archive is
    # therefore never counted toward $KEEP and never triggers deletion of a genuinely-good
    # older backup — the exact failure this hardening exists to prevent. Such files are left in
    # place (and surfaced) rather than silently deleted, so an operator can investigate.
    shopt -s nullglob
    all_archives=("$OUTPUT_DIR"/lifeai-backup-*.tar.gz)
    shopt -u nullglob
    valid_archives=()
    if [ "${#all_archives[@]}" -gt 0 ]; then
        # Newest first by filename — the UTC timestamp in the name sorts lexically by time.
        mapfile -t sorted_archives < <(printf '%s\n' "${all_archives[@]}" | sort -r)
        for arc in "${sorted_archives[@]}"; do
            arc_name=$(basename -- "$arc")
            if [ -f "$arc.sha256" ] && (cd "$OUTPUT_DIR" && sha256sum -c "$arc_name.sha256" > /dev/null 2>&1); then
                valid_archives+=("$arc")
            else
                log_warn "Ignoring for retention (no valid .sha256 — incomplete or corrupt; NOT counted toward --keep and NOT deleted): $arc_name"
            fi
        done
    fi
    # Keep the newest $KEEP valid archives; delete only the surplus VALID ones.
    if [ "${#valid_archives[@]}" -gt "$KEEP" ]; then
        for old in "${valid_archives[@]:$KEEP}"; do
            log_info "Pruning old backup: $(basename -- "$old")"
            run rm -f "$old" "$old.sha256"
        done
    fi
else
    log_warn "--keep 0: not pruning anything (0 means unlimited retention, not delete-all)."
fi

log_info "Backup complete: $ARCHIVE_PATH"
log_info "Verify with: sha256sum -c $ARCHIVE_PATH.sha256"
log_info "Restore with: ./restore.sh --from $ARCHIVE_PATH --target-dir <disposable-dir> --confirm"
