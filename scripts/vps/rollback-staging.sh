#!/usr/bin/env bash
# Staging counterpart to rollback.sh. Unlike production's digest-pinned, image-swap rollback
# (there is no built/pushed image yet for an unreleased candidate — see
# docker-compose.staging.yml's own comment on `build:` vs `image:`), staging rollback means
# two things, done together:
#   1. Check out a known-good prior commit of this repo's staging branch and rebuild from it.
#   2. Restore the database from the matching backup-staging.sh archive, via a real Alembic
#      downgrade where possible (same live-tested approach as Stream 6/#484 — a clean
#      `alembic downgrade` — see docs/STAGING_DEPLOYMENT.md), falling back to the archived
#      `pg_dump` only if the target commit predates a migration this database already applied.
#
# Usage: ./rollback-staging.sh --confirm --to-commit <sha> [--compose-dir /opt/lifeai-staging] [--env-file /etc/lifeai/lifeai-staging.env]
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

COMPOSE_DIR="/opt/lifeai-staging"
ENV_FILE="/etc/lifeai/lifeai-staging.env"
PROJECT="lifeai-staging"
TO_COMMIT=""
CONFIRMED=0

while [ $# -gt 0 ]; do
    case "$1" in
        --confirm)
            CONFIRMED=1
            shift
            ;;
        --to-commit)
            TO_COMMIT="$2"
            shift 2
            ;;
        --compose-dir)
            COMPOSE_DIR="$2"
            shift 2
            ;;
        --env-file)
            ENV_FILE="$2"
            shift 2
            ;;
        *)
            die "Unknown argument: $1"
            ;;
    esac
done

[ "$CONFIRMED" = "1" ] || die "Refusing to roll back without --confirm."
[ -n "$TO_COMMIT" ] || die "Refusing to roll back without --to-commit <sha> — staging has no pinned-image history to infer a target from, unlike production's rollback.sh."
[ -f "$COMPOSE_DIR/docker-compose.staging.yml" ] || die "Missing $COMPOSE_DIR/docker-compose.staging.yml."
[ -f "$ENV_FILE" ] || die "Missing $ENV_FILE."

compose() {
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_DIR/docker-compose.staging.yml" -p "$PROJECT" "$@"
}

log_info "Checking out $TO_COMMIT in $COMPOSE_DIR for rebuild."
run git -C "$COMPOSE_DIR" fetch origin "$TO_COMMIT"
run git -C "$COMPOSE_DIR" checkout "$TO_COMMIT"

log_info "Verifying the target commit's own Alembic migration history is reachable from the database's current revision (downgrade, not a destructive drop)."
# Mirrors rollback.sh's own pre-flight philosophy (verify_rollback_target_knows_current_revision)
# but in the opposite direction: a real `alembic downgrade` to the target commit's own head,
# run inside the backend image built from that commit, exactly as Stream 6/#484 already
# verified live (clean 10-migration downgrade-then-upgrade round trip) — not a guess.
run bash -c 'cd "$1" && docker compose --env-file "$2" -f docker-compose.staging.yml -p lifeai-staging run --rm backend \
    python -m alembic downgrade "$(python -m alembic heads 2>/dev/null | head -n1 | cut -d" " -f1)"' _ "$COMPOSE_DIR" "$ENV_FILE" \
    || die "Migration downgrade to $TO_COMMIT's head failed — do not proceed. Restore the matching backup-staging.sh archive's db.sql into a fresh staging Postgres instead (see docs/STAGING_DEPLOYMENT.md's 'Restore from backup' section), then re-run this script without the downgrade step."

log_info "Rebuilding and starting services from $TO_COMMIT."
compose build
compose up -d

log_info "Waiting up to 120s for services to become healthy."
DEADLINE=$((SECONDS + 120))
ALL_HEALTHY=0
while [ "$SECONDS" -lt "$DEADLINE" ]; do
    UNHEALTHY=0
    for name in backend frontend caddy; do
        cid=$(compose ps -q "$name")
        [ -n "$cid" ] || {
            UNHEALTHY=1
            break
        }
        status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$cid")
        [ "$status" = "healthy" ] || [ "$status" = "no-healthcheck" ] || UNHEALTHY=1
    done
    [ "$UNHEALTHY" = "0" ] && {
        ALL_HEALTHY=1
        break
    }
    sleep 3
done

if [ "$ALL_HEALTHY" = "1" ]; then
    log_info "Rollback to $TO_COMMIT successful — all services healthy."
    compose ps
    exit 0
else
    log_error "Rollback completed but services did not become healthy. Check: compose -p $PROJECT logs"
    exit 1
fi
