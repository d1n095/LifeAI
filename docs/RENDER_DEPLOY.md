# Render-driftsättning

Detta dokument beskriver `render.yaml` (repo-roten) — ett [Render
Blueprint](https://render.com/docs/blueprint-spec) för hela den nuvarande webb-stacken:
PostgreSQL, Redis, FastAPI-backend och Next.js-frontend. Målet är att få den befintliga
webbversionen fullt fungerande live och lösa det tidigare rapporterade `Failed to fetch`-felet
vid registrering, vars mest sannolika grundorsak var att `NEXT_PUBLIC_API_URL` aldrig byggdes in
korrekt och/eller att ingen backend var deployad alls (se felsökningen i tidigare commits).

**Vercel förblir den skarpa/live frontend-adressen tills Render är verifierat fungerande.**
Ingenting i det här dokumentet eller i `render.yaml` ändrar Vercel-projektet. `mainai-frontend`
på Render finns för att verifiera hela kedjan end-to-end innan en eventuell växling.

**Qdrant ingår inte** i den här leveransen (endast Postgres + Redis begärdes). Det påverkar inte
registrering/inloggning (`get_qdrant_client()` i `backend/app/rag/qdrant_store.py` kopplar upp
lat, per anrop — inte vid uppstart), men `/api/knowledge` och `/api/documents/upload` kommer att
fela tills en Qdrant-instans läggs till separat.

## Arkitektur: hur de fyra tjänsterna hänger ihop

```
┌─────────────────┐        ┌──────────────────┐        ┌──────────────────┐
│ mainai-frontend  │──HTTPS→│  mainai-backend   │──TCP──→│  mainai-postgres  │
│ (Next.js, Docker)│        │ (FastAPI, Docker) │  (internt nätverk)│
└─────────────────┘        └─────────┬─────────┘        └──────────────────┘
                                      │
                                      ↓ TCP (internt nätverk)
                               ┌──────────────┐
                               │ mainai-redis  │
                               └──────────────┘
```

### Databasrollerna — varför det inte är en enda `DATABASE_URL`

Precis som i `docker-compose.yml` kör backend alla runtime-frågor genom en **egen, icke-superuser
databasroll** (`mainai_app`) — inte den administratörsroll som Render skapar åt dig
(`mainai_admin` i det här Blueprintet). Det är detta som gör Row-Level Security verkningsfull:
en superuser/ägarroll kringgår RLS per definition (se README.md).

Lokalt (Docker Compose) skapas `mainai_app` av `backend/db-init/01-app-role.sh`, monterad som ett
Postgres-init-skript. **Render (och praktiskt taget alla hanterade Postgres-tjänster) stödjer inte
att montera init-skript** i sin Postgres-container — du får bara den ena admin-anslutningen.
`render.yaml` löser det genom att:

1. Generera `MAINAI_APP_PASSWORD` som en plattforms-hemlighet (`generateValue: true` —
   Render slumpar värdet, det hamnar aldrig i repot eller i någon logg).
2. Vid varje containerstart (`backend/docker-entrypoint.sh`, innan `alembic upgrade head`) köra
   `backend/scripts/ensure_app_role.py`, som ansluter med `DATABASE_URL` (admin-rollen) och
   idempotent skapar/uppdaterar `mainai_app`-rollen med samma GRANT:ar som
   `01-app-role.sh` ger lokalt.
3. Skriptet skriver den fullständiga `APP_DATABASE_URL` (härledd från `DATABASE_URL`:s
   host/port/databasnamn + `mainai_app` + det genererade lösenordet) till en tillfällig fil som
   entrypoint-skriptet `source`:ar innan `uvicorn` startar — så `app_database_url` i
   `backend/app/config.py` får rätt värde utan att någon behöver skriva in det manuellt.

Det här steget körs bara om `MAINAI_APP_PASSWORD` är satt. Lokal Docker Compose sätter den
variabeln på `postgres`-tjänsten men inte på `backend`-containern, så det befintliga lokala
flödet (init-skriptet) är oförändrat.

## Miljövariabler — fullständig lista

### Automatiska (Render kopplar ihop tjänsterna åt dig — inget du fyller i)

| Variabel | Tjänst | Källa |
|---|---|---|
| `DATABASE_URL` | backend | `fromDatabase: mainai-postgres` (admin-anslutningen) |
| `REDIS_URL` | backend | `fromService: mainai-redis` |

### Genererade hemligheter (Render slumpar värdet — hamnar aldrig i repot)

| Variabel | Tjänst | Vad den styr |
|---|---|---|
| `SECRET_KEY` | backend | Signerar JWT-access-/refresh-tokens |
| `MAINAI_APP_PASSWORD` | backend | Lösenord för den begränsade RLS-databasrollen (se ovan) |

### Måste fyllas i manuellt i Render-dashboarden (`sync: false` i render.yaml — Render frågar
efter dessa när Blueprintet appliceras, eller de kan sättas efteråt under tjänstens
**Environment**-flik)

| Variabel | Tjänst | Vad du sätter |
|---|---|---|
| `ADMIN_EMAIL` | backend | E-post för det automatiskt skapade admin-kontot (skapas bara om inga användare finns) |
| `ADMIN_PASSWORD` | backend | Lösenord för samma konto — välj ett starkt värde, inte `.env.example`s placeholder |
| `SMTP_HOST` | backend | **Obligatorisk i produktion** — backend vägrar nu starta (`ENVIRONMENT=production` utan `SMTP_HOST`, se `app/main.py`s `_check_smtp_configured`) om den saknas. Utan SMTP når verifierings-/återställningsmail aldrig fram, och det ska inte ske tyst. |
| `SMTP_USERNAME` | backend | Enligt din SMTP-leverantör |
| `SMTP_PASSWORD` | backend | Enligt din SMTP-leverantör |
| `SMTP_FROM_EMAIL` | backend | Avsändaradress för konto-mail |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` / `DEEPSEEK_API_KEY` / `OPENROUTER_API_KEY` | backend | Fyll i minst en — lämna resten tomma. Ingen ändras här; bara de leverantörer med ifylld nyckel går att aktivera i `/admin`. |

Ingen av ovanstående finns i `render.yaml` som klartext — filen innehåller bara *nycklarna*, inte
värdena, precis som `sync: false` är avsett för.

### Redan satta, synliga värden i `render.yaml` (inte hemliga, men dokumenterade här för överblick)

| Variabel | Värde | Kommentar |
|---|---|---|
| `ENVIRONMENT` | `production` | Utlöser SMTP-tvånget ovan och andra produktionsbeteenden |
| `FRONTEND_ORIGINS` | `https://life-ai-seven.vercel.app,https://mainai-frontend.onrender.com` | CORS-allowlist — båda origins under övergångsperioden. **Verifiera det faktiska tilldelade `mainai-frontend`-hostnamnet i dashboarden** (Render lägger till en suffix om namnet är upptaget) och rätta annars + omdeploya. |
| `PUBLIC_APP_URL` | `https://life-ai-seven.vercel.app` | Länkar i verifierings-/återställningsmail — pekar på den fortfarande skarpa Vercel-adressen, inte CORS-listan |
| `COOKIE_SECURE` | `true` | Oförändrat säkert default |
| `COOKIE_SAMESITE` | `none` | Krävs eftersom frontend/backend är olika origins |
| `NEXT_PUBLIC_API_URL` (frontend) | `https://mainai-backend.onrender.com` | **Samma sak att verifiera** — om Render tilldelat backend ett annat hostnamn, rätta detta värde och trigga en ny frontend-build (bakas in vid `next build`, se `frontend/Dockerfile`) |

## Deploy sker bara via CI, aldrig av ett push i sig

`render.yaml` sätter `autoDeploy: false` på både `mainai-backend` och `mainai-frontend` — ett
push till `claude/det-kommer-mer-879lcm` deployar alltså **ingenting** på Render av sig självt.
Den enda utlösaren är jobbet `deploy-render` i `.github/workflows/ci.yml`, som körs efter — och
bara om — `all-checks-passed` blir grönt, och bara för push (inte pull request) till exakt den
branchen. Det jobbet anropar Render-tjänsternas **Deploy Hook**-URL:er via
`RENDER_BACKEND_DEPLOY_HOOK_URL` / `RENDER_FRONTEND_DEPLOY_HOOK_URL` — två GitHub Actions-secrets
som **inte finns än**. Så länge de saknas är jobbet ett ofarligt no-op (loggar att det hoppar
över, avslutar med kod 0) — dvs det här Blueprintet i sig kan inte trigga en deploy.

Att lägga till dessa secrets (Repo → Settings → Secrets and variables → Actions → New repository
secret) är alltså det steg som faktiskt kopplar på automatisk deploy, och det görs efter att
tjänsterna finns i Render (Deploy Hook-URL:en visas under respektive tjänsts **Settings** →
**Deploy Hook** i Render-dashboarden, efter att Blueprintet applicerats).

## Klick-steg i Render (första steget — invänta bekräftelse innan nästa)

**Steg 1 — endast detta, invänta din bekräftelse innan något mer görs:**

1. Logga in i [Render Dashboard](https://dashboard.render.com).
2. **New** → **Blueprint**.
3. Koppla (eller välj, om redan kopplat) GitHub-repot `d1n095/LifeAI`.
4. Välj branch `claude/det-kommer-mer-879lcm` — Render läser `render.yaml` från roten och listar
   de fyra tjänsterna (`mainai-postgres`, `mainai-redis`, `mainai-backend`, `mainai-frontend`)
   för granskning. **Klicka inte Apply än** — det är ett separat, senare steg.

Jag väntar på din bekräftelse innan vi går vidare till att fylla i de manuella variablerna och
faktiskt applicera Blueprintet, i linje med att inga externa tjänster ska ändras utan uttrycklig
bekräftelse.

## Verifiering efter en fullständig deploy (för senare, när vi är där)

1. `curl -i https://mainai-backend.onrender.com/api/health` → förväntat `200 {"status":"ok"}`.
2. `curl -i -X OPTIONS https://mainai-backend.onrender.com/api/auth/register -H "Origin: https://mainai-frontend.onrender.com" -H "Access-Control-Request-Method: POST"` → `Access-Control-Allow-Origin` ska matcha exakt.
3. Öppna `https://mainai-frontend.onrender.com/register`, försök skapa ett konto, kontrollera i DevTools → Network att `POST /api/auth/register` ger `200`/`201`.
4. Kontrollera att ett verifieringsmail faktiskt kommer fram (bekräftar att `SMTP_HOST` m.fl. är korrekt ifyllda).
