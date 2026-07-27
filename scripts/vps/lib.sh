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

# The full set of secret/config variable NAMES /etc/lifeai/lifeai.env must define — never
# their values. Shared by deploy.sh (which refuses to deploy if any is missing) and
# backup.sh (which writes this list, names only, into its manifest so a from-scratch restore
# has a checklist of what to re-provision from your own secret store — see
# docs/VPS_SECRETS_INVENTORY.md). Defined once here so the two can never drift apart.
#
# REDIS_PASSWORD, not REDIS_URL: Redis now runs as its own private container
# (docker-compose.vps.yml's `redis` service) instead of an external service like Upstash, and
# REDIS_URL is constructed automatically by Compose from this password
# (redis://:<password>@redis:6379/0) — never filled in by hand.
# shellcheck disable=SC2034 # used by every script that sources this file, not by lib.sh itself
LIFEAI_REQUIRED_ENV_VARS="SECRET_KEY MAINAI_APP_PASSWORD FOUNDER_EMAIL FOUNDER_PASSWORD DATABASE_URL REDIS_PASSWORD FRONTEND_ORIGINS PUBLIC_APP_URL SMTP_HOST SMTP_PORT SMTP_USERNAME SMTP_PASSWORD SMTP_FROM_EMAIL DOMAIN BACKEND_IMAGE FRONTEND_IMAGE"

# Validates that $1 is a digest-pinned image reference: a non-empty image name (no
# whitespace, no bare '@') followed by @sha256: and exactly 64 lowercase hexadecimal
# characters, anchored across the whole string. A bare `*@sha256:*` glob (this repo's
# original check) also matches an empty digest — e.g. a template placeholder left as
# `ghcr.io/example/backend@sha256:` after a copy-paste — so deploy.sh must use this instead
# of a case/glob check. $2 is the variable name, used only in the die() message.
validate_digest_pinned_image() {
    local image="$1" label="$2"
    if [[ ! "$image" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]]; then
        die "$label ('$image') is not a valid digest-pinned image reference (expected a non-empty image name followed by '@sha256:' and exactly 64 lowercase hexadecimal characters). Never deploy a mutable tag or an incomplete digest."
    fi
}

# Validates that $1 (REDIS_PASSWORD's value) is non-empty, at least 20 characters, and —
# deliberately — restricted to [A-Za-z0-9]. That charset restriction is this project's
# chosen answer to "URL-encode it or handle it safely": docker-compose.vps.yml builds
# REDIS_URL as redis://:${REDIS_PASSWORD}@redis:6379/0 via plain Compose variable
# interpolation, which does NOT percent-encode anything — a password containing `:`, `@`,
# `/`, `%`, `#`, `?`, whitespace, or any other URL-meaningful/shell-meaningful character
# would silently produce a broken or mis-parsed connection string (redis-py's own
# urlsplit-based parsing in backend/app/config.py would split on the wrong character), and
# redis-server's own --requirepass / redis-cli's -a additionally split on whitespace
# regardless of URL concerns. Refusing every non-alphanumeric character up front is simpler
# and more robust than implementing percent-encoding/decoding on both ends (Compose
# interpolation, redis-server, redis-cli, and redis-py would all need to agree on the same
# scheme) for a value this project always recommends generating with `openssl rand -hex 32`
# anyway (already pure lowercase hex, a subset of this charset). Never logs or echoes the
# value itself, only pass/fail.
validate_redis_password() {
    local password="$1"
    if [ -z "$password" ]; then
        die "REDIS_PASSWORD is empty. Generate one with: openssl rand -hex 32"
    fi
    if [ "${#password}" -lt 20 ]; then
        die "REDIS_PASSWORD is shorter than 20 characters — too weak. Generate one with: openssl rand -hex 32"
    fi
    case "$password" in
        *[![:alnum:]]*)
            die "REDIS_PASSWORD contains a character other than A-Z, a-z, or 0-9 — refusing. REDIS_URL is built by plain, non-encoding string interpolation (redis://:\$REDIS_PASSWORD@redis:6379/0), so punctuation, whitespace, or symbols could silently break or mis-parse the connection string. Generate one with: openssl rand -hex 32 (already alphanumeric)."
            ;;
    esac
}

# A repeated `KEY=` line in an env file is silently resolved by both `docker compose
# --env-file` and shell sourcing as "last line wins" — but this script's own earlier reads of
# BACKEND_IMAGE/FRONTEND_IMAGE/DOMAIN/REDIS_PASSWORD above used `grep | head -n1` (FIRST
# match), meaning a duplicated key could make deploy.sh validate and log a completely
# different value than the one Compose actually starts the containers with. Found via a real
# incident: a duplicated GOOGLE_API_KEY line left an unused placeholder value silently active
# in production because nothing ever surfaced the duplication. Refusing to deploy at all on
# any duplicate is safer than guessing which occurrence is "the real one" — the fix is for a
# human to clean up the file, not for this script to pick a side. Never prints values, only
# the duplicated key NAMES.
check_no_duplicate_env_keys() {
    local env_file="$1"
    local dupes
    dupes=$(grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$env_file" | sed 's/=$//' | sort | uniq -d)
    if [ -n "$dupes" ]; then
        die "Duplicate variable name(s) in $env_file (names only, not values): $(echo "$dupes" | tr '\n' ' ')— each key must appear exactly once. Remove the stale/duplicate line(s) before deploying; 'last line wins' semantics make it easy to silently deploy the wrong value otherwise."
    fi
}
