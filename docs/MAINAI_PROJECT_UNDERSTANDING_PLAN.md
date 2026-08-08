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
| 3 | **Fakta-/påståendeminne (dokument)** | ✅ Byggt (P3, 2026-07-28) | `claim_type` (idea/decision/task_reference/vision/technical/historical/uncategorized) extraheras nu i samma AI-anrop som påståendetexten (`app/rag/claims.py`) för NYA claims, och `backfill_claim_types()` (samma fil) klassificerar retroaktivt alla claims som skapades före P3 — idempotent, omstartssäker, uppdaterar bara `claim_type` in place, skapar aldrig nya rader. Manuellt triggerbar via `POST /api/admin/claims/backfill-types`. Se P4 för nästa steg: sortering till `project_entities`. **Konversationer/meddelanden är INTE en källa till fakta-/påståendeminnet ännu** — se den öppna arkitekturfrågan om `Conversation`/`Message` som förstklassig minneskälla (kräver en additiv, delad proveniensmodell innan P4/P6 kan byggas för den kedjan). |
| 4 | **Projektminne** | 🟡 Delvis | `Project`/`Task` finns men är INTE länkade till de källor/påståenden som gav upphov till dem — inget `derived_from` mellan en uppgift och det dokument som föreslog den |
| 5 | **Idéminne** | 🔴 Saknas i stort | Workbench kan spara en analys märkt "idea", men ingen struktur för alternativa lösningar, varför idén uppstod, eller kopplingar mellan idéer |
| 6 | **Beslutsminne** | 🟡 Delvis | `active`+`decisions`+`supersedes` ger grunden, men "vem/när/varför" utöver `created_at` finns bara som fri text i en relations `note` |
| 7 | **Grundarminne** | 🔴 Saknas helt | `app/context/resolver.py` klassificerar konversationsturer (idé värd att spara, explicit minneskommando, etc.) men sparar INGENTING beständigt — rent observationellt, ingen tabell. **2026-07-28-revidering (§6.11):** P6 begränsas INTE längre till bara resolver-flaggade turer — resolvern blir en prioritetssignal (snabbspår), medan en generell bakgrundspipeline (S5, §8) analyserar all konversationshistoria via samma `memory_source_units`/`KnowledgeClaim`-kedja som dokument. Kräver S1–S3 (§8) byggda först. |
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

### 4.8 Universell proveniens — `MemorySourceUnit` (S1A, konsoliderad slutdesign)

Denna sektion är den enda kanoniska källan för `MemorySourceUnit`-designen. Tidigare
granskningsrundors punktlistor är borttagna härifrån (de finns kvar i PR #30:s commit-historik
för den som vill se hur beslutet vandrade) — det som står nedan är det som gäller. Ingen
Alembic-migration är skriven ännu; S1A väntar fortfarande på godkännande, och det som
återstår innan den kan godkännas listas explicit i slutet av avsnittet.

**Problemet detta löser:** `KnowledgeClaim.source_id` pekar idag hårt på `documents.id`. En
konversation har ingen `Document`-rad att peka på, och framtida källor (GitHub, webb, e-post,
media) skulle annars vardera kräva sin egen nullable `source_X_id`-kolumn på `KnowledgeClaim`
— exakt den datamodellssprawl `docs/BRANCH_REGISTRY.md`s grundprincip varnar för.

#### Schema (S1A-omfattning)

```
memory_source_units
  id, owner_id
  source_kind           -- 'document_chunk' | 'document_version' | 'document_record'
  source_identity_key    -- oföränderlig: 'document_chunk:<chunk_id>' |
                         -- 'document_version:<version_id>' | 'document_record:<document_id>'
  source_role             -- 'founder' | 'assistant' | 'external' | 'system' | 'unknown'
  observed_at              -- NOT NULL, när DETTA system skapade/tog emot enheten
  occurred_at               -- nullable, när originalinnehållet faktiskt uppstod, om känt
  occurred_at_basis          -- 'explicit' | 'source_metadata' | 'inferred' | 'unknown'
  content_text                -- oföränderlig textsnapshot, NULL om inte snapshot_status='exact'
  content_hash                 -- sha256, ENDAST teknisk dedup/integritet, se hård regel nedan
  snapshot_status                -- 'exact' | 'degraded' | 'missing'
  lifecycle_status                 -- 'active' | 'revoked' | 'purged'
  revoked_at, revocation_reason, purged_at, purge_reason
  project_id                        -- nullable, vanlig FK mot projects.id (se "Ägarintegritet")
  created_at
  UNIQUE (id, owner_id)
  UNIQUE (id, owner_id, source_kind)      -- bär typen till subtypens komposit-FK
  UNIQUE (owner_id, source_identity_key)  -- möjliggör säker find-or-create

document_source_units
  memory_source_id (PK, FK -> memory_source_units.id)
  owner_id, source_kind                    -- speglar föräldern, verifierad av komposit-FK
  document_id                              -- NOT NULL, REFERENCES documents(id)
  version_id                               -- nullable, REFERENCES knowledge_versions(id)
  chunk_id                                 -- nullable, REFERENCES document_chunks(id) ON DELETE SET NULL
  FOREIGN KEY (memory_source_id, owner_id, source_kind)
    REFERENCES memory_source_units (id, owner_id, source_kind)
  -- typstyrda partiella unika index, se "Dokumentgranularitet"

memory_source_lifecycle_events   -- append-only revisionslogg, se "Livscykel"
  id, owner_id, memory_source_id
  from_status, to_status, reason, actor_type, actor_id, created_at
  FOREIGN KEY (memory_source_id, owner_id) REFERENCES memory_source_units (id, owner_id)

knowledge_claims.memory_source_id  -- nullable, REFERENCES memory_source_units(id) ON DELETE RESTRICT
```

`knowledge_claim_evidence` och alla meddelandetabeller (`message_source_units`,
`messages.sequence_number`) ligger UTANFÖR S1A — se "S1A/S1B/S1C" nedan för var de hör hemma.

#### Atomär enhet, inte analysfönster

En `memory_source_units`-rad är den MINSTA odelbara källan — i S1A ett `DocumentChunk` eller
en hel `KnowledgeVersion`/`Document`-post när chunken saknas. Framtida källtyper (`Message`,
`MediaSegment`, GitHub-snapshots) blir egna subtyptabeller efter samma mönster, aldrig nya
nullable kolumner på `KnowledgeClaim`. Ett `ConversationSegment` (§4.9) är ett analysfönster
som GRUPPERAR flera redan-existerande `memory_source_units`-rader — det är aldrig självt en
källa och har ingen egen `source_role`.

#### `source_role` — uppladdare ≠ författare

Fem värden: `founder | assistant | external | system | unknown`. Att grundaren laddade upp ett
dokument betyder inte att grundaren skrev det. `founder`/`external` sätts BARA när
författarskapet är explicit attribuerat. **Alla dokumentbackfillade rader i S1A får
`source_role=unknown`**, permanent — författarskapet för redan importerat material är inte
känt, och att gissa vore precis den sortens antagande som senare får konsekvenser för
promotion (§6.10). En framtida, versionshanterad `source_attributions`-tabell för granskad
omattribuering (t.ex. ett dokument som senare bekräftas vara grundarens eget) är en egen
utökning utanför S1A — `source_role` är strukturellt immutable på raden själv (se
"Immutability" nedan), aldrig en tyst `UPDATE`.

#### `content_hash` — hård regel

Identisk text vid olika tillfällen är olika episodiska händelser. `content_hash` verifierar
bara att innehållet inte korrumperats och används för begränsad TEKNISK deduplicering — den
får ALDRIG slå ihop två olika `memory_source_units`-rader bara för att texten råkar matcha.
`source_identity_key` (nedan) är den enda deduplicerings-/identitetsnyckeln.

#### Dokumentgranularitet — `source_kind` styr fälten strukturellt

`document_chunk` (chunk fortfarande spårbar), `document_version` (chunk purgad, version
kvar), `document_record` (varken chunk eller version kvar — bara `document_id` känt, en
uttryckligen degraderad proveniens). En trigger validerar att varje `source_kind` har exakt
de fält den kräver (`document_chunk`: `document_id`+`chunk_id` om `lifecycle_status='active'`,
`chunk_id` nullable annars; `document_version`: `document_id`+`version_id`, `chunk_id` alltid
NULL; `document_record`: bara `document_id`, `snapshot_status <> 'exact'`).

`source_kind` ligger dessutom på `document_source_units` självt (inte bara på föräldern) och
är strukturellt låst till förälderns värde via komposit-FK `(memory_source_id, owner_id,
source_kind) → memory_source_units(id, owner_id, source_kind)`. Detta löser ett konkret
databasfel som annars uppstår vid chunk-purge: om typen bara levde på `chunk_id IS NOT NULL`
skulle TIO purgade chunks från samma dokumentversion (alla får `chunk_id=NULL`) plötsligt
alla matcha samma typstyrda unika index som `document_version`/`document_record`-rader och
kollidera. Med `source_kind` som en egen, immutable, komposit-FK-buren kolumn förblir en
purgad `document_chunk`-rad `document_chunk` för alltid — den byter aldrig semantisk typ bara
för att sin locator nollas. De typstyrda partiella unika indexen blir därför:

```sql
CREATE UNIQUE INDEX uq_dsu_chunk ON document_source_units (owner_id, chunk_id)
    WHERE source_kind = 'document_chunk' AND chunk_id IS NOT NULL;
CREATE UNIQUE INDEX uq_dsu_version ON document_source_units (owner_id, document_id, version_id)
    WHERE source_kind = 'document_version';
CREATE UNIQUE INDEX uq_dsu_record ON document_source_units (owner_id, document_id)
    WHERE source_kind = 'document_record';
```

#### Källidentitet och säker find-or-create

`source_identity_key` (`document_chunk:<chunk_id>` / `document_version:<version_id>` /
`document_record:<document_id>`) är den stabila, immutable nyckeln backfill och dual-write
använder för att hitta-eller-skapa en källenhet utan att kunna skapa dubbletter eller
föräldralösa rader. En `DEFERRABLE INITIALLY DEFERRED` constraint-trigger verifierar vid
INSERT att `source_identity_key` faktiskt stämmer strukturellt med `document_source_units`s
locator (`document_chunk:<chunk_id>` måste motsvara den faktiska `chunk_id`, osv.) — nyckeln
kan alltså inte peka på fel rad.

Applikationskoden hittar-eller-skapar via ett SAVEPOINT-mönster (INSERT parent+subtyp
tillsammans, fånga `IntegrityError` vid nyckelkonflikt, rulla tillbaka bara savepointen,
`SELECT` den existerande raden) — standardmönstret för säker Postgres-upsert. Vid konflikt
verifierar koden EXPLICIT att den funna raden verkligen är rätt källa innan den återanvänds:
`source_kind` matchar, `document_id`/`version_id`/`chunk_id` matchar (`chunk_id` får vara
`NULL` om raden purgats sedan den skapades — annat mismatch är ett fel), och
`snapshot_status` matchar det förväntade materialet om raden fortfarande är `active`. En
nyckelkonflikt med avvikande innehåll avbryter jobbet med ett tydligt fel istället för att
tyst koppla en claim till fel källa.

#### Ägarintegritet — komposit-FK, inte bara dupliceat `owner_id`

`UNIQUE (id, owner_id)` på `memory_source_units` plus
`FOREIGN KEY (memory_source_id, owner_id) REFERENCES memory_source_units (id, owner_id)` på
subtypen gör en avvikande `owner_id` till ett FK-brott. En egen trigger verifierar dessutom att
`document_id` faktiskt tillhör `owner_id` (`documents.uploaded_by = owner_id`), att `version_id`
tillhör `document_id`+`owner_id` (`knowledge_versions.source_id`/`owner_id`), och att `chunk_id`
tillhör `document_id`+`owner_id` (`document_chunks.document_id`/`owner_id`) — RLS skyddar VEM
som kan läsa/skriva en rad, inte att relationerna INOM raden är strukturellt korrekta.
`project_id` behandlas INTE som en ägar-FK: `Project.created_by` är nullable och projekt är
fortsatt delad, icke-RLS-skyddad företagskunskap (`app/rls.py`) — `memory_source_units.
project_id` förblir en vanlig FK utan owner-verifiering.

#### Exclusive arc

En `DEFERRABLE INITIALLY DEFERRED` constraint-trigger på `memory_source_units` verifierar vid
commit att exakt en `document_source_units`-rad finns för varje `memory_source_units.id`.
Direkt radering av rader i endera tabellen är förbjuden i S1A (se "Immutability" nedan) — det
finns alltså ingen legitim väg att lämna en förälder utan subtyp EFTER att den en gång fått
en, men triggerns commit-tidskontroll skyddar mot att en förälder någonsin skapas UTAN subtyp
i första läget (bugg, ofullständig applikationskod, direkt SQL som kringgår
find-or-create-mönstret).

#### Immutability

En `BEFORE UPDATE`-trigger på `memory_source_units` fryser `source_kind`, `source_role`,
`source_identity_key`, `observed_at`, `occurred_at`, `occurred_at_basis` och `owner_id`
permanent, alltid, oavsett vem som skriver — den kontrollen är inte databasbehörighets-
modellens jobb (nedan), utan en oberoende andra spärr. En separat `BEFORE UPDATE`-trigger på
`document_source_units` fryser `memory_source_id`/`owner_id`/`document_id`/`version_id`/
`source_kind` permanent, och tillåter `chunk_id` att gå från satt till `NULL` ENDAST när
förälderns `lifecycle_status` redan är `revoked`/`purged` (den kontrollerade
FK-`SET NULL`-övergången när en chunk hårdraderas).

#### Databasbehörighetsmodell — riktig spärr, inte en GUC vem som helst kan sätta

En tidigare version av den här designen försökte skydda livscykelfält och radering med en
transaktionslokal markör (`SET LOCAL memory.transition_active/erasure_in_progress = 'on'`)
som bara den avsedda funktionen "skulle" sätta. Det är INTE en verklig spärr — vilken kod som
helst på samma databasanslutning kan sätta samma markör själv, så påståendet att bara en viss
funktion kan skriva vore strukturellt falskt.

Repot har redan den riktiga lösningen på plats, inte som en ny mekanism S1A måste uppfinna:
`backend/scripts/ensure_app_role.py`/`backend/db-init/01-app-role.sh` visar att applikationen
ALDRIG kör runtime-frågor som samma roll som äger tabellerna. Migrationer körs som
admin/superuser-rollen (`settings.database_url`, `migration_engine` i `app/db.py`) — den
rollen äger tabellerna. All runtime-trafik (allt `app/routers/*.py` gör) körs istället som den
separata, icke-superuser-rollen `mainai_app` (`settings.app_database_url`, `engine` i
`app/db.py`), som bara är BEVILJAD privilegier via `GRANT`, inte ägare. Eftersom `mainai_app`
inte äger tabellerna fungerar en vanlig `REVOKE` på den fullt ut (till skillnad från RLS, där
FORCE-flaggan behövs specifikt för att en TABELLÄGARE annars förbigår RLS — ett problem
`mainai_app` inte har, eftersom den inte är ägaren).

*(Sidoanmärkning, inte en del av S1A: `app/rls.py`s docstring säger idag "the app connects as
the table owner", vilket enligt `app/db.py`/`ensure_app_role.py` är inaktuellt/felaktigt —
värt en egen, separat rättning senare enligt isoleringsprincipen, inte något som blandas in
i den här migrationen.)*

S1A:s migration använder den befintliga rolluppdelningen konkret:

```sql
-- mainai_app får läsa och skapa nya rader (backfill, dual-write, find-or-create),
-- men aldrig UPDATE eller DELETE dessa tre tabeller direkt.
REVOKE UPDATE, DELETE ON memory_source_units, document_source_units,
    memory_source_lifecycle_events FROM mainai_app;
-- INSERT på memory_source_lifecycle_events sker ENDAST via de kontrollerade funktionerna.
REVOKE INSERT ON memory_source_lifecycle_events FROM mainai_app;

CREATE FUNCTION transition_own_memory_source(
    p_source_id UUID, p_target_status VARCHAR(16), p_reason TEXT, p_actor_kind VARCHAR(16)
) RETURNS VOID
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog  -- INGET schema i sökvägen, se "search_path" nedan
AS $$ ... $$;  -- kropp: se "Livscykel" nedan för den fullständiga owner-kontrollen

CREATE FUNCTION transition_memory_source_admin(
    p_source_id UUID, p_target_status VARCHAR(16), p_reason TEXT,
    p_actor_type VARCHAR(16), p_actor_id UUID
) RETURNS VOID
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog
AS $$ ... $$;

REVOKE ALL ON FUNCTION transition_own_memory_source(...) FROM PUBLIC;
REVOKE ALL ON FUNCTION transition_memory_source_admin(...) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION transition_own_memory_source(...) TO mainai_app;
-- transition_memory_source_admin får ALDRIG EXECUTE för mainai_app — bara admin-/
-- migrationsrollen kan anropa den, och den anropas aldrig från normal request-hantering.
-- samma tvådelade mönster för erase_owner_memory(...)/erase_owner_memory_admin(...)
```

`SECURITY DEFINER` gör att funktionen kör med DESS ÄGARES rättigheter (admin-rollen, som äger
tabellen och alltså får `UPDATE`/`DELETE`) när `mainai_app` anropar den — inte anroparens egna,
nu avsiktligt begränsade rättigheter. `mainai_app` kan därför bokstavligen inte köra `UPDATE
memory_source_units ...` direkt; Postgres avvisar det med ett behörighetsfel, oavsett vilka
sessionsvariabler den anslutningen sätter. Det här är kontrollerat av Postgres själv vid varje
sats, inte av en flagga i applikationslogiken. `BEFORE UPDATE`/`BEFORE DELETE`-triggrarna på
tabellerna finns KVAR som ett oberoende andra lager (skyddar även admin-rollens egen direkta
`psql`-åtkomst mot ett misstag), men det är GRANT/REVOKE-modellen — inte en trigger som litar
på en sessionsflagga — som faktiskt gör påståendet "bara denna funktion kan skriva" sant.

**`SECURITY DEFINER` kräver EN funktion till, inte bara EN spärr.** Att en funktion kör med
admin-rollens rättigheter betyder att den, om den själv inte är noggrann, kan kringgå precis
den ägarisolering RLS annars ger — `mainai_app` skulle annars i princip kunna anropa
`transition_own_memory_source(<någon_annans_source_id>, 'purged', ..., ...)` och få det
utfört, eftersom funktionen kör som admin-rollen och alltså inte själv stoppas av
`memory_source_units_isolation`-policyn (RLS gäller per-anslutning, inte per `SECURITY
DEFINER`-anrop). Två separata skydd, inte ett:

1. **Ägarkontroll INUTI funktionen, inte bara RLS.** `transition_own_memory_source` verifierar
   FÖRST, som sin egen explicita `WHERE`/`IF`-kontroll (inte genom att lita på RLS, som den
   som `SECURITY DEFINER` ändå kringgår):
   ```sql
   SELECT owner_id INTO v_owner FROM memory_source_units WHERE id = p_source_id FOR UPDATE;
   IF v_owner IS DISTINCT FROM NULLIF(current_setting('app.current_user_id', true), '')::uuid THEN
       RAISE EXCEPTION 'transition_own_memory_source: source % does not belong to caller', p_source_id;
   END IF;
   ```
   `p_actor_kind` accepteras BARA som `'founder'` eller `'system'` (funktionen reser ett
   undantag för något annat värde) — aldrig `'admin'`/`'migration'`. `actor_id` skrivs ALDRIG
   från ett parametervärde; funktionen sätter det internt till
   `NULLIF(current_setting('app.current_user_id', true), '')::uuid`, samma värde den redan
   verifierat äger källan — anropande kod kan alltså inte påstå att en annan användare/roll
   utförde övergången, oavsett vad den skickar in. `p_actor_kind='system'` används av
   `delete_source`s purge-anrop (en systemdriven följd av grundarens egen raderingsåtgärd,
   fortfarande inom SAMMA autentiserade request/`app.current_user_id`) — `'founder'` av en
   framtida direkt UI-åtgärd (t.ex. en "återställ"-knapp). Ingen av dem kan någonsin peka på
   en annan ägares rad, eftersom ägarkontrollen ovan körs FÖRST, oavsett `p_actor_kind`.
2. **`transition_memory_source_admin`** har full flexibilitet (godtycklig `source_id`, fritt
   `actor_type` inklusive `'admin'`/`'migration'`, fritt `actor_id`) men `EXECUTE` beviljas
   ALDRIG till `mainai_app` — bara till admin-/migrationsrollen, för genuint
   administrativa/migrationsdrivna underhållsflöden som körs UTANFÖR normal
   request-hantering. Normal apptrafik kan alltså strukturellt aldrig nå den flexibla
   varianten, oavsett vad en enskild request skickar in.

`erase_owner_memory(p_owner_id)` hade redan motsvarande självägarskapskontroll
(`p_owner_id = current_setting('app.current_user_id')`) — den principen bekräftas här som
det generella mönstret för VARJE `SECURITY DEFINER`-funktion `mainai_app` får `EXECUTE` på,
inte ett engångsundantag.

**`search_path` — inget schema i sökvägen, inte bara `pg_catalog` FÖRE `public`.** Ett fixerat
`SET search_path = pg_catalog, public` skyddar mot att en malikös roll lurar funktionen att
anropa en likadant namngiven funktion i ETT ANNAT schema tidigt i sökvägen — men skyddar INTE
mot att någon (om `PUBLIC` någonsin har `CREATE` på schemat `public`) skapar en skuggande
TABELL eller FUNKTION direkt i `public` självt, som sedan matchas av ett okvalificerat namn
inuti funktionskroppen. Säkrare: `SET search_path = pg_catalog` (inget `public` alls) och
fullt kvalificerade objektnamn inuti funktionerna (`public.memory_source_units`,
`public.memory_source_lifecycle_events`, osv.) — då spelar det ingen roll vem som kan skapa
vad i `public`, eftersom funktionen aldrig litar på ett okvalificerat namn för att hitta rätt
objekt. `apply_runtime_privileges` (nedan) verifierar ÄVEN att `PUBLIC` och `mainai_app`
saknar `CREATE` på schemat `public`, som ett andra, oberoende lager utöver
schema-kvalificeringen — inte antingen/eller.

**Privilegierna måste härdas om vid VARJE boot, inte bara i migrationen — verifierat mot
`backend/docker-entrypoint.sh`.** Bootordningen idag är: `ensure_app_role.py` (om
`MAINAI_APP_PASSWORD` satt) → `alembic upgrade head` → `exec` (starta appen).
`ensure_app_role.py` kör OVILLKORLIGT, på VARJE boot — inte bara vid rollskapande —
`GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO mainai_app` samt motsvarande
`ALTER DEFAULT PRIVILEGES`. Samma sak gör `backend/db-init/01-app-role.sh` för lokal Docker
Compose. Det betyder att en `REVOKE UPDATE, DELETE` inskriven bara i S1A:s migration skulle
fungera vid FÖRSTA deployen, men bli tyst återställd vid nästa vanliga omstart: `ensure_app_
role.py` beviljar tillbaka `ALL PRIVILEGES` INNAN Alembic ens körs, och Alembic har då inget
nytt att köra (migrationen ligger redan på head) — `REVOKE` körs alltså aldrig om.

Rätt boot-ordning, tillagd som ett fjärde steg i `docker-entrypoint.sh` (och motsvarande i
den lokala Docker-init-vägen så miljöerna inte divergerar):

```
ensure_app_role  →  alembic upgrade head  →  apply_runtime_privileges  →  starta backend
```

`apply_runtime_privileges` körs VARJE boot, EFTER Alembic, och är själv idempotent (kör om
den redan är korrekt, ändrar inget om så). Den:
- `REVOKE UPDATE, DELETE` på de skyddade proveniens-tabellerna från `mainai_app` (samma lista
  triggrarna/RLS-policyerna redan känner till — en enda källa, inte en andra lista som kan
  glömmas bort när en ny tabell läggs till).
- `REVOKE EXECUTE ON FUNCTION ... FROM PUBLIC` för BÅDA varianterna
  (`transition_own_memory_source`/`transition_memory_source_admin`, motsvarande för
  `erase_owner_memory`), `GRANT EXECUTE` till `mainai_app` ENDAST på de icke-admin-varianterna.
- `REVOKE CREATE ON SCHEMA public FROM PUBLIC, mainai_app` — oberoende av
  schema-kvalificeringen inuti funktionerna (se "search_path" ovan), ett andra lager.
- Verifierar resultatet med riktiga behörighetsfrågor, inte bara antar att `REVOKE`/`GRANT`
  lyckades:
  - `has_table_privilege('mainai_app', 'memory_source_units', 'UPDATE')` måste vara `false`.
  - `has_function_privilege('mainai_app', 'transition_own_memory_source(...)', 'EXECUTE')`
    måste vara `true`.
  - `has_function_privilege('mainai_app', 'transition_memory_source_admin(...)', 'EXECUTE')`
    måste vara `false` — `mainai_app` får ALDRIG kunna anropa adminvarianten.
  - `has_function_privilege('public', 'transition_own_memory_source(...)', 'EXECUTE')` (dvs.
    `PUBLIC`) måste vara `false` för BÅDA varianterna.
  - `has_schema_privilege('mainai_app', 'public', 'CREATE')` måste vara `false`.
  - tabellernas/funktionernas ägare (`pg_tables.tableowner`/`pg_proc`s ägare) får aldrig vara
    `mainai_app`.
- Om NÅGON av dessa verifieringar misslyckas: avslutar med ett fel som får hela
  `docker-entrypoint.sh` att stoppa (`set -e`, samma disciplin som redan gäller `alembic
  upgrade head` — appen startar hellre inte alls än startar med fel behörighetsläge).

Det här skrivs in i den kanoniska designen HÄR, men implementeras (det faktiska scriptet,
kopplingen i `docker-entrypoint.sh`, motsvarigheten i `db-init/`) i S1A:s
implementations-PR tillsammans med migrationen — inte i det här design-only-PR:et.

#### Livscykel — `transition_own_memory_source()`, den enda skrivvägen för `mainai_app`

```
transition_own_memory_source(source_id, target_status, reason, actor_kind)
```

Funktionen (se "Databasbehörighetsmodell" ovan för den fullständiga ägar-/sökvägshärdningen)
verifierar FÖRST att `memory_source_units.owner_id` för `source_id` matchar den autentiserade
anroparens egen `app.current_user_id` — annars ett undantag, oavsett `target_status`. Sedan
verifierar den atomiskt att övergången är laglig (`active → revoked`, `revoked → active`
["restore", kräver ett EGET `reason`, INTE det gamla `revocation_reason`], `active/revoked →
purged`; `purged` är terminalt), uppdaterar `memory_source_units`, och skriver en rad i
`memory_source_lifecycle_events`. `actor_kind` accepteras BARA som `'founder'` (en direkt
UI-åtgärd, framtida) eller `'system'` (en systemdriven följd av grundarens egen åtgärd, t.ex.
`delete_source`s purge nedan — fortfarande inom samma autentiserade request) — funktionen
själv sätter `actor_id` internt till samma redan-verifierade `app.current_user_id`, ALDRIG
från ett parametervärde, så anropande kod kan inte påstå att någon annan utförde övergången.
Genuint administrativa/migrationsdrivna övergångar (godtycklig `source_id`, `actor_type
IN ('admin','migration')`, fritt `actor_id`) går genom den separata `transition_memory_
source_admin()`, som `mainai_app` strukturellt aldrig kan anropa (`EXECUTE` aldrig beviljat,
se ovan) — inte samma funktion med en bredare parameteruppsättning.
`memory_source_lifecycle_events` är append-only (`mainai_app` saknar `UPDATE`/`DELETE`/direkt
`INSERT` på den, se ovan) och skrivs bara inifrån dessa funktioner — en rad i händelseloggen
är därför en pålitlig revisionslogg, inte något godtycklig applikationskod kan förfalska.

**Två operationer, tydligt separerade och mappade till verklig kod:**
- **Revoke** ("ta bort ur aktivt minne", en FRAMTIDA UI-åtgärd, inte byggd ännu): utesluter
  omedelbart ur retrieval/promotion/tolkningsförslag, men behåller `content_text` och alla
  claims — reversibelt via restore.
- **Purge — permanent radering, definierad fullständigt, inte bara `MemorySourceUnit`s kopia.**
  Motsvarar dagens `delete_source` (Library) OCH `DELETE /api/documents/{id}` (den äldre
  `app/routers/documents.py`-rutten) — se "En gemensam purge-tjänst" nedan för varför båda
  måste anropa SAMMA implementation. En verklig permanent radering måste hantera VARJE
  innehållsbärande kopia, inte bara `memory_source_units`:
  - `memory_source_units.content_text`/`content_hash` → `NULL` (via `transition_own_memory_
    source(..., 'purged', ..., 'system')` — `purge_source()` kör inom samma autentiserade
    request som utlöste raderingen, så ägarkontrollen i funktionen håller normalt).
  - `knowledge_claims WHERE source_id = <dokumentet> AND owner_id = <ägaren>` → raderas
    (`claim_relationships` cascadar bort automatiskt, migration 0007). En claim är härledd,
    potentiellt känslig text i sig själv.
  - `Document.content_preview` → nollas (ett rått textutdrag av originalet, inte bara en
    referens till det).
  - `Document.media_blob` → nollas (STEG 13:s in-databas-kopia av media-bytes, om satt).
  - Blobben på disk (`Document.storage_key`) → `maybe_purge_blob()` (redan existerande,
    content-addresserad, referensräknande radering — oförändrad, men måste faktiskt anropas
    av BÅDA raderingsvägarna, se nedan).
  - `Document.file_path` → samma hantering som `storage_key` om den fortfarande pekar på
    kvarvarande bytes (legacy-fält från innan `storage_key`/blob-lagret fanns).
  - `Document.title`/`original_filename`/`category`/`source_url` → BEHÅLLS som neutral
    metadata (ett filnamn eller en URL är i sig sällan känsligt innehåll, och att radera dem
    skulle göra även den soft-deletade `Document`-raden meningslös för framtida revisionsspår)
    — men om `source_url`/`title` i ett enskilt fall FAKTISKT innehåller identifierande
    personligt innehåll är det en separat, medveten framtida utökning (per-fält-purge), inte
    något S1A löser generellt. UI:t för "radera källa" måste vara ärligt om exakt detta: vad
    som raderas (text, claims, media) kontra vad som stannar kvar som metadata (titel,
    filnamn, URL, tidsstämplar).
  - Ordning: (1) `transition_own_memory_source(..., 'purged', 'source_deleted', 'system')`
    per matchande `memory_source_units`-rad, (2) radera matchande `knowledge_claims`, (3)
    nolla `Document.content_preview`/`media_blob`, (4) radera `DocumentChunk`-raderna (FK:ns
    `SET NULL` mot redan-purgade `document_source_units` passerar nu immutability-triggerns
    lifecycle-medvetna kontroll), (5) `maybe_purge_blob()` för `storage_key`/`file_path`, (6)
    fortsätt som idag (`Document.deleted_at` osv). Skrivs med SQLAlchemy Core `UPDATE`/`DELETE`
    mot en subquery, inte en joined ORM `Query.update()`.

**En gemensam purge-tjänst — inte två raderingsimplementationer.** Utöver Librarys
`delete_source` finns en äldre, fortfarande LIVE rutt: `DELETE /api/documents/{document_id}`
(`app/routers/documents.py`, kopplad i `main.py`, anropad från `frontend/lib/api.ts`s
`deleteDocument`) — den hårdraderar `DocumentChunk`-rader och sedan `Document`-raden direkt,
en helt separat implementation utan `deleted_at`, utan blob-purge, och (dess egen docstring
medger det) med en känd olöst multi-uploader-brist. Med S1A:s FK:er (`document_source_units.
document_id` utan `ON DELETE`-åtgärd) skulle den rutten dessutom blockeras rakt av (FK-brott).
S1A:s PR extraherar purge-logiken ovan till en delad funktion (t.ex.
`app/rag/source_purge.py::purge_source(db, document_id, owner_id, request)`), och BÅDA
rutterna anropar den — `documents.py`s `delete_document` blir en tunn wrapper, inte en egen
parallell implementation. Det här är en avsiktlig beteendeförändring för den äldre rutten
(den får nu samma soft-delete+blob-purge+minnespurge som Library redan har, inte bara en
chunk+dokument-radering) — inte en bugg.

#### Kontoradering — måste uppdateras i S1A:s PR, inte bara i DDL:n

Dagens `delete_account` (`app/routers/account.py`) hårdraderar `DocumentChunk` →
`KnowledgeVersion` → `SourceRelationship` → `Document` (i den ordningen) plus
`ImportJob`/`Conversation`/`Message`, och nollar attribution på `Project`/`Task`/`UsageLog`/
`AuditLog`. S1A:s nya FK:er skulle annars BLOCKERA den raderingen (FK-brott → transaktionen
rullas tillbaka → HTTP 500) eftersom `document_source_units`/`memory_source_units`-rader
fortfarande skulle peka på dokument/ägaren som håller på att raderas — och `mainai_app` saknar
(se ovan) direkt `DELETE`-rättighet på dem. Lösningen är samma modell som "Livscykel": EN
`SECURITY DEFINER`-funktion, `erase_owner_memory(p_owner_id)`, ägd av admin-rollen,
`EXECUTE` beviljad ENDAST till `mainai_app`. Funktionen verifierar FÖRST att `p_owner_id =
current_setting('app.current_user_id')::uuid` — kan alltså strukturellt aldrig radera någon
ANNAN än den redan autentiserade, lösenords-bekräftade anroparen själv — och raderar sedan,
med de rättigheter DESS ägare (admin-rollen) har, i denna ordning:

1. `knowledge_claims WHERE owner_id = p_owner_id` (även detta personlig/härledd data — se
   samma resonemang som Purge ovan; tar bort RESTRICT-blockeraren mot `memory_source_units`).
2. `document_source_units WHERE owner_id = p_owner_id`.
3. `memory_source_units WHERE owner_id = p_owner_id` (`memory_source_lifecycle_events`
   cascadar bort automatiskt — `ON DELETE CASCADE`, till skillnad från den vanliga RESTRICT-
   hållningen, eftersom en händelselogg för en källa som inte längre finns saknar syfte).

`actor_id`/`owner_id` som pekar på användaren blockerar INTE detta: kontots egna rader
raderas i just den ordningen ovan innan `delete_account`s befintliga steg (som redan idag
raderar `User`-raden sist) fortsätter oförändrat — det finns ingen kvarvarande referens från
`memory_source_lifecycle_events` när `User` väl raderas, eftersom dess FK mot
`memory_source_units` redan cascadat bort i steg 3. `delete_account` anropar
`erase_owner_memory(user_id)` FÖRE sin befintliga `DocumentChunk`/`KnowledgeVersion`/
`Document`-radering, i SAMMA databastransaktion — om något steg senare i `delete_account`
misslyckas rullas hela transaktionen (inklusive `erase_owner_memory`s del) tillbaka
automatiskt, exakt som `delete_account`s befintliga try/except/rollback-mönster redan
garanterar. `/api/account/export` måste samtidigt utökas med `knowledge_claims` (dess
docstring säger idag felaktigt att claims "has no backing table yet" — redan fel sedan
PR #29, dubbelt fel efter S1A) och en sammanfattning av `memory_source_units`/
`memory_source_lifecycle_events` (innehåll där inte purgat, annars en tydlig
livscykelmarkör) — annars exporterar kontot inte längre allt personligt/härlett material det
faktiskt håller.

#### Fasad migrationsplan för `KnowledgeClaim.source_id`/`version_id`/`chunk_id`

1. Lägg till `memory_source_units`/`document_source_units` samt `KnowledgeClaim.memory_source_id`
   (nullable) — rent additivt.
2. Backfilla: en `memory_source_units`+`document_source_units`-rad per DISTINKT
   `document_chunk`/`document_version`/`document_record`-källa bland befintliga claims (ALDRIG
   en per claim), `source_role=unknown`, via find-or-create-mönstret ovan.
3. Dual-write: `extract_claims_for_document` sätter BÅDE de gamla kolumnerna OCH
   `memory_source_id` för varje ny claim.
4. `memory_source_id` blir kanonisk källa för allt nytt kodbaserat.
5. En separat, granskad migration slutar läsa/skriva de gamla kolumnerna.
6. En separat, ÄNNU senare migration tar bort dem.

#### S1A/S1B/S1C — S1 delas i tre migrationer

- **S1A** (nästa steg): `memory_source_units`/`document_source_units`/
  `memory_source_lifecycle_events`, `transition_own_memory_source()`/`transition_memory_
  source_admin()`, nullable
  `KnowledgeClaim.memory_source_id`, deterministisk backfill, dual-write, kontoradering/export-
  integration. INGA meddelandetabeller, INGEN `knowledge_claim_evidence` (ingen aktiv writer
  förrän S1C/P4).
- **S1B**: `messages.sequence_number` — expand (nullable) → dual-write → durable historisk
  backfill → verifiering → contract (separat migration: `NOT NULL`+`UNIQUE`). `SET NOT NULL`
  kan inte ske i samma migration som lägger till kolumnen (backfillen hinner inte köra under
  migrationen). Helt oberoende av S1A.

  **Status 2026-08-07 (PR för `claude/s1b-message-sequence-number`, se Pass 42 i
  `docs/BRANCH_REGISTRY.md`):** de FYRA FÖRSTA stegen är byggda — expand, dual-write,
  durabel historisk backfill, och verifieringsfunktionen. CONTRACT-steget är MEDVETET INTE
  byggt och får inte byggas förrän backfillen faktiskt körts mot produktionsdata.
  - **Expand** = migration `0030_message_sequence_number`: nullable `integer`-kolumn,
    `ck_messages_sequence_number_positive`, partiellt unikt index
    `uq_messages_conversation_sequence_number` (giltigt redan nu eftersom alla befintliga rader
    är `NULL`), samt det tidigare helt saknade `ix_messages_conversation_id`.
  - **Dual-write** = en `BEFORE INSERT`-trigger (`messages_assign_sequence_number`), INTE en
    rad i `app/routers/chat.py`. Samma resonemang som migration 0029 använde för sin
    trigger: en numreringsregel som bara lever i EN skrivare är bara så bra som varje FRAMTIDA
    skrivare som minns den — och det finns redan tre INSERT-vägar in i `messages`. Formeln är
    `GREATEST(max(sequence_number), count(*)) + 1` per konversation, inte `max + 1`:
    `count`-termen är det som garanterar att ett nytt meddelande i en ÄNNU INTE backfillad
    konversation alltid får ett nummer OVANFÖR det `1..N`-intervall backfillen senare delar
    ut (bevis i migrationens egen docstring). Serialiseras med `pg_advisory_xact_lock` på en
    per-konversationsnyckel (namespace `72197002`) som backfillen tar samma lås på.
  - En andra trigger (`messages_deny_sequence_number_rewrite`) gör ett tilldelat ordinal
    OFÖRÄNDERLIGT och förbjuder att ett meddelande flyttas till en annan konversation.
    `NULL → värde` är den enda tillåtna övergången — backfillens egen.
  - **Durabel backfill** = `app/rag/message_sequence_backfill.py` +
    `app/rag/message_sequence_backfill_job.py`, körd som ett riktigt `mainai_jobs`-jobb
    (`job_type=message_sequence_backfill`) på den befintliga workern. §6.12:s
    `memory_processing_jobs`-skiss är alltså INTE byggd som en egen tabell — jobbruntimen från
    PR #36 fanns inte när §6.12 skrevs, och att bygga en andra parallell kö för en backfill
    vore precis det projektet upprepade gånger avvisat. Numreringen är deterministisk på
    `(created_at, id)`, atomisk per konversation, och fail-closed vid konflikt.
  - **Verifiering** = `count_unsequenced_messages()`. Den ska rapportera 0 mot verklig
    produktionsdata innan CONTRACT-migrationen ens skrivs.
  - **INTE byggt, medvetet:** CONTRACT-migrationen (`SET NOT NULL` + riktigt
    `UNIQUE`-constraint), och omställningen av läsvägarna till `ORDER BY sequence_number`
    (`app/routers/conversations.py`, `app/routers/chat.py`) — historiska rader är `NULL` tills
    backfillen körts, så en läsväg som sorterar på ordinalet skulle vara fel just nu. Båda
    läsvägarna fick däremot `id` som deterministisk tiebreaker efter `created_at`, samma
    ordning backfillen numrerar i, så transkript, export och ordinaler är överens med varandra
    redan under expand-fasen.
- **S1C**: `message_source_units` + `knowledge_claim_evidence` + ägar-/konversationsintegritet
  + backfill Message→MemorySourceUnit. Kräver S1B. Ingen claim-extraktion från konversationer
  än — det kommer efter S1C.

##### `messages` RLS — egen ägarisolering (migration 0031, Pass 43)

Ett eget, litet steg mellan S1B och S1C, byggt på egen branch och egen PR precis som Pass 42
sa att det borde bli. **Det är INTE en del av S1B och inte en del av S1C** — det rör varken
`sequence_number`, dess nullability, CONTRACT-migrationen eller någon ny tabell, och det är
korrekt oavsett om backfillen körts mot produktion, körs senare, eller aldrig körs.

**Problemet.** `messages` var den sista tabellen med direkt personligt innehåll utan egen
RLS-policy. Isoleringen vilade helt på en konvention: varje router slår först upp den
RLS-skyddade `conversations`-raden och rör `messages` först därefter. Konventionen följdes
korrekt av alla fem DB-vägar som finns idag — men den var en egenskap hos fem anropsplatser,
inte hos tabellen. Ett glömt `JOIN conversations` i en framtida bulkfråga (och både S1C:s
backfill och S2:s segmenteringspass kommer att skanna `messages` i bulk) var före 0031 en tyst
läsning över ägargränsen; efter 0031 ger den noll rader. Att fela stängt är en bugg; att fela
öppet är en incident.

**Lösningen: HÄRLEDD ägare, inte en denormaliserad `owner_id`-kolumn.** Policyn är
`conversation_id IN (SELECT c.id FROM conversations c WHERE c.user_id = <uid>)`. Två skäl:
en andra kopia av ägarfaktumet kan driva isär från `conversations.user_id`, en härledning kan
inte; och en ny kolumn hade krävt ännu en expand/backfill/contract-kedja som inte gick att
slutföra utan en produktionsbackfill. Den härledda policyn är korrekt i samma ögonblick den
skapas, på varje rad som redan finns. Uncorrelated `IN` (inte korrelerad `EXISTS`) är ett
MÄTT val — se migrationens docstring för mätserien; `IN` planerar på no-RLS-nivå för de
bulkskanningar backfillen och kontoexporten gör, `EXISTS` kostar ungefär dubbelt.

**Den subtila konsekvensen — samspelet med 0030:s tilldelningstrigger.**
`messages_assign_sequence_number()` är inte SECURITY DEFINER, så dess aggregat över
`public.messages` blev RLS-filtrerat i och med 0031. Räkningen är ändå fortfarande sann, av ett
STARKARE skäl än "RLS gäller inte": policyns synlighetsenhet är KONVERSATIONEN, så för en
given konversation är antingen alla dess meddelanderader synliga eller ingen — och den INSERT
som utlöste triggern måste själv ha passerat WITH CHECK på samma konversation, vilket bevisar
att sessionen ser den och därmed alla dess befintliga meddelanden. En session som inte äger
konversationen avvisas innan aggregatet ens körs. S1B:s kollisionsfrihetsbevis är alltså
bevarat — och det är fastspikat av test, inte lämnat som resonemang
(`tests/backend/test_messages_rls.py`, mutationstestat: en policy som kan dölja onumrerade
rader i samma konversation ger `assert 1 == 4`, alltså exakt den kollision beviset utesluter).

Migration 0031 lägger också till `ix_conversations_user_id`, som saknats sedan `0001`: policyns
subquery filtrerar `conversations` på `user_id` vid varje sats som rör `messages`. Det är ett
direkt krav från predikatet, i exakt samma mening som `ix_messages_conversation_id` var ett
direkt krav från 0030:s trigger.

#### Status: PR #30 (design) kontra S1A-implementations-PR:n (senare, separat)

PR #30 är design-only och innehåller ingen kod — det den här sektionen (§4.8) beskriver är
VAD som ska byggas, inte byggkoden själv. Två separata godkännande-trösklar, inte en:

**Krävs för att PR #30 (arkitekturbeslutet) ska kunna mergas:** att designen är intern
konsekvent, verifierad mot verklig kod (kolumnnamn, RLS-policyer, boot-ordning, rolluppdelning
— allt ovan är det), och inte innehåller några kvarstående motsägelser. Det är uppfyllt i den
här versionen. Produktionsdataprofilen krävs INTE för att merga PR #30 — den är ett villkor
för S1A-IMPLEMENTATIONEN, se nedan.

**Krävs innan en separat S1A-implementations-PR (migration + kod) kan MERGAS** (inte innan
den öppnas — migrationen, `purge_source()`, RLS-kod, export/kontoradering, boot-härdning och
testmatrisen ska byggas OCH granskas i den PR:en):

1. **Produktionsdataprofilen.** Den här sessionen har ingen databasåtkomst (ingen
   `DATABASE_URL`, ingen körande Postgres/Docker) och kan alltså inte köra den:
   ```sql
   SELECT
     count(*) FILTER (WHERE chunk_id IS NOT NULL AND version_id IS NULL) AS chunk_only,
     count(*) FILTER (WHERE chunk_id IS NULL AND version_id IS NOT NULL) AS version_only,
     count(*) FILTER (WHERE chunk_id IS NULL AND version_id IS NULL) AS neither
   FROM knowledge_claims;
   ```
   Schemat är konstruerat för att fungera oavsett resultat (`version_id` nullable även för
   `document_chunk`), men resultatet ska ändå bekräftas innan den PR:en mergas.
2. Migrationsfilen: tabeller, triggers, CHECK-constraints, `REVOKE`/`SECURITY DEFINER`/
   `GRANT EXECUTE`-satserna för `mainai_app` (se "Databasbehörighetsmodell").
3. `apply_runtime_privileges`-steget i `docker-entrypoint.sh` (efter Alembic, före appstart)
   och motsvarande i den lokala Docker-init-vägen — se boot-härdningen ovan.
4. `app/rls.py`-uppdateringen för de tre nya tabellerna.
5. Den delade `purge_source()`-tjänsten och `delete_source`/`documents.py`s `delete_document`s
   omskrivning till att anropa den.
6. `delete_account`/`export_account`-ändringarna (kontoradering/export).
7. Testmatrisen, inklusive regressionstester för boot-härdningen och
   ägar-/behörighetsuppdelningen specifikt: (a) första provisionering+migration ger korrekta
   grants, (b) en ANDRA, vanlig boot får INTE tillbaka `UPDATE`/`DELETE` på de skyddade
   tabellerna, (c) `mainai_app` kan inte köra en direkt lifecycle-`UPDATE`/`DELETE`, (d)
   `mainai_app` KAN anropa `transition_own_memory_source`/`erase_owner_memory` på EGNA rader,
   (e) `mainai_app` kan INTE anropa `transition_own_memory_source` på en ANNAN ägares
   `source_id` (funktionen reser undantag), (f) `mainai_app` kan INTE anropa
   `transition_memory_source_admin` överhuvudtaget (`EXECUTE` saknas), (g) `PUBLIC` kan inte
   anropa någon av dem, (h) `mainai_app` saknar `CREATE` på schemat `public`.

### 4.9 `ConversationSegment` — analysfönster, versionshanterat

```
conversation_segments
  id, owner_id, conversation_id
  segmentation_version   -- vilken segmenteringsalgoritm som skapade detta segment
  boundary_reason         -- new_conversation | long_gap | new_topic | project_change |
                            -- explicit_return_to_thread | reply_reference
  created_by              -- vilken process/version som stängde segmentet
  start_message_id, end_message_id
  roles_present            -- METADATA för visning ("founder+assistant") — styr ALDRIG promotion,
                            -- se §6.10
  project_id               -- nullable
  created_at                -- timestamp WITH time zone (se "Tid och ordning" nedan)

conversation_segment_members
  segment_id, memory_source_id, ordinal
```

Segment är OFÖRÄNDERLIGA när stängda. En förbättrad segmenteringsalgoritm skapar en NY
`segmentation_version` och nya segment-rader — aldrig en tyst omskrivning av gamla. Gränser
härleds i första hand från `app/context/resolver.py`s redan existerande, testade signaler
(`new_topic`-klassificering, `LONG_GAP`=30 min) — ingen ny segmenteringsheuristik uppfinns
från grunden — utökat med projektbyte, explicit återgång till en tidigare tråd, och
reply/reference-länkar till ett äldre meddelande.

**Meddelandeordning — riktig ordinal, inte bara `created_at` (2026-07-28-korrigering).**
`Message` har idag bara `created_at`, ingen sekvenskolumn — två meddelanden med identisk
tidsstämpel (samma millisekund, eller en klocka som inte är monotont säker) kan inte
ordnas entydigt. Lägg till `Message.sequence_number` med `UNIQUE (conversation_id,
sequence_number)`: nya meddelanden får sin sekvens tilldelad vid sparande (nästa lediga
heltal för den konversationen), historiska meddelanden backfillas deterministiskt genom att
sortera på `(created_at, id)` och numrera i den ordningen. Alla NYA minnes-/segmenttabeller
(`memory_source_units.occurred_at`/`created_at`, `conversation_segments`, m.fl.) använder
`timestamp WITH time zone` — det framtida minnet måste kunna skilja lokal tid, UTC, och när
materialet faktiskt skapades, vilket en tidszonslös `timestamp` inte kan uttrycka entydigt.

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

**2026-07-28: samma princip, uttryckligt för konversationer.** Promotion (en claim/ett förslag
som når `interpretation_proposals.status=approved` och därigenom `project_entities`/
`founder_memory_notes`) avgörs av `memory_source_units.source_role` på claimens PRIMÄRA
källenhet (§4.8) — aldrig av `conversation_segments.roles_present`, som bara är visningsmetadata
för ett helt segment. Konkret:
- `source_role=founder` (claimens primära källa är grundarens eget meddelande): kan nå
  `pending` fritt via automatisk extraktion, precis som dokumentclaims idag — samma
  godkännandeflöde, ingen genväg.
- `source_role=assistant` (claimens primära källa är MainAI:s eget meddelande): lagras och
  analyseras — förslag, planer, kod, alternativ, felaktiga slutsatser som senare korrigerades
  är värdefull historik — men får ALDRIG ensamt nå `status=active`/en beslutsentitet.
  Promotion kräver ETT av: samma sakuppgift finns oberoende i en `founder`-sourcad claim,
  en explicit grundargodkännande på just det förslaget, eller ett senare `founder`-sourcat
  meddelande som uttryckligen bekräftar/ersätter det (en `project_entity_relationships`-rad
  med `relationship_type=derived_from` eller `supersedes` tillbaka till assistant-claimen).
- Ett segment med `roles_present={founder,assistant}` innehåller alltså BÅDA sorters claims
  samtidigt, korrekt attribuerade var för sig — segmentets egen "mixed"-status styr aldrig
  någon enskild claims promotion.

**2026-07-28-korrigering: kontextbevis och grounding får inte blandas ihop.** Om claimens
primärkälla är grundarens "Exakt" och kontextkällan är assistant-meddelandet som beskrev
alternativ B, blir dagens ordöverlappsbaserade `grounding_score` (§4.1, `app/rag/claims.py`)
mycket lågt mätt mot BARA primärkällan — "Exakt" delar nästan inga ord med "alternativ B".
Claim-extraktionen får därför INTE själv hoppa till en fullt upplöst text som "Beslut: kör på
alternativ B" — det vore att låta råtextraktionen göra ett tolkningssteg den inte har
underlag för. Rätt lager för det tolkningssteget är P4, inte extraktionen:
- Claimlagret (P3-extraktion) stannar vid den bokstavligt grundade texten: "Grundaren
  godkänner föregående förslag", med assistant-meddelandet kopplat som `context`-bevis (se
  §4.8/nedan för `knowledge_claim_evidence`s reviderade roller).
- P4:s tolkningssteg (inte rå extraktion) löser referensen till det konkreta tidigare
  förslaget och FÖRESLÅR den fullt upplösta `project_entities`-raden ("Beslut: alternativ B")
  — som ett `interpretation_proposals`-förslag, granskningsbart precis som alla andra.
- `knowledge_claim_evidence.evidence_role` blir `context | supports | contradicts |
  corroborates` — INTE `direct`, eftersom `KnowledgeClaim.memory_source_id` redan ÄR den
  direkta primärkällan; en fjärde "direct"-bevisroll vore en duplicerad sanning.
- Två separata mätvärden, inte ett: `primary_grounding_score` (ordöverlapp mot primärkällans
  egen text — dagens `grounding_score`, samma kolumn, bara begreppsmässigt förtydligad, ingen
  destruktiv omdöpning i S1) och ett SENARE `context_resolution_score` (hur väl P4:s föreslagna
  tolkning faktiskt stöds av kontextbevisen tillsammans) — att blanda "ordagrant stödd av
  primärkällan" med "semantiskt upplöst genom samtalskontext" till EN siffra skulle dölja
  precis den skillnad det här avsnittet handlar om. **2026-07-28-korrigering: `context_
  resolution_score` läggs INTE till i S1** — beräkningssätt, betydelse, skrivare och
  versionshantering är inte fastslaget förrän P4:s kontextupplösningslogik faktiskt byggs;
  att lägga till ett halvdefinierat fält i proveniensskiktet vore att låsa fast ett kontrakt
  ingen ännu vet formen på.

### 6.11 Konversationer som förstklassig minneskälla (P3/P4/P6 tillsammans, inte ett fjärde system)
`Conversation`/`Message` är redan episodiskt originalminne (lager 2, §2) — varje meddelande
bevaras rått och oförändrat, ingen filtrering vid ingestion. Det som saknas är att koppla dem
in i SAMMA `KnowledgeClaim`/`interpretation_proposals`/`project_entities`-kedja som dokument,
inte ett separat "chat memory". Konkret, utökar redan beskrivna paket snarare än att lägga
till ett nytt:
- **P3** (§6.3): samma `claim_type`-taxonomi, samma `_parse_claims`/prompt-mönster, applicerad
  på ett `conversation_segments`-fönster istället för en dokument-chunk — `extract_claims_for_
  conversation_segment` blir `extract_claims_for_document`s syskon, inte en konkurrent.
- **P4** (§6.4): läser claims oavsett ursprung (`memory_source_units.source_kind` ∈
  `{document_chunk, document_version, document_record}` ELLER `message`) —
  `interpretation_proposals`/`project_entities` skiljer inte på källtyp, bara på innehåll och
  `source_role`-baserad promotion (§6.10).
- **P6** (§6.6): Context Resolver (`app/context/resolver.py`) blir en PRIORITETSSIGNAL, inte
  den enda porten — en `INTENT_EXPLICIT_MEMORY`/`INTENT_IDEA_WORTH_SAVING`-klassad tur
  fast-trackas direkt (hög prioritet, låg latens till `founder_memory_notes`), medan den
  allmänna bakgrundspipelinen (segment-för-segment, §4.9) analyserar ALL konversationshistorik
  asynkront, inte bara de turer resolvern flaggar. Det löser den tidigare för snäva
  begränsningen ("bara explicit minne eller idé sparas") utan att göra resolverns redan
  testade, snabba signal överflödig.
- **Två separata backfills krävs**, inte en:
  1. Dokumentclaim-backfill: `claim_type`-omtypning av BEFINTLIGA claims (§4.1, byggd — se
     `app/rag/claims.py`s `backfill_claim_types`) OCH en separat extraktions-backfill för
     `indexed`-dokument som saknar claims helt (misslyckad/aldrig körd/noll-träff extraktion)
     — läser redan lagrade `DocumentChunk`-rader, ingen omindexering, deduplicerar på
     `(memory_source_id, normaliserad claim_text)`.
  2. Konversationsbackfill: historiska segment som aldrig analyserats — samma extraktion, bakåt
     i tiden, samma dedupnyckel.
  Båda körs som `memory_processing_jobs`-jobb (nedan), inte som obegränsade HTTP-anrop.

### 6.12 `memory_processing_jobs` — generell arbetskö för minnesbearbetning, inte `knowledge_import_jobs`

> **2026-08-07-uppdatering (S1B, Pass 42):** det här avsnittet skrevs INNAN
> `mainai_jobs`-runtimen (migrationerna 0026–0029, PR #36) fanns. Den runtimen levererar redan
> exakt det §6.12 efterfrågar — owner-scopad durabel jobbrad, lease + fencing-token, heartbeat,
> avbrytning, retry-budget, append-only händelsehistorik, auditlogg och admin-UI — och den
> dispatchar redan på `job_type` i `app/worker.py`. S1B:s historiska backfill blev därför ett
> nytt `job_type` (`message_sequence_backfill`) på DEN runtimen, inte en ny tabell. Att bygga
> `memory_processing_jobs` som en andra parallell kö vore precis det "ad hoc parallell
> mekanism"-mönster projektet upprepade gånger avvisat. Tabellskissen nedan står kvar som
> historik över kravbilden — den beskriver INTE något som ska byggas som egen tabell nu.
> Ett kvarstående, verkligt gap: `mainai_jobs` har ingen `payload`-kolumn och inget
> `partial`-status. Ingen av S1B:s behov krävde dem (jobbet tar inga indata alls, och en
> ofullständig körning rapporteras sanningsenligt i `public_message`), men ett framtida
> `job_kind` som faktiskt behöver parametrar bör lägga till en `payload`-kolumn på
> `mainai_jobs` — inte återuppliva en separat tabell.
`knowledge_import_jobs` är uttryckligen fil-/ZIP-specifik (`source_filename`, `source_checksum`,
`source_storage_key`, `file_results` per fil) — att lägga en `job_kind=conversation_backfill`
där hade gett en massa irrelevanta nullable filfält i varje konversationsjobb. Istället, en ny,
generell tabell som återanvänder EXAKT samma beprövade mönster (samma worker-container, samma
`app/jobs/lease.py`-principer):

```
memory_processing_jobs
  id, owner_id
  job_kind             -- claim_type_backfill | claim_extraction_backfill |
                        -- conversation_backfill | conversation_segment_extraction | ...
  payload               -- JSON, job_kind-specifikt (t.ex. batch-gräns, datumintervall)
  status                -- pending | running | completed | partial | failed | blocked
  progress_current, progress_total
  succeeded_count, failed_count, blocked_count
  attempt_count, max_attempts
  locked_by, lease_expires_at, last_heartbeat_at
  failure_reason
  created_at, started_at, completed_at
```

`app/worker.py`s pollloop utökas att claima BÅDE `knowledge_import_jobs` och
`memory_processing_jobs` (två separata `claim_next_job`-varianter, samma
`FOR UPDATE SKIP LOCKED`-mönster, samma lease/heartbeat/retry-kod) och dispatchar på
`job_kind`. `POST /api/admin/claims/backfill-types` (den befintliga, batch-begränsade
endpointen) blir en tunn kompatibilitetsväg som skapar ett `memory_processing_jobs`-jobb
istället för att köra arbetet inline, en gång den här tabellen finns.

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
| **RLS-glapp på BEFINTLIGA tabeller** (inte bara nya) | `messages` visade att mönstret ovan bara någonsin tillämpats på tabeller som var nya när regeln skrevs: `messages` fanns sedan `0001` och fick aldrig en egen policy, utan skyddades av att varje router kom ihåg att gå via `conversations`. Stängt av migration 0031 (§4.8). Generaliseringen: en tabell utan `owner_id` är inte automatiskt "inte ägarscopad" — den kan ha en HÄRLEDD ägare, och då ska policyn härleda den i databasen istället för att förlita sig på anropsordning i applikationskoden |
| **Zip-bomb via nästlad zip** | Delad, inte per-nivå, total byte-budget över alla nästlade nivåer (P2) |

---

## 8. Rekommenderad byggordning

**Reviderad 2026-07-28** efter grundlig granskning av den universella proveniensmodellen
(§4.8/§4.9/§6.10/§6.11/§6.12 — §4.8 är den konsoliderade slutdesignen, inte en punktlista över
granskningsrundor). P3 (claim-typning för dokument, §6.3) är byggd och mergad (PR #29) — allt
nedan är vad som ÅTERSTÅR, i ordning:

```
P1   Provider-förhandsverifiering        ← KLAR, mergad
P2   ZIP-hårdning för massimport         ← KLAR, mergad
P7A  Tidig governance-ingestion          ← kan börja när som helst efter P1/P2 — fryst,
                                            inget separat beslut taget än (docs/BRANCH_REGISTRY.md)
P3   Claim-typning (dokument)            ← KLAR, mergad (PR #29), inkl. retroaktiv backfill
                                            för BEFINTLIGA claims (backfill_claim_types)

--- återstår, ny ordning efter proveniens-granskningen (S1 delat i S1A/S1B/S1C, §4.8) ---

S1A  memory_source_units + document_source_units + KnowledgeClaim.memory_source_id
     (nullable) + snapshot/lifecycle/immutability + memory_source_lifecycle_events +
     deterministisk dokumentclaim-backfill + dual-write + delete_source-purge-integration
                                          ← additiv migration, egen PR. INGA meddelandetabeller,
                                            INGEN knowledge_claim_evidence (flyttad till S1C/P4
                                            — ingen aktiv writer i S1A). Nästa konkreta steg —
                                            se §4.8:s "Status: PR #30 kontra S1A-implementations-PR:n".
S1B  messages.sequence_number: expand (nullable) → dual-write-kod → durable historisk
     backfill → verifiering → contract (separat migration: NOT NULL + UNIQUE)
                                          ← egen PR, kräver S1A inte alls (oberoende spår).
                                            EXPAND + DUAL-WRITE + BACKFILL + VERIFIERING är
                                            BYGGDA (migration 0030, PR för
                                            claude/s1b-message-sequence-number, Pass 42).
                                            CONTRACT återstår och är GATED på att backfillen
                                            faktiskt körts mot produktionsdata och att
                                            count_unsequenced_messages() rapporterar 0 —
                                            se §4.8:s S1B-statusavsnitt.
S1B-RLS  messages egen RLS-policy (migration 0031)
                                          ← KLAR (byggd, egen branch/PR, se Pass 43 i
                                            docs/BRANCH_REGISTRY.md). Litet, fristående
                                            säkerhetssteg — INTE en del av S1B eller S1C, och
                                            INTE gated på produktionsbackfillen: policyn
                                            härleder ägaren ur `conversations` och är korrekt
                                            oavsett om `sequence_number` är NULL, ifylld eller
                                            halvvägs. Bör granskas/mergas FÖRE S1C, som är
                                            det första som skannar `messages` i bulk.
S1C  message_source_units + knowledge_claim_evidence + ägar-/konversationsintegritet +
     backfill Message→MemorySourceUnit
                                          ← kräver S1B:s sequence_number klart, egen PR.
                                            Fortfarande ingen claim-extraktion från konversationer.
S2   conversation_segments + conversation_segment_members + segmenteringspass
                                          ← kräver S1C, återanvänder app/context/resolver.py:s
                                            new_topic/LONG_GAP-signaler (§4.9), egen PR
S3   memory_processing_jobs + worker-dispatch på job_kind
                                          ← egen PR, oberoende av S1A–S2, ingen ny
                                            beteendepåverkan än — bara infrastrukturen §6.12 beskriver
S4   Dokumentclaim-extraktionsbackfill (saknade claims på indexed dokument, §6.11 punkt 1)
                                          ← kräver S1A+S3, körs som ett memory_processing_jobs-jobb
S5   extract_claims_for_conversation_segment (dual-write memory_source_id från start) +
     konversationsbackfill (historiska segment, §6.11 punkt 2)
                                          ← kräver S2+S3, ny källa in i SAMMA KnowledgeClaim-tabell
P6   Grundarminne + korrigeringsloop     ← kan byggas parallellt med P4 (ingen hård
                                            beroendekedja), men läser nu BÅDE resolverns
                                            snabbspår OCH S5:s bakgrundspipeline (§6.11)
P4   Tolkningskö + relationer + karta    ← det stora paketet, kräver P3 (klar) — sorterar
                                            claims oavsett källa (dokument eller konversation)
P5   Första förståelserapporten          ← kräver P4
P7B  Governance enforcement              ← FORTFARANDE SIST, kräver P4:s
      (aktivering av konstitutionen)        godkännandeinfrastruktur bevisad
```

P1/P2/P3 blockerar inte längre något (klara). P7A kan fortfarande börja när som helst,
oberoende av S1–S5/P4/P6. S1–S3 är ren infrastruktur (ingen ny data skapas, inget nytt
beteende) och bör granskas/mergas FÖRE S4/S5 så att dokument- och konversationsbackfillen
byggs direkt mot rätt tabellform istället för att behöva göras om. P7B förblir sist eftersom
AKTIVERING (till skillnad från ingestion) kräver att dubbelspärr- och granskningsmönstret
redan är byggt och beprövat på lägre-risk-lager (P4) först.

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

---

## 10. P2 — ZIP-hårdning för massimport: exakt implementationsplan

**Status:** designförslag, grundat i faktisk läsning av koden på commit `5690aa21c9c799a06deca3c5994878a5c93a6bcd`
(P1, godkänd och levererad som PR #7, draft). Ingen kod skriven, ingen migration körd, ingen
merge, ingen deploy. Nästa paket i byggordningen (§8) efter P1 — det andra av de två paket som
blockerar en säker första massuppladdning av dina riktiga arkiv.

### 10.1 Fasens exakta mål

Tre konkreta, var för sig testbara mål — inget annat:

1. **Nästlad ZIP hanteras säkert**, inte tyst hoppas över. En zip inuti en zip (upp till ett
   bestämt djuptak) packas upp och dess innehåll importeras precis som toppnivåfiler, med en
   **delad** total byte-budget över alla nivåer — aldrig en ny, oberoende 200 MB-budget per
   nästlad nivå (det vore en konkret zip-bomb-lucka: ett arkiv som är säkert på varje enskild
   nivå men exploderar i total datamängd när nivåerna summeras).
2. **Lösenordsskyddade/krypterade arkiv eller filer flaggas explicit och tydligt** — en egen,
   namngiven status, aldrig en generisk "kunde inte läsa filen"-rad. Ingen lösenordsprompt,
   ingen gissning, inget brute-force-försök — bara en ärlig, specifik flagga.
3. **`MAX_FILES` omprövas mot en riktigt uppmätt kapacitetsgräns**, inte en gissning. Antingen
   höjs den till ett nytt, uppmätt-säkert värde, eller så bekräftas 500 vara rätt gräns — i
   båda fallen med ett riktigt kapacitetstest som bevis, samma disciplin som durable-workerns
   egen resursmätning (PR #6).

### 10.2 Vad som redan finns (bekräftat genom läsning av `app/rag/zip_import.py`,
`app/routers/library.py`, `docs/KNOWLEDGE_IMPORT_SECURITY.md`)

- **Path traversal/Zip Slip-skydd** (`_is_safe_member_name`, segmentkontroll via
  `PurePosixPath.parts`) — orört av P2, gäller identiskt på varje nästlingsnivå.
- **Tvålagers zip-bomb-skydd**: ett metadata-baserat komprimeringskvotfilter
  (`MAX_COMPRESSION_RATIO=100`) som körs FÖRE någon dekomprimering, plus `_read_with_hard_cap()`
  som strömmar och avbryter direkt om verklig data överskrider gränsen — litar aldrig på
  zip-arkivets egna storleksuppgifter. Båda lagren återanvänds oförändrade per nästlingsnivå.
- **`MAX_FILES=500`**, **`MAX_TOTAL_UNCOMPRESSED_BYTES=200 MB`**,
  **`MAX_SINGLE_FILE_UNCOMPRESSED_BYTES=25 MB`** — dagens, oprövade konstanter.
- **`MAX_UPLOAD_BYTES=60 MB`** i `app/routers/library.py` — ett separat tak på själva
  HTTP-uppladdningens rådata (den komprimerade storleken), innan zip-motorn ens öppnar filen.
  **Rörs inte av P2** (se §10.9) om inte kapacitetstestet visar ett konkret behov.
- **`EXECUTABLE_EXTENSIONS`-blockering** och **magic-byte-verifiering** (PDF/DOCX) — gäller
  identiskt på varje nästlingsnivå, ingen ändring.
- **`ZipEntryResult`** (`status: str` — redan en fri sträng, inte en databas-enum) — dagens
  värden `"ok"`/`"skipped"`/`"rejected"`. Eftersom fältet redan är en vanlig sträng, inte en
  Postgres-enum, kan nya statusvärden läggas till utan migration (se §10.4).
- **Redan durabelt lagrat**: det YTTRE zip-arkivets råa bytes (`ImportJob.source_storage_key`,
  PR #6) — grunden P2:s proveniens för nästlade nivåer bygger vidare på (se §10.6).
- **Flexibla JSON-fält utan schemaändring**: `ImportJob.manifest`/`file_results` och
  `KnowledgeVersion.raw_metadata` — redan tillräckligt fria för att bära den nya
  nästlingsmetadatan utan migration (se §10.4).
- **`docs/KNOWLEDGE_IMPORT_SECURITY.md`** — befintlig hotmodell, rad "Resursuttömning via
  filantal/total storlek" pekar redan på exakt de konstanter P2 omprövar.

### 10.3 Vad som saknas

- **Ingen nästlad ZIP-hantering alls.** `.zip` finns inte i `ALLOWED_EXTENSIONS` — en zip
  inuti en zip blir idag `status="skipped", reason="Filtypen .zip stöds inte."`, exakt som
  vilken annan otillåten filtyp som helst. Inget djuptak, ingen delad budget, eftersom inget
  av detta någonsin triggas.
- **Ingen explicit kryptering/lösenordsdetektion.** `zipfile` kastar ett `RuntimeError`
  ("... is encrypted, password required for extraction") när `zf.open(info)` anropas på en
  krypterad post — det fångas idag av det generella `except Exception` i
  `validate_and_extract_zip()`, blir `status="rejected", reason=f"Kunde inte läsa filen:
  {exc}"`. Ordet "encrypted" råkar synas i den råa undantagstexten, men det finns ingen
  strukturerad, namngiven status en founder eller ett UI kan lita på — exakt det generiska
  beteende du bad om att få bort.
- **Inget uppmätt kapacitetstak.** 500 filer/200 MB är rimliga, försiktiga gissningar från
  FKS-v1, aldrig belastningstestade mot den riktiga workerns faktiska genomströmning (en
  sekventiell worker, `worker_concurrency=1`, ett `db.commit()` per fil).
- **Ingen provenienskedja för nästlingsnivåer.** `KnowledgeVersion.raw_metadata` innehåller
  idag bara `{"original_filename", "size_bytes", "media_type"}` för toppnivåfiler — ingenting
  som säger "den här filen låg i `sub/inner.zip`, som i sin tur låg i det yttre arkivet."

### 10.4 Datamodeller och migrationer

**Ingen ny migration.** Detta är en medveten, verifierad slutsats, inte en utelämnelse:

- `ZipEntryResult.status` är redan en fri Python-sträng (aldrig en Postgres-enum) — ett nytt
  värde `"encrypted"` kräver ingen `ALTER TYPE`.
- `ImportJob.file_results` (redan `JSON`) kan bära ett nytt, valfritt fält per post,
  `"archive_path": [...]`, utan schemaändring.
- `KnowledgeVersion.raw_metadata` (redan `JSON`) kan på samma sätt bära en ny nyckel,
  `"zip_chain"` (se §10.6) — samma mönster som `extraction_version`/`original_filename`
  redan använder.
- Nästlade zip-arkiv får **ingen egen rad i `provider_verification_checks`-stil eller egen
  `storage_key`** — de lagras aldrig separat. Se §10.6 för varför det är en medveten
  arkitekturprincip, inte en genväg: det yttre arkivets bytes är redan den enda källan som
  behövs för att deterministiskt återskapa exakt samma nästlade arkiv och exakt samma filer,
  varje gång — att lagra dem igen vore att duplicera något som redan är fullt återhärledbart,
  precis som `app/storage/`s innehållsadressering redan vägrar duplicera identiskt innehåll.

Om kapacitetstestet (§10.7) visar att `MAX_FILES` bör höjas är det en ren konstantändring i
`app/rag/zip_import.py` — ingen migration, eftersom gränsen aldrig var en databaskolumn.

### 10.5 API- och worker-flöden

**`app/rag/zip_import.py` — den enda modulen som ändras strukturellt:**

```
validate_and_extract_zip(raw, *, max_files=..., max_total_bytes=..., max_file_bytes=...,
                          max_nesting_depth=3)
    -> anropar en ny intern _extract_recursive(zf, *, depth, archive_path, budget) som:
       - för varje post: samma path-traversal/exekverbar-/magic-byte-kontroll som idag,
         OFÖRÄNDRAT, oavsett djup
       - suffix == ".zip": FÖRSÖK öppna som nästlat arkiv (inte "skippa som okänd typ"):
           - depth >= max_nesting_depth -> status="rejected",
             reason="Nästlingsdjupet överskrider gränsen (max {max_nesting_depth} nivåer)."
           - annars: läs posten med SAMMA _read_with_hard_cap (samma delade `budget`-räknare,
             inte en ny), försök zipfile.ZipFile(io.BytesIO(nested_bytes)), rekursera med
             depth+1 och archive_path + [{"filename": name, "checksum": sha256(nested_bytes)}]
           - ogiltigt nästlat arkiv (BadZipFile) -> status="rejected",
             reason="Nästlat arkiv kunde inte läsas (skadat eller inte en riktig ZIP)."
             — precis som idag för toppnivån, avbryter INTE hela importen
       - zf.open(info) kastar RuntimeError/NotImplementedError vid kryptering -> NY,
         EXPLICIT except-gren (inte det generella `except Exception`):
             status="encrypted",
             reason="Filen/arkivet är lösenordsskyddat — kan inte läsas automatiskt."
       - annars: exakt samma flöde som idag (magic bytes, checksum, content)
       - varje ZipEntryResult får två nya, valfria fält (se §10.6 för det exakta formatet):
           archive_path: str | None    -- människoläsbar sökväg, t.ex.
                                           "backup.zip!/users/docs.zip!/contracts/lease.pdf"
           archive_chain: list[dict] | None  -- samma kedja, maskinverifierbar (checksum per nivå)
         (båda None för toppnivåfiler — bakåtkompatibelt, oförändrat för icke-nästlade paket)
```

**Den avgörande regeln, uttryckt konkret:** `budget` (den ackumulerade
`total_uncompressed`-räknaren) skickas **som samma objekt/referens** in i varje rekursivt
anrop — en nästlad nivås byte-räkning LÄGGS TILL den redan ackumulerade summan, den startar
aldrig om från noll. Ett arkiv där varje enskild nivå ser ut att vara under 200 MB, men vars
SAMMANLAGDA uppackade data över alla nivåer överskrider 200 MB, avvisas — det är den konkreta,
testbara definitionen av "delad, inte per-nivå" (redan skriven in i riskmatrisen i §7).

**`app/rag/library_import.py` — ingen strukturell ändring.** `_run_once()`s loop över
`zip_result.entries` är redan flat (en lista `ZipEntryResult`, oavsett hur många nästlingsnivåer
som producerade dem — rekursionen händer helt inuti `zip_import.py`, `library_import.py` ser
bara den färdiga, platta listan). Den enda ändringen: `_manifest_entry_for()`/
`_import_one_file()`-anropet skickar med `entry.archive_path` in i `KnowledgeVersion`s
`raw_metadata` (se §10.6) — en enda ny nyckel i en dict som redan byggs.

**`app/worker.py` — ingen ändring alls.** Nästlad uppackning är ren, synkron Python-logik utan
någon AI-provider inblandad — påverkar varken pre-flight-verifieringen (P1) eller
återköningsmekanismen.

**Ny status i `FileOutcome`/`ImportJob.file_results`:** `"encrypted"` läggs till som ett giltigt
värde, bredvid `"indexed"/"duplicate"/"failed"/"skipped"/"cancelled"/"blocked"`. **Inte
återupptagningsbar** i P1:s mening — ett lösenordsskyddat arkiv väntar inte på att något ska
"bli klart", det kräver en mänsklig åtgärd (en ny export utan lösenord) som varken workern
eller en framtida nyckelrättning kan lösa automatiskt. Räknas därför som `skipped`/`rejected`
i jobbnivåns `succeeded`/`failed`/`blocked`-summering (§4.7s modell rörs inte) — bara
diagnostiktexten blir tydligare.

### 10.6 Provenienskrav

#### 10.6.1 `archive_path` — exakt format

Varje fil som kommer från ett nästlat arkiv får en stabil, människoläsbar sökväg som visar
HELA kedjan av innesluta arkiv, till exempel:

```
backup.zip!/users/docs.zip!/contracts/lease.pdf
```

**Konstruktion, deterministisk, ren funktion av indata:**

```
archive_path = "!/".join(segment for segment in [outer_filename, *inner_paths, final_path])
```

där varje `segment`:

- är den **normaliserade** posten (`name.replace("\\", "/")`, ALDRIG rå Windows-backslash i
  slutresultatet — samma normalisering `_is_safe_member_name()` redan gör internt, bara nu
  även använd för att BYGGA strängen, inte bara validera den),
- redan har passerat `_is_safe_member_name()` OFÖRÄNDRAT — path traversal (`..`), absoluta
  sökvägar och enhetsbokstäver är alltså strukturellt omöjliga i `archive_path`, inte bara
  "osannolika": om en post inte klarar den kontrollen avbryts hela importen (`ZipSecurityError`,
  oförändrat beteende) INNAN någon `archive_path` ens byggs för den,
- separeras med **`!/`** mellan varje arkivgräns (aldrig bara `/`, som inte skulle gå att
  skilja från en vanlig undermapp) — samma konvention som JAR-URL:er redan använder för
  nästlade arkiv, valt för att vara otvetydigt och plattformsoberoende, inte en egen
  uppfinning.

**Determinism, konkret:** samma yttre arkiv-bytes + samma interna sökväg ger ALLTID samma
`archive_path`-sträng — konstruktionen använder bara postens namn (redan en fast egenskap av
arkivets bytes, inte något som beror på bearbetningsordning, tidsstämplar eller
dict-iterationsordning i Python). Två OLIKA uppladdningar av identiskt innehåll under olika
filnamn ger olika `archive_path`-strängar (den är beskrivande för DEN HÄR importen, inte en
innehålls-identitet) — det är `archive_chain`s checksummor och `Document.checksum` som redan
ger den strikta, innehållsbaserade identiteten, oförändrat.

#### 10.6.2 Den fullständiga kedjan

```
Document.storage_key (den extraherade filens EGEN durabla blob, oförändrat från idag)
  -> Document.import_job_id
  -> ImportJob.source_storage_key (det YTTRE arkivets EGEN durabla blob — den enda som lagras)
  -> KnowledgeVersion.raw_metadata["archive_path"] = "backup.zip!/users/docs.zip!/contracts/lease.pdf"
  -> KnowledgeVersion.raw_metadata["archive_chain"] = [
       {"filename": "backup.zip", "checksum": "<= ImportJob.source_checksum>"},
       {"filename": "users/docs.zip", "checksum": "<sha256 av det nästlade arkivets bytes>"},
       {"filename": "contracts/lease.pdf", "checksum": "<sha256 av den extraherade filen, = Document.checksum>"}
     ]
```

`archive_path` (sträng) är den människoläsbara, visningsbara varianten. `archive_chain`
(strukturerad lista) är samma kedja fast maskinverifierbar — varje nivås checksumma, inklusive
den yttersta som redan är identisk med `ImportJob.source_checksum`. Båda beräknas en gång, vid
import, och sparas — de räknas aldrig om i efterhand, så en framtida källhänvisning (chatt,
Workbench) kan visa "från `backup.zip!/users/docs.zip!/contracts/lease.pdf`" direkt från redan
lagrad metadata utan att öppna arkivet igen. **Att faktiskt koppla in `archive_path` i en
körande källhänvisning (`SourceRef`, `chat.py`) är INTE en del av P2** (se §10.9) — P2:s jobb
är bara att fältet finns, är korrekt ifyllt och aldrig behöver räknas om senare.

**Varför nästlade arkiv inte behöver sin egen `storage_key`:** determinism. Samma yttre
zip-bytes producerar, varje gång de packas upp igen, exakt samma nästlade arkiv och exakt
samma extraherade filer (zip-formatet har inget icke-deterministiskt inslag som skulle ändra
det). Att lagra en nästlad zip separat vore att spara en andra kopia av data som redan är
100 % återhärledbar från den första — samma princip som redan gäller för identiskt filinnehåll
i `app/storage/local_fs.py`s innehållsadressering. Om du (grundaren) senare vill se exakt vilket
nästlat arkiv en fil kom ifrån räcker `archive_chain`-metadatan + en omkörning av
`validate_and_extract_zip()` mot det redan lagrade yttre arkivet — ingen extra lagring krävs
för att kunna svara på den frågan.

#### 10.6.3 Var `archive_path`/`archive_chain` sparas — inga nya kolumner

- **`ImportJob.file_results`** (redan `JSON`): varje post får de två nya, valfria nycklarna
  `"archive_path"`/`"archive_chain"` — samma dict som redan byggs av `FileOutcome.__dict__`
  i `_run_once()`, bara två nya fält på `FileOutcome`-dataclassen (`archive_path: str | None`,
  `archive_chain: list[dict] | None`, båda default `None`).
- **`KnowledgeVersion.raw_metadata`** (redan `JSON`): samma två nycklar, satta av
  `_import_one_file()`/`_resume_blocked_document()` när `KnowledgeVersion`-raden skapas — exakt
  samma mönster som `original_filename`/`size_bytes`/`media_type` redan sparas där idag.
- **Ingen ny kolumn på `Document`.** `Document.checksum` är redan den extraherade filens egen
  checksumma (= sista posten i `archive_chain`) — ingen dubblering.
- **Bekräftat: ingen migration krävs** för `archive_path`/`archive_chain` — samma slutsats som
  §10.4 redan drog för nästlingsstödet i stort, nu även uttryckligen för proveniensfälten.

### 10.7 Kapacitetstest (kravet, inte bara en idé)

Ett nytt, dedikerat testscenario (inte en del av de vanliga enhetstesterna, körs separat och
rapporteras med siffror, samma stil som PR #6:s resursmätning):

1. Bygg ett syntetiskt ZIP-paket med **minst 2 000–5 000 små textfiler** (realistisk storlek
   för "flera tusen filer", inte en extrapolering från 500).
2. Kör det genom **hela, riktiga pipelinen** — HTTP-intag → `app/worker.py` →
   `validate_and_extract_zip` → `_import_one_file` per fil → embedding (mockad leverantör,
   ingen riktig kostnad) → `indexed` — inte bara `zip_import.py` isolerat.
3. Mät verklig tid (worker är `worker_concurrency=1`, sekventiell, ett `db.commit()` per fil
   — den dominerande kostnaden är sannolikt antalet databas-round trips, inte
   uppackningen i sig) och peak-minnesanvändning.
4. **Beslut baserat på siffrorna, inte en gissning:** antingen bekräftas `MAX_FILES=500` vara
   en rimlig gräns (med den uppmätta tiden/minnet som motivering i kommentaren i
   `zip_import.py`), eller höjs den till ett nytt, uppmätt-säkert värde. Om ett paket med
   flera tusen filer visar sig ta orimligt lång tid (t.ex. tiotals minuter) dokumenteras det
   explicit som en känd begränsning snarare än att gränsen höjs blint.

#### 10.7.1 Resultat (kört, inte gissat)

`tests/backend/test_zip_import_capacity.py::test_capacity_2500_files_through_the_real_import_pipeline`
(körs explicit via `RUN_CAPACITY_TEST=1`, inte del av standardsviten — se filens egen
docstring för varför):

- **Paket:** 2 500 filer, 2 nästlingsnivåer (region.zip → shard.zip → textfiler), 48 210 bytes
  totalt (realistiskt för textdokument, inte en byte-tung stress-test).
- **Väg:** den riktiga `run_import_job()` — riktig Postgres (inkl. RLS), riktig
  lagringsbackend, mockad men strukturellt realistisk embedding-/chattleverantör (samma mock
  som resten av `test_library_import.py` använder) — inte `zip_import.py` isolerat.
- **Mätt tid:** 447,7 s totalt, 179,1 ms/fil.
- **Mätt minne:** 11,3 MB Python-heap-topp (tracemalloc), 175,0 MB processens RSS-vattenmärke
  (oförändrat före/efter — ingen minnesläcka över 2 500 filer).
- **Beslut: `MAX_FILES=500` behålls oförändrad**, medvetet INTE höjd. Motivering: den uppmätta
  kostnaden per fil är dominerad av databas-rundresor (ett `db.commit()` per fil, sekventiell
  worker), exakt som förväntat — men mätningen mockar leverantörsanropen och fångar därför
  INTE den riktiga nätverkslatensen mot en verklig embedding-/chattleverantör, som i
  produktion sannolikt dominerar över DB-kostnaden. Att höja gränsen på enbart den här siffran
  vore precis den gissning kravet i §10.7 uttryckligen förbjuder. Vid 500 filer ger den
  uppmätta takten en bästa-scenario-tid på ~90 s — en rimlig, konservativ gräns. Se
  kommentaren ovanför `MAX_FILES` i `app/rag/zip_import.py` för samma motivering i koden.
- **Känd begränsning:** ingen mätning finns ännu mot en riktig (icke-mockad) AI-leverantör —
  om `MAX_FILES` ska omprövas igen bör det ske med ett motsvarande test mot en verklig
  leverantör, inte en extrapolering av den här mätningen.

### 10.8 Tester

- `test_nested_zip_one_level_extracts_and_indexes_normally`
- `test_nested_zip_at_max_depth_boundary_still_extracts` (exakt `max_nesting_depth`-nivåer)
- `test_nested_zip_exceeding_max_depth_is_rejected_not_the_whole_import`
- `test_nested_zip_shared_budget_rejects_a_bomb_that_looks_small_per_level` — **den
  viktigaste säkerhetstesten i hela paketet**: konstruerar ett arkiv där varje enskild nivå
  ligger klart under `MAX_TOTAL_UNCOMPRESSED_BYTES`, men den ackumulerade summan över alla
  nivåer överskrider den — bevisar konkret att budgeten delas, inte återställs per nivå.
- `test_corrupt_nested_archive_is_rejected_as_one_entry_not_the_whole_batch`
- `test_encrypted_top_level_entry_gets_a_distinct_encrypted_status_not_generic_rejected`
- `test_encrypted_entry_inside_a_nested_archive_is_still_correctly_classified`
- `test_encrypted_status_is_never_treated_as_resumable_by_the_worker` — bevisar att
  `"encrypted"` INTE hamnar i `ImportJobStatus.blocked` eller återköas av
  `_requeue_blocked_jobs` (skiljer den tydligt från P1:s `awaiting_provider`/`blocked_provider`).
- `test_archive_chain_provenance_is_recorded_correctly_for_a_nested_file` — verifierar att
  `KnowledgeVersion.raw_metadata["archive_chain"]` innehåller rätt filnamn och checksummor för
  en fil två nivåer ner, inklusive att den yttersta postens checksumma matchar
  `ImportJob.source_checksum`.
- `test_archive_path_matches_the_documented_format_exactly` — bevisar konkret
  `"backup.zip!/users/docs.zip!/contracts/lease.pdf"`-formatet (rätt separator `!/` mellan
  varje arkivgräns, inte bara mellan mappar inuti samma arkiv).
- `test_archive_path_is_deterministic_across_repeated_imports_of_identical_bytes` —
  packar upp samma arkiv två gånger (t.ex. via en riktig re-upload), verifierar att
  `archive_path` blir en identisk sträng båda gångerna.
- `test_archive_path_never_contains_raw_backslashes_or_traversal_segments` — matar in ett
  konstgjort postnamn med `\`-separatorer (vanligt i Windows-skapade zip-filer) och bevisar att
  den resulterande `archive_path`-strängen bara innehåller `/`, aldrig `\`, och att en post som
  inte klarar `_is_safe_member_name()` aldrig når fram till att få ett `archive_path` alls
  (hela importen avbryts innan, oförändrat beteende).
- `test_top_level_files_have_no_archive_path_unchanged_from_before_p2` — regressionsskydd:
  ett icke-nästlat paket beter sig exakt som idag (`archive_path`/`archive_chain` båda `None`).
- `test_existing_500_file_and_200mb_limits_still_enforced_at_top_level` — regressionsskydd
  för dagens redan testade gränser.
- Kapacitetstestet (§10.7) som ett eget, tydligt namngivet test/skript med rapporterade
  siffror i testoutputet eller en kort separat rapport — inte bara ett tyst pass/fail.
- `docs/KNOWLEDGE_IMPORT_SECURITY.md` uppdateras med två nya rader i attackyta 1-tabellen
  (nästlad zip-bomb via delad budget, explicit kryptering-detektion) — granskad som en del av
  leveransen, inte bara koden.

### 10.9 Vad som uttryckligen INTE ingår i P2

- **Inget lösenordsstöd.** Ingen UI för att ange ett lösenord, inget försök att öppna ett
  krypterat arkiv med ett gissat/vanligt lösenord, ingen brute-force. Ett krypterat arkiv
  flaggas — punkt. (Redan ett hårt krav från din allra första begäran i den här sessionen.)
  Om lösenordsstöd någonsin blir aktuellt är det ett eget, separat, explicit beslut senare.
- **Ingen ändring av `MAX_SINGLE_FILE_UNCOMPRESSED_BYTES` (25 MB) eller `MAX_UPLOAD_BYTES`
  (60 MB, HTTP-intagets råa gräns).** P2 rör filANTAL och nästling, inte enskild filstorlek
  eller den yttre uppladdningens byte-tak — om inte kapacitetstestet (§10.7) konkret visar att
  någon av dem också behöver omprövas, i så fall som ett explicit, separat beslut.
- **Ingen malware-/antivirusskanning** — redan uttryckligen utanför scope i
  `docs/KNOWLEDGE_IMPORT_SECURITY.md`, oförändrat.
- **Ingen frontend-förändring garanterad.** Library-UI:ts uppladdningskö (`lib/uploadQueue.tsx`,
  `app/(shell)/library/page.tsx`) visar idag bara jobbnivåns `failure_reason` — INTE per-fil
  `file_results[].reason` alls. Utan en separat, uttrycklig frontend-uppgift kommer det nya
  `"encrypted"`-värdet och `zip_chain`-provenienstexten synas i API-svaret
  (`GET /api/library/jobs/{id}`) och i tester, men INTE automatiskt bli synligt för dig i
  Library-gränssnittet. Flaggat här explicit, inte tyst antaget löst — säg till om du vill att
  det inkluderas som en liten, avgränsad frontend-uppgift i samma paket eller som en egen,
  senare uppföljning.
- **Ingen P3–P7-funktionalitet** — ingen claim-typning, ingen relationsupptäckt, ingen
  governance, inget grundarminne. P2 är uteslutande `app/rag/zip_import.py` och den smala
  provenienstillägget i `library_import.py`.
- **Ingen ändring av P1:s provider-verifieringslogik** — nästlad uppackning är ren,
  AI-fri Python-logik, rör aldrig `ensure_verified()`/`_requeue_blocked_jobs()`.
- **`archive_path`/`archive_chain` kopplas INTE in i `SourceRef`/chat-källhänvisningar i P2.**
  Fälten sparas fullständigt och korrekt vid import (§10.6), men att faktiskt VISA
  "från backup.zip!/users/docs.zip!/..." i ett chattsvar eller i Workbench är en separat,
  senare uppgift (`app/schemas.py`s `SourceRef`, `chat.py`) — precis som §6.9 i den övergripande
  planen redan säger att bounded-retrieval-frågor hör till P4:s frågelager, inte P2.
