# Säker storfilsimport (mål: ≥2 GB per originalfil) — scoped implementeringsplan

**Status:** planeringsdokument, INGA produktionsgränser, scheman, Caddy- eller
containergränser ändrade av detta dokument eller denna korrigeringsrunda.
**Bakgrund:** en föreslagen PR som bara höjde Caddys `request_body max_size` från 30 MB till
65–70 MB stoppades innan den öppnades — grundarens riktiga filer kan vara runt **1,3 GB**, så
60 MB var aldrig den arkitektoniska målstorleken. Den första versionen av det här dokumentet
ersatte den punktfixen med en bredare plan (PR A–G), men den planen hade i sin tur flera
tekniska fel som identifierades i en granskningsrunda 2026-07-27 och som korrigeras här.
**Ingen av PR A–G ska påbörjas förrän den korrigerade versionen nedan är godkänd.**

Se även `docs/BRANCH_REGISTRY.md` (levande branch-/PR-karta), `docs/KNOWLEDGE_IMPORT_SECURITY.md`
(ZIP-hotmodellen som flera av gränserna nedan redan är dokumenterade i) och
`docs/LIFE_LIBRARY_PLAN.md` (den bredare Life Library-arkitekturen detta är en fördjupning av
importvägen för).

---

## 0. Korrigeringsrunda 2026-07-27 — vad som ändrades och varför

Den tekniska granskningen av den första versionen hittade sex sakfel/risker. Sammanfattat:

1. **ZIP-bearbetningen påstods vara strömmande genomgående — det är den inte.**
   `_read_with_hard_cap()` (`zip_import.py:183-208`) läser visserligen entryt i 64 KB-chunkar
   och avbryter tidigt om `max_bytes` överskrids, men ackumulerar varje chunk i
   `chunks: list[bytes]` och avslutar med `return b"".join(chunks)` — hela det (begränsade)
   uppackade entryt existerar som EN sammanhängande `bytes`-buffert i minnet vid returen.
   `ZipEntryResult.content` (`zip_import.py:93`) lagrar sedan detta `bytes`-värde per entry, och
   nästlade ZIP-filer öppnas via `zipfile.ZipFile(io.BytesIO(content))` (`zip_import.py:324`) —
   ytterligare en full kopia i minnet. Att bara höja `MAX_SINGLE_FILE_UNCOMPRESSED_BYTES` (PR D
   i den ursprungliga planen) utan att ändra det HÄR skulle återinföra exakt den OOM-risk hela
   arbetet ska lösa, bara vid en högre gräns. Åtgärdat i §2.2 nedan — ny spool-baserad design.
2. **PR-ordningen var riskabel.** Den ursprungliga planen lade resumable-upload-endpoints (PR B)
   FÖRE workerns minnesfix (PR C), med PR C beroende av PR B. Det hade kunnat resultera i ett
   läge där en 1–2 GB-fil KAN lagras via de nya endpointsen men INTE kan bearbetas säkert —
   exponerar en produktionskapabel stor uppladdningsväg innan mottagaren (workern) är
   minnessäker. PR C behöver dessutom inte vänta på PR B: ett test kan lägga en stor fil
   direkt i storage-backend (samma sak `open_read()` skulle läsa från) utan att gå via
   uppladdnings-API:t alls. Åtgärdat i §3 — workerns minnessäkerhet flyttad tidigare, och
   resumable-endpoints hålls avstängda/begränsade tills den är verifierad.
3. **Antagandet att Caddy måste höjas var för tidigt.** Med segment på ~8–16 MB ryms varje
   enskild HTTP-request redan under dagens 30 MB-gräns — helhetsgränsen (2 GB) ska ligga på
   `upload_sessions`-kontraktet, inte på en enskild request. Caddy höjs bara om uppmätt
   multipart-overhead eller det faktiskt valda segmentstorleken kräver det. Åtgärdat i §2.1
   och §3 (PR F).
4. **`received_chunk_indexes` som lista/bitmask i en sessionrad är svagt vid samtidiga
   anrop** — två parallella `PUT .../chunks/{index}`-anrop mot samma rad är en typisk
   race/överskrivningsrisk. Ersatt med en separat `upload_parts`-tabell, se §2.1.
5. **Testtäckningen dög inte.** Sparse nollfyllda filer är varken giltiga PDF:er, ZIP-filer,
   DOCX eller mediafiler — de bevisar inte att den riktiga bearbetningskoden (som gör riktiga
   format-specifika saker: sidparsning, XML, container-parsning, kompression) fungerar korrekt
   vid stor storlek. Värre: en ZIP fylld med stora mängder nollor har en extremt hög
   kompressionskvot och kan MED RÄTTA fångas av zip-bomb-skyddet (`MAX_COMPRESSION_RATIO=100`,
   `zip_import.py:50`) — testet hade alltså kunnat "bevisa" ett fel som egentligen är korrekt
   säkerhetsbeteende. Åtgärdat i §4.
6. **Påståendet "workerns minne blir oberoende av filstorlek" var för starkt.** `pypdf`,
   `python-docx`, ZIP-hanteringen och medietranskriberingspipelinen kan var för sig fortfarande
   ha internt minnesbruk proportionellt mot sidantal, objektstruktur, XML-DOM-storlek eller
   metadata — oberoende av om RÅDATAN strömmas in. Ersatt med ett mätbart krav per filtyp i
   §2.2 och §4.
7. **(Tillagt i granskningen, inte i den ursprungliga listan men en direkt konsekvens av #1
   och #6):** planen saknade explicita policyer per filkategori. En 2 GB video, en 2 GB ZIP med
   tusentals små projektfiler, och en enda 2 GB PDF har olika bearbetnings-, säkerhets- och
   leverantörsbegränsningar — "2 GB originaluppladdning" får INTE automatiskt tolkas som "2 GB
   tillåtet per uppackad ZIP-post". Ny §2.4 nedan.

---

## 1. Verifierade nuvarande flaskhalsar

Allt nedan är läst direkt ur den faktiska koden på `claude/det-kommer-mer-879lcm` — inget är
antaget. Rad 7 är korrigerad i denna omgång (se §0 punkt 1).

| # | Komponent | Fil:rad | Nuvarande gräns/beteende | Problem vid 1,3–2 GB |
|---|---|---|---|---|
| 1 | Caddy ingress | `Caddyfile:28-30` | `request_body { max_size 30MB }`, inga explicita `timeouts` satta | Bindande gräns PER REQUEST idag — men se §0 punkt 3: med segmenterad uppladdning är detta inte nödvändigtvis ett problem längre |
| 2 | Backend uppladdningsväg | `backend/app/routers/library.py:53` | `MAX_UPLOAD_BYTES = 60 * 1024 * 1024` | Gäller idag hela originalfilen i EN request — ersätts konceptuellt av en `declared_total_bytes`-gräns på sessionsnivå (§2.1), inte en enskild requests storlek |
| 3 | Next.js-proxy | `frontend/app/api/[...path]/route.ts:73-77` | `body: request.body`, `duplex: "half"` | **Redan korrekt** — strömmar rakt igenom utan buffring. Inte en flaskhals. |
| 4 | Backend-container | `docker-compose.vps.yml:160-161` | `mem_limit: 512m` | Inte i sig ett problem SÅ LÄNGE ingen kod i backend-processen buffrar hela filen (se rad 5) |
| 5 | Storage-backend (uppladdningsfasen) | `backend/app/storage/local_fs.py:53-...` (`write_stream`) | Strömmar i 1 MB-chunkar till en temp-fil, inkrementell sha256, kontrollerar storlek LÖPANDE (`StorageSizeLimitExceeded` mitt i strömmen, inte efter full inläsning) | **Redan korrekt arkitektur** — mönstret resten av kedjan ska följa, inte ersättas |
| 6 | Workerns läsning av originalfilen | `backend/app/rag/library_import.py:574-575` | `with storage.open_read(...) as f: raw = f.read()` | Läser HELA originalfilen till RAM. En 1,3 GB-fil i en container med `mem_limit: 384m` (`docker-compose.vps.yml:220`) dödas av OOM långt innan bearbetning ens börjar. |
| 7 | ZIP-avpackning — **KORRIGERAD** | `zip_import.py:397` (öppning), `zip_import.py:183-208` (`_read_with_hard_cap`), `zip_import.py:89-93` (`ZipEntryResult.content`), `zip_import.py:324` (nästlad öppning) | `zipfile.ZipFile(io.BytesIO(raw))` kräver hela ZIP-filen i minnet. `_read_with_hard_cap()` läser varje entry i 64 KB-chunkar men samlar dem i `chunks: list[bytes]` och returnerar `b"".join(chunks)` — INTE strömmande hela vägen, bara en tidig avbrottskontroll under läsningen. Varje entry hålls sedan som `content: bytes`. Nästlade ZIP:ar öppnas via `io.BytesIO(content)`. | Fyra separata fullständiga minneskopior i värsta fall (ytterarkivet, ett entry, ett nästlat arkiv, dess egna entries) — att bara höja de numeriska taken utan att ändra denna kod återinför OOM-risken vid en HÖGRE gräns |
| 8 | ZIP-säkerhetskonstanter | `zip_import.py:47-57` | `MAX_FILES=500`, `MAX_TOTAL_UNCOMPRESSED_BYTES=200MB` (delat över nästling), `MAX_SINGLE_FILE_UNCOMPRESSED_BYTES=25MB` (per fil, varje nivå), `MAX_NESTING_DEPTH=3`, `MAX_COMPRESSION_RATIO=100` | Redan lägre än dagens 60 MB-gräns i vissa fall, långt under 2 GB-målet — men se §2.4: höjning måste vara filkategori-medveten, inte en enkel linjär skalning |
| 9 | Textextraktion | `backend/app/rag/extract.py:9-17` | `extract_text(filename, raw: bytes)` — `PdfReader(BytesIO(raw))`, `DocxDocument(BytesIO(raw))` | Tar `bytes`, inte path/stream — ytterligare en full kopia ovanpå rad 6. Se §2.2 för varför en path-parameter INTE per automatik löser bibliotekens EGNA interna minnesbruk. |
| 10 | Media-import (ljud/video) | `backend/app/rag/media_import.py:71` | `validate_media_bytes(filename, content: bytes, media_kind)` | Samma bytes-baserade mönster — STEG 12 byggdes för dagens små filer |
| 11 | Disk-kvot/utrymme | `backend/app/routers/library.py:457-461` | `shutil.disk_usage()` beräknas och EXPONERAS via `GET /api/library/ops/status`, används INTE som gate | Inget stoppar en uppladdning som skulle fylla disken |
| 12 | Återupptagning vid avbrott | Ingen kod hittad | En `POST /api/library/import` är ETT enda multipart-anrop | Ett avbrott tvingar en helt ny överföring av allt |
| 13 | Progress inom EN fils bearbetning | `ImportJob.progress_current`/`progress_total` (`import_job.py:56-57`) | Uppdateras per FIL i en ZIP, inte per byte/chunk inom en enskild stor fils extraktion | Ingen delframgång syns för en enda stor fil under hela bearbetningen |

---

## 2. Föreslagen arkitektur

### 2.1 Uppladdningsfasen (klient → durabel lagring)

- **Resumable/chunked upload**, inte ett enda multipart-POST. Klienten delar filen i fasta
  delar ("parts", t.ex. 8–16 MB — exakt värde olöst, se §6), laddar upp varje del för sig mot
  ett `upload_session_id`, och kan fråga servern vilka delar som redan finns efter ett avbrott.
- **Caddy höjs INTE per automatik.** Med 8–16 MB-delar plus multipart-overhead ryms varje
  enskild HTTP-request redan bekvämt under dagens `request_body max_size 30MB` — det finns
  ingen a priori anledning att röra Caddy alls. 2 GB-gränsen är en egenskap hos
  `upload_sessions`-kontraktet (summan av alla mottagna delar), INTE hos en enskild HTTP-request.
  Caddy höjs bara om PR F:s mätning av verklig multipart-overhead vid det slutgiltigt valda
  delstorlek visar att det faktiskt behövs — ett beslut som fattas med data, inte i förväg.
- **Durabelt uppladdningstillstånd, concurrency-säkert.** Två tabeller, inte en enda
  sessionsrad med en lista/bitmask:
  - `upload_sessions`: `id`, `owner_id`, `declared_total_bytes`, `declared_filename`,
    `declared_checksum_sha256` (valfri, av klienten uppgiven hel-fils-checksumma för tidig
    integritetskontroll vid finalisering), `part_size`, `status`
    (`reserved`/`in_progress`/`finalizing`/`completed`/`aborted`/`expired`), `created_at`,
    `expires_at`, `quota_reserved_bytes` (se nedan), `finalized_at`.
  - `upload_parts`: **unikt** `(session_id, part_index)`, `expected_bytes`, `received_bytes`,
    `checksum_sha256` (per del), `storage_key` (delens egen temporära lagringsplats),
    `status` (`pending`/`received`/`verified`), `received_at`. Unik-constrainten på
    `(session_id, part_index)` gör en samtidig dubbel-inlämning av samma del till ett
    databaskonflikt istället för en tyst race — hanteras som "redan mottagen, jämför
    checksumma" (se idempotens nedan), aldrig en tyst överskrivning.
  - **Idempotent retry:** ett `PUT`-anrop mot en redan mottagen del med SAMMA checksumma är en
    no-op (returnerar 200/samma resultat); en `PUT` mot en redan mottagen del med EN ANNAN
    checksumma avvisas explicit (`409 Conflict`) — en redan färdig del får aldrig tyst skrivas
    över med annat innehåll.
  - **Sessionsägarskap:** varje del-operation kräver att den anropande användaren äger
    `upload_session`-raden (samma RLS-mönster som redan gäller `ImportJob`/`Document`) — en
    session kan aldrig kompletteras eller läsas av en annan användare.
  - **Atomisk finalisering:** `POST .../finalize` tar ett DB-lås (samma `SELECT ... FOR UPDATE`-
    mönster som `app/worker.py`s `claim_next_job` redan använder) på sessionsraden, verifierar
    ATT alla deklarerade delar finns, ATT deras `expected_bytes`-summa matchar
    `declared_total_bytes`, och ATT en eventuell `declared_checksum_sha256` matchar den
    beräknade helhetssumman — INNAN delarna konkateneras till en slutgiltig lagrad blob via
    samma `write_stream()`-mönster (strömmande, aldrig hela filen i minnet på en gång) som
    redan finns i `local_fs.py`. En redan slutförd session som finaliseras igen är en no-op
    (samma resultat returneras), inte ett fel.
  - Varje mottagen del skrivs direkt till sin egen temporära fil via samma streamade,
    chunkade mönster som `write_stream()` redan använder — aldrig buffrad i minnet.
- **Disk-kvot: reservation, inte bara kontroll.** Vid `POST /api/library/upload-sessions`
  (session-skapande) reserveras `declared_total_bytes` mot tillgängligt utrymme
  (`shutil.disk_usage()`, redan beräknat i `library.py:459` men aldrig använt som gate) INNAN
  någon data tas emot — avvisas tidigt om det inte får plats. Reservationen hålls i
  `quota_reserved_bytes` och släpps (frigörs) explicit vid `completed`/`aborted`/`expired` —
  annars skulle flera samtidiga stora sessioner kunna över-boka samma fysiska utrymme mellan
  varandra, eftersom en ren "kontrollera vid varje del"-modell inte skyddar mot att TVÅ
  sessioner startar samtidigt och båda ser samma "ledigt utrymme".
- **Städning av övergivna uppladdningar:** ett periodiskt jobb (samma mönster som
  `app/cleanup.py`s befintliga städjobb) tar bort `upload_sessions`/`upload_parts` och deras
  partiella data som passerat `expires_at` utan att slutföras, och frigör kvotreservationen.

### 2.2 Bearbetningsfasen (workern) — spool-baserad, inte bara "path istället för bytes"

Den ursprungliga planens formulering ("skicka en path/filhandle istället för bytes") var för
enkel — se §0 punkt 1 och 6. Den korrigerade designen:

- **ZIP — ny pipeline:**
  - Ytterarkivet öppnas mot en path/filhandle (`zipfile.ZipFile` stöder detta direkt) — `raw`
    som en fullständig `bytes`-variabel ska aldrig existera, varken för ytterarkivet eller ett
    nästlat.
  - Varje entry strömmas till en **bounded spool-fil** (en temporär fil på disk, inte en
    `bytes`-lista) MEDAN den läses — inkrementell sha256-checksumma och magic-byte-inspektion
    (`_magic_bytes_ok()`, redan finns) körs LÖPANDE på de första lästa bytesen, inte efter att
    hela entryt är i minnet. `_read_with_hard_cap()` skrivs om att skriva varje chunk till
    spool-filen istället för `chunks.append(chunk)`, och returnerar en path/filhandle till
    spool-filen istället för `b"".join(chunks)`.
  - `ZipEntryResult.content: bytes` ersätts av `ZipEntryResult.content_path: Path` (eller en
    öppnad filhandle) — allt nedströms (extraktion, dokumentskapande) tar emot en path istället
    för att förvänta sig `bytes`.
  - **Nästlade ZIP:ar** öppnas mot sin egen spool-fil (`zipfile.ZipFile(spool_path)`), aldrig
    `io.BytesIO(content)` — samma princip rekursivt, oavsett nästlingsdjup.
  - Spool-filer städas deterministiskt (try/finally) oavsett om entryt accepteras, avvisas,
    eller om ett fel kastas mitt i — annars läcker temporära filer vid varje avbruten import.
  - Det EXISTERANDE tvålagers zip-bomb-försvaret (kompressionskvot-förfilter INNAN
    avpackning, sedan `_read_with_hard_cap()`s löpande hård gräns) förblir oförändrat som
    MEKANISM — bara lagringsmediet för det redan strömmade innehållet (spool-fil istället för
    minne) och de numeriska taken (§2.4) ändras.
- **Textextraktion (PDF/DOCX):** `extract.py` skrivs om att ta en path/filhandle. Detta löser
  RÅDATA-kopian (`BytesIO(raw)` görs onödig), men **löser INTE automatiskt** `pypdf`s eller
  `python-docx`s EGNA interna minnesbruk — båda biblioteken bygger interna objektträd/DOM-
  liknande strukturer proportionella mot sid-/elementantal, inte bara mot rådatans byteantal.
  En 2 GB PDF med extremt många sidor/objekt kan alltså fortfarande vara minneskrävande även
  utan att rådatan buffras. Detta MÅSTE mätas separat (se §4) — inte antas löst av att bara
  byta `bytes`-parametern mot en path.
- **Ljud/video:** `media_import.py` skrivs om att ta en path. Precis som PDF/DOCX ovan,
  transkriptionsbibliotekets/providerns EGNA minnes-/uppladdningsbeteende måste utredas separat
  — okänt i skrivande stund exakt vilken transkriptionsprovider som anropas idag och om den
  stöder streaming-uppladdning eller har en egen filstorleksgräns (flaggat i §6).
- **Mätbart minneskrav, inte ett löfte om oberoende:** för varje filkategori (PDF, DOCX, TXT/MD/
  JSON/HTML, ZIP, ljud, video) definieras ett **max observerat RSS** vid den nya målstorleken,
  uppmätt i ett riktigt test (§4), och verifierat att ligga tydligt under containerns
  `mem_limit`. Om ett bibliotek visar sig ha en oacceptabel intern minnesprofil för en given
  kategori (t.ex. `pypdf` vid extremt sidantal) är svaret INTE att anta att det löser sig — det
  är ett eget, dokumenterat beslut (byta bibliotek, sätta en lägre kategori-specifik gräns för
  just den typen, eller strömma sidvis om biblioteket stöder det) som tas med mätdata i handen.

### 2.3 Progress, avbrott, återupptagning

- **Uppladdning:** progress rapporteras per mottagen `upload_parts`-rad
  (`received_bytes`/`declared_total_bytes`) — trivialt att exponera i UI på sessionsnivå.
- **Bearbetning:** `ImportJob.progress_current`/`progress_total` utökas till att även kunna
  representera bytes-/sidgenomströmning INOM en enskild stor fils extraktion, inte bara antal
  filer i en ZIP.
- **Avbrott/cancellation:** `JobCancelled`-mönstret (`library_import.py:64-67`) utökas till att
  kontrolleras med samma frekvens som progress uppdateras (per chunk/del, inte bara per fil).
- **Retry/omstart:** `app/worker.py`s befintliga lease-/claim-mekanism (`JobLock`,
  `renew_lease`, `lease_expires_at`) återanvänds oförändrat för jobbnivå-krascher. En krasch
  mitt i en påbörjad UPPLADDNING (inte bearbetning) återupptas via `upload_sessions`/
  `upload_parts` (§2.1) — separat från jobbnivå-återupptagningen.

### 2.4 Policyer per filkategori — ny, svarar på §0 punkt 7

"2 GB originaluppladdning" är INTE samma sak som "2 GB tillåtet per uppackad ZIP-post" eller
"2 GB video hanteras likadant som 2 GB PDF". Minst tre distinkta fall måste ges egna,
explicita policyer (exakta tal är olösta produktbeslut, se §6 — det som fastställs HÄR är att
de måste vara SEPARATA beslut, inte en enda global konstant):

| Fall | Vad som är olikt | Vad som måste beslutas separat |
|---|---|---|
| En enda stor fil (t.ex. 2 GB PDF eller video) | Ett extraktionsanrop, en leverantör (om alls) | Max storlek för DEN filtypen specifikt (kan skilja sig från det generella `declared_total_bytes`-taket om t.ex. transkriptionsprovidern har en lägre egen gräns) |
| En ZIP med FÅ, STORA filer (t.ex. en handfull 500 MB-videor i ett paket) | `MAX_SINGLE_FILE_UNCOMPRESSED_BYTES` (per-entry-taket) blir den styrande gränsen, inte `MAX_TOTAL_UNCOMPRESSED_BYTES` | Om per-entry-taket ska tillåta en enskild ZIP-post lika stor som en fristående uppladdning, eller om ZIP-poster ska ha ett LÄGRE eget tak av försiktighetsskäl |
| En ZIP med TUSENTALS SMÅ filer som tillsammans når 2 GB | `MAX_FILES=500` (idag) blir den styrande gränsen långt innan totalbytes gör det | Om `MAX_FILES` ska höjas, och i så fall hur mycket, utan att öppna för en fil-antal-baserad resursuttömningsattack (oberoende av total bytestorlek) |

Ingen av dessa tre fall får en gemensam, odiskuterad konstant i den slutgiltiga
implementationen (PR D) — varje tak (`MAX_TOTAL_UNCOMPRESSED_BYTES`,
`MAX_SINGLE_FILE_UNCOMPRESSED_BYTES`, `MAX_FILES`) motiveras för sig i PR D:s beskrivning.

### 2.5 Vad som INTE ska ändras

- `frontend/app/api/[...path]/route.ts`s streamade proxy — redan korrekt.
- `backend/app/storage/local_fs.py`s `write_stream()` — redan korrekt, blir mallen för
  del-mottagning OCH för finaliseringens konkatenering.
- Content-addresserad lagring (`{sha256[:2]}/{sha256}`) och path-traversal-skyddet i
  `_resolve()` — oförändrat.
- Det tvålagers zip-bomb-försvaret som MEKANISM (kompressionskvot-förfilter, delat
  budgetobjekt, nästlingstak) — bara lagringsmediet (spool-fil) och de numeriska taken ändras,
  aldrig mekanismen själv.

---

## 3. PR-uppdelning i säker implementationsordning (korrigerad)

Grundprincip, skärpt efter granskningen: **workerns minnessäkerhet (nu PR B) kommer FÖRE någon
produktionskapabel, obegränsad resumable-uppladdningsväg.** PR B behöver inte vänta på en
uppladdnings-API — dess tester seedar en stor fil direkt i storage-backend.

| PR | Innehåll | Beroende av | Ändrar produktionsgränser? |
|---|---|---|---|
| **A** | Design-/gränskontrakt: exakt request-/response-format för resumable upload, `upload_sessions`/`upload_parts`-schema, delstorlek (§6), Caddy-timeout-granskning (§6), och de filkategori-policyerna (§2.4) som ANNU INTE har konkreta tal men vars STRUKTUR (separata beslut per kategori) läggs fast här | Inget | Nej |
| **B** | Worker: ta bort `raw = f.read()` och `chunks: list[bytes]`/`ZipEntryResult.content: bytes`/nästlad `io.BytesIO` genomgående — spool-baserad ZIP-pipeline (§2.2), path-baserad textextraktion/mediaimport. Tester seedar en stor fil DIREKT i storage-backend (ingen uppladdnings-API krävs). Mätbart RSS-krav per filtyp (se §4) | Inget (kan seeda storage direkt) | Nej — samma 60 MB/30 MB gränser gäller fortfarande, bara den interna bearbetningen blir minnessäker för den storlek som redan tillåts |
| **C** | Backend: `upload_sessions`/`upload_parts`-modeller + migration, del-mottagnings-endpoints, kvotreservation, städjobb. **Måste vara feature-flaggad och hårt begränsad** (t.ex. samma 60 MB-tak som idag) tills PR B är verifierad grön i produktion — annars kan en stor fil lagras innan den kan bearbetas säkert | A, B (får inte höja sitt eget tak förrän B är bevisat) | Nej — ny väg, `/api/library/import` orörd, gamla klienter opåverkade |
| **D** | ZIP-säkerhetskonstanter: filkategori-medvetna tak per §2.4 (inte en enda linjär skalning) — `MAX_TOTAL_UNCOMPRESSED_BYTES`, `MAX_SINGLE_FILE_UNCOMPRESSED_BYTES`, ev. `MAX_FILES`, var och en explicit motiverad | B (spool-designen måste finnas innan taken höjs) | Ja — men bara ZIP-interna tak |
| **E** | Progress/avbrott: bytes-/sidnivå progress inom en enskild stor fils bearbetning, tätare cancellation-kontroll | B | Nej |
| **F** | Höj de faktiska gränserna TILLSAMMANS, med mätdata i handen: `upload_sessions.declared_total_bytes`-taket (2 GB), ta bort C:s tillfälliga begränsning, HÖJ Caddys `max_size` ENDAST OM uppmätt multipart-overhead vid vald delstorlek visar att det faktiskt krävs (annars lämnas den OFÖRÄNDRAD på 30 MB som en fortsatt per-request-gräns), ev. `mem_limit`-justeringar om B:s profilering visar att 384m/512m inte räcker | B, C (obegränsad), D | Ja — enda PR:n som höjer den faktiska helhetstaket, görs sist |
| **G** | Dokumentation: `docs/VPS_ARCHITECTURE.md`, `docs/KNOWLEDGE_IMPORT_SECURITY.md`, `docs/VPS_DOCKER_HARDENING.md` uppdateras samlat | F | Nej |

---

## 4. Tester per PR (korrigerad teststrategi)

**Generell princip (svar på §0 punkt 5):** snabba, deterministiska PR-tester använder GILTIGA,
små-men-realistiska genererade fixtures (en riktig liten PDF/DOCX/ZIP/ljudfil producerad med
samma typ av bibliotek som skapar riktiga sådana filer, inte sparse nollor) — dessa körs på
VARJE PR. En separat, tung, verklighetstrogen ~1,3–2 GB-test körs mer sällan (t.ex. manuellt
eller i en egen, längre CI-körning), inte som en del av den vanliga snabba svängen.

- **A:** inga körbara tester — granskas manuellt av grundaren.
- **B:**
  - *Snabba PR-tester:* seeda storage-backend direkt (ingen uppladdnings-API) med giltiga,
    små men strukturellt realistiska fixtures per typ (en riktig flersidig PDF genererad med
    t.ex. `reportlab`/`pypdf`, en riktig DOCX med `python-docx`, en riktig ZIP med flera
    filer och en rimlig, ICKE zip-bomb-liknande kompressionskvot) — bekräftar att den nya
    spool-/path-baserade koden producerar SAMMA extraktionsresultat som innan omskrivningen.
  - *Tung, verklighetstrogen minnesprofilering (separat körning, inte varje PR):* generera en
    GILTIG stor fixture per kategori — t.ex. en flersidig PDF med tillräckligt många sidor för
    att nå ~1–2 GB, en ZIP med många giltiga (ej degenererade) filer som tillsammans når
    målstorleken utan att trigga kompressionskvot-förfiltret av fel anledning, en riktig
    (om än syntetisk) video/ljudfil av rätt längd — mät faktisk RSS under HELA bearbetningen
    och assertera att den håller sig under ett dokumenterat max per kategori (§2.2), tydligt
    under `mem_limit`. Genererade filer committas ALDRIG till repot — skapas i CI/lokalt vid
    körtillfället.
  - Explicit negativt test: en RIKTIG zip-bomb (hög kompressionskvot på en LITEN fil) fortsätter
    avvisas av det oförändrade förfiltret — bekräftar att spool-omskrivningen inte råkat
    försvaga den befintliga detektionen.
- **C:** migrationstest (upp/ned), enhetstester för del-mottagning (`upload_parts`-unik-
  constrainten, idempotent retry med samma checksumma, `409` vid avvikande checksumma på en
  redan mottagen del, saknade delar vid finalisering avvisas, atomisk finalisering under
  samtidiga finaliseringsförsök), städjobbstest, kvotreservationstest (en reservation som
  överskrider ledigt utrymme avvisas TIDIGT; en frigjord reservation gör utrymmet tillgängligt
  för nästa session), samt ett explicit test att C:s EGEN gräns (den tillfälliga begränsningen
  tills B är verifierad) faktiskt gäller.
- **D:** ZIP-säkerhetstest med de HÖJDA, filkategori-medvetna taken — bekräfta att en riktig
  zip-bomb fortfarande avvisas vid de nya gränserna för VARJE kategori i §2.4:s tabell separat
  (få-stora-filer-fallet, många-små-filer-fallet), inte bara en generisk höjning. Regressionskör
  de befintliga P2-testerna.
- **E:** progressuppdateringar syns med rimlig frekvens under lång bearbetning; en avbruten
  stor fil stoppar inom en kort, testad tidsram.
- **F:** end-to-end: en riktig ~1,3–2 GB GILTIG fil (inte sparse-nollor) laddas upp via HELA
  den nya resumable-vägen (inklusive ett simulerat avbrott och återupptagning), genom Caddy,
  Next.js-proxyn, backend, och blir `indexed` av workern. Bekräfta ÄVEN att en session vars
  `declared_total_bytes` överskrider den nya gränsen avvisas TIDIGT (vid sessionsskapande, inte
  efter att data börjat strömma), och att Caddy fortfarande avvisar en enskild DEL som
  överskrider sin egen (o-höjda, om F inte höjde den) `max_size`.
- **G:** ingen kod att testa — verifiera att dokumentationen stämmer mot då gällande kod.

---

## 5. Migrations- och rollback-plan (uppdaterad för ny PR-ordning)

- **B:** ingen schemaändring — ren kodrefaktorering av bearbetningsvägen. Rollback: revert:a
  PR:n; `/api/library/import` fortsätter fungera exakt som innan.
- **C:** en ny, additiv Alembic-migration (`upload_sessions` + `upload_parts`) — ingen ändring
  av befintliga tabeller. Rollback: `alembic downgrade` tar bort båda tabellerna; inga
  befintliga `ImportJob`/`Document`-rader påverkas.
- **D:** en konstant-ändring i `zip_import.py` — ingen datamigration. Rollback: sänk
  konstanterna tillbaka.
- **F:** enda PR:n med faktisk produktionspåverkan på HELHETSTAKET. Deploy-sekvens: 1) deploya
  B+C(begränsad)+D, verifiera stabilt i produktion, 2) deploya F separat (tar bort C:s
  tillfälliga begränsning, höjer ev. Caddy om mätdata kräver det). Rollback för F: återinför
  C:s begränsning och/eller sänk Caddys `max_size` tillbaka — ingen datamigration krävs.
- Gammal `/api/library/import` (enfas-uppladdning) kan hållas kvar oförändrad parallellt under
  en övergångsperiod (se §6).

---

## 6. Risker och olösta beslut (grundarbeslut, inte tekniska detaljer)

- **Delstorlek för resumable upload:** 8–16 MB föreslaget, inget bestämt värde. Beslutas i
  PR A:s kontraktsgranskning, tillsammans med Caddy-timeout-granskningen (se nedan).
- **Caddy-timeouts:** inga explicita `timeouts` satta i `Caddyfile` idag. Caddys
  defaultbeteende för en långsam (men inom storleksgränsen) del-uppladdning måste verifieras
  konkret innan PR C:s del-endpoints går i produktion — annars riskerar en legitim men
  långsam del på en svag uppkoppling att kapas av ett odokumenterat default-timeout.
- **Filkategori-specifika tak (§2.4):** de EXAKTA talen (per-entry-tak för en ZIP-post, om det
  ska matcha eller understiga en fristående uppladdnings tak; om/hur mycket `MAX_FILES` ska
  höjas) är olösta produktbeslut, inte tekniska — se §2.4:s tabell.
- **Transkriptionsproviderns egna gränser:** okänt i skrivande stund exakt vilken extern
  provider `media_import.py` anropar och om DEN har en egen filstorleksgräns långt under
  1,3 GB. Måste utredas konkret innan PR B:s scope för media-kategorin låses.
- **Bibliotekens interna minnesprofil (pypdf/python-docx):** om ett bibliotek visar oacceptabelt
  minnesbruk vid stort sid-/objektantal (uppmätt i PR B:s tester), krävs ett eget beslut —
  byta bibliotek, sänka just den kategorins tak, eller sidvis strömning om möjligt. Inte löst
  i förväg av det här dokumentet.
- **Disk-kapacitet på VPS:en i absoluta tal:** ingen dokumenterad avsiktlig gräns för hur många
  samtidiga stora sessioner VPS:en ska klara innan den byggs om (större disk/extern object
  storage) — utanför scope, men en framtida skalningsgräns värd att notera.
- **Ska gamla `/api/library/import` (enfas) tas bort eller behållas som fallback för korta
  filer** (t.ex. under 10 MB)? Olöst, produktbeslut för PR F.

---

## 7. Slutgiltiga acceptanskriterier — innan PR F får höja någon produktionsgräns

Samtliga punkter nedan måste vara sanna och VERIFIERADE (inte antagna) innan PR F:s
gränshöjning (inklusive en eventuell Caddy-höjning) går i produktion:

1. PR B:s mätbara RSS-krav per filkategori (§2.2, §4) är uppmätt och dokumenterat, och ligger
   tydligt under workerns `mem_limit` — för PDF, DOCX, ZIP (både få-stora- och
   många-små-filer-fallet), och media separat.
2. Ingen kod i den aktiva importvägen håller längre en fullständig originalfil eller ett
   fullständigt ZIP-entry som en sammanhängande `bytes`-variabel i minnet (verifierat genom
   kodgranskning av exakt de call-sites §0/§1 identifierade, inte bara ett allmänt intryck).
3. Det befintliga tvålagers zip-bomb-försvaret fortsätter fånga en riktig zip-bomb vid de NYA,
   höjda taken — inte bara vid de gamla.
4. PR C:s resumable-uppladdningsväg har körts med sin tillfälliga, låga begränsning i
   produktion under en verifieringsperiod innan PR F tar bort den begränsningen.
5. `upload_parts`-designens concurrency-egenskaper (unik constraint, idempotent retry,
   atomisk finalisering) är testade under faktisk samtidighet, inte bara sekventiellt.
6. Caddys `max_size` höjs ENDAST om ett uppmätt värde (verklig multipart-overhead vid det
   valda delstorlek) visar att 30 MB faktiskt är otillräckligt för en enskild del — annars
   förblir den oförändrad.
7. Ett verkligt, giltigt (inte sparse-nollor) 1,3–2 GB-filtest per relevant kategori har körts
   genom hela den nya vägen end-to-end och når `indexed`.

---

## 8. Vad som INTE görs nu

Per grundarens explicita instruktion: **inga produktionsgränser, scheman, Caddy- eller
containerkonfigurationer ändras av det här dokumentet eller denna korrigeringsrunda.** Caddys
`request_body max_size` förblir 30 MB, `library.py`s `MAX_UPLOAD_BYTES` förblir 60 MB, och
`zip_import.py`s konstanter förblir oförändrade tills PR B (workerns minnessäkerhet) är byggd,
granskad och verifierad — före PR C, D och F, i den ordningen i §3.

**Produktionstest av den redan mergade uppladdnings-VOLYMFIXEN (PR #27):** ska även
fortsättningsvis köras med en LITEN fil (några MB) — en 1,3 GB-fil stöds inte säkert förrän
den korrigerade storfilsarkitekturen ovan är implementerad och PR F är mergad.
