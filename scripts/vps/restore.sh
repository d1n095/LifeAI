#!/usr/bin/env bash
# Restores a backup.sh archive. By design, restores ONLY into an explicit, disposable target
# directory — never in place over a live /opt/lifeai, and never over the live caddy_data/
# caddy_config Docker volumes — unless --overwrite-live-volumes is passed in addition to
# --confirm, a second, deliberately separate gate for the one genuinely destructive path.
#
# Verifies the archive's checksum and structure BEFORE extracting anything, so a corrupted or
# truncated backup file is refused loudly instead of silently restoring partial/garbage state.
#
# Usage:
#   ./restore.sh --from <path/to/lifeai-backup-TIMESTAMP.tar.gz> --target-dir <dir> --confirm
#     [--overwrite-live-volumes]
#
# After a normal (non---overwrite-live-volumes) restore, the caddy_data/caddy_config contents
# land in NEW Docker volumes named lifeai_restore_<timestamp>_caddy_data/_caddy_config —
# inspect them, then manually decide whether/how to bring that state into the live stack (see
# docs/VPS_BACKUP_RESTORE.md's restore drill). This script never makes that call for you.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

parse_common_flags "$@"

FROM=""
TARGET_DIR=""
CONFIRMED=0
OVERWRITE_LIVE_VOLUMES=0

while [ $# -gt 0 ]; do
    case "$1" in
        --from)
            FROM="$2"
            shift 2
            ;;
        --target-dir)
            TARGET_DIR="$2"
            shift 2
            ;;
        --confirm)
            CONFIRMED=1
            shift
            ;;
        --overwrite-live-volumes)
            OVERWRITE_LIVE_VOLUMES=1
            shift
            ;;
        --dry-run | --yes | -y)
            shift
            ;;
        *)
            die "Unknown argument: $1"
            ;;
    esac
done

[ -n "$FROM" ] || die "Missing --from <path/to/lifeai-backup-TIMESTAMP.tar.gz>."
[ -n "$TARGET_DIR" ] || die "Missing --target-dir <disposable-dir>. Restoring in place over a live deployment is refused by design — see this script's header comment."
[ "$CONFIRMED" = "1" ] || die "Refusing to restore without --confirm."
[ -f "$FROM" ] || die "Backup archive not found: $FROM"

case "$TARGET_DIR" in
    /opt/lifeai | /opt/lifeai/*)
        die "Refusing to target /opt/lifeai directly — pass a separate, disposable directory and copy in what you need by hand once you've inspected it. See docs/VPS_BACKUP_RESTORE.md."
        ;;
esac

log_info "== 1/4: verifying archive integrity =="
CHECKSUM_FILE="$FROM.sha256"
if [ -f "$CHECKSUM_FILE" ]; then
    # sha256sum -c expects the checksum file's recorded filename to resolve relative to the
    # current directory — cd into the archive's own directory so a --from path with leading
    # directories still verifies correctly.
    FROM_DIR=$(cd -- "$(dirname -- "$FROM")" &> /dev/null && pwd)
    FROM_BASENAME=$(basename -- "$FROM")
    if ! (cd "$FROM_DIR" && sha256sum -c "$(basename -- "$CHECKSUM_FILE")" &> /dev/null); then
        die "Checksum verification FAILED for $FROM — the archive is corrupted or was tampered with. Refusing to restore. (Expected checksum: $CHECKSUM_FILE, file: $FROM_BASENAME)"
    fi
    log_info "Checksum OK."
else
    log_warn "No checksum file found at $CHECKSUM_FILE — proceeding without integrity verification. Only continue if you trust the source of this archive."
fi

if ! tar tzf "$FROM" &> /dev/null; then
    die "Archive is not a valid tar.gz (corrupt or truncated): $FROM"
fi

log_info "== 2/4: extracting into $TARGET_DIR =="
run mkdir -p "$TARGET_DIR"
STAGING_DIR=$(mktemp -d)
trap 'rm -rf "$STAGING_DIR"' EXIT
if [ "$DRY_RUN" = "1" ]; then
    log_info "[dry-run] would run: tar xzf $FROM -C $STAGING_DIR"
else
    tar xzf "$FROM" -C "$STAGING_DIR"
fi

[ "$DRY_RUN" = "1" ] || [ -f "$STAGING_DIR/manifest.json" ] || die "Archive is missing manifest.json — not a valid backup.sh archive."
if [ "$DRY_RUN" != "1" ]; then
    log_info "Manifest:"
    jq . "$STAGING_DIR/manifest.json"
fi

if [ "$DRY_RUN" = "1" ]; then
    log_info "[dry-run] would extract opt-lifeai.tar.gz into $TARGET_DIR"
else
    [ -f "$STAGING_DIR/opt-lifeai.tar.gz" ] || die "Archive is missing opt-lifeai.tar.gz."
    tar xzf "$STAGING_DIR/opt-lifeai.tar.gz" -C "$TARGET_DIR"
fi
log_info "Extracted docker-compose.vps.yml, Caddyfile, and deployment records into $TARGET_DIR."
log_info "NOTE: /etc/lifeai/lifeai.env was never in this archive — re-provision secrets from your own secret store before using this restore for a real deploy. See docs/VPS_SECRETS_INVENTORY.md."

log_info "== 3/4: restoring Docker volumes =="
BACKUP_TIMESTAMP=$(basename -- "$FROM" | sed -E 's/^lifeai-backup-(.*)\.tar\.gz$/\1/')
for short_name in caddy_data caddy_config; do
    vol_archive="$STAGING_DIR/$short_name.tar.gz"
    [ -f "$vol_archive" ] || { log_warn "$short_name.tar.gz not present in this backup — skipping."; continue; }

    if [ "$OVERWRITE_LIVE_VOLUMES" = "1" ]; then
        target_volume="lifeai_$short_name"
        log_warn "--overwrite-live-volumes: restoring directly into the LIVE volume $target_volume. This overwrites whatever is running right now."
    else
        target_volume="lifeai_restore_${BACKUP_TIMESTAMP}_${short_name}"
    fi

    if [ "$DRY_RUN" = "1" ]; then
        log_info "[dry-run] would run: docker volume create $target_volume && docker run --rm -v $target_volume:/vol -v \$STAGING_DIR:/backup:ro alpine sh -c 'rm -rf /vol/* /vol/..?* /vol/.[!.]* 2>/dev/null; tar xzf /backup/$short_name.tar.gz -C /vol'"
    else
        docker volume create "$target_volume" > /dev/null
        docker run --rm \
            -v "$target_volume:/vol" \
            -v "$STAGING_DIR:/backup:ro" \
            alpine:3 sh -c "rm -rf /vol/* /vol/..?* /vol/.[!.]* 2>/dev/null; tar xzf /backup/$short_name.tar.gz -C /vol"
        log_info "Restored $short_name into volume: $target_volume"
    fi
done

log_info "== 4/4: done =="
if [ "$OVERWRITE_LIVE_VOLUMES" = "1" ]; then
    log_warn "Live volumes were overwritten. Restart the affected services (docker compose restart caddy) if they were already running."
else
    log_info "Restored into disposable target: $TARGET_DIR (files) and lifeai_restore_${BACKUP_TIMESTAMP}_* (volumes)."
    log_info "Nothing live was touched. Inspect the restored state, then follow docs/VPS_BACKUP_RESTORE.md's restore drill to bring it into production if needed."
fi
