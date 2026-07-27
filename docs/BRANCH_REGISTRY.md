# Branch-/PR-register — projektets levande karta

Detta är INTE bara en lista över brancher — det är projektets levande karta, och den
manuella motsvarigheten till vad MainAI själv ska kunna göra en dag (se `CLAUDE.md`s
"Målet"-avsnitt och `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`). Den ska hållas uppdaterad
varje gång en branch/PR skapas, mergas, stängs eller fryses, eller när en konflikt/risk för
dubbelarbete upptäcks — se `CLAUDE.md`s "Branch Registry"-avsnitt för när.

**Senast verifierat mot faktiskt git-/GitHub-läge:** 2026-07-27, mot GitHubs Actions-API och
PR-API direkt (`mcp__github__actions_list`/`actions_get`/`pull_request_read`, inte memorerat)
efter att Pass 6:s PR öppnades och PR #26 stängdes.

## Pass 6 (2026-07-27): MainAI Core Loop v1 — engångsundantag från per-funktion-branch-regeln,
PR #28 öppen

**Grundaren har explicit auktoriserat ett medvetet, engångsundantag** från `CLAUDE.md`s
"en funktion = en branch/PR"-grundprincip för den här uppgiften specifikt (se uppdragets egen
text) — arbete sker kontinuerligt på EN integrationsbranch med många små, logiskt separerade
commits, och EN enda PR öppnas när hela den vertikala kedjan (upload → lagring → worker →
indexering → sökning → chatt med citat → omstartsöverlevnad → providernedgradering → CI →
deploy/rollback-verifiering) är bevisligen fungerande end-to-end.

| Branch | PR | Status | Scope | Bas |
|---|---|---|---|---|
| `claude/mainai-core-loop-v1` | [#28](https://github.com/d1n095/LifeAI/pull/28) | **Öppen, väntar på grundarens granskning** — INTE mergad av den här sessionen (mänsklig granskningsgrind krävs, samma regel som alla tidigare PR:er). | MainAI Core Loop v1 — hela den vertikala kedjan upload→lagring→worker→indexering→sökning→chatt-med-citat→omstartsöverlevnad→providernedgradering→CI→deploy/rollback, verifierad med RIKTIG körning av `docker-compose.vps.yml`+`docker-compose.vps.ci.yml`-topologin på riktiga GitHub Actions-runners (körning [30304755138](https://github.com/d1n095/LifeAI/actions/runs/30304755138), attempt 2, helt grön — se PR #28:s beskrivning för fullständig punkt-för-punkt-verifiering). Lokal `docker build` av de riktiga bilderna är blockerad i den här sessionens sandlåda (nätverkspolicyn tillåter inte apt-get mot deb.debian.org), se `docs/CORE_LOOP_V1_BACKLOG.md`. Innehåller PR #26:s tidigare öppna innehåll (docs + rundtripstest), cherry-pickat och utökat med chatt-med-citat/omstartsöverlevnad/providernedgradering-steg i samma CI-jobb istället för ett nytt. PR #26 stängd med kommentar som pekar hit (ingen merge, allt innehåll bevarat). | `claude/det-kommer-mer-879lcm` @ `13a9677` (inkluderar PR #27) |

**Verifierat (Pass 6, klart):** Full lokal backend-testsvit (532 passade, 1 medvetet skippad)
körd mot riktig Postgres+Redis i denna sessions sandlåda. `vps-compose-verify`s utökade jobb
och `vps-deploy-rollback-test` kördes verkligen på GitHub Actions (inte bara lokalt) — sista
körningen (`30304755138`, attempt 2) helt grön: alla jobb success eller medvetet skippade.
Attempt 1 hade en infrastrukturflimmer (Docker Hub-timeout vid `pgvector/pgvector:pg16`-pull i
`Backend — unit/integration tests`, orelaterat till den här branchens ändringar — varje annat
jobb som pullar samma image lyckades) — löst med `rerun_failed_jobs`, grönt på omkörning. Under
arbetet avslöjades en RIKTIG bugg i omstartstestet (dockerds `restart: unless-stopped` hann
inte starta om en SIGKILLad worker inom CI:ns 30s-fönster) — fixat genom att explicit köra
`docker compose ... start worker` istället för att lita på dockerds egen timing, se commit
`4d47820`.

## Pass 5 (2026-07-27): PR #27 mergad, PR #26 väntar, storfilsimport-plan i stället för en Caddy-punktfix

| Branch | PR | Status | Scope | Bas |
|---|---|---|---|---|
| `claude/fix-uploads-volume-permission` | [#27](https://github.com/d1n095/LifeAI/pull/27) | **Mergad**, merge-commit `a3194981e4adf94bb4807660003cbb7a4200e50e` | Akut produktionsbugg: `lifeai_uploads`-volymen var root-ägd, backend/worker kör som UID 10001 — varje riktig uppladdning fick 500 (`PermissionError`). Ny `uploads-init`-engångstjänst (root ENDAST för `chown -R 10001:10001`, `cap_drop: ALL`+`cap_add: [CHOWN]`, `restart: "no"`) som `backend`/`worker` nu väntar på (`depends_on: service_completed_successfully`). Upptäckt av PR #26:s nya CI-rundtripstest, löst i en egen PR per grundarens explicita val (alternativ 2) i stället för att blandas in i #26. | `claude/det-kommer-mer-879lcm` @ cd334d1 |
| `claude/vps-embedding-worker-docs-ci` | [#26](https://github.com/d1n095/LifeAI/pull/26) | **Stängd (inte mergad)**, suppersederad av [#28](https://github.com/d1n095/LifeAI/pull/28) (Pass 6:s `claude/mainai-core-loop-v1`, se ovan) — dess innehåll är cherry-pickat dit och utökat med chatt-med-citat/omstart/providernedgradering i samma CI-jobb. Stängd med en kommentar som pekar till #28 — inget innehåll förlorat. | Docs (`.env.vps.example`, `docs/STRATO_VPS_DEPLOY.md`) + ny CI-rundtripstest (verklig worker, nätverksisolerad Ollama-stub) som bevisar embedding-provider-konfigurationen fungerar end-to-end. Blev CI-rött på exakt den `PermissionError` #27 sedan fixade. | `claude/det-kommer-mer-879lcm` @ cd334d1 (föråldrad — #27 mergad sedan dess) |
| `claude/fix-caddy-upload-body-limit` | — (öppnades aldrig) | **Övergiven, aldrig committad/pushad** — inga commits fanns när branchen togs bort lokalt | Skulle bara höjt Caddys `request_body max_size` 30→65-70 MB för att synka med backendens 60 MB-gräns. Stoppad av grundaren INNAN PR öppnades: de verkliga produktionsfilerna är ~1,3 GB, så 60 MB är inte det arkitektoniska målet — en punktfix hade bara flyttat felet till workern, som läser hela originalfilet till minnet (`library_import.py:574-575`, `raw = f.read()`) i en container med `mem_limit: 384m`. Ersatt av `docs/LARGE_FILE_UPLOAD_PLAN.md` — en fullständig scoped plan för säker storfilsimport (mål ≥2 GB), som måste granskas och brytas ned i PR:er (se dokumentets §3) INNAN någon gräns höjs. **2026-07-27, korrigeringsrunda:** planens första version hade sex tekniska fel (ZIP påstods strömma men buffrar fortfarande varje entry som `bytes` via `_read_with_hard_cap()`s `chunks: list[bytes]`; PR-ordningen exponerade en obegränsad uppladdningsväg före workerns minnesfix; Caddy antogs behöva höjas utan grund; svag concurrency-design för del-mottagning; otillräcklig teststrategi med sparse-nollfiler; för starkt påstående om minnesoberoende) — samtliga korrigerade i dokumentets §0. Ingen kod ändrad i korrigeringen, bara dokumentet. | `claude/det-kommer-mer-879lcm` @ a319498 (aldrig pushad) |

**Uppdaterad rekommenderad ordning (efter Pass 6):** #26 är nu stängd, suppersederad av #28 —
se Pass 6-avsnittet ovan för fullständig status. Storfilsimport-planens PR-kedja (A–G, se
`docs/LARGE_FILE_UPLOAD_PLAN.md`) är ett helt separat, större spår och ska INTE byggas före
en genomgång/godkännande av planen själv.

## Pass 4 (2026-07-27): säkerhetsincidenter + chat context-status awareness

Samma dag som Pass 3 slutade (PR #16 mergad som `502b082`), en snabb sekvens av verifierade
produktionsincidenter, var och en löst i en egen branch/PR från huvudgrenens då-aktuella tip
(inte i förväg, se Merge-regeln):

| Branch | PR | Status | Scope | Bas |
|---|---|---|---|---|
| `claude/reject-placeholder-secrets` | [#22](https://github.com/d1n095/LifeAI/pull/22) | **Mergad**, merge-commit `33c316b` | Säkerhetsincident: läckta SMTP/Redis-hemligheter + Gemini-platshållarbugg. `looks_like_placeholder_secret()` i alla 5 providrars `is_configured()`, `check_no_duplicate_env_keys()` i `deploy.sh`/CI, rotationsrunbook i `docs/VPS_OPERATIONS_RUNBOOK.md` | `claude/det-kommer-mer-879lcm` @ 502b082 |
| `claude/gemini-header-auth-security-fix` | [#23](https://github.com/d1n095/LifeAI/pull/23) | **Mergad**, merge-commit `5ab6e81` | Säkerhetsincident: Gemini-nyckeln skickades som `?key=...` URL-query och läckte via `httpx.HTTPStatusError` in i Docker-loggar. Flyttad till `x-goog-api-key`-header; `chat_with_fallback()` loggar nu alltid via `classify_provider_exception()` | `claude/det-kommer-mer-879lcm` @ 33c316b |
| `claude/gemini-diagnostic-logging` | [#24](https://github.com/d1n095/LifeAI/pull/24) | **Mergad**, merge-commit `b0481c1` | Fortsatt 404-utredning efter header-fixen: `_normalize_model()` (Compose stripper inte citattecken), enhetlig URL-byggare, Googles egna saniterade felmeddelande ytligt via `ProviderError.category`, `classify_provider_exception()` litar nu på ett redan satt `category` | `claude/det-kommer-mer-879lcm` @ 5ab6e81 |
| `claude/chat-context-status-awareness` | [#25](https://github.com/d1n095/LifeAI/pull/25) | **Mergad**, merge-commit `cd334d105012a4f26f3b9a81fa9beb20fe471e00`, driftsatt och verifierad i produktion | Bekräftad produktionsincident: chat kollapsade varje nollträff-tillstånd (worker nere, filer under bearbetning, saknad embedding-leverantör, indexeringsfel, sökfel just den frågan, genuint ingen träff, inga uppladdade filer) till samma fasta sträng "Ingen relevant kunskap hittades." Ny `app/rag/context_status.py` klassificerar den verkliga orsaken från redan existerande signaler (IndexStatus, worker-heartbeat, `classify_provider_exception`) — strukturerad `context_status` på `ChatMessageOut`, renderad i chat-UI:t. 7 nya regressionstester. Se PR-beskrivningen för fullständig svarsform. Kopiera-knapp/meddelandeåtgärder medvetet UTANFÖR scope — egen, separat uppföljande PR. | `claude/det-kommer-mer-879lcm` @ b0481c1 |

Även upptäckt och åtgärdat under samma pass, inte en egen branch (för litet för en egen PR,
men värt att notera här så det inte glöms): `chat.py`s embedding-provider-catch fångade bara
`ProviderError`, inte ett rått `httpx.HTTPError` — en konfigurerad-men-ogiltig nyckel kunde ge
en ohanterad 500. Fixat som en del av PR #25 (samma commit, samma test), inte en separat PR,
eftersom det är samma kodrad som ändå ändrades för context-status-syftet.

**GitHub-nyckelrotation:** grundaren skapade en ny Gemini-nyckel (även den `AQ.`-prefixad, som
är normalt) men installerar den avsiktligt inte förrän PR #24:s bild är driftsatt — se PR
#24:s incidentbeskrivning. SMTP/Redis-hemligheterna som exponerades innan PR #22 kräver
fortfarande rotation på grundarens faktiska produktions-VPS (operativt, inte kod — runbook
finns i `docs/VPS_OPERATIONS_RUNBOOK.md`).

## Pass 3 (2026-07-26): PR #13 mergad, MainAI Core-orkestrering påbörjad

9. **PR #13** (MainAI Project Memory & Coordination Loop, Fas 1–4) — 18/18 CI grönt, markerad
   ready-for-review och **mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `7afb01f`.
   Detta är första gången huvudgrenen innehåller MainAI:s eget projektminne (`project_notes`,
   `project_checkpoints`, `project_sources`, `project_branch_pr_status`) och den founder-only
   `/admin/memory`-vyn.
10. Grundaren utökade uppdraget samma dag från "bygg ett checkpointsystem" till "bygg första
    operativa kärnan av MainAI" — se `CLAUDE.md`s "MainAI Core"-riktning: konversations-/
    kunskapsretrieval, en lätt systemkarta, agentorkestrering (kod-/granskningsagent via
    befintliga provider-adaptrar), och en GitHub-integration med verklig skriv-behörighet
    bakom hårda säkerhetsgrindar (ingen merge-kapacitet implementerad alls i denna fas).
11. **`claude/mainai-core-orchestration-v1`** grenad direkt från huvudgrenens nya tip
    (`7afb01f`, alltså EFTER att PR #13 mergat — inte i förväg, se Merge-regeln). Bygger:
    migration 0016 (`agent_tasks`/`agent_task_events`), `app/agent_orchestration.py`,
    `app/integrations/github_client.py` (read/branch/commit/PR — medvetet ingen merge-metod),
    `retrieve_relevant_context()`/systemkarta i `app/project_memory.py`, ny `NoteKind.idea`,
    founder-UI `/admin/agents`. Se separat sektion nedan för scope och verifiering.

## Mergekedjan 2026-07-26 — genomförd i sin helhet

**Pass 1 (fristående fixar + processdokumentation):**
1. **PR #9** (`next` 16.2.10→16.2.11) → mergad, merge-commit `0081e562`.
2. **PR #11** (`brace-expansion`/GHSA-mh99-v99m-4gvg, ny CI-allowlist) → mergad, merge-commit
   `6929b700`. Se `docs/SECURITY_BLOCKERS.md` punkt 3.
3. **PR #10** (`CLAUDE.md` + den här filen) → mergad, merge-commit `403adc06`.

**Pass 2 (P1/P2-integration i huvudkedjan — grundarens explicita mandat):**
4. **PR #7** (P1) mergad i sin bas `claude/life-library-durable-worker-merged`
   (merge-commit `16959661`) — det steget skedde redan i pass 1:s förlängning.
5. `claude/life-library-durable-worker-merged` synkades mot huvudgrenens allra senaste tip
   (`5769cffa`, inkl. PR #12:s registeruppdatering) — ren merge, inga konflikter, ny tip
   `aa4d4b9`.
6. **PR #14** (ny, "Integrate Life Library durable worker + P1 into the main line") —
   `head: claude/life-library-durable-worker-merged` → `base: claude/det-kommer-mer-879lcm`.
   Innehåller BÅDE PR #6:s tidigare aldrig-mergade durable-worker-paket OCH P1, eftersom P1
   byggde direkt ovanpå PR #6:s bas-snapshot och de aldrig kan separeras utan
   historieomskrivning (uttryckligen undvikt). 18/18 CI grönt, `mergeable_state: clean`,
   **mergad** — merge-commit `2ddfeddc`. **Detta är första gången huvudgrenen någonsin
   innehållit P1 (eller PR #6).**
7. **PR #8** (P2) — verifierat via `git merge-base` att `909a5f1` (P1:s innehåll) nu är
   ancestor till den nya huvudgrenen, alltså att en ombasering INTE skulle ändra diffen.
   Bas ombasearad direkt till `claude/det-kommer-mer-879lcm` (utan mellansteg), diff
   bekräftat oförändrat (7 filer, +1225/-63), samma redan gröna CI-körning (huvudet
   oförändrat, `mergeable_state: clean`). **Mergad** — merge-commit `89682a18`.
8. **PR #13** (MainAI Project Memory-loopen, se separat sektion nedan) — bas ombasearad från
   `claude/p2-zip-hardening-plan` direkt till `claude/det-kommer-mer-879lcm` på samma sätt
   (verifierat via `git merge-base`, diff oförändrat: 8 filer, +835/-1). Fortsatt draft,
   fortsatt öppen.
9. Full lokal verifiering körd på den fullständigt integrerade koden (real Postgres+Redis i
   Docker): 411/411 tester gröna (346 backend + 65 security/account), inga regressioner.

P7A rördes inte — fortsatt fryst, se eget avsnitt nedan om dess nu ytterligare föråldrade bas.

## Huvudkedjans nuläge (efter PR #13)

```
claude/det-kommer-mer-879lcm (huvudgren, tip 7afb01f)
  innehåller nu: PR #9, #11, #10, #12, PR #14 (durable worker + P1), PR #8 (P2), PR #13
  (MainAI Project Memory & Coordination Loop, Fas 1-4)
  └─ claude/mainai-core-orchestration-v1 — MainAI Core-orkestrering, öppen (ingen PR
       skapad än vid senaste verifiering), @ se lokal branch
       (grenad EFTER PR #13:s merge, inte i förväg — se Merge-regeln)

claude/p7a-governance-ingestion-plan — FRYST, INGEN PR, @ df597f2
  (grenad från en nu mycket föråldrad P2-tip, 15487e2 — inte 7afb01f)
```

**`claude/life-library-durable-worker-merged`, `claude/founder-knowledge-studio-v1`,
`claude/p2-zip-hardening-plan` och `claude/mainai-memory-loop-v1` är nu subsumerade** —
deras innehåll finns i huvudgrenen via PR #14/#8/#13. Brancharna själva kan städas när
grundaren bekräftar (inte gjort automatiskt, se säkerhetsprotokollet mot destruktiva
åtgärder).

## Fristående, orelaterade fixar (grenade direkt från huvudgrenen)

Dessa rör INTE P1/P2/P7A-kedjan och ska inte blandas in i den — se `CLAUDE.md`s
grundprincip för varför de fick egna brancher/PR:er istället för att fogas in i en pågående.

| Branch | PR | Status | Scope | Bas |
|---|---|---|---|---|
| `claude/frontend-npm-audit-next-16-2-11` | [#9](https://github.com/d1n095/LifeAI/pull/9) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `0081e562` | `next` 16.2.10 → 16.2.11 (stänger `npm audit --audit-level=high`, 9 säkerhetsfixar, inga brytande ändringar) | `claude/det-kommer-mer-879lcm` @ a141065 |
| `claude/frontend-npm-audit-brace-expansion` | [#11](https://github.com/d1n095/LifeAI/pull/11) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `6929b700` | `brace-expansion`/GHSA-mh99-v99m-4gvg — daterad, ID-specifik CI-allowlist (`frontend/scripts/check-npm-audit.js`), se `docs/SECURITY_BLOCKERS.md` punkt 3 | `claude/det-kommer-mer-879lcm` @ 0081e562 (efter PR #9) |
| `claude/development-workflow-principles` | [#10](https://github.com/d1n095/LifeAI/pull/10) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `403adc06` | `CLAUDE.md` + den här filen — arbetsprinciper, inget applikationskod | `claude/det-kommer-mer-879lcm` @ 6929b700 (efter PR #11) |
| `claude/branch-registry-post-merge-chain-update` | [#12](https://github.com/d1n095/LifeAI/pull/12) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `5769cffa` | Registerpost-mergekedja | `claude/det-kommer-mer-879lcm` @ 403adc06 |
| `claude/life-library-durable-worker-merged` | [#14](https://github.com/d1n095/LifeAI/pull/14) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `2ddfeddc` | PR #6 (durable worker/lagring) + P1 (provider-verifiering) — landade i huvudgrenen för första gången | `claude/det-kommer-mer-879lcm` @ 5769cffa |
| `claude/p2-zip-hardening-plan` | [#8](https://github.com/d1n095/LifeAI/pull/8) | **Mergad** i `claude/det-kommer-mer-879lcm` (ombasearad dit efter PR #14), merge-commit `89682a18` | P2: nästlad ZIP-hantering, `encrypted`-status, `archive_path`/`archive_chain` | `claude/det-kommer-mer-879lcm` @ 2ddfeddc (efter PR #14) |
| `claude/mainai-memory-loop-v1` | [#13](https://github.com/d1n095/LifeAI/pull/13) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `7afb01f`. 18/18 CI grönt. | MainAI Project Memory & Coordination Loop, Fas 1–4: `project_notes`/`project_checkpoints`/`project_sources`/`project_branch_pr_status`, resumption-brief, founder-UI `/admin/memory` | `claude/det-kommer-mer-879lcm` @ 89682a18 |
| `claude/chat-message-persistence-fix` | [#17](https://github.com/d1n095/LifeAI/pull/17) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `8dcf8161`. 18/18 CI grönt före merge, `mergeable_state: clean`. | PR A (LLM Coupling & Failure-Boundary Audit): användarmeddelandet persisteras och committas OBEROENDE av providerkallet; ny `MessageStatus`/`in_reply_to_id`/`error_category` på `Message`, migration `0016_chat_message_status`; `POST /messages/{id}/retry`; `ChatMessageOut` skiljer explicit `user_message_saved` från `assistant_status`. Se sektionen nedan för fullständig audit-bakgrund. | `claude/det-kommer-mer-879lcm` @ 7afb01f |
| `claude/search-embedding-failure-fallback` | [#18](https://github.com/d1n095/LifeAI/pull/18) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `f250e728`. 18/18 CI grönt före merge, `mergeable_state: clean`. | PR B: `hybrid_search()` accepterar `vector: list[float] \| None`, hoppar över den semantiska kanalen (inte en fejkad nollvektor) när providern saknas. Ny `LibrarySearchResponseOut` med `semantic_search_available`/`degraded_reason`. | `claude/det-kommer-mer-879lcm` @ 7afb01f |
| `claude/mainai-local-first-principle` | [#19](https://github.com/d1n095/LifeAI/pull/19) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `ce432613`. CI grönt före merge, `mergeable_state: clean`. | Grundprincip "MainAI är systemets intelligens, inte en extern tjänst" i `docs/MAINAI_ARCHITECTURE.md` §1, PR C:s stängningsbeslut (se nedan). | `claude/det-kommer-mer-879lcm` @ 7afb01f |
| `claude/mainai-core-orchestration-v1` | [#16](https://github.com/d1n095/LifeAI/pull/16) | **Öppen, draft — under granskning.** Efter att PR #17/#18/#19 mergades blev `mergeable_state: dirty` (migration 0016 kolliderade — se nedan), åtgärdat genom att döpa om `0016_agent_orchestration.py` → `0017_agent_orchestration.py` (`down_revision` uppdaterad till PR #17:s `0016_chat_message_status`) och en `git merge` av den nya huvudgrenens tip in i branchen (`schemas.py`/`lib/api.ts` auto-mergade konfliktfritt, bara den här filen krockade). Full lokal verifiering omkörs efter mergen innan draft-status lyfts. | MainAI Core v0: kategoriserad retrieval + systemkarta, agentorkestrering (`agent_tasks`/`agent_task_events`, migration **0017** efter omnumrering), minimal GitHub-klient (läsning/branch/commit/PR, ALDRIG merge i klienten), founder-UI `/admin/agents`. Se separat scope-sektion nedan. Dispatch returnerar en säker, icke-läckande 503 om samtliga providers misslyckas. | `claude/det-kommer-mer-879lcm` @ ce432613 (efter merge av PR #17/#18/#19) |

## MainAI Core: agentorkestrering (`claude/mainai-core-orchestration-v1`, PR #16) — scope och verifiering

Bygger vidare på Fas 1–4 (nu i huvudgrenen), enligt CLAUDE.md's 2026-07-26 "MainAI Core"-
riktning: inte bara ett checkpointsystem, utan första vertikala kedjan samtal → minne →
helhetsbild → problem → agentuppdrag → kod → granskning → GitHub-PR → checkpoint.

**Byggt i denna omgång:**
- `retrieve_relevant_context()` — kategoriserad, relevans-rankad hämtning (heuristisk
  token-overlap, samma metod som `detect_conflicts_and_duplicates()` redan använder) i
  stället för att dumpa hela minnet. Skiljer uttryckligen `verifierade_fakta_och_status` /
  `grundarens_beslut` / `ej_beslutade_ideer` (ny `NoteKind.idea`) / `blockerare` /
  `nasta_steg` / `osakra_eller_motstridiga` / `historik`.
- `build_system_map()`/`ingest_system_map()` — lätt, textbaserad skanning av routers/
  modeller/migrationer/frontend-routes under `PROJECT_ROOT`, lagrad via samma
  content-addressed storage som allt annat i modulen (`ProjectSource(source_type="system_map")`).
  Medvetet smal denna omgång — dupliceras inte mot redan befintlig admin-status
  (`/api/admin/library/ops`, `/api/admin/providers`).
- Migration **0017** (omnumrerad från 0016 efter att PR #17:s `0016_chat_message_status`
  mergades först — se raden ovan): `agent_tasks` (ett avgränsat uppdrag — titel, filer,
  begränsningar, acceptanskriterier, krävda tester) + `agent_task_events` (append-only
  historik: dispatch, resultat, testresultat, granskning, GitHub-operationer).
- `app/agent_orchestration.py` — `create_agent_task`/`dispatch_task` (kodagent via befintlig
  `chat_with_fallback`)/`record_test_results`/`review_task` (granskningsagent, BLOCKERAD utan
  registrerade testresultat, kan aldrig godkänna på röda tester även om modellen säger
  "approved")/`prepare_github_pr`/`attempt_auto_merge` (ALLTID blockerad — se nedan). Dispatch
  fångar nu `ProviderError` och returnerar en fast, icke-läckande 503 istället för en ohanterad
  500 — se modulens "Local-first status"-avsnitt för samma Idag/Målarkitektur-uppdelning som
  `docs/MAINAI_ARCHITECTURE.md` §1:s grundprincip.
- `app/integrations/github_client.py` — minimal REST-klient (läsning, branch, commit, PR).
  **Ingen merge-metod finns i klienten alls** — inte bara avstängd bakom en flagga, genuint
  frånvarande som kod. `github_write_enabled` (default `False`) styr om `prepare_github_pr()`
  bara FÖRESLÅR exakt PR-innehåll (branch/commit/PR-text, ingen GitHub-anrop) eller faktiskt
  skapar branch/commit/PR. `github_auto_merge_enabled` finns som konfigurationsflagga men
  gatear ingen faktisk kapacitet ännu.
- Founder-UI `/admin/agents` — uppdragslista, detaljvy med händelsehistorik, knappar för
  varje steg i kedjan. Verifierat i riktig Chromium-webbläsare mot en riktig backend: login,
  uppdragsskapande, dispatch (korrekt, ren felyta när ingen provider-nyckel är konfigurerad —
  exakt samma beteende som resten av appen), och det alltid-blockerade merge-försöket.

**Explicit avgränsat bort denna omgång** (registrerat, inte bortglömt):
- Verklig GitHub-skrivbehörighet är byggd och testad (mockad HTTP-nivå) men aldrig körd mot
  ett riktigt repo i denna session — kräver en riktig `GITHUB_TOKEN` som grundaren
  provisionerar separat (samma mönster som providernycklarna, aldrig i chatten).
- Semantisk (embedding-baserad) retrieval — nuvarande implementation är medvetet en
  heuristik, inte en ny vektorpipeline för noteringar.
- Hela "Autonomous Verification & Interaction Layer" (webbläsarstyrd funktionstestning,
  persona-simulering, digital tvilling av grundaren) — en betydligt större, separat
  initiativ som inte påbörjats.

**Verifiering:** 15 nya tester i `test_agent_orchestration.py` (inkl. ett fullständigt
vertikalt bevis: note → task → dispatch → test → review → PR-förslag → checkpoint → kall
läsning, samt en test för den icke-läckande 503:an på total providerkollaps), 5 nya i
`test_project_memory.py` (retrieval + systemkarta). Full befintlig svit senast omkörd (före
denna merge): 455 gröna, 1 medvetet skippad, inga regressioner — omkörs igen efter mergen och
migrationsomnumreringen innan draft-status lyfts. Migrationsrundtripp verifieras på nytt mot
den förlängda kedjan (…→0016→0017→nedgradering×2→uppgradering). Frontend: `tsc`/`eslint`/
`next build` gröna, UI verifierad i riktig webbläsare mot riktig backend+Postgres.

## LLM Coupling & Failure-Boundary Audit — genomförd (2026-07-26)

En extern audit av grundaren identifierade två verkliga fel — inte hypotetiska — där ett
AI-providerfel kunde få oavsiktliga konsekvenser för icke-AI-funktionalitet: (1) chatt
tappade det redan sparade användarmeddelandet om providern misslyckades efteråt, (2)
biblioteks-sökningen 500:ade helt om embedding-providern var otillgänglig trots att dess
textmatchningskanal inte behöver någon provider alls. Grundaren godkände audit-splitten och
skärpte båda kraven: PR A måste skilja explicit mellan "meddelande sparat" och "AI-svar
misslyckades" i kontraktet (inte en slentrianmässig 200:a), och PR B måste ha en RIKTIG
lokal fallback (inte bara fånga felet och returnera tomt). PR A (#17), PR B (#18) och
principdokumentationen (#19) är nu alla mergade i huvudgrenen. Minimal dispatch-fix i PR #16
är byggd och pushad (se sektionen ovan) — kvar är att slutföra PR #16:s granskning och merga
den.

**PR C stängd, inte byggd — verifierat inte längre relevant.** Grundaren godkände "Skip PR C"
med ett uttryckligt tilläggskrav: verifiera först att `/api/documents/upload` verkligen saknar
kvarvarande konsumenter innan något stängs eller städas. Verifiering genomförd:

- `POST /api/documents/upload` har INGEN frontend-anropare kvar. `frontend/lib/api.ts`s
  `uploadDocument()`-funktion existerar men anropas ingenstans — `app/(shell)/documents/page.tsx`
  gör bara `router.replace("/library")` sedan commit `0d9f487` ("Life Library: single upload
  hub...", 2026-07-22). All riktig uppladdning går via `/api/library`s importpipeline, som
  redan har full `ImportJob`-baserad status, säker felklassificering och en riktig retry-
  åtgärd (byggt i P1/P2/STEG-arbetet, långt mer robust än vad PR C skulle byggt).
- Backend-sidans felhantering som PR C skulle lagt till **finns redan** för `/documents/upload`-
  vägen: `app/rag/ingest.py`s `index_document()` sätter redan distinkta `IndexStatus`-värden
  (`extraction_failed`/`awaiting_provider`/`blocked_provider`/`indexing_failed`/`failed`) och
  använder redan `classify_provider_exception()` — aldrig rå `str(exc)` — för varje
  felläge, inklusive embedding-providerfel EFTER en godkänd pre-flight-kontroll. Detta byggdes
  redan under P1, innan den här auditen. En `reindex_document_id()`-retry-funktion finns redan
  skriven men anropas ingenstans (orphaned).
- Enda verkliga luckan (`DocumentOut` exponerar inte `status`/`error_message`, ingen
  `/retry`-rutt) gäller alltså en väg utan någon aktiv UI-konsument — att bygga den skulle
  vara arbete för en död kodväg, inte en verklig felyta en grundare kan träffa på.
- `frontend/e2e/shell-pages.spec.ts`s "documents: empty state..."-test refererar fortfarande
  den gamla `/documents`-sidans UI-text/knappar (från commit `a46dc7a`, FÖRE
  konsolideringen) och skulle idag fela mot verklig kod — men det upptäcks aldrig, eftersom
  `.github/workflows/ci.yml`s "E2E — Playwright (full stack)"-jobb explicit bara kör
  `e2e/auth.spec.ts e2e/security.spec.ts e2e/account.spec.ts`. Filen är alltså redan
  exkluderad ur CI, inte bara föråldrad.

**Ny uppföljningsuppgift (inte byggd nu, se grundarens explicita instruktion att inte utöka
en död kodväg):** en separat städ-branch/PR som tar bort `POST /api/documents/upload` och dess
bakgrundsindexering (`_index_in_background`, den orphanade `reindex_document_id()`), tar bort
eller uppdaterar det föråldrade `frontend/e2e/shell-pages.spec.ts`, och rättar
`docs/MAINAI_ARCHITECTURE.md` rad 303 (§5, som fortfarande beskriver `/api/documents/upload`
som det aktiva uppladdningsflödet) — `GET /api/documents` och `DELETE /api/documents/{id}`
ska sannolikt vara kvar (fortsatt bakåtkompatibel läsning/borttagning av samma delade
`documents`-tabell, se `test_a_source_deleted_via_library_disappears_from_the_older_documents_router_too`).
Detta är EN ny, ren "ta bort död kod"-uppgift — inte en utökning av PR C:s ursprungliga scope.

## Stängda utan merge (subsumerade, inte relevanta att slå ihop)

| PR | Branch | Status | Anledning |
|---|---|---|---|
| [#4](https://github.com/d1n095/LifeAI/pull/4) | `claude/founder-knowledge-studio-v1` | **Stängd** (inte mergad) | `git merge-base --is-ancestor 909a5f1 origin/claude/det-kommer-mer-879lcm` = sant — hela innehållet landade redan via PR #14. Stale bas (`claude/night-shift-mainai-web`). |
| [#6](https://github.com/d1n095/LifeAI/pull/6) | `claude/life-library-durable-worker` | **Stängd** (inte mergad) | `git merge-base --is-ancestor a6d16b5 origin/claude/det-kommer-mer-879lcm` = sant — hela innehållet landade redan via PR #14. Stale bas (`claude/life-library-upload-queue`). |

## Merge-regeln (se `CLAUDE.md`)

**Ingen branch rebasas eller uppdateras i förväg "för säkerhets skull".** Rebase/"Update
branch" sker FÖRST när branchens faktiska beroende faktiskt har mergats, aldrig tidigare.
Avsnitten nedan skiljer uttryckligen på "väntar på ett beroende" (rör INTE branchen än) och
"kan mergas oberoende" (ingen väntan alls) — de är inte samma sak.

## Rekommenderad merge-ordning (nuläge)

1. ~~PR #9~~, ~~#11~~, ~~#10~~, ~~#7~~, ~~#8~~, ~~#14~~, ~~#13~~, ~~#16~~, ~~#17~~, ~~#18~~,
   ~~#19~~, ~~#22~~, ~~#23~~, ~~#24~~, ~~#25~~, ~~#27~~ — samtliga mergade i huvudgrenen (se
   Pass 4/5-avsnitten ovan). ~~PR #4~~, ~~PR #6~~, ~~PR #26~~ stängda utan merge (#4/#6
   subsumerade, se ovan; #26 suppersederad av #28, se Pass 6).
2. **PR #28** (MainAI Core Loop v1) — öppen, grenad från huvudgrenens tip (`13a9677`, inkl.
   PR #27). Fullständigt grön på GitHub Actions (körning `30304755138`, attempt 2) inkl.
   `vps-compose-verify` och `vps-deploy-rollback-test`; väntar på grundarens granskning.
3. ~~PR C~~ — stängd, inte byggd. Se "LLM Coupling & Failure-Boundary Audit"-sektionen ovan
   för verifiering och den nya, separata "ta bort död kod"-uppföljningsuppgiften.
4. **P7A** → implementation kan börja på `claude/p7a-governance-ingestion-plan` FÖRST efter
   ett separat, uttryckligt beslut (branchen är fryst). Kräver DESSUTOM en egen ombasering
   mot huvudgrenens nya tip innan aktivering — dess bas (`15487e2`) är nu långt bakom både
   P2:s slutliga tip och själva huvudgrenen.

## Vilka brancher blockerar andra

- **Inget öppet PR blockerar ett annat öppet PR just nu.** PR #28 är den enda öppna branchen,
  fristående ovanpå huvudgrenen (PR #25 mergad, se Pass 4/5; PR #26 stängd, suppersederad av
  #28).
- **P7A:s egen aktivering blockeras** av både ett uttryckligt beslut och en ombasering (se
  ovan) — inte av något annat öppet PR.

## Vilka brancher kan mergas oberoende

**PR #28** kan mergas oberoende av allt annat öppet just nu (det finns inget annat öppet PR) —
den är grenad direkt från huvudgrenens tip och rör bara `.github/workflows/ci.yml`,
`docker-compose.vps.ci.yml`, `.env.vps.example`, `docs/STRATO_VPS_DEPLOY.md`,
`docs/BRANCH_REGISTRY.md`, `docs/CORE_LOOP_V1_BACKLOG.md` (ny fil) och
`scripts/vps/ci_provider_stub.py` (ny fil, döpt om från `ci_embedding_stub.py`) — ingen
ändring i applikationskoden (`backend/app`, `frontend/`) alls, bara CI/docs/scripts.

## Vilka brancher väntar på ett beroende innan de bör uppdateras

Enligt Merge-regeln — dessa ska INTE röras förrän beroendet faktiskt är mergat, inte i
förväg:

- **P7A** väntar på ett separat, uttryckligt beslut om att börja implementation, plus sin
  egen ombasering när det beslutet tas — inte på något annat öppet PR just nu.

## Konflikter

**Löst 2026-07-26: migrationsnummer-krock mellan PR #16 och PR #17.** Båda branchades från
samma tip (`7afb01f`) och skapade oberoende av varandra en `0016_*.py`-migration —
`0016_agent_orchestration.py` (PR #16) och `0016_chat_message_status.py` (PR #17), båda med
`down_revision = "0015"`. Väntat och redan flaggat i förväg (se tidigare passets anteckning
om att detta skulle lösas "när PR #16 rebasas efter PR A/B merge, inte i förväg"). När PR #17
mergades blev PR #16:s `mergeable_state: dirty`. Löst genom att döpa om PR #16:s fil till
`0017_agent_orchestration.py` och sätta `revision = "0017"`, `down_revision = "0016"` (pekar
nu på PR #17:s migration), plus en vanlig `git merge` av huvudgrenens nya tip in i PR #16:s
branch (`schemas.py`/`frontend/lib/api.ts` auto-mergade konfliktfritt eftersom PR #16/#17/#18
lägger till olika, icke-överlappande klasser i samma filer; bara den här filen — som båda
sidor redigerat samtidigt — krockade textuellt). Migrationsrundtripp och full testsvit
verifieras på nytt efter denna ändring innan PR #16 mergas.

Utöver detta: inga andra kända filkonflikter. Samtliga integrationssteg (branch-synk, PR #14,
PR #8:s ombasering, PR #13:s ombasering) verifierades konfliktfria via `git merge-base` innan
de utfördes — se "Mergekedjan"-sektionen ovan för detaljer per steg.

Om en verklig filkonflikt upptäcks i framtiden ska den listas här explicit — vilka brancher,
vilka filer, och vilken lösning som föreslås — inte bara upptäckas i förbigående när en merge
misslyckas.

## Risk för dubbelarbete

Ingen känd, aktiv risk för dubbelarbete just nu. P7A:s bas är föråldrad (se ovan) men
branchen är fryst, så ingen aktiv utvecklingsrisk finns förrän ett beslut tas att återuppta
den — vid det laget måste den ombaseras mot huvudgrenens då-aktuella tip, inte mot P2:s
gamla tip.

Innan en ny branch/implementation påbörjas: jämför dess tilltänkta scope mot ALLA rader i
tabellerna ovan, inte bara den senaste. Om något överlappar, uppdatera det här avsnittet
INNAN arbetet påbörjas, inte efteråt.

## Stale/redan sammanslagna brancher (kandidater för städning, INTE raderade)

Verifierat via `git merge-base --is-ancestor` mot `claude/det-kommer-mer-879lcm` — dessa är
redan fullt innehållna i huvudgrenen. Listade här som referens, inte raderade utan explicit
tillåtelse (destruktiv åtgärd, se säkerhetsprotokollet):

`claude/fix-entrypoint-startup-race`, `claude/fix-pooler-auth-hardening`,
`claude/fix-render-public-port`, `claude/fix-supabase-pooler-role`,
`claude/fix-supabase-pooler-tenant-suffix`, `claude/founder-only-launch`,
`claude/integrate-founder-vps`, `claude/mainai-architecture-designs`,
`claude/night-shift-mainai-web`, `claude/render-service-name-fix`,
`claude/strato-vps-prep`, `claude/verify-combined-container`,
`claude/frontend-npm-audit-next-16-2-11` (PR #9, mergad),
`claude/frontend-npm-audit-brace-expansion` (PR #11, mergad),
`claude/development-workflow-principles` (PR #10, mergad),
`claude/life-library-durable-worker-merged` (PR #14, mergad — bar PR #6 + P1 in i huvudgrenen),
`claude/founder-knowledge-studio-v1` (PR #7's head, subsumerad via PR #14),
`claude/p2-zip-hardening-plan` (PR #8, mergad),
`claude/mainai-memory-loop-v1` (PR #13, mergad).

## Subsumerade i den aktiva kedjan (inte längre fristående)

Dessa branchars innehåll finns redan helt inom `claude/founder-knowledge-studio-v1` (P1)
högre upp i kedjan — de behöver inget eget beslut, bara noteras som vad de blev:

- `claude/life-library-upload-queue` → innehåll i → `claude/life-library-durable-worker` →
  innehåll i → `claude/life-library-durable-worker-merged` (PR #7:s bas-snapshot).

## Orphaned — kräver ett beslut, inte del av någon aktiv kedja

- `claude/fkp-v1.1` — 1 commit ("FKP v1.1: docs-only korrigering och integration av
  review-overlay + samtalsregister"), varken mergad i huvudgrenen eller del av P1/P2/P7A-
  kedjan. Status okänd tills grundaren tar ställning: merga, revidera, eller överge.

## Ej fullständigt granskade

Denna lista byggdes från en snabb `git merge-base`-genomgång av samtliga fjärrbranchar vid
tillfället ovan — den täcker ANCESTRY (är X en förfader till Y), inte innehållet i varje
branch i detalj. Om en branch saknas här, eller om något ser fel ut, uppdatera det här
dokumentet efter verifiering — gissa inte.
