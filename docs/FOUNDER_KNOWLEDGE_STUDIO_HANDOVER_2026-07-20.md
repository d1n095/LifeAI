# Founder Knowledge Studio v1 — Handover 2026-07-20

**Branch:** `claude/founder-knowledge-studio-v1` (bas: `claude/night-shift-mainai-web`).
**Status vid session slut:** Ingenting mergat. Ingenting deployat. Ingen Render-, Supabase-,
Upstash- eller Strato-inställning rörd. Inga produktionshemligheter lästa eller committade.
Inget riktigt/privat FKP-material importerat — endast syntetiska testpaket byggda i minnet
under testkörning.

## Commits, alla pushade, alla gröna i CI

| Commit | Del | CI-run |
|---|---|---|
| `cd2ab82` | DEL 1 — datamodell + migration 0006 | [29705265695](https://github.com/d1n095/LifeAI/actions/runs/29705265695) |
| `fd0e364` | DEL 2 — säker ZIP-importmotor | [29705366002](https://github.com/d1n095/LifeAI/actions/runs/29705366002) |
| `eac9446` | DEL 3 — import-orkestrering + idempotens | [29705545954](https://github.com/d1n095/LifeAI/actions/runs/29705545954) |
| `dcfdfbf` | DEL 4 — /api/library API + hybrid-sök | [29705798989](https://github.com/d1n095/LifeAI/actions/runs/29705798989) |
| `7ca0424` | DEL 5/6/8 — källgrundad MainAI-chatt + Trust Engine | [29716920435](https://github.com/d1n095/LifeAI/actions/runs/29716920435) |
| `5c63f14` | DEL 7 — Conversation Context Resolver v1 | [29717184743](https://github.com/d1n095/LifeAI/actions/runs/29717184743) |
| `b37c0bc` | DEL 4 — Library-UI + vertikalt E2E-flöde + tvärroutar-buggfix | [29718372402](https://github.com/d1n095/LifeAI/actions/runs/29718372402) |
| `75b1dd6` | DEL 11 — GDPR-export utökad med kunskapsdata | [29718482476](https://github.com/d1n095/LifeAI/actions/runs/29718482476) |
| `ae518ea` | DEL 9 — Founder Workbench | [29718835246](https://github.com/d1n095/LifeAI/actions/runs/29718835246) |

Total diff mot `claude/night-shift-mainai-web`: 9 commits, se `git log
claude/night-shift-mainai-web..HEAD` för fullständig lista.

## Det verifierade minimikravet: fungerande vertikalt flöde

`importera paket -> validera -> lagra -> extrahera -> indexera -> visa i bibliotek -> söka ->
fråga MainAI -> få källhänvisat svar -> öppna källan -> fortsätta samtalet -> radera materialet`

Bevisat av `frontend/e2e/founder-knowledge-studio.spec.ts` mot den riktiga backenden (endast
AI-providerns chat/embed-anrop och e-post fejkade). Senaste fulla E2E-regressionskörning,
lokalt: **17 passed, 1 skipped** (det överhoppade testet kräver container-isolering och hoppas
alltid över lokalt, samma som i tidigare nattpass) — `auth.spec.ts`, `security.spec.ts`,
`account.spec.ts`, `shell-pages.spec.ts`, `founder-knowledge-studio.spec.ts` alla gröna
tillsammans.

## Testresultat

- **Backend-pytest:** 227 tester gröna (växte från startpunkten 124 genom sessionen via de nio
  commit:en ovan — se respektive commits diffstat för exakta siffror per steg).
- **Frontend:** TypeScript-typecheck grön, ESLint grön (0 fel), produktionsbygge
  (`npx next build`) grönt med `/library`, `/library/[id]` och `/workbench` registrerade som
  rutter.
- **Migration:** `alembic upgrade head` OCH `alembic downgrade -1` verifierade lokalt mot
  riktig Postgres 16 med pgvector.
- **Säkerhetstester:** 22 dedikerade ZIP-importtester (en per attackklass), 6 nya
  RLS-isolationstester, isolationstester i export/workbench/library som aktivt försöker läcka
  en annan användares data.

## Verkliga buggar hittade och fixade under sessionen (inte hypotetiska)

1. **Kontoradering skulle krascha under den nya RLS-policyn.** `account.py`s gamla
   `uploaded_by=NULL`-anonymisering bröt mot den nya `documents_isolation`-policyns
   `WITH CHECK`. Fixad genom att radera dokument helt istället för att anonymisera (dokument är
   nu ägar-scopad grundardata, inte delad företagskunskap). Regressionstest tillagt.
2. **`vector_store.search()` saknade helt ett `deleted_at IS NULL`-filter** — en riktig
   säkerhetslucka (soft delete är helt nytt i denna session) som innebar att en raderad källas
   chunkar kunde dyka upp i chatt-hämtning eller sökning. Fixad, gynnar både det nya
   biblioteket och den befintliga chatt-hämtningen automatiskt.
3. **Cross-router-läcka:** `app/routers/documents.py`s äldre `list_documents()` hade samma
   saknade filter — en källa raderad via `/api/library` dök ändå upp via `/api/documents`
   (samma underliggande tabell). Hittad via ett reproducerbart E2E-testfel, verifierad genom
   revert/reapply-metodik (testet failar mot okorrigerad kod, passerar med fixen) innan den
   rapporterades som en riktig bugg och inte antogs vara det.
4. **`manifest.json` importerades som ett vanligt kunskapsdokument** istället för att bara
   användas för metadata — fixad genom att explicit hoppa över filen i importloopen.
5. **`LibrarySearchHit`-schemat saknade `text_match`** — FastAPI:s `response_model` tystade
   bort fältet trots att `hybrid_search()` satte det korrekt. Fixad genom att lägga till fältet
   i schemat.

## Säkerhetsgranskning

Se `docs/KNOWLEDGE_IMPORT_SECURITY.md` för den fullständiga hotmodellen (import/hämtning/
källhänvisning). Sammanfattning: RLS `FORCE ROW LEVEL SECURITY` på alla fem nya/ändrade
tabeller, ZIP-import med tvålagers zip-bomb-skydd och path-traversal-skydd, magic-byte-
verifiering där ett format har en fast signatur, checksummabaserad idempotens på två nivåer,
Trust Engine som tak:ar (aldrig höjer) konfidens för icke-aktivt material, strukturell (inte
NLP-baserad, dokumenterad begränsning) konfliktdetektion, källhänvisningar som alltid kommer
från den faktiska retrieval-listan (kan aldrig hallucineras av modellen).

## Vad som INTE är byggt (dokumenterat, inte glömt)

Se det fullständiga avsnittet i `docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md`. Kort:
- Påstående-nivå (`KnowledgeClaim`) trust-bedömning (DEL 8-fördjupning) — kräver en ny tabell,
  medvetet avgränsad.
- Redis-baserade jobblås för flerprocess-/replikskydd (DEL 10) — dagens `ImportJob` +
  `BackgroundTasks` är enprocess-säkert men saknar den explicita lås-abstraktionen.
- Formell prestanda-/kostnadsmätning (DEL 14) — gränser finns (60 MB, 500 filer, top_k=5) men
  är inte instrumenterade.
- Ljud/video/transkript-import — ingen extraktion för dessa format.
- Automatisk AI-system-handover mellan MainAI-instanser.
- Riktig extern malware-/antivirusskanning av importerat innehåll (kräver en tjänst som inte
  är aktiverad, i linje med uppdragets förbud).

## Migrationsordning och rollback

En enda ny migration, `0006_founder_knowledge_studio.py`, körs efter
`claude/night-shift-mainai-web`s senaste migration via `alembic upgrade head`. Rollback:
`alembic downgrade -1`, verifierad lokalt. Innehåller samma "ta inte bort enum-värdet"-mönster
som migration 0005 redan etablerade för Postgres (`zip_import`-värdet på `documentsource`-enumen
kan inte tas bort vid nedgradering, bara tabellerna som använder det).

## Rekommenderad granskningsordning

1. `backend/alembic/versions/0006_founder_knowledge_studio.py` + `app/rls.py`
2. `app/rag/zip_import.py` + `tests/backend/test_zip_import_security.py`
3. `app/rag/trust.py` + `tests/backend/test_chat_source_grounding.py`
4. `app/routers/library.py` + `app/routers/workbench.py`
5. `frontend/e2e/founder-knowledge-studio.spec.ts`

## Exakt nästa steg för nästa session/granskare

1. Läs igenom Draft PR:en (öppnas i samband med detta handover-dokument, se nedan) och de fem
   filerna i granskningsordningen ovan.
2. Om godkänd: merga `claude/founder-knowledge-studio-v1` in i `claude/night-shift-mainai-web`
   (INTE till `main`/produktion utan en separat, explicit produktionsbeslutsprocess — se
   `docs/RENDER_DEPLOY.md`).
3. Nästa naturliga djup, i ordning efter vad som redan är mest byggt: DEL 8-fördjupning
   (`KnowledgeClaim`-tabell), DEL 10 (Redis-jobblås inför flerinstansdrift), DEL 14
   (prestanda-/kostnadsmätning), ljud/video-import.
4. Inga öppna produktbeslut väntar på svar just nu — allt som saknades dokumenterades och
   avgränsades löpande (se "inte byggt"-avsnitten) istället för att blockera arbetet.

## Bekräftelse

Inget mergat. Inget deployat. Ingen Render-, Supabase-, Upstash- eller Strato-inställning
rörd. Inga produktionshemligheter lästa eller committade. Inga riktiga AI-nycklar använda i
tester (alla providertester använder deterministiska fejk-providrar). Inget riktigt/privat
material importerat — endast syntetiska testpaket. Ingen offentlig registrering
återinförd. Alla databasändringar via en reversibel Alembic-migration med verifierad
`upgrade()` och `downgrade()`.
