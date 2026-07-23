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
   │                                             │
   │  publikt internet (utanför VPS:en)          │  lifeai_data — privat nätverk,
   │  — TLS till Supabase                        │  internal: true (ingen route ut
   ▼                                             ▼  alls, inte bara ingen host-port)
Supabase (Postgres, Session Pooler,     redis (valkey/valkey:8.1.9-alpine, digest-
  port 5432)                              pinnad, requirepass, tmpfs /data — ingen
                                           disk-persistens, se "Redis vs Valkey" nedan)
```

## Redis vs Valkey — vald lösning och motivering

**Vald lösning: Valkey**, inte Redis, för den privata cache-tjänsten på VPS:en. Exakt
avbild, digest-pinnad, aldrig en flytande tagg:

```
valkey/valkey:8.1.9-alpine@sha256:a038175878d66b9d274fbf8be73c0305e93798b83917647f167e18cef3c71eec
```

(`valkey-server --version` inuti containern: `Valkey server v=8.1.9`; `INFO server`
rapporterar `server_name:valkey`, `valkey_version:8.1.9` — bekräftat direkt mot den
verkliga, körande containern, inte antaget.) Docker Compose-tjänsten heter fortfarande
`redis` (samma DNS-namn, samma `REDIS_URL`/`REDIS_PASSWORD`-variabelnamn som redan används i
koden och mallen) — bara avbilden bakom den bytt.

Bedömning mot de fyra kraven:

1. **Kompatibilitet med nuvarande Python-klient:** total. Valkey talar exakt samma
   RESP2/RESP3-protokoll som Redis — `redis-py` (den klient `backend/app/limiter.py` och
   `backend/app/main.py::_check_redis_reachable()` redan använder via `redis.from_url()`)
   gör ingen skillnad mellan servrarna. Inget i applikationskoden behövde ändras. De
   funktionella testerna (autentisering, `PING`/`PONG`, healthcheck) är körda mot den
   RIKTIGA, digest-pinnade containern ovan — inte antagna.
2. **Stabil officiell container-image:** `valkey/valkey` på Docker Hub är Valkey-projektets
   egen officiella image, aktivt underhållen. Verifierat direkt (pullad och körd i den här
   granskningen): binärerna heter `valkey-server`/`valkey-cli`, men avbilden installerar
   `redis-server`/`redis-cli` som symlänkar till dem för bakåtkompatibilitet — så
   `docker-compose.vps.yml`s `command:`/`healthcheck:`-rader (som anropar `redis-server`/
   `redis-cli`) fungerar oförändrade.
3. **Låg resursförbrukning:** samma Alpine-baserade, C-implementerade, i praktiken
   entrådiga arkitektur som Redis — jämförbar minnes-/CPU-fotavtryck. Samma
   `mem_limit`/`cpus`/`pids_limit` som redan var satta för Redis gäller oförändrat (verifierat
   live: håller sig inom 128 MB).
4. **Öppen och långsiktigt hållbar — licens:** detta är den avgörande skillnaden. Redis Inc
   bytte Redis' licens med Redis 7.4 (mars 2024) från det permissiva BSD-3-Clause till en
   källkodstillgänglig (men inte OSI-godkänd öppen källkod) dubbellicens (RSALv2/SSPLv1). En
   flytande tagg som `redis:7-alpine` följer i praktiken den senaste patch-versionen inom
   major-version 7 — vilket över tid riskerar att tyst börja peka på en build under den nya
   licensen utan att någon rad i den här repot ändrats. Valkey 8.1.9 är licensierad under
   **BSD-3-Clause** (bekräftat mot Valkey-projektets egen källkodsrepo — den körande
   containeravbilden själv bakar inte in en LICENSE-fil, sökt igenom men inte hittad, så
   licensuppgiften kommer från projektet, inte avbildens filsystem). Valkey är
   Linux Foundation-styrd (grundad av bland andra AWS, Google Cloud, Oracle, Ericsson och
   flera av Redis' ursprungliga underhållare efter licensbytet) och har inget sådant
   licensrisk-scenario.

**Inget verkligt tekniskt problem hittades** som skulle motivera att tillfälligt behålla
Redis — kompatibiliteten är total och verifierad. Valkey väljs alltså enligt
självständighetsmålet.

**Avstådt:** att skriva om `backend/app/config.py`/`backend/app/limiter.py` — inget behövde
ändras där, exakt det uppdraget bad om att undvika.

## Var lösenordet (REDIS_PASSWORD) faktiskt syns — verifierat, inte antaget

**Ärlig utgångspunkt:** root på VPS:en (eller vem som helst med `docker exec`/`docker
inspect`-åtkomst till `redis`-containern, vilket i praktiken kräver root eller
Docker-socket-åtkomst) KAN läsa värdet. Det påståendet görs inte om att vara omöjligt att
läsa för serverns root-operatör — samma tillitsgräns som redan gäller för
`/etc/lifeai/lifeai.env` självt. Målet är att det inte exponeras för icke-root-användare,
nätverket, eller vanliga loggar/CI.

Varje kanal nedan är verifierad direkt mot den riktiga, digest-pinnade containern (inte
antagen):

| Kanal | Visar lösenordet? | Verifierat hur |
|---|---|---|
| `docker inspect .Config.Cmd` / `.Entrypoint` | **Nej** — literalt `"$REDIS_PASSWORD"` | `docker inspect` mot en riktig körande container |
| `docker inspect .Config.Healthcheck.Test` | **Nej** — literalt `"$REDIS_PASSWORD"` | Samma |
| `docker inspect .Config.Env` | **Ja** | Förväntat och accepterat — samma tillitsgräns som `/etc/lifeai/lifeai.env` |
| `docker compose config` (upplöst YAML) | **Nej** för `command`/`healthcheck` (literalt `$REDIS_PASSWORD`) | Kört direkt mot compose-filen |
| Den körande `redis-server`-processens `/proc/<pid>/cmdline` (INTE PID 1 när `init: true` är satt — det är `docker-init`; den riktiga processen är dess barn) | **Nej** — `redis-server`/`valkey-server` skriver om sin egen processtitel efter uppstart (välkänt Redis/Valkey-beteende), läses tillbaka som bara `redis-server *:6379` | `ps aux` och `/proc/<pid>/cmdline` inuti en riktig container, jämfört före/efter identifiering av rätt PID |
| Samma process `/proc/<pid>/environ` | **Nej** — nollställd av processen själv (byte-storlek finns kvar, innehållet är utsuddat) | `grep`/`wc -c` mot `/proc/<pid>/environ` för den bekräftade `redis-server`-processen |
| `docker top`/`ps aux` (från host ELLER inifrån containern) | **Nej** — samma processtitel-omskrivning | `docker top` mot en riktig container |
| `docker-init`s (PID 1, när `init: true`) egen `/proc/1/cmdline` | **Nej** — visar den URSPRUNGLIGA, ICKE-EXPANDERADE kommandosträngen (`"$REDIS_PASSWORD"` som text, aldrig substituerat) | Samma |
| `docker-init`s (PID 1) egen `/proc/1/environ` | **Ja** — `docker-init` gör ingen processtitel-trick, ärver bara containerns miljö normalt | Samma tillitsgräns som `.Config.Env` ovan — kräver samma root/exec-åtkomst |
| Healthcheck-loggen (`docker inspect .State.Health.Log`) | **Nej** — `Output` är tom (kommandot pipas genom `grep -q`, och `redis-cli`/`valkey-cli` skriver aldrig ut lösenordet självt) | Verifierat mot en riktig healthcheck-körning, både lyckad och innan autentisering fungerade |
| `redis-cli`/`valkey-cli`s EGEN process-argv under healthcheck | **Åtgärdat i denna omgång** — bytt från `-a "$REDIS_PASSWORD"` (satte lösenordet i cli-processens egen, kortlivade argv — bredare exponering, läsbar av vem som helst med processlistningsåtkomst i containern) till `REDISCLI_AUTH` (miljövariabel `redis-cli` självt läser) — samma smalare tillitsgräns som `.Config.Env` (kräver samma UID eller root), inte den bredare argv-listningsytan | Verifierad princip (POSIX exec-semantik: en `VAR=val cmd`-prefixad miljövariabel hamnar aldrig i `cmd`s egen argv) — INTE fångad live pga healthcheck-processens livslängd på under 100 ms, för snabb för tillförlitlig `ps`-fångst i denna granskning |
| `scripts/vps/deploy.sh` / `scripts/vps/lib.sh`s utskrifter | **Nej** — `validate_redis_password()`s samtliga felmeddelanden nämner bara variabelnamnet `REDIS_PASSWORD`, aldrig `$password`s innehåll (granskad rad för rad) | Källkodsgranskning av samtliga `REDIS_PASSWORD`-träffar i båda filerna |
| CI-loggar (`.github/workflows/ci.yml`) | **Nej** — CI:s testlösenord är ett hårdkodat CI-bara testvärde (`ci-vps-compose-verify-redis-password-...`, aldrig en riktig hemlighet), och samtliga nya CI-steg skriver bara ut härledda statusord (`Confirmed: ...`), aldrig råa värden | Källkodsgranskning av samtliga nya CI-steg i denna och föregående granskningsrunda |

**Kvarstående, accepterad exponeringsyta (oförändrad av detta arbete, gäller ALLA
hemligheter i `/etc/lifeai/lifeai.env` lika mycket):** `docker inspect .Config.Env` och
`docker exec`/`printenv` inuti containern. Detta kräver samma root-/Docker-socket-åtkomst
som redan krävs för att läsa `/etc/lifeai/lifeai.env` direkt — ingen NY tillitsgräns, bara
samma gamla, nu explicit dokumenterad istället för underförstådd.

## Tillitsgränser, explicit

| Gräns | Vad som korsar den | Skydd |
|---|---|---|
| Internet → Caddy | Rå HTTP/HTTPS, okänd klient | TLS-terminering, brandvägg (bara 80/443/22 öppna), inga andra containrar publicerade |
| Caddy → frontend | Vidarebefordrad HTTP, `X-Forwarded-*`-headers | Privat Docker-nätverk, ingen extern åtkomst möjlig eftersom frontend saknar `ports:` |
| Frontend → backend | Server-till-server `fetch()`, `INTERNAL_API_URL=http://backend:8000` | Samma privata nätverk; webbläsaren når ALDRIG detta hopp direkt |
| Backend → Supabase | TLS, Session Pooler-uppkoppling, två separata roller (superuser för migrationer, `mainai_app` för request-serving) | RLS med `FORCE ROW LEVEL SECURITY`, `mainai_app` kan aldrig kringgå RLS eftersom den inte är ägare/superuser |
| Backend → redis (lokal container) | Okrypterat inom Docker, `REDIS_URL` med lösenord (`requirepass`) | Privat `lifeai_data`-nätverk (`internal: true`, ingen route ut alls) — inte "brandväggat", konstruktivt onåbart för allt utom backend; ingen känslig data lagras i Redis (bara räknare/nycklar), och ingen disk-persistens (tmpfs) |
| frontend/caddy → redis | — (finns inte) | frontend/caddy är inte anslutna till `lifeai_data` överhuvudtaget — inte ens ett nätverkslager att försöka autentisera mot |
| Hemlighetsfil → containrar | `/etc/lifeai/lifeai.env` (root-ägd, `chmod 600`) läses via `env_file:` | Aldrig i Git, aldrig läsbar för den vanliga deploy-användaren, bara för root/Docker-daemonen |

## Publika kontra privata gränssnitt

- **Publikt (nått från internet):** enbart Caddy på 80/443. Verifierat i CI (`vps-compose-verify`s "Verify Caddy is the only container publishing anything to the host").
- **Privat (bara Docker-nätverket `lifeai_internal`):** frontend:3000, backend:8000. Ingen av dem har en `ports:`-rad i `docker-compose.vps.yml` — inte "brandväggad", utan konstruktivt onåbar utifrån eftersom ingen NAT/DNAT-regel någonsin skapas för dem.
- **Privat, ett steg smalare (bara Docker-nätverket `lifeai_data`, `internal: true`):** redis:6379. Bara backend är anslutet till detta nätverk — frontend och caddy är inte anslutna till det överhuvudtaget, så de kan inte ens *försöka* nå det (ingen DNS-post, ingen route), inte bara nekas med lösenord. Verifierat i CI (`vps-compose-verify`s nätverksisoleringssteg).
- **Extern, utanför VPS:en helt:** Supabase (Session Pooler, port 5432) — extraherad text/embeddings/metadata. Redis kör privat på VPS:en (se `redis`-tjänsten i `docker-compose.vps.yml`), utan disk-persistens (tmpfs `/data`). **Sedan durable-worker-paketet har VPS:en dock ETT eget permanent stateful datalager:** `lifeai_uploads` (Life Library-originalfiler, innehållsadresserade) — se ovanstående "Filsystem, uppladdningar och persistens"-avsnitt och `docs/VPS_BACKUP_RESTORE.md` för vad som faktiskt behöver säkerhetskopieras (hemlighetsfilen, Caddys TLS-certifikat, OCH nu `lifeai_uploads`; Redis förblir medvetet exkluderat).

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
  backend-replik (`docker-compose.vps.yml` skalar inte horisontellt), men den lokala
  `redis`-tjänsten körs ändå för konsekvens med Render-konfigurationen och för att inte tyst
  tappa skyddet om antalet repliker någonsin ändras. Att Redis nu körs på samma VPS (istället
  för en extern tjänst som Upstash) ändrar inte den här egenskapen — `REDIS_URL` pekar bara
  på `redis:6379` istället för en extern host.
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

**Uppdaterat av durable-worker-paketet (Life Library).** Extraherad text/embeddings lagras
fortfarande som `document_chunks` i Postgres (pgvector) — det har inte ändrats. Men det
**ursprungliga uppladdade filinnehållet** (den råa filen en användare laddade upp, innan
extraktion) lagras nu innehållsadresserat (`{sha256[:2]}/{sha256}`, aldrig av
användarens filnamn — se `backend/app/storage/local_fs.py`) på en **egen, namngiven,
persistent Docker-volym**, `lifeai_uploads`, monterad på `/var/lib/lifeai/uploads` i BÅDE
`backend` (skriver vid uppladdning) och den nya `worker`-tjänsten (läser vid bearbetning).
Detta är den ENDA skrivbara volymen någon container i topologin har utöver Caddys egen
`caddy_data`/`caddy_config` — `backend`/`worker` är annars `read_only: true` med bara `/tmp`
som `tmpfs`.

**worker (ny tjänst, samma avbild som `backend`, annat `command:`)**: en fristående
poll-loop (`python -m app.worker`) som hämtar väntande `ImportJob`-rader ur Postgres via
`FOR UPDATE SKIP LOCKED` och bearbetar dem — ersätter den gamla process-bundna FastAPI
`BackgroundTask`-vägen (som inte överlevde en backend-omstart). Ingen `ports:`, ingen
Caddy-routing, aldrig nåbar direkt. Delar `lifeai_internal` (utgående internet till
Supabase/AI-leverantörer) och `lifeai_data` (Redis, för STEG 11:s befintliga
distribuerade lås, omskopat till `(owner, innehålls-checksumma)`) med `backend`, exakt
samma nätverksprofil. Se `backend/app/worker.py`s moduldocstring för klaim/lease/
retry-mekaniken i detalj.

**Varför en egen tjänst och inte bara ett nytt anrop i backend:** en `BackgroundTask` lever
och dör med den HTTP-förfrågan/processen som schemalade den — en backend-omstart (deploy,
krasch, `docker compose restart`) tappade tidigare varje pågående import helt. Nu är
Postgres själv källan till sanning för vilka jobb som väntar/körs (`ImportJob.status`), och
`worker` kan startas om, skalas, eller till och med köras på en annan container helt utan
att jobb tappas — bara att de tar lite längre tid innan de plockas upp igen (nästa
poll-cykel, eller efter att en förfallen lease gör ett övergivet jobb återtagbart, se
`app/jobs/lease.py`).

**Migrationer körs bara i `backend`**, inte i `worker` — trots att de delar avbild och
ENTRYPOINT (`backend/docker-entrypoint.sh`), sätter `worker` `RUN_MIGRATIONS=false` så två
containrar aldrig racear `alembic upgrade head` mot samma databas vid samtidig uppstart.

## Vad detta dokument INTE täcker

Se de separata dokumenten för djupare behandling av respektive ämne:
- Containerhärdning (icke-root, minimala images, etc.): `docs/VPS_DOCKER_HARDENING.md`
- Hotmodell: `docs/VPS_THREAT_MODEL.md`
- Hemlighetsinventering: `docs/VPS_SECRETS_INVENTORY.md`
- Migrationssäkerhet: `docs/VPS_MIGRATION_SAFETY.md`
- Drift/incidenthantering: `docs/VPS_OPERATIONS_RUNBOOK.md`
- Backup/återställning: `docs/VPS_BACKUP_RESTORE.md`
- Exakt installationsordning: `docs/STRATO_VPS_DEPLOY.md`
