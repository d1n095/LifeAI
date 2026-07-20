#!/usr/bin/env bash
# Creates /opt/lifeai (the deploy checkout — owned by the deploy user) and /etc/lifeai (the
# secrets directory — root-owned, mode 700, NEVER readable by the deploy user) plus log and
# backup directories. Idempotent: never changes ownership/permissions of a directory that
# already exists with the expected owner, so re-running this never clobbers a directory an
# operator may have already populated by hand.
#
# Usage: sudo ./30_setup_directories.sh [--dry-run] [--user lifeai]
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

parse_common_flags "$@"

DEPLOY_USER="lifeai"
while [ $# -gt 0 ]; do
    case "$1" in
        --user)
            DEPLOY_USER="$2"
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

require_root

id "$DEPLOY_USER" &> /dev/null || die "User '$DEPLOY_USER' does not exist — run 20_create_deploy_user.sh first."

ensure_dir() {
    local path="$1" owner="$2" mode="$3"
    if [ -d "$path" ]; then
        log_info "Directory already exists: $path (leaving ownership/permissions as-is — inspect manually if unsure)"
        return 0
    fi
    log_info "Creating $path (owner=$owner mode=$mode)"
    run mkdir -p "$path"
    run chown "$owner" "$path"
    run chmod "$mode" "$path"
}

# The deploy checkout — docker-compose.vps.yml, Caddyfile — owned by the deploy user, NOT
# root, since day-to-day operation (docker compose pull/up) happens as that user via its
# docker group membership, never as root.
ensure_dir /opt/lifeai "$DEPLOY_USER:$DEPLOY_USER" 0755

# The secrets directory. root:root, 0700 — deliberately NOT readable by the deploy user, even
# though that user runs `docker compose`, because Docker itself (running as root) reads
# env_file paths, not the invoking user's own read permission. See docs/VPS_SECRETS_INVENTORY.md.
ensure_dir /etc/lifeai root:root 0700

# Deployment records (digests, timestamps, rollback history — see scripts/vps/deploy.sh and
# scripts/vps/rollback.sh) and backups (scripts/vps/backup.sh). Owned by the deploy user —
# these are operational records, not secrets, and the deploy user needs to write to them.
ensure_dir /opt/lifeai/deployments "$DEPLOY_USER:$DEPLOY_USER" 0755
ensure_dir /var/backups/lifeai "$DEPLOY_USER:$DEPLOY_USER" 0750

log_info "Directory setup complete. Verify with: ls -la /opt/lifeai /etc/lifeai"
log_info "Next: 40_configure_firewall.sh."
