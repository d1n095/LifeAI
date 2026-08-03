# Branch-/PR-register — projektets levande karta

Detta är INTE bara en lista över brancher — det är projektets levande karta, och den
manuella motsvarigheten till vad MainAI själv ska kunna göra en dag (se `CLAUDE.md`s
"Målet"-avsnitt och `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`). Den ska hållas uppdaterad
varje gång en branch/PR skapas, mergas, stängs eller fryses, eller när en konflikt/risk för
dubbelarbete upptäcks — se `CLAUDE.md`s "Branch Registry"-avsnitt för när.

**Senast verifierat mot faktiskt git-/GitHub-läge:** 2026-08-04 (Pass 14) — ny branch
`claude/mainai-job-runtime-foundation` skapad, pushad (7 commits, head `238aff5`), ingen PR
öppnad än (väntar på grundarens granskning). Dessförinnan 2026-07-29, mot GitHubs PR-API direkt
(`mcp__github__pull_request_read`/`update_pull_request`/`merge_pull_request`, inte memorerat)
— **PR #29 mergad** som `0bdf03d`, verifierad grön (18/18 checkar) på exakt head-SHA `df9e9c8`
innan merge, inte en äldre commit. **PR #30 mergad** som `9b15840` in i
`claude/det-kommer-mer-879lcm` — verifierad grön (18/18 checkar, "All required checks passed")
på exakt head-SHA `b2347e4` (PR-branchens sista commit) direkt innan merge, samma disciplin
som PR #29. `claude/memory-source-unit-design` är nu mergad och kan städas bort (branchen har
inga oavslutade delar kvar — hela dess innehåll är designdokumentation som nu lever i
`docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`s §4.8 på huvudgrenen). §4.8 är den kanoniska,
GODKÄNDA arkitekturen för `MemorySourceUnit`/S1A — inget ytterligare designbeslut krävs innan
en S1A-implementations-PR (migration + kod) öppnas — se §4.8:s "Status: PR #30 kontra
S1A-implementations-PR:n" för den exakta listan (produktionsdataprofil, migrationsfil,
`apply_runtime_privileges`, `app/rls.py`, delad `purge_source()`, kontoradering/export,
testmatris) på vad som krävs för att MERGA den kommande implementations-PR:n.

## Pass 14 (2026-08-03/04): MainAI Runtime Truthfulness and Durable Job Foundation — ny branch, byggd medan grundaren sov

Grundaren gav en uttrycklig, avgränsad instruktion att bygga en helt ny grund, INTE en
dokumentkontroll: "Do not stop after planning. Implement the foundation." Skapad som en helt
ny, oberoende branch — **`claude/mainai-job-runtime-foundation`**, grenad från
`claude/det-kommer-mer-879lcm` @ `56f46c8` (INTE från PR #31:s branch — PR #31 och dess
migrationskedja 0019-0024 är helt orörda; se nedan för varför).

**Syfte:** MainAI ska aldrig kunna påstå att den "arbetar" på något utan att en riktig,
varaktig, oberoende observerbar rad finns — en människa (eller en automatiserad
återhämtningspassage) ska kunna fråga, avbryta och se den misslyckas eller slutföras utan att
lita på MainAI:s eget påstående om sitt eget tillstånd.

**Byggt (7 commits, se `docs/MAINAI_JOB_RUNTIME.md` för fullständig arkitektur/hotmodell):**
migration `0025` (`mainai_jobs`/`mainai_job_events`/`mainai_job_proposals`, RLS per tabell,
samma mönster som migration 0007), `app/mainai_runtime_contract.py`
(`MainAIExecutionResponse`s Pydantic-validator gör det till ett valideringsfel att konstruera
ett jobbstött svarsläge utan ett riktigt `job_id`, plus `require_capability()`s stängda
kapacitetsmanifest — idag bara `corpus_review`), `app/jobs/mainai_job_lease.py` (enfas
claim/lease, säkert eftersom `corpus_review`-jobb aldrig skriver lagringsblobbar),
`app/rag/mainai_jobs_service.py` (create/get/list/cancel/retry/mark_* — varje mutation
skriver både en `MainAIJobEvent` och en `audit_log`-rad), `app/rag/corpus_review_job.py`
(första riktiga jobbtypen — läser befintliga indexerade dokument, anropar samma riktiga
`chat_with_fallback()` som `agent_orchestration.py` använder, producerar `MainAIJobProposal`-
rader som ALDRIG blir en `KnowledgeClaim` automatiskt), `app/routers/mainai_jobs.py`
(grundarens-enda API under `/api/mainai/jobs`, plus en strukturellt separat `/admin/all`),
`app/worker.py` (delad poll-loop — provar `knowledge_import_jobs` först, sedan `mainai_jobs`,
inte en andra workerprocess), 43 tester i `tests/backend/test_mainai_jobs.py`, och en
Jobs/Activity-frontend på `/admin/jobs`.

**Verifiering:** 43/43 nya tester gröna mot riktig Postgres (RLS påslaget, endast AI-
providern fejkad). Fullständig regressionskörning: `tests/backend/` 541 passed/1 medvetet
skippad, `tests/security/` + `tests/account/` 65 passed — 0 regressioner. `tsc --noEmit`/
`eslint`/`next build`: rena. En verklig bugg hittades och fixades under arbetet: `get_job()`s
`db.get()` returnerade tyst från SQLAlchemys identity map utan att köra om RLS-policyn när
samma session bytte ägarkontext (workerns poll-loop gör exakt detta) — fixat med
`populate_existing=True`.

**Explicit ej gjort, i linje med grundarens gränser:** ingen produktionsdrift, ingen deploy,
ingen merge, ingen omstart av tjänster, PR #31 orörd, ingen godtycklig terminal-/skalexekvering
implementerad, inga platshållare eller fejkat förlopp. UI:t klarade `tsc`/`eslint`/`next
build` men kunde inte klickas igenom i en riktig inloggad webbläsare i den här sandlådan — ett
differentialtest bevisade att det är sandlådans headless-webbläsar-uppsättning som hänger sig
(redan existerande `/admin/agents`, orörd av denna branch, uppvisar exakt samma "Kontrollerar
inloggning…"-låsning under samma testsele), inte ett fel i den nya koden. Rekommenderas:
grundaren klickar igenom `/admin/jobs` manuellt i en riktig webbläsare innan den litas på.

| Branch | PR | Status | Scope | Bas |
|---|---|---|---|---|
| `claude/mainai-job-runtime-foundation` | Ingen PR öppnad ännu (PR-färdig titel/body i sessionens slutrapport) | **Pushad, 7 commits, redo för PR** — inte mergad, inte granskad av grundaren än | MainAI Runtime Truthfulness and Durable Job Foundation: migration 0025, runtime-kontrakt, jobb-API, worker-integration, `corpus_review`-jobbtyp, 43 tester, Jobs/Activity-UI, arkitekturdokument | `claude/det-kommer-mer-879lcm` @ `56f46c8` |

**Beroenden:** Helt oberoende av PR #31 (S1A/MemorySourceUnit) — ingen delad kod, ingen delad
migration, olika tabeller. Kan granskas/mergas i valfri ordning relativt PR #31.

## Pass 13 (2026-07-29): PR #30 — SECURITY DEFINER-funktionen fick eget ägarskydd

Grundaren hittade att `transition_memory_source()` (SECURITY DEFINER, kör med admin-rollens
rättigheter) inte själv verifierade att källan den skulle övergå faktiskt tillhör
anroparen — RLS gäller inte inuti en `SECURITY DEFINER`-funktion, så `mainai_app` kunde i
princip ha övergått en ANNAN ägares `memory_source_units`-rad, och `actor_type`/`actor_id`
var fria parametrar som kunde sättas till `'admin'`/en godtycklig användare. Löst genom att
dela funktionen i två: `transition_own_memory_source()` (beviljad `mainai_app`, verifierar
`owner_id = current_user_id` FÖRST, `actor_kind` begränsad till `'founder'|'system'`,
`actor_id` härlett internt — aldrig ett parametervärde) och `transition_memory_source_admin()`
(full flexibilitet, `EXECUTE` ALDRIG beviljad `mainai_app`). `search_path` skärpt till enbart
`pg_catalog` + schema-kvalificerade objektnamn istället för `pg_catalog, public`.
`apply_runtime_privileges` utökad att verifiera hela uppdelningen (inte bara UPDATE/DELETE).
CI verifierad grön direkt via GitHub API på PR #30:s exakta head vid varje steg i den här
granskningen, inte antagen från en tidigare commit.

## Pass 12 (2026-07-29): PR #30 — reboot-persistent privilegiehärdning, CI verifierad grön

Grundaren hittade ett verkligt driftfel i privilegieplanen (Pass 11): `backend/docker-
entrypoint.sh` kör `ensure_app_role.py` (som ovillkorligt beviljar `ALL PRIVILEGES` till
`mainai_app` på VARJE boot, inte bara vid rollskapande) FÖRE `alembic upgrade head`. En
`REVOKE UPDATE, DELETE` inskriven bara i S1A:s migration skulle alltså fungera vid första
deployen men bli tyst återställd vid nästa vanliga omstart, eftersom Alembic då inte har
något nytt att köra och `REVOKE` aldrig körs om. Löst i §4.8 genom att lägga till ett fjärde
boot-steg, `apply_runtime_privileges` (idempotent, körs EFTER Alembic, FÖRE appstart, på
VARJE boot — verifierar med `has_table_privilege`/`has_function_privilege` istället för att
anta att `REVOKE`/`GRANT` lyckades, stoppar uppstarten vid avvikelse). Skrivs in i designen
nu, implementeras i S1A-implementations-PR:n tillsammans med migrationen.

Även löst: "Vad som återstår"-listan delad i två explicita trösklar (vad som krävs för att
merga PR #30 självt, kontra vad som krävs för att merga den separata, senare
S1A-implementations-PR:n — produktionsdataprofilen blockerar den senare, inte PR #30).

**CI-status verifierad direkt mot GitHub API** (`pull_request_read` med `get_check_runs`/
`get_status`) på PR #30:s exakta head vid tidpunkten (`a4f4591...`): "All required checks
passed" = success, 18/18 checks completed (VPS-specifika jobb `skipped` som väntat för en
docs-only-PR, resten `success`). Grundarens observation om avsaknad av synlig Actions-körning
var alltså en timing-fråga (körningen hann inte synas/slutföras än) — inte ett kvarstående
CI-problem.

## Pass 11 (2026-07-29): PR #30 — konsoliderad kanonisk design, tre kvarstående blockerare

`docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`s §4.8 skrevs om från en punktlista över fem
sekventiella granskningsrundor (delvis motsägande — äldre `document_tombstone`/subtyp-regler
levde kvar bredvid sina ersättningar) till EN sammanhängande, aktuell design. PR #30:s
beskrivning uppdaterad på GitHub för att matcha (tog bort "Third correction round"-språk och
felaktig `knowledge_claim_evidence`-i-S1A-referens).

Under konsolideringen hittades och löstes tre ytterligare verkliga fel: (1) `document_source_
units` saknade en egen, komposit-FK-buren `source_kind`, vilket skulle fått flera purgade
`document_chunk`-rader från samma dokumentversion att kollidera i `document_version`s
partiella unika index när deras `chunk_id` nollas; (2) en `SET LOCAL`-sessionsflagga
(`memory.transition_active`/`erasure_in_progress`) hävdades vara den enda skrivvägen till
livscykelfält/radering, vilket är strukturellt falskt — vilken kod som helst på samma
DB-anslutning kan sätta samma flagga. Löst med repots REDAN EXISTERANDE rolluppdelning
(`mainai_app`, en icke-ägande runtime-roll, skild från migrationsrollen som äger tabellerna —
se `ensure_app_role.py`): `REVOKE UPDATE, DELETE` från `mainai_app` på proveniens-tabellerna,
`SECURITY DEFINER`-funktioner (`transition_memory_source`/`erase_owner_memory`) med fixerad
`search_path` och `EXECUTE` beviljad enbart till `mainai_app` — en gräns Postgres själv
upprätthåller, inte en flagga en session kan sätta. (3) Purge var ofullständig
(`MemorySourceUnit.content_text` nollades, men `KnowledgeClaim.claim_text`/`Document.
content_preview`/`media_blob`/diskblobben kunde fortfarande innehålla samma material) och det
finns en andra, fortfarande LIVE dokumentraderingsväg (`DELETE /api/documents/{id}`,
`app/routers/documents.py`, anropad från `frontend/lib/api.ts`) som skulle blockeras rakt av
S1A:s nya FK:er — båda måste konsolideras till EN delad `purge_source()`-tjänst.

**Kvarstår innan en S1A-implementations-PR (migration + kod) får öppnas:**
1. Produktionsdataprofilen (`chunk_id`/`version_id`-nullkombinationer på `knowledge_claims`)
   är fortfarande inte körd — ingen databasåtkomst från den här sessionen.
2. Den delade `purge_source()`-tjänsten, `app/rls.py`-uppdateringen, och `delete_account`/
   `export_account`-ändringarna är beskrivna i §4.8 men inte implementerade.
3. Testmatrisen (migration/dual-write/delete/kontoradering/RLS/behörighet/konkurrens) är
   specificerad men inte skriven som kod.

## Pass 10 (2026-07-28): PR #30 — fjärde granskningsrundan av MemorySourceUnit-modellen

Ytterligare åtta korrigeringar innan någon S1-migration skrivs (source_role utökad med
`system`/`unknown`, backfill defaultar till `unknown` inte `external`; `lifecycle_status`
(`active|revoked|purged`) på `memory_source_units` istället för att förlita sig på
`ON DELETE SET NULL` som enda livscykelmodell — nulägesbilden av Library-radering (soft
delete: `Document` behålls, `deleted_at` sätts, chunks raderas) korrigerad; oföränderlig
`content_text`-snapshot krävs eftersom varken `KnowledgeVersion` eller `DocumentChunk`
garanterat bevarar källtexten; deferrable constraint-triggers för en verkligt
databasupprätthållen exclusive arc; komposit-FK `(memory_source_id, owner_id)` för
ägarintegritet; `Message.sequence_number` för deterministisk ordning; `document_chunk`
kontra `document_version`/`document_tombstone` som explicit granularitet istället för ett
odifferentierat `source_kind=document`; `knowledge_claim_evidence`s roller ändrade till
`context|supports|contradicts|corroborates`, `direct` borttaget eftersom
`KnowledgeClaim.memory_source_id` redan ÄR den direkta primärkällan). Reviderad DDL
presenterad i konversationen, ännu inte skriven som Alembic-fil. Se PR #30 för den
uppdaterade designdokumentationen.

Grundaren granskade PR #29 och hittade en verklig, blockerande lucka: migration 0018 satte
alla BEFINTLIGA `knowledge_claims`-rader till `claim_type=uncategorized`, och
`_import_one_file`s dublett-kontroll kör aldrig om `extract_claims_for_document` för ett
redan-`indexed` dokument — så material importerat före P3 skulle aldrig få riktiga
`claim_type`-värden organiskt. Löst med `backfill_claim_types()` (`app/rag/claims.py`):
idempotent, omstartssäker, uppdaterar `claim_type`/`extraction_version` in place på
BEFINTLIGA rader, skapar aldrig nya — se den fullständiga kandidat-/avgränsningslogiken i
funktionens docstring. Manuellt triggerbar via `POST /api/admin/claims/backfill-types`.

**Egen bugg hittad och fixad under implementationen** (inte grundarens fynd): den första
versionen av backfill-loopen requeryade samma misslyckade batch om och om igen inom samma
anrop (ingen exkludering av redan-försökta rader vid providerfel/längdmissmatch,
`max_batches=None` som standard) — en verklig oändlig loop, bekräftad genom att testprocessen
körde 10+ minuter med växande minnesförbrukning innan den dödades manuellt och buggen
identifierades. Fixad med en `failed_ids`-uteslutning per anrop; om testet hade fått köra
längre hade det aldrig terminerat. Kvarstående lärdom: kör alltid nya loopar med en hård
timeout lokalt, aldrig obegränsat, innan de committas.

Samtidigt en större, ännu INTE implementerad arkitekturkorrigering: grundaren pekade ut att
konversationer/meddelanden (`Conversation`/`Message`) ska vara en förstklassig källa till
SAMMA minneskärna som filer — inte bara turer som Context Resolver flaggar som explicit
minne/idé (dagens P6-plan), utan hela historiken, analyserad asynkront i bakgrunden.
Grundaren beordrade uttryckligen: **skriv ingen P4/P6-migration förrän en delad,
additiv proveniensmodell (Document ELLER Message som källa, utan polymorfa FK:er) är låst** —
se konversationen för den fullständiga arkitekturanalysen (exklusiv-arc-mönster via
`num_nonnulls()`-CHECK-constraint, `extract_claims_for_message` som syskonfunktion till
`extract_claims_for_document`, ny bakgrundsworker för konversationsklassificering eftersom
chat.py:s svarsväg är synkron och inte kan bära extra AI-anrop per tur, säkerhetsregler för
att aldrig behandla assistent-genererad text som grundarfakta). **P4/P6-migrationer är därför
INTE påbörjade** — väntar på grundarens bekräftelse av den föreslagna proveniensmodellen.

## Pass 8 (2026-07-28): MainAI Memory Core — P3 (claim-typning), första skivan

Grundaren korrigerade en felaktig uppdelning: "Connected Memory & Project Context v1" (ett
tidigare, för brett formulerat uppdrag) ska INTE byggas som ett separat minnessystem parallellt
med Life Library/Founder Knowledge Studio. Repots egen `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`
specificerar redan EN gemensam minneskärna (§4): Kunskap/Projekt/Idéer/Beslut/Uppgifter/
Grundarminne/Konstitution är typer, relationer och vyer ovanpå SAMMA underliggande kedja
(`ImportJob → Document → KnowledgeVersion → DocumentChunk → KnowledgeClaim`), inte separata
lagringsplatser. Se konversationen för den fullständiga arkitekturgenomgången (befintliga
tabeller som återanvänds, additiva tabeller/kolumner per P3/P4/P6/P7, och en uttrycklig
varning om att INTE förväxla den nya `project_entities`-familjen (P4) med det redan
existerande `app/models/project_memory.py` — ett annat, orelaterat system för LifeAI-repots
EGEN utvecklingsstatus, inte grundarens liv/affärsprojekt).

Byggordning låst till repots egen §8: **P3 (denna branch) → P6 (parallellt, ingen ny PR än) →
P4 (kräver P3) → P5 (kräver P4) → P7B (sist, kräver P4:s godkännandeinfrastruktur)**. Ingen
`MainAICoreContext`, ingen ny retrieval-ordning, ingen systemprompt-ändring i denna PR — de
kräver P4:s `project_entities`-tabell för att ha något att läsa, och byggs i en separat,
senare PR.

| Branch | PR | Status | Scope | Bas |
|---|---|---|---|---|
| `claude/p3-claim-typing` | [#29](https://github.com/d1n095/LifeAI/pull/29) | **Öppen, inte mergad, `mergeable_state: clean`, CI grönt (verifierat)** — väntar på grundarens slutgranskning | P3: `KnowledgeClaim.claim_type` (migration 0018, additiv), utökat STEG 10-extraktionsanrop, `KnowledgeClaimOut`-schema + Library-UI-badge, PLUS (Pass 9) `backfill_claim_types()` för retroaktiv klassificering av befintliga claims + `POST /api/admin/claims/backfill-types`. 18 tester totalt i `test_claims.py` + full lokal svit 562 passed/1 skipped. | `claude/det-kommer-mer-879lcm` @ `dace6c8` (efter PR #28) |

**Verifierat lokalt (Pass 9, klart):** Full backend-svit mot riktig Postgres+Redis: 562 passed,
1 medvetet skippad (P2-kapacitetstestet), 0 regressioner. `tsc --noEmit`/`eslint`: rena.
GitHub Actions-CI verifierad grön direkt mot PR #29:s head-SHA via `pull_request_read` (18/18
checkar, "All required checks passed" = success) — inte bara Vercel.

Grundaren granskade PR #28 kod-för-kod (inte bara CI-status) i två separata rundor efter att
Pass 6:s ursprungliga vertikala kedja redan var grön, och hittade båda gångerna en verklig,
kvarvarande bugg i krasch/återupptagnings-logiken — se `MAINAI_CONTEXT_BUNDLE.md`s produktions-
incident (dokumentet fastnade permanent i `embedding`-status trots att dess ImportJob visade
"Klar"):

- **Runda 1** (commit `5bbc979`): `_import_one_file` behandlade ETT befintligt dokument i
  `RESUMABLE_INDEX_STATUSES` (t.ex. `embedding`, `extracting`) som en vanlig "duplicate" istället
  för att återuppta det — bara `awaiting_provider`/`blocked_provider` återupptogs innan detta.
  Ny `RESUMABLE_INDEX_STATUSES`-konstant (`app/models/document.py`), utökad
  `_resume_incomplete_document` (omdöpt från `_resume_blocked_document`), ny
  `_run_once`-spärr mot att markera ett jobb `completed`/`partial`/`failed` medan ett kopplat
  dokument fortfarande sitter fast, samt ny `app/worker.py`s `_reconcile_orphaned_documents` som
  reparerar ett REDAN terminalt jobb (den mekanism som faktiskt löser det befintliga
  `MAINAI_CONTEXT_BUNDLE.md`-fallet).
- **Runda 2** (commit `8d61f96`): samma återupptagningsfunktion anropade ovillkorligt
  textpipelinen (`extract_text`/`index_document`) — en MP3/MP4 som kraschat i
  `extracting`/`embedding` hade blivit felaktigt skickad dit istället för mediepipelinen
  (`media_import.validate_media_bytes`/`index_media_document`), med risk att bli
  `extraction_failed`. Fixat med dispatch på `media_import.media_kind_for(filename)`. Samtidigt
  fixades att `_requeue_blocked_jobs` lämnade `completed_at`/`failure_reason` kvar vid
  `partial → pending`-återställning (dataintegritetsfel, inte en funktionell bugg).

Båda rundorna fullt lokalt testade (545 passerade, 1 medvetet skippad), `tsc`/`eslint` rena, och
verifierade grönt på riktig GitHub Actions-CI (18/18 kontroller, "All required checks passed")
INNAN merge — se PR #28:s commit-historik för fullständiga detaljer per runda.

**Mergad av grundaren efter explicit, villkorat godkännande** ("När CI faktiskt visar grönt är
beslutet: merga") — sessionen verifierade villkoret (CI grönt på huvud-SHA `8d61f966...`) och
utförde själva merget via GitHub API (`merge_pull_request`, merge-commit `c32c339`), i linje med
grundarens uttryckliga instruktion i det ögonblicket. Ingen deploy utförd av sessionen — se
"Kvarstår efter merge" nedan.

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
| `claude/mainai-core-loop-v1` | [#28](https://github.com/d1n095/LifeAI/pull/28) | **Mergad** (Pass 7, 2026-07-28), merge-commit `c32c339f9710648604c537c24205e686c083f811`, efter grundarens explicita granskning och två ytterligare fix-rundor (`5bbc979`, `8d61f96`) — se Pass 7-avsnittet ovan. | MainAI Core Loop v1 — hela den vertikala kedjan upload→lagring→worker→indexering→sökning→chatt-med-citat→omstartsöverlevnad→providernedgradering→CI→deploy/rollback, verifierad med RIKTIG körning av `docker-compose.vps.yml`+`docker-compose.vps.ci.yml`-topologin på riktiga GitHub Actions-runners (körning [30304755138](https://github.com/d1n095/LifeAI/actions/runs/30304755138), attempt 2, helt grön — se PR #28:s beskrivning för fullständig punkt-för-punkt-verifiering). Lokal `docker build` av de riktiga bilderna är blockerad i den här sessionens sandlåda (nätverkspolicyn tillåter inte apt-get mot deb.debian.org), se `docs/CORE_LOOP_V1_BACKLOG.md`. Innehåller PR #26:s tidigare öppna innehåll (docs + rundtripstest), cherry-pickat och utökat med chatt-med-citat/omstartsöverlevnad/providernedgradering-steg i samma CI-jobb istället för ett nytt. PR #26 stängd med kommentar som pekar hit (ingen merge, allt innehåll bevarat). | `claude/det-kommer-mer-879lcm` @ `13a9677` (inkluderar PR #27) |

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
   ~~#19~~, ~~#22~~, ~~#23~~, ~~#24~~, ~~#25~~, ~~#27~~, ~~#28~~ — samtliga mergade i
   huvudgrenen (se Pass 4/5/7-avsnitten ovan). ~~PR #4~~, ~~PR #6~~, ~~PR #26~~ stängda utan
   merge (#4/#6 subsumerade, se ovan; #26 suppersederad av #28, se Pass 6).
2. ~~PR C~~ — stängd, inte byggd. Se "LLM Coupling & Failure-Boundary Audit"-sektionen ovan
   för verifiering och den nya, separata "ta bort död kod"-uppföljningsuppgiften.
3. **P7A** → implementation kan börja på `claude/p7a-governance-ingestion-plan` FÖRST efter
   ett separat, uttryckligt beslut (branchen är fryst). Kräver DESSUTOM en egen ombasering
   mot huvudgrenens nya tip (nu `c32c339`, efter PR #28) innan aktivering — dess bas
   (`15487e2`) är nu långt bakom både P2:s slutliga tip och själva huvudgrenen.

## Vilka brancher blockerar andra

- **Inget öppet PR finns just nu.** PR #28 mergades (Pass 7); inget annat PR är öppet ovanpå
  huvudgrenen.
- **P7A:s egen aktivering blockeras** av både ett uttryckligt beslut och en ombasering (se
  ovan) — inte av något annat öppet PR.

## Kvarstår efter PR #28:s merge (2026-07-28)

- **Deploy till produktions-VPS:n är INTE utförd av den här sessionen.** Grundarens sista
  instruktion bad om merge OCH deploy "med den befintliga verifierings- och
  rollback-processen", men den här sessionen har varken SSH-nycklar till produktions-VPS:en
  eller tillåtelse att köra en verklig deploy autonomt (se `CLAUDE.md`s standing regler och
  denna sessions upprepade SSH-avslag). VPS Phase 5+6 gjorde deploy explicit manuellt grindad
  (`docs/VPS_OPERATIONS_RUNBOOK.md`) — grundaren behöver köra det faktiska deploy-steget
  (`scripts/vps/deploy.sh`) själv, eller uttryckligen förse sessionen med en riktig, aktuell
  SSH-nyckel och en förnyad bekräftelse i det ögonblicket deployet ska köras.
- Efter en lyckad deploy: `_reconcile_orphaned_documents` (ny i denna PR) kör automatiskt vid
  nästa worker-pollcykel och ska reparera det redan fastnade `MAINAI_CONTEXT_BUNDLE.md`-
  dokumentet (och alla andra dokument i samma läge) utan manuellt ingrepp — se Pass 7 ovan.
  Detta bör verifieras mot produktions-`GET /api/library/{id}`-statusen efter deploy, inte bara
  antas.

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
