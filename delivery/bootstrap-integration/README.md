# Bootstrap-integrationskandidat — fem verifierade leveranser i en kedja (2026-11-27)

**Branch:** `freebuff/bootstrap-integration-20261127`
**Bas:** `freebuff/governed-execution-park-20261127` (`09ed1bf8`, F1 + F7)
**Strategisk referens:** PR #513 `docs/MAINAI_BOOTSTRAP_STRATEGY.md` (grundarbeslut om
självständig utveckling). Dokumentet är en referens för prioritering, inte en ny modul eller ett
nytt exekveringsmandat.

Ingen merge till default, ingen produktionsdeployment, ingen ändring av `POLICY_LOCK`,
`first_cycle_go=false` oförändrat, ingen påverkan på staging.

## Varför en integrationsgren

De fem leveranserna från 2026-11-27 rör samma subsystem (Supervisor → envelope → Operator →
durable jobb) och bygger alla på F1/F7. De var verifierade **var för sig**, men aldrig tillsammans.
Den här grenen är den sammansatta kandidaten: samma kod, korrekt beroendeordning, en enda
migrationskedja och en gemensam verifiering.

## Integrationsordning (faktisk, inte påstådd)

| # | Leverans | Källa | Innehåll |
|---|---|---|---|
| 1 | F1 + F7 | `09ed1bf8` (bas) | Envelope-styrda mål körs aldrig av V0.1; permanenta konfigurationsfel parkeras; misslyckad tick släpper mål-leasen |
| 2 | Founder-återhämtning | `b1925137` | `POST /goals/{id}/reauthorize`, `PATCH approval-policy`, `POST resume-parked-tasks`; omauktorisering kan inte utöka scope |
| 3 | F2 task-scopat recept | `91230051` | Olika `repo_edit`-uppgifter i samma mål gör olika ändringar; kumulativt; stage+commit endast om envelopen auktoriserar det |
| 4 | F4 lease-livscykel | `3e959fae` | En live-rad per mål, append-only historik i `supervisor_goal_lease_events` (migration **0090**), backfill före städning |
| 5 | Transient-E2E | `592cb73a` | Verkligt E2E för tillfälliga fel (SIGKILL mitt i körande jobb, backoff, attempts-budget) + fynd T1/T2 |

Merge skedde med `--no-ff` i den ordningen; enda konflikten var `docs/BRANCH_REGISTRY.md`, löst
som en union av båda sidornas poster (ingen post tappad). `alembic heads` visar exakt **0090**.
Ingen blind merge: varje steg verifierades innan nästa lades på.

## T1 — åtgärdad (blockerande för obevakad återhämtning)

`kill -9` mitt i en Supervisor-tick lämnar målets lease `active` i upp till TTL:ns 3 600 s.
F7 släpper bara när ticken **kastar**; en dödad process kastar ingenting, så målet kunde inte
plockas upp igen förrän en timme senare — exakt den återhämtning nattarbetet är beroende av.

**Fix:** `release_supervisor_goal_leases_from_previous_process()` (`development_supervisor/lease.py`)
körs en gång, på worker-processens första poll-cykel, **innan** den kan ha claimat något. Den
släpper aktiva leases som innehas av **samma worker-identitet** — de måste tillhöra en tidigare
process — och skriver en `released`-post med orsak `worker_process_restarted`. Andra identiteters
leases rörs aldrig (deras TTL är fortfarande gränsen). Fenced writes gör en gammal process som
mot förmodan lever fail-closed i stället för att skriva under förlorad auktoritet.

**Bevis:** `evidence/e2e-t1.txt` — riktig worker SIGKILL:as och startas om; en aktiv lease med en
timme kvar släpps inom ~1 s, historiken visar orsaken, och workern lever. Ingen klockmanipulation.
Enhetstest: `test_a_restarted_process_releases_only_its_own_previous_leases`.

## T2 — bedömd, **inte** blockerande (och därför inte "fixad" med en ny väg)

Fyndet beskrev ett kvarlämnat `queued`-jobb + `running` task som ingen väg drev vidare. Vi
återupprepade tillståndet mot den riktiga executorsökvägen innan något ändrades, och fyndet håller
inte som permanent stillestånd:

* det kvarlämnade jobbet **claimas av den ordinarie pollningen** (`claim_next_mainai_job()`);
* för ett **envelope-styrt** mål fencar F1 V0.1 och finaliserar tasken genom den befintliga
  outcome-gaten: `retryable_failed` med riktig `next_retry_at`, jobbet `failed`/
  `capability_unavailable`, ingen `work_result`-checkpoint;
* för ett **aldrig styrt** mål kör V0.1 uppgiften normalt;
* det gäller även det smalare kraschfönstret där `mainai_job_id` aldrig skrevs (kolumnen är en FK
  och kan bara sättas efter `create_job()`s commit): F1-fencen matchar `NULL` och
  retry-ticken återställer tasken till `ready`.

Det som **saknades** var en aktiv väg medan workern låg nere — det är korrekt väntan, inte en
förlorad uppgift. Därför ingen ny återhämtningsväg, ingen ändrad terminal-status för `queued`-jobb
(det skulle kräva en osann `started_at`). Beteendet låses i stället med riktiga regressionstester i
`tests/backend/mainai/test_orphaned_dispatch_recovery.py` (båda länkvarianterna + aldrig-styrt mål).

**Live-bevis 2026-11-27:** transient-E2E:t på kandidaten avbröt workern exakt i det här fönstret
(`task=running, job=queued`) och uppgiften återhämtades till ett riktigt andra försök med backoff
och ett nytt jobb — se `evidence/e2e-transient-integration.txt`.

## F2 — verkligt E2E, och ett riktigt fel som bara E2E:t hittade

`evidence/e2e-f2.txt`: ett mål med **två olika** `repo_edit`-uppgifter (implementation, sedan
fokustest) körs av den riktiga workern genom Supervisor → Operator → ruff/pytest → git.

```
task 1 completed, verification_passed, attempts 1
task 2 completed, verification_passed, attempts 1
implementation commit files: ['calculator.py']
focused-test commit files:   ['test_calculator.py']
calculator.py: def subtract count=1
test_calculator.py: def test_subtract count=1
1 passed in 0.01s   (testet kört i mål-worktreen)
F2 E2E OK
```

Första körningen **misslyckades** och avslöjade en riktig defekt i F2:s heuristik:
delsträngsmatchning gjorde `test_calculator.py` till en träff på `calculator.py` och
`subtract-funktionen` till en träff på `funktion`, så fokustest-uppgiften klassades som `both`.
Den kunde då återskapa källfilen (dubbelarbete med implementationsuppgiften) och märkte sitt commit
fel. Enhetstestet såg grönt ut eftersom source-upserten råkade vara en no-op i dess fixture.

**Fix:** en explicit filväg i uppgiftens beskrivning avgör omfånget själv; hyphensplittrade
sammansättningar kan inte längre ge falska träffar. Regressionstesterna är skärpta så att
test-uppgiften inte längre får skapa källfilen, plus ett prov där båda filerna nämns och hela
ändringen därför är korrekt.

## Transient-E2E på kandidaten — återhämtning, beständigt arbete och kontrollerade återförsök

`evidence/e2e-transient-integration.txt`: samma drivrutin som transient-leveransen, körd mot
**kandidaten** med riktig backend, worker, Postgres, Redis, Ollama och mål-worktree. Två mål, två
faser:

**Fas 1 — återhämtning.** Workern SIGKILL:as när uppgiften är `running` och jobbet ännu `queued`
(precis kraschfönstret mellan dispatch-commit och jobbclaim). Efter omstart:

```
interrupting: task=running job=queued worker=1828690
PASS the worker process is genuinely gone
PASS goal lease not blocking (released by the restarted worker -- no clock change)
phase 1 task: completed attempts 2/3
PASS it took a real second attempt -- 2
PASS a durable retry/backoff event was recorded -- 1
PASS exactly one completed event (no duplicate execution) -- 1
PASS recovery dispatched a new job -- 2
```

Mål-worktreen visar båda uppgifterna som separata, kumulativa commits (`Add subtract helper`,
`Add focused subtract test`), rätt innehåll och en ren arbetsyta — arbetet från det avbrutna
försöket bevarades och den beroende uppgiften byggde vidare på det.

**Fas 2 — försöksbudget.** Workern dödas varje gång uppgiften faktiskt kör:

```
PASS attempt 1 interrupted while the worker had it (1/3 used)
PASS attempt 2 interrupted while the worker had it (2/3 used)
PASS attempt 3 interrupted while the worker had it (3/3 used)
phase 2 task: failed attempts 3/3 next_retry_at None
PASS the attempts budget is never exceeded -- 3/3
PASS no retry clock is left behind after the budget is exhausted
PASS no new jobs are generated after the budget is exhausted -- 3 -> 3
PASS nothing is reported completed for the exhausted task
PASS the completed task stayed completed (no collateral damage)
```

Efteråt omplanerade workern målet till plan v2 (begränsat av `MAX_AUTO_REPLANS=3`), de nya
uppgifterna krävde **nytt** grundargodkännande (inget approval-arv), och efter godkännande
slutfördes de med två nya commits — men målet stängdes som `failed` eftersom originaluppgiften
uttömde sin budget. Ingen tyst framgång, ingen oändlig replan-loop.

**T2 i verkligheten:** avbrottet i fas 1 skedde exakt i det fönster där T2 beskrev ett kvarlämnat
`queued`-jobb. Det återhämtades av den ordinarie vägen till ett riktigt andra försök — samma
slutsats som T2-bedömningen, nu även live.

**T1 i verkligheten:** leasen släpptes av den omstartade workern utan någon klockmanipulation
(`no clock change`) i varje avbrottscykel.

## Säkerhetsgränser (oförändrade)

* Ingen självtilldelning av behörighet: F2:s beskrivning väljer bara vilka av receptets redan
  bundna steg som körs, varje väg valideras fortfarande mot envelopen; stage/commit kräver
  `stage_scoped_changes` + `commit_scoped_changes`.
* Envelope-styrda mål körs aldrig av V0.1 (F1), även när ett kvarlämnat jobb claimas.
* F4:s historik är append-only på databasnivå; runtime-rollen har bara `SELECT, INSERT`.
* `POLICY_LOCK` och `first_cycle_go=false` är orörda; ingen merge/deploy.

## Så verifieras kandidaten

```bash
cd backend
DATABASE_URL=postgresql://postgres@127.0.0.1:55432/lifeos_test \
APP_DATABASE_URL=postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_test \
REDIS_URL=redis://127.0.0.1:56381/15 SECRET_KEY=dev-only-local-secret ENVIRONMENT=development \
python3 -m pytest tests/backend/mainai tests/backend/test_mainai_execution_auto_recovery.py \
  tests/backend/test_mainai_execution_retry_tick.py \
  tests/backend/test_advance_tasks_excludes_envelope_governed_goals.py \
  tests/backend/test_lease_clock_domain.py \
  tests/backend/test_recovery_takeover_authority_fencing.py -q
```

Verkligt E2E (isolerad stack, kandidaten utcheckad, **exakt en** worker — en kvarlämnad worker från
en tidigare körning kan annars träffas av testets SIGKILL i stället för den som kör jobbet):

```bash
python3 delivery/bootstrap-integration/e2e-t1.py
python3 delivery/bootstrap-integration/e2e-f2.py
FOUNDER_PASSWORD=... python3 delivery/transient-failure-recovery/e2e-transient.py
```

## Överlämning: ta emot andra agenters verifierade leveranser utan dubbelarbete

Kedjan är avsiktligt byggd för att ta emot Mini och övriga agenters arbete:

1. **En leverans = en gren/commit med ett tydligt syfte**, baserad på
   `freebuff/bootstrap-integration-20261127` (eller dess efterföljare), aldrig på en gammal bas.
2. **Kontrollera `docs/BRANCH_REGISTRY.md` först** — om samma sak redan pågår byggs det vidare
   där i stället för att dupliceras.
3. **Verifiera leveransen själv** (minst dess egna tester + den breda sviten ovan) innan den
   läggs på kedjan; rapportera PASS/FAIL/NOT_RUN/BLOCKED, aldrig "ser rimligt ut".
4. **Lägg på i beroendeordning** med `--no-ff`; en konflikt i registret löses som union, aldrig
   genom att tappa en post. Migrationer måste sluta i en enda head.
5. **Kör om den sammansatta verifieringen** efter varje pålägg; en tidigare grön körning gäller
   inte för nya filer.
6. **Uppdatera registret** med gren, syfte, beroende, merge-ordning och status.

Dubbelarbete undviks genom att F1/F7, founder-vägen, F2, F4 och transient-E2E:t nu är gemensam
grund: nya leveranser ska återanvända dem (t.ex. `release_supervisor_goal_leases_from_previous_process`,
`_recipe_scope`, `supervisor_goal_lease_events`) i stället för att bygga parallella mekanismer.
