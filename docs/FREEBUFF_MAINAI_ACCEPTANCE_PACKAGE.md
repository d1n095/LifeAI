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

> **Uppdaterad 2026-11-26:** matrisen nedan var ofullständig. En verklig merge av `70b7fda8` in i
> `b313c9df` ger **16** konflikter, inte 17: `routers/chat.py` saknades helt, medan
> `routers/agents.py` och `security/POLICY_LOCK.json` inte längre konflikerar vid nuvarande tips.
> Se **§11.4** för den korrigerade raden, migrationsläget och vad som ändrades.

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

---

## 10. SHA-referens och exakta kommandon

| Roll | Värde |
|---|---|
| Gren | `freebuff/mainai-founderalpha-acceptance-20261126` |
| Bas (publicerad integrationsöverlämning) | `c854c2dfaa7c950abd3ace2e8fb36733aa066af8` |
| Kod- och dokumentcommit för detta paket | `ecfc642c` |
| Merge-base mot Alpha | `86671468a6dd271b61a3f985434130c4bf470ea8` |
| Alpha/VPS-bas | `b313c9df812bc780c89f34f8d0bc296e21a2d342` |
| Integrationsgren | `0adb108fa5145ad6b320950fb029bfaf96f003b4` |
| Uppföljning 2026-11-26 (kandidatmätning + verktygsfixar) | `9a09e69a` |

Ändrade filer i `ecfc642c`: sju — två nya verktyg, en README-sektion, en rättad
säkerhetsregressionstest, acceptansdokumentet, branchregistret och rättelsen i den tidigare
överlämningen. `security/POLICY_LOCK.json` är **inte** bland dem — den finns över huvud taget
inte på den här sidan (se §11.4), så INV-12 är `BLOCKED` här och `PASS` först på en merge.

**Minimikommando för Codex, i ordning:**

```bash
git fetch origin freebuff/mainai-founderalpha-acceptance-20261126
git switch -c scratch/alpha-merge origin/integration/founder-alpha-20261121

cd backend
python scripts/acceptance/alpha_merge_invariants.py --expected-head 0095 \
  --policy-lock-base b313c9df
#   före merge (på min gren): PASS 6 / FAIL 6 / BLOCKED 2 — de sex FAIL är konflikterna;
#   BLOCKED = INV-11 (--merged) och INV-12 (POLICY_LOCK finns bara på Alpha)

git merge freebuff/mainai-founderalpha-acceptance-20261126
#   lös enligt §4 ovan; stoppa vid providers/registry.py och eskalera till Z.ai

python scripts/acceptance/alpha_merge_invariants.py \
  --merged HEAD --expected-head 0095 --policy-lock-base b313c9df
#   krav: PASS 14 / FAIL 0 / BLOCKED 0 innan något annat körs

alembic heads          # exakt en rad: 0095
alembic upgrade head   # mot färsk databas, inte en redan migrerad

python scripts/acceptance/mainai_capability_acceptance.py \
  --allow-real-ollama \
  --database-url "postgresql://postgres@<host>:<port>/lifeos_test_acceptance" \
  --app-database-url "postgresql://mainai_app:<pw>@<host>:<port>/lifeos_test_acceptance" \
  --redis-url "redis://<host>:56379/11" \
  --out-dir ../docs/evidence/mainai-capability/merged
```

Efter merge ska `A08` få ett grönt `alpha_merge`-ben (`test_migration_state_privileges.py` finns
då) och `A05`/`A09` avgörs av om F2/F3 åtgärdats på egna brancher — paketet rapporterar det
ärligt i stället för att runda det.

---

## 11. Codex-läget 2026-11-26 — kandidaten mätt, inte antagen

Detta avsnitt tillkom i uppföljningsomgången. Det ersätter inget ovan; det rättar två saker som
visade sig fel och lägger till det som saknades för att Codex ska kunna köra utan att gissa.

### 11.1 Verifierat gren- och PR-läge

Kontrollerat med `git ls-remote`/`git fetch` och GitHub API (`GET /repos/d1n095/LifeAI/pulls`),
inte ur minnet:

| Ref | SHA | Betydelse |
|---|---|---|
| `integration/founder-alpha-20261121` | `0adb108f` | Codex integrationsgren, **oförändrad sedan 2026-11-21** |
| `claude/founder-alpha-pre-vps-consolidation` | `b313c9df` | PR **#498** (open, **draft**, base = integrationsgrenen) — nyaste kandidatinnehållet |
| `freebuff/mainai-founderalpha-acceptance-20261126` | `70b7fda8` | denna gren, pushad, **ingen PR** (ingen skrivtoken här) |

`0adb108f` är **förfader** till `b313c9df`, så PR #498 är den kandidat som ska verifieras; det
finns ingen nyare Codex-merge. Min gren har merge-base `dd69cb30` med kandidaten och `86671468`
med Alpha-sidan.

Två praktiska konsekvenser:

- **Det finns ingen sammanslagen kandidat än.** Det som mäts här är kandidaten *som den ligger*.
  Siffrorna i §11.2–§11.3 är därför ett mått på kandidaten, inte på en merge.
- **`tools/integration/freebuff_integration.py` kan inte användas för den här grenen.** Den är
  hårdgrindad på en specifik gammal kandidat (`e392022`) och avvisar allt annat med flit. Det är
  inte ett fel i verktyget och inte heller rätt mekanism här — Codex ska inte felsöka det.

### 11.2 Kandidaten mätt med invarianterna (`b313c9df`)

`python scripts/acceptance/alpha_merge_invariants.py --expected-head 0095 --policy-lock-base
b313c9df` i en worktree på `b313c9df`: **PASS 8 / FAIL 5 / BLOCKED 1**.

| PASS (kandidaten uppfyller redan) | FAIL (kräver mergen) | BLOCKED |
|---|---|---|
| INV-01 strömningskontrakt (`StreamChunk`, ingen `ChatStreamChunk`) | INV-02 `follow_redirects=False, trust_env=False` på streaming-klienten | INV-11 ingen tappad testfil (`--merged` krävs) |
| INV-03 `settings.local_only_mode` | INV-08 `MessageOut` status-`field_validator` | |
| INV-04 lease-assertion + `_filter_local_only` + `_require_policy_candidate` | INV-09 `effect_guard=True` i corpus_review | |
| INV-05 `detect_conflict` fail-closed, ingen `owner_id` | INV-10 head **0089**, inte 0095 | |
| INV-06 lease-kontraktet `lease_worker_id`/`lease_generation` | INV-14 idempotent turn-resolver saknas i chat-routern | |
| INV-07 `authorized_by_user_id` obligatorisk | | |
| INV-12 POLICY_LOCK identisk med kandidaten (den finns bara här) | | |
| INV-13 `first_cycle_go=false` | | |

Samma körning på min gren (`70b7fda8`, `--policy-lock-base b313c9df`) ger **PASS 6 / FAIL 6 /
BLOCKED 2**: min sida saknar INV-01/03/04/06/14, kandidaten saknar INV-02/08/09/10/14 — i stort
sett **komplementära** fel, och INV-12 kan inte avgöras här (filen finns inte). Det är den
förväntade bilden av två grenar som var för sig är ofullständiga och tillsammans ska bli 14 PASS.
Ingen av sidorna är "grön" före merge.

### 11.3 Kandidaten mätt med kapacitetssviten (`b313c9df`)

Samma körning med `--allow-missing`, riktig Postgres/Redis och `--allow-real-ollama`:

- `local`: **1 failed, 541 passed, 1 skipped in 779,6 s**
- `alpha_merge`: **10 passed in 17,6 s** (`tests/backend/test_migration_state_privileges.py` finns
  på kandidaten — benet tänds alltså av sig själv, som det skulle)

**8 PASS / 1 FAIL / 1 NOT_RUN:**

| id | kapacitet | status | tester på kandidaten |
|---|---|---|---|
| A01 | samtal utan kunskapsbibliotek | `PASS` | 43 (2 av 5 moduler saknas) |
| A02 | beständigt minne | `NOT_RUN` | 26 lokalt PASS; real-Ollama-benet har **inga moduler** här |
| A03 | projektkontext | `PASS` | 21 |
| A04 | dokument/beslut/uppgifter | `PASS` | 19 |
| A05 | mål → plan → godkännande | `PASS` | 64 |
| A06 | agentstatus ARBETAR/BLOCKERAD/KLAR | `PASS` | 69 (samma dokumenterade HTTP-lucka G2) |
| A07 | ingen dubbelkörning | `PASS` | 58 |
| A08 | ägarisolering och behörighet | **`FAIL`** | 154 + 10, **1 fel** (se nedan) |
| A09 | återhämtning | `PASS` | 74 |
| A10 | korrekta kapacitetspåståenden | `PASS` | 32 (samma dokumenterade lucka G3) |

A02 är `NOT_RUN` och inte `PASS` eftersom den verkliga modell-delen av beständigt minne
(`test_project_session_real_model.py`, `test_project_session_os_restart.py`) är mina och inte
finns på kandidaten. Det är rätt status: "lokalt grönt, modellen otestad" får aldrig läsas som PASS.

#### A08-fejlet har två oberoende orsaker — och en av dem skapar mergen

`tests/security/test_security_controls.py::test_the_repository_policy_lock_verifies` fallerar.
Uppmätt, inte gissat:

| Orsak | Bevis | Vems |
|---|---|---|
| `.github/workflows/real-local-ai-verification.yml` har ändrats utan att låset uppdaterats | på **orörd** `b313c9df` ger `verify_lock` exakt **1** post: `changed control: … (expected 09076e19a887…, found 5febc65c8064…)` | Alpha/Z.ai — PR #492 är granskningspaketet för just den posten |
| **mina tre acceptansskript är oskyddade styrningar** | med skripten på plats ger samma `verify_lock` **4** poster: de tre nedan som `uncovered control` | Z.ai (låset) + Codex (mergen) |

Låset skyddar `backend/scripts/acceptance/*` fil för fil, och `program_matrix.py` (min gren) samt
de två nya verktygen matchar mönstret men finns inte i `security/POLICY_LOCK.json`. Efter merge
fallerar alltså **A08 på den sammanslagna kandidaten** tills låset fått tre poster — även om alla
tester i övrigt är gröna. Det får inte "lösas" genom att hoppa över testet, och jag rör inte
filen (Z.ai äger den). Hasherna Z.ai behöver, beräknade i den här grenen:

| Fil | sha256 |
|---|---|
| `backend/scripts/acceptance/program_matrix.py` | `987726933c8b4b027de02f450db631889b4f7063239bcf3a810b9b5eca669936` |
| `backend/scripts/acceptance/mainai_capability_acceptance.py` | `4c01c94c9311aca8d6a10db1562a2ab6e399818885eaf96943cd66ac2690455e` |
| `backend/scripts/acceptance/alpha_merge_invariants.py` | `167eae557717b84b54990201b370b73656ad9753859d12c8e900bd25d2502fc5` |

Rekommenderad grind före staging: `pytest tests/security/test_security_controls.py -q` **och**
`python scripts/security/verify_policy_lock.py` (eller motsvarande) ska ge noll poster på den
sammanslagna kandidaten. `--update` i låset är en granskad handling, inte ett testfix.

### 11.4 Korrigeringar — fyra saker som var fel eller ofullständiga

**1. `routers/chat.py` saknades i konfliktmatrisen (§4/§6).** En verklig merge av `70b7fda8` in i
`b313c9df` ger **16** konflikter, och chat-routern är en av dem. Den är semantisk, inte textuell:
Alpha byggde `_ChatContext`/`_build_chat_context`/`_stream_chat_reply` (SSE), min gren byggde
`_get_or_create_chat_turn`/`_replay_chat_request` (idempotens, migration 0090) kring **samma**
entry points. Att ta endera sidan helt tappar den andra garantin — antingen strömningen eller
"ett meddelande ger aldrig två debiterbara provideranrop".

| Fil | Regel | Verifiering | Ansvarig |
|---|---|---|---|
| `backend/app/routers/chat.py` | **Behåll bägge.** Alpha:s streaming-API + min idempotenta turn-resolver, och `stream_chat` måste lösa sin turn via `_get_or_create_chat_turn` — inte via en återuppstånden `_create_or_get_conversation_and_user_message` | INV-14 + `pytest tests/backend/chat/test_chat_request_idempotency.py tests/backend/chat/test_chat_streaming.py` | Codex |

**2. Två rader i den gamla matrisen konflikerar inte längre — och POLICY_LOCK finns bara på ena
sidan.** `routers/agents.py` (#5) och `security/POLICY_LOCK.json` (#17) går igenom merge utan
konflikt. Viktigare: **filen finns inte alls på min sida**, inte heller i `86671468`; den är
Alpha-only (`git ls-tree -r 70b7fda8` och `86671468` ger noll träffar, `b313c9df` och `0adb108f`
ger `security/POLICY_LOCK.json`). Merge-resultatet är byte-identiskt med `b313c9df`, så Z.ai:s fil
är intakt och ingen behöver lösa den — men INV-12 kan inte säga PASS på en gren som inte har filen.

Att jämföra mot `86671468` var därför ett fel i mina egna kommandon: tom diff mot en bas utan lås
ser ut som "orörd" om en fil som inte finns. INV-12 är nu `BLOCKED` i det läget (och i det helt
frånvarande läget), och `--policy-lock-base` ska vara **`b313c9df`**, sha:n som äger låset. Det var
det enda rena falska PASS jag hittade i mitt eget paket den här omgången.

**3. Migrationsläget: nu mätt, inte bara påstått.** Kandidaten har head **0089**; mina `0090`–`0095`
finns bara på min sida — det syns nu som INV-10 FAIL (`head is 0089, expected 0095`). Kedjan är
additiv och kollisionsfri: `0090.down_revision = "0089"` mot *samma*
`0089_github_status_snapshots.py` (`revision = "0089"`, `down_revision = "0088"`) på båda sidorna.
Alltså: efter merge ska `alembic heads` ge exakt `0095`, och staging behöver
`alembic upgrade head` för sex migrationer (chat-idempotens, provider-attempts, runtime-incidents,
life-workspaces, workspace-sync/supervised goals, personal work management). Ingen omnumrering.

**4. `providers/registry.py` är inte längre en blockerare i den form §7.1 beskrev.** Kandidatens fil
innehåller redan alla tre skydd INV-04 mäter (`_verify_lease_assertion`, `_filter_local_only`,
`_require_policy_candidate`), dvs. även den per-försök-omverifiering som §7.1 tillskrev min sida.
Det som *bara* min sida har i den filen är `resolve_egress_owner(db, owner_id, worker_lease)` —
en omläsning av jobbraden som bevisar anroparens ägare — plus `WorkerLease`. Kandidaten har
varken modulen eller anropet.

Konkret för Codex: **ta Alpha:s `registry.py`**. Det tappar exakt en härdning (owner-resolution),
inte LOCAL_ONLY_MODE eller lease-kontraktet, och bryter ingen import: `resolve_egress_owner` finns
kvar i `app/egress_policy/service.py` och används av `provider_planning/service.py` (enda
anropsplatsen med `worker_lease=`). Om härdningen ska tillbaka är det Z.ai:s och grundarens beslut
**efter** merge, som ett tillägg ovanpå Alpha:s modell — det blockerar inte längre mergen
mekaniskt. INV-04/INV-06 ska ändå vara PASS efter merge; de mäter Alpha:s kontrakt.

**Rättelse av min egen checker:** INV-05 gav först falskt FAIL på kandidaten eftersom den letade
strängen `chat_with_fallback` i funktionens källtext — och Alpha:s fail-closed-funktion *förklarar
i sin egen docstring* att den inte anropar den. INV-05 går nu på `ast.Call`-noder och ignorerar
strängar. Kandidaten är alltså fail-closed (§7.2 gäller oförändrat: **min** sida är den som bryter
direktivet), och det var verktyget, inte kandidaten, som hade felet.

### 11.5 De sju områdena — reproducerbara kommandon

Samma miljö hela vägen; `$PY` är en venv med `backend/requirements.txt`. Alla sviter nedan finns i
den här grenen; kolumnen säger om de även finns på kandidaten idag.

```bash
cd backend
export DATABASE_URL=postgresql://postgres@127.0.0.1:55432/lifeos_test_acceptance
export APP_DATABASE_URL=postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_test_acceptance
export REDIS_URL=redis://127.0.0.1:56379/11
export LIFEAI_TEST_DATABASE_NAME=lifeos_test_acceptance
export OLLAMA_BASE_URL=http://127.0.0.1:11434
PY=/tmp/r15-venv/bin/python
```

| # | Område | Kommando | På kandidaten idag |
|---|---|---|---|
| 1 | Beständigt minne | `$PY -m pytest tests/backend/chat/test_project_session_resume.py tests/backend/mainai/test_founder_memory_foundation.py tests/backend/mainai/test_inspectable_memory_foundation.py tests/backend/mainai/test_founder_memory_prompt.py -q` | `test_founder_memory_prompt.py` saknas |
| 2 | Samtal utan kunskapsbibliotek | `$PY -m pytest tests/backend/chat/test_chat_context_status.py tests/backend/chat/test_chat_message_persistence.py tests/backend/chat/test_message_out_contract.py tests/backend/chat/test_chat_failure_recovery.py -q` | `test_message_out_contract.py`, `test_chat_failure_recovery.py` saknas |
| 3 | Projektkontext | `$PY -m pytest tests/backend/test_projects_detail.py tests/backend/mainai/test_project_entities.py tests/backend/mainai/test_project_entity_work_candidate_capture.py -q` | `test_projects_detail.py` saknas |
| 4 | Arbetsflöden och agentstatus | `$PY -m pytest tests/backend/mainai/test_founder_long_horizon_workflow.py tests/backend/mainai/test_execution_envelopes.py tests/backend/mainai/test_workforce_task_approval_enforcement.py tests/backend/mainai/test_agent_runtime_control_plane.py -q` | `test_founder_long_horizon_workflow.py`, `test_agent_runtime_control_plane.py` saknas |
| 5 | Omstart och återhämtning | `$PY -m pytest tests/backend/test_mainai_execution_recovery.py tests/backend/test_mainai_execution_recovery_takeover.py tests/backend/mainai/test_recovery_takeover_authority_fencing.py tests/backend/mainai/test_worker_live_lease_expiry_takeover.py -q` — kör sedan `restart_check.py` före/efter en riktig omstart (nedan) | delvis (recovery-sviterna finns) |
| 6 | Faktisk Ollama | `$PY scripts/acceptance/local_model_acceptance.py --chat-model qwen2.5:0.5b --embed-model all-minilm --out /tmp/local-model.json` | **finns** — återanvänds, ingen dubblering |
| 7 | Behörigheter och ägarisolering | `$PY -m pytest tests/security/ tests/security_r15_egress/ tests/security_r15_successor/ tests/backend/chat/test_messages_rls.py tests/backend/test_runtime_table_privileges.py -q` | `tests/security_r15_egress/`, `tests/security_r15_successor/` saknas (mina) |

Omstart (område 5) på HTTP-nivå — `restart_check.py` skapar inget, den läser bara, så en skillnad
mellan två körningar är en riktig persistensbugg:

```bash
$PY scripts/acceptance/restart_check.py --api http://127.0.0.1:8011 --label before --out /tmp/restart-before.json
# stoppa och starta API + worker på riktigt, sedan:
$PY scripts/acceptance/restart_check.py --api http://127.0.0.1:8011 --label after --out /tmp/restart-after.json
diff /tmp/restart-before.json /tmp/restart-after.json   # förväntat: ingen skillnad i varaktigt tillstånd
```

Riktig modell på område 1 och 2 (kräver installerad pinnad modell; annars NOT_RUN, aldrig PASS):

```bash
$PY scripts/acceptance/mainai_capability_acceptance.py --allow-real-ollama --only A01,A02 \
  --database-url "$DATABASE_URL" --app-database-url "$APP_DATABASE_URL" \
  --redis-url "$REDIS_URL" --out-dir ../docs/evidence/mainai-capability/real-ollama
```

En helhetskörning av alla tio kapaciteter är `--only`-flaggan utan argument (utelämnad), se §2.

### 11.6 Ändringar i den här omgången

Bara i mitt eget ansvarsområde — två verktyg och dokumentation. Ingen produktkod, ingen frontend,
ingen chattimplementation, ingen ändring av Z.ai:s eller Claudes grenar, `POLICY_LOCK` orörd
(den finns inte ens på den här sidan).

**Commit:** `9a09e69a` på `freebuff/mainai-founderalpha-acceptance-20261126`, pushad (remote =
local). Föregående leverans i samma gren var `70b7fda8`.

| Ändring | Varför |
|---|---|
| `mainai_capability_acceptance.py --allow-missing` | verktyget avbröt på en kandidat som inte bär min gren; nu rapporteras frånvarande moduler som `NOT_RUN` med namnet utskrivet i stället för att körningen dör. Standard är fortfarande strikt, så en stavfel i matrisen fångas |
| INV-05 går på `ast.Call` | falskt FAIL av en docstring; en säkerhetsinvariant får inte avgöras av en strängmatchning |
| INV-14 chattrouter-union | konflikten i `routers/chat.py` fanns inte i matrisen och hade därför ingen grind alls |
| INV-12 kräver ett sha som faktiskt äger låset | den gav PASS mot `86671468`, som inte har `POLICY_LOCK` alls; tom diff mot en frånvarande fil är ett falskt PASS på en säkerhetsgrind |
| §11.3: A08-grinden dokumenterad | `verify_lock` ger 4 poster med mina skript på plats men 1 på orörd kandidat — skillnaden är mergens ansvar, inte kanditatens |
