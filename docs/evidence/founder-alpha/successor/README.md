# Founder Alpha successor — QA-evidens (2026-11-21)

**Kodkandidat:** `4814a1d5e10974b490af0fb41f3fc2c08ca43e8b`
**Branch:** `claude/founder-alpha-fullstack-qa` (bas `6ececab`)
**QA-grind:** **BLOCKED** — 21 PASS, 0 FAIL, 3 BLOCKED, 0 SKIP
**Ingen push, PR, merge eller deployment har gjorts.** `first_cycle_go=false`.

## Vad som verifierades och ändrades

- **Crash-säker dokumentversion:** resume-vägen återskapar nu en saknad
  `KnowledgeVersion` och `storage_key` om processen dödats mellan
  `Document`-committen och versions-/lagringscommitten. Den verifierar
  `content_checksum` mot `Document.checksum`, behåller exakt en version och
  använder den riktiga uppladdningsgränsen. Regressionstest reproducerar det
  exakta kraschens fönster.
- **Explicit dokumentersättning (F11/F22):** en ny fil skapar nästa
  `Document`-version, ett nytt durabelt `ImportJob` och en `supersedes`-relation.
  Den gamla källan, dess `KnowledgeVersion`-historik, claims och original finns
  kvar, men markeras `superseded` och utesluts från både semantisk och textuell
  sökning. Identiskt innehåll, innehåll som redan finns som annan källa och en
  redan ersatt källa avvisas explicit. Knowledge Studio-detaljsidan har ett
  bekräftat ersättningsflöde.
- **RAG-kvalitet (F09):** 6 riktiga PostgreSQL/RLS-tester täcker relevant
  träff med källfält, irrelevant utebliven träff, två motstridiga källor med
  `contradicts`-flagga, historisk källa med kapad confidence och explicit
  varning, explicit `supersedes`-uteslutning samt tomt resultat med
  osäkerhetsinstruktion. Ingen retrieval-, RLS- eller trust-kod är mockad.
- **Full founder-E2E (F12/F19/F21):** 12/12 Playwright-tester mot riktig
  Next.js-proxy, backend och durable worker: login/session, chatt, import,
  indexering, sökning, källciterat svar, öppna källa, fortsätta samtal,
  radering, projekt/uppgift, reload, nätverksfel, mobil layout och
  felmeddelanden. Körningen gjordes per spec med rensade testtabeller för att
  undvika ordningsberoenden; JSON:en är sammanslagen i
  `playwright-founder.json`.
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
| Samlad backend-JUnit | **527 tester, 0 failures, 0 errors, 2 skips (525 passed)** |
| Health/startup/OpenAPI/config | **48 passed** |
| Auth/session | **11 passed** |
| Providers | **228 passed** |
| Chat-historik | **18 passed** |
| Bibliotek/RAG/uppladdning | **81 passed, 1 skipped** (`zip` saknas) |
| F09 RAG-kvalitet | **6 passed** |
| Beteendekrav (beslut/agent/korrigering) | **3 passed** |
| PR #464 oberoende fokustester | **66 passed** |
| Playwright founder/E2E | **12 passed, 0 failed, 0 flaky** |
| Explicit ersättning/version | **5 passed** |
| Crash-säker version-resume | **2 passed** |
| Worker/recovery/minne/lease/Redis | **88 passed** |
| OpenAI consult | **35 passed, 1 skipped** (live-test kräver nyckel/nätverk) |
| F13 PostgreSQL-faults | **6/6 PASS** |
| F18 recovery-matris | **PASS** |
| F01–F24 | **21 PASS, 0 FAIL, 3 BLOCKED** |
| QA-grind | **BLOCKED** (exit 3) |

## PR #464 — oberoende granskning

PR #464 (`8667146`, bas `16a6bd3`) granskades som en riktig diff och dess egna
R15-/chat-retrytester kördes i en separat worktree: **66 passed, 0 failed, 0
skipped**. Inga kodändringar gjordes i PR:en. Granskningen omfattar retry och
dubbelräkning, provider-timeout/503, osäkra externa effekter, eval-isolering och
race conditions. Fullständig analys och kvarstående risker finns i
[`../pr464-review.md`](../pr464-review.md).

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
export E2E_FOUNDER_PASSWORD="TestFounderPassword123!"

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
pytest tests/backend/rag/test_library_replace.py --junitxml=/tmp/replace-tests.xml -q
pytest tests/backend/rag/test_library_import.py -k "resume" --junitxml=/tmp/version-resume.xml -q
pytest tests/backend/rag/test_rag_quality_matrix.py --junitxml=/tmp/rag-quality.xml -q
pytest tests/backend/mainai/test_founder_behavior_requirements.py \
  --junitxml=/tmp/behavior.xml -q

python scripts/acceptance/merge_junit.py --out /tmp/version-replace.xml \
  /tmp/version-resume.xml /tmp/replace-tests.xml

python scripts/acceptance/merge_junit.py --out /tmp/program-tests.xml \
  /tmp/sub-a.xml /tmp/sub-b.xml /tmp/sub-c.xml /tmp/sub-d.xml \
  /tmp/sub-e.xml /tmp/sub-f.xml /tmp/sub-g.xml /tmp/replace-tests.xml \
  /tmp/rag-quality.xml /tmp/behavior.xml

# Playwright: kör per spec med rensade testtabeller mellan varje, mot riktig
# backend/worker. Upprepa för auth, shell-pages, founder-knowledge-studio,
# library-workbench-mobile och founder-alpha-resilience; slå ihop JSON-filerna.
(
  cd ../frontend
  CI=1 E2E_FOUNDER_PASSWORD="$E2E_FOUNDER_PASSWORD" \
    E2E_INTERNAL_API_URL=http://127.0.0.1:8010 \
    npx playwright test e2e/founder-knowledge-studio.spec.ts \
    --project=chromium --workers=1 --reporter=json \
    > /tmp/playwright-founder.json
)
# Den committade evidensen ligger i ../docs/evidence/founder-alpha/successor/;
# använd playwright-founder.json och rag-quality.json för att verifiera leveransen
# utan att köra om hela sviten.

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
  --version-junit /tmp/version-replace.xml \
  --playwright-json /tmp/playwright-founder.json \
  --rag-quality-evidence /tmp/rag-quality.json \
  --postgres-fault-evidence /tmp/postgres-faults.json \
  --out /tmp/program-matrix.json

python scripts/acceptance/qa_gate.py \
  --expected-sha "0db06d528942dec26d3217128ba1443c09fbf1f3" \
  --database-url "postgresql://postgres@127.0.0.1:55432/lifeos_e2e_r16" \
  --redis-url "redis://127.0.0.1:56379/15" \
  --install-evidence ../docs/evidence/founder-alpha/install-matrix.json \
  --performance-evidence ../docs/evidence/founder-alpha/resource-probe.json \
  --recovery-evidence ../docs/evidence/founder-alpha/recovery.json \
  --pytest-junit /tmp/program-tests.xml \
  --playwright-json /tmp/playwright-founder.json \
  --program-evidence /tmp/program-matrix.json \
  --out-dir /tmp/qa-gate
```

## Kvarstående blockerare

- **F04:** Ollama är inte installerad/port `11434` svarar inte; providern använder
  `stream=false`. Ingen verklig lokal konversation kunde köras. Detta får inte
  lösas genom att installera en tung modell på 1,8 GiB-VPS:en.
- **F23:** Fyra timmars aktiv soak är inte körd; en kort smoke är inte
  ekvivalent. Kräver en separat resursbegränsad testmiljö. Förberedd runbook:
  [`../soak-runbook.md`](../soak-runbook.md).
- **F24:** Grinden är BLOCKED eftersom F04 och F23 är BLOCKED; alla övriga
  arbetsområden är PASS.

## Kända begränsningar

`pg_stat_statements` saknas (långsamma queries är BLOCKED, inte gissade);
`zip`-binären saknas för ett test; Z.ai-CLI-beroende tester körs inte; exakt-head
GitHub Actions kräver godkänd push; resursutmattning (CPU/minne/disk) är inte
mätt. E2E kräver en skrivbar `STORAGE_ROOT`; standardvägen `/var/lib/lifeai`
är inte användbar i den isolerade testmiljön. `delivery/founder-alpha-reliability/`
är orörd och dess checksummor är inte ändrade.
