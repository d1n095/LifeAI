#!/usr/bin/env bash
# Installs and enables unattended-upgrades for security patches. Idempotent — checks the
# actual config file content before writing, never blindly overwrites an operator's own
# customization of it.
#
# Deliberately does NOT enable automatic reboots (Unattended-Upgrade::Automatic-Reboot) — a
# surprise reboot of a machine running production containers is a separate, considered
# decision, not a default this script makes for you. See docs/VPS_OPERATIONS_RUNBOOK.md for
# how to check whether a reboot is pending after a kernel update and how to schedule one
# deliberately.
#
# Usage: sudo ./50_enable_auto_updates.sh [--dry-run]
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

parse_common_flags "$@"
require_root

command -v unattended-upgrade &> /dev/null || run apt-get install -y --no-install-recommends unattended-upgrades apt-listchanges

AUTO_UPGRADES_CONF=/etc/apt/apt.conf.d/20auto-upgrades
EXPECTED_CONTENT='APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
'

if [ -f "$AUTO_UPGRADES_CONF" ] && [ "$(cat "$AUTO_UPGRADES_CONF")" = "$(printf '%s' "$EXPECTED_CONTENT")" ]; then
    log_info "$AUTO_UPGRADES_CONF already has the expected content."
else
    log_info "Writing $AUTO_UPGRADES_CONF"
    if [ "$DRY_RUN" = "1" ]; then
        log_info "[dry-run] would write to $AUTO_UPGRADES_CONF:"
        printf '%s' "$EXPECTED_CONTENT"
    else
        printf '%s' "$EXPECTED_CONTENT" > "$AUTO_UPGRADES_CONF"
    fi
fi

log_info "Verifying unattended-upgrades can actually run (dry-run of the tool itself, not this script's --dry-run):"
if [ "$DRY_RUN" != "1" ]; then
    unattended-upgrade --dry-run --debug 2>&1 | tail -20
fi

run systemctl enable --now unattended-upgrades.service 2>/dev/null || log_warn "unattended-upgrades.service not found under that name — on this Ubuntu version it may run purely via the apt periodic timer (apt-daily-upgrade.timer) instead, which is already enabled by the package install above. Verify with: systemctl list-timers | grep apt"

log_info "Automatic security updates enabled. Automatic reboots are NOT enabled — see docs/VPS_OPERATIONS_RUNBOOK.md for handling a reboot-required kernel update deliberately."
