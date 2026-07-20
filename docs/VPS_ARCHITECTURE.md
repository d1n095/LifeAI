# Strato VPS — arkitektur och tillitsgränser

**Syfte:** spårar den faktiska runtime-vägen för en riktig begäran, inte bara en filkarta —
från Internet till databassvar, med varje tillitsgräns, publikt/privat gränssnitt och
hemlighetsgräns explicit markerad. Skriven som en del av VPS-förberedelsen på
`claude/strato-vps-prep`; se `docs/vps/START_HERE.md` för hur detta dokument hänger ihop med
resten av VPS-underlaget.

## Begärandevägen, steg för steg

```
Internet
   │  TLS (Let's Encrypt, automatiskt via Caddy)
   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Caddy (caddy:2-alpine)                                                 │
│  - ENDA processen som publicerar värdportar (80/443)                   │
│  - terminerar TLS, sätter X-Forwarded-For/-Proto/-Host mot frontend    │
│  - reverse_proxy frontend:3000 (Docker-DNS på lifeai_internal)         │
└────────────────────────────────────────────────────────────────────────┘
   │  privat Docker-nätverk (lifeai_internal), okrypterat HTTP internt
   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ frontend (Next.js standalone, node:20-slim)                            │
│  - ingen egen ports:-publicering — bara nåbar från Caddy               │
│  - statiska sidor renderas direkt                                      │
│  - /api/* fångas av app/api/[...path]/route.ts, ett server-till-server │
│    proxy-anrop (fetch) mot INTERNAL_API_URL — webbläsaren ser ALDRIG   │
│    backendens adress, och Set-Cookie-headers vidarebefordras byte-för- │
│    byte så sessionskakan förblir samma-ursprung ur webbläsarens synvinkel│
└────────────────────────────────────────────────────────────────────────┘
   │  privat Docker-nätverk (lifeai_internal), server-till-server fetch()
   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ backend (FastAPI/uvicorn, python:3.12-slim)                            │
│  - ingen egen ports:-publicering — bara nåbar från frontend            │
│  - uvicorn --proxy-headers --forwarded-allow-ips=* : litar på          │
│    X-Forwarded-For från VARJE anslutande part — säkert ENDAST för att  │
│    den enda parten som någonsin ansluter är frontend-proxyn ovan       │
│  - CORS-allowlist (FRONTEND_ORIGINS), CSRF via app/deps.py, RLS via    │
│    Postgres SET LOCAL app.current_user_id                              │
└────────────────────────────────────────────────────────────────────────┘
   │  publikt internet (utanför VPS:en) — TLS till Supabase/Upstash
   ▼
Supabase (Postgres, Session Pooler, port 5432)   Upstash (Redis)
```

## Tillitsgränser, explicit

| Gräns | Vad som korsar den | Skydd |
|---|---|---|
| Internet → Caddy | Rå HTTP/HTTPS, okänd klient | TLS-terminering, brandvägg (bara 80/443/22 öppna), inga andra containrar publicerade |
| Caddy → frontend | Vidarebefordrad HTTP, `X-Forwarded-*`-headers | Privat Docker-nätverk, ingen extern åtkomst möjlig eftersom frontend saknar `ports:` |
| Frontend → backend | Server-till-server `fetch()`, `INTERNAL_API_URL=http://backend:8000` | Samma privata nätverk; webbläsaren når ALDRIG detta hopp direkt |
| Backend → Supabase | TLS, Session Pooler-uppkoppling, två separata roller (superuser för migrationer, `mainai_app` för request-serving) | RLS med `FORCE ROW LEVEL SECURITY`, `mainai_app` kan aldrig kringgå RLS eftersom den inte är ägare/superuser |
| Backend → Upstash | TLS, `REDIS_URL` | Delad rate-limit-räknare, ingen känslig data lagras i Redis (bara räknare/nycklar) |
| Hemlighetsfil → containrar | `/etc/lifeai/lifeai.env` (root-ägd, `chmod 600`) läses via `env_file:` | Aldrig i Git, aldrig läsbar för den vanliga deploy-användaren, bara för root/Docker-daemonen |

## Publika kontra privata gränssnitt

- **Publikt (nått från internet):** enbart Caddy på 80/443. Verifierat i CI (`vps-compose-verify`s "Verify Caddy is the only container publishing anything to the host").
- **Privat (bara Docker-nätverket `lifeai_internal`):** frontend:3000, backend:8000. Ingen av dem har en `ports:`-rad i `docker-compose.vps.yml` — inte "brandväggad", utan konstruktivt onåbar utifrån eftersom ingen NAT/DNAT-regel någonsin skapas för dem.
- **Extern, utanför VPS:en helt:** Supabase (Session Pooler, port 5432) och Upstash (Redis). VPS:en har alltså inget eget stateful data för applikationens kärndata — se `docs/VPS_BACKUP_RESTORE.md` för vad som faktiskt behöver säkerhetskopieras på själva VPS:en (i praktiken bara hemlighetsfilen och Caddys TLS-certifikat).

## Autentisering, cookies och CSRF (`backend/app/deps.py`, `backend/app/cookies.py`)

- Åtkomsttoken och refresh-token är båda **HttpOnly-kakor**, aldrig läsbara för JavaScript,
  aldrig returnerade i en JSON-kropp. `access_token` är path `/` (skickas på varje anrop),
  `refresh_token` är path `/api/auth` (bara skickad till login/refresh/logout — en smalare
  exponeringsyta för den känsligaste, längst-levande behörigheten).
- **Ingen separat CSRF-kaka.** Eftersom frontend/backend historiskt kunde vara olika
  ursprung (Render-arkitekturen), kan en kaka backend sätter aldrig läsas av frontend-
  JavaScript oavsett HttpOnly-flagga — det är en grundläggande same-origin-policy-regel, inte
  något vi valt bort. CSRF-värdet levereras istället en gång i login/refresh-svarskroppen och
  hålls i minnet klient-sida; verifieras server-side i `get_current_user()` mot en databaslagrad
  rad, med `secrets.compare_digest` (tidskonstant jämförelse). **Detta mönster fungerar
  identiskt på VPS:en** eftersom Next.js-proxyn fortfarande gör backend-anropet
  server-till-server — webbläsaren pratar bara med Caddy/frontend, aldrig direkt med backend.
- RLS-bindning (`SET LOCAL app.current_user_id`) sker i SAMMA dependency som autentiserar
  användaren — ingen separat middleware att hålla i synk.

## Betrodda proxy-headers — den mest VPS-specifika detaljen

`backend/Dockerfile`s uvicorn-kommando kör med `--proxy-headers --forwarded-allow-ips=*`,
vilket gör att uvicorn litar på `X-Forwarded-For` från **vem som helst som ansluter direkt**
och använder det värdet som `request.client.host` (det `app/limiter.py`s hastighetsbegränsning,
`app/audit.py` och inloggnings-IP-loggning i `app/routers/auth.py` alla nycklar på).

**Detta är bara säkert eftersom backend-containern konstruktivt aldrig kan nås av någon annan
än frontend-proxyn** — se "Publika kontra privata gränssnitt" ovan. Om backend någonsin fick
en `ports:`-rad (ett misstag denna arkitektur är specifikt utformad för att aldrig tillåta),
skulle VILKEN SOM HELST extern anropare kunna förfalska sin egen `X-Forwarded-For` och
kringgå IP-baserad hastighetsbegränsning helt. Detta är exakt samma antagande
`Dockerfile.combined`s loopback-isolering byggde på (se `docs/RENDER_DEPLOY.md`) — bara
uttryckt som nätverkssegmentering istället för processisolering i en container.

**Caddy sätter `X-Forwarded-For`/`X-Forwarded-Proto`/`X-Forwarded-Host` automatiskt** för
varje `reverse_proxy`-direktiv (Caddys standardbeteende, ingen extra konfiguration behövs) —
och skriver ALLTID över en inkommande klients egen `X-Forwarded-For`-header med den verkliga
anslutande IP-adressen snarare än att blint vidarebefordra vad klienten skickade, vilket är
vad som gör hela kedjan pålitlig: Caddy är den enda parten som får avgöra vad den "verkliga"
klient-IP:n är, och allt nedströms (Next.js-proxyn, sedan uvicorn) litar transitivt på Caddy,
inte på den ursprungliga klienten direkt.

## CORS (`backend/app/main.py`)

`FRONTEND_ORIGINS`-allowlisten är nästan overksam på VPS:en av samma skäl som på Render:
webbläsaren pratar bara med Caddy/frontend (samma ursprung som sidan laddades från).
Proxy-anropet till backend sker server-till-server och skickar ingen `Origin`-header (ett
webbläsarkoncept) — CORS-mellanvaran triggas alltså inte av den vägen. Kvar av
försvar-i-djupet-skäl, satt till den riktiga VPS-domänen (`https://<domän>`), aldrig `*`
(oförenligt med `allow_credentials=True` som kakbaserad auth kräver).

## Hastighetsbegränsning, uppladdningsgränser, kroppsstorlek

- **Hastighetsbegränsning** (`app/limiter.py`): nycklas på autentiserad användare där möjligt,
  annars IP (se ovanstående avsnitt om betrodda proxy-headers). Kräver `REDIS_URL` satt i
  produktion för att vara delad över flera repliker — VPS:en kör i praktiken alltid EN
  backend-replik (`docker-compose.vps.yml` skalar inte horisontellt), men Upstash används
  ändå för konsekvens med Render-konfigurationen och för att inte tyst tappa skyddet om
  antalet repliker någonsin ändras.
- **Uppladdningsgräns**: `backend/app/routers/documents.py` avvisar allt över 25 MB — men
  EFTER att hela kroppen redan lästs in i minnet (`len(raw) > MAX_UPLOAD_BYTES`). Caddy själv
  hade INGEN egen kroppsstorleksgräns innan detta VPS-arbete (se
  `docs/VPS_DOCKER_HARDENING.md`) — en klient kunde tvinga Caddy och Next.js-proxyn att
  strömma en godtyckligt stor kropp hela vägen till backend innan den avvisades, ett
  resurstömningsproblem oberoende av att slutresultatet (avvisning) var korrekt. Åtgärdat med
  en explicit `request_body { max_size }`-direktiv i `Caddyfile` — se hårdningsdokumentet.
- **Next.js-proxyn** (`frontend/app/api/[...path]/route.ts`) strömmar kroppen
  (`request.body`) rakt igenom utan att buffra den i minnet — korrekt för både JSON och
  multipart-uppladdningar, och innebär att Next.js-lagret i sig inte introducerar en egen
  storleksgräns eller minnesrisk.

## Uppstart, avstängning och beroendeordning

- **Backend** (`backend/docker-entrypoint.sh`): kör `ensure_app_role.py` (om
  `MAINAI_APP_PASSWORD` är satt) → `alembic upgrade head` → `exec uvicorn`. Alla DB-beröringar
  vid uppstart (RLS-tillämpning, grundarbootstrap) går genom `call_with_db_retry()`
  (exponentiell backoff, 5 försök) — en verifierad produktionsincident visade att Supabase
  Session Poolerns auth-cache kan ha en kort propageringsfördröjning direkt efter att en roll
  skapats/roterats.
- **Frontend väntar på en frisk backend** i `docker-compose.vps.yml` via
  `depends_on: backend: condition: service_healthy` — motsvarande garanti som
  `scripts/entrypoint-combined.sh` gav inom EN container, nu uttryckt som ett riktigt
  Compose-beroende mellan containrar.
- **Caddy väntar på en frisk frontend** på samma sätt — Caddy börjar aldrig route:a trafik
  till en frontend som inte svarat frisk än.
- **Avstängning**: FastAPIs `@app.on_event("shutdown")` stoppar bara schemaläggaren
  (`stop_scheduler()`) — inga öppna anslutningar hanteras explicit utöver vad
  SQLAlchemy/uvicorns egna graceful-shutdown redan ger. Next.js standalone-servern hanterar
  `SIGTERM` självt (Node-processens standardbeteende). Docker Composes `stop_grace_period`
  (standard 10s) ger båda processerna tid att avsluta pågående förfrågningar innan `SIGKILL`.

## Bakgrundsjobb

`backend/app/scheduler.py` kör ett `APScheduler`-bakgrundsjobb (token-städning,
`app/cleanup.py`) IN-PROCESS i varje backend-replik. Säkert även med flera repliker eftersom
`app/cleanup.py` redan använder ett **Postgres advisory lock** så bara en replik faktiskt gör
jobbet per intervall — samma mönster rekommenderas för migrationskörning i
`docs/VPS_MIGRATION_SAFETY.md`, eftersom precedensen redan finns i kodbasen.

## Anslutningspoolning (`backend/app/db.py`)

Två separata SQLAlchemy-motorer: `migration_engine` (superuser, bara migrationer/RLS-setup)
och `engine` (begränsad `mainai_app`-roll, all request-serving). Båda med `pool_pre_ping=True`
— en död pool-anslutning (t.ex. efter en Supavisor-omstart) upptäcks och byts ut istället för
att ge ett kryptiskt fel mitt i en förfrågan. `expire_on_commit=False` är en medveten
inställning (inte standardvärdet) för att undvika att en tyst ny transaktion öppnas mellan
`db.commit()` och nästa attributåtkomst, vilket annars riskerar att tappa RLS-kontexten
mitt i en förfrågan — se filens egen kommentar för den fullständiga förklaringen.

## Render-specifika antaganden som INTE får läcka in i VPS-topologin

| Antagande på Render | Varför det INTE gäller VPS:en |
|---|---|
| En enda Web Service kör både FastAPI och Next.js som syskonprocesser (`Dockerfile.combined`) | VPS:en kör dem som separata containrar bakom en riktig reverse proxy — se `docs/RENDER_DEPLOY.md`s "ROTORSAK BEKRÄFTAD AV RENDER SUPPORT" för varför detta bytdes bort |
| `INTERNAL_API_URL=http://127.0.0.1:8000` (loopback, samma nätverksnamnrymd) | VPS:en använder `http://backend:8000` (Docker-DNS över `lifeai_internal`) — se `render.yaml` kontra `docker-compose.vps.yml` |
| Renders egen hälsokontroll kan nå tjänsten via en annan väg än publik trafik (roten till 502-incidenten) | Caddy är den enda process som någonsin route:ar publik trafik — ingen dold andra väg finns att förväxlas med |
| Render tillhandahåller TLS automatiskt utan egen konfiguration | Caddy måste själv hantera Let's Encrypt (automatiskt, men kräver att DNS pekar rätt FÖRE första start — se `docs/STRATO_VPS_DEPLOY.md`) |
| `MAINAI_APP_ROTATE_PASSWORD` och `ensure_app_role.py`s Supavisor-specifika beteende (pooler-inloggningsidentitet med `.`-suffix) | Gäller lika mycket på VPS:en — samma Supabase-databas, samma Session Pooler, ingen ändring behövs |
| Render Free saknar en "Private Service"-typ, vilket ursprungligen tvingade fram `Dockerfile.combined`s design | Irrelevant på en VPS — riktig nätverksisolering (`lifeai_internal`) ger samma säkerhetsegenskap utan den begränsningen |

## Filsystem, uppladdningar och persistens

Applikationen lagrar **ingen** användardata i containerns filsystem — dokumentuppladdningar
processas i minnet och lagras som `document_chunks`/embeddings i Postgres (pgvector), inte
som filer på disk (se `backend/app/rag/vector_store.py`). Det enda VPS-lokala stateful datat
är Caddys eget `caddy_data`-volym (TLS-certifikat) och den root-ägda hemlighetsfilen — se
`docs/VPS_BACKUP_RESTORE.md`.

## Vad detta dokument INTE täcker

Se de separata dokumenten för djupare behandling av respektive ämne:
- Containerhärdning (icke-root, minimala images, etc.): `docs/VPS_DOCKER_HARDENING.md`
- Hotmodell: `docs/VPS_THREAT_MODEL.md`
- Hemlighetsinventering: `docs/VPS_SECRETS_INVENTORY.md`
- Migrationssäkerhet: `docs/VPS_MIGRATION_SAFETY.md`
- Drift/incidenthantering: `docs/VPS_OPERATIONS_RUNBOOK.md`
- Backup/återställning: `docs/VPS_BACKUP_RESTORE.md`
- Exakt installationsordning: `docs/STRATO_VPS_DEPLOY.md`
