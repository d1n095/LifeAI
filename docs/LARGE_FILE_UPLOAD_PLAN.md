# Säker storfilsimport (mål: ≥2 GB per originalfil) — scoped implementeringsplan

**Status:** planeringsdokument, INGA produktionsgränser ändrade av detta dokument.
**Bakgrund:** en föreslagen PR som bara höjde Caddys `request_body max_size` från 30 MB till
65–70 MB stoppades innan den öppnades. Grundarens riktiga filer kan vara runt **1,3 GB** —
60 MB (dagens faktiska backend-gräns) är alltså inte i närheten av den arkitektoniska
målstorleken, och en liten Caddy-höjning hade bara flyttat felet ett steg nedströms (till
backend, sedan till workern) utan att lösa något. Det här dokumentet ersätter den planen med
en fullständig genomgång av hela den verkliga vägen en stor fil måste ta, innan någon gräns
höjs.

Se även `docs/BRANCH_REGISTRY.md` (levande branch-/PR-karta), `docs/KNOWLEDGE_IMPORT_SECURITY.md`
(ZIP-hotmodellen som flera av gränserna nedan redan är dokumenterade i) och
`docs/LIFE_LIBRARY_PLAN.md` (den bredare Life Library-arkitekturen detta är en fördjupning av
importvägen för).

---

## 1. Verifierade nuvarande flaskhalsar

Allt nedan är läst direkt ur den faktiska koden på `claude/det-kommer-mer-879lcm` — inget är
antaget.

| # | Komponent | Fil:rad | Nuvarande gräns/beteende | Problem vid 1,3 GB |
|---|---|---|---|---|
| 1 | Caddy ingress | `Caddyfile:28-30` | `request_body { max_size 30MB }`, inga explicita `timeouts` satta | Stoppar redan vid 30 MB — den lägsta, alltså bindande, gränsen idag |
| 2 | Backend uppladdningsväg | `backend/app/routers/library.py:53` | `MAX_UPLOAD_BYTES = 60 * 1024 * 1024` | Andra bindande gränsen — men **strömmas redan korrekt** (se rad 5) |
| 3 | Next.js-proxy | `frontend/app/api/[...path]/route.ts:73-77` | `body: request.body`, `duplex: "half"` | **Redan korrekt** — strömmar rakt igenom utan buffring. Inte en flaskhals. |
| 4 | Backend-container | `docker-compose.vps.yml:160-161` | `mem_limit: 512m` | Inte i sig ett problem SÅ LÄNGE ingen kod i backend-processen buffrar hela filen (se rad 5 — det gör den inte längre) |
| 5 | Storage-backend (uppladdningsfasen) | `backend/app/storage/local_fs.py:53-...` (`write_stream`) | Strömmar i 1 MB-chunkar till en temp-fil, inkrementell sha256, kontrollerar storlek LÖPANDE (`StorageSizeLimitExceeded` mitt i strömmen, inte efter full inläsning) | **Redan korrekt arkitektur** — detta är mönstret resten av kedjan ska följa, inte ersättas |
| 6 | Workerns läsning av originalfilen | `backend/app/rag/library_import.py:574-575` | `with storage.open_read(...) as f: raw = f.read()` | Läser HELA originalfilen till RAM. En 1,3 GB-fil i en container med `mem_limit: 384m` (`docker-compose.vps.yml:220`) dödas av OOM långt innan bearbetning ens börjar. |
| 7 | ZIP-avpackning | `backend/app/rag/zip_import.py:397` | `zipfile.ZipFile(io.BytesIO(raw))` | Kräver hela ZIP-filen i minnet som `bytes` INNAN `zipfile` ens kan öppna den — samma OOM-risk som rad 6, en nivå djupare |
| 8 | ZIP-säkerhetskonstanter | `zip_import.py:47-57` | `MAX_FILES=500`, `MAX_TOTAL_UNCOMPRESSED_BYTES=200MB` (delat över nästling), `MAX_SINGLE_FILE_UNCOMPRESSED_BYTES=25MB` (per fil, varje nivå), `MAX_NESTING_DEPTH=3` | Dessa är redan LÄGRE än ens dagens 60 MB-gräns i vissa fall, och långt under ett 2 GB-mål — måste omvärderas som en DEL av det här arbetet, inte bara toppnivågränsen |
| 9 | Textextraktion | `backend/app/rag/extract.py:9-17` | `extract_text(filename, raw: bytes)` — `PdfReader(BytesIO(raw))`, `DocxDocument(BytesIO(raw))` | Tar `bytes`, inte en path/stream — ytterligare en fullständig kopia av filinnehållet i minnet ovanpå rad 6 |
| 10 | Media-import (ljud/video) | `backend/app/rag/media_import.py:71` | `validate_media_bytes(filename, content: bytes, media_kind)` | Samma bytes-baserade mönster — STEG 12 byggdes för dagens små filer, inte för 1,3 GB-video |
| 11 | Disk-kvot/utrymme | `backend/app/routers/library.py:457-461` | `shutil.disk_usage()` beräknas och EXPONERAS via `GET /api/library/ops/status`, men används INTE som en förhandskontroll eller per-användare-kvot innan/under en uppladdning | Inget stoppar en uppladdning som skulle fylla disken, bara observerbarhet i efterhand |
| 12 | Återupptagning vid avbrott | Ingen kod hittad | En `POST /api/library/import` är ETT enda multipart-anrop; avbryts klienten mitt i finns inget protokoll för att återuppta — hela uppladdningen måste börja om | Ett avbrott efter t.ex. 1,2 GB av 1,3 GB tvingar en helt ny överföring av allt |
| 13 | Progress under EN fils bearbetning | `ImportJob.progress_current`/`progress_total` (`backend/app/models/import_job.py:56-57`) | Uppdateras per FIL i en ZIP (flera filer), inte per byte/chunk INOM en enskild stor fils extraktion | En användare som laddar upp EN 1,3 GB-video ser ingen delframgång förrän hela extraktionen är klar |

**Sammanfattning:** Next.js-proxyn och `local_fs.py`s `write_stream()` är redan rätt
byggda — strömmande, ingen full buffring, inkrementell integritetskontroll. Allt FRÅN och MED
`library_import.py:574` (`raw = f.read()`) och nedströms (ZIP-avpackning, textextraktion,
mediaimport) är byggt för filer i tiotals MB, inte gigabyte, och måste skrivas om innan någon
gräns högre upp i kedjan höjs meningsfullt.

---

## 2. Föreslagen arkitektur

### 2.1 Uppladdningsfasen (klient → durabel lagring)

- **Resumable/chunked upload**, inte ett enda multipart-POST. Rekommenderat mönster: klienten
  delar filen i fasta segment (t.ex. 8–16 MB), laddar upp varje segment för sig mot ett
  `upload_session_id`, och kan fråga servern "vilka segment har du redan?" efter ett avbrott
  för att bara skicka om det som saknas. Detta är samma princip som `write_stream()` redan
  följer internt (chunkad, inkrementell), bara upphöjt till protokollnivå mellan klient och
  server.
- Caddy och Next.js-proxyn behöver INGEN arkitekturändring för detta — de strömmar redan.
  Caddys `request_body max_size` sätts per SEGMENT-storlek (t.ex. 20–24 MB för ett 16 MB-
  segment plus multipart-marginal), inte per hel-fil-storlek — det är den nyckelinsikt som
  gör att Caddy aldrig behöver känna till hela filens 1,3 GB på en gång.
- **Durabelt uppladdningstillstånd**: en ny tabell (t.ex. `upload_sessions`) som håller
  `id`, `owner_id`, `declared_total_bytes`, `declared_filename`, `chunk_size`,
  `received_chunk_indexes` (eller en bitmask/lista), `storage_key_prefix`,
  `created_at`, `expires_at`, `status` (`in_progress`/`finalizing`/`completed`/`aborted`/
  `expired`). Varje mottaget segment skrivs direkt till sin egen del av en temp-fil (eller en
  egen fil per segment som sedan konkateneras) via samma `write_stream()`-mönster som idag,
  aldrig buffrat i minnet.
- **Checksum**: sha256 räknas inkrementellt per segment OCH för hela filen vid
  finalisering (samma `hashlib.sha256()`-mönster `local_fs.py` redan använder), så
  integritetskontrollen inte försvagas jämfört med dagens enfas-uppladdning.
- **Städning av övergivna uppladdningar**: en periodisk jobb (samma mönster som
  `app/cleanup.py`s befintliga städjobb för utgångna tokens) tar bort `upload_sessions` och
  deras partiella data som passerat `expires_at` utan att slutföras — annars läcker
  diskutrymme från avbrutna 1,3 GB-uppladdningar som aldrig blir klara.
- **Disk-kvot/utrymmeskontroll FÖRE och UNDER uppladdning**: innan en ny `upload_session`
  skapas, kontrollera `shutil.disk_usage()` (redan beräknat i `library.py:459`, bara aldrig
  använt som en gate) mot `declared_total_bytes` plus en säkerhetsmarginal, och avvisa tidigt
  om det inte finns plats. Samma kontroll upprepas periodiskt under mottagandet av segment,
  inte bara en gång vid start — en lång uppladdning kan annars fylla disken från en helt
  annan samtidig import som startade efteråt.

### 2.2 Bearbetningsfasen (workern)

- **Ersätt `raw = f.read()`** (`library_import.py:574-575`) med path-/strömbaserad
  bearbetning genomgående: `storage.open_read()` ger redan en filhandle — den ska skickas
  vidare (eller en path till den underliggande temp-/lagringsfilen) genom hela kedjan istället
  för att omedelbart konsumeras till en `bytes`-variabel.
- **ZIP**: `zipfile.ZipFile` stöder att öppnas direkt mot en filhandle/path
  (`zipfile.ZipFile(path_or_file_obj)`) utan att hela arkivet läses in som `bytes` först —
  `zip_import.py:397`s `io.BytesIO(raw)` blir onödig när `raw` aldrig behöver existera. Varje
  entrys egen `_read_with_hard_cap()` (redan chunkad, `zip_import.py:183-198`) fortsätter
  fungera oförändrat eftersom den redan streamar per-entry.
- **ZIP-säkerhetskonstanter måste omvärderas, inte bara ärvas**: `MAX_TOTAL_UNCOMPRESSED_BYTES`
  (idag 200 MB) och `MAX_SINGLE_FILE_UNCOMPRESSED_BYTES` (idag 25 MB, `zip_import.py:49`)
  måste höjas i linje med 2 GB-målet — men eftersom skyddet mot zip-bomber (P2:s
  `_ExtractionBudget`, kompressionskvot-förfilter, `MAX_NESTING_DEPTH`) redan är byggt för att
  fungera UTAN att buffra hela arkivet, kan gränserna höjas utan att försvaga skyddet i sig —
  bara dess numeriska tak. Detta måste beslutas explicit (se §6 "Olösta beslut"), inte bara
  skalas linjärt.
- **Textextraktion (PDF/DOCX)**: `pypdf`/`python-docx` stöder båda att öppnas mot en path
  eller filhandle istället för `BytesIO(raw)` — `extract.py:9-17` skrivs om att ta en path/
  filhandle-parameter istället för `raw: bytes`. Om ett bibliotek internt ändå buffrar (vissa
  gör det för vissa operationer) dokumenteras det explicit per filtyp, det antas inte.
- **Ljud/video**: `media_import.py`s transkriptionspipeline (STEG 12) skrivs om att ta en
  path och (om den underliggande providern kräver det) strömma i bitar till providern istället
  för att läsa hela filen till `bytes` först. Kräver en egen, separat granskning av exakt
  vilken transkriptions-provider som används idag och om den redan stöder streaming-uppladdning
  (okänt i skrivande stund — flaggat som en explicit öppen fråga, se §6).
- **Minnesbudget**: med path-/strömbaserad bearbetning genomgående blir workerns
  minnesförbrukning oberoende av filstorlek (begränsad av chunk-/buffertstorlek, inte hela
  filen) — `mem_limit: 384m` (`docker-compose.vps.yml:220`) kan då sannolikt förbli
  oförändrad även för 2 GB-filer, men detta MÅSTE verifieras med ett riktigt minnesprofilerat
  test (se §4, PR C) innan det antas.

### 2.3 Progress, avbrott, återupptagning

- **Uppladdning**: progress rapporteras per mottaget segment (`received_chunk_indexes` /
  `declared_total_bytes`) — trivialt att exponera i UI redan på `upload_sessions`-nivå.
- **Bearbetning**: `ImportJob.progress_current`/`progress_total` (redan finns,
  `import_job.py:56-57`) utökas till att även kunna representera bytes-genomströmning INOM en
  enskild stor fils extraktion (t.ex. sidor genomströmda för en PDF, eller bytes lästa för
  ZIP-avpackning), inte bara antal filer i en ZIP — annars ser en användare som laddar upp EN
  stor fil ingen delframgång alls under hela bearbetningen.
- **Avbrott/cancellation**: `JobCancelled`-mönstret (`library_import.py:64-67`) finns redan på
  jobb-/filnivå för ZIP-poster — utökas till att kontrolleras med samma frekvens som progress
  uppdateras (per chunk, inte bara per fil), så en avbruten stor fil faktiskt stoppar snabbt
  istället för att vänta ut hela extraktionen av EN fil.
- **Retry/omstart**: `app/worker.py`s befintliga lease-/claim-mekanism (`JobLock`,
  `renew_lease`, `lease_expires_at`) hanterar redan "workern kraschade mitt i" på jobb-nivå —
  detta återanvänds oförändrat. Det som är nytt är att en KRASCH mitt i en påbörjad
  uppladdning (inte bearbetning) ska kunna återupptas via `upload_sessions` (§2.1), inte bara
  en krasch mitt i bearbetningen av en redan fullständigt mottagen fil.

### 2.4 Vad som INTE ska ändras

- `frontend/app/api/[...path]/route.ts`s streamade proxy — redan korrekt, verifierat i §1 rad 3.
- `backend/app/storage/local_fs.py`s `write_stream()` — redan korrekt, blir MALLEN för hur
  resten av kedjan ska bete sig, inte något som byts ut.
- Content-addresserad lagring (`{sha256[:2]}/{sha256}`) och path-traversal-skyddet i
  `_resolve()` (`local_fs.py:24-...`) — oförändrat, resumable upload lägger bara till ETT
  extra lager (segment-mottagning) FÖRE den befintliga finaliseringen, ersätter den inte.
- P2:s zip-bomb-försvar i sig (delat budgetobjekt, kompressionskvot-förfilter, nästlingstak) —
  bara de numeriska taken (§2.2) omvärderas, inte mekanismen.

---

## 3. PR-uppdelning i säker implementationsordning

Varje PR är oberoende granskningsbar och lämnar systemet i ett fungerande tillstånd — ingen PR
höjer en produktionsgräns förrän PR C (workerns minnessäkerhet) är klar och verifierad, exakt
i linje med kravet "raising Caddy before worker-side memory safety exists would expose a path
that can accept a huge request but then crash during processing."

| PR | Innehåll | Beroende av | Ändrar produktionsgränser? |
|---|---|---|---|
| **A** | Design-/gränskontrakt: dokumentera exakt request-/response-format för resumable upload (utan implementation) — API-kontraktet grundaren och en eventuell mobilklient kan granska INNAN kod skrivs | Inget | Nej |
| **B** | Backend: `upload_sessions`-modell + migration, segment-mottagnings-endpoints (`POST /api/library/upload-sessions`, `PUT .../chunks/{index}`, `POST .../finalize`), disk-kvotkontroll, städjobb för övergivna sessioner | A | Nej — nya endpoints, `/api/library/import` orörd, gamla klienter fortsätter fungera precis som idag |
| **C** | Worker: ta bort `raw = f.read()` genomgående — path-/strömbaserad ZIP-avpackning, textextraktion, mediaimport. Minnesprofilerat test med en riktig ~1–2 GB testfil i CI (se §4) | B (behöver kunna FÅ en stor fil in i lagringen för att testa mot) | Nej — samma 60 MB/30 MB gränser gäller fortfarande, bara den interna bearbetningen blir minnessäker för DEN storlek som redan tillåts, som grund för att sedan höja den |
| **D** | ZIP-säkerhetskonstanter: höj `MAX_TOTAL_UNCOMPRESSED_BYTES`/`MAX_SINGLE_FILE_UNCOMPRESSED_BYTES`/ev. `MAX_FILES` i linje med 2 GB-målet, med explicit motivering per värde (se §6) | C (skyddet måste vara strömmande innan taken höjs, annars återinförs OOM-risken taken skulle stoppa) | Ja — men bara ZIP-interna tak, inte toppnivå-uppladdningsgränsen |
| **E** | Progress/avbrott: bytes-nivå progress inom en enskild stor fils bearbetning, tätare cancellation-kontroll | C | Nej |
| **F** | Höj de faktiska gränserna tillsammans: `library.py`s `MAX_UPLOAD_BYTES`, Caddyfile-kommentaren/`max_size` (satt per SEGMENT-storlek enligt §2.1, inte per hel-fil), `docker-compose.vps.yml`s eventuella `mem_limit`-justeringar om C:s profilering visar att 384m/512m inte räcker | B, C, D | Ja — detta är den enda PR:n som faktiskt höjer produktionsgränser, och görs sist, med bevis från C/D i handen |
| **G** | Dokumentation: `docs/VPS_ARCHITECTURE.md`, `docs/KNOWLEDGE_IMPORT_SECURITY.md`, `docs/VPS_DOCKER_HARDENING.md` uppdateras samlat att spegla den nya arkitekturen — inklusive den tidigare identifierade föråldrade referensen till den döda `documents.py`-vägen | F | Nej |

---

## 4. Tester per PR

- **A:** inga körbara tester (rent kontraktsdokument) — granskas manuellt av grundaren.
- **B:** migrationstest (upp/ned), enhetstester för segment-mottagning (ordning, dubbletter,
  saknade segment vid finalisering avvisas), städjobbstest (en `upload_session` förbi
  `expires_at` städas bort, en aktiv rörs inte), disk-kvottest (en deklarerad storlek större än
  tillgängligt utrymme avvisas TIDIGT, inte efter att ha börjat ta emot data).
- **C:** **minnesprofilerat** test — kör den faktiska worker-processen mot en genererad
  (sparse, inte committad) ~1–2 GB testfil och mät faktisk RSS-minnesanvändning under hela
  bearbetningen, assertera att den ALDRIG närmar sig `mem_limit`. Genereras i CI med t.ex.
  `dd if=/dev/zero of=... bs=1M count=... seek=...` (sparse-fil, tar inte upp verkligt
  diskutrymme för nollor) eller motsvarande — exakt samma "generera, committa inte"-princip
  `vps-compose-verify`s befintliga stora testfiler i `.github/workflows/ci.yml` redan följer.
  Både en stor enskild PDF/textfil OCH en stor ZIP (för att träffa både `extract.py`- och
  `zip_import.py`-vägarna) testas separat.
- **D:** ZIP-säkerhetstest med de HÖJDA taken — bekräfta att en zip-bomb (hög
  kompressionskvot, många nästlade nivåer) fortfarande avvisas vid de nya, högre gränserna,
  inte bara att legitima stora paket nu accepteras. Regressionskör de befintliga P2-testerna
  (`docs/KNOWLEDGE_IMPORT_SECURITY.md`s attackyta 1) mot de nya konstanterna.
- **E:** progressuppdateringar syns med rimlig frekvens under en lång bearbetning (inte bara
  vid start/slut), en avbruten stor fil stoppar bearbetningen inom en kort, testad tidsram
  istället för att vänta ut hela extraktionen.
- **F:** end-to-end: en riktig ~1,3–2 GB fil laddas upp via HELA den nya resumable-vägen
  (inklusive ett simulerat avbrott och återupptagning mitt i), genom Caddy, Next.js-proxyn,
  backend, och blir `indexed` av workern — samma typ av verklig väg-genom-systemet-verifiering
  som `vps-compose-verify`s befintliga uppladdningstester redan använder (se
  `.github/workflows/ci.yml`s `vps-compose-verify`-jobb), inte bara enhetstester i isolation.
  Bekräfta ÄVEN att en fil över den nya gränsen fortfarande avvisas med rätt statuskod på
  rätt nivå (Caddy för en oversized SEGMENT, backend för ett för stort deklarerat
  `declared_total_bytes`).
- **G:** ingen kod att testa — verifiera att dokumentationen faktiskt stämmer mot den då
  gällande koden (samma disciplin som den här sessionens tidigare Caddy/backend-korrigering).

---

## 5. Migrations- och rollback-plan

- **B:** en ny, additiv Alembic-migration (`upload_sessions`-tabell) — ingen ändring av
  befintliga tabeller. Rollback: `alembic downgrade` tar bort tabellen; inga befintliga
  `ImportJob`/`Document`-rader påverkas eftersom inget i B kopplar dem till varandra än.
- **C:** ingen schemaändring — ren kodrefaktorering av bearbetningsvägen. Rollback: revert:a
  PR:n; `/api/library/import` fortsätter fungera exakt som innan (samma 60 MB-gräns, samma
  beteende), bara internt fortfarande minnesbuffrat för filer under den gränsen (dvs.
  rollback är riskfri eftersom C i sig inte höjer några gränser).
- **D:** en konstant-ändring i `zip_import.py` — ingen datamigration. Rollback: sänk
  konstanterna tillbaka; befintliga importerade dokument påverkas inte retroaktivt (gränserna
  gäller bara vid IMPORT-tillfället, inte lagrade dokument).
- **F:** detta är den enda PR:n med faktisk produktionspåverkan. Deploy-sekvens: 1) deploya B+C
  +D (ingen gränsändring ännu, ren infrastruktur/refaktorering), verifiera stabilt i
  produktion ett tag, 2) deploya F separat med de faktiska gränshöjningarna, så ett problem i
  refaktoreringen (C/D) upptäcks INNAN det kombineras med en högre gräns som skulle exponera
  det värre. Rollback för F: sänk `MAX_UPLOAD_BYTES`/Caddys `max_size`/segment-storleken
  tillbaka till tidigare värden — kräver ingen datamigration, bara konfiguration.
- Gammal `/api/library/import` (enfas-uppladdning) kan hållas kvar oförändrad parallellt med
  den nya resumable-vägen under en övergångsperiod (se §6, öppet beslut om huruvida den
  fasas ut eller behålls som fallback för små filer) — ingen "big bang"-migrering av klienter
  krävs.

---

## 6. Risker och olösta beslut

- **Transkriptionsproviderns egna gränser (media/ljud/video):** okänt i skrivande stund exakt
  vilken extern provider `media_import.py` anropar för transkribering och om DEN providern
  själv har en filstorleksgräns långt under 1,3 GB (många transkriptions-API:er har egna
  gränser, t.ex. 25 MB för vissa Whisper-API-varianter) — om så, krävs chunkning/segmentering
  på LJUDNIVÅ (inte bara uppladdningsnivå) innan providern anropas, vilket är ett väsentligt
  större arbete än PDF/DOCX-vägen. Måste utredas konkret innan PR C:s scope för media låses.
- **Vilket värde ska `MAX_TOTAL_UNCOMPRESSED_BYTES`/`MAX_SINGLE_FILE_UNCOMPRESSED_BYTES`
  faktiskt höjas till (PR D)?** 2 GB är målet för EN originalfil — men om originalfilen är en
  ZIP med FLERA stora filer i sig, ska paketets totala tak vara detsamma (2 GB) eller högre
  (t.ex. tillåta ett paket med flera 1–2 GB-filer, alltså tiotals GB totalt)? Detta är ett
  produktbeslut, inte bara ett tekniskt, och avgör hur högt `MAX_TOTAL_UNCOMPRESSED_BYTES` ska
  sättas.
- **Segmentstorlek för resumable upload (§2.1):** mindre segment (t.ex. 8 MB) ger snabbare
  återhämtning efter avbrott men fler HTTP-anrop och mer bokföring per uppladdning; större
  segment (t.ex. 64 MB) är motsatsen. Ingen bestämd rekommendation i det här dokumentet —
  flaggat för beslut i PR A:s kontraktsgranskning.
- **Disk-kapacitet på VPS:en i absoluta tal:** `shutil.disk_usage()` ger en körande siffra,
  men det finns ingen dokumenterad, avsiktlig gräns för hur många samtidiga stora
  uppladdningar/importer VPS:en ska klara innan den byggs om till en större disk eller
  extern lagring (t.ex. S3-kompatibel object storage) — utanför det här dokumentets scope,
  men värt att notera som en framtida skalningsgräns, inte en blockerare för 2 GB PER FIL.
- **Ska gamla `/api/library/import` (enfas) tas bort eller behållas som fallback för korta
  filer** (t.ex. under 10 MB, där resumable-protokollets overhead inte är värt det)? Olöst,
  produktbeslut för PR F.
- **Caddy-timeouts (§1, rad 1):** inga explicita `timeouts` är satta i `Caddyfile` idag.
  Caddys defaultbeteende för en mycket långsam (men inom storleksgränsen) uppladdning måste
  verifieras konkret — riskerar annars att en legitim men långsam 2 GB-uppladdning på en svag
  mobil-uppkoppling kapas av ett odokumenterat default-timeout innan den ens hinner klart.
  Del av PR A:s kontraktsgranskning.

---

## 7. Vad som INTE görs nu

Per grundarens explicita instruktion: **inga produktionsgränser ändras av det här dokumentet.**
Caddys `request_body max_size` förblir 30 MB, `library.py`s `MAX_UPLOAD_BYTES` förblir 60 MB,
och `zip_import.py`s konstanter förblir oförändrade tills PR C och D (workerns minnessäkerhet
respektive de omvärderade ZIP-taken) är byggda, granskade och verifierade — i den ordningen.
