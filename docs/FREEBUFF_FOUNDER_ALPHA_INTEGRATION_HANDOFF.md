# Freebuff → Founder Alpha — integrationsöverlämning till Codex

**Datum:** 2026-11-26
**Från:** `freebuff/mainai-context-atlas-decision-support-20261125`
**Till:** Codex (slutlig integration), med Z.ai som säkerhetsägare för R31
**Status:** integrationsklar kandidat — **ingen merge, ingen deployment, ingen POLICY_LOCK-ändring,
`first_cycle_go=false`**

Detta dokument är skrivet för att kunna läsas utan tidigare kontext. Det svarar på: vad som ska
integreras, i vilken ordning, vilka filer som krockar, vem som vinner varje krock och varför,
vilka krockar som **inte** får avgöras automatiskt, samt vad som är verifierat respektive inte
kört.

---

## 1. Utgångsläge (verifierat mot GitHub och git, inte memorerat)

| Sak | Värde |
|---|---|
| Min gren | `freebuff/mainai-context-atlas-decision-support-20261125` |
| Min HEAD (före denna omgång) | `13fc211b18123877280280c953b6cd3e54bb3b2d` |
| Gemensam förfader (merge-base) | `86671468a6dd271b61a3f985434130c4bf470ea8` |
| VPS-stagingens bas | `b313c9df812bc780c89f34f8d0bc296e21a2d342` (`claude/founder-alpha-pre-vps-consolidation`, PR #498) |
| Integrationsmål | `integration/founder-alpha-20261121` @ `0adb108fa5145ad6b320950fb029bfaf96f003b4` |
| PR för min gren | **0** — ingen PR finns; `POST /pulls` ger 403 `Resource not accessible by personal access token` |
| Öppna PR:er mot integrationsgrenen | 30 (bl.a. #487–#511, #498 = VPS-basen) |
| CI på min gren | **0 workflow-körningar**; enda check-run på `13fc211b` är `Vercel Preview Comments` (success) |

Slutsats: grenen är publicerad och läsbar, men **ingen CI har någonsin kört på den** och den har
ingen PR. Det påverkar vad som får kallas verifierat i avsnitt 11.

---

## 2. Varför detta inte är en fast-forward

```
merge-base 86671468
   ├── 109 commits  → min gren (MainAI, context atlas, workspaces, R15-säkerhet, bevis)
   └──  74 commits  → Founder Alpha (streaming, egress-centralisering, PI-1d, SFA-1, staging)
```

Bägge sidor har alltså byggt vidare **oberoende** sedan `86671468`. Konsekvenser som måste tas
hänsyn till:

- Min gren **kan inte** vara bas för Alpha-kandidaten: den saknar 74 commits som redan ligger på
  VPS-staging, däribland egress-centraliseringen (#468), PI-1d (#469), den riktiga
  token-strömningen (#487), fail-closed vid leverantörsfel (#491) och SFA-1-arbetet.
- Alpha-basen **kan inte** heller användas ensam: den saknar mina 109 commits.
- En naiv "ta min sida" i konflikterna skulle **återinföra stängda säkerhetshål**. En naiv
  "ta deras sida" skulle kasta bort hela MainAI-leveransen. Därför är konfliktmatrisen i
  avsnitt 6 en direktivtabell, inte en lista.

---

## 3. Vad som faktiskt ska integreras (MainAI-värdet)

Min gren är inte en enda funktion. Grupperat efter vad som är värt att ta in:

- **Långsiktigt grundararbetsflöde (denna omgångens kärna).** `8976adbd` gör målintaget
  idempotent (identiskt aktivt mål återanvänds i stället för att dubbelskapas, med
  owner-radslås) och skyddar en aktiv plan mot tyst överskrivning: `POST /goals/{id}/plan`
  svarar `409 active_plan_exists` före AI-anropet om inte `?replan=true` anges. `40faa30f`
  visar upp till tre olösta motsägelser ur grundarens minne som `OSTRIDIGT` i chattprompten i
  stället för att tysta dem. `f51b9ae1` är A–G-testet för hela det långa flödet (8 passed).
- **Context Atlas / System Intelligence 2.0 / executive.** `7f630e76`, `f220501c`,
  `21c608ef`, `c19e87fb`, `bdbaf7a4`, `d1c631be`, `790e2eaf` m.fl.: läsbar källidentitet,
  projektisolering, konsekvenskontroll, path-scoped ownership, glömd-krav-kö och
  evidence-driven utredning.
- **Livsarbetsytor och personligt arbete.** `a26a23d3`, `83c87049`, `0d917c32` med
  migrationer `0093`–`0095`.
- **Robusthet och streaming-återhämtning.** `2265a0e5`, `10bbfea3`, `59184c93`, `9b731f47`,
  `8c4c3f6d`, `832ba6ee`, `3c0e9fe3` samt runtime-incidenter (`0092`).
- **R15-säkerhet.** `db9029e2`, `d06c9eaf`, `69ef11f4`, `6410abee`, `f0c5c346`.
- **Bevis, skript och integrationsverktyg.** `scripts/integration/`, `scripts/testlab/`,
  `scripts/ops/export_commit_stack.sh`, `docs/evidence/**`.

Det som **inte** är nytt och därför inte ska byggas igen: uppgiftsmotor, projektminne,
checkpoints, executive-lager, agentkoordinering och provider-routing fanns redan och
återanvändes oförändrat.

---

## 4. Migrationer — ingen kollision

Verifierat genom att läsa `revision`/`down_revision` på båda sidor:

| Sida | Högsta revision | Kedja |
|---|---|---|
| Founder Alpha (`b313c9df`) | `0089` | … `0088 → 0089` |
| Min gren | `0095` | `0089 → 0090 → 0091 → 0092 → 0093 → 0094 → 0095` |

- Kedjan är **linjär och additiv**, med **exakt en head**. Ingen omanumrering behövs, ingen
  två-headsgaffel uppstår.
- `0090`–`0095` är mina: `chat_request_idempotency`, `chat_provider_attempts`,
  `runtime_incidents`, `life_workspaces`, `workspace_sync_and_supervised_goals`,
  `personal_work_management`.
- Alpha-basen har `0080` omkedjad mot `0079` (`9c662202`) — den ändringen ligger i basen och
  ska bevaras orörd.
- **Krav på Codex:** kör `alembic heads` efter merge och verifiera att svaret är exakt en rad
  (`0095`). Kör `alembic upgrade head` mot en färsk databas, inte bara mot en redan migrerad.

---

## 5. API-kontraktsändringar

| Kontrakt | Founder Alpha | Min gren | Åtgärd |
|---|---|---|---|
| Strömning | `StreamChunk` + `LLMProvider.chat_stream()` (inline NDJSON i Ollama-providern) | `ChatStreamChunk` + `stream_chat()` + `app/providers/streaming.iter_ollama_ndjson` | **Alpha vinner.** Två oberoende implementationer av samma sak; Alpha är den som staging, frontend och e2e redan konsumerar. |
| `chat_with_fallback` låskontrakt | `lease_worker_id` + `lease_generation` (+ `job_id`) | `worker_lease: WorkerLease` | **Säkerhetskärna — får inte avgöras automatiskt.** Se avsnitt 7. |
| `chat_with_fallback` streaming | ingen `stream_observer` | `stream_observer=` | Min blir död kod om Alpha vinner på provider-lagret. Chatt-routern måste då pekas om till `provider.chat_stream()`. |
| Agent-dispatch/granskning | `authorized_by_user_id` (obligatorisk, inget default) | `owner_id` | **Redan åtgärdat i min gren** — se avsnitt 8. |
| `MessageOut` | `status` obligatoriskt | `status` med default + `field_validator` som coercar ORM-enum → str | Min är en superset; behåll min validator och Alpha:s förklarande kommentar. |
| Corpus review | `max_tokens=400` | `effect_guard=True` | **Behåll bägge** — redan åtgärdat i min gren. |
| `detect_conflict` | fail-closed, **noll** provideranrop | kräver `owner_id`, gör riktigt anrop | **Alpha vinner — säkerhetspolicy.** Se avsnitt 7. |

---

## 6. Konfliktmatris — 17 filer med direktiv

`git merge` av `13fc211b` in i `b313c9df` gav 17 verkliga konflikter av 33 filer som båda
sidorna rört. "Alpha" = `b313c9df`-sidan, "Min" = min gren.

| # | Fil | Typ | Direktiv | Motiv |
|---|---|---|---|---|
| 1 | `backend/app/providers/base.py` | innehåll | **Alpha** | `StreamChunk`/`chat_stream` är det som redan går på staging. Min `ChatStreamChunk` ska inte in. |
| 2 | `backend/app/providers/ollama_provider.py` | innehåll | **Alpha**, med ett tillskott från min | Behåll Alpha:s `chat_stream`. Ta med mina `follow_redirects=False, trust_env=False` på streaming-klienten — de stänger miljöproxy- och redirect-egress. |
| 3 | `backend/app/providers/registry.py` | innehåll | **BLOCKERAD — säkerhetsägare** | Två inkompatibla egress-modeller. Se avsnitt 7.1. |
| 4 | `backend/app/agent_orchestration.py` | innehåll | **Redan löst i min gren** | Signatur och `purpose` följer nu Alpha:s kontrakt; kvar är endast additivt `task_id`/`effect_guard`. |
| 5 | `backend/app/routers/agents.py` | innehåll | **Redan löst i min gren** | Anropar `authorized_by_user_id=user.id`. |
| 6 | `backend/app/jobs/handlers/corpus_review.py` | innehåll | **Behåll bägge** | Alpha:s `max_tokens=400` (obundet lokalt svar sprängde 120 s-timeouten) + min `effect_guard=True`. Redan båda i min gren. |
| 7 | `backend/app/mainai_execution/lesson_conflicts.py` | innehåll | **Alpha — säkerhetspolicy** | Se avsnitt 7.2. Min version får **inte** mergas in. |
| 8 | `backend/app/schemas.py` | innehåll | **Min (superset)** | Min har default + `field_validator` utöver Alpha:s fält; ta med Alpha:s kommentar. |
| 9 | `backend/scripts/ci/run_e2e_backend.py` | innehåll | **Alpha** | 5 `StreamChunk`-referenser bundna till Alpha:s strömningskontrakt. |
| 10 | `backend/tests/backend/chat/test_chat_streaming.py` | add/add | **Alpha** | 27 `StreamChunk`-referenser; min fil testar min API-yta. |
| 11 | `backend/tests/backend/test_agent_orchestration.py` | innehåll | **Alpha eller min (nu likvärdiga)** | Min är uppdaterad till `authorized_by_user_id`. |
| 12 | `docs/FOUNDER_ALPHA_LOCAL_AI.md` | innehåll | Manuell, trivial | Endast dokumentation. |
| 13 | `frontend/app/(shell)/chat/page.tsx` | innehåll | **Alpha** | Konsumerar Alpha:s SSE-kontrakt. **Kräver visuell granskning.** |
| 14 | `frontend/app/(shell)/page.tsx` | innehåll | Manuell | Alpha skrev om dashboarden (+642); min +348. Alpha:s founder-console vinner för Alpha. |
| 15 | `frontend/components/Sidebar.tsx` | innehåll | Alpha + mina 5 rader | Ta Alpha:s nav; lägg tillbaka mina länkar om de fortfarande är relevanta. |
| 16 | `frontend/lib/api.ts` | innehåll | Manuell | Behåll Alpha:s stream-typer; lägg till mina endpoints (workspaces, tasks, search, calendar). |
| 17 | `security/POLICY_LOCK.json` | divergerar | **Rör inte** | Z.ai ansvarar. Se avsnitt 7.3. |

---

## 7. Säkerhetskonflikter som kräver ägarbeslut

### 7.1 `providers/registry.py` — två egress-modeller

**Alpha:** `lease_worker_id`/`lease_generation` re-verifieras mot `mainai_jobs` via
`_verify_lease_assertion()` omedelbart före första provideranropet, och `_filter_local_only()`
tillämpar `settings.local_only_mode` (founder-direktivets krav 8) innan någon provider provas.

**Min gren:** `worker_lease: WorkerLease` + `resolve_egress_owner(owner_id, worker_lease)`,
fail-closed lokality (`not getattr(provider, "is_local", False)` ⇒ extern) och
`_require_policy_candidate()` som omverifierar valet per försök.

**Konkret fara:** `local_only_mode` **förekommer inte alls** i min gren. Om min sida vinner i
den här filen försvinner LOCAL_ONLY_MODE tyst — ett krav som redan är stängt på Alpha. Om
Alpha:s sida vinner försvinner min locality-fail-closed och per-försök-omverifieringen.

**Direktiv:** Alpha vinner som bas. Mina två härdningar (locality-fail-closed,
per-försök-omverifiering) får endast läggas ovanpå, och endast efter granskning av Z.ai
(R31) och Codex. Detta är en säkerhetskärna — ingen automatisk konfliktlösning.

### 7.2 `detect_conflict` — min version bryter ett founder-direktiv

Alpha **fail-closar** funktionen med noll provideranrop, med motiveringen att
`EngineeringLesson` är founder-wide, ownerlös state utan `owner_id`-kolumn och att det inte
finns någon legitim founder-wide egress-auktorisering (`ExecutionAuthorizationEnvelope` kräver
`goal_id`). Direktivet (Task #60, krav 5) förbjuder uttryckligen att man hittar på en
`owner_id`.

Min gren tar `owner_id: uuid.UUID` och gör ett riktigt `chat_with_fallback()`-anrop. Att
merga in den sidan vore att fabricera en ägare för ownerlös state — samma anti-mönster som
direktivet förbjuder.

**Direktiv:** Alpha vinner. Min implementation får inte med. Detta är ett medvetet avsteg från
min egen funktion och ska stå kvar som ett öppet beslut tills en riktig founder-wide
auktoriseringsprimitive finns — inte lösas genom att välja min sida.

### 7.3 `security/POLICY_LOCK.json`

Divergerar mellan sidorna. Filen är **orörd** av mig i denna omgång. Z.ai äger R31.
Codex ska inte ta min version.

### 7.4 Alpha-arbete som min gren saknar och som inte får tappas

Egress-centralisering (#468), PI-1d proposal-mode-isolering (#469), prompt-injection-fencing
(`9938fd25`), SFA-1/alembic-privilegier (`2ece0c9b`, `3c759ad2`), hemläckage-tester
(`ab6df85a`) och runtime-privilegier (`test_runtime_privilege_self_heal.py`). Dessa ligger
enbart i Alpha-sidan och ska bevaras oförändrade.

---

## 8. Vad jag ändrade i min egen gren denna omgång (säkra fixar)

Endast två integrationshinder har åtgärdats, båda avgränsade och verifierade. Inga nya
funktioner, ingen omstart av arbetet, ingen rebase.

1. **`backend/app/jobs/handlers/corpus_review.py`** — lade till Alpha:s `max_tokens=400`
   bredvid min `effect_guard=True`. Utan taket kan en lokal modell generera till hela sin
   kontext, vilket sprängde providerns 120 s-timeout och fick jobbet att fallera. Konflikten
   blir nu "behåll bägge" i stället för ett val.
2. **`backend/app/agent_orchestration.py`, `backend/app/routers/agents.py`,
   `backend/tests/backend/test_agent_orchestration.py`** — bytte min `owner_id`-parameter mot
   Alpha:s redan publicerade `authorized_by_user_id` (fortfarande obligatorisk, inget default),
   och alignade `purpose` till `agent_orchestration_dispatch_task` /
   `agent_orchestration_review_task` så att disclosures-ledgern får samma taxonomi på bägge
   sidor. Semantiken är oförändrad: det är fortfarande den live-autentiserade grundarens
   `user.id` som krävs.

**Verifiering efter ändringarna:**

```
pytest tests/backend/test_agent_orchestration.py \
       tests/backend/jobs/test_mainai_jobs.py \
       tests/backend/mainai/test_founder_long_horizon_workflow.py -q
→ 172 passed, 1287 warnings in 211.38s
ruff check app/agent_orchestration.py app/routers/agents.py app/jobs/handlers/corpus_review.py
→ All checks passed!
```

Miljö: riktig PostgreSQL (`127.0.0.1:55432`), riktig Redis (`127.0.0.1:56379`), riktiga
leases och befintlig recovery. Inget mockas på databas-/kö-nivå; LLM-transporten i
A–G-testet är fortsatt fejkad (kodvägen oförändrad).

---

## 9. Rekommenderad integrationsordning för Codex

1. **Skapa en scratch-integrationsbranch** från `integration/founder-alpha-20261121`. Merga
   aldrig direkt i produktions- eller VPS-grenen.
2. **Merga Alpha → min gren** (inte omvänt), så att Alpha-sidans säkerhetscommits är den sida
   som "vinner" i provider-/egress-filerna. Min gren är den som ska anpassa sig.
3. **Lös per avsnitt 6.** Alpha vinner på: `providers/base.py`,
   `providers/ollama_provider.py` (plus mina två klientflaggor),
   `scripts/ci/run_e2e_backend.py`, `tests/.../test_chat_streaming.py`,
   `lesson_conflicts.py`. Union på: `corpus_review.py`, `schemas.py`. Redan lösta:
   `agent_orchestration.py`, `routers/agents.py`, `test_agent_orchestration.py`.
4. **Stoppa vid `providers/registry.py`** och eskalera till Z.ai + grundaren. Merga inte den
   filen på egen hand.
5. **Frontend manuellt** (`chat/page.tsx`, `page.tsx`, `Sidebar.tsx`, `lib/api.ts`) med Alpha:s
   SSE-kontrakt som sanning. Kör Playwright och granska visuellt — det har inte jag kunnat.
6. **Migrera:** `alembic heads` ska ge exakt en rad (`0095`); kör `alembic upgrade head` mot
   färsk databas.
7. **Kör sviterna i avsnitt 10.** Först därefter öppnas PR mot integrationsgrenen.

## 10. Verifiering som måste köras efter merge

```
# MainAI-kärnan
pytest tests/backend/mainai/test_founder_long_horizon_workflow.py -q
pytest tests/backend/test_mainai_execution_api.py tests/backend/test_mainai_execution_planner.py -q

# Det som krockar
pytest tests/backend/test_agent_orchestration.py tests/backend/jobs/test_mainai_jobs.py -q
pytest tests/backend/test_mainai_execution_lesson_conflicts.py -q
pytest tests/backend/chat/test_chat_streaming.py -q
pytest tests/backend/providers/ -q

# Säkerhet (Alpha-sidans, får inte regrediera)
pytest tests/security/ -q
pytest tests/backend/test_migration_state_privileges.py tests/backend/test_runtime_table_privileges.py -q
```

## 11. Kvarvarande risker och NOT_RUN

| Post | Status |
|---|---|
| `providers/registry.py` egress-konflikt | **BLOCKED** — kräver Z.ai (R31) + grundarbeslut |
| `detect_conflict`-divergensen | **Öppen policyfråga** — min sida får inte vinna |
| CI på min gren | **NOT_RUN** — 0 workflow-körningar; endast Vercel-check |
| Draft-PR | **BLOCKED** — token saknar `Pull requests: write` (403) |
| Frontend/Playwright visuellt | **NOT_RUN** — Chrome saknas i denna miljö |
| 16 GB-VPS med produktionsmodell | **NOT_RUN** — denna maskin är 2 CPU/1 GB |
| `security/POLICY_LOCK.json` | Divergerar, orörd, Z.ai ansvarar |
| `first_cycle_go` | `false` — oförändrat |

## 12. SHA-referens

| Roll | SHA |
|---|---|
| Min HEAD före denna omgång | `13fc211b18123877280280c953b6cd3e54bb3b2d` |
| Föregående testbevis | `f51b9ae1` (A–G), `8976adbd` (idempotens), `40faa30f` (OSTRIDIGT) |
| Merge-base | `86671468a6dd271b61a3f985434130c4bf470ea8` |
| Alpha/VPS-bas | `b313c9df812bc780c89f34f8d0bc296e21a2d342` |
| Integrationsgren | `0adb108fa5145ad6b320950fb029bfaf96f003b4` |
| Denna omgångs commits | se `git log 13fc211b..HEAD` (två commits: kod + dokumentation) |
