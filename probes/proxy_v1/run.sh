#!/bin/bash
# Same-origin proxy adversarial probes. Usage: run.sh <built-frontend-dir>  (a copy with node_modules + .next)
# Loopback only: a recording fake backend, `next start`, raw-socket probes. No external network.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; FE="$(cd "${1:?built frontend dir}" && pwd)"
BP=18071; NP=13071; export NEXT_TELEMETRY_DISABLED=1
python3 "$HERE/fake_backend.py" $BP & B=$!
( cd "$FE" && INTERNAL_API_URL="http://127.0.0.1:$BP" PORT=$NP HOSTNAME=127.0.0.1 npx next start -p $NP -H 127.0.0.1 > "$HERE/next.log" 2>&1 ) & N=$!
for i in $(seq 1 60); do curl -s -o /dev/null "http://127.0.0.1:$NP/api/edge-probe?probe_id=x" && break; sleep 0.5; done
python3 "$HERE/probe_proxy.py" $NP; RC=$?
kill $B; sleep 0.5
# PX-6 backend down
OUT=$(curl -s -w ' %{http_code}' "http://127.0.0.1:$NP/api/health"); echo "PX6|$OUT"
if echo "$OUT" | grep -q '502$' && ! echo "$OUT" | grep -qi "127.0.0.1\|$BP\|ECONNREFUSED\|at "; then echo "PASSED proxy::PX-6_backend_down_structured_502"; else echo "FAILED proxy::PX-6_backend_down_structured_502"; RC=1; fi
pkill -f "next start -p $NP" ; kill $N 2>/dev/null; wait 2>/dev/null; exit $RC
