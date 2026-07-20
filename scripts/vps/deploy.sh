#!/usr/bin/env bash
# Manual-gated production deployment. Refuses to run at all unless --confirm is passed
# explicitly — this script NEVER runs unattended, is NEVER called from CI, and contains no
# real server address, username, domain, token, password, or SSH key: it operates entirely on
# whatever docker-compose.vps.yml/Caddyfile/lifeai.env ALREADY on this machine say, which is
# exactly why it's safe to commit to a public-ish repo in the first place.
#
# What it does, in order:
#   1. Validates required local files exist (compose file, Caddyfile, secrets file).
#   2. Validates required env var NAMES are present in the secrets file — never prints values.
#   3. Best-effort verification that GHCR credentials are configured.
#   4. Pulls the exact image digests named in the secrets file (BACKEND_IMAGE/FRONTEND_IMAGE).
#   5. Verifies the pulled images actually match this host's CPU architecture.
#   6. Validates the Compose config and the Caddyfile.
#   7. Records a timestamped deployment entry (digests, checksums, git commit) BEFORE
#      starting anything, so a rollback always has something to roll back to even if this
#      deploy itself fails.
#   8. Starts services (Compose's own depends_on/condition:service_healthy already orders
#      backend -> frontend -> caddy correctly).
#   9. Waits, bounded, for all services to report healthy.
#  10. Runs a real end-to-end request through Caddy.
#  11. On ANY failure in steps 9-10: automatically invokes rollback.sh against the previous
#      known-good record and reports which happened.
#
# Usage: sudo ./deploy.sh --confirm [--compose-dir /opt/lifeai] [--env-file /etc/lifeai/lifeai.env] [--timeout 180]
#
# --extra-compose-file is a TESTING-ONLY escape hatch (used by
# .github/workflows/ci.yml's vps-deploy-rollback-test job to layer in
# docker-compose.vps.ci.yml's host.docker.internal wiring for the CI runner's own Postgres/
# Redis) — never set on the real VPS, where a single compose file is always correct and this
# stays unset.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

COMPOSE_DIR="/opt/lifeai"
ENV_FILE="/etc/lifeai/lifeai.env"
HEALTH_TIMEOUT=180
CONFIRMED=0
EXTRA_COMPOSE_FILE=""

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
        --timeout)
            HEALTH_TIMEOUT="$2"
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
    die "Refusing to deploy without --confirm. This is the deliberate manual gate — see docs/STRATO_VPS_DEPLOY.md's 'Steg 5'. Re-run as: sudo ./deploy.sh --confirm"
fi

require_root

COMPOSE_FILE="$COMPOSE_DIR/docker-compose.vps.yml"
CADDYFILE="$COMPOSE_DIR/Caddyfile"
DEPLOYMENTS_DIR="$COMPOSE_DIR/deployments"

log_info "== Step 1/11: validating required local files =="
[ -f "$COMPOSE_FILE" ] || die "Missing $COMPOSE_FILE."
[ -f "$CADDYFILE" ] || die "Missing $CADDYFILE."
[ -f "$ENV_FILE" ] || die "Missing $ENV_FILE — see docs/STRATO_VPS_DEPLOY.md Steg 2."
[ -d "$DEPLOYMENTS_DIR" ] || die "Missing $DEPLOYMENTS_DIR — run 30_setup_directories.sh first."
log_info "OK."

log_info "== Step 2/11: validating required secret NAMES are present (values never printed) =="
REQUIRED_VARS="SECRET_KEY MAINAI_APP_PASSWORD FOUNDER_EMAIL FOUNDER_PASSWORD DATABASE_URL REDIS_URL FRONTEND_ORIGINS PUBLIC_APP_URL SMTP_HOST SMTP_PORT SMTP_USERNAME SMTP_PASSWORD SMTP_FROM_EMAIL DOMAIN BACKEND_IMAGE FRONTEND_IMAGE"
MISSING_VARS=""
for var in $REQUIRED_VARS; do
    if ! grep -qE "^${var}=" "$ENV_FILE"; then
        MISSING_VARS="$MISSING_VARS $var"
    fi
done
if [ -n "$MISSING_VARS" ]; then
    die "Missing required variable(s) in $ENV_FILE (names only, not values):$MISSING_VARS — see .env.vps.example."
fi
log_info "All required variable names present."

log_info "== Step 3/11: checking GHCR credentials are configured =="
DOCKER_CONFIG_FILE="${DOCKER_CONFIG:-$HOME/.docker}/config.json"
if [ -f "$DOCKER_CONFIG_FILE" ] && { jq -e '.auths["ghcr.io"] // empty' "$DOCKER_CONFIG_FILE" &> /dev/null || jq -e '.credHelpers["ghcr.io"] // .credsStore // empty' "$DOCKER_CONFIG_FILE" &> /dev/null; }; then
    log_info "GHCR credential configuration found in $DOCKER_CONFIG_FILE."
else
    die "No GHCR credential configuration found in $DOCKER_CONFIG_FILE. Run: docker login ghcr.io -u <github-username> --password-stdin (see docs/STRATO_VPS_DEPLOY.md Steg 3)."
fi

BACKEND_IMAGE=$(grep -E "^BACKEND_IMAGE=" "$ENV_FILE" | head -n1 | cut -d= -f2-)
FRONTEND_IMAGE=$(grep -E "^FRONTEND_IMAGE=" "$ENV_FILE" | head -n1 | cut -d= -f2-)
[ -n "$BACKEND_IMAGE" ] || die "BACKEND_IMAGE is empty in $ENV_FILE."
[ -n "$FRONTEND_IMAGE" ] || die "FRONTEND_IMAGE is empty in $ENV_FILE."
case "$BACKEND_IMAGE" in
    *@sha256:*) ;;
    *) die "BACKEND_IMAGE ('$BACKEND_IMAGE') is not digest-pinned (expected '...@sha256:...'). Never deploy a mutable tag." ;;
esac
case "$FRONTEND_IMAGE" in
    *@sha256:*) ;;
    *) die "FRONTEND_IMAGE ('$FRONTEND_IMAGE') is not digest-pinned (expected '...@sha256:...'). Never deploy a mutable tag." ;;
esac
log_info "Deploying:"
log_info "  BACKEND_IMAGE=$BACKEND_IMAGE"
log_info "  FRONTEND_IMAGE=$FRONTEND_IMAGE"

# Every docker compose invocation against this file needs --env-file: DOMAIN/BACKEND_IMAGE/
# FRONTEND_IMAGE are resolved by Compose's OWN ${VAR} interpolation of the compose YAML
# itself, which is a different mechanism from the `env_file:` directive INSIDE the compose
# file (that one only affects what env vars land inside the containers, not what Compose
# substitutes while parsing the YAML) — without --env-file here, ${BACKEND_IMAGE:?...} would
# have nothing to resolve against and fail immediately.
compose() {
    if [ -n "$EXTRA_COMPOSE_FILE" ]; then
        docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" -f "$EXTRA_COMPOSE_FILE" -p lifeai "$@"
    else
        docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" -p lifeai "$@"
    fi
}

log_info "== Step 4/11: pulling exact image digests =="
compose pull

log_info "== Step 5/11: verifying pulled image architecture matches this host =="
HOST_ARCH=$(uname -m)
case "$HOST_ARCH" in
    x86_64) EXPECTED_DOCKER_ARCH="amd64" ;;
    aarch64) EXPECTED_DOCKER_ARCH="arm64" ;;
    *) die "Unrecognized host architecture '$HOST_ARCH' — extend this check before deploying." ;;
esac
for image in "$BACKEND_IMAGE" "$FRONTEND_IMAGE"; do
    image_arch=$(docker image inspect "$image" --format '{{.Architecture}}')
    [ "$image_arch" = "$EXPECTED_DOCKER_ARCH" ] || die "$image is built for '$image_arch', this host is '$EXPECTED_DOCKER_ARCH'. Refusing to deploy an incompatible image."
done
log_info "Image architecture matches host ($EXPECTED_DOCKER_ARCH)."

log_info "== Step 6/11: validating Compose config and Caddyfile =="
compose config -q
CADDY_IMAGE=$(compose config | awk '/^  caddy:/{f=1} f && /image:/{print $2; exit}')
docker run --rm -v "$CADDYFILE:/etc/caddy/Caddyfile:ro" "${CADDY_IMAGE:-caddy:2-alpine}" caddy validate --config /etc/caddy/Caddyfile
log_info "Compose config and Caddyfile are valid."

log_info "== Step 7/11: recording deployment metadata =="
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
RECORD_FILE="$DEPLOYMENTS_DIR/$TIMESTAMP.json"
GIT_COMMIT=$(git -C "$COMPOSE_DIR" rev-parse HEAD 2> /dev/null || echo "unknown")
COMPOSE_CHECKSUM=$(sha256sum "$COMPOSE_FILE" | cut -d' ' -f1)
CADDYFILE_CHECKSUM=$(sha256sum "$CADDYFILE" | cut -d' ' -f1)

jq -n \
    --arg timestamp "$TIMESTAMP" \
    --arg git_commit "$GIT_COMMIT" \
    --arg backend_image "$BACKEND_IMAGE" \
    --arg frontend_image "$FRONTEND_IMAGE" \
    --arg compose_checksum "$COMPOSE_CHECKSUM" \
    --arg caddyfile_checksum "$CADDYFILE_CHECKSUM" \
    '{
        timestamp: $timestamp,
        git_commit: $git_commit,
        backend_image: $backend_image,
        frontend_image: $frontend_image,
        compose_checksum: $compose_checksum,
        caddyfile_checksum: $caddyfile_checksum,
        result: "pending",
        rollback_result: null
    }' > "$RECORD_FILE"
log_info "Recorded: $RECORD_FILE"

mark_result() {
    local result="$1"
    jq --arg r "$result" '.result = $r' "$RECORD_FILE" > "$RECORD_FILE.tmp" && mv "$RECORD_FILE.tmp" "$RECORD_FILE"
}

log_info "== Step 8/11: starting services =="
if ! compose up -d; then
    mark_result "failed_to_start"
    die "docker compose up failed. See docker compose logs. Deployment record: $RECORD_FILE"
fi

log_info "== Step 9/11: waiting up to ${HEALTH_TIMEOUT}s for all services to become healthy =="
DEADLINE=$((SECONDS + HEALTH_TIMEOUT))
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

DEPLOY_OK=1
if [ "$ALL_HEALTHY" != "1" ]; then
    log_error "Not all services became healthy within ${HEALTH_TIMEOUT}s."
    DEPLOY_OK=0
else
    log_info "== Step 10/11: verifying a real request through Caddy =="
    DOMAIN_VALUE=$(grep -E "^DOMAIN=" "$ENV_FILE" | head -n1 | cut -d= -f2-)
    if [ "$DOMAIN_VALUE" = ":80" ] || [ -z "$DOMAIN_VALUE" ]; then
        HEALTH_URL="http://localhost/api/health"
    else
        HEALTH_URL="https://$DOMAIN_VALUE/api/health"
    fi
    if ! curl -sSf --max-time 10 "$HEALTH_URL" > /dev/null; then
        log_error "Post-deploy health check against $HEALTH_URL failed."
        DEPLOY_OK=0
    fi
fi

if [ "$DEPLOY_OK" = "1" ]; then
    mark_result "success"
    log_info "== Step 11/11: deployment successful =="
    log_info "Record: $RECORD_FILE"
    compose ps
    exit 0
fi

log_error "== Step 11/11: deployment verification FAILED — automatically rolling back =="
mark_result "failed_verification"
ROLLBACK_ARGS=(--compose-dir "$COMPOSE_DIR" --env-file "$ENV_FILE" --confirm)
if [ -n "$EXTRA_COMPOSE_FILE" ]; then
    ROLLBACK_ARGS+=(--extra-compose-file "$EXTRA_COMPOSE_FILE")
fi
if "$SCRIPT_DIR/rollback.sh" "${ROLLBACK_ARGS[@]}"; then
    log_warn "Rolled back to the previous known-good deployment. The FAILED deployment record is: $RECORD_FILE — investigate before trying again."
    exit 1
else
    log_error "AUTOMATIC ROLLBACK ALSO FAILED. Manual intervention required immediately — see docs/VPS_OPERATIONS_RUNBOOK.md's 'failed rollback' procedure. Failed record: $RECORD_FILE"
    exit 2
fi
