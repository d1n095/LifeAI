# Life OS / MainAI

Lokal AI-plattform som samlar företagets kunskap, dokument, projekt och källkod på ett ställe,
med ett leverantörsoberoende AI-lager (OpenAI, Anthropic, Google Gemini, DeepSeek, OpenRouter
eller en lokal modell via Ollama).

Se `docs/ARCHITECTURE.md` för systemarkitektur, `docs/ROADMAP.md` för den generella fasindelade
planen, `docs/MAINAI_0.1_PLAN.md`/`docs/STATUS.md` för den pågående MainAI 0.1-leveransen, och
**`docs/SECURITY_BLOCKERS.md` för kända säkerhetsuppgifter som måste lösas före
produktionsdrift** (bl.a. en nödvändig Next.js-versionsuppgradering).

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

## Inloggning

Vid första uppstart skapas automatiskt ett admin-konto från `ADMIN_EMAIL`/`ADMIN_PASSWORD` i
`.env`. Logga in mot `POST /api/auth/login` (JSON: `{"email": "...", "password": "..."}`) för
att få en JWT — skicka den som `Authorization: Bearer <token>` på alla övriga anrop. Alla
API-routrar utom `/api/health`, `/api/auth/login` kräver inloggning; `/api/admin/*` kräver
dessutom adminroll.

Frontend har en egen inloggningssida (`/login`) som gör exakt detta och sparar token i
`localStorage` (medvetet val, se `frontend/lib/auth.ts` och `docs/SECURITY_BLOCKERS.md`).
Alla sidor utom `/login` är skyddade av `AuthGuard` — obehörig eller utgången token skickar
tillbaka till `/login` automatiskt.

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

### Fallback och tillförlitlighet

`/api/chat` faller automatiskt tillbaka genom `CHAT_FALLBACK_ORDER` (standard:
`openai,anthropic,gemini`) om den aktiva leverantören felar — svaret anger i
`providers_attempted` exakt vilka leverantörer som försöktes. Varje svar innehåller också en
`confidence`-nivå (`high`/`medium`/`low`/`none`) beräknad från hur väl frågan matchar det som
faktiskt finns i kunskapsbiblioteket (Trust Engine, `app/rag/trust.py`) — vid svagt eller
obefintligt underlag instrueras modellen uttryckligen att inte presentera gissningar som fakta.
Kostnad och tokenanvändning loggas per anrop (`usage_log`) och kan summeras via
`GET /api/admin/usage/summary`.

### Chattgränssnittet — röst, orb, historik

- **Animerad orb** (`components/Orb.tsx`) visar fyra tillstånd: vilar, lyssnar, tänker, talar
  och fel — rent CSS-driven, ingen extern beroende.
- **Röstinmatning/röstutmatning** (`lib/useVoice.ts`) via webbläsarens inbyggda Web Speech
  API — ingen serverkostnad, inget API-nyckelbehov. Mikrofonknappen visas bara om
  webbläsaren stödjer taligenkänning (svagt/inget stöd i t.ex. Firefox); "Läs upp
  svar"-kryssrutan visas bara om talsyntes stöds.
- **Konversationshistorik**: sidopanel i `/chat` mot `/api/conversations` — lista, öppna,
  radera. Ny konversation-knapp startar om utan att tappa historiken.
- **Confidence/källvisning**: varje AI-svar visar en färgkodad tillförlitlighetsbadge
  (`components/ConfidenceBadge.tsx`) och de faktiska källorna svaret bygger på.

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
  app/
    login/          # publik inloggningssida (utanför AuthGuard)
    (shell)/         # alla skyddade sidor: Dashboard, Chat, Kunskapsdatabas, Projekt, Dokument, Admin
  components/        # Sidebar, AuthGuard, Orb, ConfidenceBadge
  lib/
    api.ts           # enda kontaktpunkten mot backend-API:et (bifogar JWT, hanterar 401)
    auth.ts          # token-lagring
    useVoice.ts       # Web Speech API-hook (röst in/ut)
docs/
  ARCHITECTURE.md
  ROADMAP.md
  SECURITY_BLOCKERS.md
docker-compose.yml
```

## Drift och säkerhet — status

- **Autentisering**: JWT-baserad inloggning, lösenord hashas med bcrypt, tokens signeras med
  `SECRET_KEY` (byt från default i produktion).
- **Row-Level Security**: konversationer är strikt isolerade per användare på databasnivå
  (Postgres RLS, inte bara applikationslogik). Backend kör därför alla runtime-frågor via en
  egen, icke-superuser databasroll (`mainai_app`, skapas automatiskt av
  `backend/db-init/01-app-role.sh`) — den superuser-roll som skapar tabellerna
  (`POSTGRES_USER`/`DATABASE_URL`) kringgår RLS per definition och används endast för
  schemamigrering. Se `docs/MAINAI_0.1_PLAN.md` för vilka tabeller som är RLS-skyddade och varför.
- **Rate limiting**: `/api/chat` är hastighetsbegränsad per användare (`RATE_LIMIT_CHAT_PER_MINUTE`),
  övriga endpoints har en generösare gräns. In-memory — byt till Redis-backend innan flera
  backend-repliker körs samtidigt.
- **Audit log**: inloggningar, dokumentborttagning och providerbyten loggas i `audit_log`
  (aldrig API-nycklar eller lösenord).
- **Next.js/React**: uppgraderat till `next@16.2.10` + `react@19.2.7` (från `14.2.15`/`18.3.1`)
  — `npm audit` visar 0 sårbarheter. Se `docs/NEXTJS_UPGRADE_PLAN.md` för
  kompatibilitetsanalysen och `docs/SECURITY_BLOCKERS.md` för övriga kvarstående
  säkerhetsuppgifter (ingen av dem blockerar denna leverans).
- **Responsivt och tillgängligt**: sidomenyn kollapsar till en hamburgermeny på mobil,
  formulärfält har kopplade `<label>`, statusändringar annonseras via `aria-live`/`role`,
  och tabeller har `scope`/`caption` för skärmläsare.
- Databaser är inte exponerade utanför Docker-nätverket i produktion (endast `backend` behöver nå dem).
- Tabeller skapas i nuläget via `Base.metadata.create_all` vid uppstart — byt till Alembic-migrationer
  innan produktionsdrift (se `docs/ROADMAP.md`, Fas 1).
- Backup: kör regelbunden `pg_dump` av PostgreSQL och ta Qdrant-snapshots (`/collections/{name}/snapshots`).

## Nästa steg

Se `docs/MAINAI_0.1_PLAN.md` för den aktuella planen (provider-fallback, kostnadsloggning,
Trust Engine, konversationshistorik, röst, animerad orb) och `docs/ROADMAP.md` för den
generella, längre planen efter MainAI 0.1.
