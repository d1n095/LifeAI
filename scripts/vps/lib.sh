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

# Founder-requested hotfix (VPS deploy of PR #36's merged base): a rollback must never start an
# OLDER backend image whose own bundled Alembic migration history doesn't know about the
# database's CURRENT revision. Concretely: deploy N ships a new migration and succeeds; deploy
# N+1 fails for an unrelated reason (e.g. a network blip on the health check) and
# deploy.sh auto-rolls-back to deploy N (fine — same schema). But an operator manually rolling
# back FURTHER, past deploy N, to an image built BEFORE that migration existed, would start
# application code that doesn't understand the now-forward-migrated schema — new columns/
# tables/triggers the old ORM models and business logic were never written against. Refusing
# this is the safe default; resolving it (forward-migrate to a newer image, or a deliberate,
# DBA-reviewed `alembic downgrade`) is a manual decision, not something this script should ever
# guess at.
#
# Reads the database's current `alembic_version` directly (via a throwaway container run from
# the TARGET image — any correctly-built backend image has the psycopg2 this needs, and
# reading `alembic_version` doesn't depend on which image's migration history is asked), then
# asks the TARGET image's own `alembic show <revision>` whether it recognizes that revision at
# all (a purely local check against that image's bundled `alembic/versions/` — no DB write, no
# assumption that the revision is that image's own `head`, just that it's SOMEWHERE in its
# known history). `alembic show` exits non-zero with `ResolutionError` if the revision is
# unknown to that image's migration scripts.
#
# --add-host=host.docker.internal:host-gateway on both `docker run` calls below: a bare
# `docker run` (unlike the backend/worker services in docker-compose.vps.yml /
# docker-compose.vps.ci.yml) gets none of Compose's own `extra_hosts:` configuration, so on
# the CI test topology — where DATABASE_URL points at `host.docker.internal` (the runner's own
# host-level Postgres, see docker-compose.vps.ci.yml's comment) — this container would
# otherwise fail to resolve that hostname at all. Harmless on the real VPS, where DATABASE_URL
# points at Supabase over the public internet and this mapping is simply never referenced.
verify_rollback_target_knows_current_revision() {
    local target_image="$1" env_file="$2"
    local current_rev

    current_rev=$(docker run --rm --add-host=host.docker.internal:host-gateway --env-file "$env_file" --entrypoint python "$target_image" -c '
import os, sys
import psycopg2
try:
    conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=10)
    cur = conn.cursor()
    cur.execute("SELECT version_num FROM alembic_version")
    row = cur.fetchone()
    if row is None:
        print("ERROR: alembic_version table is empty", file=sys.stderr)
        sys.exit(1)
    print(row[0])
except Exception as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(1)
') || die "Could not read the current Alembic revision from the database (via $target_image) — refusing to roll back without knowing it. See docs/VPS_OPERATIONS_RUNBOOK.md."

    [ -n "$current_rev" ] || die "Current Alembic revision came back empty — refusing to roll back."

    log_info "Current database Alembic revision: $current_rev"
    log_info "Verifying rollback target $target_image's own migration history includes $current_rev..."

    if ! docker run --rm --add-host=host.docker.internal:host-gateway --env-file "$env_file" --entrypoint alembic "$target_image" show "$current_rev" &> /dev/null; then
        die "Refusing to roll back: rollback target image ($target_image) does not know about the database's current Alembic revision ($current_rev) in its own migration history. Starting it would run OLDER application code against a NEWER database schema. This must be resolved manually — see docs/VPS_OPERATIONS_RUNBOOK.md's rollback-after-schema-upgrade procedure (forward-migrate to a newer image, or a deliberate, reviewed 'alembic downgrade')."
    fi

    log_info "Rollback target's migration history includes the current revision ($current_rev) — safe to proceed."
}
