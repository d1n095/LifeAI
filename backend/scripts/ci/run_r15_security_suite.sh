#!/usr/bin/env bash
# Reproducible R15 security verification against a DISPOSABLE PostgreSQL/pgvector + Redis.
#
# This script never starts, provisions or touches a production service. Point the three
# URLs below at a throwaway environment (the GitHub Actions `r15-security-tests` job uses
# service containers), then run from anywhere:
#
#   backend/scripts/ci/run_r15_security_suite.sh
#
# Without any services, the DB-free policy suites still run:
#
#   backend/scripts/ci/run_r15_security_suite.sh --db-free-only
#
# The DB-backed half loads backend/tests/conftest.py, which CREATES and DROPS its own
# database and creates the restricted `mainai_app` role. The target PostgreSQL role must
# therefore be allowed to CREATE DATABASE / CREATE ROLE, and the database named in
# DATABASE_URL must be disposable.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

db_free_only=false
for arg in "$@"; do
  case "$arg" in
    --db-free-only) db_free_only=true ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

echo "== DB-free R15 policy suites =="
python -m pytest tests/security_r15_successor -q --confcutdir=tests/security_r15_successor
python -m pytest tests/security_r15_egress -q --confcutdir=tests/security_r15_egress

if "$db_free_only"; then
  echo "Skipping DB/Redis-backed suites (--db-free-only)."
  exit 0
fi

: "${DATABASE_URL:?DATABASE_URL must point at a disposable PostgreSQL database}"
: "${APP_DATABASE_URL:?APP_DATABASE_URL must point at the same disposable database}"
: "${REDIS_URL:?REDIS_URL must point at a disposable Redis database}"

echo "== Preflight: PostgreSQL and Redis reachability =="
python - <<'PY'
import os
import sys

import psycopg2
import redis

try:
    connection = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=5)
    connection.close()
except Exception as exc:  # noqa: BLE001 - preflight must report the real driver error
    print(f"PostgreSQL preflight failed: {exc}", file=sys.stderr)
    sys.exit(2)

try:
    client = redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=5)
    client.ping()
    client.close()
except Exception as exc:  # noqa: BLE001
    print(f"Redis preflight failed: {exc}", file=sys.stderr)
    sys.exit(2)
PY

echo "== DB/Redis-backed R15 boundaries =="
python -m pytest \
  tests/backend/providers/test_r15_integration_security.py \
  tests/backend/chat/test_chat_request_idempotency.py \
  tests/backend/test_financial_correctness.py \
  tests/backend/test_billing_engine.py \
  tests/backend/test_agent_orchestration.py \
  tests/backend/test_mainai_execution_lesson_conflicts.py \
  tests/backend/mainai/test_egress_policy.py \
  tests/backend/mainai/test_provider_crash_before_settle.py \
  -q
