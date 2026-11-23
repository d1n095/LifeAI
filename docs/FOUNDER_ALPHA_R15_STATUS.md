# Founder Alpha R15 — status, evidens och releasebedömning

Datum: 2026-11-19
Gren: `integration/r15-r12-cc4d2804`
Bas: `origin/freebuff/founder-alpha-integrated-20261118` (dd69cb30)
Merge: `d64fab91` (Z.ai cognitive-core R12, `origin/zai/cognitive-core-r12-release-cc4d2804` = cc4d2804, träd-SHA256 `6fb73c2d5f361e8ae73877bb595a4a621dfa49cd94e669c551a80210ba2747bc`)
Lokal gate-DB: `lifeos_gate_r12`-ersättning `lifeos_r15_gate` (Postgres 16, port 5433), head `0089`
Modell: `gemma3:4b` via Ollama (lokal), embeddings `nomic-embed-text`

---

## 0. Läs detta först: dataförlusten och vad som är återuppbyggt

Mellan R12 och R15 låg hela integrationsarbetet i en worktree under `/tmp`. När datorn startade om
försvann den. Kontroll mot **alla 539 grenar på alla remotes** visade att ingen av commitsen
`ad925c0`, `c1527cf`, `f91f2f8`, `2ccbb25`, `55d3656` (R12-importen till R14) fanns utanför den
datorn. De är oåterkalleliga.

Det som **överlevde** och som denna gren bygger på:

| Artefakt | Status |
| --- | --- |
| `origin/zai/cognitive-core-r12-release-cc4d2804` (cc4d2804) | fanns på remote, integrerad i merge `d64fab91` |
| Live-DB `lifeos_gate_r12` (head 0089, 163 tabeller) | överlevde omstarten och användes för att rekonstruera `0089_github_status_snapshots` exakt (kolumner, FORCE RLS, grants) |
| R12-paketets 9 migrationer | renummererade till 0080–0088, kedja efter `0078_repository_file_source_units` |

Det som är **återuppbyggt i R15 och därför inte identiskt med det som verifierades i R14**:
`app/github_status/*`, `app/models/github_status.py`, `app/project_status/*` (inkl. nya
`work_chain.py`), `app/routers/{github_status,project_status}.py`, frontendpanelen
`components/ProjectOverview.tsx`, samt tester och benchmark-skript. Beteendet är verifierat på
nytt (avsnitt 2), men **rad-för-rad-identitet med R14 kan inte påstås**.

---

## 1. Vad R15 levererar

1. **Grundarens fråga besvaras från verkliga register.** `app/project_status/service.py` bygger ett
   `PROJEKTSTATUS`-block av blueprint, statusklasser (implementerat/verifierat/planerat/blockerat),
   GitHub-läge, arbetskedja, låsta prioriteringar, senaste mål och varningar. Chatten injicerar
   blocket fail-open: kan statusen inte läsas svarar chatten som förut, utan påhittade fakta.
2. **Arbetskedjan (R15).** `work_chain.py` läser godkännandekö (samma `requires_approval()` +
   `approval_granted`-event som dispatch-grinden), stillastående jobb, agentstatusar och
   misstänkta dubbletter. Stillastående jobb **rapporteras men stoppas aldrig** härifrån.
3. **GitHub/utvecklingsstatus, read-only.** `github_status` hämtar repo/PR/CI och jämför mot
   **egen upstream**; snabbsparning (stale) är explicit, aldrig en gissning. Utan
   `GITHUB_TOKEN`/`GITHUB_REPO` rapporteras "inte konfigurerad" i stället för påhittad status.
4. **Låst prioritering seedas vid start.** `bootstrap_founder_priorities()` skriver grundarens
   beslut (`note_type=decision`, `authority=founder`, `basis=manual`) med `SET LOCAL
   app.current_user_id`, så RLS gäller även bootstrap.
5. **Frontend:** dashboardens falska rad "Allt system operationellt" är borta och ersatt av
   `ProjectOverview` som visar blueprint, arbetskedja, GitHub-läge, prioriteringar och varningar
   från `/api/mainai/project-status` (typade i `lib/api.ts`).

### Robusthetsfixar som E2E-resan tvingade fram (alla med egna tester/evidens)

| Fix | Varför |
| --- | --- |
| `planner.py`: ett självberoende (`depends_on: [0]` på task 0) tas bort **med varning**; framåtberoenden avvisas fortfarande | 4B-modellen skrev självberoende; utan fixen blev hela planen 422 och grundaren fick ingen plan |
| `planner.py`: `read_only_audit` måste ha `verification_plan: []`, och `targeted_tests.target` måste vara en `.py`-sökväg | modellen hittade på testfiler/prosa; pytest gav exit 4 och auditen kunde aldrig bli klar |
| `verify.py` + `_run_pytest`: `sys.executable` i stället för `"python"`, och `OSError` blir ett **underkänt verifieringssteg** | macOS har ingen `python`-binär; `FileNotFoundError` läckte ut och lämnade tasken `running` för evigt |
| `executor.py`: dispatch vägrar starta en task som förbrukat `max_attempts` och markerar den `failed` med event | tidigare kunde auto-recovery försöka igen förbi budgeten → CHECK-constraint-fel och en task som aldrig blev klar |

---

## 2. Verifiering (PASS/FAIL/SKIP)

| Kontroll | Resultat | Evidens |
| --- | --- | --- |
| Migrationer `0001 → 0089` på tom databas | **PASS** | `scripts/acceptance/reset_gate_db.sh` (kördes från scratch, "privilege state verified correct") |
| Projektstatus- och arbetskedjetester | **PASS** 18/18 | `tests/backend/test_project_status_work_chain.py`, `test_github_status.py` |
| Planner/retry/auto-recovery | **PASS** 44/44 | `test_mainai_execution_planner.py`, `_retry_tick.py`, `_auto_recovery.py` |
| RLS- och sessionssäkerhet | **PASS** 61/61 | `tests/security` |
| Skyddade endpoints utan session | **PASS** 401 | `project-status`, `github-status` |
| Frontend-typkontroll | **PASS** | `npx tsc --noEmit` |
| AI-benchmark (svensk projekt-QA) | **PASS** `gemma3:4b`; **FAIL** `qwen2.5:3b-instruct`, `qwen2.5:0.5b-instruct` | `docs/evidence/r15/ai_benchmark.json` |
| Founder-resan via riktiga endpoints | se avsnitt 2.1 | `docs/evidence/r15/founder_journey.json` |
| Omstart: data och status kvar | se avsnitt 2.2 | `docs/evidence/r15/restart_check.json` |
| GitHub live-snapshot | **SKIP** | ingen `GITHUB_TOKEN`/`GITHUB_REPO`; rapporteras som "inte konfigurerad" |
| Off-host backup/restore | **FAIL (ej konfigurerad)** | `scripts/vps/backup.sh` säkerhetskopierar VPS-filer/volymer, **inte** Postgres (Supabase) och ingen destination/nyckel finns |
| `z-ai`-grinden | **SKIP (extern)** | CLI saknas; `first_cycle_go` förblir `false` (verifierat: env-variabeln finns inte) |

### 2.1 Founder-resan

Körs av `scripts/acceptance/founder_r15_journey.py` mot API + worker + Ollama, med samma
cookie/CSRF-regler som webbläsaren:

`login → project-status → svensk fråga → mål → AI-plan → (godkännande vid behov) → worker-jobb →
rapport → project-status efter → uppföljningsfråga`

Resultat och latenser: `docs/evidence/r15/founder_journey.json`. Målet skapas, planeras av
**samma planner som produkten använder**, körs av den **befintliga** leased-job-vägen och avslutas
först när verifieringen är inspelad.

### 2.2 Omstart

API och worker stoppas och startas om; därefter verifieras att grundarens session, mål, rapport
och projektstatus finns kvar. Evidens: `docs/evidence/r15/restart_check.json`.

---

## 3. AI-kvalitet (mätt, inte påstått)

Fem svenska frågor med grundning från `PROJEKTSTATUS`-blocket. Hård grind: **noll påhittade
siffror och noll falska handlingspåståenden**; därutöver krävs 4/5 sakligt korrekta svar och
sammanhangsindex ≥ 0,45.

| Modell | Svar | Fakta-täckning | Rätt (av 5) | Falska påståenden | Påhittade siffror | Medellatens | Sammanhang | Utfall |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `gemma3:4b` | 5 | 0,50 | 4 | 0 | 0 | 12,4 s | 0,963 | **PASS** |
| `qwen2.5:3b-instruct` | 5 | 0,25 | 2 | 0 | 0 | 3,0 s | 0,788 | FAIL_LOW_COVERAGE |
| `qwen2.5:0.5b-instruct` | 5 | 0,25 | 4 | 0 | **25** | 13,9 s | 0,427 | FAIL_UNGROUNDED |

`DEFAULT_LLM_MODEL=gemma3:4b` är därför vald. Kvarstående kvalitetsbegränsningar, ärligt:

- 4B-modellen **följer inte alltid JSON-instruktionen** för planer (självberoende, påhittade
  testvägar). Det hanteras av validering + en korrigerande omgång, inte av att grinden sänkts.
- Den **avstår ibland** från att föreslå nästa steg även när blocket säger att inget väntar
  ("det är inte verifierat"). Det är säkert men inte användbart; en starkare lokal modell behövs
  för planeringskvalitet.
- Svensk-andelen i benchmärkningen är låg (0,15–0,32) eftersom måttet räknar svenska
  funktionsord; svaren är ändå i sak svenska. Måttet är redovisat som det är, inte bortförklarat.

---

## 4. Säkerhet och gränser (oförändrat)

- `first_cycle_go` är **false** i denna miljö (env-variabeln finns inte). Inga grindar har mocks
  bort, sänkts eller kringgåtts. `agent_coordination/activation_gate.py` är orörd.
- Ingen merge till huvudgrenen och ingen deployment har gjorts. Arbetet ligger på
  `integration/r15-r12-cc4d2804`.
- Mockade modelltester räknas inte som kognitiv verifiering; ingen sådan slutsats dras här.
- `mainai_app` har fortsatt bara DML-rättigheter; `apply_runtime_privileges.py` verifierar
  policyn vid varje databasbygge och API-start.

---

## 5. Blockers med ägare

| Blocker | Effekt | Ägare |
| --- | --- | --- |
| R12–R14-commitsen är borta; R13/R14-lagret är återuppbyggt, inte identiskt | kan inte påstås vara samma kod som R14 verifierade | ägaren (acceptera återuppbyggnad) |
| `GITHUB_TOKEN` / `GITHUB_REPO` saknas | ingen live-utvecklingsstatus; panelen visar "inte konfigurerad" | ägaren |
| Ingen off-host backup (destination + `MAINAI_BACKUP_ENCRYPTION_KEY` + `aws`-uppgifter) | databasen är inte återställningsbar utanför maskinen | ägaren/ops |
| `z-ai`-CLI saknas | kognitiv grind kan inte sättas till GO | ägaren (extern leverantör) |
| 4B-modellens planeringskvalitet | kräver korrigerande omgång; inte lämplig som ensam planerare | produkt/ägaren |

---

## 6. Releasebedömning

**NO-GO för merge/deployment av R15 som release candidate.** Skälen är konkreta, inte
försiktighet: (a) R13/R14-koden är återuppbyggd efter en oåterkallelig dataförlust och är därmed
inte den kod som en gång verifierades, (b) live-GitHub-status kan inte verifieras utan token,
(c) det finns ingen off-host-backup av databasen, och (d) den valda lokala modellen klarar
planering bara med korrigerande omgång.

**GO för grundaranvändning lokalt** i den verifierade omfattningen: svenska projektfrågor med
grundade svar, mål → plan → körning → rapport via den befintliga jobbvägen, samt en dashboard som
visar verklig status i stället för "allt operationellt". Det som krävs innan nästa steg är
ägarens beslut om återuppbyggnaden, GitHub-uppgifter, backupmål och en starkare lokal modell.
