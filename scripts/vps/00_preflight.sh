#!/usr/bin/env bash
# Preflight checks — run this FIRST, before any other script in this directory. Read-only:
# never changes anything on the machine, safe to run as many times as you want, including
# just to double-check the server before you commit to anything. See docs/VPS_BOOTSTRAP.md.
#
# Usage: sudo ./00_preflight.sh [--min-disk-gb N] [--min-mem-mb N] [--domain example.com]
#
# shellcheck disable=SC2317
# ^ every check_*() function here is invoked indirectly, by name, via `check "..." check_x`
# below — shellcheck's data-flow analysis can't trace that and flags the function bodies as
# unreachable. They aren't; see https://www.shellcheck.net/wiki/SC2317's own "or ignore if
# invoked indirectly" note, which is exactly this pattern.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

MIN_DISK_GB=20
MIN_MEM_MB=1800
DOMAIN=""

while [ $# -gt 0 ]; do
    case "$1" in
        --min-disk-gb)
            MIN_DISK_GB="$2"
            shift 2
            ;;
        --min-mem-mb)
            MIN_MEM_MB="$2"
            shift 2
            ;;
        --domain)
            DOMAIN="$2"
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

# --- Supported OS ---
check_ubuntu_version() {
    [ -r /etc/os-release ] || return 1
    # shellcheck source=/dev/null
    . /etc/os-release
    [ "${ID:-}" = "ubuntu" ] || return 1
    case "${VERSION_ID:-}" in
        22.04 | 24.04) return 0 ;;
        *)
            log_warn "Ubuntu ${VERSION_ID:-unknown} is not one of the versions docs/STRATO_VPS_DEPLOY.md was verified against (22.04, 24.04)."
            return 1
            ;;
    esac
}
check "Supported Ubuntu version (22.04 or 24.04 LTS)" check_ubuntu_version

# --- Not already bootstrapped in a way that would conflict ---
check_no_conflicting_docker() {
    ! command -v docker &> /dev/null || docker --version | grep -qi "docker-ce\|Docker version"
}
check "No conflicting Docker installation blocking a clean install" check_no_conflicting_docker

# --- Disk space ---
check_disk_space() {
    local avail_kb
    avail_kb=$(df --output=avail -k / | tail -n1 | tr -d ' ')
    local avail_gb=$((avail_kb / 1024 / 1024))
    log_info "Available disk space on /: ${avail_gb}GB (minimum required: ${MIN_DISK_GB}GB)"
    [ "$avail_gb" -ge "$MIN_DISK_GB" ]
}
check "At least ${MIN_DISK_GB}GB free disk space on /" check_disk_space

# --- Memory ---
check_memory() {
    local total_mb
    total_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
    log_info "Total memory: ${total_mb}MB (minimum required: ${MIN_MEM_MB}MB)"
    [ "$total_mb" -ge "$MIN_MEM_MB" ]
}
check "At least ${MIN_MEM_MB}MB total memory" check_memory

# --- Ports 80/443 available (not already bound by something else) ---
check_port_free() {
    local port="$1"
    ! ss -Htln "sport = :$port" 2>/dev/null | grep -q .
}
check "Port 80 is not already in use" check_port_free 80
check "Port 443 is not already in use" check_port_free 443

# --- Required tools present (or installable in the next script) ---
check_curl_present() {
    command -v curl &> /dev/null
}
check "curl is available" check_curl_present

# --- DNS preflight (optional — only if --domain was given) ---
if [ -n "$DOMAIN" ]; then
    check_dns_resolves() {
        command -v dig &> /dev/null || command -v host &> /dev/null || command -v getent &> /dev/null || return 1
        getent hosts "$DOMAIN" &> /dev/null
    }
    check "DNS for $DOMAIN resolves from this machine" check_dns_resolves
    log_warn "DNS resolving is necessary but not sufficient — verify the A record actually points at THIS server's public IP before running Caddy (docs/STRATO_VPS_DEPLOY.md), a mismatched record will make Let's Encrypt issuance fail."
else
    log_warn "No --domain given — skipping the DNS preflight check. Run again with --domain <your-domain> once DNS is set up, before the first deploy."
fi

echo
if [ "$FAILURES" -eq 0 ]; then
    log_info "All preflight checks passed. Safe to continue to 10_install_docker.sh."
    exit 0
else
    die "$FAILURES preflight check(s) failed. Fix the issues above before continuing — do not proceed with a failing preflight."
fi
