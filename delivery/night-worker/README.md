# Nattarbete i LifeAI — verifierat 2026-11-27 (isolerad stack, 31.70.202.6)

Allt nedan är kört mot den isolerade installationen i `/home/lifeai-security/lifeai-local-stack`
(Postgres 55432, Redis 56381, Ollama 11434, backend 8000, frontend 3000 — samtliga bundna till
`127.0.0.1`). Ingen staging-container, port, databas eller volym har rörts. Ingen merge, ingen
deployment, ingen ändring av `POLICY_LOCK`, `first_cycle_go=false` oförändrat.

## Kort svar

LifeAI **kan** redan arbeta obevakat: den befintliga arbetsmotorn (Postgres-kö med leases,
`mainai_execution`, `development_supervisor`, `development_operator`) kör en godkänd
utvecklingsuppgift utan chattsession, i en isolerad worktree, med tester, checkpoints och
återupptagning efter krasch. Det som saknades var **inte kod** utan två konfigurationsfel i den
lokala installationen, plus ett antal dokumenterade gränser.

Två kodändringar gjordes **inte** — se "Fynd" nedan, särskilt F1 som kräver ett eget beslut.

## Resultat

| # | Krav | Status | Bevis |
|---|------|--------|-------|
| 1 | Verkligt samtal (inte bara HTTP 200) | **PASS** | `POST /api/chat` → `BIRCH-7731` ur `note.md`, `provider=ollama`, `confidence`-satt källa score 0.74, 84 s |
| 2 | Minne över omstart | **PASS** | 7 konversationer / 16 meddelanden kvar efter backend- och Postgres-omstart; assistentsvaret persisterat |
| 3 | Uppgifter (mål/plan/task) körs obevakat | **PASS** | 4 mål `completed`, 9 tasks, 62 task-events, 94 checkpoints |
| 4 | Ett godkänt mål körs utan chattsession | **PASS** | `2de94169…` → plan → godkännande → envelope → tick → worktree → `completed` på 29 s |
| 5 | Ändringar sker i isolerad arbetskopia | **PASS** | `/tmp/mainai-supervisor-goal-worktrees/<goal_id>/calculator.py`; delade checkouten `git status` tom |
| 6 | Relevanta tester körs | **PASS** | receptets `focused_pytest` → `verification_passed`; manuell omkörning `1 passed` |
| 7 | Resultat dokumenteras + checkpoint | **PASS** | `mainai_task_events` (append-only) + `mainai_checkpoints` + `final_outcome`-rapport |
| 8 | Fortsätter med nästa godkända steg | **PASS** | `run_tests`-task blev `ready` och kördes i nästa tick efter `repo_edit` |
| 9 | Återupptar efter processkrasch | **PASS** | `SIGKILL` mitt i `read_only_audit`; nytt jobb (attempt 2) efter lease-utgång, **exakt en** `completed` |
| 10 | Återupptar efter server-/DB-omstart | **PASS** | `pg_ctl restart` + worker-omstart: 4 mål/7 tasks/50 events/79 checkpoints identiska, inget kördes om |
| 11 | Avbrott leder inte till dubbelkörning | **PASS** | Inga dubblerade `completed`-events; `verification_passed` en gång per task |
| 12 | Parkerar vid säkerhetsgräns | **PASS** | `WAITING_APPROVAL` varje tick (ej självgodkännande), `PROVIDER_SPEND_NOT_AUTHORIZED`, policy-vägran |
| 13 | Reproducerbart skript | **PASS** | `nightly-e2e.sh` → `GOAL COMPLETED`, exit 0 |
| 14 | GitHub-anslutning återanvändbar | **PASS** | deploy-nyckeln `~/.ssh/lifeai_github` mot `git@github.com:d1n095/LifeAI.git`; `git push --dry-run` accepterad, därefter publicerade båda grenarna (se "Publicering") |
| 15 | V0.1-kodagent med lokal modell | **FAIL** | `llama3.1` gav `Code agent response was not valid JSON` |
| 16 | Kontrollerad körning vid misslyckad supervisor-tick | **FAIL (F1)** | kvarlämnat jobb kördes av envelope-blind V0.1-väg |
| 17 | Dockerbaserad isolerad installation | **NOT_RUN** | rollen saknar docker-grupp och sudo (oförändrat hinder) |
| 18 | Riktig PR mot GitHub | **NOT_RUN** | grenarna är publicerade, men ingen PR öppnas i det här uppdraget (ingen merge, ingen PR-begäran) |
| 19 | UI-verifiering i webbläsare | **NOT_RUN** | ingen webbläsare/Playwright i miljön |
| 20 | Codex-SHA / Z.ai-bundle | **BLOCKED** | leveranserna finns inte på remote; ej rekonstruerade (enligt instruktion) |

## Vad som faktiskt behövde ändras (konfiguration, ingen kod)

1. **`DEFAULT_LLM_PROVIDER`/`DEFAULT_LLM_MODEL` saknades.** `get_capability_status()` frågar bara
   den *primära* providern (`resolve_active`), medan samtalen faller tillbaka via
   `chat_with_fallback` → Ollama. Följden var att `task_execution` rapporterades som
   "implemented but not currently configured to run" trots att chatten fungerade. Åtgärdat i
   stackens `start-backend.sh`/`start-worker.sh` (samma mönster som `DEFAULT_EMBEDDING_*`).
   Efteråt: alla fyra capabilities `available=True`.
2. **`approval_policy` måste vara `autonomous_development_work`.** Safe Planner vägrar planera
   lokala skrivningar under `standard_repo_work` (som är default för grundarskapade mål). Det är
   korrekt fail-closed, men måste sättas explicit när målet skapas.

## Flödet som verifierades

```
POST /api/mainai/execution/goals            approval_policy=autonomous_development_work
POST /api/mainai/execution/goals/{id}/plan  lokal modell, ~45 s
POST /api/mainai/execution/tasks/{id}/approve        (varje repo_edit-task)
POST /api/execution-envelopes/goals/{id}/propose
POST /api/execution-envelopes/proposals/{id}/authorize   paths + capabilities, risk=low
POST /api/provider-spend/authorize          1.00 USD / 50 anrop, endast ollama/llama3.1

worker (2 s poll, ingen chatt):
  eligible_authorized_goals -> supervisor_goal_lease -> dispatch_ready_task
  -> prepare_context (envelope omverifieras) -> Safe Planner (deterministiskt recept)
  -> development_driver -> development_operator (create_file, focused_pytest)
  -> verification -> mainai_checkpoints -> task completed -> nästa task -> goal rollup
```

Avgränsning: worktreen skapas med `git worktree add` från workerns checkout. `push_branch` ingår
aldrig i capability-setet, `remote_write_authorized=False`, och ingen merge/deploy-task finns i
`KNOWN_TASK_TYPES`.

## Krasch- och återhämtningsbevis (mål `635ca2da…`)

```
07:53:40  SIGKILL mitt i read_only_audit   task=running job=running lease_left=102 s
07:55:24  dead_detected -> recovery_started -> recovery_inspected -> recovery_classified
          -> takeover_started -> salvage_started/completed -> takeover_completed
07:55:24  task: auto_recovery_triggered -> retry_scheduled -> dispatched (attempt 2, nytt jobb)
07:56:14  verification_passed -> completed        (exakt en completed)
```

Återhämtningstiden styrs av `mainai_jobs`-leasens TTL (120 s), inte av mål-leasen:
`supervisor_goal_leases` claimas och släpps i samma transaktion, så en `SIGKILL` mitt i en tick
rullar tillbaka den helt (verifierat — ingen kvarlämnad aktiv lease).

## Fynd

**F1 (hög, kräver eget beslut/PR) — kvarlämnat jobb kan köras av envelope-blind V0.1-väg.**
`run_supervisor()` anropar `dispatch_ready_task()` *innan* planeringen. Misslyckas ticken därefter
(t.ex. `CandidateValidationError` för fel `approval_policy`) ligger `mainai_jobs`-raden kvar som
`queued` och plockas av `claim_next_mainai_job` → `run_task_execution_job` → `_propose_repo_edit`,
som i `github_write_enabled=false`-låge skriver i den **delade** checkouten utan att konsultera
envelopen. `_advance_mainai_execution_tasks` exkluderar envelope-styrda mål, men jobbklaimningen
gör ingen sådan kontroll. Reproducerat: jobb `fbfa05b4…` kördes denna väg (föll enbart för att
modellen inte gav giltig JSON). Rekommendation: avvisa `task_execution`-jobb vars mål är
EVER_GOVERNED i `process_claimed_mainai_job`, och parkera tasken (inte `ready`, som skulle ge
jobbspam varannan sekund) — kräver ett beslut om park-vs-retry innan implementation, därför inte
gjort här.

**F2 (medel) — det deterministiska receptet är målscopat, inte task-scopat.** Varje `repo_edit` i
samma mål upprepar samma två filer (`calculator.py`, `test_calculator.py`); worktreen nollställs
mellan tasks, så task 2 skriver om task 1:s arbete. Fungerar för verifiering, men en plan med
flera `repo_edit` kan inte utföra olika arbeten.

**F3 (medel) — lokal modell duger inte som kodagent.** `llama3.1` producerade ogiltig JSON för
`_propose_repo_edit`. Den deterministiska operator-vägen (supervisor) är oberoende av modellen och
fungerar; V0.1-vägen för `repo_edit`/`read_only_audit` kräver en starkare modell.

**F4 (låg) — `supervisor_goal_leases` växer med en `released`-rad per tick.** 46+ rader per mål
under en kort körning. Ofarligt men onödig tillväxt; ingen städning finns.

**F5 (låg) — capability-grinden och samtalskedjan är inte samma sanning.** `get_capability_status`
använder `resolve_active`, samtalen använder `resolve_chat_chain`. En installation med fungernade
fallback men oanvänd primär provider rapporterar capabilities som otillgängliga. Åtgärdat här via
konfiguration; om beteendet ska ändras är det ett policybeslut (får en capability räknas som
tillgänglig enbart via fallback?).

**F6 (låg, miljö) — sessionscookien blir ogiltig efter backend-omstart** i den här lokala miljön
(401 "Inloggning krävs" till dess att man loggar in igen). Inte undersökt närmare; påverkade bara
testsessionen.

**F7 (hög, åtgärdad på egen branch) — en misslyckad tick höll målet låst i upp till en timme.**
`run_authorized_goal_supervisor_tick()` claimar `supervisor_goal_leases` och släpper den i
`finally`, men `dispatch_ready_task()` committar mitt i ticken (`create_job()`s atomiska commit).
Faller ticken därefter rullar anroparen (`_advance_authorized_supervisor_goals`) tillbaka hela
transaktionen — inklusive releasen — så en redan committad, aktiv lease blev kvar. Mätt: mål
`1e765096…` låg `active` med 3600 s kvar efter ett enda `CandidateValidationError`, dvs. ingen
återupptagning på en timme. Åtgärdat i
`freebuff/supervisor-failed-tick-lease-release-20261127` (egen branch, regressionsprov som
faller utan fixen). **Får inte mergas ensam:** med leasen frisläppt återförsöker en permanent
felkonfigurerad måltick var ~2 s, vilket måste lösas tillsammans med F1 (parkera tasken i
stället för att lämna den `running` och lämna jobbet åt V0.1-vägen).

## Vad som INTE är gjort

* Ingen merge, ingen PR, ingen deploy. Två grenar är publicerade (se "Publicering"); ingen av
  dem är mergad. Produktfixen (F7) är **inte** aktiverad i den isolerade stacken, som
  fortfarande kör den verifierade kandidaten `85a1c10e`.
* Ingen docker/staging-verifiering (behörighet saknas).
* Codex- och Z.ai-leveranserna är fortfarande blockerade och har inte rekonstruerats.

## Branch och patchar

* Branch: `freebuff/supervisor-failed-tick-lease-release-20261127` (bas `85a1c10e`), två commits,
  publicerad till `origin` (se "Publicering").
* `0001-Sl-pp-m-l-leasen-n-r-en-supervisor-tick-misslyckas.patch` — fix + regressionsprov.
* `0002-Registrera-branchen-f-r-m-l-lease-fixen.patch` — `docs/BRANCH_REGISTRY.md`.
* Verifiering: 21/21 i `test_supervisor_production_entry.py`, 35/35 i
  `test_supervisor_goal_lease.py` + `test_recovery_takeover_authority_fencing.py` +
  `test_worker_live_lease_expiry_takeover.py` + `test_autonomy_spend_narrow_wire.py`, ruff rent.
  Provet faller utan fixen (`assert ['active'] == []`).

## Publicering

Skrivbehörigheten verifierades med `git push --dry-run` innan något skickades. Publiceringen gick
via deploy-nyckeln `~/.ssh/lifeai_github` mot `git@github.com:d1n095/LifeAI.git` (fetch via
HTTPS). Ingen force-push, ingen merge, ingen PR, ingen deployment.

| Gren | Bas | Innehåll |
|------|-----|----------|
| `freebuff/night-worker-delivery-20261127` | `85a1c10e` | den här katalogen + registerposten |
| `freebuff/supervisor-failed-tick-lease-release-20261127` | `85a1c10e` | F7-fixen `3fa49751` + registerposten `f51e85bd` |

Publicerad commit på den här grenen: `7d2c08d6b6e58350c2142b03903bb17d0514961e`.
F7-grenens topp är `f51e85bd5d1b94cb4bf15bc72c773d8452762a53` (själva fixen `3fa49751`).

Basen `85a1c10e` fanns inte på `origin` före publiceringen, så grenarna bär med sig den
verifierade kandidatens historik (76 respektive 78 commits från närmaste gemensamma förfader).
Det är avsiktligt: det som publiceras är exakt den kod som verifierades, inte en ombaserad kopia.
Ingen av grenarna rör `origin`s standardgren `claude/det-kommer-mer-879lcm`.

## Filer

* `nightly-e2e.sh` — hela kedjan, exit 0 vid `GOAL COMPLETED`.
* `local-stack-config.md` — de två konfigurationsändringarna i den lokala stacken.
* `0001…patch`, `0002…patch` — F7-branchen som patchar.
