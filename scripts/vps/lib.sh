#!/usr/bin/env bash
# Shared helpers for scripts/vps/*.sh. Sourced, never executed directly.
#
# Every script in this directory is meant to be run manually, once, in order, by Dennis
# himself on the real Strato VPS — see docs/STRATO_VPS_DEPLOY.md for the exact sequence and
# docs/VPS_BOOTSTRAP.md for what each script does and why. Nothing here is wired into any CI
# workflow to run automatically against a real server; --check/--dry-run modes are validated
# in CI (shellcheck, and where practical, a dry-run execution — see
# .github/workflows/ci.yml's vps-scripts-check job), the real (non-dry-run) paths are not,
# because there is no real server to run them against yet.

set -euo pipefail

VPS_LIB_COLOR_RED=$'\033[31m'
VPS_LIB_COLOR_YELLOW=$'\033[33m'
VPS_LIB_COLOR_GREEN=$'\033[32m'
VPS_LIB_COLOR_RESET=$'\033[0m'

log_info() {
    printf '%s[info]%s %s\n' "$VPS_LIB_COLOR_GREEN" "$VPS_LIB_COLOR_RESET" "$*"
}

log_warn() {
    printf '%s[warn]%s %s\n' "$VPS_LIB_COLOR_YELLOW" "$VPS_LIB_COLOR_RESET" "$*" >&2
}

log_error() {
    printf '%s[error]%s %s\n' "$VPS_LIB_COLOR_RED" "$VPS_LIB_COLOR_RESET" "$*" >&2
}

die() {
    log_error "$*"
    exit 1
}

# DRY_RUN=1 (set by --dry-run on the calling script) makes run() print the command instead of
# executing it. Every mutating action in every script in this directory MUST go through
# run(), not be invoked directly — that is what makes --dry-run actually trustworthy instead
# of just documentation.
DRY_RUN="${DRY_RUN:-0}"

run() {
    if [ "$DRY_RUN" = "1" ]; then
        printf '%s[dry-run]%s would run: %s\n' "$VPS_LIB_COLOR_YELLOW" "$VPS_LIB_COLOR_RESET" "$*"
        return 0
    fi
    "$@"
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        die "$(basename "$0") must be run as root (e.g. with sudo). Refusing to guess at partial privilege."
    fi
}

require_not_root() {
    if [ "$(id -u)" -eq 0 ]; then
        die "$(basename "$0") must NOT be run as root — it performs actions as an unprivileged user on purpose. Re-run as the intended deploy user."
    fi
}

# Idempotency helper: only appends a line to a file if it isn't already present, and never
# truncates or overwrites the file's existing content. Used instead of blind `>>` so re-running
# a script never duplicates configuration.
ensure_line_in_file() {
    local line="$1" file="$2"
    if [ -f "$file" ] && grep -qxF "$line" "$file"; then
        log_info "Already present in $file: $line"
        return 0
    fi
    log_info "Adding to $file: $line"
    run bash -c "printf '%s\n' \"\$1\" >> \"\$2\"" _ "$line" "$file"
}

confirm() {
    local prompt="$1"
    if [ "${VPS_ASSUME_YES:-0}" = "1" ]; then
        return 0
    fi
    read -r -p "$prompt [y/N] " reply
    case "$reply" in
        [yY][eE][sS] | [yY]) return 0 ;;
        *) return 1 ;;
    esac
}

parse_common_flags() {
    for arg in "$@"; do
        case "$arg" in
            --dry-run)
                DRY_RUN=1
                ;;
            --yes | -y)
                VPS_ASSUME_YES=1
                ;;
        esac
    done
}
