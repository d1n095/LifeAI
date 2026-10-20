#!/bin/bash
# PX-9: does a client-chosen X-Forwarded-For defeat the login rate limit end to end?
# Real backend exactly as backend/Dockerfile runs it (uvicorn --proxy-headers --forwarded-allow-ips=*),
# behind the real Next.js proxy (`next start`). Loopback only, disposable DB, synthetic founder password.
# Usage: xff_ratelimit.sh <candidate-worktree> <built-frontend-dir>
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; TARGET="$(cd "$1" && pwd)"; FE="$(cd "$2" && pwd)"
VENV="$HERE/../../../venv312/bin"; DB="examiner_xff_$$"; BP=18072; NP=13072
export NEXT_TELEMETRY_DISABLED=1 DATABASE_URL="postgresql://lifeos@localhost:5433/$DB" APP_DATABASE_URL="postgresql://mainai_app:mainai_app_pw@localhost:5433/$DB" \
  REDIS_URL="redis://127.0.0.1:6390/5" SECRET_KEY=examiner-xff FOUNDER_EMAIL=founder@lifeos.local FOUNDER_PASSWORD='TestFounderPassword123!' \
  FRONTEND_ORIGINS="http://127.0.0.1:$NP" PUBLIC_APP_URL="http://127.0.0.1:$NP" ENABLE_SCHEDULED_CLEANUP=false OPENAI_API_KEY=fake-not-used \
  STORAGE_ROOT="$(mktemp -d)" HOME="$(mktemp -d)"
unset GH_TOKEN GITHUB_TOKEN ANTHROPIC_API_KEY
lsof -nP -iTCP:$BP -iTCP:$NP -sTCP:LISTEN >/dev/null && { echo "ports busy"; exit 3; }; redis-cli -p 6390 -n 5 flushdb >/dev/null
psql -h localhost -p 5433 -U lifeos -d postgres -qc "CREATE DATABASE $DB" || exit 2
psql -h localhost -p 5433 -U lifeos -d $DB -qc "GRANT USAGE ON SCHEMA public TO mainai_app; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mainai_app; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON SEQUENCES TO mainai_app;"
( cd "$TARGET/backend" && "$VENV/alembic" upgrade head > "$HERE/xff_alembic.log" 2>&1 ) || { echo "alembic failed"; tail -5 "$HERE/xff_alembic.log"; }
( cd "$TARGET/backend" && "$VENV/uvicorn" app.main:app --host 127.0.0.1 --port $BP --proxy-headers --forwarded-allow-ips='*' --log-level warning > "$HERE/xff_backend.log" 2>&1 ) & U=$!
( cd "$FE" && INTERNAL_API_URL="http://127.0.0.1:$BP" npx next start -p $NP -H 127.0.0.1 > "$HERE/xff_next.log" 2>&1 ) & N=$!
for i in $(seq 1 80); do curl -sf -o /dev/null "http://127.0.0.1:$NP/api/health" && break; sleep 0.5; done
# Password spraying: a DIFFERENT account each attempt, so the per-account lockout never engages and only the
# per-IP limiter (rate_limit_login_per_minute) can stop it.
attempt() { local n=$1; shift; curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$NP/api/auth/login" -H 'content-type: application/json' "$@" \
  --data "{\"email\":\"spray$n@example.com\",\"password\":\"Spring2026!\"}"; }
A=""; for i in $(seq 1 14); do A="$A $(attempt a$i)"; done
redis-cli -p 6390 -n 5 flushdb >/dev/null
B=""; for i in $(seq 1 30); do B="$B $(attempt b$i -H "X-Forwarded-For: 198.51.100.$i")"; done
redis-cli -p 6390 -n 5 flushdb >/dev/null
D=""; for i in $(seq 1 14); do D="$D $(attempt d$i -H "X-Forwarded-For: 198.51.100.7")"; done
C=n/a
echo "PX9|same_ip:$A"; echo "PX9|rotating_xff:$B"; echo "PX9|fixed_spoofed_xff:$D"; grep -c "198.51.100" "$HERE/xff_backend.log" >/dev/null 2>&1; 
pkill -f "next start -p $NP"; pkill -f "uvicorn app.main:app --host 127.0.0.1 --port $BP"; kill $U $N 2>/dev/null; wait 2>/dev/null
psql -h localhost -p 5433 -U lifeos -d postgres -qc "DROP DATABASE IF EXISTS $DB WITH (FORCE)"
echo "$A" | grep -q 429 || echo "PX9 NOTE: control never limited"
echo "$A" | grep -q 429 && echo "PASSED proxy::PX-9a_login_ip_limit_control" || echo "FAILED proxy::PX-9a_login_ip_limit_control"
echo "$B" | grep -q 429 && { echo "PASSED proxy::PX-9_rotating_xff_cannot_bypass_login_limit"; exit 0; } || { echo "FAILED proxy::PX-9_rotating_xff_cannot_bypass_login_limit (login rate limit bypassed by rotating X-Forwarded-For)"; exit 1; }
