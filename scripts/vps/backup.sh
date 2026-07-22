#!/usr/bin/env bash
# Backs up the VPS-hosted state that deploy.sh/rollback.sh can't reconstruct on their own:
#   - /opt/lifeai's non-secret files: docker-compose.vps.yml, Caddyfile, deployment records
#     (scripts/vps/deploy.sh's and rollback.sh's *.json history — digests/timestamps/results
#     only, never secrets, see deploy.sh's own record-writing code).
#   - the caddy_data and caddy_config named Docker volumes (TLS certificates + Caddy's own
#     autosaved config) — the only persistent Docker volumes docker-compose.vps.yml defines;
#     backend/frontend have none (see docs/VPS_ARCHITECTURE.md).
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
#   - Uploaded files: this app stores none on the VPS's own disk — document content lives in
#     Postgres (document_chunks), not local files (see docs/VPS_ARCHITECTURE.md).
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

TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
STAGING_DIR=$(mktemp -d)
trap 'rm -rf "$STAGING_DIR"' EXIT

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

log_info "== 2/4: archiving Docker volumes (caddy_data, caddy_config) =="
BACKED_UP_VOLUMES="[]"
for vol in lifeai_caddy_data lifeai_caddy_config; do
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
        note: "opt-lifeai.tar.gz and any *_volume.tar.gz members contain no secrets. /etc/lifeai/lifeai.env is intentionally NOT included — see docs/VPS_BACKUP_RESTORE.md.",
        required_env_var_names: ($required_env_vars | split(" "))
    }')
if [ "$DRY_RUN" = "1" ]; then
    log_info "[dry-run] would write manifest.json:"
    echo "$MANIFEST"
else
    echo "$MANIFEST" > "$STAGING_DIR/manifest.json"
fi

ARCHIVE_NAME="lifeai-backup-$TIMESTAMP.tar.gz"
ARCHIVE_PATH="$OUTPUT_DIR/$ARCHIVE_NAME"
run tar czf "$ARCHIVE_PATH" -C "$STAGING_DIR" .
run chmod 0600 "$ARCHIVE_PATH"

if [ "$DRY_RUN" = "1" ]; then
    log_info "[dry-run] would write checksum file: $ARCHIVE_PATH.sha256"
else
    (cd "$OUTPUT_DIR" && sha256sum "$ARCHIVE_NAME" > "$ARCHIVE_NAME.sha256")
    run chmod 0600 "$ARCHIVE_PATH.sha256"
fi

log_info "== 4/4: pruning old backups (keeping the most recent $KEEP) =="
if [ "$KEEP" -gt 0 ]; then
    # List oldest-first, drop the newest $KEEP, delete what's left. -r on sort reverses;
    # tail -n +N skips the first N-1 lines, i.e. keeps everything AFTER the newest $KEEP.
    mapfile -t old_archives < <(find "$OUTPUT_DIR" -maxdepth 1 -name 'lifeai-backup-*.tar.gz' -printf '%f\n' 2> /dev/null | sort -r | tail -n "+$((KEEP + 1))")
    for old in "${old_archives[@]}"; do
        log_info "Pruning old backup: $old"
        run rm -f "$OUTPUT_DIR/$old" "$OUTPUT_DIR/$old.sha256"
    done
else
    log_warn "--keep 0: not pruning anything (0 means unlimited retention, not delete-all)."
fi

log_info "Backup complete: $ARCHIVE_PATH"
log_info "Verify with: sha256sum -c $ARCHIVE_PATH.sha256"
log_info "Restore with: ./restore.sh --from $ARCHIVE_PATH --target-dir <disposable-dir> --confirm"
