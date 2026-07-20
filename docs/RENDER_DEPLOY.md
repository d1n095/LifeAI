# Render-driftsättning

Detta dokument beskriver `render.yaml` (repo-roten) — ett [Render
Blueprint](https://render.com/docs/blueprint-spec) för en enda Render Free-webbtjänst som kör
hela stacken: Next.js-frontend och FastAPI-backend som syskonprocesser i **samma container**,
mot en extern Postgres (Supabase Free, med pgvector) och extern Redis (Upstash Free). Mål:
0 kr/månad.

**Den faktiska live-adressen är `https://lifeai-1.onrender.com`.** Tidigare versioner av det
här dokumentet och `render.yaml` antog att dashboard-*namnet* var `LifeAI` (utan `-1`) och att
`-1` bara var ett hostnamnstillägg Render la på eftersom slugen `lifeai` redan var upptagen. Det
antagandet var **fel** — grundaren har verifierat direkt i Render-dashboarden (2026-07-19) att
tjänstens faktiska namn är `LifeAI-1`. `render.yaml`s enda tjänst heter därför `LifeAI-1`
(skiftlägeskänsligt) för att matcha exakt.

Varför namnet troligen blev `LifeAI-1` i första hand: enligt Renders egen dokumentation lägger
Render till ett suffix på en *ny* resurs specifikt för att undvika en namnkrock med en redan
existerande resurs, när ett Blueprint syncas mot ett namn som redan är upptaget
(https://render.com/docs/infrastructure-as-code). Det är en trolig förklaring till att den
levande tjänsten fick suffixet `-1` — ett tidigare syncförsök mot ett Blueprint som (liksom
detta dokument tidigare gjorde) angav `name: LifeAI` utan `-1`. Detta är en rimlig förklaring
grundad i Renders dokumenterade beteende, inte en bekräftad logg av vad som faktiskt hände i det
här kontot.

**Status 2026-07-20, verifierat direkt i dashboarden:** Blueprintet är redan syncat och har
redan adopterat den befintliga tjänsten. Den kör redan `runtime: docker` mot
`dockerfilePath: ./Dockerfile.combined` med tom Root Directory, exakt som `render.yaml`
beskriver — den äldre, separata Node-frontend-arkitekturen är inte längre i drift. Se
"Klick-steg i Render" nedan för vad som faktiskt återstår (en enda manuell redeploy, inte en
runtime-övergång).

## Varför en enda container, inte separat frontend/backend

Render Free erbjuder ingen **Private Service**-plan (den tidigare arkitekturen i det här
dokumentet förutsatte det). Utan en privat tjänsttyp skulle en fristående FastAPI-tjänst på
Free-planen få en publik URL vare den vill eller ej. Lösningen: kör Next.js och FastAPI som två
processer i **samma** container (`Dockerfile.combined`,
`scripts/entrypoint-combined.sh`) istället för två Render-tjänster:

- **Next.js** binder publikt till `$PORT` — det här är den enda process som Render faktiskt
  exponerar.
- **FastAPI** binder till `127.0.0.1:8000` — loopback-only. Ingenting utanför containern kan
  nå den, oavsett vad Render gör på plattformsnivå med tjänstens publika URL, eftersom
  operativsystemets socket helt enkelt inte lyssnar på något annat än loopback-gränssnittet.
  Det här ersätter Private Service-isoleringen utan att kosta något extra.
- `frontend/app/api/[...path]/route.ts` proxar `/api/*`-anrop till FastAPI via
  `INTERNAL_API_URL=http://127.0.0.1:8000` — samma same-origin-proxy-mönster som tidigare,
  bara att båda ändarna nu råkar dela process-namespace istället för att prata över Renders
  interna nätverk mellan två tjänster.

Det finns bara **en** tjänst i `render.yaml`: `LifeAI-1`, `plan: free`. Ingen
`mainai-backend`-tjänst ska någonsin skapas separat — det vore både en extra kostnad och
onödigt, eftersom loopback-bindningen redan ger samma isolering.

### Verifierat lokalt (utan Docker — se `docker`-anmärkningen nedan)

Docker-daemonen är inte tillgänglig i den sandlåda det här arbetet gjordes i, så
`Dockerfile.combined` kunde inte byggas och köras som en riktig container. Istället kördes
exakt samma kommandon (`docker-entrypoint.sh` + `uvicorn --host 127.0.0.1` och
`node server.js`) direkt på sandlåde-hosten, med symlänkar som efterliknar containerns
filstruktur (`/app/backend`, `/app/frontend-standalone`) — logiken i
`scripts/entrypoint-combined.sh` (bindning, proxy, signalhantering, processlivscykel) är
alltså verifierad på riktigt, även om själva Docker-bygget inte kunde köras. Detaljer:

- **Loopback-isolering**: bekräftad på socket-nivå via `/proc/net/tcp` (inte bara tolkning av
  `--host`-flaggan) — backend lyssnar bara på `0100007F:1F40` (127.0.0.1:8000), frontend på
  `00000000:...` (0.0.0.0:$PORT).
- **Proxykedjan**: `curl` genom hela kedjan (webbläsarens ingångspunkt → Next.js → loopback →
  FastAPI → riktig Postgres/Redis) — `/api/health` och en riktig `POST /api/auth/register` gav
  korrekta svar.
- **Signalhantering**: `SIGTERM` till entrypoint-processen gav en ren, korrekt ordnad
  nedstängning av båda barnprocesserna och lämnade inga föräldralösa processer kvar.
- **Krasch-länkning**: `kill -KILL` mot enbart backend-processen utlöste `wait -n`-fångsten,
  loggade att den andra processen stängs ner, och hela entrypoint-skriptet avslutades
  fullständigt — bekräftar att en krasch i endera processen tar ner hela containern (vilket är
  avsiktligt: en halvdöd container utan fungerande backend ska inte fortsätta serva trafik).
- **Minnesförbrukning**: RSS-sampling (`/proc/<pid>/status`, var 0.5:e sekund) under en riktig
  Playwright-driven E2E-körning mot den kombinerade containern gav ≈198 MB kombinerat i vila
  och ≈298 MB kombinerat under belastning (backend ≈180 MB, frontend ≈121 MB vid toppen). Detta
  är uppmätt, inte skattat — men jämförelsen mot Render Free-planens exakta minnesgräns är
  **inte** verifierad från den här miljön (ingen nätverksåtkomst till render.com), så
  jämförelsen bör bekräftas i dashboarden innan produktionsdeploy.

## pgvector istället för Qdrant

Ingen separat Qdrant-tjänst finns längre. Embeddings lagras i en `document_chunks`-tabell i
samma Postgres, med `pgvector`-kolumnen (`vector(1536)`) och ett HNSW-index
(`backend/alembic/versions/0004_pgvector_document_chunks.py`). Se
`backend/app/rag/vector_store.py` för sökning/skrivning och
`backend/app/models/document_chunk.py` för schemat.

`document_chunks` har **full Row-Level Security** (`ENABLE` + `FORCE ROW LEVEL SECURITY`,
policy i `backend/app/rls.py`), scopead per `owner_id` — till skillnad från `Document` självt
(som förblir delad "företagskunskap" enligt befintlig design) är chunks/embeddings strikt
per-uppladdare. Både sökning (`search()`) och skrivning (`upsert_chunks()`) filtrerar explicit
på `owner_id` **och** förlitar sig på RLS som försvar i djupet — se
`backend/tests/security/test_rls_isolation.py` för tester som bevisar att två användare aldrig
kan läsa eller söka varandras chunks, varken via en direkt query eller via den faktiska
`search()`-kodvägen chatt/kunskapssökning använder, inklusive ett scenario där applikationskod
av misstag skickar fel `owner_id` till `search()` — RLS-sessionen (satt via
`SET LOCAL app.current_user_id`) begränsar ändå frågan till en omöjlig skärning, inte till
"ofiltrerat".

Bakgrundsindexering (`backend/app/routers/documents.py`s `_index_in_background`) körs i en
egen `SessionLocal()`/event loop som aldrig passerar `app/deps.py`s `get_current_user` — den
sätter därför `app.current_user_id` explicit från dokumentets `uploaded_by`-kolumn (ett
beständigt DB-värde), inte från någon contextvar som bara existerar under en HTTP-request.

pgvectors `<=>`-operator returnerar **avstånd** (0 = identiskt, 2 = motsatt) — motsatt riktning
mot Qdrants likhetspoäng (högre = bättre). `vector_store.search()` konverterar
`likhet = 1 - avstånd` innan resultatet används, så Trust Engine-logiken som litar på "högre
poäng = bättre träff" är oförändrad.

Lokal utveckling och CI använder samma pgvector-aktiverade Postgres-image
(`pgvector/pgvector:pg16`, se `docker-compose.yml` och `.github/workflows/ci.yml`) som
produktion (Supabase Free, som har pgvector aktiverat) — ingen risk att en migration eller
frågeplan beter sig annorlunda i produktion än lokalt.

## `/api/health` — generiskt svar, inga interna detaljer

`backend/app/routers/health.py` kontrollerar både databasen (`SELECT 1`) och Redis (`PING`, om
`REDIS_URL` är satt). Om något beroende misslyckas: HTTP **503** och `{"status":
"unavailable"}` — inga adresser, inga stacktraces, inga interna felmeddelanden i svaret. Den
faktiska exceptionen loggas fullständigt server-side (`logger.exception`) för felsökning, men
skickas aldrig till klienten. Verifierat manuellt genom att stänga av databasanslutningen
(`ALTER DATABASE ... CONNECTION LIMIT 0`) och bekräfta både klientsvaret och serverloggen, samt
återhämtning till `{"status": "ok"}`/200 efter att gränsen återställdes.

## Databasrollerna — varför det inte är en enda `DATABASE_URL`

Backend kör alla runtime-frågor genom en **egen, icke-superuser databasroll** (`mainai_app`) —
inte den admin-roll `DATABASE_URL` pekar på. Det är detta som gör Row-Level Security
verkningsfull: en superuser/ägarroll kringgår RLS per definition.

Supabase stödjer inte att montera ett Postgres-init-skript (det
`backend/db-init/01-app-role.sh` gör lokalt via Docker Compose), så samma mekanism som i den
tidigare arkitekturen används: vid varje containerstart (`backend/docker-entrypoint.sh`, innan
`alembic upgrade head`) körs `backend/scripts/ensure_app_role.py`, som ansluter med
`DATABASE_URL` (admin-rollen) och idempotent skapar/uppdaterar `mainai_app`-rollen. Skriptet
skriver den fullständiga `APP_DATABASE_URL` till en tillfällig fil som entrypoint-skriptet
`source`:ar innan `uvicorn` startar.

**Viktigt — uppdaterat 2026-07-19, ersätter tidigare felaktig rekommendation:** Denna sektion
sa tidigare att `DATABASE_URL` måste peka på Supabases **DIRECT**-anslutning (port 5432), och
varnade för "den poolade pgbouncer-anslutningen (port 6543)". Det är fortfarande sant att
**Transaction pooler** (port 6543, transaction-pooling-läge) inte pålitligt stödjer
rollskapandet och Alembics DDL, som behöver sessionsnivå-operationer.

Men Direct connection visade sig i praktiken **inte vara nåbar från Render** — Supabases Direct
connection är IPv6-only, och Render Free saknar utgående IPv6. Den faktiska, verifierade lösningen
är Supabases **Session pooler** (Supavisor i sessionsläge, körs också över port 5432, men med ett
annat värdnamn/portmönster än Direct — kopiera den exakta anslutningssträngen Supabase visar för
"Session pooler" specifikt, inte "Transaction pooler"). Session pooler stödjer sessionsnivå-
operationer (det är hela poängen med sessionsläge, till skillnad från transaction-läge) och löser
IPv4-problemet, eftersom Supavisor själv är IPv4-nåbart även när den bakomliggande Postgres-
instansen bara har en IPv6-adress.

**Känt fel att inte upprepa:** Session poolerns anslutningssträng har ett användarnamn av formen
`postgres.<project-ref>` (t.ex. `postgres.ruwihvifpgftcwakdmvo`) — det är Supavisors
pooler-inloggningsidentitet, inte ett riktigt Postgres-rollnamn. Kod som antar att
`DATABASE_URL`s användarnamn direkt är ett rollnamn (t.ex. för `ALTER DEFAULT PRIVILEGES FOR
ROLE <användarnamn>`) kraschar med `psycopg2.errors.UndefinedObject: role
"postgres.<project-ref>" does not exist`, eftersom poolern mappar den identiteten till den
faktiska rollen (`postgres`) internt — ingen roll med det pooler-namnet finns i `pg_roles`.
`backend/scripts/ensure_app_role.py` frågar numera `SELECT current_user` för att få den
faktiska anslutna rollen istället för att anta att URL-användarnamnet är rollnamnet — se
skriptets kommentarer och `backend/tests/backend/test_ensure_app_role.py` för regressionstestet.

Lokal Docker Compose är oförändrad (skriptet är ett no-op där — `MAINAI_APP_PASSWORD` sätts
bara på Render, inte på `backend`-containern i `docker-compose.yml`). Lokal Postgres har heller
aldrig någon pooler i vägen, så `current_user` där är alltid helt enkelt anslutningens eget
användarnamn (t.ex. `lifeos`) — samma beteende som innan denna fix, verifierat av
`test_full_script_run_against_real_local_postgres_is_idempotent` i samma testfil.

## Miljövariabler — fullständig lista

### Genererade hemligheter (Render slumpar värdet — hamnar aldrig i repot)

| Variabel | Vad den styr |
|---|---|
| `SECRET_KEY` | Signerar JWT-access-/refresh-tokens |
| `MAINAI_APP_PASSWORD` | Lösenord för den begränsade RLS-databasrollen (se ovan) |

### Måste fyllas i manuellt i Render-dashboarden (`sync: false` i render.yaml)

| Variabel | Vad du sätter |
|---|---|
| `FOUNDER_EMAIL` / `FOUNDER_PASSWORD` | Det enda grundarkontot MainAI någonsin tillåter (fast primärnyckel, se `backend/app/founder.py`) — skapas automatiskt vid första uppstart om det inte redan finns |
| `DATABASE_URL` | Supabase Free — **Session pooler**-anslutningen (Supavisor, port 5432) — INTE Direct (IPv6-only, onåbar från Render Free) och INTE Transaction pooler (port 6543, stödjer inte sessionsnivå-DDL). Se "Databasrollerna" ovan. |
| `REDIS_URL` | Upstash Free |
| `SMTP_HOST` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_FROM_EMAIL` | **Obligatoriskt i produktion** (`ENVIRONMENT=production` utan `SMTP_HOST` gör att backend vägrar starta, se `_check_smtp_configured` i `app/main.py`) — en gratis transaktionell e-postleverantör (t.ex. Resend eller Brevos gratisnivå) räcker |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` / `DEEPSEEK_API_KEY` / `OPENROUTER_API_KEY` | Fyll i minst en |

### Redan satta, synliga värden i `render.yaml`

| Variabel | Värde | Kommentar |
|---|---|---|
| `ENVIRONMENT` | `production` | Utlöser SMTP-tvånget ovan |
| `INTERNAL_API_URL` | `http://127.0.0.1:8000` | Loopback — proxyn når backend härigenom, se arkitekturavsnittet ovan |
| `FRONTEND_ORIGINS` | `https://lifeai-1.onrender.com` | Defense-in-depth — se "CORS" nedan |
| `PUBLIC_APP_URL` | `https://lifeai-1.onrender.com` | Länkar i verifierings-/återställningsmail |
| `COOKIE_SECURE` / `COOKIE_SAMESITE` | `true` / `none` | Oförändrat säkra defaults |

**Ingen `NEXT_PUBLIC_API_URL`** sätts längre någonstans i `render.yaml` — det är en avsiktlig
utelämning, inte ett förbiseende (se `frontend/lib/api.ts`).

### CORS är nästan irrelevant nu

Webbläsaren pratar bara med `lifeai-1.onrender.com` (samma tjänst som proxyn). Proxyanropet
till FastAPI sker server-till-server över loopback (Node `fetch` till `127.0.0.1:8000`) och
skickar ingen `Origin`-header (det är ett webbläsarkoncept), så CORS-mellanvaran i
`app/main.py` triggas inte av den vägen. `FRONTEND_ORIGINS` är kvar av
dokumentations-/försvar-i-djupet-skäl.

## Deploy sker bara via CI, aldrig av ett push i sig

`render.yaml` sätter `autoDeploy: false`. Enda utlösaren är jobbet `deploy-render` i
`.github/workflows/ci.yml`, som körs efter — och bara om — `all-checks-passed` blir grönt, och
bara på push till exakt `claude/det-kommer-mer-879lcm`. Det anropar tjänstens **Deploy Hook**-
URL:er via två separata GitHub Actions-secrets, `RENDER_BACKEND_DEPLOY_HOOK_URL` och
`RENDER_FRONTEND_DEPLOY_HOOK_URL` — **ingen av dem finns än** (verifierat i jobbloggen för
commit `f9f3899`: båda skriver "is not set — skipping" och avslutar med kod 0) — så länge de
saknas är jobbet ett ofarligt no-op, oberoende av varandra.

## Klick-steg i Render

**Status 2026-07-20, verifierat direkt av grundaren i dashboarden — ersätter allt tidigare
"återstår att byta till Docker"-innehåll i det här avsnittet, som är inaktuellt:** tjänsten
`LifeAI-1` är redan adopterad av Render Blueprintet, kör redan `Runtime: Docker`, har redan en
tom Root Directory, och bygger redan mot `Dockerfile.combined` — runtime-övergången
Node → Docker som föregående version av det här avsnittet beskrev som återstående är alltså
redan genomförd. `DATABASE_URL` pekar redan på Supabases **Session pooler** (port 5432), inte
Direct connection (som är IPv6-only och onåbar från Render Free) — se "Databasrollerna" ovan.

**Senaste kända produktionsstart nådde `ensure_app_role.py` och kraschade med:**
```
role "postgres.<project-ref>" does not exist
```
Rotorsaken var att skriptet antog att `DATABASE_URL`s användarnamn (Session poolerns
pooler-inloggningsidentitet, formen `postgres.<project-ref>`) var ett riktigt Postgres-
rollnamn. **Fixat och mergat till `claude/det-kommer-mer-879lcm`** (commit `f9f3899`, mergar in
`claude/fix-supabase-pooler-role`s två commits `e428ab9`/`e3e4633`) — skriptet frågar nu
`SELECT current_user` för den faktiska anslutna rollen istället. CI grön på merge-commiten:
https://github.com/d1n095/LifeAI/actions/runs/29733943975. Ingen ny miljövariabel, hemlighet
eller dashboard-inställning krävs för fixen — den ligger helt i applikationskoden.

### Vad som konkret återstår: ett enda manuellt klick

Eftersom `render.yaml` har `autoDeploy: false` och CI:s `deploy-render`-jobb är ett medvetet
no-op tills `RENDER_BACKEND_DEPLOY_HOOK_URL`/`RENDER_FRONTEND_DEPLOY_HOOK_URL` sätts som
GitHub Actions-secrets (verifierat i klartext i jobbloggen för merge-commiten ovan — ingen
deploy triggades av den här sessionens push), har den senaste fixen INTE nått produktionen
automatiskt. Nästa steg är:

**I Render-dashboarden, på den befintliga `LifeAI-1`-tjänsten: Manual Deploy → "Deploy latest
commit".**

Det hämtar och bygger om `claude/det-kommer-mer-879lcm`s senaste commit (`f9f3899`, som
innehåller pooler-fixen) mot samma redan korrekta Docker-runtime/`Dockerfile.combined`/
`DATABASE_URL`-konfiguration tjänsten redan har — ingen Blueprint-sync, ingen
runtime-övergång, ingen ny secret eller dashboard-ändring behövs. Om starten fortfarande
kraschar efter det klicket är felet något annat än den nu fixade rollbuggen och bör
undersökas separat innan fler ändringar görs.

## Verifiering efter en fullständig deploy (för senare, när vi är där)

1. `https://lifeai-1.onrender.com/api/health` i webbläsaren → `{"status":"ok"}` (går genom
   hela kedjan: Next.js → loopback → FastAPI → Supabase/Upstash).
2. MainAI är Founder-only — det finns ingen `/register`-sida att testa mot (den omdirigerar
   till `/login`) och `POST /api/auth/register` ska ge `404` i produktion (se
   `backend/app/routers/auth.py`). Verifiera istället att `/login` med `FOUNDER_EMAIL`/
   `FOUNDER_PASSWORD` loggar in, och att `POST /api/auth/register` faktiskt svarar `404` (inte
   `202`) mot den riktiga produktionstjänsten.
3. Kontrollera att ett lösenordsåterställningsmail faktiskt kommer fram till grundarens
   e-postadress (bekräftar `SMTP_HOST` m.fl.) — `/forgot-password` fungerar oavsett
   Founder-only-läget.
4. DevTools → Application → Cookies: `access_token`/`refresh_token` ska stå under
   `lifeai-1.onrender.com`, inte något annat värdnamn.
5. Kontrollera minnesanvändning i Render-dashboardens metrics-flik under riktig belastning —
   jämför mot de lokalt uppmätta ≈198 MB (vila) / ≈298 MB (belastning) som riktmärke, men lita
   på Renders egen siffra, inte den lokala uppskattningen, för det faktiska
   platsbeslutet om minnesgränsen räcker.
