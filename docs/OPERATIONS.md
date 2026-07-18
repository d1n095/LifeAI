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
# Fyll i SECRET_KEY, ADMIN_EMAIL/ADMIN_PASSWORD, MAINAI_APP_PASSWORD, ev. API-nycklar.
docker compose up -d postgres redis qdrant
docker compose run --rm backend alembic upgrade head
docker compose up -d backend frontend
```

`backend/docker-entrypoint.sh` kör `alembic upgrade head` automatiskt vid varje
containerstart också — det extra `alembic upgrade head`-steget ovan är för att se
migrationsloggen tydligt vid en helt ny installation, inte strikt nödvändigt.

Vid första uppstart skapas automatiskt ett admin-konto från `ADMIN_EMAIL`/`ADMIN_PASSWORD`
(förverifierat, inget e-postflöde krävs — se `app/bootstrap.py`).

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

# 3. Frontend
cd frontend
npm ci
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run build
npm start
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
