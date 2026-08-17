#!/usr/bin/env bash
# Per-boot reconciliation: starts Postgres + Redis and re-applies the idempotent database
# provisioning + migrations so the schema always matches the checked-out code before the
# backend/frontend terminals launch. Dependency installation lives in .cursor/install.sh, not
# here — this must stay fast and safe to run on every start.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bash "$REPO/.cursor/setup-services.sh"
