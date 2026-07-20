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
| `5929b19` | DEL 15 — obligatorisk dokumentation | [29719031419](https://github.com/d1n095/LifeAI/actions/runs/29719031419) |
| `4d400af` | DEL 14 — lokal prestandamätning | [29719246143](https://github.com/d1n095/LifeAI/actions/runs/29719246143) |
| `a4f38b5` | Handover uppdaterad med commit-lista + PR-länk | (dokumentation, ingen kodändring) |
| `c930811` | STEG 9 — automatiserat migrationsrundturstest | [29719643222](https://github.com/d1n095/LifeAI/actions/runs/29719643222) |
| `8766b78` | STEG 7 — fixa obegränsad chunk-storlek (verklig bugg) | [29719836847](https://github.com/d1n095/LifeAI/actions/runs/29719836847) |
| `41b64a1` | STEG 7 — verifiera att råa DB-fel aldrig läcker | [29720028211](https://github.com/d1n095/LifeAI/actions/runs/29720028211) |
| `28db7f8` | STEG 9 — OpenAPI-kontroll för /api/library och /api/workbench | [29720247185](https://github.com/d1n095/LifeAI/actions/runs/29720247185) |
| `9794e45` | STEG 3/9 — Playwright mobil-täckning för /library och /workbench | [29720420425](https://github.com/d1n095/LifeAI/actions/runs/29720420425) |

Total diff mot `claude/night-shift-mainai-web`: 17 commits, se `git log
claude/night-shift-mainai-web..HEAD` för fullständig lista.

**Draft PR:** [d1n095/LifeAI#4](https://github.com/d1n095/LifeAI/pull/4) mot
`claude/night-shift-mainai-web`. Öppen som draft, INTE mergad.

## Det verifierade minimikravet: fungerande vertikalt flöde

`importera paket -> validera -> lagra -> extrahera -> indexera -> visa i bibliotek -> söka ->
fråga MainAI -> få källhänvisat svar -> öppna källan -> fortsätta samtalet -> radera materialet`

Bevisat av `frontend/e2e/founder-knowledge-studio.spec.ts` mot den riktiga backenden (endast
AI-providerns chat/embed-anrop och e-post fejkade). Senaste fulla E2E-regressionskörning,
lokalt: **19 passed, 1 skipped** (det överhoppade testet kräver container-isolering och hoppas
alltid över lokalt, samma som i tidigare nattpass) — `auth.spec.ts`, `security.spec.ts`,
`account.spec.ts`, `shell-pages.spec.ts`, `founder-knowledge-studio.spec.ts` och den nya
`library-workbench-mobile.spec.ts` alla gröna tillsammans.

## Testresultat

- **Backend-pytest:** 238 tester gröna (växte från startpunkten 124 genom sessionen — se
  respektive commits diffstat för exakta siffror per steg).
- **Frontend:** TypeScript-typecheck grön, ESLint grön (0 fel), produktionsbygge
  (`npx next build`) grönt med `/library`, `/library/[id]` och `/workbench` registrerade som
  rutter. `npm audit` — 0 sårbarheter.
- **Migration:** `alembic upgrade head` → `downgrade -1` → `upgrade head` verifierat både
  manuellt och som ett automatiserat, återkörbart backend-test
  (`tests/backend/test_migration_roundtrip.py`) som jämför hela schemat (tabeller +
  documents-kolumner) före och efter rundturen, inte bara att kommandona lyckas utan fel.
- **Säkerhetstester:** 22 dedikerade ZIP-importtester (en per attackklass), 6 nya
  RLS-isolationstester, isolationstester i export/workbench/library som aktivt försöker läcka
  en annan användares data, ett verifierat test att råa DB-fel aldrig läcker i ett HTTP-svar,
  en OpenAPI-schemakontroll för alla åtta nya rutter.
- **Mobil:** `frontend/e2e/library-workbench-mobile.spec.ts` — /library och /workbench vid en
  telefon-bredd (390px): mobilmenyn fungerar, ingen horisontell overflow, kärnkontroller
  synliga och användbara.

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
6. **Obegränsad chunk-storlek.** `app/rag/chunking.py`s `chunk_text()` delar text på
   whitespace och styr chunkstorlek via ordantal — ett dokument helt utan whitespace (t.ex.
   en minifierad fil eller en base64-blob) blev ett enda "ord" och därmed EN enda,
   obegränsad chunk. Bekräftat lokalt: en 2 MB whitespace-fri sträng gav en 2 000 000-tecken
   lång chunk, som sedan skulle gå hel till både embedding-providern och (om den citerades)
   rakt in i en chatt-/workbench-prompt — en obegränsad kostnads-/promptstorlekssårbarhet.
   Fixad med `MAX_CHUNK_CHARS`, en hård teckenbaserad fallback-gräns. Verifierad via
   revert/reapply (regressionstestet failar mot okorrigerad kod med ett tydligt
   `ImportError`, passerar med fixen).

## Säkerhetsgranskning

Se `docs/KNOWLEDGE_IMPORT_SECURITY.md` för den fullständiga hotmodellen (import/hämtning/
källhänvisning). Sammanfattning: RLS `FORCE ROW LEVEL SECURITY` på alla fem nya/ändrade
tabeller, ZIP-import med tvålagers zip-bomb-skydd och path-traversal-skydd, magic-byte-
verifiering där ett format har en fast signatur, checksummabaserad idempotens på två nivåer,
Trust Engine som tak:ar (aldrig höjer) konfidens för icke-aktivt material, strukturell (inte
NLP-baserad, dokumenterad begränsning) konfliktdetektion, källhänvisningar som alltid kommer
från den faktiska retrieval-listan (kan aldrig hallucineras av modellen).

## Vad som INTE är byggt (dokumenterat, inte glömt)

Se det fullständiga avsnittet i `docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md`. Kort — uppdaterat efter
STEG 10 (påstående-nivå trust) och STEG 11 (Redis-jobblås), båda nu byggda, se avsnittet
"STEG 10–11-tillägg" nedan:
- Ljud/video/transkript-import (STEG 12–13) — ingen extraktion för dessa format ännu, näst i
  trappan.
- Automatisk AI-system-handover mellan MainAI-instanser.
- Riktig extern malware-/antivirusskanning av importerat innehåll (kräver en tjänst som inte
  är aktiverad, i linje med uppdragets förbud).

## STEG 10–11-tillägg (denna session)

**STEG 10 — påstående-nivå trust:** migration 0007 lägger till `knowledge_claims` och
`claim_relationships` (samma RLS-mönster som migration 0006). `app/rag/claims.py` extraherar
testbara påståenden per chunk via providern (kapat vid `MAX_CHUNKS_PER_DOCUMENT=20`), binder
varje påstående till exakt källa/version/chunk, och beräknar ett objektivt `grounding_score`
(ordöverlapp mot chunk-texten — INTE modellens egen självrapporterade konfidens, samma princip
som redan gäller på källnivå i `app/rag/trust.py`). Konfidensen (`assess_claim_confidence`)
beräknas LIVE vid varje läsning, aldrig cachad: en `contradicts`-relation tak:ar den
ovillkorligt till `conflict`, `no_basis` kan aldrig höjas, och `likely`→`certain` kräver en
OBEROENDE `supports`-relation från en annan källa. `detect_claim_conflicts` i `chat.py` fångar
motsägelser på påstående-nivå som saknar en explicit källrelation. Exponerat i
`GET /api/library/{id}` och `/library/[id]`-sidan. 21 nya tester
(`tests/backend/test_claims.py`), inklusive en cross-owner-isolationstest som skapar data som
ägare A och verifierar att en session scopead till ägare B fortfarande ser rätt (icke-höjd)
konfidens även när den ges ägare A:s objekt direkt.

**STEG 11 — robusta importjobb för flera instanser:** `app/jobs/lock.py` är ett
Redis-baserat distribuerat lås (samma enda Redis-instans som redan används för rate limiting
— ingen ny betaltjänst, ingen ny produktionsworker): atomärt förvärv via `SET NX EX`, Lua-CAS
för förnyelse/frisläppning så bara rätt innehavare (token-matchning) kan agera, och en
leasing-TTL på varje lås så det aldrig kan bli permanent. `app/jobs/retry.py` klassificerar
fel som tillfälliga (nätverk, `JobLockUnavailable`) eller permanenta (`ZipSecurityError`,
`ValueError`, okänd typ default:ar till permanent — vitlista, inte svartlista) och beräknar
exponentiell backoff med full jitter. `run_import_job` (`app/rag/library_import.py`) förvärvar
ett lås nyckla på `(ägare, innehålls-checksumma)`, hjärtslår det per fil under stora paket, och
degraderar till okoordinerad körning (inte blockering) om Redis själv är nere. Migration 0008
lägger till `attempt_count`/`max_attempts`/`last_failure_transient` på `knowledge_import_jobs`,
exponerat i `ImportJobOut` och synligt i Library-UI:t ("försöker igen automatiskt" vs.
permanent fel). Återupptagbarhet är en emergent egenskap av den redan existerande per-fil-
checksummaidempotensen — ingen ny logik krävdes. 29 nya tester
(`test_job_lock.py` — 18, `test_job_retry.py` — 11, plus retry/lås-integrationstester i
`test_library_import.py`), inklusive ett test av två genuint samtidiga importförsök av samma
innehåll där exakt ett blockeras av låset (verifierat med en riktig asyncio-interleaving via en
monkeypatchad fördröjning i embed-anropet, inte bara sekventiell körning som råkar se
samtidig ut).

Under STEG 11-arbetet hittades och fixades ytterligare en instans av det redan kända
RLS/ContextVar-buggmönstret (en testhjälpfunktion satte bara den råa `SET LOCAL`-SQL:en, inte
`current_user_id`-kontextvariabeln som `app/db.py`s `after_begin`-lyssnare behöver för att
återapplicera RLS-scoping på en session's ANDRA transaktion) — fångat proaktivt av ett nytt
test, inte i produktion.

## Slutgranskning (STEG 9-checklista, uppdaterad efter STEG 10–11)

- [x] Hela backend-testsviten — 285 tester gröna (upp från 238 vid STEG 9, se STEG 10–11-
      tillägget ovan)
- [x] Migration upgrade → downgrade → upgrade — automatiserat test, schema jämfört rad för rad
- [x] Frontend typecheck, ESLint, produktionsbygge — alla gröna
- [x] Playwright desktop (fullt vertikalt flöde) — grönt
- [x] Playwright mobil (/library, /workbench) — grönt, nytt denna omgång
- [x] `npm audit` — 0 sårbarheter
- [x] OpenAPI-kontroll för alla åtta nya rutter — schemat byggs, alla rutter dokumenterade med
      riktiga JSON-scheman
- [x] Hemlighetsgenomsökning av hela grendiffen — mönsterbaserad, inga träffar utöver kända
      testplaceholders (GitHub:s strukturerade scanner kunde inte ta hela ~300 KB-diffen i ett
      anrop; se not i commit `28db7f8`)
- [x] CI grön på samtliga 17 commits

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
3. Nästa naturliga djup: ljud/video-import v1 (STEG 12 — transkriptionsprovider-gränssnitt,
   deterministisk mock i tester, tidsstämplade segment/chunks, sökbart, citerbart med
   tidsstämpel), sedan multimedia i UI (STEG 13), sedan en full vertikal 12-stegsverifiering
   som även täcker ljud/video (STEG 14).
4. Inga öppna produktbeslut väntar på svar just nu — allt som saknades dokumenterades och
   avgränsades löpande (se "inte byggt"-avsnitten) istället för att blockera arbetet.

## Bekräftelse

Inget mergat. Inget deployat. Ingen Render-, Supabase-, Upstash- eller Strato-inställning
rörd. Inga produktionshemligheter lästa eller committade. Inga riktiga AI-nycklar använda i
tester (alla providertester använder deterministiska fejk-providrar). Inget riktigt/privat
material importerat — endast syntetiska testpaket. Ingen offentlig registrering
återinförd. Alla databasändringar via en reversibel Alembic-migration med verifierad
`upgrade()` och `downgrade()`.
