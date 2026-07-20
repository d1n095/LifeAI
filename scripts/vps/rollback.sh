#!/usr/bin/env bash
# Deterministic rollback: finds the most recent deployment record with result="success" in
# $COMPOSE_DIR/deployments/, restores ITS backend/frontend image digests into the secrets
# file, pulls and starts them, and verifies health. Called automatically by deploy.sh when a
# fresh deployment fails its own post-deploy verification, and safe to run by hand too.
#
# Contains no real server address, username, domain, token, password, or SSH key — like
# deploy.sh, it only ever acts on whatever is already on this machine.
#
# Usage: sudo ./rollback.sh --confirm [--compose-dir /opt/lifeai] [--env-file /etc/lifeai/lifeai.env]
#
# --extra-compose-file <path> is a TESTING-ONLY escape hatch (used by
# .github/workflows/ci.yml's vps-deploy-rollback-test job to layer in
# docker-compose.vps.ci.yml's host.docker.internal wiring for the CI runner's own
# Postgres/Redis) — never set on the real VPS, where a single compose file is always
# correct and this stays unset.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

COMPOSE_DIR="/opt/lifeai"
ENV_FILE="/etc/lifeai/lifeai.env"
EXTRA_COMPOSE_FILE=""
CONFIRMED=0

while [ $# -gt 0 ]; do
    case "$1" in
        --confirm)
            CONFIRMED=1
            shift
            ;;
        --compose-dir)
            COMPOSE_DIR="$2"
            shift 2
            ;;
        --env-file)
            ENV_FILE="$2"
            shift 2
            ;;
        --extra-compose-file)
            EXTRA_COMPOSE_FILE="$2"
            shift 2
            ;;
        *)
            die "Unknown argument: $1"
            ;;
    esac
done

if [ "$CONFIRMED" != "1" ]; then
    die "Refusing to roll back without --confirm. Re-run as: sudo ./rollback.sh --confirm"
fi

require_root

COMPOSE_FILE="$COMPOSE_DIR/docker-compose.vps.yml"
DEPLOYMENTS_DIR="$COMPOSE_DIR/deployments"
[ -f "$COMPOSE_FILE" ] || die "Missing $COMPOSE_FILE."
[ -f "$ENV_FILE" ] || die "Missing $ENV_FILE."
[ -d "$DEPLOYMENTS_DIR" ] || die "Missing $DEPLOYMENTS_DIR — nothing to roll back to."

compose() {
    if [ -n "$EXTRA_COMPOSE_FILE" ]; then
        docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" -f "$EXTRA_COMPOSE_FILE" -p lifeai "$@"
    else
        docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" -p lifeai "$@"
    fi
}

# The most recent record whose OWN deploy succeeded — not counting the record currently being
# rolled back FROM (which, if this is being called by deploy.sh, was just marked
# failed_verification and therefore already excluded by this same result="success" filter).
TARGET_RECORD=$(find "$DEPLOYMENTS_DIR" -maxdepth 1 -name '*.json' -print0 \
    | xargs -0 -r jq -s '[.[] | select(.result == "success")] | sort_by(.timestamp) | last')

if [ -z "$TARGET_RECORD" ] || [ "$TARGET_RECORD" = "null" ]; then
    die "No previous successful deployment record found in $DEPLOYMENTS_DIR — nothing to roll back to. This must be resolved manually (see docs/VPS_OPERATIONS_RUNBOOK.md)."
fi

TARGET_BACKEND=$(echo "$TARGET_RECORD" | jq -r '.backend_image')
TARGET_FRONTEND=$(echo "$TARGET_RECORD" | jq -r '.frontend_image')
TARGET_TIMESTAMP=$(echo "$TARGET_RECORD" | jq -r '.timestamp')
log_info "Rolling back to deployment $TARGET_TIMESTAMP:"
log_info "  BACKEND_IMAGE=$TARGET_BACKEND"
log_info "  FRONTEND_IMAGE=$TARGET_FRONTEND"

# In-place, line-targeted replacement — touches ONLY the BACKEND_IMAGE/FRONTEND_IMAGE lines,
# never any other line in the secrets file (no secret value is ever read into this script's
# own variables beyond these two non-secret image references).
run bash -c 'sed -i -E "s|^BACKEND_IMAGE=.*|BACKEND_IMAGE=$1|" "$2"' _ "$TARGET_BACKEND" "$ENV_FILE"
run bash -c 'sed -i -E "s|^FRONTEND_IMAGE=.*|FRONTEND_IMAGE=$1|" "$2"' _ "$TARGET_FRONTEND" "$ENV_FILE"

log_info "Pulling and starting the rolled-back images."
compose pull
compose up -d

log_info "Waiting up to 120s for services to become healthy after rollback."
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
        if [ "$status" != "healthy" ] && [ "$status" != "no-healthcheck" ]; then
            UNHEALTHY=1
        fi
    done
    if [ "$UNHEALTHY" = "0" ]; then
        ALL_HEALTHY=1
        break
    fi
    sleep 3
done

ROLLBACK_TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
if [ "$ALL_HEALTHY" = "1" ]; then
    log_info "Rollback successful — all services healthy on the restored images."
    jq -n --arg ts "$ROLLBACK_TIMESTAMP" --arg target "$TARGET_TIMESTAMP" '{timestamp: $ts, rolled_back_to: $target, result: "success"}' \
        > "$DEPLOYMENTS_DIR/$ROLLBACK_TIMESTAMP-rollback.json"
    compose ps
    exit 0
else
    log_error "Rollback completed but services did not become healthy. This needs immediate manual investigation — see docs/VPS_OPERATIONS_RUNBOOK.md."
    jq -n --arg ts "$ROLLBACK_TIMESTAMP" --arg target "$TARGET_TIMESTAMP" '{timestamp: $ts, rolled_back_to: $target, result: "failed"}' \
        > "$DEPLOYMENTS_DIR/$ROLLBACK_TIMESTAMP-rollback.json"
    exit 1
fi
