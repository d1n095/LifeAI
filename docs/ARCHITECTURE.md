# Systemarkitektur — LifeOS / MainAI

## 1. Översikt

LifeOS är en lokalt körd AI-plattform som fungerar som ett företags centrala kunskaps- och
arbetshjärna. Plattformen samlar dokument, källkod, projekt, uppgifter och konversationer i
ett system, med ett RAG-lager (Retrieval Augmented Generation) som ger AI:n permanent minne
om företaget, och en leverantörsoberoende AI-motor som kan växla mellan flera LLM-leverantörer
utan att resten av systemet behöver ändras.

```
┌─────────────────────────────────────────────────────────────────┐
│                          Frontend (Next.js)                     │
│  Dashboard · Chat · Kunskapsdatabas · Projekt · Dokument · Admin │
└───────────────────────────────┬─────────────────────────────────┘
                                 │ REST/JSON (HTTPS)
┌───────────────────────────────▼─────────────────────────────────┐
│                       Backend (FastAPI)                         │
│  ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌──────────────────┐ │
│  │  Routers  │ │  RAG-lager│ │ Provider- │ │  Ingestion/       │ │
│  │  (chat,   │ │ (chunking,│ │ router    │ │  Crawler          │ │
│  │  docs,    │ │ embedding,│ │ (5 st AI- │ │  (webbplats,      │ │
│  │  projekt, │ │ retrieval)│ │ leverant.)│ │  dokument, kod)   │ │
│  │  admin)   │ │           │ │           │ │                    │ │
│  └─────┬─────┘ └─────┬─────┘ └─────┬─────┘ └─────────┬──────────┘ │
└────────┼─────────────┼─────────────┼─────────────────┼────────────┘
         │             │             │                 │
   ┌─────▼─────┐ ┌─────▼─────┐ ┌─────▼─────────────────▼────┐
   │ PostgreSQL│ │  Qdrant   │ │  Externa/lokala LLM-API:er │
   │ (struktur-│ │ (vektor-  │ │  OpenAI / Anthropic /      │
   │  erad data)│ │  databas) │ │  Gemini / DeepSeek /       │
   │           │ │           │ │  OpenRouter / lokal (Ollama)│
   └───────────┘ └───────────┘ └─────────────────────────────┘
```

## 2. Komponenter

### 2.1 Frontend — Next.js (App Router, TypeScript)
- **Dashboard**: systemstatus, senaste aktivitet, förslag från AI:n.
- **Chat**: konversationsgränssnitt mot backend, med källhänvisningar till dokument som RAG hämtat.
- **Kunskapsdatabas**: sök/bläddra i indexerad kunskap (dokument, kod, webbplats).
- **Projektöversikt**: projekt, uppgifter, roadmap-status.
- **Dokumenthantering**: ladda upp, tagga, ta bort dokument; visar indexeringsstatus.
- **Adminpanel**: hantera API-nycklar per leverantör, välj aktiv modell, se användning/kostnad.

### 2.2 Backend — FastAPI (Python)
- **Routers**: tunna HTTP-lager, ett per domän (chat, documents, projects, tasks, knowledge, admin, health).
- **RAG-lager**: chunkning av inkommande text, embedding-generering, lagring/sökning i Qdrant,
  sammanställning av kontext till LLM-anrop.
- **Provider-router**: gemensamt interface (`LLMProvider`) med en implementation per leverantör.
  Aktiv leverantör/modell väljs via konfiguration (DB eller env) — inga andra delar av systemet
  behöver känna till vilken leverantör som används.
- **Ingestion/Crawler**: läser in dokument (PDF/DOCX/MD/TXT/kod), samt en enkel webbcrawler som
  hämtar och indexerar den egna webbplatsens sidor.

### 2.3 Datalager
- **PostgreSQL**: strukturerad data — projekt, uppgifter, roadmap, dokumentmetadata,
  företagsinformation, kundinformation, konversationshistorik, providerkonfiguration.
- **Qdrant**: vektordatabas för embeddings av all indexerad text (dokument, kod, webbsidor,
  konversationer) — ger AI:n semantisk sökbarhet och permanent minne.

### 2.4 AI-lager — leverantörsoberoende
Alla leverantörer implementerar samma interface:

```python
class LLMProvider(Protocol):
    async def chat(self, messages: list[Message], **kwargs) -> ChatResult: ...
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
```

Stöd i MVP:
1. OpenAI
2. Anthropic
3. Google Gemini
4. DeepSeek
5. OpenRouter
6. Lokal modell via Ollama (Llama/Qwen/Mistral) — för att på sikt minska beroendet av externa API:er

Byte av leverantör sker via en rad i providerkonfigurationen (admin-panelen eller `.env`) —
resten av plattformen pratar bara mot `LLMProvider`-interfacet.

## 3. Dataflöde: dokumentindexering
1. Dokument laddas upp (UI) eller crawlas (webbplats) → sparas i objektlagring/filsystem.
2. Metadata skrivs till PostgreSQL (`documents`-tabell).
3. Bakgrundsjobb chunkar texten, genererar embeddings via aktiv provider, skriver vektorer + metadata till Qdrant.
4. Status uppdateras (`indexing` → `indexed`).

## 4. Dataflöde: chat med minne (RAG)
1. Användarens fråga → backend.
2. Frågan embeddas → semantisk sökning i Qdrant → topp-K relevanta chunkar.
3. Chunkar + konversationshistorik + fråga skickas till aktiv LLM-provider.
4. Svaret sparas i PostgreSQL (konversationshistorik) och returneras med källhänvisningar.
5. Ny information som uppstår i konversationen (beslut, fakta) kan sparas explicit till
   kunskapsbiblioteket ("kom ihåg det här") — indexeras på samma sätt som dokument.

## 5. Säkerhet & drift
- Alla API-nycklar lagras som miljövariabler/secrets, aldrig i kod eller i databasen i klartext.
- Backend och frontend körs i separata Docker-containrar, kommunicerar internt i ett Docker-nätverk.
- PostgreSQL och Qdrant exponeras inte publikt — endast backend har åtkomst.
- Autentisering (MVP): enkel API-nyckel/session för adminpanelen. Fas 2: fullständig
  användarhantering med roller.
- Backup: schemalagd `pg_dump` samt Qdrant snapshot till lokal disk/extern lagring.

## 6. Skalbarhet
- Stateless backend (FastAPI) → kan köras med flera repliker bakom en reverse proxy.
- Qdrant och PostgreSQL kan flyttas till egna noder/servrar vid behov utan kodändring
  (endast anslutningssträng).
- Provider-abstraktionen gör det möjligt att lägga till fler leverantörer eller byta till
  en helt lokal modell utan att ändra frontend eller övriga backend-moduler.
