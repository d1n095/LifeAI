# Founder Knowledge Studio v1

**Branch:** `claude/founder-knowledge-studio-v1` (bas: `claude/night-shift-mainai-web`).
**Status:** Fungerande vertikal produkt, verifierad end-to-end. Inget mergat, inget deployat.

Detta dokument beskriver vad som faktiskt är byggt och testat i kod — inte en plan. Där
design skiljer sig från implementationen är koden/testerna facit, inte det här dokumentet.

## Vad Founder Knowledge Studio är

MainAI:s privata kunskapssystem: grundaren importerar dokumentpaket, materialet organiseras,
indexeras och blir sökbart (semantiskt och textuellt), och MainAI kan svara på frågor med
källhänvisningar — utan att någonsin presentera historiskt eller omtvistat material som
avgjord sanning. Grundar-endast (`require_founder` på varje ny rutt). Grunden för framtida
UserAI, Life Library och agentstationer, men bygger ingen av dem ikväll.

## Det verifierade vertikala flödet

`importera paket -> validera -> lagra -> extrahera -> indexera -> visa i bibliotek -> söka ->
fråga MainAI -> få källhänvisat svar -> öppna källan -> fortsätta samtalet -> exportera eller
radera materialet`

Bevisat av `frontend/e2e/founder-knowledge-studio.spec.ts`, som kör hela kedjan mot den
riktiga backenden (endast AI-providerns chat/embed-anrop och e-post är fejkade — se
`backend/scripts/ci/run_e2e_backend.py`). Grönt i senaste fulla E2E-regressionskörning
tillsammans med `auth.spec.ts`, `security.spec.ts`, `account.spec.ts`, `shell-pages.spec.ts`.

## Datamodell (DEL 1)

Migration `backend/alembic/versions/0006_founder_knowledge_studio.py`, med verifierad
`upgrade()` OCH `downgrade()`.

- **Document** (utökad, inte duplicerad): `checksum`, `media_type`, `original_filename`,
  `classification` (vision/architecture/decisions/history/security/general),
  `active_truth_status` (active/historical/proposed/superseded/disputed — ett separat
  epistemiskt fält från det tekniska `status`/`IndexStatus`), `project_id`, `version_number`,
  `imported_at`, `deleted_at` (soft delete), `import_job_id`.
- **KnowledgeVersion**: oföränderlig versionshistorik per källa (`source_id`, `checksum`,
  `extraction_version`, `raw_metadata`).
- **ImportJob**: status (pending/running/completed/failed/partial), progress, per-fil-resultat
  (`file_results`), `source_checksum` för idempotens på hela uppladdningen.
- **SourceRelationship**: riktad kant mellan två källor (`derived_from`, `supersedes`,
  `contradicts`, `supports`, `duplicates`, `belongs_to`) — bär bl.a. konfliktdetektionen i
  DEL 6/8 och proveniens-kedjan i DEL 9.

`documents` gick från delad företagskunskap till RLS-skyddad, ägar-scopad grundardata (se
`app/rls.py`s `documents_isolation`-policy och kontodraderings-/exportkoden i
`app/routers/account.py`). Alla fyra tabeller har `FORCE ROW LEVEL SECURITY` + explicit
policy, samma mönster som befintliga `conversations`/`document_chunks`.

## Säker import (DEL 2) — se `docs/KNOWLEDGE_IMPORT_SECURITY.md` för hotmodellen

`app/rag/zip_import.py`: path traversal/Zip Slip, absoluta sökvägar, filantal (max 500),
total/enskild okomprimerad storlek (200 MB / 25 MB), zip-bomb-skydd i två oberoende lager
(metadata-kvot + strömmande hård byte-gräns), exekverbara filer ignoreras, magic-byte-
verifiering för PDF/DOCX (extensionen litas aldrig på ensam), manifest.json valideras,
checksummor verifieras. 22 säkerhetstester i
`backend/tests/backend/rag/test_zip_import_security.py`, ett per attackklass.

## Extraktion och indexering (DEL 3)

`app/rag/library_import.py`: per-fil-dedup på checksumma+ägare, extraktionsversion
(`extract-v1`), manifest-styrd klassificering/status, ett fils fel avbryter aldrig hela
batchen (`FileOutcome` per fil), `manifest.json` självt importeras aldrig som dokument.
`app/routers/library.py`s `ImportJob`-idempotens på hela uppladdningens checksumma
förhindrar dubbla jobb för identiskt innehåll. Deterministiska mock-embeddings i alla tester
— aldrig riktiga AI-nycklar.

## Founder Library-API och UI (DEL 4)

`app/routers/library.py` (`/api/library`): import, jobbstatus, lista med filter
(klassificering/status/projekt/filtyp/datum), källdetalj (versioner, relationer,
chunk-förhandsvisning), radering med explicit bekräftelse (`DeleteConfirmIn`), skapa
källrelationer. `frontend/app/(shell)/library/` (lista + detalj): drag-and-drop-import med
jobbstatuspolling, filter, sök som växlar mellan listvy och sökträffar, tomma/laddnings-/
feltillstånd, tvåstegs raderingsbekräftelse, tillgängliga labels, ingen rå traceback exponerad.

## Hybrid-sök (DEL 5)

`app/rag/vector_store.py`s `hybrid_search()`: semantisk pgvector-kanal + textuell
ILIKE-kanal, sammanslagna per chunk-id med en additiv textträff-bonus (`text_match: bool`
exponerat separat, inte gömt i en enda poäng). Filter på projekt/klassificering/status.
Isolationstester i `test_library_routes.py` och `test_rls_isolation.py` bekräftar att en
annan användares material aldrig kan nås.

## Källgrundad MainAI-chatt (DEL 6) och Trust Engine (DEL 8)

`app/rag/trust.py`: `assess_confidence()` tak:ar (aldrig höjer) konfidens till "medium" när
toppkällan har en icke-aktiv status; `detect_conflicts()` flaggar konflikt endast när en
explicit `contradicts`-relation finns OCH båda ändpunkterna faktiskt hämtades för det här
svaret (strukturell, inte NLP-baserad detektion — dokumenterad begränsning).
`build_trust_instructions()` bygger statusspecifika varningstexter som går rakt in i
systemprompten. `app/routers/chat.py` kopplar ihop detta med källhänvisningar i svaret
(`active_truth_status` per `SourceRef`) och länkar i UI:t direkt till exakt källa i
biblioteket (`frontend/app/(shell)/chat/page.tsx`). 8 dedikerade tester i
`tests/backend/chat/test_chat_source_grounding.py` täcker: en träff, historisk källa som tak:ar konfidens,
omtvistad källa, motstridiga källor, ingen falsk konflikt utan relation, raderad källa
används aldrig, isolation mellan användare, och den faktiska system-prompt-texten (inte bara
svarsfältet) verifieras innehålla varningen.

## Conversation Context Resolver v1 (DEL 7) — se `docs/CONTEXT_RESOLVER_V1.md`

`app/context/resolver.py`: regelbaserad (inte NLP/LLM-baserad) klassificering av varje tur i
en konversation. Rent observationell ikväll — påverkar inte retrieval eller systemprompten,
exponeras bara på svaret (`context_intent`, `context_confidence`) för framtida UI/beteende
att bygga vidare på. 26 tester, inklusive alla obligatoriska ordagranna testfraser.

## Founder Workbench (DEL 9)

`app/routers/workbench.py` (`/api/workbench`): `POST /analyze` (välj projekt och/eller en
specifik källa, ställ en fråga, återanvänder EXAKT samma retrieval/trust/provider-kedja som
`/api/chat` — ingen parallell implementation som skulle behöva verifieras separat) och
`POST /save` (sparar resultatet som ett nytt, sökbart `Document` märkt
idé/förslag/beslut/historik, med `derived_from`-relationer tillbaka till de analyserade
källorna). Uppföljningsuppgifter återanvänder den befintliga `POST /api/projects/tasks`.
`frontend/app/(shell)/workbench/page.tsx`. Grund för en framtida agentstation — inte hela
agentorganisationen.

## GDPR-export och radering (DEL 11)

`app/routers/account.py`s `export_account()` inkluderar nu källmetadata, versionshistorik,
källrelationer och importjobb för den inloggade användaren — verifierat att en annan
användares material aldrig läcker in. `delete_account()` raderar dokument/chunks/versioner/
relationer/importjobb helt (inte anonymisering — se motiveringen i koden om varför
`documents` inte längre kan anonymiseras under RLS). Delad företagskunskap (projekt/uppgifter)
förblir orörd, endast attribution rensas.

## API och säkerhet (DEL 12)

Varje ny rutt: `require_founder` server-side, storleksgräns på uppladdning (60 MB), egen
rate limit (`rate_limit_library_import_per_minute`, `rate_limit_workbench_per_minute`),
inga filsystemsökvägar eller DB-fel exponerade i svar, CSRF via samma `get_current_user`-
mekanism som resten av API:t, RLS/ägarisolering på alla nya tabeller. Se
`docs/KNOWLEDGE_IMPORT_SECURITY.md` för den dedikerade hotmodellen för import/sökning/
källhänvisning.

## Byggt, testat — och medvetet INTE byggt ikväll

**Byggt och testat:** DEL 1–9, 11, 12 (se ovan), plus STEG 10 (påstående-nivå trust), STEG 11
(Redis-baserade jobblås/återförsök), STEG 12 (ljud/video-import v1) och STEG 13 (multimedia i
UI — spelare, tidsstämplad transkriptvy/sök, felåterförsök, citat som öppnar rätt ögonblick)
från senare sessioner — se `docs/FOUNDER_KNOWLEDGE_STUDIO_HANDOVER_2026-07-20.md`s
"STEG 10–11-" och "STEG 12–13-tillägg" för den fulla tekniska beskrivningen. Hela sviten grön
(314 tester). Full Playwright-regression grön (auth/security/account/shell-pages/founder-
knowledge-studio/founder-knowledge-studio-media/library-workbench-mobile).

**Design-only / dokumenterat men inte byggt:**
- **STEG 14** — full vertikal 12-stegsverifiering som även täcker ljud/video. Inte påbörjad;
  medvetet nedprioriterad tills produktionsstarten på Render är löst (se handover-dokumentets
  "Exakt nästa steg").
- **DEL 14** — lätt lokal mätning genomförd (`backend/tests/backend/test_performance_measurement.py`,
  kör med `-s` för att se siffrorna), inte en fullständig instrumenterad produktionsmätning.
  Ett syntetiskt paket (10 filer × 20 stycken) lokalt mot riktig Postgres/pgvector med en
  deterministisk fejk-embedding gav: import 0,244s totalt (~0,024s/fil), 10 chunkar (1
  chunk/fil vid den textstorleken — chunkningsgränsen är större än en enskild fil här),
  hybrid-sök (top_k=10) 37 ms, chatt-hämtning (top_k=5) 10 ms med 10 145 tecken kontext till
  prompten. Siffrorna är inte absoluta produktionsvärden (lokal maskin, syntetisk data) utan
  en baslinje och ett lätt regressionsskydd — de hårda säkerhetsgränserna som faktiskt
  förhindrar obegränsad kostnad är redan på plats och testade oberoende av detta
  (`MAX_FILES=500`, `MAX_TOTAL_UNCOMPRESSED_BYTES=200MB`, `top_k=5` i retrieval — se
  `docs/KNOWLEDGE_IMPORT_SECURITY.md`).
- Multimedia i UI (STEG 13) — uppladdning/spelare/tidsstämplade citat i Library/Workbench-
  frontend. Backend-pipelinen (STEG 12) är klar och testad; ingen UI byggd för den ännu.
- Riktig transkription (Whisper/Gemini/etc) — STEG 12 bygger hela pipelinen och ett
  leverantörsgränssnitt, men ingen riktig, betald leverantör är inkopplad (se STEG 12-
  tillägget nedan).
- Automatisk AI-system-handover mellan MainAI-instanser — inte påbörjat.

## Migrationsordning och rollback

Migration 0006 är den enda nya migrationen. `alembic upgrade head` från
`claude/night-shift-mainai-web`s senaste migration kör den. `alembic downgrade -1` verifierad
lokalt (droppar tabellerna i omvänd beroendeordning, samma "ta inte bort enum-värdet"-resonemang
som migration 0005 redan etablerade för Postgres-enums).

## Rekommenderad granskningsordning

1. `backend/alembic/versions/0006_founder_knowledge_studio.py` + `app/rls.py` (datamodell och
   isolation är grunden allt annat vilar på).
2. `app/rag/zip_import.py` + `test_zip_import_security.py` (säkerhetskritiskt, litet och
   fristående).
3. `app/rag/trust.py` + `tests/backend/chat/test_chat_source_grounding.py` (den centrala "ljug aldrig om vad en
   källa säger"-garantin).
4. `app/routers/library.py` + `app/routers/workbench.py` (API-ytan).
5. `frontend/e2e/founder-knowledge-studio.spec.ts` (bevis på att helheten faktiskt fungerar).

## Exakt nästa steg

Se `docs/FOUNDER_KNOWLEDGE_STUDIO_HANDOVER_2026-07-20.md` för commit-för-commit-status,
CI-länkar och en konkret att-göra-lista för nästa session.
