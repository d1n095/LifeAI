# F1 + F7 — envelope-styrda mål kan inte köras av V0.1, och permanenta konfigurationsfel parkeras

**Branch:** `freebuff/governed-execution-park-20261127`
**Bas:** `freebuff/supervisor-failed-tick-lease-release-20261127` (`85a1c10e` + F7-fixen)
**Innehåller F7.** Branchen är den sammanhängande säkerhetskorrigering som F7-posten i
`docs/BRANCH_REGISTRY.md` kräver innan merge.

Ingen merge till default, ingen produktionsdeployment, ingen ändring av `POLICY_LOCK`, ingen
påverkan på befintlig staging, `first_cycle_go=false` oförändrat.

## Problemet (F1)

`run_supervisor()` anropar `dispatch_ready_task()` **innan** planeringen. Den commiten är
durabel. När planeringen därefter vägrar (mätt i den isolerade installationen 2026-11-27:
`CandidateValidationError` eftersom målets `approval_policy` inte var scopad för obevakad
körning) låg `mainai_jobs`-raden kvar som `queued`. `_advance_mainai_execution_tasks`
exkluderar envelope-styrda mål, men **jobbklaimningen gjorde ingen sådan kontroll**:
`claim_next_mainai_job()` → `run_task_execution_job()` → `_propose_repo_edit()` skrev i den
**delade** checkouten utan hänsyn till `allowed_paths`/`allowed_capabilities`/`maximum_risk`.
Med F7:s korrekta lease-release blev dessutom en permanent felkonfigurerad måltick återförsökt
var ~2 s.

## Vad som ändrats

1. **Strukturell ingångsfence i V0.1-executorn**
   (`backend/app/mainai_execution/execution_job.py`, `_fence_governed_task_execution`).
   Ett mål som **någonsin** haft en `ExecutionAuthorizationEnvelope` (vilken status som helst —
   samma durabila EVER_GOVERNED-predikat som `_advance_mainai_execution_tasks` och
   `execute_takeover` redan använder) körs aldrig av V0.1. Jobbet fence-failas med
   `capability_unavailable`; en task som fortfarande är `running` på jobbet finaliseras genom
   den **befintliga** outcome-gaten: `retryable_failed` med ordinarie backoff-klocka, eller
   `failed` när attempts-budgeten är uttömd. En task som redan parkerats/finaliserats av en
   annan gräns lämnas orörd — fencen neutraliserar bara jobbet.
2. **Parkering av permanenta konfigurationsfel**
   (`backend/app/development_supervisor/production_entry.py`,
   `park_tasks_after_supervisor_refusal` + `except CandidateValidationError`). Ticken rollbackar
   sitt ocommittade arbete, parkerar tasken som `blocked` med synlig orsak och **utan**
   `next_retry_at`, fence-failar ett jobb vars lease den fortfarande håller, och släpper
   mål-leasen i en egen committad transaktion. Den returnerar `CONFIG_REFUSED` i stället för att
   kasta — ingen fel-logg varannan sekund.
3. **Kontrollerad återupptagning** (`backend/app/mainai_execution/provider_wait_wake.py`).
   Parken registrerar vilken envelope den vägrades under. Varje tick reconcilerar
   (`wake_tasks_blocked_for_supervisor_refusal`): en task som parkerats under en **annan**
   envelope än målets nu aktiva väcks exakt en gång. Samma aktiva envelope väcker aldrig, så en
   oförändrad konfiguration kan inte bli en wake/park-loop. Reconciliation i ticken (inte som
   bieffekt i `authorize_execution_scope`) undviker dessutom en goal→task-låsordning i en
   founder-anropad väg och gör att ett crash mellan auktorisering och väckning inte tappar
   något — nästa tick tar det.

Checkpoints, append-only-händelser, attempts-/backoff-budget och övriga mål är oförändrade.
Ingen parallell arbetsmotor har byggts.

## Beteendematris

| Situation | Utfall |
|---|---|
| Envelope-styrt mål, kvarlämnat `task_execution`-jobb | Aldrig kört av V0.1; jobbet `failed`/`capability_unavailable`; tasken `retryable_failed`+backoff eller `failed` vid uttömd budget |
| Aldrig envelope-styrt mål | Oförändrat (V0.1 kör som förut) |
| Permanent konfigurationsfel i Supervisor-ticken | Tasken `blocked`, ingen retry-klocka, **ett** jobb, leasen släppt |
| Tillfälligt fel | Befintlig budget/backoff; ingen ny mekanism |
| Founder auktoriserar en ny envelope | Exakt en väckning, exakt ett nytt försök |
| Samma aktiva envelope | Ingen väckning, inget nytt jobb |
| Lease expiry på ett `task_execution`-jobb | Aldrig blind-reclaimad; går via befintlig recovery som vägrar V0.1 för styrda mål |
| Dubbel leverans/omstart av samma jobb | Terminalt jobb → ren no-op, inga dubblerade events |

## Verifiering

### Regressionstester (riktig Postgres, riktig RLS-roll)

* `backend/tests/backend/mainai/test_governed_execution_park.py` — **9 nya prov**: fencen kör
  aldrig V0.1 (inget `work_result`-checkpoint, ingen `verification_started`), supersederad
  envelope fencar fortfarande, aldrig-styrt mål påverkas inte, parkerad task skrivs aldrig över,
  terminalt jobb är en ren no-op, **två riktiga trådar** om samma kvarlämnade jobb ger exakt en
  claim, expired `task_execution`-lease blind-reclaimas aldrig, omauktorisering väcker exakt en
  gång, och park-eventet registrerar orsak/jobb/envelope.
* `backend/tests/backend/mainai/test_supervisor_production_entry.py` — F7-provet är omarbetat
  till parkeringsprovet (park + släppt lease + **inget nytt jobb** på nästa tick) och ett nytt
  prov kör hela väckningskedjan genom den riktiga ticken (en founder-akt → ett nytt jobb).
* `test_supervisor_goal_lease.py`, `test_worker_live_lease_expiry_takeover.py`,
  `test_autonomy_spend_narrow_wire.py`, `test_recovery_takeover_authority_fencing.py`,
  `test_execution_envelopes.py` — **50 passed**.
* Brett svep (`tests/backend/mainai/` + advance-tasks-exkluderingen + executor/resilience/
  replan/auto-recovery) — **923 passed, 1 failed**. Det enda felet är miljömässigt:
  `rg` finns inte installerat (`test_structured_read_search_and_trace_are_provider_independent`),
  och det faller identiskt på en orörd checkout.
* `ruff check` på samtliga berörda filer: rent.

### Verkligt E2E i den isolerade stacken

`e2e-f1f7.py` kördes mot den **körande** backend + worker + Postgres, lokal Ollama och riktiga
per-mål-worktrees. Fullständig logg i `evidence/e2e-f1f7.txt`; resultat:

```
A: mål med standard_repo_work -> task blockerad, blocker_reason satt, next_retry_at=None
   ett jobb, failed/capability_unavailable (V0.1-fencen), ingen work_result-checkpoint
C: 20 s senare fortfarande exakt ett jobb (ingen churn)
D: ett korrekt konfigurerat mål completed medan A låg kvar parkerat
E: konfigurationen rättad + ny envelope -> retry_scheduled (reauth_wake) -> ett nytt jobb
   -> verification_passed -> completed; totalt två jobb
E2E OK
```

Loggen visar också de exakta task-eventen: `supervisor_config_refusal_park` följt av
`supervisor_refusal_envelope_reauth_wake` med `previous_execution_envelope_id` och den nya
envelopen.

## Så reproduceras det

```bash
# 1. Kör stacken från denna branch (isolerad checkout)
git -C ~/lifeai-local-stack/repo checkout --detach <denna branchs SHA>
~/lifeai-local-stack/start-backend.sh &
~/lifeai-local-stack/start-worker.sh &

# 2. Kör E2E (skapar egna mål, rör ingen staging)
FOUNDER_PASSWORD=... python3 delivery/governed-execution-park/e2e-f1f7.py
```

Testkörning (disposable test-DB, kräver Postgres på 55432):

```bash
cd backend
DATABASE_URL=postgresql://postgres@127.0.0.1:55432/lifeos_test_x \
APP_DATABASE_URL=postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_test_x \
REDIS_URL=redis://127.0.0.1:56381/5 \
python -m pytest tests/backend/mainai/test_governed_execution_park.py \
  tests/backend/mainai/test_supervisor_production_entry.py -q
```

## Gränser och kvarstående observationer

* **Docker/sudo saknas** för rollen → containerbaserad verifiering är fortfarande `NOT_RUN`.
  Allt ovan är kört i den isolerade lokala installationen.
* **Ingen webbläsare** i miljön → UI är inte verifierat (ingen UI ändrades).
* **Det finns ingen PATCH-route för `approval_policy`** och ingen unblock-route. E2E:t rättar
  konfigurationen med ett direkt DB-utlåtande, vilket är den enda tillgängliga motsvarigheten
  till en founder-åtgärd i den isolerade miljön. Parken är ändå rätt utfall: fail closed, synlig
  orsak, ingen churn.
* **Omauktorisering kräver ett nytt, explicit scope.** Propose-routen är idempotent på identiskt
  innehåll, så ett byte-identiskt förslag returnerar det redan auktoriserade förslaget och
  authorize vägrar korrekt. E2E:t använder därför ett medvetet nytt scope (samma paths + de
  scopade commit-capabilities som arbetet behöver). Det är dokumenterat i
  `docs/LIFE_SUPERVISOR_PRODUCTION_ENTRY.md`.
* F2 (receptet är målscopat) och F4 (lease-radtillväxt) är oförändrade och hör hemma i egna
  brancher.

## Filer

* `backend/app/mainai_execution/execution_job.py` — V0.1-fencen.
* `backend/app/development_supervisor/production_entry.py` — parken + väckningsreconciliation.
* `backend/app/mainai_execution/provider_wait_wake.py` — parkens orsaker + väckningsfunktion.
* `backend/tests/backend/mainai/test_governed_execution_park.py` — 9 nya prov.
* `backend/tests/backend/mainai/test_supervisor_production_entry.py` — omarbetat F7-prov + nytt
  väckningsprov.
* `docs/LIFE_SUPERVISOR_PRODUCTION_ENTRY.md`, `docs/BRANCH_REGISTRY.md` — arkitektur och register.
* `delivery/governed-execution-park/e2e-f1f7.py`, `evidence/` — reproducerbart E2E + loggar.
