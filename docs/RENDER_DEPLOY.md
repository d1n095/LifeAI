# Render-driftsättning

Detta dokument beskriver `render.yaml` (repo-roten) — ett [Render
Blueprint](https://render.com/docs/blueprint-spec) för hela webb-stacken: PostgreSQL, Redis,
Qdrant, FastAPI-backend och Next.js-frontend.

**Den faktiska live-adressen är `https://lifeai-1.onrender.com`** — ett frontend-webbtjänst
som redan finns i Render (inte skapat av detta Blueprint). Vercel är inte längre
felsökningsmålet.

**Viktigt att inte blanda ihop:** tjänstens namn i Render-dashboarden är **`LifeAI`** — det är
det namnet `render.yaml` måste matcha för att Blueprintet ska adoptera den befintliga tjänsten
istället för att skapa en dubblett. `lifeai-1.onrender.com` är bara det *hostnamn* Render
tilldelade (eftersom slugen `lifeai` redan var upptagen) — inte tjänstens namn. `render.yaml`s
frontend-tjänst heter därför `LifeAI` (skiftlägeskänsligt, exakt som i dashboarden), inte
`lifeai-1`.

Grundorsaken till `Failed to fetch`/`Kunde inte nå servern` vid registrering var en kombination
av att ingen backend (eller ofullständig backend) var kopplad till den existerande
frontend-tjänsten, och att en cross-origin-arkitektur (separat frontend-/backend-domän) för
med sig en hel klass av CORS-/cookie-fallgropar. Det här Blueprintet löser båda: en komplett,
verifierad backend-stack, och en **same-origin-proxy** som gör att webbläsaren aldrig anropar
backend direkt.

## Arkitektur: same-origin-proxyn

```
Webbläsare                    lifeai-1 (Next.js, web)         mainai-backend (FastAPI, PRIVATE)
    │  https://lifeai-1.onrender.com/api/auth/register             (ingen publik URL alls)
    ├────────────────────────────►│                                        │
    │  (samma origin — inget CORS,│  app/api/[...path]/route.ts           │
    │   inget SameSite-problem)   │  forwarding via INTERNAL_API_URL      │
    │                             ├───────────────────────────────────────►│
    │                             │  Render internt privat nätverk        │
    │  ◄────────────────────────── (Set-Cookie, statuskod, body)          │
```

Webbläsaren pratar **bara** med `lifeai-1.onrender.com`. Den vet aldrig att `mainai-backend`
existerar, och kan det inte heller — `mainai-backend` är en Render **Private Service** (typ
`pserv` i `render.yaml`), utan publik URL över huvud taget, bara nåbar från andra tjänster i
samma Render-projekt över det interna nätverket.

**Varför:** det här är den konkreta lösningen på uppdraget "undvik cross-origin-cookieproblem".
Sessionscookien (`HttpOnly`, se `backend/app/cookies.py`) sätts av backend men skickas till
webbläsaren *via* `lifeai-1`s egen server — webbläsaren ser den som en förstapartscookie
(samma origin den redan pratar med), oavsett var backend faktiskt kör. `SameSite=None` (redan
default i `app/config.py`) fungerar fortfarande, det är bara inte längre den enda linjen som
håller det ihop.

### Proxyn i detalj — `frontend/app/api/[...path]/route.ts`

Varje anrop under `/api/*` från webbläsaren landar i den här Route Handlern, som vidarebefordrar
det byte-för-byte till backend via `INTERNAL_API_URL` (en ren server-side miljövariabel, aldrig
i klientbundeln — se `frontend/lib/api.ts`s kommentar om detta):

- **Metod, query-sträng, body**: strömmas rakt igenom (fungerar oförändrat för både JSON och
  multipart-filuppladdning, och för framtida strömmande svar) — inget parsas/serialiseras om.
- **Headers**: kopieras rakt av, minus de hop-by-hop-headers (RFC 7230 §6.1) som bara gäller ett
  enskilt transportsteg.
- **Set-Cookie**: Node/undicis `getSetCookie()` används explicit — annars slår `Headers`-API:et
  ihop flera `Set-Cookie`-instanser till en kommaseparerad sträng som webbläsaren inte kan tolka
  tillbaka till separata cookies. Verifierat manuellt (se "Verifiering som redan gjorts" nedan)
  att `access_token`- och `refresh_token`-cookien båda kommer fram som separata cookies.
- **Statuskod**: vidarebefordras exakt (verifierat: en 400/401/403/404 från backend syns
  identiskt genom proxyn, inte som ett generiskt Next.js-fel).
- **Caching**: `export const dynamic = "force-dynamic"` — den här routen får **aldrig** cachas
  eller statiskt optimeras av Next.js, eftersom svaret kan vara specifikt för en inloggad
  användare (sessionscookien). Verifierat i produktionsbygget: `/api/[...path]` listas som
  `ƒ Dynamic`, inte `○ Static`.

### Den verkliga klient-IP:n — `X-Forwarded-For`

Med proxyn framför backend skulle `request.client.host` (det `app/limiter.py`s rate limiting,
`app/audit.py` och `app/routers/auth.py`s inloggningslogg alla nycklar på) annars alltid visa
**`lifeai-1`s egen adress** för varje enda användare — rate limiting hade i praktiken blivit
delad mellan alla användare istället för per person. Löst med två delar:

1. Proxyn vidarebefordrar `X-Forwarded-For` precis som alla andra headers (ingen särskild kod
   behövs — Render sätter den redan på anropet som når `lifeai-1`, från den riktiga klienten).
2. `backend/Dockerfile`s uvicorn-kommando kör med `--proxy-headers --forwarded-allow-ips=*` —
   uvicorn litar då på `X-Forwarded-For` och skriver om `request.client.host` därefter.
   `--forwarded-allow-ips=*` (lita på *vem som helst* som ansluter) är bara säkert **eftersom**
   `mainai-backend` är en Private Service utan publik ingång alls — det enda som någonsin kan
   ansluta är `lifeai-1`s proxy. Exponera aldrig den här containerns port publikt med den
   flaggan kvar.

Verifierat manuellt: ett anrop till proxyn med `X-Forwarded-For: 203.0.113.55` gav
`203.0.113.55` i backendens access-logg, inte loopback-adressen curl faktiskt anslöt från.

### Databasrollerna — varför det inte är en enda `DATABASE_URL`

Backend kör alla runtime-frågor genom en **egen, icke-superuser databasroll** (`mainai_app`) —
inte administratörsrollen Render skapar åt dig (`mainai_admin`). Det är detta som gör
Row-Level Security verkningsfull: en superuser/ägarroll kringgår RLS per definition.

Render stödjer inte att montera ett Postgres-init-skript (det `backend/db-init/01-app-role.sh`
gör lokalt via Docker Compose), så `render.yaml` löser det istället genom att:

1. Generera `MAINAI_APP_PASSWORD` som en plattforms-hemlighet (`generateValue: true`).
2. Vid varje containerstart (`backend/docker-entrypoint.sh`, innan `alembic upgrade head`) köra
   `backend/scripts/ensure_app_role.py`, som ansluter med `DATABASE_URL` (admin-rollen) och
   idempotent skapar/uppdaterar `mainai_app`-rollen med samma GRANT:ar som `01-app-role.sh`.
3. Skriptet skriver den fullständiga `APP_DATABASE_URL` till en tillfällig fil som
   entrypoint-skriptet `source`:ar innan `uvicorn` startar.

Verifierat manuellt mot en riktig lokal Postgres, inklusive med specialtecken
(`$ / + = !`) i det genererade lösenordet — rollen skapas/uppdateras korrekt och
`mainai_app` kan ansluta med den URL-kodade `APP_DATABASE_URL`:n.

Lokal Docker Compose är oförändrad (skriptet är ett no-op där — `MAINAI_APP_PASSWORD` sätts
bara på Render, inte på `backend`-containern i `docker-compose.yml`).

### Qdrant

Ingen managed Qdrant-tjänst finns på Render, så `mainai-qdrant` körs som en privat,
disk-uppbackad tjänst av samma officiella image `docker-compose.yml` använder lokalt
(`qdrant/qdrant:v1.11.5`), via `runtime: image` istället för att bygga från en Dockerfile.
Jag kunde inte nå render.com från den här miljön för att bekräfta exakt Blueprint-syntax för
`runtime: image` eller disk-prissättning — verifiera i dashboarden innan Apply.

## Miljövariabler — fullständig lista

### Automatiska (Render kopplar ihop tjänsterna åt dig)

| Variabel | Tjänst | Källa |
|---|---|---|
| `DATABASE_URL` | mainai-backend | `fromDatabase: mainai-postgres` (admin-anslutningen) |
| `REDIS_URL` | mainai-backend | `fromService: mainai-redis` |

### Genererade hemligheter (Render slumpar värdet — hamnar aldrig i repot)

| Variabel | Tjänst | Vad den styr |
|---|---|---|
| `SECRET_KEY` | mainai-backend | Signerar JWT-access-/refresh-tokens |
| `MAINAI_APP_PASSWORD` | mainai-backend | Lösenord för den begränsade RLS-databasrollen (se ovan) |

### Måste fyllas i manuellt i Render-dashboarden (`sync: false` i render.yaml)

| Variabel | Tjänst | Vad du sätter |
|---|---|---|
| `ADMIN_EMAIL` | mainai-backend | E-post för det automatiskt skapade admin-kontot (skapas bara om inga användare finns) |
| `ADMIN_PASSWORD` | mainai-backend | Starkt lösenord, inte `.env.example`s placeholder |
| `SMTP_HOST` | mainai-backend | **Obligatorisk i produktion** — backend vägrar nu starta (`ENVIRONMENT=production` utan `SMTP_HOST`, se `_check_smtp_configured` i `app/main.py`) om den saknas |
| `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_FROM_EMAIL` | mainai-backend | Enligt din SMTP-leverantör |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` / `DEEPSEEK_API_KEY` / `OPENROUTER_API_KEY` | mainai-backend | Fyll i minst en |

### Redan satta, synliga värden i `render.yaml`

| Variabel | Tjänst | Värde | Kommentar |
|---|---|---|---|
| `ENVIRONMENT` | mainai-backend | `production` | Utlöser SMTP-tvånget ovan |
| `QDRANT_URL` | mainai-backend | `http://mainai-qdrant:6333` | Privat nätverksadress |
| `FRONTEND_ORIGINS` | mainai-backend | `https://lifeai-1.onrender.com` | Mest defense-in-depth nu — se "CORS är nästan irrelevant nu" nedan |
| `PUBLIC_APP_URL` | mainai-backend | `https://lifeai-1.onrender.com` | Länkar i verifierings-/återställningsmail |
| `COOKIE_SECURE` / `COOKIE_SAMESITE` | mainai-backend | `true` / `none` | Oförändrat säkra defaults — se ovan för varför `none` fortfarande är rätt trots att cookien i praktiken är same-origin nu |
| `INTERNAL_API_URL` | lifeai-1 | `http://mainai-backend:8000` | Servern (inte webbläsaren) når backend härigenom — se proxy-avsnittet ovan |

**Ingen `NEXT_PUBLIC_API_URL`** sätts längre någonstans i `render.yaml` — det är en avsiktlig
utelämning, inte ett förbiseende (se `frontend/lib/api.ts`).

### CORS är nästan irrelevant nu

Eftersom `mainai-backend` inte har någon publik URL alls kan ingenting utanför Render — inte en
webbläsare, inte Vercel, ingenting — nå den direkt oavsett `FRONTEND_ORIGINS`. Proxyanropet från
`lifeai-1`s server är server-till-server (Node `fetch`) och skickar ingen `Origin`-header (det
är ett webbläsarkoncept), så CORS-mellanvaran i `app/main.py` triggas inte ens av den vägen.
Listan är kvar av dokumentations-/försvar-i-djupet-skäl, inte för att den faktiskt portvaktar
något just nu.

**Konsekvens att vara medveten om:** om den gamla Vercel-driftsättningen fortfarande är live och
skulle försöka anropa backend direkt (cross-origin, det gamla mönstret), fungerar det inte
längre — det finns ingen publik backend-URL att anropa. Säg till om Vercel ändå ska kunna nå
backend direkt (t.ex. under en övergångsperiod) — då behöver `mainai-backend` vara `type: web`
igen (publik) istället för `pserv`, vilket är en enkel ändring men en medveten avvägning värd
ett eget beslut, inte något jag ändrar underhand.

## Deploy sker bara via CI, aldrig av ett push i sig

`render.yaml` sätter `autoDeploy: false` på både `mainai-backend` och `lifeai-1`. Enda
utlösaren är jobbet `deploy-render` i `.github/workflows/ci.yml`, som körs efter — och bara om —
`all-checks-passed` blir grönt. Det anropar Render-tjänsternas **Deploy Hook**-URL:er via
`RENDER_BACKEND_DEPLOY_HOOK_URL` / `RENDER_FRONTEND_DEPLOY_HOOK_URL`, två GitHub Actions-secrets
som **inte finns än** — så länge de saknas är jobbet ett ofarligt no-op.

## Klick-steg i Render (ett steg i taget — invänta bekräftelse innan nästa)

**Redan verifierat i dashboarden** (av dig): tjänsten heter `LifeAI`, publik URL
`https://lifeai-1.onrender.com`, kopplad till `d1n095/LifeAI`, branch
`claude/det-kommer-mer-879lcm`, runtime Docker. Senaste deployen av `38c1d57` misslyckades med
`failed to read dockerfile: open Dockerfile: no such file or directory` — Render letar efter
`Dockerfile` i repo-roten, men den ligger i `frontend/Dockerfile`. Det bekräftar att tjänstens
**Root Directory** inte är satt till `frontend`.

Jag har fixat en separat, verklig bugg detta avslöjade: `render.yaml`s frontend-tjänst hette
`lifeai-1`, men den riktiga tjänsten heter `LifeAI` — Blueprint-adoption matchar på exakt
tjänstenamn, så det hade skapat en dubblett istället för att ta över `LifeAI`. Rättat och
pushat (repo-ändring, rör ingen extern tjänst) — se `render.yaml`s `services[].name: LifeAI`.

**Nästa steg — endast detta:**

1. Öppna tjänsten **`LifeAI`** i Render Dashboard → **Settings** → **Build & Deploy**.
2. Sätt **Root Directory** till `frontend`.

Det ska (baserat på Renders vanliga beteende — jag kan inte verifiera dashboarden själv
härifrån) göra att Render letar efter Dockerfilen relativt `frontend/` istället för repo-roten,
vilket löser exakt det felmeddelande du fick. **Spara inte än om du vill att jag bekräftar
Dockerfile Path-fältet först** — men om Render bara har ett Root Directory-fält och inget
separat Dockerfile Path-fält kvar att sätta, är det här hela fixen.

Efter att du sparat detta, säg till — nästa steg blir antingen att trigga om en deploy manuellt
för att verifiera att byggfelet är löst, eller (om du hellre vill gå Blueprint-vägen direkt)
**New → Blueprint** med samma repo/branch, som nu ska känna igen `LifeAI` som en existerande
tjänst istället för att skapa en dubblett.

## Verifiering som redan gjorts (lokalt, utan Docker — se commit-historiken)

Byggd mot en riktig lokal Postgres och Redis (inte mockad), med hela proxykedjan uppe: riktig
backend (`alembic upgrade head` + `ensure_app_role.py` körda på riktigt), riktigt
`next build`/`node .next/standalone/server.js`, och en riktig webbläsare (Playwright):

- Registrering och inloggning genom proxyn ger korrekta statuskoder och JSON-svar.
- `Set-Cookie` kommer fram som två separata cookies (`access_token`, `refresh_token`), inte
  hopslagna — och cookien är scopead till frontend-origin, inte backend-adressen.
- CSRF-skyddad mutation (utloggning) fungerar med token, avvisas korrekt (`403`) utan.
- `X-Forwarded-For` från klienten når fram till backendens access-logg oförändrad genom proxyn.
- En 404 på en obefintlig `/api/`-sökväg är backendens riktiga JSON-404, inte Next.js egen
  404-sida — bekräftar att proxyn fångar alla `/api/*`-anrop, inte bara de kända routrarna.
- Den nya `frontend/e2e/same-origin-proxy.spec.ts` (körs av CI-jobbet
  `same-origin-proxy-test`) kodifierar samma kontroller automatiskt, mot en riktig webbläsare.

## Verifiering efter en fullständig deploy (för senare, när vi är där)

1. `https://lifeai-1.onrender.com/api/health` i webbläsaren → `{"status":"ok"}` (går genom
   proxyn — det finns ingen annan väg att nå backend från utanför Render).
2. Öppna `https://lifeai-1.onrender.com/register`, försök skapa ett konto, kontrollera i
   DevTools → Network att `POST /api/auth/register` (samma origin som sidan, inget
   preflight-OPTIONS) ger `202`.
3. Kontrollera att ett verifieringsmail faktiskt kommer fram (bekräftar `SMTP_HOST` m.fl.).
4. DevTools → Application → Cookies: `access_token`/`refresh_token` ska stå under
   `lifeai-1.onrender.com`, inte något annat värdnamn.
