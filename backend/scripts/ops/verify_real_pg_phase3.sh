#!/usr/bin/env bash
# P0.5 final: clean single-pass REAL-PG battery (conftest enabled, DB reset first).
set -uo pipefail
cd /home/z/my-project/review-target/LifeAI/backend
source /tmp/pg-env.sh
export APP_DATABASE_URL="postgresql://mainai_app:mainai_app@localhost:5433/lifeai_verify"
export MAINAI_APP_PASSWORD="mainai_app"
export REDIS_URL="redis://localhost:6379/0"
export ENVIRONMENT=production
PSQL="$(python3 -c 'import pgserver,os;print(os.path.join(pgserver.__path__[0],"pginstall/bin/psql"))')"
OUT=/home/z/my-project/download/ALPHA_REALPG_RESULTS.txt

echo "=== CLEAN SINGLE-PASS REAL-PG BATTERY ($(git rev-parse --short HEAD)) ===" >> "$OUT"
echo "--- DB reset ---" >> "$OUT"
"$PSQL" -h localhost -p 5433 -U lifeai -d postgres -c "DROP DATABASE IF EXISTS lifeai_verify;" >> "$OUT" 2>&1
"$PSQL" -h localhost -p 5433 -U lifeai -d postgres -c "CREATE DATABASE lifeai_verify;" >> "$OUT" 2>&1
python3 -m alembic upgrade head 2>&1 | tail -1 >> "$OUT"

echo "--- ensure app role ---" >> "$OUT"
python3 scripts/security/ensure_app_role.py 2>&1 | tail -1 >> "$OUT"

echo "--- suites (single pass, conftest enabled) ---" >> "$OUT"
for s in \
  tests/backend/test_chat_source_capture.py \
  tests/backend/test_blueprint_readiness.py \
  tests/backend/test_billing_engine.py \
  tests/backend/test_financial_correctness.py \
  tests/backend/test_ads_isolation.py \
  tests/backend/test_ads_adversarial.py \
  tests/backend/test_founder_recovery.py \
  tests/backend/test_founder_recovery_edge.py \
  tests/backend/rag/test_message_source_units.py \
  tests/backend/rag/test_memory_source_units.py \
  tests/backend/chat/test_message_sequence.py \
  tests/backend/test_ensure_app_role.py \
  tests/security/test_rls_isolation.py \
  tests/backend/test_startup_checks.py \
  tests/backend/test_openai_consult.py ; do
  r=$(python3 -m pytest "$s" -q --tb=line 2>&1 | tail -1)
  echo "$(basename $s): $r" >> "$OUT"
done
echo "PHASE3 DONE" >> "$OUT"
sed -n '/CLEAN SINGLE-PASS/,$p' "$OUT"
