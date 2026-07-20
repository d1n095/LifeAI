#!/usr/bin/env bash
# Installs Docker Engine + the Compose plugin from Docker's own official apt repository
# (not Ubuntu's own, often-older docker.io package). Idempotent — safe to re-run; skips
# steps whose result is already in place instead of redoing them.
#
# Usage: sudo ./10_install_docker.sh [--dry-run]
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

parse_common_flags "$@"
require_root

KEYRING=/etc/apt/keyrings/docker.gpg
SOURCES_LIST=/etc/apt/sources.list.d/docker.list

if command -v docker &> /dev/null && docker --version | grep -qi "docker-ce\|Docker version"; then
    log_info "Docker is already installed: $(docker --version)"
else
    log_info "Installing Docker Engine + Compose plugin from Docker's official apt repository."

    run apt-get update
    run apt-get install -y --no-install-recommends ca-certificates curl gnupg

    if [ ! -f "$KEYRING" ]; then
        run install -m 0755 -d /etc/apt/keyrings
        if [ "$DRY_RUN" = "1" ]; then
            log_info "[dry-run] would download and dearmor Docker's GPG key to $KEYRING"
        else
            curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o "$KEYRING"
            chmod a+r "$KEYRING"
        fi
    else
        log_info "Docker GPG keyring already present at $KEYRING — not re-fetching."
    fi

    if [ ! -f "$SOURCES_LIST" ]; then
        if [ "$DRY_RUN" = "1" ]; then
            log_info "[dry-run] would write $SOURCES_LIST"
        else
            . /etc/os-release
            echo "deb [arch=$(dpkg --print-architecture) signed-by=$KEYRING] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
                > "$SOURCES_LIST"
        fi
    else
        log_info "$SOURCES_LIST already present — not overwriting."
    fi

    run apt-get update
    run apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io docker-compose-plugin

    run systemctl enable --now docker
fi

log_info "Docker daemon status:"
run systemctl is-active docker || die "Docker daemon is not active after installation — check 'systemctl status docker'."

log_info "Verifying docker compose plugin (v2, NOT the standalone docker-compose v1 binary):"
if [ "$DRY_RUN" != "1" ]; then
    docker compose version || die "docker compose plugin did not install correctly."
fi

log_info "Docker installation complete. Next: 20_create_deploy_user.sh."
