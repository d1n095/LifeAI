# Knowledge Import/Retrieval/Citation — hotmodell

Dedikerad hotmodell för Founder Knowledge Studio v1 (DEL 12 i uppdraget), täcker tre
attackytor: import, hämtning (retrieval/sökning), och källhänvisning (citation). Se
`docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md` för vad som är byggt i stort, `docs/AUTH_THREAT_MODEL.md`
för autentisering/cookies (out of scope här — förutsätts redan gällande).

**Förutsättning:** enanvändarsystem (grundar-endast). Hoten nedan handlar därför inte om
"en illvillig annan användare" i klassisk mening, utan om: (1) skadligt/felformat *innehåll*
grundaren själv importerar (t.ex. ett paket laddat ner från nätet), (2) buggar som råkar
lägga en isolationsspricka i systemet innan en andra användare (framtida UserAI) någonsin
läggs till, och (3) MainAI självt som en felkälla — att modellen missrepresenterar sitt eget
underlag.

## Attackyta 1: Import

| Hot | Kontroll | Var |
|---|---|---|
| Zip Slip / path traversal (`../../etc/passwd`, absolut sökväg, Windows-enhetsbokstav) | `_is_safe_member_name()` — segmentkontroll via `PurePosixPath.parts`, inte substrängssökning (undviker falska positiva på legitima filnamn som råkar innehålla `..`) | `app/rag/zip_import.py` |
| Zip-bomb (extrem komprimeringskvot) | Två oberoende lager: (1) metadata-baserat kvotfilter, snabbt men litar på zip:ens deklarerade storlekar; (2) strömmande dekomprimering med hård byte-gräns som ALDRIG litar på deklarerad metadata — avbryter mitt i läsningen om den verkliga datan överskrider gränsen | `_read_with_hard_cap()` |
| Resursuttömning via filantal/total storlek | `MAX_FILES=500`, `MAX_TOTAL_UNCOMPRESSED_BYTES=200MB`, `MAX_SINGLE_FILE_UNCOMPRESSED_BYTES=25MB` | `zip_import.py`-konstanter |
| Nästlad zip-bomb (liten per nivå, stor totalt — kringgår en per-nivå-gräns genom att sprida ut storleken över flera lager av zip-i-zip) | (P2) `_ExtractionBudget` är ETT delat, muterbart objekt som trådas genom varje rekursivt anrop och ALDRIG nollställs per nivå — total fil- och byteräkning är därför alltid kumulativ över hela paketet, oavsett nästlingsdjup. `MAX_SINGLE_FILE_UNCOMPRESSED_BYTES` gäller dessutom varje enskild fil på varje nivå, inklusive ett nästlat arkivs egen uppackade storlek (det räknas som "en fil" ur sin förälders perspektiv). `MAX_NESTING_DEPTH=3` sätter dessutom ett hårt tak på rekursionsdjupet | `_ExtractionBudget`, `_extract_recursive()` i `zip_import.py` |
| Krypterat/lösenordsskyddat arkiv eller enskild post | Explicit klassificering via `_classify_unreadable_entry()` — `RuntimeError`/`NotImplementedError` från `zipfile` vid en krypterad post ger en dedikerad `status="encrypted"`, skild från generisk `"rejected"`. INGA lösenordsförsök eller lösenordsgissningar görs eller kommer någonsin göras — filen flaggas bara som olässbar utan lösenord | `_classify_unreadable_entry()` i `zip_import.py` |
| Körbar kod smugglad in som "dokument" | `EXECUTABLE_EXTENSIONS` ignoreras explicit, oavsett vad manifestet påstår | `zip_import.py` |
| Filändelsen ljuger om innehållet (`.pdf` som egentligen är något annat) | Magic-byte-verifiering för PDF (`%PDF-`) och DOCX (`PK\x03\x04`) — de enda två formaten med en fast signatur. Rena textformat (txt/md/json/html) har ingen sådan signatur att kontrollera; där betyder "lita aldrig på extensionen ensam" istället säker UTF-8-avkodning nedströms, inte en obefintlig magic-byte-kontroll | `_magic_bytes_ok()` |
| Trasigt/skadligt `manifest.json` | JSON-parsning i eget try/except, kräver ett dict på toppnivå, ett fels manifest gör inte att hela importen kraschar — den fortsätter utan manifestdriven metadata | `zip_import.py` |
| Ett enskilt fils fel korrumperar hela batchen | `FileOutcome` per fil, `run_import_job()` fortsätter till nästa fil vid fel på en | `app/rag/library_import.py` |
| Dubbelimport (samma innehåll igen) | Idempotens på två nivåer: per fil (checksumma + ägare + ej raderad) och per hel uppladdning (`ImportJob.source_checksum` + ägare + `completed`-status ger samma jobb tillbaka, ingen ny körning) | `library_import.py`, `app/routers/library.py` |
| Checksummafalsk manifest-uppgift (manifestet påstår fel checksumma för en fil) | Verifieras explicit mot den faktiska filens sha256 — mismatch avvisar just den filen, inte hela batchen | `library_import.py` |
| För stor total uppladdning över HTTP | `MAX_UPLOAD_BYTES=60MB` på routernivå, innan zip-motorn ens öppnar filen | `app/routers/library.py` |
| Överdriven anropsfrekvens (skript som spammar import) | `rate_limit_library_import_per_minute` (Redis-baserad, per användare) | `app/config.py`, `app/limiter.py` |

**Medvetet inte byggt:** riktig antivirus-/malware-skanning av filinnehåll (kräver en extern
tjänst — inget sådant har aktiverats, i linje med uppdragets förbud mot att aktivera betalda
tjänster). `library_import.py`s pipeline har en tydlig plats för ett sådant steg (mellan
checksummaverifiering och textextraktion) om/när en sådan tjänst godkänns.

## Attackyta 2: Hämtning (retrieval/sökning)

| Hot | Kontroll | Var |
|---|---|---|
| En raderad källas innehåll dyker ändå upp i sökning/chatt | `search()`/`hybrid_search()` joinar `Document` explicit och filtrerar `deleted_at IS NULL` på SQL-nivå, inte bara i UI:t — verifierad som en RIKTIG bugg denna session (fanns inte innan Founder Knowledge Studio v1:s soft delete existerade) | `app/rag/vector_store.py` |
| Cross-router-läcka: raderat via ett API, ändå synligt via ett annat | `app/routers/documents.py`s äldre `list_documents()` saknade samma filter — hittad och fixad via E2E-test, regressionstest tillagt | `documents.py`, `tests/backend/rag/test_library_routes.py` |
| En användares material läcker till en annan (isolation) | RLS `FORCE ROW LEVEL SECURITY` på `documents`/`document_chunks`/`knowledge_versions`/`knowledge_import_jobs`/`source_relationships`, plus explicit `owner_id`-filter i Python-koden som defense-in-depth (inte den enda gränsen — se `upsert_chunks()`s docstring) | `app/rls.py`, alla RAG-moduler |
| Projekt-läcka mellan projekt trots samma ägare | `project_id`-filter i `search()`/`hybrid_search()`/`retrieve_context()`, testat explicit | `vector_store.py`, `test_workbench.py` |
| Överdriven kontextstorlek (kostnad/prompt-injektion via massiva chunkar) | `top_k=5` (chatt/workbench) resp. begränsad `CHUNK_PREVIEW_LENGTH` i UI — hindrar inte fullständigt men begränsar blast radius | `chat.py`, `workbench.py`, `library.py` |

**Isolationstester som aktivt försöker bryta detta** (inte bara "finns med"-kontroller):
`test_rls_isolation.py` (6 nya tester denna session), `test_library_routes.py`, alla nya
GDPR-export-/workbench-tester verifierar explicit att en annan ägares data ALDRIG är med i
svaret, inte bara att den egna datan är det.

## Attackyta 3: Källhänvisning (citation) — MainAI som felkälla

Det här är skillnaden mellan en RAG-demo och ett system grundaren kan lita på. Kontrollerna
här handlar inte om extern attack utan om att modellen själv aldrig får ljuga om sitt eget
underlag.

| Hot | Kontroll | Var |
|---|---|---|
| Ett historiskt/föreslaget/omtvistat dokument presenteras som avgjord sanning | `assess_confidence()` tak:ar (aldrig höjer) konfidensen till "medium" när toppträffens `active_truth_status` inte är `active`; `build_trust_instructions()` bygger en explicit varningstext in i systemprompten (`STATUS_INSTRUCTIONS`) | `app/rag/trust.py` |
| Dold konflikt mellan två källor göms för grundaren | `detect_conflicts()` — flaggar ENDAST när en explicit `contradicts`-`SourceRelationship` finns OCH båda ändpunkterna faktiskt hämtades för det aktuella svaret. Medvetet INTE NLP-baserad textjämförelse — dokumenterad begränsning, inte en påstådd fullständig lösning | `trust.py` |
| Modellens egen självsäkerhet tas som bevis | Trust-nivån beräknas av `assess_confidence()` från källornas likhetspoäng och status — inte från vad modellen själv säger om sin egen säkerhet | `trust.py`, `chat.py` |
| Ett raderat dokument citeras ändå | Samma `deleted_at`-filter som attackyta 2 — retrieval kan aldrig hämta det, så det kan aldrig citeras heller | `vector_store.py` |
| Källa hittas i ett annat projekt än det frågan gäller | `project_id`-filter i retrieval, verifierat i chat- och workbench-tester | `chat.py`, `workbench.py` |
| Grundaren kan inte verifiera ett svar mot originalkällan | Varje `SourceRef` i svaret bär `document_id` + `active_truth_status`; frontend länkar direkt till `/library/{document_id}` från både chatt (`chat/page.tsx`) och workbench | `chat/page.tsx`, `workbench/page.tsx` |
| Hallucinerad källhänvisning (modellen hittar på en käll-titel som inte finns) | Källlistan i svaret kommer ALLTID från den faktiska retrieval-listan (`hits`), aldrig från modellens fritextsvar — modellen kan inte lägga till en källa som inte redan hämtades | `chat.py`, `workbench.py` |

**Obligatoriska testscenarier, alla täckta i `tests/backend/chat/test_chat_source_grounding.py`/
`test_workbench.py`:** en träff, flera samstämmiga källor, motstridiga källor, ingen relevant
källa, historiskt vs. aktivt dokument, raderad källa, isolation mellan användare. (Ett
scenario med en faktiskt trasig providerkedja täcks separat av providerarkitekturens egna
fallback-tester, inte omskrivet här.)

## Sammanfattning: vad som INTE är löst

- Ingen extern malware-skanning av importerat filinnehåll (kräver en tjänst som inte är
  aktiverad).
- `detect_conflicts()` upptäcker bara konflikter som redan är explicit annoterade som en
  `SourceRelationship` — upptäcker inte automatiskt att två texter säger emot varandra.
- Ingen påstående-nivå (`KnowledgeClaim`) trust-bedömning ännu (se DEL 8-avsnittet i
  `FOUNDER_KNOWLEDGE_STUDIO_V1.md`) — dagens trust-bedömning är per källa, inte per enskilt
  påstående inom en källa.
- Rate limiting skyddar mot volymbaserad kostnadsuttömning men inte mot en enskild extremt
  stor/dyr fråga (t.ex. en workbench-analys mot en väldigt stor kontext) — samma gräns
  (`top_k=5`) håller det begränsat men ingen hård token-/kostnadstak per anrop finns ännu
  (se DEL 14 i `FOUNDER_KNOWLEDGE_STUDIO_V1.md`).
