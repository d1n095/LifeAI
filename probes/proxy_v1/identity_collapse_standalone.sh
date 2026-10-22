#!/bin/bash
# PX-10: does the proxy preserve DISTINCT client identities for per-IP rate limits?
# Real backend with the Dockerfile flags (--proxy-headers --forwarded-allow-ips=*) behind standalone `node .next/standalone/server.js` (dual stack ::).
# Client A = 127.0.0.1 (IPv4), client B = ::1 (IPv6): two genuinely different socket peers on one host.
# A exhausts the per-IP login limit with password spraying (other accounts, so no per-account lockout);
# then B logs in to the Founder account with the synthetic test password. Distinct clients must not share A's limit.
# Usage: identity_collapse.sh <candidate-worktree> <built-frontend-dir>
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; TARGET="$(cd "$1" && pwd)"; FE="$(cd "$2" && pwd)"
VENV="$HERE/../../../venv312/bin"; DB="examiner_idc_$$"; BP=18074; NP=13074
export NEXT_TELEMETRY_DISABLED=1 DATABASE_URL="postgresql://lifeos@localhost:5433/$DB" APP_DATABASE_URL="postgresql://mainai_app:mainai_app_pw@localhost:5433/$DB" \
  REDIS_URL="redis://127.0.0.1:6390/7" SECRET_KEY=examiner-idc FOUNDER_EMAIL=founder@lifeos.local FOUNDER_PASSWORD='TestFounderPassword123!' \
  FRONTEND_ORIGINS="http://127.0.0.1:$NP" PUBLIC_APP_URL="http://127.0.0.1:$NP" ENABLE_SCHEDULED_CLEANUP=false OPENAI_API_KEY=fake-not-used \
  STORAGE_ROOT="$(mktemp -d)" HOME="$(mktemp -d)"
unset GH_TOKEN GITHUB_TOKEN ANTHROPIC_API_KEY
lsof -nP -iTCP:$BP -iTCP:$NP -sTCP:LISTEN >/dev/null && { echo "ports busy"; exit 3; }
redis-cli -p 6390 -n 7 flushdb >/dev/null
psql -h localhost -p 5433 -U lifeos -d postgres -qc "CREATE DATABASE $DB" || exit 2
psql -h localhost -p 5433 -U lifeos -d $DB -qc "GRANT USAGE ON SCHEMA public TO mainai_app; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mainai_app; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON SEQUENCES TO mainai_app;"
( cd "$TARGET/backend" && "$VENV/alembic" upgrade head > /dev/null 2>&1 )
( cd "$TARGET/backend" && exec "$VENV/uvicorn" app.main:app --host 127.0.0.1 --port $BP --proxy-headers --forwarded-allow-ips='*' --log-level warning > "$HERE/.idc_backend.log" 2>&1 ) &
( cd "$FE" && cp -r .next/static .next/standalone/.next/static 2>/dev/null; cp -r public .next/standalone/public 2>/dev/null; cd .next/standalone && INTERNAL_API_URL="http://127.0.0.1:$BP" PORT=$NP HOSTNAME=:: exec node server.js > "$HERE/.idc_next.log" 2>&1 ) &
UP=0; for i in $(seq 1 180); do curl -sf -o /dev/null "http://127.0.0.1:$NP/api/health" && { UP=1; break; }; sleep 0.5; done
if [ $UP = 0 ]; then echo "FAILED proxy::PX-10_SETUP_STACK_DID_NOT_START (not evidence)"; tail -5 "$HERE/.idc_backend.log" "$HERE/.idc_next.log"
  pkill -f "standalone/server.js"; pkill -f "uvicorn app.main:app --host 127.0.0.1 --port $BP"; exit 1; fi
login() { curl -s -o /dev/null -w '%{http_code}' -X POST "$1/api/auth/login" -H 'content-type: application/json' --data "$2"; }
A=""; for i in $(seq 1 12); do A="$A $(login "http://127.0.0.1:$NP" "{\"email\":\"spray$i@example.com\",\"password\":\"Spring2026!\"}")"; done
B=$(login "http://[::1]:$NP" '{"email":"founder@lifeos.local","password":"TestFounderPassword123!"}')
echo "PX10|clientA(127.0.0.1)_spraying:$A"; echo "PX10|clientB(::1)_founder_login:$B"
# PX-11: A forges B's address in every forwarding header while spraying; B must still get its own, untouched limit.
redis-cli -p 6390 -n 7 flushdb >/dev/null
C=""; for i in $(seq 1 12); do C="$C $(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$NP/api/auth/login" -H 'content-type: application/json' \
  -H 'X-Forwarded-For: ::1' -H 'X-Real-IP: ::1' -H 'Forwarded: for="[::1]"' --data "{\"email\":\"forge$i@example.com\",\"password\":\"Spring2026!\"}")"; done
E=$(login "http://[::1]:$NP" '{"email":"founder@lifeos.local","password":"TestFounderPassword123!"}')
echo "PX11|clientA_forging_B:$C"; echo "PX11|clientB_founder_login:$E"
pkill -f "standalone/server.js"; pkill -f "uvicorn app.main:app --host 127.0.0.1 --port $BP"; sleep 1
psql -h localhost -p 5433 -U lifeos -d postgres -qc "DROP DATABASE IF EXISTS $DB WITH (FORCE)" < /dev/null &
echo "$A" | grep -q 429 || { echo "FAILED proxy::PX-10a_control_client_A_limited"; exit 1; }
echo "PASSED proxy::PX-10a_control_client_A_limited"
RC=0
if [ "$B" = "200" ]; then echo "PASSED proxy::PX-10_distinct_clients_keep_distinct_limits"; else echo "FAILED proxy::PX-10_distinct_clients_keep_distinct_limits (client B got $B after client A exhausted the limit: identities collapsed)"; RC=1; fi
if [ "$E" = "200" ]; then echo "PASSED proxy::PX-11_forged_forwarding_headers_cannot_exhaust_another_clients_limit"; else echo "FAILED proxy::PX-11_forged_forwarding_headers_cannot_exhaust_another_clients_limit (client B got $E after A forged B's address)"; RC=1; fi
exit $RC
