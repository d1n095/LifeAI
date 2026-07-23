#!/usr/bin/env bash
# OPT-IN SSH hardening: disables password authentication and root login over SSH.
#
# Deliberately NOT run automatically by any other script in this directory, and deliberately
# refuses to run at all unless it can verify a working public key is already installed for
# the target admin user — this is the one step in the whole bootstrap sequence with real
# potential to lock you out of your own server, so it earns extra caution:
#   - Never touches the SSH port.
#   - Never reboots or force-restarts sshd mid-session; only reloads after `sshd -t` passes.
#   - Backs up sshd_config before editing it, with a timestamped filename, never overwriting
#     a previous backup.
#   - Refuses outright if it can't find a plausible public key for the target user's
#     authorized_keys.
#
# You MUST have already run `ssh-copy-id <user>@<server>` and verified you can log in without
# a password prompt BEFORE running this script. This script cannot verify that from inside
# the same SSH session it might be about to break — it can only verify the key FILE is
# present, not that the corresponding private key is actually usable from wherever you
# connect from. If in doubt, open a SECOND terminal and test a fresh login before proceeding.
#
# Usage: sudo ./41_harden_ssh.sh --user lifeai [--dry-run]
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

parse_common_flags "$@"

TARGET_USER=""
while [ $# -gt 0 ]; do
    case "$1" in
        --user)
            TARGET_USER="$2"
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
[ -n "$TARGET_USER" ] || die "Usage: sudo ./41_harden_ssh.sh --user <admin-username> [--dry-run]. Refusing to guess which user's key to check."
id "$TARGET_USER" &> /dev/null || die "User '$TARGET_USER' does not exist."

USER_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
AUTH_KEYS="$USER_HOME/.ssh/authorized_keys"

if [ ! -s "$AUTH_KEYS" ]; then
    die "No authorized_keys file (or it's empty) at $AUTH_KEYS for user '$TARGET_USER'. Run 'ssh-copy-id $TARGET_USER@<this-server>' from your own machine FIRST, verify you can log in without a password, and only then re-run this script. Refusing to disable password authentication without a verified key in place."
fi

KEY_LINE_COUNT=$(grep -cE '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-|sk-ssh-ed25519|sk-ecdsa-sha2-)' "$AUTH_KEYS" || true)
if [ "$KEY_LINE_COUNT" -lt 1 ]; then
    die "$AUTH_KEYS exists but doesn't contain anything that looks like a real public key. Refusing to proceed."
fi
log_info "Found $KEY_LINE_COUNT public key(s) for '$TARGET_USER' in $AUTH_KEYS."

if ! confirm "Have you ALREADY confirmed — in a separate, still-open session — that 'ssh $TARGET_USER@<this-server>' logs in without a password prompt?"; then
    die "Confirm that first. Nothing was changed."
fi

SSHD_CONFIG=/etc/ssh/sshd_config
BACKUP="/etc/ssh/sshd_config.bak.$(date +%Y%m%dT%H%M%S)"
log_info "Backing up $SSHD_CONFIG to $BACKUP"
run cp -p "$SSHD_CONFIG" "$BACKUP"

set_sshd_option() {
    local key="$1" value="$2"
    if grep -qE "^[[:space:]]*${key}[[:space:]]" "$SSHD_CONFIG" 2>/dev/null || grep -qE "^[[:space:]]*#[[:space:]]*${key}[[:space:]]" "$SSHD_CONFIG" 2>/dev/null; then
        if [ "$DRY_RUN" = "1" ]; then
            log_info "[dry-run] would set '$key $value' in $SSHD_CONFIG (existing directive)"
        else
            sed -i -E "s|^[[:space:]]*#?[[:space:]]*${key}[[:space:]].*|${key} ${value}|" "$SSHD_CONFIG"
        fi
    else
        ensure_line_in_file "$key $value" "$SSHD_CONFIG"
    fi
}

set_sshd_option "PasswordAuthentication" "no"
set_sshd_option "PermitRootLogin" "no"
set_sshd_option "ChallengeResponseAuthentication" "no"
set_sshd_option "KbdInteractiveAuthentication" "no"

if [ "$DRY_RUN" = "1" ]; then
    log_info "[dry-run] would validate config with 'sshd -t' and reload sshd"
else
    if ! sshd -t; then
        log_error "sshd -t reported a syntax error in $SSHD_CONFIG — restoring the backup and NOT reloading."
        cp -p "$BACKUP" "$SSHD_CONFIG"
        die "Restored $SSHD_CONFIG from backup. No changes are active. Investigate before retrying."
    fi
    log_info "sshd config is syntactically valid. Reloading (not restarting) sshd — existing connections are not dropped."
    systemctl reload ssh 2>/dev/null || systemctl reload sshd
fi

log_warn "Before closing this session: open a NEW terminal and confirm 'ssh $TARGET_USER@<this-server>' still works. If it doesn't, restore the backup immediately: sudo cp $BACKUP $SSHD_CONFIG && sudo systemctl reload ssh"
log_info "SSH hardening applied: password authentication and root login are now disabled. This machine was not rebooted."
