# MainAI:s hjärna, minne och projektförståelse — designförslag

**Status:** Designförslag. Ingen kod skriven, ingen merge, ingen deploy. Grundat i faktisk
läsning av kodbasen (inte gissningar) och i en verklig felsökning av Library-uppladdningar
utförd i den här sessionen. Bygger ovanpå Life Library, Founder Knowledge Studio och den
nyligen levererade durable-worker-arkitekturen (`claude/life-library-durable-worker`, PR #6)
utan att ändra eller ersätta något av det.

**Beslutat, inte längre öppet:** all massimport (flera filer, ZIP, mappar) går genom **en enda
gemensam kö** — Life Librarys uppladdningskö. Knowledge Studios ("Kunskapsdatabas",
`/knowledge`) enfils-begränsning byggs INTE vidare som ett eget uppladdningsflöde. Den sidan
förblir vad den redan är: en sökvy ovanpå det gemensamma minnet, inte en egen intagsväg.

---

## 0. Blockerande krav innan riktiga ZIP-arkiv laddas upp

Detta måste vara på plats FÖRST, oavsett i vilken ordning resten av paketen nedan byggs:

**AI-provider/embedding-nyckeln måste vara giltig, inte bara "satt".** Under felsökningen i
den här sessionen laddades en riktig fil upp med en overifierad nyckel (`fake-key-for-tests`)
— den accepterades, skapade en `Document`-rad, och blev `failed` först flera sekunder senare,
djupt inne i bakgrundsbearbetningen (`provider.embed()` kastade ett fel). `GET /api/admin/
providers/status` kontrollerar idag bara `is_configured()` — om en API-nyckel-STRÄNG finns
satt, inte om den faktiskt fungerar mot leverantören. En overifierad men "konfigurerad" nyckel
ger exakt det beteende som observerades: allt accepteras, allt blir `failed` efter en stund.

**Konsekvens för dig konkret:** om du laddar upp dina riktiga arkiv INNAN detta är verifierat
kommer troligen HELA importen misslyckas på samma sätt — filerna är inte förlorade (se §5 om
varför lagring redan är säker oberoende av AI-bearbetning), men ingenting blir sökbart eller
del av minnet förrän en giltig nyckel finns och en riktig, aktiv provider-verifiering är byggd
(paket P1 nedan). **Kontrollera Admin → Providers innan du laddar upp något på riktigt.**

**Tillägg efter granskning:** idag blir en ogiltig nyckel bara `failed` — samma status som en
verkligt trasig fil, vilket ser ut som att importen misslyckats i grunden. Det är fel bild. §4.7
nedan bryter isär det här i sex skiljbara, namngivna tillstånd — bland annat en egen,
**återupptagningsbar** `blocked_provider`/`awaiting_provider`-status för just det här fallet —
så att "min nyckel är fel" aldrig ser ut som "min import är förstörd" i UI:t.

---

## 1. Nulägesanalys — vad som faktiskt finns

### 1.1 Redan byggt och verifierat (durable-worker-paketet, PR #6)

Det här är den viktigaste nyheten sedan förra utkastet av det här dokumentet: **stabil,
processoberoende lagring av originalfiler finns redan**, och löser redan en del av det du
efterfrågar under "säker lagring även när AI-bearbetning misslyckas":

- **`app/storage/local_fs.py`**: innehållsadresserad lagring (`{sha256[:2]}/{sha256}`, aldrig
  användarens filnamn), atomär skrivning (temp-fil → fsync → atomär rename), automatisk
  deduplicering av identiskt innehåll, skydd mot path traversal och symlänk-attacker.
- **`app/worker.py`**: en fristående, omstartssäker poll-loop (egen Docker Compose-tjänst,
  inga publika portar) som klaimar jobb via Postgres `FOR UPDATE SKIP LOCKED` — INGEN
  processbunden `BackgroundTask` längre för Library-importer. Ett jobb som avbryts av en
  omstart (backend, worker, hela VPS:en) återupptas automatiskt av nästa worker-poll, inte
  förlorat.
- **Ordningen som redan gäller per fil:** `received → original_storing → original_stored →
  extracting → extracted → embedding → indexed`, eller `failed`/`cancelled`. **Originalfilen
  lagras och verifieras (`original_stored`) INNAN någon AI-bearbetning ens påbörjas** — det
  här är redan exakt den separation mellan lagring och AI-bearbetning som §5 nedan bad om.
  En misslyckad `embedding`-fas lämnar filen kvar, verifierad, återförsöksbar — den försvinner
  aldrig för att en AI-leverantör var nere eller en nyckel var ogiltig.
- **Backup**: `lifeai_uploads`-volymen (originalfilerna) ingår nu i `backup.sh`/`restore.sh`,
  med egen innehålls- och manifestverifiering efter återställning.

Det som INTE är byggt än i det paketet, och som är direkt relevant här: **ingen AI-driven
klassificering, ingen relationsupptäckt, inget beständigt minne ovanpå det som redan är
lagrat.** Det paketet löste "filen försvinner aldrig" — det löste inte "MainAI förstår vad som
är i filen".

### 1.2 Redan byggt (Founder Knowledge Studio, före durable-worker)

- **`Document`**: `classification` (vision/architecture/decisions/history/security/general,
  manuellt satt via `manifest.json` eller default `general` — INGEN AI-klassificering sker
  automatiskt idag) + `active_truth_status` (active/historical/proposed/superseded/disputed).
- **`KnowledgeVersion`**: oföränderlig versionshistorik per källa.
- **`SourceRelationship`**: riktad kant mellan hela dokument (`derived_from`/`supersedes`/
  `contradicts`/`supports`/`duplicates`/`belongs_to`) — **skapas idag 100 % manuellt** av
  grundaren via UI, ingen automatisk upptäckt.
- **`KnowledgeClaim`** (STEG 10): diskreta, testbara påståenden extraherade per chunk, med ett
  objektivt `grounding_score` (ordöverlapp, aldrig modellens egen självrapporterade säkerhet)
  och ett status/confidence-par. **Den viktigaste byggstenen för hela det här förslaget** — en
  atomär påstående-nivå under dokumentnivån finns redan.
- **`ClaimRelationship`**: samma relationstyper som ovan men mellan enskilda påståenden.
  Modellen finns men **ingen kodväg skapar den än** — bara läst av `trust.py`, aldrig skriven.
- **`app/rag/trust.py`**: `assess_claim_confidence()` räknar om ett påståendes AKTUELLA
  konfidens dynamiskt från dess relationer — grunden till motsägelsedetektorn du efterfrågar.
- **`Task.suggested_by_ai`**: en befintlig, smal precedent för "AI föreslår, människa
  bekräftar" (Workbenchens spara-flöde).

### 1.3 Zip-importmotorn idag (`app/rag/zip_import.py`)

- Path traversal/Zip Slip-skydd, execut­abler blockerade, magic-byte-verifiering för PDF/DOCX,
  zip-bomb-skydd i två lager (metadata-kvot + strömmande hård gräns).
- **`MAX_FILES = 500`**, **`MAX_TOTAL_UNCOMPRESSED_BYTES = 200 MB`** per paket.
- **Ingen nästlad ZIP-hantering**: `.zip` är inte i `ALLOWED_EXTENSIONS`, så en zip inuti en
  zip hoppas bara över tyst ("filtypen stöds inte") — flaggas inte som nästlad, extraheras
  inte alls.
- **Ingen explicit hantering av lösenordsskyddade arkiv**: `zipfile` kastar ett generiskt fel
  vid ett krypterat entry, som idag fångas av den generella `except Exception` och blir en
  odiskret "Kunde inte läsa filen"-rad — inte en tydlig "det här arkivet är
  lösenordsskyddat"-flagga.

---

## 2. Luckor — vad som saknas, mappat mot dina åtta minneslager

| # | Lager | Status | Konkret lucka |
|---|---|---|---|
| 1 | **Originalminne** | ✅ Byggt (PR #6) | — |
| 2 | **Källminne** | ✅ Byggt | — |
| 3 | **Fakta-/påståendeminne** | 🟡 Delvis | `claim_type` (idé/beslut/uppgift/vision/teknisk/historisk) saknas — bara status/confidence finns idag, inte VILKEN SORTS påstående det är |
| 4 | **Projektminne** | 🟡 Delvis | `Project`/`Task` finns men är INTE länkade till de källor/påståenden som gav upphov till dem — inget `derived_from` mellan en uppgift och det dokument som föreslog den |
| 5 | **Idéminne** | 🔴 Saknas i stort | Workbench kan spara en analys märkt "idea", men ingen struktur för alternativa lösningar, varför idén uppstod, eller kopplingar mellan idéer |
| 6 | **Beslutsminne** | 🟡 Delvis | `active`+`decisions`+`supersedes` ger grunden, men "vem/när/varför" utöver `created_at` finns bara som fri text i en relations `note` |
| 7 | **Grundarminne** | 🔴 Saknas helt | `app/context/resolver.py` klassificerar konversationsturer (idé värd att spara, explicit minneskommando, etc.) men sparar INGENTING beständigt — rent observationellt, ingen tabell |
| 8 | **MainAI:s konstitution** | 🔴 Saknas helt (delas i **P7A** tidig ingestion + **P7B** sen aktivering, se §6.7) | P7A: ingen mekanism idag för att importera/identifiera/versionera möjliga styrdokument. P7B: ingen mekanism för att ett GODKÄNT sådant faktiskt ska påverka beteendet — `app/routers/chat.py`s systemprompt är en hårdkodad konstant |

Utöver de åtta lagren, tre till konkreta luckor:

- **Ingen automatisk relationsupptäckt** — `SourceRelationship`/`ClaimRelationship` skapas
  bara manuellt idag.
- **Ingen provider-förhandsverifiering** — se §0.
- **Ingen nästlad ZIP / lösenordsskydd-hantering** — se §1.3.

---

## 3. Målarkitektur

Samma grunddesign som föregående utkast av det här dokumentet höll fast vid, nu omformulerad
med din egen terminologi som primär struktur. **En enda gemensam kunskaps- och minnesbas** —
inte separata minnen per funktion — där MainAI, en framtida användar-AI och framtida
expertagenter läser/skriver samma underliggande tabeller med olika behörighetsfilter (samma
RLS-mönster som redan gäller konsekvent i hela kodbasen, bara applicerat på fler tabeller).

```
                    ┌─────────────────────────────────────────┐
                    │   1. ORIGINALMINNE (byggt, PR #6)         │
                    │   lifeai_uploads, innehållsadresserat      │
                    └───────────────┬─────────────────────────┘
                                    │ extraheras av worker
                    ┌───────────────▼─────────────────────────┐
                    │   2. KÄLLMINNE (byggt)                    │
                    │   KnowledgeVersion, DocumentChunk          │
                    └───────────────┬─────────────────────────┘
                                    │ claim-extraktion (STEG 10, utökas)
                    ┌───────────────▼─────────────────────────┐
                    │   3. FAKTA-/PÅSTÅENDEMINNE (delvis byggt) │
                    │   KnowledgeClaim + NY claim_type           │
                    └───────────────┬─────────────────────────┘
                                    │ tolkningsförslag → godkännande (§6)
        ┌───────────┬───────────┬──▼────────┬───────────┬───────────┐
        ▼           ▼           ▼            ▼           ▼           ▼
  4.PROJEKT-   5.IDÉ-      6.BESLUTS-   7.GRUNDAR-   8.KONSTITUTION
  MINNE        MINNE       MINNE        MINNE        P7A: ingestion tidigt
  (utökas)     (NY)        (utökas)     (NY)          P7B: aktivering sent,
        │           │           │            │        dubbelspärr (se §6.7)
        └───────────┴─────┬─────┴────────────┘
                          ▼
              PROJEKTKARTA — en gemensam,
              frågbar graf ovanpå allt ovan
              (samma tabell-familj för alla
              konsumenter: MainAI, framtida
              användar-AI, expertagenter)
```

**Varför den här ordningen (1→2→3→4-8) och inte tvärtom:** varje lager längre ner i kedjan
ÄRVER proveniens från laget ovanför — ett beslut (lager 6) pekar alltid tillbaka på de
påståenden (lager 3) som pekar tillbaka på de chunkar (lager 2) som pekar tillbaka på den
exakta filen och ZIP-arkivet (lager 1). Ingen genväg där ett högre lager skapas utan en
obruten kedja till originalet — det är den konkreta mekanismen bakom §5:s proveniensfråga.

**Undantaget: lager 8 är medvetet TVÅDELAT i tid, inte en enda punkt i den här kedjan.**
P7A (import, identifiering som möjligt styrdokument, versionshistorik, jämförelse mot andra
styrdokument, granskningskö) kräver bara lager 1–2 och samma godkännande-UI som resten av
förslagen — den kan alltså börja tidigt, redan i din första arkivimport, precis som du bad om.
P7B (aktivering, prioritet, konfliktlösning, faktisk påverkan på MainAI:s beteende) kräver
däremot att hela godkännandekedjan (P4) och dess dubbelspärr-mönster redan finns byggt och
beprövat på lägre-risk-lager först — det är därför AKTIVERING fortfarande kommer sent, även om
IMPORTEN inte längre gör det.

---

## 4. Datamodell

Additiv, inga befintliga tabeller ändras destruktivt — bara nya kolumner och nya tabeller,
samma disciplin som durable-worker-migrationen (0012) redan höll.

### 4.1 Lager 3 — Fakta-/påståendeminne (utökning av `KnowledgeClaim`)

```
KnowledgeClaim (befintlig, +1 kolumn)
  + claim_type: idea | decision | task_reference | vision | technical | historical | uncategorized
```

### 4.2 Lager 4–6 — Projekt-, idé- och beslutsminne (NY, gemensam tabellfamilj)

Samma mönster oavsett lager — det är MEDVETET en och samma struktur, inte tre separata
tabeller, eftersom idéer blir beslut och beslut refererar projekt: att ha en gemensam
`project_entities`-tabell med ett `entity_type`-fält gör en idé-till-beslut-övergång till en
statusändring på samma rad (med full historik via §4.4:s princip om att aldrig mutera), inte
en flytt mellan olika tabeller.

```
project_entities
  id, owner_id
  entity_type          -- idea | decision | task_reference | vision_statement | open_question
  title, summary
  status                -- återanvänder ActiveTruthStatus (active/historical/proposed/superseded/disputed)
  derived_from          -- OBLIGATORISK länk till käll-claims/dokument (proveniens, se §5)
  decided_by, decided_at, supersedes_entity_id   -- endast satta för entity_type=decision
  last_reviewed_at
  created_at

project_entity_relationships
  from_entity_id, to_entity_id, relationship_type
  -- relates_to | supersedes | contradicts | blocks | answers | duplicates | derived_from
```

`task_reference`-typen länkar till en BEFINTLIG `Task`-rad (inget dubbellagrat) — det här är
just den saknade länken från §2:s Projektminne-lucka.

### 4.3 Lager 7 — Grundarminne (NY)

```
founder_memory_notes
  id, owner_id
  note_type           -- working_style | expressed_goal | correction | preference
  content              -- fritext, alltid grundarens egna ord, ALDRIG en tolkad/inferred version
  source               -- conversation_message_id | explicit_statement | correction_of (entity_id)
  created_at
```

**Hård princip, ärvd direkt från `app/context/resolver.py`s befintliga regel:** den här
tabellen får ALDRIG innehålla en sluten härledning om grundarens humör, stress eller
psykologiska tillstånd — bara det grundaren uttryckligen sagt med egna ord, precis som
context-resolvern redan är designad. En rad skapas bara från `INTENT_EXPLICIT_MEMORY`/
`INTENT_IDEA_WORTH_SAVING`-klassificerade turer (redan byggda detektorer, bara aldrig
kopplade till beständig lagring förrän nu) eller en explicit korrigering (§6.6).

### 4.4 Lager 8 — MainAI:s konstitution (NY — datamodell/ingestion tidigt via P7A, aktivering sent via P7B)

```
governance_documents
  id, source_document_id   -- FK till Document — styrdokumentet är ALLTID spårbart till en riktig källa
  version                  -- ny rad per ändring, aldrig UPDATE (samma oföränderlighetsprincip som KnowledgeVersion)
  scope                    -- role | behavior | working_method | constraint | founder_relationship | uncategorized
                            -- (grov klassificering av VAD dokumentet påstår sig styra, satt vid P7A-import,
                            --  omprövningsbar av grundaren — påverkar ALDRIG systemprompten själv)
  status                   -- proposal | draft | approved | active | superseded | revoked
  compiled_prompt_fragment -- det faktiska textblock som SKULLE injiceras — beräknas och lagras redan vid
                            -- P7A (så jämförelse mot andra styrdokument går att göra tidigt), men LÄSES
                            -- av chat.py:s systemprompt-uppbyggnad EXKLUSIVT när status=active (P7B:s gate)
  approved_at, approved_by -- P7A: kan sättas ("jag litar på att det här ÄR ett styrdokument")
  activated_at, activated_by -- NY, separat par — P7B: sätts ENDAST av dubbelspärren, aldrig av P7A-flödet
  superseded_by
```

**Den avgörande gränsen mellan P7A och P7B ligger i EN enda regel, inte i två separata
tabeller:** `status=active` är det enda värde som `app/routers/chat.py` någonsin får läsa när
den bygger systemprompten — och ingenting i P7A:s kodväg (import, klassificering, versionering,
jämförelse, `approved_at`) får någonsin skriva `active`. Bara P7B:s dubbelspärrade
aktiveringsflöde (§6.7b) kan göra den skrivningen. Ett dokument kan alltså vara importerat,
identifierat, versionerat, jämfört och till och med `approved` LÅNGT innan P7B ens är byggt —
utan att det har någon som helst effekt på MainAI:s faktiska beteende under tiden.

### 4.5 Tolknings- och godkännandekö (den mekanism som binder ihop allt)

```
interpretation_proposals
  id, owner_id, proposal_type
    -- classify_document | classify_claim | create_relationship
    -- create_entity | update_entity | promote_governance
  target_ref, payload
  rationale         -- MainAI:s förklaring i klartext, ALLTID mänskligt läsbar
  status            -- pending | approved | rejected | edited_and_approved
  created_at, decided_at, decided_by
```

### 4.6 Provider-verifiering (NY, litet, för §0)

```
provider_verification_checks
  id, provider_name, role   -- chat | embedding
  verified_at, result        -- ok | invalid_key | unreachable | rate_limited
  checked_by                 -- system (pre-flight) | founder (manuell "testa nu"-knapp)
```

### 4.7 Statusmodell: sex skiljbara tillstånd istället för binärt lyckades/misslyckades

Idag ger `IndexStatus` bara `pending | indexing | indexed | failed` — en ogiltig
provider-nyckel och en verkligt trasig fil blir exakt samma `failed`-rad, vilket är precis det
missvisande beteendet som orsakade förvirringen i den här sessionens felsökning (§0). De sex
tillstånden nedan mappar en-till-en mot de kategorier som efterfrågats:

| # | Situation | Status (föreslagen) | Återupptagningsbar? |
|---|---|---|---|
| 1 | Filen kunde inte tas emot eller lagras (disk full, checksum-mismatch under strömning, avbrott innan `original_stored`) | `storage_failed` | Nej — kräver ny uppladdning, originalet finns aldrig verifierat lagrat |
| 2 | Filen är säkert lagrad (`original_stored` nådd) men ingen provider-nyckel är satt alls | `awaiting_provider` | **Ja, automatiskt** — workern försöker igen så fort `provider_verification_checks` visar `ok` för embedding-rollen, ingen ny uppladdning krävs |
| 3 | Provider är konfigurerad (nyckel satt) men P1:s förhandsverifiering misslyckas (`invalid_key`/`unreachable`/`rate_limited`) | `blocked_provider` | **Ja, automatiskt**, samma mekanism som ovan — men med ett SPECIFIKT, användbart felmeddelande hämtat från `provider_verification_checks.result` istället för ett generiskt "misslyckades" |
| 4 | Textextraktion misslyckas för just den här filen (korrupt PDF, olåsbar formatering) | `extraction_failed` | Nej — filens INNEHÅLL är problemet, inte provider eller lagring; kräver antingen en korrigerad fil eller en manuell override |
| 5 | Extraktion lyckades, men själva embedding-anropet misslyckas trots att P1:s förhandskontroll gick igenom (t.ex. en enskild transient API-hicka mitt i en stor batch) | `indexing_failed` | Ja, men INTE automatiskt av samma skäl som 2–3 — kräver en uttrycklig "försök igen"-åtgärd (workern vet inte om felet var engångs- eller permanent utan att fråga igen) |
| 6 | Innehållet är fullt `indexed` och sökbart, men väntar på grundarens beslut i tolkningskön (P4) | *(egen axel, inte IndexStatus)* `interpretation_proposals.status = pending` | Ja — väntar på en människa, inte på teknik |

**Två separata statusaxlar, avsiktligt inte en:** rad 1–5 svarar på "är innehållet sökbart än"
(pipeline-status, samma familj som redan finns i PR #6:s
`received → original_storing → original_stored → extracting → extracted → embedding → indexed`,
här utökad med de nya paus-/fellägena). Rad 6 svarar på en helt annan fråga — "har en människa
granskat vad AI:n TROR att innehållet betyder" — och får aldrig blandas in i samma fält, av
samma skäl som `ActiveTruthStatus` redan hålls isär från `IndexStatus` (§1.2). Ett dokument kan
alltså vara `indexed` OCH ha `pending`-förslag i tolkningskön samtidigt — UI:t måste visa båda,
inte bara den ena.

**Den konkreta regeln för §0:s bugg:** en ogiltig nyckel ska hamna i rad 2 eller 3 — ALDRIG i
`storage_failed` eller ett odifferentierat `failed`. Det är den direkta, namngivna fixen på det
du observerade: "allt blev failed efter ett tag" ska efter det här bli "allt är säkert lagrat
och väntar på en giltig provider-nyckel", med en knapp för att trigga om workern så fort nyckeln
är fixad — inte en ny uppladdning.

---

## 5. Import- och minnesflöde: ZIP → långtidsminne

```
0. FÖRHANDSKONTROLL (NY, §0 + P1) — INNAN AI-bearbetning påbörjas (INTE innan
   uppladdningen accepteras — se punkt 1, filen tas alltid emot och lagras
   oavsett providerstatus):
   En riktig, liten, billig testfråga mot både chat- och embedding-providern
   (t.ex. embedda en enda kort textsträng). Misslyckas den: filerna markeras
   `awaiting_provider`/`blocked_provider` (§4.7, rad 2–3) med ett tydligt,
   SPECIFIKT fel — "ingen nyckel satt än" respektive "nyckeln avvisades av
   leverantören" — INTE tyst efter minuter djupt i bakgrunden, och INTE som
   ett odifferentierat `failed`. Resultatet cachas kort (t.ex. 5 min) så
   varje enskild filuppladdning i en batch inte gör ett eget testanrop.

1. INTAG (byggt, PR #6, oförändrat)
   Fil/ZIP strömmas direkt till lagring — sha256 + storlek räknas medan den
   strömmar, aldrig ett fullständigt `await file.read()`. ZIP-arkivets EGEN
   blob sparas (ImportJob.source_storage_key) INNAN något packas upp.

2. ZIP-VALIDERING OCH UPPACKNING (byggt + UTÖKAS, P2)
   Idag: path traversal/zip-bomb-skydd, magic bytes, 500 filer/200 MB-tak.
   Utökas: nästlad ZIP (P2, säkert djup- och storlekstak, t.ex. max 3 nivåer
   och samma totala byte-budget delad över alla nivåer — aldrig en ny,
   oberoende 200 MB-budget PER nästlad nivå, det vore en zip-bomb-lucka),
   explicit "lösenordsskyddat arkiv"-detektion (en tydlig, egen ZipEntryResult-
   status, inte en generisk läsfel-rad), höjt filantal för "flera tusen filer"
   (kräver omprövning av MAX_FILES mot verklig minnes-/tidsbudget, inte bara
   en konstant-ändring — se P2:s egna kapacitetstest).

3. PER-FIL LAGRING + PIPELINE (byggt i PR #6, UTÖKAS med §4.7:s statusmodell)
   Varje extraherad fil får sin EGEN innehållsadresserade blob (dedup mot
   redan lagrat innehåll, oavsett vilket ZIP-arkiv eller vilken tidigare
   uppladdning den kom från — "dubbletter och olika versioner" från din
   kravlista är redan löst på fil-nivå). received → original_storing →
   original_stored → extracting → extracted → embedding → indexed, med
   `storage_failed` (terminalt), `awaiting_provider`/`blocked_provider`
   (paus, återupptas automatiskt) och `extraction_failed`/`indexing_failed`
   (terminalt per fil, kräver uttrycklig åtgärd) som namngivna grenar
   istället för ett enda odifferentierat `failed` (§4.7).

4. KÄLLMINNE (byggt)
   KnowledgeVersion (oföränderlig text-snapshot) + DocumentChunk (segment,
   embeddings) skapas per fil.

5. FAKTA-/PÅSTÅENDEMINNE (utökas, P3)
   Claim-extraktion (STEG 10, redan byggd) + NY claim_type-klassificering i
   SAMMA AI-anrop (inte ett extra anrop per chunk).

6. TOLKNINGSFÖRSLAG (NY, P4)
   Relationsjämförelse mot redan GODKÄNT material (aldrig mot andra väntande
   förslag). Skriver ALDRIG direkt — bara interpretation_proposals.

7. GRUNDARGRANSKNING (NY UI, P4) — detta ÄR §4.7:s rad 6 ("tolkning väntar
   på granskning") konkret realiserad
   "Tolkningskö" — grundaren ser varje förslag i klartext, godkänner/
   redigerar/avvisar. En korrigering sparas som en founder_memory_notes-rad
   (note_type=correction) och påverkar framtida tolkning (§6.6). Ett
   dokument kan redan här vara fullt `indexed` och sökbart — väntande
   tolkningsförslag blockerar aldrig sökbarhet, bara den AI-tolkade
   klassificeringen/relationerna.

8. COMMIT (NY, P4)
   Godkända förslag skapar/uppdaterar project_entities/relationships.
   promote_governance kräver en ANDRA, separat bekräftelse (P6).

9. FÖRSTA FÖRSTÅELSERAPPORTEN (NY, P5, se §6.4)
   Efter en STOR batch (t.ex. hela din första ZIP-import): en sammanfattande
   rapport ISTÄLLET FÖR hundratals enskilda förslag att klicka igenom en och
   en — "så här förstår jag visionen, det här verkar vara beslut, det här
   är motsägelser, det här saknar underlag" — grundaren godkänner/korrigerar
   på rapportnivå, vilket sedan committar de underliggande förslagen i bulk.
```

**Provenienskedjan, konkret, för valfritt påstående i minnet:**
`project_entity → derived_from → KnowledgeClaim → chunk_id → DocumentChunk → document_id →
Document.storage_key (filens egen blob) → Document.import_job_id → ImportJob.source_storage_key
(hela ZIP-arkivets blob)`. Varje led i kedjan är redan en riktig kolumn i en redan byggd eller
här föreslagen tabell — inget hopp, inget "ungefär den här filen".

---

## 6. Moduler (implementationspaket, se §7 för numrering)

### 6.1 P1 — Provider-förhandsverifiering
Löser §0. Litet, fristående, blockerande för allt annat.

### 6.2 P2 — ZIP-hårdning för massimport
Nästlad ZIP, lösenordsdetektion, omprövat filantalstak, kapacitetstest med syntetiska
flertusen-filers-paket (inte bara antaget — mätt, samma disciplin som durable-workerns
resursmätning).

### 6.3 P3 — Claim-typning
Utökar STEG 10:s befintliga extraktion med `claim_type`, i samma AI-anrop.

### 6.4 P4 — Tolkningskö + relationsupptäckt + projektkarta
Det största paketet: `interpretation_proposals`, `project_entities`,
`project_entity_relationships`, ny UI ("Tolkningskö"), relationsjämförelse (embedding-försil +
riktat LLM-anrop bara för gränsfall, bounded cost).

### 6.5 P5 — Första förståelserapporten
En sammanfattande, godkännandebar rapport efter en stor batch, byggd OVANPÅ P4:s förslag (en
rapport är en vy över många `interpretation_proposals` grupperade och sammanfattade, inte en
egen ny mekanism).

### 6.6 P6 — Grundarminne + korrigeringsloop
`founder_memory_notes`, kopplingen från Context Resolver till beständig lagring, och den
konkreta feedback-mekanismen: en korrigering av ett MainAI-förslag skapar en
`founder_memory_notes`-rad som P4:s relationsjämförelse och P3:s klassificering läser INNAN
nästa förslag genereras — det här är den faktiska "gradvis lär känna dig"-loopen, inte en
vag avsikt.

### 6.7 P7 — MainAI:s konstitution (delat i två paket, se din begäran om att flytta ingestion tidigt)

#### 6.7a P7A — Tidig governance-ingestion
Kan byggas TIDIGT (§8), direkt efter P1/P2 — kräver inget av P3–P6. Omfattar:
- Import av dokument som beskriver MainAI:s roll, beteende, arbetssätt, begränsningar eller
  relation till grundaren — går genom SAMMA gemensamma Life Library-kö som allt annat, ingen
  separat uppladdningsväg.
- Automatisk IDENTIFIERING av kandidater (t.ex. nyckelord/mönstermatchning + ett riktat
  LLM-anrop: "verkar det här dokumentet beskriva hur MainAI ska bete sig?") — resultatet blir
  ett `interpretation_proposals`-förslag (`proposal_type=classify_document`, en variant
  specifikt för governance), aldrig en tyst automatisk markering.
- `governance_documents`-raden skapas med `status=proposal` eller `draft`, `scope` satt,
  proveniens till originaldokumentet, och en beräknad `compiled_prompt_fragment` — så att
  JÄMFÖRELSE mot andra styrdokument (motsägelser, överlapp, en nyare version som ersätter en
  äldre) går att göra tidigt, innan något är aktivt.
- Grundaren kan granska och `approve` i den vanliga tolkningskön (§5, steg 7) — det sätter
  `approved_at`/`approved_by`, ALDRIG `status=active`.
- **Explicit, hård gräns:** ingenting i P7A rör `app/routers/chat.py`s systemprompt. Ett
  `approved` styrdokument har noll beteendepåverkan tills P7B existerar och körs.

#### 6.7b P7B — Sen governance enforcement
Byggs SIST (§8), medvetet, efter P4/P5 finns beprövade på lägre-risk-lager. Omfattar:
- **Aktivering**: den enda kodvägen som får sätta `governance_documents.status=active` —
  samma tvåstegs dubbelspärr-mönster som Librarys radering redan använder (`useTwoStepDelete`),
  fast med högre trösklar (t.ex. en förklarande sammanfattning av vad som FAKTISKT ändras i
  MainAI:s beteende, visad innan den andra bekräftelsen).
- **Prioritetsregler**: vad händer om två `active` styrdokument delvis motsäger varandra —
  `scope` (§4.4) avgör vilket promptblock som vinner inom samma område, med en explicit,
  loggad regel, inte en tyst sammanslagning.
- **Konfliktlösning**: en ny `active`-aktivering som motsäger en redan `active` rad kräver en
  uttrycklig `supersedes`-koppling, annars blockeras aktiveringen med ett tydligt fel.
- **Auditlogg**: varje aktivering/deaktivering skriver en `record_audit`-rad (samma mönster
  som redan används överallt i kodbasen), inklusive VILKET promptfragment som var i kraft
  under vilken period — nödvändigt för att i efterhand kunna förklara varför MainAI svarade
  som den gjorde en given dag.
- **Rollback**: att sätta en tidigare version `active` igen (t.ex. efter en felaktig
  aktivering) är alltid möjligt och alltid spårbart — aldrig en radering, bara en ny rad med
  `superseded_by` som pekar tillbaka.
- **Skydd mot att importerade filer utger sig för att vara styrande:** ett dokument kan ALDRIG
  självmant hävda i sin egen text att det är styrdokument och få det att gälla — P7A:s
  identifiering är bara ett FÖRSLAG, och bara en explicit, andra bekräftelse av grundaren i
  P7B:s aktiveringsflöde kan göra en rad `active`. Konstitutionen har högre auktoritet än
  vanliga anteckningar (injiceras i ett eget, tydligt avgränsat promptblock) men skriver
  ALDRIG över säkerhetsregler eller systemregler — de ligger utanför vad ett promptfragment
  någonsin kan påverka, samma gräns som redan finns mellan `trust.py`s instruktioner och den
  faktiska serverkoden.

### 6.8 GitHub som levande källa (förberedd, inte byggd här)
Nästa paket efter det här (redan aviserat i PR #6). Det enda som förbereds i det här
designarbetet: `DocumentSource` får en ny källtyp (`github`), och `KnowledgeVersion`s
`extraction_version`+`raw_metadata` räcker redan för att hålla en commit-SHA per version —
ingen ny mekanism behövs för "versionsbunden källa", bara en ny leverantör av innehåll in i
samma pipeline.

### 6.9 Hur MainAI laddar rätt minne utan att skicka hela databasen
Redan löst i grunden av `app/rag/retrieve.py`s `top_k`-gränsade hämtning — samma princip
appliceras på projektkartan: P4:s frågelager (§6:s "vad har vi bestämt om X") gör en riktad
sökning i `project_entities` (textmatchning + embedding-likhet, `top_k`-begränsad, samma
mönster som `hybrid_search()` redan använder), aldrig en full tabellskopia till prompten.

### 6.10 Hur AI-genererade slutsatser inte förväxlas med originalkällor
Redan en etablerad princip i kodbasen (`app/routers/workbench.py`s spara-flöde skapar ett NYTT
`Document` märkt med `derived_from`, aldrig en ändring av originalet) — förlängs rakt av:
`project_entities` är ALLTID en egen rad, ALDRIG en skrivning in i `Document`/`KnowledgeClaim`,
och `derived_from` gör skillnaden "det här är vad MainAI drog för slutsats" kontra "det här är
vad källan faktiskt sa" till en strukturell, frågbar egenskap — inte bara en konvention i
UI-texten.

---

## 7. Säkerhets- och integritetsrisker

| Risk | Mitigering |
|---|---|
| **Prompt injection via konstitutionen** — ett dokument hävdar att det är regler MainAI ska följa | P7B:s dubbelspärr: bara explicit, tvåstegs grundarbekräftelse kan sätta `status=active`, oavsett vad dokumentet självt hävdar. P7A:s tidiga ingestion är avsiktligt maktlös — `approved` (P7A) är INTE `active` (P7B) |
| **Ett `approved` (P7A) styrdokument uppfattas eller visas felaktigt som redan gällande** | UI-krav, inte bara datamodell: varje vy som listar `governance_documents` måste tydligt badge:a status (t.ex. grå "förslag/godkänt, ej aktivt" kontra grön "aktivt") — samma disciplin som redan gäller för `ActiveTruthStatus`-badges i Library |
| **Falska motsägelser/relationer** | Allt är förslag (P4), aldrig commit; UI:t säger uttryckligen "MainAI:s bästa gissning" |
| **Provider-nyckel exponeras i felmeddelanden** | P1:s verifieringsanrop måste återanvända samma "aldrig exponera rå leverantörsrespons"-disciplin som redan gäller i `app/providers/` |
| **Granskningströtthet blockerar riktig användning** | P5:s förståelserapport löser exakt detta — grupperad bulk-granskning istället för hundratals enskilda klick |
| **Personliga/känsliga arkiv (inte bara företagsdata)** | Grundarminne (P6) och konstitutionen (P7A/P7B) kan innehålla mer personligt material än tidigare Founder Knowledge Studio-innehåll — samma RLS/ägarskydd som redan gäller `documents` måste explicit verifieras täcka de nya tabellerna, inte antas |
| **Kostnads-/latensdrift** | P3:s klassificering läggs i SAMMA anrop som befintlig claim-extraktion; P4:s relationsjämförelse gated bakom en billig embedding-försil, inte varje-mot-varje |
| **Datamodellsprawl** | Byggs i de åtta separata paketen (P1–P6, P7A, P7B) i §8-ordning, inte en jättemigration |
| **RLS-glapp på nya tabeller** | Samma `FORCE ROW LEVEL SECURITY`-mönster som alla åtta befintliga FKS/STEG-tabeller redan följer — en explicit checklisterad granskningspunkt per migration |
| **Zip-bomb via nästlad zip** | Delad, inte per-nivå, total byte-budget över alla nästlade nivåer (P2) |

---

## 8. Rekommenderad byggordning

```
P1   Provider-förhandsverifiering        ← FÖRST, blockerar riktig användning idag
P2   ZIP-hårdning för massimport         ← krävs innan dina riktiga arkiv är säkra att ladda upp
P7A  Tidig governance-ingestion          ← FLYTTAD TIDIGARE (denna revidering): kan börja så
                                            fort P1/P2 är klara — inga beroenden till P3–P6,
                                            ingen beteendepåverkan än (§6.7a)
P3   Claim-typning                        ← litet, bygger direkt på STEG 10
P6   Grundarminne + korrigeringsloop     ← kan byggas parallellt med P4, ingen hård beroendekedja
P4   Tolkningskö + relationer + karta    ← det stora paketet, kräver P3
P5   Första förståelserapporten          ← kräver P4 (är en vy ovanpå dess förslag)
P7B  Governance enforcement              ← FORTFARANDE SIST, medvetet, egen granskning innan
      (aktivering av konstitutionen)        start — kräver P4:s godkännandeinfrastruktur bevisad
```

P1 och P2 är de enda som blockerar en riktig första massuppladdning. **P7A är flyttad tidigare
i den här revideringen** — den kan starta direkt efter P1/P2, parallellt med P3, eftersom
import/identifiering/versionering/jämförelse av möjliga styrdokument varken kräver eller
påverkar P3–P6. P4/P5/P7B är det som gör MainAI till en aktiv projektledningspartner snarare än
ett lagringsskåp — värdefullt, men inte blockerande för att börja mata in material säkert. P7B
förblir sist eftersom AKTIVERING (till skillnad från ingestion) kräver att dubbelspärr- och
granskningsmönstret redan är byggt och beprövat på lägre-risk-lager (P4) först.

---

## 9. Vad som måste fungera innan du laddar upp dina riktiga arkiv

1. **P1 klart och verifierat** — en riktig testimport mot en riktig, giltig nyckel visar
   `indexed`, inte `failed`.
2. **P2:s kapacitetstest kört mot ett syntetiskt paket i din faktiska storleksordning** — inte
   antaget att 500 filer/200 MB räcker, faktiskt mätt mot vad dina riktiga arkiv innehåller.
3. **En riktig backup tagen** (redan möjlig idag via `scripts/vps/backup.sh`, verifierad i
   den här sessionen) — inte för att något förväntas gå fel, utan för att det är den billigaste
   försäkringen som redan finns färdigbyggd.
4. Allt annat (P3–P6, P7B) kan byggas EFTER din första riktiga uppladdning utan att den behöver
   göras om — proveniensen (§5) gör att claim-typning, relationsupptäckt och minneslager kan
   appliceras retroaktivt på redan importerat material, inte bara på nya uppladdningar.
5. **P7A är valfri redan vid din första uppladdning, inte blockerande.** Om du redan då vill
   ladda upp dokument som beskriver MainAI:s roll/arbetssätt/regler kan de gå igenom samma kö
   och identifieras/versioneras direkt — det kräver bara P1/P2, inte P3–P6. De får dock
   garanterat noll effekt på MainAI:s faktiska beteende förrän P7B är byggt och du uttryckligen
   aktiverar något (§6.7b).
6. **§4.7:s statusmodell (`storage_failed`/`awaiting_provider`/`blocked_provider`/
   `extraction_failed`/`indexing_failed`) bör vara på plats innan en stor batch** — annars är
   du tillbaka i samma situation som den här sessionens felsökning: en ogiltig nyckel syns bara
   som ett odifferentierat `failed` istället för en tydlig, återupptagningsbar
   providerblockering. Det här hänger ihop med P1 (samma paket, samma `provider_verification_checks`-tabell).
