# Life Library / Life Knowledge Studio / LifeCast — arkitektur och fasplan

**Status: PLANERAD, EJ PÅBÖRJAD.** Detta dokument beskriver en framtida MainAI-fas på
arkitekturnivå. Ingen kod i det här dokumentet är byggd eller ska byggas förrän den
uttryckliga spärren nedan är uppfylld.

> **Byggspärr:** implementation påbörjas inte förrän den nuvarande Render-driftsättningen är
> stabil och kontoflödet (registrering, e-postverifiering, inloggning, lösenordsåterställning)
> fungerar tillförlitligt i produktion — se `docs/RENDER_DEPLOY.md` för aktuell status. Den
> här planen är avsiktligt skriven nu, medan arkitekturen är färsk, men den är dokumentation,
> inte ett åtagande om när arbetet startar.

## 1. Vad det här är

Tre arbetsnamn för samma sammanhängande fas — en fördjupning av det kunskapsbibliotek MainAI
redan har (RAG-lagret, se `docs/ARCHITECTURE.md` §2.2–2.4), inte ett separat system:

- **Life Library** — källor och samlingar. Import, proveniens, multimodal extraktion och
  sökbarhet för allt användaren lägger in: dokument, webbsidor, bilder, ljud, video.
- **Life Knowledge Studio** — analys och genererade kunskapsprodukter ovanpå biblioteket:
  sammanfattningar, rapporter, tidslinjer, tankekartor, quiz, flashcards, jämförelser.
- **LifeCast** — podcast-/ljudproduktion: källgrundade manus, syntetiskt tal med valbara
  röster, automatiska shownotes, senare interaktiva avsnitt.

Studio och LifeCast är konsumenter av Library — de kan inte byggas innan Library:s
provenienskedja och Trust Engine-utökning finns, eftersom båda måste kunna citera exakt
varifrån varje påstående kommer.

## 2. Koppling till befintlig MainAI — vad återanvänds, vad är nytt

| Befintligt (finns i repot idag) | Roll i denna fas |
|---|---|
| RAG-lagret (`backend/app/rag/`: chunkning, embedding, Qdrant-lagring, retrieval) | Bas att bygga vidare på — Library lägger till fler extraktionstyper (OCR, tal, scenbyten) som matar in i samma chunkning/embedding-pipeline, inte en parallell |
| `Document`-modellen (`backend/app/models/document.py`) | För smal för denna fas (en källtyp, ingen proveniens/version/behörighet, ingen multimodal metadata) — se §3, ersätts inte utan **utökas via ett nytt, generellt objektlager ovanpå** |
| Trust Engine (`backend/app/rag/trust.py`, `assess_confidence`) | Utökas — se §5. I dag: en enda confidence-nivå per svar från topp-likhet. Denna fas kräver att varje enskilt påstående kan märkas separat |
| Provider-abstraktionen (`LLMProvider`-protokollet, `backend/app/providers/`) | Mönstret återanvänds rakt av för nya providertyper (transkribering, OCR, TTS) — se §7, inte en ny abstraktionsstil |
| `UsageLog` (`backend/app/models/usage.py`, `role: "chat" \| "embedding"`) | Utökas med fler roller (`transcription`, `vision`, `tts`, `diarization`) — samma tabell, samma admin-summeringsvy, ingen ny kostnadsloggmekanism |
| RLS + per-användare ägarskap (`user_id`-FK + Postgres Row-Level Security) | Behörighetsmodellen denna fas bygger vidare på — se §8. Inget nytt multi-tenant-lager: repot är enanvändar-/enföretagsmodell idag (`CompanyInfo` är global nyckel/värde, inte per-tenant), och det ändras inte av denna fas |
| `docs/AUTH_THREAT_MODEL.md`, audit-loggen (`AuditLog`) | Radering/samtycke/rättighetshändelser (§9) loggas i samma audit-tabell, samma mönster som kontohändelser idag |

**Universal Object Model** och **Knowledge Objects** (nämnda i uppdraget) existerar **inte**
i repot idag — de är den nya abstraktion den här fasen introducerar, beskriven i §3. De är
inte ett dolt lager som redan finns; det är viktigt att vara tydlig med det här så att den
här planen inte läses som en beskrivning av nuvarande kod.

## 3. Universal Object Model (UOM) — den nya kärnabstraktionen

Dagens `Document` beskriver bara "en uppladdad fil". Library/Studio/LifeCast behöver att
**allt** — källa, extraherad text, genererad sammanfattning, podcastavsnitt, quiz — är samma
sorts spårbara, behörighetsstyrda objekt, så att en genererad rapport kan citera ett
videoklipp precis lika exakt som ett stycke i ett PDF-dokument.

```
KnowledgeObject (abstrakt bas — konceptuell, inte nödvändigtvis en enda DB-tabell)
├── id, object_type, owner_id, collection_id[]
├── provenance: original_source_ref, checksum (sha256), imported_at, imported_by
├── permission: visibility, consent_flags (se §9), rights_basis (se §9.2)
├── version_of: KnowledgeObject.id | null   — versionshistorik som en kedja, inte mutation
├── status: pending | processing | ready | failed
└── created_at, updated_at

  ├── SourceObject (object_type = "source")
  │     Import av PDF/DOCX/text/webbsida/bild/ljud/YouTube-länk/video (krav 1).
  │     `origin_type`, `origin_url_or_path`, `mime_type`, `duration_seconds` (media),
  │     `page_count` (dokument).
  │
  ├── ExtractionObject (object_type = "extraction")
  │     Resultatet av att köra en SourceObject genom en extraktionsprovider (krav 3):
  │     text, OCR-text, tal-till-text, talarsegment (diarization), tidsstämplar,
  │     scenbytesbilder (video), bildtext-till-text (presentationer).
  │     `source_object_id`, `extraction_type`, `content`, `timestamp_range`,
  │     `speaker_label`, `confidence` (extraktionens egen, inte Trust Engine-nivån — se §5).
  │     Detta är det som faktiskt chunkas/embeddas in i Qdrant, precis som `Document` gör idag.
  │
  ├── GeneratedProduct (object_type = "generated_product")
  │     Output från Studio (krav 7): sammanfattning, rapport, tidslinje, tankekarta, quiz,
  │     flashcards, jämförelse. `source_refs: CitationRef[]` (se nedan), `generation_provider`,
  │     `prompt_template_id` för reproducerbarhet.
  │
  └── PodcastEpisode (object_type = "podcast_episode")
        LifeCast-output (krav 8–10). `script_object_id` (manuset som eget GeneratedProduct,
        källgrundat), `audio_object_id` (SourceObject-liknande men genererat, inte importerat),
        `voice_config`, `language`, `length_target`, `tone`, `knowledge_level`,
        `shownotes_object_id`.

CitationRef  — inte ett eget KnowledgeObject, en fristående, lätt struktur som binder ihop
  ett påstående i ett GeneratedProduct med en exakt plats i en ExtractionObject:
  { extraction_object_id, page? , timestamp_start?, timestamp_end?, char_offset_range? }
  Det här är vad som gör "exakta hänvisningar till sida, dokument och videotidsstämpel"
  (krav 5) möjligt — samma idé som RAG-lagrets nuvarande källhänvisningar i chattsvar, men
  generaliserad till alla produkttyper, inte bara chatt.
```

**Migrationsväg, inte big-bang:** `documents`-tabellen blir en `SourceObject` med
`origin_type = "upload"` — befintliga rader migreras (Alembic, samma mönster som tidigare
migrationer i `backend/alembic/versions/`), ingen data tappas. RAG-lagrets chunkning/embedding
pekar om till att läsa från `ExtractionObject` istället för `Document.content_preview`, men
chunknings-/embeddinglogiken i sig ändras inte i grunden.

## 4. Ingestion, extraktion och multimodalt index (krav 1–4)

### 4.1 Import
Samma ingestion-router-mönster som idag (`/api/documents/upload`), utökat med fler
källtyper: PDF, DOCX, text, webbsidor (redan planerat i `docs/ROADMAP.md` Fas 2:s
webbplatscrawler — den här fasen generaliserar den till godtyckliga externa webbsidor, inte
bara den egna), bilder, ljud, YouTube-länkar och uppladdade videor.

Varje import skapar en `SourceObject` med `checksum` beräknad direkt vid import —
**deduplicering sker på checksumnivå före extraktion**, inte efteråt, så samma fil laddad
två gånger (eller av två användare) inte kostar dubbel transkribering/OCR (krav 4: "utan
dubbelregistrering").

### 4.2 Extraktion — pluggbara providers, samma mönster som `LLMProvider`
```python
class TranscriptionProvider(Protocol):
    async def transcribe(self, audio: SourceRef, **kwargs) -> list[ExtractionSegment]: ...

class VisionProvider(Protocol):
    async def detect_scenes(self, video: SourceRef, **kwargs) -> list[SceneFrame]: ...
    async def ocr(self, image: SourceRef, **kwargs) -> str: ...

class SpeechProvider(Protocol):  # TTS, för LifeCast — se §6
    async def synthesize(self, script: str, voice: VoiceConfig, **kwargs) -> AudioRef: ...
```
Krav 14 ("provider-oberoende för transkribering, multimodal analys, embeddings, språkmodell
och text-till-tal") är alltså inte ett separat krav att lösa — det är samma redan bevisade
mönster som `app/providers/base.py` (chat/embedding) tillämpat på tre nya providerroller.
Aktiv provider per roll väljs i adminpanelen precis som idag, med fallback-ordning
(`chat_fallback_order`-mönstret generaliseras).

### 4.3 Index
`ExtractionObject`-innehåll chunkas och embeddas in i Qdrant precis som `Document` gör idag
— samma collection-mekanism (`app/rag/qdrant_store.py`), men med extraktionsmetadata
(tidsstämplar, talare, sida) bevarad som payload på varje vektor, inte bara text. Det är vad
som gör exakta citat (§3, `CitationRef`) möjliga vid retrieval.

## 5. Trust Engine — utökning för källa vs. tolkning vs. osäkerhet vs. konflikt (krav 6)

Dagens `assess_confidence()` (`app/rag/trust.py`) ger **en** nivå per chattsvar, härledd från
topp-likhetspoängen bland hämtade chunkar. Den logiken är fortfarande rätt grundprincip
(mätbart signal, inte modellens egen självrapportering) men måste utökas till att klassificera
**varje enskilt påstående**, inte bara hela svaret, i fyra kategorier:

1. **Källa** — ett direkt citat/faktapåstående med en `CitationRef` som pekar på en
   ExtractionObject med hög likhetspoäng.
2. **Modellens tolkning** — en slutsats modellen drar genom att kombinera flera källor; märks
   explicit som tolkning, inte fakta, även om den är rimlig.
3. **Osäker uppgift** — låg likhetspoäng eller ingen matchande källa alls (samma tröskel-idé
   som dagens `LOW_THRESHOLD`/`MEDIUM_THRESHOLD`, men utvärderad per påstående).
4. **Källkonflikt** — två `ExtractionObject` med jämförbar likhetspoäng som säger emot
   varandra (kräver ett explicit konfliktdetekteringssteg — jfr `docs/ROADMAP.md` Fas 2:s
   redan planerade "Deduplicering och konfliktdetektering i kunskapsbiblioteket").

Det här är rimligen den mest arkitekturellt känsliga delen av hela fasen — fel här undergräver
hela poängen med Trust Engine. Den bör specificeras i ett eget, mer detaljerat designdokument
innan den byggs, inte bara den här sammanfattningen.

## 6. Studio — genererade kunskapsprodukter (krav 7)

`GeneratedProduct` (§3) för sammanfattningar, rapporter, tidslinjer, tankekartor, quiz,
flashcards och jämförelser. Varje produkt bär `source_refs: CitationRef[]` — genereras aldrig
utan spårbar koppling till de `ExtractionObject` den bygger på, i linje med Trust Engine-kravet
i §5 (annars kan en "rapport" inte skilja fakta från gissning bättre än ett vanligt chattsvar).

## 7. LifeCast — podcastproduktion (krav 8–10)

1. **Manus** (`GeneratedProduct`, källgrundat som allt annat i Studio) — genereras med
   valbart språk, längd, fokus, kunskapsnivå och ton, enligt samma `source_refs`-krav.
2. **Röstsyntes** (`SpeechProvider`, §4.2) — en eller flera röster, provider-oberoende TTS.
3. **Shownotes** (`GeneratedProduct` kopplad till `PodcastEpisode`) genereras automatiskt från
   manuset + dess `source_refs` — inga fristående, ospårade shownotes.
4. **Interaktiv podcast** (krav 10, uttryckligen "stöd **senare**") — inte del av
   grundleveransen. Kräver realtids-STT + avbrottshantering i uppspelningsklienten och en
   "AI-värd"-konversationsloop ovanpå befintlig chat-infrastruktur; skjuts till en egen,
   senare delfas när grundflödet (manus → syntetiskt tal → shownotes) är stabilt.

## 8. Asynkron körning: kö, status, återupptagning, kostnadstak, avbryt (krav 11)

`docs/ROADMAP.md` Fas 1 har redan en öppen punkt: "Verklig bakgrundskö för indexeringsjobb
(t.ex. RQ/Celery) istället för synkron indexering." **Den här fasen antar att den punkten är
löst innan Library byggs**, inte en anledning att bygga en andra, parallell köimplementation.
Tunga jobb här (transkribering av en timmes ljud, scenanalys av video, TTS för ett helt
avsnitt) är för dyra och långsamma för synkron request/response-hantering på samma sätt som
dokumentindexering redan är idag i mindre skala.

Krav på jobbmodellen (utöver vad ren kö-infrastruktur ger): per-jobb kostnadstak (avbryt
automatiskt om ett jobb skulle överskrida ett användarsatt tak — utökning av `UsageLog`s
redan existerande per-anrops-kostnadsspårning till ackumulerad per-jobb-summering),
återupptagning från senaste lyckade steg (inte om från början — relevant för t.ex. ett
20-kapitels transkriberingsjobb som avbryts på kapitel 14), och explicit
avbryt-med-delvis-resultat (ett avbrutet jobb ska inte förlora redan extraherat innehåll).

## 9. Samtycke, radering, retention, rättigheter (krav 12–13)

### 9.1 Samtycke och delning
Material får aldrig användas för modellträning eller delas utan uttryckligt samtycke.
`consent_flags` på varje `KnowledgeObject` (§3) — inte ett globalt inställningsflagga, per
objekt, eftersom ett bibliotek rimligen blandar material med olika samtyckesstatus.
Träningsanvändning är i praktiken redan uteslutet strukturellt idag (systemet skickar bara
data till providerns inferens-API, aldrig till en fine-tuning-endpoint) — den här punkten
handlar om att göra det uttryckliga, verifierbara och auditerbara, inte bara implicit sant.

### 9.2 Upphovsrätt
Systemet analyserar användarens eget, uppladdat eller behörigt material — det är inte en
generell videohämtare. För YouTube-länkar (krav 1) specifikt: bearbeta via
transkript/metadata-API utan att ladda ner och lagra en kopia av själva videofilen, om inte
användaren uttryckligen laddar upp en egen fil de har rätt till (`rights_basis`-fältet på
`SourceObject`, §3, gör den distinktionen explicit och kontrollerbar, inte en implicit
policytext). Extraherad text/transkript från en extern video kan lagras (det är analys av
materialet, inte en olovlig kopia av verket) — själva videofilen inte, om den inte är
användarens egen.

### 9.3 Radering och retention
Samma mönster som `docs/OPERATIONS.md`s städjobb för auth-tokens och
`docs/SECURITY_BLOCKERS.md`s princip om permanent, icke-mjuk radering vid kontoradering:
radering av en `KnowledgeObject` måste kaskadera genom hela kedjan den täcker (dess
`ExtractionObject`, alla `GeneratedProduct`/`PodcastEpisode` som citerar den via
`CitationRef`, och motsvarande Qdrant-vektorer) — en "säker radering" som lämnar kvar
embeddings i Qdrant eller citat i en gammal rapport är inte säker radering.

## 10. Fasindelning (föreslagen, under `docs/ROADMAP.md` Fas 2 "Kunskapsdjup")

Inte påbörjad — ordningen nedan är tänkt att minimera risk: proveniens och Trust Engine-
utökningen (som allt annat beror på) före generering, generering före LifeCast (som beror på
generering), interaktivitet sist (uttryckligen "senare" i kravet).

- **L0 — Förutsättning:** bakgrundskö i produktion (`docs/ROADMAP.md` Fas 1), Render-drift +
  kontoflöde stabilt (denna plans byggspärr, se toppen av dokumentet).
- **L1 — Universal Object Model:** `SourceObject`/`ExtractionObject`-schemat, migrering av
  `documents` → `SourceObject`, checksum-baserad deduplicering.
- **L2 — Multimodal extraktion:** OCR, tal-till-text, diarization, scenbytesdetektion,
  presentationstextextraktion — en `TranscriptionProvider`/`VisionProvider` var, med minst en
  konkret implementation var.
- **L3 — Multimodalt index + citat:** Qdrant-payload med tidsstämplar/sida/talare,
  `CitationRef`, chatt mot en eller flera samlingar med exakta hänvisningar (krav 5).
- **L4 — Trust Engine-utökning:** käll-/tolknings-/osäkerhets-/konfliktklassificering (§5) —
  eget, mer detaljerat designdokument innan bygge.
- **L5 — Studio:** `GeneratedProduct`-generering (sammanfattning → rapport → tidslinje →
  tankekarta → quiz/flashcards → jämförelse, ungefär i den ordningen efter komplexitet).
- **L6 — LifeCast grund:** manus → TTS → shownotes.
- **L7 — Samtycke/rättigheter/retention som produktionshårdning:** `consent_flags`,
  `rights_basis`-kontroller, kaskaderande radering — **inte sist för att det är mindre
  viktigt, utan för att det inte går att bygga rätt förrän L1–L3 finns** att applicera
  kontrollerna på. Ingen delleverans av L1–L6 går skarpt i produktion utan L7.
- **L8 — Interaktiv LifeCast** (krav 10): uttryckligen senare, egen delfas.

## 11. Kostnadsmedvetenhet

Transkribering, vision-analys och TTS är dyrare per-anrop än chatt/embedding hos de flesta
leverantörer. `UsageLog`s per-roll-kostnadsspårning (redan i produktion för chat/embedding)
utökas med samma mönster för de nya rollerna innan L2/L6 går live — inte i efterhand — så att
adminpanelens kostnadsöversikt förblir fullständig från dag ett för denna fas, i linje med
`README.md`s princip att kostnad loggas per anrop, inte uppskattas i efterhand.
