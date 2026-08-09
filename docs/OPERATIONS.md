# Drift — installation, uppgradering, backup, rollback, incidenthantering

Skriven för att kunna följas utan tidigare kontext från chatthistoriken. Om något här
motsäger koden: koden vinner, och den här filen bör uppdateras.

## Arkitektur i korthet

- **Backend**: FastAPI (`backend/`), Postgres (två roller — se nedan), Qdrant, valfritt Redis.
- **Frontend**: Next.js (`frontend/`), separat tjänst/origin från backend.
- **Två Postgres-roller**, alltid:
  - `POSTGRES_USER`/`DATABASE_URL` — superuser, används **endast** för schemamigrationer
    (Alembic). Superusers kringgår Row-Level Security ovillkorligt i Postgres, så denna roll
    får aldrig användas för vanlig frågehantering.
  - `mainai_app`/`APP_DATABASE_URL` — begränsad roll, används för **all** frågehantering vid
    körning. Det är detta som gör RLS verkningsfull. Skapas automatiskt av
    `backend/db-init/01-app-role.sh` i Docker Compose, eller manuellt (se nedan).

## Ren installation

### Docker Compose (rekommenderat lokalt/för enkel drift)

```bash
cp .env.example .env
# Fyll i SECRET_KEY, FOUNDER_EMAIL/FOUNDER_PASSWORD, MAINAI_APP_PASSWORD, ev. API-nycklar.
docker compose up -d postgres redis qdrant
docker compose run --rm backend alembic upgrade head
docker compose up -d backend frontend
```

`backend/docker-entrypoint.sh` kör `alembic upgrade head` automatiskt vid varje
containerstart också — det extra `alembic upgrade head`-steget ovan är för att se
migrationsloggen tydligt vid en helt ny installation, inte strikt nödvändigt.

Vid första uppstart skapas automatiskt det enda grundarkontot MainAI tillåter, från
`FOUNDER_EMAIL`/`FOUNDER_PASSWORD` (förverifierat, inget e-postflöde krävs — se
`app/bootstrap.py`, `app/founder.py`). Publik självregistrering är avstängd i produktion —
se `docs/FOUNDER_KNOWLEDGE_BOOTSTRAP.md`.

### Manuell installation (utan Docker)

```bash
# 1. Postgres: skapa databas + den begränsade rollen (se backend/db-init/01-app-role.sh
#    för exakt SQL om du inte kör Compose).
createdb lifeos
psql lifeos -c "CREATE ROLE mainai_app LOGIN PASSWORD '<lösenord>';"
psql lifeos -c "GRANT USAGE ON SCHEMA public TO mainai_app;"
psql lifeos -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON TABLES TO mainai_app;"
psql lifeos -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON SEQUENCES TO mainai_app;"

# 2. Backend
cd backend
pip install -r requirements.txt
export DATABASE_URL=postgresql://<superuser>@localhost/lifeos
export APP_DATABASE_URL=postgresql://mainai_app:<lösenord>@localhost/lifeos
alembic upgrade head
uvicorn app.main:app

# 3. Frontend — same-origin by default (see frontend/app/api/[...path]/route.ts and
#    docs/RENDER_DEPLOY.md): the browser calls this server's own /api/*, which this server
#    forwards to the backend server-side via INTERNAL_API_URL. No NEXT_PUBLIC_API_URL needed.
cd frontend
npm ci
INTERNAL_API_URL=http://localhost:8000 npm run build
INTERNAL_API_URL=http://localhost:8000 npm start
```

**`Base.metadata.create_all` används aldrig i produktion.** Schemat ägs uteslutande av
Alembic-migrationerna i `backend/alembic/versions/`. Om du ser `create_all` köras någonstans
i produktionskod är det en regression — flagga det.

## Uppgradering

```bash
git pull
cd backend && pip install -r requirements.txt
alembic upgrade head        # applicerar alla nya migrationer i ordning
# starta om backend-processen (eller: docker compose up -d --build backend, som kör
# migrationen automatiskt via docker-entrypoint.sh innan appen startar)
cd ../frontend && npm ci && npm run build
```

**Ta alltid en backup innan du kör `alembic upgrade head` i produktion** (se nästa avsnitt).
Migrationerna i det här projektet är skrivna för att aldrig retroaktivt förstöra befintlig
data (se t.ex. `0003_account_lifecycle.py`:s kommentarer om hur befintliga användare
grandfathras in som verifierade istället för att plötsligt låsas ute), men en backup kostar
minuter och en trasig produktionsdatabas kostar mycket mer.

Verifiera efteråt:
```bash
alembic current   # ska visa senaste revision (t.ex. "0003 (head)")
```

## Backup och återställning

```bash
# Backup (kör regelbundet, inte bara innan migrationer)
pg_dump -Fc "$DATABASE_URL" > backup_$(date +%Y%m%d_%H%M%S).dump

# Återställning till en TOM databas
createdb lifeos_restored
pg_restore -d lifeos_restored backup_20260718_120000.dump
```

Qdrant: ta snapshots via dess eget API (`POST /collections/{name}/snapshots`) — se
Qdrant-dokumentationen. Detta projekt lagrar ingen känslig data i Qdrant (embeddings av
företagets kunskapsbas), men förlust av index innebär att RAG-sökningen slutar fungera tills
dokumenten indexeras om.

Redis: rate limiting-räknarna i Redis är **inte** kritisk data — vid förlust återställs de
bara till noll (mildare skydd tillfälligt, ingen säkerhetslucka). Ingen backup behövs.

## Rollback / downgrade

Varje migration har en testad `downgrade()` (se `backend/alembic/versions/`, och
`.github/workflows/ci.yml`:s `migration-check`-jobb som kör en fullständig
downgrade-till-base-och-upp-igen som en obligatorisk kontroll på varje push).

```bash
alembic downgrade -1        # en migration bakåt
alembic downgrade 0002      # till en specifik revision
alembic downgrade base      # allt bort (destruktivt — bara för engångs-teständamål)
```

**Viktigt:** en `downgrade()` tar bara bort schemat en migration lade till (t.ex. tar
`0003`:s downgrade bort `email_verified`-kolumnen och konto-tokentabellerna) — den återställer
**inte** data som fanns i de borttagna kolumnerna/raderna. En riktig rollback i produktion
efter att ha upptäckt ett problem bör normalt vara: **återställ från backup**, inte
`alembic downgrade`, om migrationen redan körts mot verklig data. `alembic downgrade` är
främst för utveckling/CI och för att verifiera att migrationerna är korrekt skrivna åt båda
hållen.

## Städjobb (utgångna/återkallade auth-tokens)

`backend/app/cleanup.py` — se `docs/AUTH_THREAT_MODEL.md` och koden själv för fullständig
motivering. Körs automatiskt var `CLEANUP_INTERVAL_HOURS`:e timme (standard 24) i varje
backend-instans (`ENABLE_SCHEDULED_CLEANUP=true`, standard), skyddat av ett Postgres
advisory lock så att bara en instans faktiskt gör jobbet even om flera instanser kör samma
schema samtidigt.

- **Manuell körning**: `POST /api/admin/cleanup` (kräver adminroll) — returnerar antal
  raderade rader per tabell.
- **Retention**: `TOKEN_CLEANUP_RETENTION_DAYS` (standard 30) — utgångna/återkallade/använda
  tokens behålls så länge för säkerhetsrevision (t.ex. utreda ett
  refresh-token-återanvändningsincident i efterhand), raderas permanent därefter.
- **Avstängning**: `ENABLE_SCHEDULED_CLEANUP=false` om du hellre kör det via en extern
  schemaläggare (t.ex. ett Kubernetes CronJob som anropar admin-endpointen) — rekommenderat
  om du kör många repliker och vill ha en enda, observerbar körplats istället för att lita
  på att advisory-locket alltid gör rätt instans "vinnaren".

## Rate limiting och Redis-drift

Se `docs/AUTH_THREAT_MODEL.md` och `app/limiter.py`. Sammanfattning:

- **`REDIS_URL` satt** (rekommenderat, `docker-compose.yml` sätter detta mot `redis`-tjänsten
  som standard): rate limiting är delad över alla backend-instanser och överlever omstart.
- **`REDIS_URL` osatt**: faller tillbaka till processlokal in-memory-räkning — **korrekt bara
  med exakt en backend-instans**. Loggar en varning vid uppstart om `ENVIRONMENT=production`
  och `REDIS_URL` saknas.
- **Redis otillgängligt vid uppstart** (när `REDIS_URL` är satt): backend **vägrar starta**
  (`app/main.py`:s `_check_redis_reachable`) — medvetet fail-fast istället för att tyst tappa
  det delade skyddet. Åtgärda genom att säkerställa att Redis-tjänsten är uppe innan backend
  startar (`depends_on` i `docker-compose.yml` gör detta automatiskt).
- **Redis otillgängligt under drift** (efter lyckad uppstart): rate limiting-anrop börjar
  faila. `slowapi`/`limits`-biblioteket propagerar det som ett fel på det anropet — övervaka
  felfrekvens på `/api/auth/*` som en indikator.

## GitHub Actions — obligatoriska kontroller före merge

`.github/workflows/ci.yml` kör vid varje push/PR:
`lint-and-typecheck`, `npm-audit`, `frontend-build` (Docker- och Vercel-läge),
`backend-tests`, `rls-security-tests`, `account-rate-limit-tests`, `migration-check`,
`e2e-tests` — och en samlande `all-checks-passed`-jobb.

**Detta blockerar INTE merge automatiskt** förrän branch protection är påslaget i
repo-inställningarna (kodändringar kan inte göra det åt dig — det är en
repo-administratörsåtgärd):

1. GitHub → repo → Settings → Branches → Add branch protection rule (för `main`).
2. Kryssa i "Require status checks to pass before merging".
3. Välj `All required checks passed` (jobbet `all-checks-passed` ovan) som obligatorisk
   check — den är beroende av alla andra jobb, så ett enda kryss räcker.
4. (Rekommenderat) Kryssa även i "Require branches to be up to date before merging".

Efter `all-checks-passed` finns även `deploy-render` — **permanent avstängt sedan
2026-07-21, superseded av den manuellt gate:ade Strato VPS-arkitekturen** (se
`docs/STRATO_VPS_DEPLOY.md`). Jobbet gör inte längre något nätverksanrop och läser inte
längre några Render-hemligheter alls — det är inte bara villkorat på att secrets saknas,
själva koden som skulle anropat Render Deploy Hook-URL:erna är borttagen ur
`.github/workflows/ci.yml`. Historiskt (innan 2026-07-21) var detta vägen en Render-deploy
triggades på via GitHub Actions; det stycket nedan är kvar som historik.

**Detta täcker fortfarande inte varje väg till en Render-deploy**: om ett Render Blueprint
fortfarande är länkat mot repot i Render-dashboarden med "Auto Sync" påslaget kan Render
själv applicera `render.yaml`-ändringar (inklusive ett runtime-byte) direkt vid push till
den länkade branchen, oberoende av GitHub Actions och oberoende av att `deploy-render`-jobbet
ovan nu är avstängt — det är en Render-dashboard-inställning, inte något en kodändring i det
här repot kan styra. Bekräfta manuellt i Render-dashboarden att Auto Sync är avstängt (manuell
sync) om du vill vara säker på att ingen push någonsin kan nå Render. Se avsnittet om Auto
Sync i `docs/RENDER_DEPLOY.md` (nu markerat superseded, men innehållet om Auto Sync-risken
gäller fortfarande tills det bekräftats avstängt i dashboarden).

## Incidenthantering

### Misstänkt stulen/återanvänd refresh-token

Systemet upptäcker detta **automatiskt**: en redan roterad-bort refresh-token som presenteras
igen återkallar hela sessionsfamiljen omedelbart (`refresh_token_reuse_detected` i
`audit_log`) — se `docs/AUTH_THREAT_MODEL.md`. Vid en rapporterad misstanke:

1. Sök `audit_log` för `action = 'refresh_token_reuse_detected'` och den drabbade
   `entity_id` (familje-ID) för att se omfattning och tidpunkt.
2. Kontrollera `refresh_tokens`-tabellen för den familjen: `ip_address`/`user_agent` per rad
   ger en forensisk tidslinje (kvar i minst `TOKEN_CLEANUP_RETENTION_DAYS` dagar).
3. Om kontot verkar komprometterat bortom bara den sessionen: uppmana användaren att
   återställa lösenordet (`/forgot-password`) — det återkallar **alla** sessioner för kontot
   omedelbart, inte bara den ena familjen.

### Misstänkt komprometterat konto (annan orsak, t.ex. läckt lösenord)

- Användaren kan själv trigga `POST /api/auth/logout-all` (knappen "Logga ut från alla
  enheter" på `/account`) — dödar alla sessioner direkt.
- En lösenordsåterställning gör samma sak plus byter lösenordet.
- Som admin: det finns idag ingen endpoint för att tvinga fram detta åt en användare (bara
  användaren själv, via inloggning eller lösenordsåterställning, kan trigga det) — en
  admin-driven "tvinga utloggning"-funktion är inte byggd, notera som en framtida
  ROADMAP-post om behovet uppstår.

### Konto raderat av misstag

**Kontoradering är permanent och oåterkallelig by design** (GDPR-krav, se
`docs/SECURITY_BLOCKERS.md`) — konversationer och kontodata raderas fysiskt, inte bara
mjukraderas. Det finns **ingen** "ångra"-funktion i applikationen. Enda vägen tillbaka är att
återställa databasen från en backup som föregår raderingen (se "Backup och återställning"
ovan) — vilket också återställer allt annat som hänt sedan dess, så gör det bara om
konsekvenserna av det är acceptabla, eller exportera/återskapa manuellt utifrån backupen
istället för en fullständig databasåterställning.

### SMTP nere / e-postleverans fungerar inte

`app/email.py` kastar aldrig ett fel mot anroparen om utskicket misslyckas (registrering/
återställning måste fortsätta fungera även om mejlet inte kom fram — kontot/token:en finns
redan). Ett misslyckat SMTP-anrop loggas som `logger.exception("Kunde inte skicka e-post
till %s", to)` (utan mejlets innehåll — se nedan). Övervaka den loggposten. Användare kan
alltid begära `POST /api/auth/resend-verification` eller `POST /api/auth/forgot-password`
igen när SMTP är återställt.

**Aldrig** aktivera `DEV_MAIL_OUTBOX_DIR` i produktion — det skriver hela mejlet (inklusive
en giltig engångstoken) till en fil om SMTP inte är konfigurerat. Avsett endast för lokal
utveckling (se `.env.example`).

`SMTP_HOST` är obligatorisk så fort `ENVIRONMENT=production` — backend vägrar starta annars
(`_check_smtp_configured` i `app/main.py`), samma fail-closed-princip som `REDIS_URL`-kontrollen
ovan.

### Backend startar inte: `role "postgres.<project-ref>" does not exist`

Verifierat fel 2026-07-19, fixat i `backend/scripts/security/ensure_app_role.py`. Uppstår vid
`ALTER DEFAULT PRIVILEGES` när `DATABASE_URL` pekar på Supabases **Session pooler** — dess
anslutningssträng har ett användarnamn av formen `postgres.<project-ref>`, en
pooler-inloggningsidentitet, inte ett riktigt Postgres-rollnamn (den faktiska rollen, oftast
`postgres`, finns bara i `pg_roles`). Skriptet frågar numera `SELECT current_user` för den
riktiga anslutna rollen istället för att anta att URL-användarnamnet är rollen. Se
`docs/RENDER_DEPLOY.md`s avsnitt "Databasrollerna" för hela bakgrunden (inklusive varför Session
pooler används istället för Direct connection) och `backend/tests/backend/test_ensure_app_role.py`
för regressionstestet. Om detta fel dyker upp igen efter framtida ändringar i skriptet: det är
nästan alltid ett tecken på att någon kod börjat lita på `DATABASE_URL`s användarnamn som ett
rollnamn igen istället för att fråga databasen.

### Backend startar (nästan) men kraschar sedan: `FATAL: (NODENOTIFIER) no tenant identifier provided (external_id or sni_hostname required)`

Verifierat fel 2026-07-20 på en riktig produktionsdeploy, EFTER att `ensure_app_role.py` redan
lyckats — det här är alltså inte samma fel som ovan. Uppstår när appens egna runtime-anslutningar
(via `APP_DATABASE_URL`, den begränsade `mainai_app`-rollen) går genom Supabases Session pooler:
Supavisor kräver ett `.{project-ref}`-suffix på **varje** användarnamn den routar (samma suffix
som `DATABASE_URL`s eget användarnamn redan har, t.ex. `postgres.ruwihvifpgftcwakdmvo`), annars
vet den inte vilket projekts Postgres anslutningen hör till. `ensure_app_role.py` byggde tidigare
`APP_DATABASE_URL`s användarnamn som bara `mainai_app`, utan det suffixet. Fixat i skriptets
`_app_username()` — suffixet kopieras nu från `DATABASE_URL`s användarnamn när ett finns. Se
`docs/RENDER_DEPLOY.md`s avsnitt "Databasrollerna" och
`backend/tests/backend/test_ensure_app_role.py`.

### Backend startar (nästan) men kraschar sedan: `password authentication failed for user "mainai_app"` — och rot-URL:en ger 502 trots att `/api/health` gav 200 i loggen

Verifierat fel 2026-07-20 på en riktig produktionsdeploy — ett TREDJE, separat pooler-fel, inte
samma som de två ovan. Uppstod EFTER att både `ensure_app_role.py` och backend-hälsokontrollen
redan lyckats. Rotorsak: `ensure_app_role.py` roterade `mainai_app`s lösenord **på varje
enda uppstart**, även när det redan var korrekt — under Supabases Session Pooler (Supavisor)
kan det göra poolerns egen auth-cache kortvarigt inaktuell, så nästa anslutning med SAMMA
lösenord kan avvisas i några sekunder innan cachen hinner uppdateras. `app/main.py`s
`on_startup()` gjorde sin första `APP_DATABASE_URL`-anslutning utan något återförsök alls, så
den transienta avvisningen tog ner hela processen. Den efterföljande 502:an på rot-URL:en
(trots att loggen visade en senare lyckad uppstart och flera `/api/health 200`) var en
sekundäreffekt: Render hade redan låst deployen som misslyckad utifrån det första kraschade
försöket och rev troligen ner den instans som faktiskt blev frisk — inte en separat bugg i
frontend eller entrypointen (`node server.js` lyssnade bevisligen korrekt fram till en extern,
signalstyrd avstängning, inte en krasch).

Fixat: `ensure_app_role.py` ändrar nu bara lösenordet vid första rollskapandet eller ett
uttryckligt `MAINAI_APP_ROTATE_PASSWORD=true` (se `docs/RENDER_DEPLOY.md`), och gör en
självtest-anslutning med återförsök/backoff varje gång lösenordet faktiskt ändras.
`app/main.py`/`app/db.py`s nya `call_with_db_retry` ger samma skydd som ett andra lager kring
appens egen uppstart. Se `docs/RENDER_DEPLOY.md`s avsnitt "Databasrollerna" ("Ett tredje,
separat pooler-fel"), `backend/tests/backend/test_ensure_app_role.py`,
`backend/tests/backend/test_db_retry.py`, och Container E i
`.github/workflows/ci.yml`s `combined-container-verify`.
