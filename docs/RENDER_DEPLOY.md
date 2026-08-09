# Render-driftsättning

> **SUPERSEDED, 2026-07-21 — Render-driftsättning är inte längre den aktiva vägen.**
> Render ersattes av den manuellt gate:ade Strato VPS-arkitekturen — se
> `docs/STRATO_VPS_DEPLOY.md` och `docs/VPS_ARCHITECTURE.md` för den nuvarande vägen till
> produktion. `.github/workflows/ci.yml`s `deploy-render`-jobb är permanent avstängt (gör
> inget nätverksanrop längre, oavsett vilka secrets som finns satta i repot) — se
> `docs/checkpoints/INTEGRATION_FOUNDER_VPS_2026-07-21.md` för den ändringen. Resten av det
> här dokumentet är bevarat som historisk utredning (Render-namnkonflikten, pgvector- och
> databasroll-utredningarna, SMTP-felsökningen m.m.) — inga instruktioner nedan ska följas
> för att faktiskt driftsätta något längre. Om ett Render Blueprint fortfarande är länkat mot
> det här repot i Render-dashboarden, bekräfta manuellt att "Auto Sync" är avstängt där också
> (det är en dashboard-inställning, inte något en kodändring i det här repot kan styra).

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
`alembic upgrade head`) körs `backend/scripts/security/ensure_app_role.py`, som ansluter med
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
`backend/scripts/security/ensure_app_role.py` frågar numera `SELECT current_user` för att få den
faktiska anslutna rollen istället för att anta att URL-användarnamnet är rollnamnet — se
skriptets kommentarer och `backend/tests/backend/test_ensure_app_role.py` för regressionstestet.

Lokal Docker Compose är oförändrad (skriptet är ett no-op där — `MAINAI_APP_PASSWORD` sätts
bara på Render, inte på `backend`-containern i `docker-compose.yml`). Lokal Postgres har heller
aldrig någon pooler i vägen, så `current_user` där är alltid helt enkelt anslutningens eget
användarnamn (t.ex. `lifeos`) — samma beteende som innan denna fix, verifierat av
`test_full_script_run_against_real_local_postgres_is_idempotent` i samma testfil.

**Ett andra, separat pooler-fel — uppdaterat 2026-07-20:** ovanstående fix löste rollkraschen i
`ensure_app_role.py` självt, men en verifierad produktionsdeploy på `ed85ff3` kraschade ändå,
den här gången EFTER att `ensure_app_role.py` lyckats — appens egna runtime-anslutningar (via
`APP_DATABASE_URL`, den begränsade `mainai_app`-rollen) avvisades av Supavisor med:
```
FATAL: (NODENOTIFIER) no tenant identifier provided (external_id or sni_hostname required)
```
Orsaken: `ensure_app_role.py` byggde `APP_DATABASE_URL`s användarnamn som bara `mainai_app`,
utan poolerns `.{project-ref}`-suffix (som `DATABASE_URL`s eget användarnamn,
`postgres.ruwihvifpgftcwakdmvo`, redan har). Supavisor kräver suffixet på **varje** anslutning
den routar, inte bara admin-anslutningen — utan det vet den inte vilket projekts Postgres
anslutningen ska gå till. Fixat i `_app_username()` i samma skript: suffixet kopieras från
`DATABASE_URL`s användarnamn till `mainai_app`s användarnamn när ett finns (poolat läge); en
vanlig lokal, icke-poolad admin-användare (inget suffix) lämnas orörd. Se
`test_app_database_url_carries_the_pooler_tenant_suffix` och
`test_app_database_url_stays_unsuffixed_for_a_plain_non_pooled_admin_username` i
`backend/tests/backend/test_ensure_app_role.py`.

**Ett tredje, separat pooler-fel — uppdaterat 2026-07-20:** en verifierad produktionsdeploy på
`acac6d1` (som redan har både fixarna ovan) kraschade en tredje gång, EFTER att både
`ensure_app_role.py` OCH backend-hälsokontrollen (se "startup-race"-avsnittet nedan) lyckats:
```
sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) connection to server at
"aws-1-us-west-2.pooler.supabase.com" ... FATAL: password authentication failed for user "mainai_app"
ERROR:    Application startup failed. Exiting.
```
**Rotorsak:** `ensure_app_role.py` körde `ALTER ROLE mainai_app LOGIN PASSWORD ...`
**ovillkorligt vid varje enda uppstart**, även när lösenordet redan var korrekt. Under
Supabases Session Pooler (Supavisor) kan den ALTER ROLE:en göra att poolerns egen
auth-cache blir kortvarigt inaktuell — en anslutning som `mainai_app` strax därefter, med
exakt samma (korrekta, precis satta) lösenord, observerades misslyckas med "password
authentication failed", för att sedan lyckas rakt av på nästa uppstartsförsök, utan någon
kod- eller lösenordsändring alls. Appens egen uppstart (`app/main.py`s `on_startup()`, den
FÖRSTA anslutningen någonsin till `APP_DATABASE_URL` i hela uppstartskedjan) hade ingen
återförsöksmekanism alls — en enda transient avvisning där tog ner hela processen
("Application startup failed. Exiting."), trots att appen i övrigt var frisk.

En sekundäreffekt av just DENNA krasch observerades också: Render-dashboarden visade "Exited
with status 1" som deployens sluttatus (från detta FÖRSTA, kraschade uppstartsförsöket), men
loggen fortsatte sedan med ett NYTT uppstartsförsök som lyckades helt — backend blev frisk,
frontend startade, flera `/api/health`-kontroller gav 200 — innan en ren, signalstyrd
avstängning (`[entrypoint] shutting down... shutdown complete`, INTE en krasch — den
loggraden syns bara vid en riktig SIGTERM, inte vid ett dött delprocess). Samtidigt gav
`https://lifeai-1.onrender.com` (rot-URL:en) 502 trots att `/api/health` gav 200 i loggen.
Den mest sannolika förklaringen: Render hade redan låst deployens status som misslyckad
utifrån det FÖRSTA kraschade försöket och rev senare ner den instans som faktiskt blivit
frisk (interna hälsokontroller i loggen kommer från Renders egna interna probe-adresser,
`10.238.x.x`, inte från publik trafik via edgen) — inte en separat bugg i frontend eller
entrypointen. Frontendens `node server.js` lyssnade bevisligen korrekt (flera lyckade
hälsokontroller, `✓ Ready in 0ms` från Next.js) fram till den externa SIGTERM:en. Detta är
alltså en förväntad, sekundär konsekvens av det första kraschade försöket, inte ett eget fel
att fixa i containern — att förhindra den första kraschen (nedan) förhindrar hela kedjan.

**Fix, i `backend/scripts/security/ensure_app_role.py`:**
1. Lösenordet ändras nu bara när rollen skapas för första gången, eller när
   `MAINAI_APP_ROTATE_PASSWORD=true` är explicit satt för just den deployen — aldrig som en
   bieffekt av en vanlig omstart. Se miljövariabeltabellen nedan.
2. Varje gång lösenordet faktiskt ändras (skapande eller uttrycklig rotation) gör skriptet nu
   en självtest-anslutning mot det nya `APP_DATABASE_URL` med exponentiell backoff (1s/2s/4s/8s)
   innan det rapporterar att det är klart — absorberar precis den propageringsfördröjning som
   orsakade kraschen, redan under provisioneringssteget, innan appen själv någonsin försöker.
3. `app/main.py`s `on_startup()` (och `app/db.py`s nya `call_with_db_retry`) har nu samma
   återförsök/backoff runt sina DB-beröringar, som ett andra skyddslager — en transient
   pooler-avvisning där tar inte längre ner en i övrigt frisk container.
4. Det schemalagda städjobbet (`app/scheduler.py`) var redan resilient (fångar och loggar,
   kraschar aldrig processen) — verifierat oförändrat, inte en ny regression.

Se `backend/tests/backend/test_ensure_app_role.py` (`test_password_is_not_rotated_on_a_normal_restart_when_the_role_already_exists`,
`test_explicit_rotate_password_env_var_rotates_and_self_tests_the_new_credential`,
`test_self_test_connection_retries_transient_failures_then_succeeds`,
`test_self_test_connection_gives_up_and_raises_after_exhausting_every_attempt`),
`backend/tests/backend/test_db_retry.py`, och Container E i `.github/workflows/ci.yml`s
`combined-container-verify`-jobb (kör mot den riktiga avbildningen: en upprepad start mot en
redan provisionerad roll roterar inte lösenordet, och simulerade transienta
anslutningsfel — via test-kroken `TEST_FORCE_APP_DB_CONNECT_FAILURES`, som aldrig sätts i en
riktig deploy — visas faktiskt återförsökta och containern blir ändå frisk).

**Ett fjärde, ännu olöst fall — 2026-07-20, efter merge av `888b41a` (som redan innehåller
alla tre fixarna ovan):** en manuell deploy gick grön/Live, Render-loggen visade en
oavbruten ström av `GET /api/health 200 OK` (Renders egen interna prob, `10.238.x.x`) i
minst en och en halv minut efter "Your service is live 🎉" — men `https://lifeai-1.onrender.com/`
gav 502 och förblev så i flera minuter, inte den korta självläkande blipp som fall tre visade
sig vara. Alltså INTE samma mönster: det första kraschade försöket i fall tre visade sig vara
en sekundäreffekt av en instans som Render redan låst som misslyckad; här finns inget
kraschat försök i loggen alls — bara en frisk container och en 502 på den publika adressen
samtidigt.

Tre hypoteser är sedan dess uttömt testade mot den riktiga avbildningen, alla motbevisade
med mätdata, inte antaganden:

1. **En vanlig appkodsbugg** — uteslutet genom en full processreproduktion (riktigt
   Next.js-standalone-bygge + riktig backend + riktig Postgres/Redis, samma
   `entrypoint-combined.sh`, produktionslika miljövariabler): `/`, `/login` och `/api/health`
   gav alla 200 konsekvent i över 70 sekunder, matchande exakt det friska fönstret i den
   riktiga Render-loggen.
2. **Fel/otydlig port trots `PORT=10000`** — Container F i `combined-container-verify`
   kör nu uttryckligen med `PORT=10000` (Renders faktiska tilldelade värde, bekräftat i
   deploy-loggen — alla tidigare CI-containrar A-E körde bara `PORT=3000`, en verklig,
   otestad lucka). `docker port` visar exakt en distinkt publicerad containersida-port,
   `10000/tcp` (aldrig `8000`, FastAPI förblir loopback-only). Ingen ambiguitet hittad.
3. **OOM under Render Frees minnesgräns** — Container F körs med `--memory=512m
   --memory-swap=512m` (Render Frees vanligen dokumenterade gräns, inte independent
   omkontrollerad mot Renders aktuella dokumentation eftersom den här sandlådan saknar
   utgående nätåtkomst dit). Fem omgångar av riktiga sidladdningar av `/`, `/login`,
   `/edge-probe.html` och `/api/edge-probe` (HTML + varje `/_next/static/*`-tillgång sidan
   refererar, samma mönster en riktig webbläsare gör) höll minnesanvändningen platt runt
   124–130 MiB av 512 MiB (~25 %) — `docker inspect .State.OOMKilled` var `false`
   genomgående. Ingen OOM reproducerad.

**Diagnostik tillagd på `claude/fix-render-public-port` (inte mergad, inte deployad) för att
avgöra var i kedjan felet faktiskt sitter, nästa gång ett kontrollerat deployförsök görs.**
Ingen `middleware.ts`, ingen generell request-loggning — en enda isolerad diagnostikroute
plus den redan existerande statiska kontrollen:

- `frontend/public/edge-probe.html` — en helt statisk fil, ingen React/SSR/serverkod alls.
  Om den är nåbar publikt men `/` inte är det, är felet specifikt i sidrendering, inte i
  Next.js-processen/porten/routingen som helhet.
- `frontend/app/api/edge-probe/route.ts` — `GET /api/edge-probe?probe_id=<id>`, en isolerad
  Next-native route. Proxas INTE till backend (till skillnad från allt annat under `/api/*`,
  se `frontend/app/api/[...path]/route.ts` — Next.js föredrar alltid en statisk
  segment-route över `[...path]`-catch-all:en för en exakt träff), rör ingen databas, Redis
  eller AI, kräver ingen custom server och ändrar inget i Next.js egen startupflöde.
  - Kräver `probe_id` som query-parameter: `^[A-Za-z0-9_-]{1,64}$`, annars `400 Bad
    Request` — ett saknat, tomt, för långt eller otillåtet `probe_id` **ekas eller loggas
    aldrig**, vilket förhindrar logginjektion.
  - Svar vid giltigt `probe_id`: `200`, `Content-Type: text/plain; charset=utf-8`,
    `Cache-Control: no-store`, kroppen `LifeAI edge probe OK`, samt headers
    `X-LifeAI-Probe: edge-v1`, `X-LifeAI-Process-ID` (Node-processens PID),
    `X-LifeAI-Boot-ID` (skapas en gång per Node-process — `crypto.randomUUID()` som en
    modulnivå-konstant, samma värde för varje anrop så länge processen lever, nytt värde
    efter omstart) och `X-LifeAI-Probe-ID` (det validerade värdet ekas tillbaka).
  - Loggar bara lyckade (giltiga) anrop, ett strukturerat fält per rad: `[edge-probe]
    time=<UTC ISO> boot_id=<...> pid=<...> probe_id=<...> host=<saniterad, max 255 tecken>
    proto=<saniterad, max 255 tecken> forwarded_for_present=<true|false>`. `Host` och
    `X-Forwarded-Proto` saniteras (C0-styrtecken och DEL, vilket täcker CR/LF, tas bort
    innan loggning) så att ingen av dem kan injicera falska loggrader. `X-Forwarded-For`
    loggas ENDAST som en boolesk indikator — aldrig adressvärdet. `Cookie`, `Authorization`,
    fullständiga IP-adresser, övriga headers och hela querysträngen loggas aldrig.

**Renders shell/CLI-baserade localhost-testning i en levande instans** — dokumenterat efter
bästa nuvarande kännedom, INTE omkontrollerat mot Renders live-dokumentation (den här
sandlådan har ingen utgående nätåtkomst till render.com, bekräftat via `$HTTPS_PROXY/
__agentproxy/status`, som visar ett explicit policy-avslag för `lifeai-1.onrender.com`).
Render har historiskt erbjudit en webbläsarbaserad "Shell"-flik per tjänst i dashboarden som
öppnar en terminal INUTI den körande containern — därifrån skulle `curl -sS
"localhost:$PORT/api/edge-probe?probe_id=shell-check"` bevisa om appen själv svarar korrekt
helt oberoende av Renders publika edge, eftersom anropet aldrig lämnar containern.
Shell-fliken har traditionellt varit en funktion på Renders betalda planer, inte på Free —
det är oklart om det fortfarande stämmer eller om LifeAI-1:s Free-tjänst har tillgång till
den; kontrollera själv i dashboarden (Shell-fliken, om den finns, under tjänstens sidomeny)
snarare än att lita på det här stycket som ett aktuellt faktum.

**Vad som ska kontrolleras vid nästa enda kontrollerade deployförsök, för att slutgiltigt
avgöra var felet sitter:**

1. Efter att Render visar tjänsten Live: besök
   `https://lifeai-1.onrender.com/api/edge-probe?probe_id=render-check-<datum>` (ett eget,
   unikt `probe_id` — gör det lätt att hitta exakt den raden i en lång logg) i webbläsaren
   eller med `curl -v`. Testa även `https://lifeai-1.onrender.com/edge-probe.html`.
2. Kontrollera **samtidigt** containerloggen i Render-dashboarden för en rad som börjar
   `[edge-probe]` och innehåller just det `probe_id`:t.
3. Kontrollera **samtidigt** om `/api/health`-strömmen (Renders egen probe) fortfarande visar
   `200 OK`-rader.

Fyra möjliga utfall, och vad vart och ett bevisar:

- **Publik dynamisk probe ger 502, OCH probe-ID:t saknas i containerloggen** → anropet når
  aldrig containern. Felet sitter troligen i Renders egen edge/routing före containern —
  utanför vad en kodändring i det här repot kan påverka.
- **Probe-ID:t loggas OCH routen producerade ett lyckat svar internt, men klienten ändå får
  502** → felet ligger på returvägen mellan container och Renders edge — ett
  container-/HTTP-svarsproblem, värt att undersöka vidare i kod (t.ex. `Connection`/
  `Transfer-Encoding`-hantering, eller något Render-specifikt om proxy-headers).
- **Dynamisk probe fungerar men statisk `/edge-probe.html` misslyckas** (eller tvärtom) →
  problemet är specifikt i statisk filhantering eller i route-handler-exekvering, inte i
  Next.js-processen/porten som helhet — värt att isolera vidare.
- **Båda proberna svarar 200, MEN `/` fortfarande ger 502** → Next.js-processen och porten
  fungerar i sig — felet är specifikt i sidrenderingen av `/` (eller `/login`), inte i
  processen/porten/routingen som helhet. Nästa steg då: jämföra `/` mot `/edge-probe.html`
  för vad som faktiskt skiljer dem åt i renderingsvägen (klientkomponent, `AuthGuard`, etc.).
- **Allt fungerar (probes och `/`)** → den tidigare 502:an var tidsbunden eller
  cutover-relaterad. Dokumentera tidpunkten och `boot_id` från detta försök utan att gissa
  vidare — om den behöver undersökas igen finns nu ett konkret, återanvändbart verktyg för
  det.

## ROTORSAK BEKRÄFTAD AV RENDER SUPPORT — 2026-07-20, ersätter alla ovanstående hypoteser

**Detta avsnitt är den slutgiltiga förklaringen.** Allt ovanför denna rubrik (de tre
motbevisade hypoteserna — appkodsbugg, portambiguitet, OOM — och den efterföljande
`/api/edge-probe`-diagnostiken) var seriös, bevisdriven felsökning som metodiskt uteslöt
varje förklaring som gick att testa från kod och CI. Den satte oss i rätt läge för att ställa
Render support en precis fråga, men ingen av de hypoteserna var den faktiska rotorsaken.
Render support har nu bekräftat den riktiga förklaringen direkt, och den är arkitekturell,
inte en bugg i det här repots kod:

**`Dockerfile.combined` kör två separata webbservrar i EN Render Web Service** — FastAPI
(uvicorn) bundet till `127.0.0.1:8000` och Next.js bundet till `0.0.0.0:$PORT` (10000 i
produktion), övervakade som syskonprocesser av `scripts/entrypoint-combined.sh`. Render
Web Services förutsätter EN process som lyssnar på `$PORT` och kan enligt Render supports
egen bekräftelse **icke-deterministiskt välja mellan flera upptäckta webbservrar** i samma
tjänst, snarare än att garanterat och konsekvent routa publik trafik till Next.js-processen.
Detta förklarar exakt det uppmätta mönstret som annars var svårförklarat: Renders EGEN
interna hälsokontroll (som i denna arkitektur alltid till slut når fram till Next.js →
loopback → FastAPI, oavsett vilken av de två processerna Render "råkar" upptäcka för sitt eget
bruk) kunde visa `200 OK` kontinuerligt, medan den PUBLIKA edgen route:ade en del eller all
trafik till fel destination internt i sin egen infrastruktur — konsekvent med att både en
helt statisk fil (`/edge-probe.html`, noll serverkod) och en enkel dynamisk route
(`/api/edge-probe`) gav `502` samtidigt.

Render dokumenterar detta explicit som en känd begränsning och rekommenderar uttryckligen
SEPARATA tjänster för frontend och backend istället för att köra flera webbservrar i en och
samma Web Service:
<https://render.com/docs/faq#can-i-deploy-multiple-apps-to-a-single-render-service>

**Varför `Dockerfile.combined` ändå byggdes så här från början, och varför det INTE var en
uppenbar designbrist i förväg:** Render Free-planen erbjuder ingen "Private Service"-typ (se
toppen av den här filens ursprungliga kommentarer och `render.yaml`s egen motivering) — utan
en andra betald tjänst fanns ingen Render-inbyggd väg att köra FastAPI overksamt-men-privat
bredvid Next.js. Den kombinerade enda-container-lösningen med loopback-isolering
(`127.0.0.1:8000`, aldrig publicerad) var den enda kostnadsfria vägen att uppnå både "en
tjänst, 0 kr/mån" och "backend aldrig direkt nåbar utifrån" SAMTIDIGT på Render specifikt.
Vad som saknades i den ursprungliga designen var inte loopback-isoleringen i sig (den fungerar
korrekt och är fortfarande bevisligen säker — se `Verify port 8000 is not reachable from
outside the container` i `combined-container-verify`), utan antagandet att Render skulle
route:a konsekvent till "processen som faktiskt lyssnar på \$PORT" i alla lägen. Den
bekräftade sanningen är att Render inte garanterar det när fler än en webbserver är
upptäckbar i samma tjänst, oavsett vilken port de facto är rätt.

**Konsekvens för det här repot:** `claude/fix-render-public-port`s diagnostikrutter
(`/api/edge-probe`, `/edge-probe.html`) har fyllt sitt syfte — de bevisade att felet inte
satt i applikationskoden, portkonfigurationen eller minnesgränsen, vilket i sin tur gjorde det
möjligt att ställa Render support en tillräckligt precis fråga för att få den riktiga
rotorsaken bekräftad. **Ingen ytterligare Render-diagnostikfunktion byggs eller planeras.**
Ingen ny Render-deploy görs för att testa detta vidare — arkitekturen i sig (en enda tjänst,
två webbservrar) är den bekräftade begränsningen, inte något ett till försök skulle kunna
runda. Se `docs/STRATO_VPS_DEPLOY.md` för den nya produktionsvägen: en riktig Strato-VPS med
Docker Compose, separata backend-/frontend-containrar på ett privat Docker-nätverk, och Caddy
som den enda publikt exponerade processen — samma säkerhetsegenskap (backend aldrig direkt
nåbar utifrån) som `Dockerfile.combined` försökte uppnå, men med en arkitektur Render supports
egen rekommendation bekräftar är korrekt: separata tjänster/processer bakom en riktig
reverse proxy, inte flera webbservrar i en och samma tjänst.

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

### Valfri, manuell drift-åtgärd (aldrig satt av render.yaml)

| Variabel | Vad du sätter |
|---|---|
| `MAINAI_APP_ROTATE_PASSWORD` | `true` för att uttryckligen rotera `mainai_app`s lösenord på just NÄSTA deploy — sätt den, deploya en gång, ta sedan bort den igen. En vanlig omstart/redeploy rör aldrig lösenordet (se "Ett tredje, separat pooler-fel" ovan) — detta är den enda avsiktliga vägen att ändra det. |

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

> **SUPERSEDED, 2026-07-21:** avsnittet nedan beskriver hur `deploy-render` *tidigare*
> fungerade, som historik. Sedan 2026-07-21 gör jobbet inget nätverksanrop alls längre —
> curl-anropen mot Deploy Hook-URL:erna är borttagna ur `.github/workflows/ci.yml`, inte
> bara villkorade på saknade secrets. Att lägga till `RENDER_BACKEND_DEPLOY_HOOK_URL`/
> `RENDER_FRONTEND_DEPLOY_HOOK_URL` som GitHub Actions-secrets i dag skulle alltså **inte**
> återaktivera något — koden som skulle läst dem finns inte kvar.

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

> **SUPERSEDED, 2026-07-21:** stegen nedan är historik, inte en aktuell instruktion. Render
> är inte längre den avsedda vägen till produktion — se `docs/STRATO_VPS_DEPLOY.md`.

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
