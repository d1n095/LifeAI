# Life OS / MainAI

Lokal AI-plattform som samlar företagets kunskap, dokument, projekt och källkod på ett ställe,
med ett leverantörsoberoende AI-lager (OpenAI, Anthropic, Google Gemini, DeepSeek, OpenRouter
eller en lokal modell via Ollama).

Se `docs/ARCHITECTURE.md` för systemarkitektur och `docs/ROADMAP.md` för fasindelad plan.

## Snabbstart (Docker — rekommenderat)

Krav: Docker + Docker Compose.

```bash
cp .env.example .env
# Öppna .env och fyll i minst en API-nyckel (t.ex. OPENAI_API_KEY)

docker compose up --build
```

Detta startar:
- **PostgreSQL** — `localhost:5432`
- **Qdrant** (vektordatabas) — `localhost:6333`
- **Backend (FastAPI)** — `localhost:8000` (Swagger-dokumentation: `localhost:8000/docs`)
- **Frontend (Next.js)** — `localhost:3000`

Vill du även köra en lokal modell (Llama/Qwen/Mistral) utan molnkostnad:

```bash
docker compose --profile local-llm up --build
docker exec -it lifeai-ollama-1 ollama pull llama3.1
```

Sätt sedan i adminpanelen (`localhost:3000/admin`) leverantören `ollama` som aktiv för chat
och/eller embedding.

## Konfigurera AI-leverantörer

Fyll i de nycklar du faktiskt har i `.env`:

| Variabel | Leverantör |
|---|---|
| `OPENAI_API_KEY` | OpenAI |
| `ANTHROPIC_API_KEY` | Anthropic (Claude) |
| `GOOGLE_API_KEY` | Google Gemini |
| `DEEPSEEK_API_KEY` | DeepSeek |
| `OPENROUTER_API_KEY` | OpenRouter |

Endast leverantörer med ifylld nyckel går att aktivera i adminpanelen (`/admin`). Byte av aktiv
leverantör för chat respektive embedding sker där, utan omstart eller kodändring — resten av
plattformen pratar alltid mot samma interna gränssnitt (`app/providers/base.py`).

**Obs:** Anthropic, DeepSeek och OpenRouter saknar publikt embedding-API i denna MVP. Använd
OpenAI, Gemini eller en lokal Ollama-modell för embedding-rollen.

## Utveckling utan Docker

### Backend
```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Kräver en körande PostgreSQL och Qdrant, t.ex.:
# docker compose up postgres qdrant

cp ../.env.example .env   # justera DATABASE_URL/QDRANT_URL till localhost om du kör lokalt
uvicorn app.main:app --reload
```

### Frontend
```bash
cd frontend
npm install
cp .env.example .env.local
npm run dev
```

## Projektstruktur

```
backend/
  app/
    providers/     # OpenAI, Anthropic, Gemini, DeepSeek, OpenRouter, Ollama — gemensamt interface
    rag/            # chunkning, embedding, Qdrant-lagring, retrieval
    routers/        # chat, documents, projects, knowledge, admin, health
    models/         # SQLAlchemy-modeller (Postgres)
    main.py
frontend/
  app/              # Next.js App Router: Dashboard, Chat, Kunskapsdatabas, Projekt, Dokument, Admin
  lib/api.ts        # enda kontaktpunkten mot backend-API:et
docs/
  ARCHITECTURE.md
  ROADMAP.md
docker-compose.yml
```

## Drift och säkerhet — status i MVP

- Databaser är inte exponerade utanför Docker-nätverket i produktion (endast `backend` behöver nå dem).
- Tabeller skapas i nuläget via `Base.metadata.create_all` vid uppstart — byt till Alembic-migrationer
  innan produktionsdrift (se `docs/ROADMAP.md`, Fas 1).
- Adminpanelen saknar ännu inloggning i denna MVP — lägg bakom autentisering innan den exponeras
  utanför ett internt nätverk (Fas 1 i roadmapen).
- Backup: kör regelbunden `pg_dump` av PostgreSQL och ta Qdrant-snapshots (`/collections/{name}/snapshots`).

## Nästa steg

Se `docs/ROADMAP.md` för hela planen. Kortsiktigt (Fas 1): autentisering, Alembic-migrationer,
bakgrundskö för indexering, och tester för provider- och RAG-lagret.
