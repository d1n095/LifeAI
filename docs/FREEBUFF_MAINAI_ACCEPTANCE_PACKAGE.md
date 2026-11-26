# Freebuff → Codex — MainAI-acceptanspaket för Founder Alpha-kandidaten

**Datum:** 2026-11-26
**Från:** `freebuff/mainai-founderalpha-acceptance-20261126` (grenad från `c854c2df`, den
publicerade integrationsöverlämningen)
**Till:** Codex (slutlig merge + acceptans), Z.ai (R31/egress), Claude (frontend)
**Status:** verifieringspaket — **ingen merge, ingen deployment, ingen ändring av
`security/POLICY_LOCK.json`, `first_cycle_go=false`**. Inga av Z.ai:s, Claudes eller Codex
grenar är rörda.

Detta dokument beskriver det automatiserade acceptanspaketet. Det svarar på: vad som faktiskt
bevisar var och en av de tio kapaciteterna, hur var och en av de 17 konflikterna verifieras och
vem som ansvarar, vad som saknas i mätbarhet, vad som går att köra lokalt respektive kräver
riktig Ollama eller 16 GB-VPS, och exakt vilka kommandon Codex ska köra.

---

## 1. Vad paketet är — och vad det inte är

Två körbara verktyg, båda orkestratorer över kod som redan finns. Inget nytt testramverk, ingen
duplicerad svit, inga nya påståenden om produkten.

| Verktyg | Svarar på | Status
|---|---|---|
| `backend/scripts/acceptance/mainai_capability_acceptance.py` | "Vilka befintliga tester bevisar kapacitet A01–A10, körde de, och vad sade de?" | ny |
| `backend/scripts/acceptance/alpha_merge_invariants.py` | "Uppfyller den sammanslagna kandidaten varje konfliktdirektiv i överlämningen?" | ny |

Båda återanvänder befintlig semantik i stället för att uppfinna egen:

- **JUnit → status** hämtas från `program_matrix.py` (`_junit`), samma tolkning som F01–F24
  använder, så en kapacitetsstatus och en workstream-status aldrig kan säga olika saker.
- **Testerna** är de 337 testmoduler som redan finns. Paketet pekar på dem; det skriver inga
  egna ersättningar.
- **Miljögrinden** (`_guard_url`) följer `scripts/integration/common.py`: loopback-only och
  databasnamnet måste innehålla `test`/`staging`/`pytest`, eftersom `backend/tests/conftest.py`
  droppar och återskapar den databasen.
- **`merge_junit.py`, `qa_gate.py`, `founder_alpha_program.py`** är orörda. Detta paket lägger
  sig bredvid dem, inte ovanpå.

Vad paketet **inte** gör: det migrerar inget, deployar inget, mergar inget, aktiverar ingen
autonom körning och rör ingen annan agents gren.

---

## 2. Kör så här

### 2.1 Kapacitetsmatrisen (lokal nivå)

```bash
cd backend
export PATH="$(dirname "$(which python)"):$PATH"   # alembic-subprocessar behöver samma bin

DATABASE_URL=postgresql://postgres@127.0.0.1:55432/lifeos_test_capability \
APP_DATABASE_URL=postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_test_capability \
LIFEAI_TEST_DATABASE_NAME=lifeos_test_capability \
REDIS_URL=redis://127.0.0.1:56379/11 \
python scripts/acceptance/mainai_capability_acceptance.py \
  --out-dir ../docs/evidence/mainai-capability/local
```

Exit-kod: `0` = PASS, `2` = FAIL, `3` = BLOCKED. Rapporten skrivs som JSON + Markdown i
`--out-dir` (JSON:en är den maskinläsbara evidensen).

### 2.2 Med riktig lokal modell

```bash
# samma kommando, plus:
  --allow-real-ollama --chat-model qwen2.5:0.5b
```

Nivån tänds **bara** om modellen faktiskt är installerad (`GET /api/tags` verifieras); annars
blir den `NOT_RUN` med orsaken utskriven. Att köra utan flaggan kan aldrig ge en grön
modellstatus.

### 2.3 Efter Codex merge — invarianterna

```bash
cd backend
python scripts/acceptance/alpha_merge_invariants.py \
  --merged <scratch-merge-sha> \
  --expected-head 0095 \
  --policy-lock-base 86671468 \
  --out ../docs/evidence/founder-alpha/merge-invariants.json
```

`--merged` krävs för INV-11 (ingen förälders testfil får försvinna i mergen) och
`--policy-lock-base` för INV-12 (POLICY_LOCK är Z.ai:s). Utan dem blir de `BLOCKED` — aldrig
`PASS`.

### 2.4 Den samlade ordningen för Codex

1. `python scripts/acceptance/alpha_merge_invariants.py --merged <sha> --policy-lock-base 86671468`
   → allt utom INV-11/12 ska vara `PASS` innan något annat körs.
2. `alembic heads` → exakt en rad, `0095`. `alembic upgrade head` mot **färsk** databas.
3. `python scripts/acceptance/mainai_capability_acceptance.py --allow-real-ollama …`
4. `python scripts/acceptance/program_matrix.py …` (F01–F24, oförändrat) + `qa_gate.py`.
5. Först därefter PR mot `integration/founder-alpha-20261121`.

---

## 3. De tio kapaciteterna och vad som bevisar dem

Statusen härleds ur JUnit-bevis, aldrig ur "en testfil finns". `--list` skriver samma tabell
direkt ur koden, så dokument och matris inte kan glida isär.

| id | kapacitet | nivåer | moduler | F-motsvarighet |
|---|---|---|---|---|
| A01 | Vanliga samtal utan kunskapsbibliotek | local | 5 | F04, F05, F06 |
| A02 | Beständigt minne mellan samtal och omstarter | local, needs_real_ollama | 6 | F06, F10, F15 |
| A03 | Projektkontext och projektstatus | local | 4 | F04, F06 |
| A04 | Kontextkoppling mellan dokument, beslut och uppgifter | local | 5 | F07, F09, F10 |
| A05 | Målsättning → plan → godkännande → tillåten åtgärd | local | 6 | F15, F16 |
| A06 | Agentstatus: ARBETAR / BLOCKERAD / KLAR | local | 4 | F15, F16 |
| A07 | Ingen oavsiktlig dubbelkörning eller uppdragsfördelning | local | 6 | F14, F15 |
| A08 | Korrekt ägarisolering och behörighetskontroll | local, alpha_merge | 9 | F03, F10 |
| A09 | Återhämtning efter avbruten körning | local | 7 | F13, F15, F18 |
| A10 | Korrekta kapacitetspåståenden | local | 6 | F04, F05 |

Två saker är värda att lyfta, eftersom de inte framgår av en filräkning:

- **A06 har en verklig implementationsyta.** `app/agent_coordination/runtime_view.py` mappar
  `WorkAssignmentStatus` → `RuntimeStatus` (`RUNNING`/`BLOCKED`/`COMPLETED` m.fl.) med
  dokumenterade prioriteringar, och `WAITING_DEPENDENCY`/`WAITING_REVIEW` är egna tillstånd i
  stället för att klumpas ihop med "arbetar". Det är den modell de svenska etiketterna
  ARBETAR/BLOCKERAD/KLAR vilar på.
- **A02 har en nivå som kräver riktig modell.** `test_project_session_os_restart.py` dödar
  riktiga `uvicorn`- och `worker`-processer med SIGKILL/SIGTERM och läser tillbaka allt ur
  Postgres. Det är det starkaste minnesbeviset som finns, och det kan inte köras på en masked
  utan installerad chattmodell.

---

## 4. Konflikterna: integrationsregel → verifieringsmetod → ansvarig

Överlämningens §6 angav regel och motiv. Här är samma 17 filer med en **körbar**
verifieringsmetod och en namngiven ägare. `INV-nn` är `alpha_merge_invariants.py`.

| # | Fil | Regel | Verifiering | Ansvarig |
|---|---|---|---|---|
| 1 | `providers/base.py` | Alpha | `pytest tests/backend/providers/ tests/backend/chat/test_chat_streaming.py` + INV-01 | Codex |
| 2 | `providers/ollama_provider.py` | Alpha + mina `follow_redirects=False, trust_env=False` | `pytest tests/backend/providers/test_provider_streaming.py tests/backend/providers/test_ollama_provider_failclosed.py` + INV-02 | Codex |
| 3 | `providers/registry.py` | **BLOCKERAD** — säkerhetsägare | INV-04 + `pytest tests/security_r15_egress/` | **Z.ai (R31) + grundaren** |
| 4 | `agent_orchestration.py` | Redan löst i min gren | `pytest tests/backend/test_agent_orchestration.py` + INV-07 | Codex (re-kör) |
| 5 | `routers/agents.py` | Redan löst i min gren | samma som #4 | Codex (re-kör) |
| 6 | `jobs/handlers/corpus_review.py` | Behåll bägge | `pytest tests/backend/jobs/test_mainai_jobs.py` + INV-09 | Codex |
| 7 | `mainai_execution/lesson_conflicts.py` | **Alpha — säkerhetspolicy** | INV-05 + `pytest tests/backend/test_mainai_execution_lesson_conflicts.py` | Codex + grundaren (policy) |
| 8 | `schemas.py` | Min superset | `pytest tests/backend/chat/test_message_out_contract.py` + INV-08 | Codex |
| 9 | `scripts/ci/run_e2e_backend.py` | Alpha | kör e2e-skriptet i CI/VPS; INV-01 täcker kontraktet | Codex |
| 10 | `tests/backend/chat/test_chat_streaming.py` | Alpha | `pytest tests/backend/chat/test_chat_streaming.py` | Codex |
| 11 | `tests/backend/test_agent_orchestration.py` | Likvärdiga | `pytest tests/backend/test_agent_orchestration.py` | Codex |
| 12 | `docs/FOUNDER_ALPHA_LOCAL_AI.md` | Manuell, trivial | ingen (dokumentation) | Codex |
| 13 | `frontend/app/(shell)/chat/page.tsx` | Alpha | Playwright + visuell granskning | **Claude** |
| 14 | `frontend/app/(shell)/page.tsx` | Manuell | Playwright | **Claude** |
| 15 | `frontend/components/Sidebar.tsx` | Alpha + mina länkar | Playwright + manuell navigering | **Claude** |
| 16 | `frontend/lib/api.ts` | Manuell | `tsc`/`next build` + Playwright | **Claude** |
| 17 | `security/POLICY_LOCK.json` | **Rör inte** | INV-12 (`git diff --stat <bas> -- security/POLICY_LOCK.json`) | **Z.ai** |

Två korrigeringar mot den tidigare överlämningen, båda verifierade i den här grenen:

- **`tests/backend/test_migration_state_privileges.py` finns inte här.** `git ls-tree
  b313c9df` visar att filen bara finns på Alpha-sidan. Kommandot i överlämningens §10 fallerar
  därför på den här grenen. Matrisen lägger den i nivån `alpha_merge`, så den tänds
  automatiskt efter Codex merge i stället för att se ut som en brist.
- **`tests/security_r15_successor/test_r15_successor_regressions.py` anropade
  `dispatch_task(owner_id=…)`**, vilket gick sönder när kontraktet bytte till
  `authorized_by_user_id` i `45fe2fb9`. Det upptäcktes av den här körningen och är åtgärdat
  (se §8).

---

## 5. Mätbarhet: svarstid, modellinformation, felrapportering

Undersökt i den här grenen, med filhänvisningar. Inget i Claudes frontend eller chattflöde är
ändrat.

### 5.1 Vad som finns

| Fråga | Svar | Var |
|---|---|---|
| Vilken provider/modell svarade? | `ChatMessageOut.provider`, `.model`, `.providers_attempted`; `Message.provider`, `.model` | `app/schemas.py`, `app/models/conversation.py` |
| Får en overifierad extern modell användas? | Nej — exakt-modell-policy är fail-closed | `app/routers/chat.py` (`_require_verified_exact_model`) |
| Gick svaret fel, och går det att försöka igen? | `assistant_status`, `error_category` (sluten vokabulär ur `VerificationResult`), `error_message`, `retryable` | `app/schemas.py` (`ChatMessageOut`, `MessageOut`) |
| Överlever felet en omladdning? | `Message.status` + `error_category` + `in_reply_to_id` | `app/models/conversation.py`, `tests/backend/chat/test_message_out_contract.py` |
| Oavgjorda, debiterbara försök | `GET /api/chat/unresolved`, `POST /api/chat/unresolved/{id}/reconcile`, `release_allowed` bara för bevisbart pre-egress | `app/routers/chat.py`, `ChatUnresolvedAttemptOut` |
| Är den lokala modellen installerad? | `/api/health` → `local_model{runtime, reachable, configured_models, missing_models}` | `app/routers/health.py` (`_check_local_model`) |
| Lever workern? | `/api/health` → `worker: alive|unknown`, plus `/api/incidents/health` | `app/routers/health.py`, `app/jobs/heartbeat.py` |
| Incidenter | lista, health, acknowledge, resolve | `app/routers/incidents.py` |

### 5.2 Konkreta luckor

- **G1 — ingen mätbar svarstid i chattflödet.** `grep -rn latency app/` ger träffar i
  `provider_planning/service.py`, `mainai_school/teacher_ledger.py`, `openai_consult/metrics.py`
  och `mainai_executive/bottleneck.py` — men **inget** i `app/routers/chat.py`,
  `app/models/conversation.py` eller `ChatMessageOut`. Grundaren kan alltså inte se hur lång
  tid ett svar tog, och Claude kan inte rendera det. Kapaciteten finns i systemet men är inte
  kopplad till samtalet. Åtgärd kräver ett fält + en migration = produktändring, utanför detta
  uppdrag.
- **G2 — ARBETAR/BLOCKERAD/KLAR saknar HTTP-yta.** `RuntimeStatus` beräknas i
  `app/agent_coordination/runtime_view.py` men **ingen router** refererar
  `agent_runtime_snapshot`/`all_agents_runtime_snapshot`/`work_registry_snapshot`. Backend kan
  svaret; gränssnittet kan inte hämta det. Se även A06 i §3.
- **G3 — identitets- och minnespåståenden neutraliseras inte.** `app/mainai_runtime_contract.py`
  omskriver sju kategorier av **arbets**påståenden ("arbetar i bakgrunden", "återkommer",
  "övervakar", "har granskat allt", "är klar", "har startat", "meddelar när det är klart").
  Motsvarande regel för "jag är …" (identitet) och "jag minns …" (minnesfunktion) finns inte;
  där bär systemprompten + provider/modell-proveniensen hela lasten. Det är en ärlig lucka, inte
  en bugg som tystats.
- **G4 — ingen token-/kostnadsvisning per chattmeddelande i svaret.** `ChatProviderAttempt`
  lagrar `reserved_tokens`/`reserved_cost_micros` för avräkning, men `ChatMessageOut` exponerar
  dem inte. Räknas som lucka, inte som fel: avräkningen är korrekt, bara inte presenterad.

---

## 6. Lokalt vs riktig Ollama vs 16 GB VPS

| Nivå | Vad den bevisar | Var den kan köras | Kört här |
|---|---|---|---|
| `local` | affärslogik, RLS, behörighet, recovery, plan/godkännande, agentstatus — mot riktig Postgres + Redis + begränsad app-roll | valfri dev-maskin | **ja** |
| `alpha_merge` | sviter som bara finns på Alpha-sidan (t.ex. `test_migration_state_privileges.py`) | efter Codex merge | nej (filen finns inte före merge) |
| `needs_real_ollama` | riktig modell i produktionskodvägen, inkl. OS-processomstart | maskin med Ollama + pinnad modell | **ja** (`qwen2.5:0.5b`) |
| `needs_vps` | 16 GB-VPS, produktionsmodell, 4 h soak, Playwright/Chromium, riktig egress | VPS:en | **nej, aldrig påstått** |

Det som **inte** får dras slutsatser om från lokal körning: produktionsmodellens latens och
minnesbehov, 4-timmars uthållighet (F23), webbläsarflöden (Chrome saknas i denna miljö) och
egress mot riktiga leverantörer.

---

## 7. Körningsresultat (verifierbara)

Två körningar mot `c854c2df` i den här maskinen: riktig PostgreSQL 16 på `127.0.0.1:55432`,
riktig Redis på `127.0.0.1:56379`, riktig Ollama på `127.0.0.1:11434` med `qwen2.5:0.5b` och
`all-minilm`.

**Körning 1 — lokal nivå, alla tio kapaciteter:**

```
2 failed, 642 passed, 1 skipped, 8695 warnings in 1028.01s (0:17:08)
```

**Körning 2 — A02 med riktig modell (`--only A02 --allow-real-ollama`):**

```
31 passed, 264 warnings in 36.91s        (lokal nivå)
 2 passed,  67 warnings in 76.05s  (0:01:16)  (needs_real_ollama: in-process + OS-omstart)
```

| id | kapacitet | status | lokalt | riktig Ollama | alpha_merge | tester | fel | skip |
|---|---|---|---|---|---|---|---|---|
| A01 | Vanliga samtal utan kunskapsbibliotek | `PASS` | PASS | — | — | 49 | 0 | 0 |
| A02 | Beständigt minne mellan samtal och omstarter | `PASS` | PASS | PASS | — | 33 | 0 | 0 |
| A03 | Projektkontext och projektstatus | `PASS` | PASS | — | — | 32 | 0 | 0 |
| A04 | Kontextkoppling mellan dokument, beslut och uppgifter | `PASS` | PASS | — | — | 30 | 0 | 1 |
| A05 | Målsättning → plan → godkännande → tillåten åtgärd | `FAIL` | FAIL | — | — | 72 | 1 | 0 |
| A06 | Agentstatus: ARBETAR / BLOCKERAD / KLAR | `PASS` | PASS | — | — | 72 | 0 | 0 |
| A07 | Ingen oavsiktlig dubbelkörning eller uppdragsfördelning | `PASS` | PASS | — | — | 61 | 0 | 0 |
| A08 | Korrekt ägarisolering och behörighetskontroll | `PASS` (lokal) | PASS | — | NOT_RUN | 190 | 0 | 0 |
| A09 | Återhämtning efter avbruten körning | `FAIL` | FAIL | — | — | 82 | 1 | 1 |
| A10 | Korrekta kapacitetspåståenden | `PASS` | PASS | — | — | 56 | 0 | 0 |

Åtta av tio kapaciteter är gröna på lokal nivå. De två `FAIL` är **inte** i den här leveransen
och är bevisat befintliga före den (se §8). De två `NOT_RUN` är nivåer som medvetet inte är
påstådda: A02:s modellben kördes separat (§7 körning 2) och A08:s `alpha_merge`-ben är den
Alpha-only-svit som inte finns före merge.

Råevidens: `mainai-capability-acceptance.json` + `junit-local.xml` per körning. JSON:en är den
maskinläsbara källan; Markdown-tabellen ovan är renderad ur den.

**Invarianter före merge** (`alpha_merge_invariants.py` på `c854c2df`):

```
Totals: {'PASS': 7, 'FAIL': 5, 'BLOCKED': 1}
```

De fem `FAIL` är exakt de oavgjorda konflikterna — INV-01 (strömningskontrakt), INV-03
(`local_only_mode`), INV-04 (egress-modellen), INV-05 (`detect_conflict`), INV-06
(lease-kontraktet) — och `BLOCKED` är INV-11, som kräver en merge-commit att jämföra föräldrar
mot. Det är den förväntade baslinjen: verktyget finns för att bevisa att Codex merge löser dem,
inte för att de ska se gröna ut i förväg.

---

## 8. Fel i sviterna: ett jag införde, två som fanns före

Acceptanskörningen gjorde precis det den ska: den hittade ett fel som jag själv hade infört.

**F1 — infört av mig, åtgärdat.**
`tests/security_r15_successor/test_r15_successor_regressions.py::test_agent_dispatch_passes_effect_guard`
anropade `agent_orchestration.dispatch_task(..., owner_id=…)`. När `45fe2fb9` bytte parametern
till `authorized_by_user_id` (Alpha:s publicerade kontrakt) uppdaterades
`tests/backend/test_agent_orchestration.py` men inte den här filen, så anropet gav `TypeError`.
Åtgärdat i den här grenen; verifierat `11 passed` för modulen och `190 passed, 0 failed` för
hela A08. Hade detta gått till staging hade ett av R15:s egna säkerhetsregressionstester varit
rött utan att någon CI kört det.

**F2 — befintligt före denna leverans, ej åtgärdat.**
`tests/backend/mainai/test_goal_intake_path_a_worker_live.py::test_goal_intake_path_a_worker_live_to_goal_complete`
fallerar på assertionen på rad 290: efter godkänd spend och åtta supervisor-varv förblir
uppgiften inte `completed`, och checkpointen rapporterar
`PROVIDER_SPEND_NOT_AUTHORIZED` trots att testets egen `provider_spend_is_live(...)` precis
passerat. **Bevisat befintligt:** samma test fallerar identiskt på `13fc211b`
(`git worktree` på den commiten: `1 failed, 86 warnings in 7.26s`), alltså före
integrationsfixarna. Rör inte den här leveransens filer. Ska utredas på egen gren —
`PROVIDER_SPEND_NOT_AUTHORIZED` efter en live-spend är antingen en testförutsättning eller en
riktig avräkningsdiskrepans, och det avgörs inte här.

**F3 — befintligt före denna leverans, ej åtgärdat.**
`tests/backend/mainai/test_recovery_takeover_authority_fencing.py::test_downgrading_past_this_migration_fails_loudly_once_a_real_row_exists_not_silently`
fallerar med `sqlalchemy.exc.ProgrammingError: column "project_id" of relation "mainai_goals"
does not exist` när testet kör `alembic downgrade -1` som riktig subprocess. **Bevisat
befintligt:** samma resultat på `13fc211b` (`1 failed, 20 passed in 42.84s`). Rotorsaken är den
testets egen docstring förutsåg: `downgrade -1` är ett *relativt* mål, och med `0090`–`0095`
ovanpå `0061` träffar `-1` en annan revision än den testet skrevs för. Rekommenderad åtgärd på
egen gren: rikta om till en explicit revision i stället för `-1`. Inte åtgärdat här.

**Miljökrav som upptäcktes:** F3 behöver `alembic` på `PATH`. Verktyget lägger nu sin egen
interpreters bin-katalog först i `PATH` för testsubprocesserna, så det fungerar i en venv utan
extra setup.

---

## 9. Kvarvarande NOT_RUN / BLOCKED

| Post | Status | Skäl |
|---|---|---|
| `providers/registry.py` (egress) | **BLOCKED** | INV-03/04/05/06 fallerar före merge; kräver Z.ai (R31) + grundaren |
| `detect_conflict` | **BLOCKED (policy)** | INV-05; Alpha ska vinna, min sida får inte med |
| A08 `alpha_merge`-ben | **NOT_RUN** | `test_migration_state_privileges.py` finns bara på Alpha-sidan |
| `needs_vps`-nivån | **NOT_RUN** | 16 GB-VPS: produktionsmodell, 4 h soak (F23), riktig egress |
| Playwright / visuell granskning | **NOT_RUN** | Chrome/Chromium saknas i denna miljö; Claudes ansvar |
| CI på grenen | **NOT_RUN** | Inga workflow-körningar; endast Vercel-check historiskt |
| Draft-PR | **BLOCKED** | Token saknade `Pull requests: write` (403) i föregående omgång — inte omverifierat här |
| `security/POLICY_LOCK.json` | orörd | INV-12 PASS mot `86671468`; Z.ai äger R31 |
| `first_cycle_go` | `false` | INV-13 PASS |

Ingen av dessa är dolda i rapporterna: `mainai_capability_acceptance.py` skriver ut nivåerna
per kapacitet och `alpha_merge_invariants.py` skriver ut varje unmet invariant med bevis.
