#!/usr/bin/env bash
# Configures ufw: default deny incoming, allow SSH (port 22 ONLY — see the note below) plus
# 80/443 for Caddy. Idempotent — ufw's own `allow`/`default` commands are already no-ops if
# the rule already exists, but this script checks first anyway so its output is honest about
# what it actually changed versus what was already true.
#
# Deliberately does NOT touch the SSH port. If you run SSH on a non-default port, edit
# SSH_PORT below yourself before running this script — this script will not guess, and will
# not silently allow only port 22 while your sshd actually listens elsewhere (which would
# lock you out).
#
# Usage: sudo ./40_configure_firewall.sh [--dry-run] [--ssh-port 22]
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

parse_common_flags "$@"

SSH_PORT=22
while [ $# -gt 0 ]; do
    case "$1" in
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

require_root

command -v ufw &> /dev/null || run apt-get install -y --no-install-recommends ufw

CURRENT_SSH_LISTEN_PORT=$(ss -Htlnp 2>/dev/null | awk '/sshd/ {split($4,a,":"); print a[length(a)]}' | sort -u | head -n1 || true)
if [ -n "$CURRENT_SSH_LISTEN_PORT" ] && [ "$CURRENT_SSH_LISTEN_PORT" != "$SSH_PORT" ]; then
    die "sshd appears to actually be listening on port $CURRENT_SSH_LISTEN_PORT, not $SSH_PORT. Re-run with --ssh-port $CURRENT_SSH_LISTEN_PORT, or you WILL lock yourself out. Refusing to proceed."
fi

log_info "Setting default policies: deny incoming, allow outgoing."
run ufw default deny incoming
run ufw default allow outgoing

log_info "Allowing SSH on port $SSH_PORT (verified against sshd's actual listening port above)."
run ufw allow "$SSH_PORT"/tcp comment "SSH"

log_info "Allowing HTTP/HTTPS for Caddy (80/443 — the only ports docker-compose.vps.yml publishes)."
run ufw allow 80/tcp comment "HTTP (Caddy, ACME challenge + redirect to HTTPS)"
run ufw allow 443/tcp comment "HTTPS (Caddy)"

if [ "$DRY_RUN" = "1" ]; then
    log_info "[dry-run] would run: ufw --force enable"
else
    if ufw status | grep -q "Status: active"; then
        log_info "ufw is already active."
    else
        log_warn "About to enable ufw. Your current SSH session should stay connected (the SSH rule was just added above), but if this is your first time doing this on this server, keep a second, independent connection method (e.g. your hosting provider's web console) open until you've confirmed you can still SSH in."
        if confirm "Enable ufw now?"; then
            ufw --force enable
        else
            log_warn "Skipped enabling ufw — rules are staged but not active. Run 'sudo ufw enable' yourself when ready, or re-run this script with --yes."
        fi
    fi
fi

log_info "Firewall rules:"
run ufw status verbose

log_info "Firewall configuration complete. Next: 50_enable_auto_updates.sh (and optionally 41_harden_ssh.sh once you've verified key-based login)."
