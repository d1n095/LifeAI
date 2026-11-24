#!/usr/bin/env bash
# Staging counterpart to backup.sh — same approach (archive the compose dir's non-secret
# files + the named Docker volumes, write a checksum, prune old archives), adapted for
# docker-compose.staging.yml's own volume names and Compose project ("lifeai-staging", not
# "lifeai"). Kept as its own small script rather than parameterizing backup.sh itself: that
# script is production-critical and already covered by CI's vps-scripts-check job against its
# exact current behavior — changing its interface to serve two different volume sets would
# risk the one backup path the real VPS actually depends on.
#
# Unlike production, staging's Postgres is NOT external (no Supabase here — see
# docker-compose.staging.yml's own comment) — so this script also dumps the staging database
# itself via `pg_dump`, reusing the same tool docs/VPS_BACKUP_RESTORE.md already documents for
# Postgres-level backups elsewhere in this project, inside the running `postgres` container
# rather than reinventing a different backup mechanism for it.
#
# Usage: ./backup-staging.sh [--dry-run] [--compose-dir /opt/lifeai-staging] [--output-dir /var/backups/lifeai-staging] [--keep N]
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

parse_common_flags "$@"

COMPOSE_DIR="/opt/lifeai-staging"
OUTPUT_DIR="/var/backups/lifeai-staging"
KEEP=5
PROJECT="lifeai-staging"

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
[ -f "$COMPOSE_DIR/docker-compose.staging.yml" ] || die "Missing $COMPOSE_DIR/docker-compose.staging.yml."
[ -d "$OUTPUT_DIR" ] || run mkdir -p "$OUTPUT_DIR"

TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
STAGING_DIR=$(mktemp -d)
trap 'rm -rf "$STAGING_DIR"' EXIT

log_info "== 1/4: archiving $COMPOSE_DIR's non-secret files =="
run mkdir -p "$STAGING_DIR/opt-lifeai-staging"
run cp "$COMPOSE_DIR/docker-compose.staging.yml" "$STAGING_DIR/opt-lifeai-staging/"
[ -f "$COMPOSE_DIR/Caddyfile.staging" ] && run cp "$COMPOSE_DIR/Caddyfile.staging" "$STAGING_DIR/opt-lifeai-staging/"

log_info "== 2/4: dumping the staging Postgres database =="
if [ "$DRY_RUN" = "1" ]; then
    log_info "[dry-run] would run: docker compose -p $PROJECT -f $COMPOSE_DIR/docker-compose.staging.yml exec -T postgres pg_dump -U lifeos_staging lifeos_staging > db.sql"
else
    (cd "$COMPOSE_DIR" && docker compose -p "$PROJECT" -f docker-compose.staging.yml exec -T postgres \
        pg_dump -U lifeos_staging lifeos_staging) > "$STAGING_DIR/db.sql"
fi

log_info "== 3/4: archiving Docker volumes (${PROJECT}_staging_uploads, ${PROJECT}_staging_caddy_data, ${PROJECT}_staging_caddy_config) =="
BACKED_UP_VOLUMES="[]"
for vol in "${PROJECT}_staging_uploads" "${PROJECT}_staging_caddy_data" "${PROJECT}_staging_caddy_config"; do
    short_name="${vol#"${PROJECT}"_}"
    if ! docker volume inspect "$vol" &> /dev/null; then
        log_warn "Volume $vol does not exist yet (no deploy has run) — skipping."
        continue
    fi
    if [ "$DRY_RUN" = "1" ]; then
        log_info "[dry-run] would run: docker run --rm -v $vol:/vol:ro -v \$STAGING_DIR:/backup alpine tar czf /backup/$short_name.tar.gz -C /vol ."
    else
        docker run --rm -v "$vol:/vol:ro" -v "$STAGING_DIR:/backup" \
            alpine:3 tar czf "/backup/$short_name.tar.gz" -C /vol .
    fi
    BACKED_UP_VOLUMES=$(echo "$BACKED_UP_VOLUMES" | jq --arg v "$short_name" '. + [$v]')
done

log_info "== 4/4: writing manifest, combining into the final archive, pruning =="
MANIFEST=$(jq -n \
    --arg timestamp "$TIMESTAMP" \
    --arg project "$PROJECT" \
    --argjson backed_up_volumes "$BACKED_UP_VOLUMES" \
    '{timestamp: $timestamp, project: $project, backed_up_volumes: $backed_up_volumes,
      note: "Staging backup: includes a real pg_dump (db.sql) unlike production backup.sh, since staging runs its own disposable Postgres instead of Supabase. No secrets included — STAGING_* passwords live only in this host'\''s env file, never in this archive."}')
if [ "$DRY_RUN" = "1" ]; then
    log_info "[dry-run] would write manifest.json:"
    echo "$MANIFEST"
else
    echo "$MANIFEST" > "$STAGING_DIR/manifest.json"
fi

ARCHIVE_NAME="lifeai-staging-backup-$TIMESTAMP.tar.gz"
ARCHIVE_PATH="$OUTPUT_DIR/$ARCHIVE_NAME"
run tar czf "$ARCHIVE_PATH" -C "$STAGING_DIR" .
run chmod 0600 "$ARCHIVE_PATH"
if [ "$DRY_RUN" != "1" ]; then
    (cd "$OUTPUT_DIR" && sha256sum "$ARCHIVE_NAME" > "$ARCHIVE_NAME.sha256")
    run chmod 0600 "$ARCHIVE_PATH.sha256"
fi

if [ "$KEEP" -gt 0 ]; then
    mapfile -t old_archives < <(find "$OUTPUT_DIR" -maxdepth 1 -name 'lifeai-staging-backup-*.tar.gz' -printf '%f\n' 2> /dev/null | sort -r | tail -n "+$((KEEP + 1))")
    for old in "${old_archives[@]}"; do
        log_info "Pruning old backup: $old"
        run rm -f "$OUTPUT_DIR/$old" "$OUTPUT_DIR/$old.sha256"
    done
fi

log_info "Backup complete: $ARCHIVE_PATH"
log_info "Verify with: sha256sum -c $ARCHIVE_PATH.sha256"
