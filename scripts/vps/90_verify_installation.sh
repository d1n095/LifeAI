#!/usr/bin/env bash
# Final, comprehensive, read-only verification that everything 00-50 set up is actually in
# place and working — run this after the earlier scripts, before touching
# docs/STRATO_VPS_DEPLOY.md's deployment steps. Never changes anything.
#
# Usage: sudo ./90_verify_installation.sh [--user lifeai] [--ssh-port 22]
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

DEPLOY_USER="lifeai"
SSH_PORT=22
while [ $# -gt 0 ]; do
    case "$1" in
        --user)
            DEPLOY_USER="$2"
            shift 2
            ;;
        --ssh-port)
            SSH_PORT="$2"
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

FAILURES=0
check() {
    local description="$1"
    shift
    if "$@"; then
        log_info "OK: $description"
    else
        log_error "FAILED: $description"
        FAILURES=$((FAILURES + 1))
    fi
}

check "Docker Engine is installed and active" bash -c "command -v docker &>/dev/null && systemctl is-active --quiet docker"
check "Docker Compose plugin (v2) is installed" bash -c "docker compose version &>/dev/null"
check "Deploy user '$DEPLOY_USER' exists" id "$DEPLOY_USER"
check "Deploy user '$DEPLOY_USER' is in the docker group" bash -c "id -nG '$DEPLOY_USER' | grep -qw docker"
check "Deploy user '$DEPLOY_USER' is in the sudo group" bash -c "id -nG '$DEPLOY_USER' | grep -qw sudo"
check "/opt/lifeai exists and is owned by '$DEPLOY_USER'" bash -c "[ -d /opt/lifeai ] && [ \"\$(stat -c %U /opt/lifeai)\" = '$DEPLOY_USER' ]"
check "/etc/lifeai exists, root-owned, mode 700" bash -c "[ -d /etc/lifeai ] && [ \"\$(stat -c %U:%a /etc/lifeai)\" = 'root:700' ]"
check "/opt/lifeai/deployments exists" bash -c "[ -d /opt/lifeai/deployments ]"
check "/var/backups/lifeai exists" bash -c "[ -d /var/backups/lifeai ]"
check "ufw is active" bash -c "ufw status | grep -q 'Status: active'"
check "ufw allows SSH on port $SSH_PORT" bash -c "ufw status | grep -qE \"^${SSH_PORT}/tcp\""
check "ufw allows 80/tcp" bash -c "ufw status | grep -qE '^80/tcp'"
check "ufw allows 443/tcp" bash -c "ufw status | grep -qE '^443/tcp'"
check "unattended-upgrades is installed" bash -c "command -v unattended-upgrade &>/dev/null"
check "20auto-upgrades periodic config is present" bash -c "[ -f /etc/apt/apt.conf.d/20auto-upgrades ] && grep -q 'Unattended-Upgrade \"1\"' /etc/apt/apt.conf.d/20auto-upgrades"
check "Disk space still healthy (>= 10GB free on /)" bash -c "[ \"\$(df --output=avail -k / | tail -n1 | tr -d ' ')\" -ge 10485760 ]"
check "No unexpected listener on port 80 or 443 yet (Caddy not started — that's expected pre-deploy)" bash -c "! ss -Htln 'sport = :80' 2>/dev/null | grep -q . && ! ss -Htln 'sport = :443' 2>/dev/null | grep -q ."

echo
if [ "$FAILURES" -eq 0 ]; then
    log_info "All installation checks passed. This server is ready for the manual steps in docs/STRATO_VPS_DEPLOY.md ('Steg 4' onward: fetch docker-compose.vps.yml/Caddyfile, populate /etc/lifeai/lifeai.env, log in to GHCR)."
    exit 0
else
    die "$FAILURES installation check(s) failed. Fix before proceeding to deployment."
fi
