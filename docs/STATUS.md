# Statusgranskning — vad som faktiskt fungerar idag

Granskning av commit `f7d75bf` (Fas 0-leveransen) innan MainAI 0.1-arbetet påbörjas.
Detta är en ärlig inventering — inget här är antaget, det är läst direkt ur koden.

## Vad som finns och är kopplat end-to-end

- **Backend startar** (`backend/app/main.py`): FastAPI-app, CORS mot frontend, skapar
  Postgres-tabeller vid uppstart via `Base.metadata.create_all` (ingen Alembic ännu).
- **Providerlager** (`app/providers/`): gemensamt interface `LLMProvider` (`base.py`) med
  fungerande implementationer för OpenAI, Anthropic, Gemini, DeepSeek, OpenRouter och lokal
  Ollama. Alla pratar riktiga HTTP-API:er via `httpx` (inga mock:ar).
- **Provider-registry** (`registry.py`): `resolve_active(db, role)` väljer aktiv leverantör
  per roll ("chat"/"embedding") — DB-konfiguration om satt, annars env-default. **Ingen
  fallback-kedja finns ännu** — om vald leverantör felar kastas felet rakt upp.
- **RAG-pipeline**: dokument laddas upp → text extraheras (PDF/DOCX/HTML/text) → chunkas
  (enkel ord-baserad sliding window) → embeddas via aktiv embedding-provider → skrivs till
  Qdrant. Status uppdateras på dokumentraden (`pending`/`indexing`/`indexed`/`failed`).
- **Chat-endpoint** (`/api/chat`): hämtar top-5 relevanta chunkar från Qdrant, bygger
  systemprompt med kontext, skickar historik (senaste 20 meddelanden) + fråga till aktiv
  chat-provider, sparar användar- och assistant-meddelande i Postgres, returnerar svar +
  källor med similarity-score. **Ingen confidence-bedömning eller "trust"-spärr finns ännu**
  — modellen kan i princip svara säkert även vid svagt underlag.
- **Projekt/uppgifter**: full CRUD via `/api/projects` och `/api/projects/tasks`.
- **Kunskapssökning**: `/api/knowledge/search` gör semantisk sökning direkt mot Qdrant.
  `/api/knowledge/company` är en enkel key/value-lagring för företagsinfo.
- **Adminpanel (backend)**: `/api/admin/providers/status` visar vilka leverantörer som har
  giltig nyckel och vilken som är aktiv. `/api/admin/providers/config` byter aktiv
  leverantör/modell utan omstart.
- **Frontend** (Next.js App Router, Tailwind, mörkt tema): Dashboard, Chat, Kunskapsdatabas,
  Dokument (upload/lista/radera), Projekt/uppgifter, Admin (providerstatus + byte). All
  kommunikation går via `lib/api.ts` mot backend-URL:en i `NEXT_PUBLIC_API_URL`.
- **Docker Compose**: Postgres, Qdrant, backend, frontend, valfri Ollama-profil
  (`--profile local-llm`). Verifierat att alla Python-filer kompilerar (`py_compile`).

## Vad som INTE finns ännu (relevant för MainAI 0.1-kravet)

- **Ingen autentisering.** Alla endpoints är öppna — vem som helst med nätverksåtkomst till
  backend kan chatta, ladda upp/radera dokument och byta AI-leverantör.
- **Ingen RLS/data-isolering.** Det finns ingen `users`-tabell och inget ägarskap
  (`user_id`) på konversationer, projekt, uppgifter eller dokument.
- **Ingen rate limiting.** Inget skydd mot att en klient spammar `/api/chat` (och därmed
  drar obegränsad API-kostnad hos leverantören).
- **Ingen audit log.** Inga händelser (inloggning, borttagning, providerbyte) loggas för
  spårbarhet.
- **Ingen kostnads-/användningsloggning.** `raw_usage` fångas per anrop från providern men
  sparas eller summeras aldrig.
- **Ingen fallback mellan leverantörer.** Ett fel hos aktiv leverantör stoppar hela svaret.
- **Ingen konversationslista/historik-API.** `Conversation`/`Message`-modellerna finns och
  används internt av `/api/chat`, men det finns ingen endpoint för att lista tidigare
  konversationer eller öppna en gammal tråd.
- **Ingen röst.** Varken röstinmatning eller röstutmatning finns i frontend.
- **Ingen animerad AI-boll.** Chat-sidan är ren text idag.
- **Ingen "Trust Engine".** Inget confidence-mått beräknas eller visas; modellen kan
  formulera sig säkert även när underlaget är svagt eller obefintligt.

## Slutsats

Grunden (Fas 0) är en fungerande, kompilerbar skelettplattform med rätt arkitektur —
providerlagret, RAG-pipelinen och grundmodellerna för konversationer går att bygga vidare på
rakt av. Allt som listas under "INTE ännu" ovan är exakt det MainAI 0.1-uppdraget beställer,
och byggs nu ovanpå befintlig kod utan att den befintliga koden skrivs om.
