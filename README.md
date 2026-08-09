# Life OS / MainAI

Lokal AI-plattform som samlar företagets kunskap, dokument, projekt och källkod på ett ställe,
med ett leverantörsoberoende AI-lager (OpenAI, Anthropic, Google Gemini, DeepSeek, OpenRouter
eller en lokal modell via Ollama).

**`docs/MAINAI_ARCHITECTURE.md` är det nuvarande, auktoritativa arkitekturdokumentet** — börja
där för systemarkitekturen som den ser ut idag. `docs/ARCHITECTURE.md` beskriver det
ursprungliga Fas 0-skelettet och är enligt `docs/MAINAI_ARCHITECTURE.md`s egen text delvis
föråldrat (t.ex. är Qdrant sedan dess ersatt av pgvector); `docs/ROADMAP.md` och `docs/STATUS.md`
är på samma sätt historiska ögonblicksbilder från Fas 0 — för aktuell status över pågående
brancher/PR:er, se istället `docs/BRANCH_REGISTRY.md`, och för den fullständiga minnes-/
projektförståelseplanen, se `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`. Se även
`docs/MAINAI_0.1_PLAN.md` för den pågående MainAI 0.1-leveransen,
**`docs/SECURITY_BLOCKERS.md` för kända säkerhetsuppgifter som måste lösas före
produktionsdrift** (bl.a. en nödvändig Next.js-versionsuppgradering), `docs/RENDER_DEPLOY.md`
för Render-Blueprintet (`render.yaml`) som driftsätter hela stacken (Postgres, Redis, backend,
frontend), och `docs/LIFE_LIBRARY_PLAN.md` för en planerad (ej påbörjad) framtida fas —
multimodalt kunskapsbibliotek, genererade kunskapsprodukter och podcastproduktion.

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

## Inloggning och konton

MainAI är Founder AI, inte en delad eller per-användare-assistent — se
`docs/FOUNDER_KNOWLEDGE_BOOTSTRAP.md`. Vid första uppstart skapas automatiskt det enda
grundarkontot från `FOUNDER_EMAIL`/`FOUNDER_PASSWORD` i `.env` (förverifierat — inget
e-postflöde behövs för det, se `backend/app/bootstrap.py`). Publik självregistrering är
avstängd (`POST /api/auth/register` ger 404 när `ENVIRONMENT=production`, och det finns
ingen `/register`-sida) — se `backend/app/deps.py`s `require_founder()`, som varje skyddad
rutt (chat, konversationer, dokument, kunskap, projekt, admin) kräver server-side, inte bara
en UI-spärr.

Sessionen levereras helt via `HttpOnly`/`Secure`-cookies (`POST /api/auth/login`) — det finns
inget klientläsbart token att skicka som `Authorization`-header, och inget lagras i
`localStorage`/`sessionStorage` (se `docs/AUTH_THREAT_MODEL.md` och
`docs/SECURITY_BLOCKERS.md`). Alla API-routrar utom `/api/health` och `/api/auth/*` kräver
inloggning; MainAI-ytan (`/api/chat`, `/api/conversations`, `/api/documents`,
`/api/knowledge/*`, `/api/projects`, `/api/admin/*`) kräver dessutom grundarkontot specifikt
(`require_founder()` i `backend/app/deps.py`) — se "Inloggning och konton" ovan.

Frontend har egna sidor för inloggning (`/login`), e-postverifiering (`/verify-email`), glömt
lösenord (`/forgot-password`), lösenordsåterställning (`/reset-password`) och kontohantering
(`/account` — export, utloggning från alla enheter, permanent radering). `/register` finns
kvar som en tom väg som omdirigerar till `/login` — ingen registreringssida renderas. Alla
sidor utom login-/verifierings-/återställningsflödet är skyddade av `AuthGuard` — obehörig
eller utgången session skickar tillbaka till `/login` automatiskt (med en transparent
förnyelse av access-token vid behov, inte en direkt utloggning).

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
  alembic/           # schemamigrationer — enda källan till schemat, se docs/OPERATIONS.md
  app/
    providers/       # OpenAI, Anthropic, Gemini, DeepSeek, OpenRouter, Ollama — gemensamt interface
    rag/             # chunkning, embedding, Qdrant-lagring, retrieval
    routers/         # auth, account, chat, documents, projects, knowledge, admin, health
    models/          # SQLAlchemy-modeller (Postgres)
    cleanup.py        # schemalagt städjobb för utgångna auth-tokens
    main.py
  tests/             # pytest — backend/, security/, account/ (se docs/OPERATIONS.md)
frontend/
  app/
    api/[...path]/route.ts   # same-origin proxy till backend (INTERNAL_API_URL) — se docs/RENDER_DEPLOY.md
    login/, register/, verify-email/, forgot-password/, reset-password/   # publika sidor
    (shell)/         # alla skyddade sidor: Dashboard, Chat, Kunskapsdatabas, Projekt, Dokument, Admin, Konto
  components/        # Sidebar, AuthGuard, Orb, ConfidenceBadge
  lib/
    api.ts           # enda kontaktpunkten mot backend-API:et (cookies + CSRF, hanterar 401 och nätverksfel)
    auth.ts          # CSRF-värdet i minnet — inga tokens lagras här, se docs/AUTH_THREAT_MODEL.md
    useVoice.ts       # Web Speech API-hook (röst in/ut)
  e2e/               # Playwright end-to-end-tester (cross-origin-läge + same-origin-proxy-läge)
docs/
  ARCHITECTURE.md
  ROADMAP.md
  SECURITY_BLOCKERS.md
  AUTH_THREAT_MODEL.md
  OPERATIONS.md
  RENDER_DEPLOY.md   # render.yaml-referens: miljövariabler, rollprovisionering, klick-steg
.github/workflows/ci.yml   # obligatoriska kontroller + CI-gated Render-deploy — se docs/OPERATIONS.md och docs/RENDER_DEPLOY.md
docker-compose.yml
render.yaml           # Render Blueprint (Postgres, Redis, backend, frontend) — se docs/RENDER_DEPLOY.md
```

## Testning

- **Backend** (`backend/tests/`, pytest): `pip install -r requirements-dev.txt && pytest tests/`
  — kräver Postgres och Redis nåbara via `DATABASE_URL`/`APP_DATABASE_URL`/`REDIS_URL`
  (testerna skapar och migrerar en egen engångsdatabas, se `tests/conftest.py`).
- **E2E** (`frontend/e2e/`, Playwright): `npx playwright test` — kräver en riktig körande
  backend (`python backend/scripts/ci/run_e2e_backend.py`, som fejkar utgående AI- och
  e-postanrop men kör allt annat på riktigt) samt att `npx playwright install --with-deps
  chromium` körts en gång.
- **CI** (`.github/workflows/ci.yml`) kör allt detta automatiskt på varje push/PR — se
  `docs/OPERATIONS.md` för hur du kopplar det till obligatoriska branch-skydd.

## Drift och säkerhet — status

- **Autentisering**: session i `HttpOnly`/`Secure`-cookies (kortlivad JWT-access-token +
  roterande refresh-token), CSRF-skydd på alla muterande anrop, lösenord hashas med Argon2id,
  tokens signeras med `SECRET_KEY` (byt från default i produktion). Full hotmodell i
  `docs/AUTH_THREAT_MODEL.md`. Nya konton kräver e-postverifiering innan inloggning.
- **Row-Level Security**: konversationer är strikt isolerade per användare på databasnivå
  (Postgres RLS, inte bara applikationslogik). Backend kör därför alla runtime-frågor via en
  egen, icke-superuser databasroll (`mainai_app`, skapas automatiskt av
  `backend/db-init/01-app-role.sh`) — den superuser-roll som skapar tabellerna
  (`POSTGRES_USER`/`DATABASE_URL`) kringgår RLS per definition och används endast för
  schemamigrering. Se `docs/MAINAI_0.1_PLAN.md` för vilka tabeller som är RLS-skyddade och varför.
- **Rate limiting**: `/api/chat` är hastighetsbegränsad per användare (`RATE_LIMIT_CHAT_PER_MINUTE`),
  övriga endpoints har en generösare gräns, plus striktare gränser och ett separat
  per-e-postadress brute-force-skydd på auth-endpoints (se `docs/AUTH_THREAT_MODEL.md`).
  Redis-backad (`REDIS_URL`) för att fungera korrekt över flera backend-repliker — se
  `docs/OPERATIONS.md`. Osatt `REDIS_URL` faller tillbaka till processlokal in-memory-räkning,
  bara korrekt med exakt en instans.
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
- Schemat hanteras av Alembic-migrationer (`backend/alembic/versions/`), aldrig av
  `Base.metadata.create_all` i produktion — se `docs/OPERATIONS.md` för install/uppgradering/
  rollback.
- Backup: kör regelbunden `pg_dump` av PostgreSQL och ta Qdrant-snapshots (`/collections/{name}/snapshots`).

## Nästa steg

Se `docs/MAINAI_0.1_PLAN.md` för den aktuella planen (provider-fallback, kostnadsloggning,
Trust Engine, konversationshistorik, röst, animerad orb) och `docs/ROADMAP.md` för den
generella, längre planen efter MainAI 0.1.
