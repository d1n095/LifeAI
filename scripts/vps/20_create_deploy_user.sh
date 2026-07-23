#!/usr/bin/env bash
# Creates a dedicated, unprivileged deploy user (sudo-capable, in the docker group) — never
# root for day-to-day operation of the VPS. Idempotent: if the user already exists, only
# ensures group membership, never touches its password or SSH configuration.
#
# This script does NOT copy an SSH key for you and does NOT disable password login — that is
# a deliberate, separate, opt-in step (see 41_harden_ssh.sh) that requires you to have
# already run `ssh-copy-id` yourself and confirmed key-based login works, so this bootstrap
# process can never lock you out of your own server.
#
# Usage: sudo ./20_create_deploy_user.sh [--dry-run] [--user lifeai]
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

if id "$DEPLOY_USER" &> /dev/null; then
    log_info "User '$DEPLOY_USER' already exists — not recreating it or touching its password."
else
    log_info "Creating user '$DEPLOY_USER' (sudo-capable, no password set here — see below)."
    run adduser --disabled-password --gecos "" "$DEPLOY_USER"
    log_warn "No password was set for '$DEPLOY_USER'. Set one yourself with 'sudo passwd $DEPLOY_USER' ONLY if you actually intend to allow password login for it — the recommended path is SSH-key-only (see 41_harden_ssh.sh) and no password at all."
fi

run usermod -aG sudo "$DEPLOY_USER"

if command -v docker &> /dev/null; then
    run usermod -aG docker "$DEPLOY_USER"
    log_info "Added '$DEPLOY_USER' to the docker group — log out and back in (or run 'newgrp docker') for this to take effect in an existing shell."
else
    log_warn "Docker is not installed yet — run 10_install_docker.sh first, then re-run this script (idempotent, safe) to pick up the docker group membership."
fi

log_info "Next steps (manual, on YOUR machine, not this script):"
log_info "  1. ssh-copy-id $DEPLOY_USER@<this-server-ip>"
log_info "  2. Verify 'ssh $DEPLOY_USER@<this-server-ip>' logs in WITHOUT a password prompt."
log_info "  3. Only after that succeeds, optionally run 41_harden_ssh.sh to disable password auth."
log_info "User setup complete. Next: 30_setup_directories.sh."
