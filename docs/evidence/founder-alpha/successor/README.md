# Founder Alpha successor — QA-evidens (2026-11-21)

**Kandidat:** `c527543cc33722dcc6bea7b7c04606723dff803b`
**Branch:** `claude/founder-alpha-fullstack-qa` (bas `6ececab`)
**QA-grind:** **BLOCKED** — 16 PASS, 0 FAIL, 8 BLOCKED, 0 SKIP
**Ingen push, PR, merge eller deployment har gjorts.** `first_cycle_go=false`.

## Vad som verifierades och ändrades

- **Crash-säker dokumentversion:** resume-vägen återskapar nu en saknad
  `KnowledgeVersion` och `storage_key` om processen dödats mellan
  `Document`-committen och versions-/lagringscommitten. Den verifierar
  `content_checksum` mot `Document.checksum`, behåller exakt en version och
  använder den riktiga uppladdningsgränsen. Regressionstest reproducerar det
  exakta kraschens fönster.
- **Recovery-matris (F18):** giltig restore gav identiska radantal; korrupt
  dump avvisades av checksumman och `pg_restore`; partiell dump avvisades;
  källan förblev oförändrad och nåbar.
- **PostgreSQL-felinjektion (F13):** riktig PostgreSQL, inga mocks:
  transaktionsrollback, exakt en deadlock (`40P01`), låstimeout (`55P03`),
  statement/commit-timeout (`57014`), dödad backend med återanslutning och
  två samtidiga commits. Alla sex PASS.
- **F01–F24-matris och QA-grind:** grinden propagerar BLOCKED från matrisen
  och kan inte göra den till PASS. JUnit från avgränsade grupper slås ihop
  med `merge_junit.py`.
- **Testkontrakt:** worker-/planner-faken tar nu emot
  `owner_id`/`task_id`/`goal_id`/`job_id`; provider-testerna använder en
  riktig verifierad ägare och RLS-kontext i stället för ett påhittat UUID.

## Faktiska resultat

| Svit | Resultat |
| --- | --- |
| Samlad backend-JUnit | **511 tester, 0 failures, 0 errors, 2 skips** |
| Health/startup/OpenAPI/config | **48 passed** |
| Auth/session | **11 passed** |
| Providers | **228 passed** |
| Chat-historik | **18 passed** |
| Bibliotek/RAG/uppladdning | **81 passed, 1 skipped** (`zip` saknas) |
| Worker/recovery/minne/lease/Redis | **88 passed** |
| OpenAI consult | **35 passed, 1 skipped** (live-test kräver nyckel/nätverk) |
| F13 PostgreSQL-faults | **6/6 PASS** |
| F18 recovery-matris | **PASS** |
| F01–F24 | **16 PASS, 0 FAIL, 8 BLOCKED** |
| QA-grind | **BLOCKED** (exit 3) |

## Reproducerbara kommandon

Kör från `backend/` med projektets testmiljö. `COOKIE_SECURE=true` krävs för
auth-sviten; `DATABASE_URL`/`REDIS_URL` pekar på den isolerade testmiljön.

```bash
export PYTHONPATH=.
export DATABASE_URL="postgresql://postgres@127.0.0.1:55432/lifeos_test_program"
export APP_DATABASE_URL="postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_test_program"
export LIFEAI_TEST_DATABASE_NAME="lifeos_test_program"
export LIFEAI_TEST_REDIS_DB=14
export REDIS_URL="redis://127.0.0.1:56379/14"
export SECRET_KEY="local-test-secret"
export COOKIE_SECURE=true

# Grupperna som ligger bakom den samlade JUnit-filen.
pytest tests/backend/test_health.py tests/backend/test_startup_checks.py \
  tests/backend/test_openapi_schema.py tests/backend/test_config_contract.py \
  --junitxml=/tmp/sub-a.xml -q
pytest tests/security/test_session_auth.py --junitxml=/tmp/sub-b.xml -q
pytest tests/backend/providers --junitxml=/tmp/sub-c.xml -q
pytest tests/backend/chat/test_chat_message_persistence.py --junitxml=/tmp/sub-d.xml -q
pytest tests/backend/rag/test_library_import.py tests/backend/rag/test_library_routes.py \
  tests/backend/test_documents_upload_durable_delivery.py --junitxml=/tmp/sub-e.xml -q
pytest tests/backend/jobs/test_worker.py \
  tests/backend/test_mainai_execution_recovery_takeover.py \
  tests/backend/test_project_memory.py \
  tests/backend/mainai/test_memory_work_linkage.py \
  tests/backend/mainai/test_worker_live_lease_expiry_takeover.py \
  tests/security_r15_successor/test_r15_11_redis_store.py \
  --junitxml=/tmp/sub-f.xml -q
pytest tests/backend/test_openai_consult.py --junitxml=/tmp/sub-g.xml -q

python scripts/acceptance/merge_junit.py --out /tmp/program-tests.xml \
  /tmp/sub-a.xml /tmp/sub-b.xml /tmp/sub-c.xml /tmp/sub-d.xml \
  /tmp/sub-e.xml /tmp/sub-f.xml /tmp/sub-g.xml

python scripts/acceptance/recovery_matrix.py \
  --source-url "postgresql://postgres@127.0.0.1:55432/lifeos_e2e" \
  --pg-bin /tmp/lifeai-testenv/root/usr/lib/postgresql/16/bin \
  --out /tmp/recovery-matrix.json

python scripts/acceptance/postgres_faults.py \
  --database-url "postgresql://postgres@127.0.0.1:55432/lifeos_gate" \
  --out /tmp/postgres-faults.json

python scripts/acceptance/program_matrix.py \
  --install-evidence ../docs/evidence/founder-alpha/install-matrix.json \
  --performance-evidence ../docs/evidence/founder-alpha/resource-probe.json \
  --recovery-evidence ../docs/evidence/founder-alpha/recovery.json \
  --recovery-matrix /tmp/recovery-matrix.json \
  --pytest-junit /tmp/program-tests.xml \
  --version-junit /tmp/version-resume.xml \
  --playwright-json ../docs/evidence/founder-alpha/playwright-resilience.json \
  --postgres-fault-evidence /tmp/postgres-faults.json \
  --out /tmp/program-matrix.json

python scripts/acceptance/qa_gate.py \
  --expected-sha "$(git rev-parse HEAD)" \
  --database-url "postgresql://postgres@127.0.0.1:55432/lifeos_gate" \
  --redis-url "redis://127.0.0.1:56379/15" \
  --install-evidence ../docs/evidence/founder-alpha/install-matrix.json \
  --performance-evidence ../docs/evidence/founder-alpha/resource-probe.json \
  --recovery-evidence ../docs/evidence/founder-alpha/recovery.json \
  --pytest-junit /tmp/program-tests.xml \
  --playwright-json ../docs/evidence/founder-alpha/playwright-resilience.json \
  --program-evidence /tmp/program-matrix.json \
  --out-dir /tmp/qa-gate
```

## Kvarstående blockerare

- **F04:** Ollama är inte installerad/port `11434` svarar inte; providern använder
  `stream=false`. Ingen verklig lokal konversation kunde köras.
- **F09:** Dedikerat RAG-kvalitetsdataset med relevanta, irrelevanta,
  motstridiga och inaktuella dokument saknas. Mekaniktesterna är gröna, men
  de bevisar inte den fyra klasserna.
- **F11:** Crash-säker version recreation är fixad och testad, men API för
  ersättning/versionsbyte med indexinvalidering är inte implementerat.
- **F12:** Knowledge Studio har inget fullständigt Playwright-JSON mot riktig
  backend i denna miljö.
- **F19:** Full Playwright-resa för grundaren (login, chat, dokument, minne,
  omstart, recovery) saknas som JSON.
- **F21:** Mobil-/tillgänglighets-JSON saknas.
- **F23:** Fyra timmars aktiv soak är inte körd; en kort smoke är inte
  ekvivalent.
- **F24:** Grinden är BLOCKED eftersom ovanstående arbetsområden är BLOCKED.

## Kända begränsningar

`pg_stat_statements` saknas (långsamma queries är BLOCKED, inte gissade);
`zip`-binären saknas för ett test; Z.ai-CLI-beroende tester körs inte; exakt-head
GitHub Actions kräver godkänd push; resursutmattning (CPU/minne/disk) är inte
mätt. `delivery/founder-alpha-reliability/` är orörd och dess checksummor är
inte ändrade.
