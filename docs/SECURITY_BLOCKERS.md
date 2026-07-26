# Kända säkerhetsblockerare före produktion

Saker som medvetet INTE är lösta i MainAI 0.1 och som måste åtgärdas innan systemet
exponeras utanför en betrodd, intern miljö. Varje post har en tydlig åtgärd — inget här är
"glömt", det är avsiktligt uppskjutet med en anledning.

## 1. [LÖST 2026-07-18] Next.js-versionen hade kända säkerhetsbrister

**Status: åtgärdat.** `frontend/package.json` låste tidigare `next@14.2.15`, som `npm audit`
flaggade som kritisk (DoS via Server Actions, informationsläckage i dev-servern,
cache-poisoning, SSRF via middleware-redirects, flera XSS-vägar i App Router — 14.x får inga
fler säkerhetspatchar från Vercel).

Uppgraderat kontrollerat till **`next@16.2.10`** (senaste `latest`-tagg på npm, verifierad
säkerhetspatchad — se `docs/NEXTJS_UPGRADE_PLAN.md` för fullständig kompatibilitetsanalys och
källverifiering) tillsammans med **React 19.2.7**, matchande `@types/react`/`@types/react-dom`,
samt `postcss` uppgraderad till 8.5.19 (löste en separat, oberoende moderate-sårbarhet i
PostCS:s CSS-stringifier) och tvingad projektbrett via `overrides` så att inte ens Next.js
egen internt buntade postcss-kopia förblev sårbar.

`npm audit` i `frontend/` visar nu **0 sårbarheter**.

**Kodpåverkan var minimal** (verifierat med grep innan ändring, se migrationsplanen) — inga
`cookies()`/`headers()`/dynamiska routesegment, ingen `middleware.ts`, inga Route Handlers,
ingen AMP, ingen legacy React Context/sträng-refs. Enda faktiska kodändringarna:
- `next lint` togs bort i v16 → ersatt med en fristående `eslint.config.mjs`
  (`eslint-config-next` flat config).
- En ny, striktare `react-hooks/set-state-in-effect`-lint-regel flaggade fem ställen med det
  vanliga "hämta data vid mount"-mönstret. Två av dem (`lib/useVoice.ts`) skrevs om till
  `useSyncExternalStore` (mer korrekt primitiv för statiska webbläsarflaggor). Tre är kvar som
  det React-dokumenterade mönstret med en riktad, motiverad `eslint-disable-next-line`
  (inte ett blockdolt undantag) eftersom mönstret redan är verifierat säkert via E2E-sviten.
- `eslint`-versionen pinnades till `9.39.5`, inte senaste `10.x` — en genuin verktygsinkompatibilitet
  (`eslint-plugin-react` kraschar mot ESLint 10:s interna API), inte en säkerhetsnedgradering:
  ESLint är ett dev-only lint-verktyg, ingår aldrig i produktionsbygget eller klientbundlen.

**Verifierat innan commit:** `npm audit` (0 sårbarheter), `tsc --noEmit` (rent), `eslint .`
(rent), full backend-py_compile (oförändrad, ingen regression), manuellt RLS-isoleringstest
mellan två användare (oförändrat korrekt), alla 19 Playwright E2E-kontroller gröna, Docker-läge
(`.next/standalone` skapas) och Vercel-läge (`.next/standalone` skapas INTE) byggda och
verifierade separat, `/`, `/login`, `/chat`, `/admin` manuellt kontrollerade efter bygge, och
att endast `NEXT_PUBLIC_API_URL` (aldrig en hemlighet) förekommer i klientbundlen.

**Kvarvarande risk:** en ny koordinerad Next.js-säkerhetsrelease var annonserad men inte
publicerad vid uppgraderingstillfället. Ingen känd öppen sårbarhet just nu — kör `npm audit`
igen som rutin när den releasen landar, precis som för vilket beroende som helst.

## 2. [LÖST 2026-07-18] JWT lagrades i `localStorage`, inte i en httpOnly-cookie

**Status: åtgärdat.** Sessionen låg tidigare i en JWT i `localStorage`, läsbar av vilken
JavaScript som helst på sidan — en enda XSS-lucka någonstans i appen (eller ett komprometterat
npm-paket) hade räckt för att stjäla en giltig session permanent. Se `docs/AUTH_THREAT_MODEL.md`
för fullständig hotmodell och designbeslut, skriven innan implementation påbörjades.

**Ny arkitektur — två separata cookies, inget token i JS överhuvudtaget:**
- `access_token`: kortlivad JWT (15 min), `HttpOnly`, `Secure`, `SameSite=None`, path `/`.
  Bär en `jti`-claim för omedelbar återkallning (se nedan).
- `refresh_token`: högentropi opak token (`secrets.token_urlsafe(48)`), samma
  cookie-inställningar, men scoped till path `/api/auth` — kan aldrig läcka till
  applikations-API:erna ens om de skulle vara XSS-sårbara. Endast SHA-256-hash lagras i
  databasen, aldrig klartext.
- `SameSite=None` + `Secure` krävs eftersom frontend och backend körs på olika origins
  (även lokalt: `127.0.0.1:3020` vs `localhost:8010` är olika origins för webbläsaren);
  `Secure` fungerar utan HTTPS på `localhost`/`127.0.0.1` eftersom webbläsare behandlar
  dem som "secure contexts" per spec.

**Refresh-token-rotation med replay-skydd:** varje `/api/auth/refresh` gör den gamla
raden `revoked_at`-markerad och skapar en ny i samma `family_id`-kedja. Om en redan
återkallad refresh-token presenteras igen (stulen och uppspelad, eller en förlorad
race) tolkas det som ett kompromissignal: **hela familjen** återkallas — alla
refresh-tokens i kedjan OCH deras respektive access-tokens (se `RevokedAccessToken`
nedan) — vilket tvingar fullständig omdinloggning. Verifierat med ett dedikerat test som
spelar upp en roterad-bort token och bekräftar att även den token som ersatte den
(annars fortfarande giltig i isolation) därefter också är död.

**CSRF-skydd — inte klassisk dubbel-cookie (fungerar inte cross-origin):** ett vanligt
double-submit-cookie-mönster förutsätter att frontendens JS kan läsa CSRF-cookien via
`document.cookie` och eka tillbaka den som header. Verifierat i praktiken (Playwright)
att detta INTE fungerar cross-origin: en cookie satt av backendens origin är aldrig
läsbar från frontendens origin via `document.cookie`, oavsett `HttpOnly`-flaggan — det är
same-origin-policy, inget vi kan konfigurera bort. Lösning: CSRF-värdet skickas i stället
en gång i JSON-svarskroppen från `/login`, `/refresh` och `/me` (läsbart cross-origin
tack vare det explicita CORS-allowlistet), hålls i en ren in-memory JS-variabel på
frontend (`lib/auth.ts`, nollställs vid varje sidladdning) och verifieras server-side mot
ett databaslagrat värde (`app/deps.py` för vanliga state-changing anrop, `app/routers/auth.py`
för `/refresh`/`/logout` som autentiserar via refresh-cookien innan access-token ens finns).
Verifierat med en äkta cross-origin-attack-simulering (separat portad "attacker-origin",
inte bara en annan path) som skickar en riktig `no-cors`-formulärliknande begäran med de
riktiga sessionscookies bifogade av webbläsaren — begäran skickas (webbläsaren blockerar
inte avsändandet, bara attackerarens möjlighet att läsa svaret), men servern avvisar den
och det förfalskade objektet skapas aldrig, eftersom attacker-sidan aldrig kunnat få tag i
CSRF-värdet.

**Fullständig utloggning, inte bara refresh-token-återkallning:** en stateless JWT går
normalt inte att ogiltigförklara innan den naturligt går ut. Löst med en liten
återkallningslista (`RevokedAccessToken`, nyckel `jti`, självstädande via `expires_at`) —
både explicit `/logout` och family-wide reuse-detection lägger till den aktuella
access-token-`jti`:n där, och `get_current_user` kontrollerar listan på varje anrop.
Verifierat: en access-token som var giltig strax före utloggning ger omedelbart 401
("Sessionen har återkallats") direkt efter, i stället för att fortsätta fungera i upp
till 15 minuter till.

**Session fixation:** varje lyckad inloggning skapar en helt ny `family_id` — ingen
existerande sessionskedja återanvänds eller förlängs, oavsett vad klienten skickade in.

**Strikt CORS:** `allow_origins` är en explicit lista (`FRONTEND_ORIGINS`, kommaseparerad),
`allow_credentials=True`, `allow_headers` begränsad till `Content-Type` och `X-CSRF-Token`.

**Rate limiting och audit-logg:** `/login` 10/min, `/refresh` 30/min, `/logout` 30/min
(per IP innan autentisering). `login_success`, `login_failed`, `refresh_success`,
`refresh_failed`, `refresh_token_reuse_detected` och `logout` skrivs alla till
`audit_log`.

**Inga tokens i `localStorage`, `sessionStorage`, URL:er eller loggar:** `frontend/lib/auth.ts`
lagrar numera bara CSRF-värdet, i minnet, aldrig i persistent webbläsarlagring.
Session-identiteten finns enbart i `HttpOnly`-cookies som frontendens JS aldrig kan läsa.

**Verifierat innan commit:** ett nytt 20-kontrollers säkerhetstestsvit (Playwright, mot
den riktiga stacken, inga mockar) som bekräftar: `access_token`/`refresh_token` är
`HttpOnly`+`Secure`, `document.cookie` exponerar dem varken på backendens eller
frontendens egna origin, en äkta cross-origin CSRF-attack avvisas och skapar aldrig sitt
mål-objekt, refresh-rotation ger en ny token och ogiltigförklarar den gamla, uppspelning
av en redan roterad token återkallar hela familjen (båda generationerna av tokens), och
utloggning dödar access-token omedelbart. Samtliga 19 befintliga Playwright E2E-kontroller
gröna. `tsc --noEmit` och `eslint .` rena på frontend. `py_compile` rent på backend.
Produktionsbygge kört och verifierat i båda lägena: Docker-läge (`.next/standalone`
skapas) och Vercel-läge (skapas inte), `/`, `/login`, `/chat`, `/admin` manuellt
kontrollerade efter bygge.

**Kvarvarande risk:** `COOKIE_DOMAIN` är `None` som standard (host-only cookies) —
måste sättas explicit i produktion om frontend och backend delar en överordnad domän
(t.ex. `.exempel.se`) för att cookies ska nå rätt subdomäner; fel värde här kan antingen
läcka cookien till fler subdomäner än avsett eller göra sessionen obrukbar, så det ska
sättas medvetet per miljö, inte lämnas som standard i produktion.

(`refresh_token`-, `email_verification_token`- och `password_reset_token`-tabellernas
tidigare obegränsade tillväxt är åtgärdad — se "Tillägg 2026-07-18: produktionshärdning"
nedan.)

### Tillägg 2026-07-18: fullständigt, säkert kontoflöde

**Status: åtgärdat.** Konton kunde tidigare bara skapas manuellt (bootstrap-admin via
miljövariabler). Byggt ovanpå cookie-sessionen ovan, i samma säkerhetsmilstolpe: självbetjänad
registrering, e-postverifiering innan full åtkomst, lösenordsåterställning, utloggning från
alla enheter, och kontoexport/-radering enligt dataskyddskrav. Fullständig hotmodell och
designbeslut i `docs/AUTH_THREAT_MODEL.md` (avsnittet "Tillägg 2026-07-18").

**Sammanfattning:**
- **Argon2id** (OWASP-rekommenderad, minneshård) ersätter bcrypt för lösenordshashning.
- **Lösenordspolicy**: minst 12 tecken, lokal denylist för vanliga svaga lösenord, får inte
  innehålla e-postadressens lokal-del (`backend/app/password_policy.py`).
- **E-post normaliseras** (NFKC + lowercase) innan uniktetskontroll och lagring
  (`backend/app/email_utils.py`) — förhindrar dubbelregistrering via Unicode-varianter.
- **Registrering** (`POST /api/auth/register`): honeypot-fält mot enkel botregistrering,
  rate limit 5/min/IP, alltid neutralt svar oavsett om adressen redan finns.
- **E-postverifiering**: kortlivad (24h), engångsanvändbar token (256-bitars slump, endast
  SHA-256-hash lagras). Inloggning är helt blockerad — inget session utfärdas — förrän kontot
  är verifierat.
- **Lösenordsåterställning**: kortlivad (1h), engångsanvändbar token. Lyckad återställning
  återkallar **alla** aktiva sessioner för kontot omedelbart (inte bara den som gjorde
  återställningen) — se `revoke_all_sessions_for_user` i `backend/app/token_revocation.py`.
- **Neutrala svar**: `/register`, `/forgot-password` och `/resend-verification` svarar
  identiskt oavsett om e-postadressen finns, redan är verifierad, eller inte — ingen
  kontoräkning (email enumeration) möjlig via dessa endpoints.
- **Rate limiting + brute-force-skydd**: nya, striktare gränser per endpoint
  (register/forgot-password/verify-email/reset-password), plus en separat räknare **per
  e-postadress** (inte bara per IP) som blockerar inloggning efter 10 misslyckade försök
  inom 15 minuter mot samma konto — skyddar mot distribuerad brute force över många IP:n.
- **Logga ut från alla enheter** (`POST /api/auth/logout-all`) och **lösenordsbyte** delar
  samma massåterkallningsmekanism: en tidsstämpel (`User.sessions_valid_after`) plus
  återkallning av alla `refresh_tokens`-rader, istället för att räkna upp enskilda tokens.
- **Kontoexport** (`GET /api/account/export`): JSON med profil, egna konversationer och
  egen audit-logg. Delat företagsinnehåll (dokument/projekt/uppgifter) ingår medvetet inte —
  det är inte personuppgifter tillhörande individen, se `docs/MAINAI_0.1_PLAN.md`.
- **Kontoradering** (`DELETE /api/account`, kräver lösenordsbekräftelse): permanent radering
  av konto, konversationer och sessionsdata; delat företagsinnehåll frikopplas
  (`created_by`/`uploaded_by` → NULL) istället för att raderas; audit-loggen behålls men
  aktörsidentiteten skrubbas.

**Verifierat innan commit:** 34 nya Playwright-kontroller mot den riktiga stacken
(registrering, dubblerad e-post, honeypot, utgången/återanvänd verifieringstoken,
inloggning blockerad före verifiering, svagt lösenord avvisat, utgången/återanvänd
återställningstoken, sessionsåterkallelse efter lösenordsbyte, utloggning från flera
enheter samtidigt, användarisolering mellan två nyregistrerade konton, kontoradering med
fel/rätt lösenord, ingen inloggning möjlig efter radering) plus 2 nya kontroller för
rate limiting/brute-force. Alla tidigare 19 E2E- och 20 säkerhetskontroller fortsatt
gröna (75 kontroller totalt). `tsc --noEmit` och `eslint .` rena, `py_compile` rent.
Produktionsbygge verifierat i båda lägena igen efter dessa ändringar.

**Kvarvarande risk:** inget CAPTCHA-beroende till tredje part (endast honeypot + rate
limiting mot automatiserad registrering) — tillräckligt för nuvarande skala, men svagare
mot en målmedveten botoperatör; lägg till t.ex. hCaptcha/Turnstile om missbruk faktiskt
observeras. SMTP måste konfigureras explicit i produktion (`SMTP_HOST` m.fl.) — annars
skickas inget verifierings-/återställningsmejl på riktigt (se "Tillägg 2026-07-18:
produktionshärdning" nedan för hur detta numera hanteras säkert istället för att bara loggas).

### Tillägg 2026-07-18: produktionshärdning

**Status: åtgärdat.** Kontoflödet ovan var byggt och testat, men inte redo för produktion
utan detta: permanent testkatalog, obligatorisk CI, riktig migrationshantering, städjobb,
distribuerad rate limiting, och att inga rå tokens hamnar i loggar. Fullständig
drift-/incidentdokumentation i `docs/OPERATIONS.md`.

- **Permanent testkatalog**: `backend/tests/` (pytest — 74 tester i tre kataloger:
  `backend/`, `security/`, `account/`, matchande CI-jobben nedan) och `frontend/e2e/`
  (Playwright — samma täckning som de tidigare 75 ad-hoc-kontrollerna, nu strukturerade
  som namngivna, körbara testfiler). Ingen kritisk testlogik finns kvar bara i en
  scratchpad-katalog.
- **GitHub Actions CI** (`.github/workflows/ci.yml`): `lint-and-typecheck`, `npm-audit`,
  `frontend-build` (Docker- och Vercel-läge), `backend-tests`, `rls-security-tests`,
  `account-rate-limit-tests`, `migration-check`, `e2e-tests`, samlat i en obligatorisk
  `all-checks-passed`-check. Kräver att branch protection slås på i repo-inställningarna
  för att faktiskt blockera merge — se `docs/OPERATIONS.md` (kodändringar kan inte göra
  detta åt dig, det är en repo-administratörsåtgärd).
- **Alembic-migrationer** (`backend/alembic/`) ersätter `Base.metadata.create_all` helt i
  produktionsvägen — tre migrationer (baseline, cookie-session, kontoflöde) verifierade att
  ge **exakt** samma schema som det gamla `create_all`-beteendet (diffat mot en
  `pg_dump`-referens), plus en testad uppgraderingsväg med bevarad data och en testad
  downgrade-till-base-och-upp-igen (körs som ett obligatoriskt CI-jobb på varje push).
  Befintliga användare grandfathras korrekt in som verifierade vid uppgradering, utan att
  bli utloggade av misstag (se migrationsfilens kommentarer om varför tidsstämpeln
  bakdateras till `created_at`, inte `now()`).
- **Idempotent städjobb** (`app/cleanup.py`): rensar utgångna/återkallade/använda
  `refresh_tokens`, `revoked_access_tokens`, `email_verification_tokens` och
  `password_reset_tokens` efter en dokumenterad retentionsperiod (`TOKEN_CLEANUP_RETENTION_DAYS`,
  standard 30 dagar — behåller metadata så länge för säkerhetsrevision, exempelvis att
  utreda ett token-återanvändningsincident i efterhand). Postgres advisory lock gör att
  flera backend-repliker på samma schema aldrig dubbelkör eller kolliderar. Kör automatiskt
  (`ENABLE_SCHEDULED_CLEANUP`) och manuellt (`POST /api/admin/cleanup`).
- **Distribuerad rate limiting**: bekräftat processlokal (in-memory) tidigare, nu
  Redis-backad (`REDIS_URL`) så skyddet gäller över flera backend-instanser och överlever
  omstart. Backend vägrar starta om `REDIS_URL` är satt men Redis inte går att nå — fail
  closed, inte en tyst nedgradering till svagare skydd.
- **Inga rå tokens i loggar**: `app/email.py`:s tidigare dev-lägesfallback loggade hela
  mejlkroppen (inklusive den rå engångstoken:en) till applikationsloggen om SMTP inte var
  konfigurerat — åtgärdat. Loggar numera bara att ett mejl inte kunde skickas, utan
  innehåll. Lokal utveckling kan istället peka SMTP mot en riktig mailfångare
  (`docker compose --profile dev-mail up mailhog`) eller, enbart lokalt, aktivera en
  explicit opt-in-fil-utkorg (`DEV_MAIL_OUTBOX_DIR`, aldrig i produktion).
- **Transaktionell kontoradering**: `DELETE /api/account` omgärdas nu av explicit
  try/except/rollback — ett fel mitt i raderingssekvensen (efter att vissa delar redan
  köats mot databas-sessionen, innan den slutgiltiga commit:en) rullar hela transaktionen
  tillbaka. Testat genom att tvinga fram ett fel mitt i sekvensen och verifiera att kontot,
  konversationerna och sessionerna finns kvar helt orörda efteråt (inget halvraderat
  tillstånd), samt att kontot fortfarande fungerar normalt direkt efteråt.

**Hittade och fixade två skarpa buggar under testarbetet** (inte bara nya kontroller —
faktiska produktionsbuggar som fanns redan i föregående milstolpe):
1. **JWT `iat`-precision vs. `sessions_valid_after`**: JWT:s `iat`-klaim har
  sekundprecision (RFC 7519), men `User.sessions_valid_after` sattes med
  mikrosekundprecision. En session utfärdad inom samma väggklocksekund som en
  massåterkallning (lösenordsåterställning, "logga ut från alla enheter", eller till och
  med kontots eget skapande) kunde av misstag räknas som "innan" återkallningen och nekas,
  eller i värsta fall — beroende på jämförelseoperator — tvärtom slippa igenom en
  återkallning den egentligen skulle träffats av. Löst med `utcnow_seconds()`/
  `utcnow_seconds_baseline()` (sekundtrunkerat, baslinjevärden bakdaterade en sekund) i
  `app/security.py`, och `<=` istället för `<` i jämförelsen i `app/deps.py` (fail closed
  vid en oavgjord sekund, inte fail open).
2. **Advisory lock kunde läcka mellan pooled databasanslutningar**: städjobbet tog låset,
  gjorde databasändringar, commit:ade (vilket kan lämna tillbaka SQLAlchemy-anslutningen
  till poolen), och försökte sedan släppa låset — som då riskerade hamna på en **annan**
  fysisk anslutning än den som tog det, vilket lämnade låset hängande tills just den
  anslutningen råkade återanvändas. Löst genom att låsets hela livscykel (ta, arbeta,
  släpp) nu körs på en enda, uttryckligen fasthållen anslutning, oberoende av
  ORM-sessionens egna commit-cykler.

**Verifierat**: 74 pytest-tester + Playwright E2E-svit (samma täckning som de tidigare 75
kontrollerna, nu i namngivna testfiler) gröna, upprepade körningar utan flakiness.
Migrationskedjan verifierad: ren installation matchar exakt det gamla schemat, uppgradering
från tidigare schema med bevarad testdata, fullständig downgrade-till-base-och-upp-igen.
`npm audit` 0 sårbarheter (`--audit-level=high`), `tsc`/`eslint` rena.

**Kvarvarande risk:** ingen admin-driven "tvinga utloggning av en annan användare"-funktion
finns än (bara användaren själv, via inloggning eller lösenordsåterställning, kan trigga en
massåterkallning av sina egna sessioner) — se `docs/OPERATIONS.md`s incidentavsnitt. CAPTCHA
mot riktade botattacker fortfarande inte implementerat (se ovan, oförändrat). Branch
protection för CI måste aktiveras manuellt i GitHub-inställningarna — se
`docs/OPERATIONS.md`.

## 3. `brace-expansion` (GHSA-mh99-v99m-4gvg) — inget kompatibelt fix ännu

**Status: dokumenterad, avsiktligt allowlistad undantag i CI, inte löst.** `npm audit`
flaggar 9 höga fynd som alla härrör från EN advisory: `brace-expansion` <=5.0.7 (DoS via
obegränsad expansion → OOM-krasch), nådd transitivt via `eslint`/`minimatch`s hela
dev-verktygskedja (`@eslint/config-array`, `@eslint/eslintrc`, `eslint-plugin-import`,
`eslint-plugin-jsx-a11y`, `eslint-plugin-react`, `eslint-config-next`).

**Utrett och avvisat, i den ordningen:**
1. **`overrides: { "brace-expansion": "5.0.8" }` (den patchade versionen) projektbrett** —
   krashar `eslint`: `minimatch@3.1.5` (den version `eslint@9.39.5`s hela kedja fortfarande
   använder) anropar `brace-expansion` som en CommonJS-funktion; `5.0.8` är en helt omskriven
   ESM/CJS-paket med annan exportform (`TypeError: expand is not a function`).
2. **Uppgradera `eslint` till `10.8.0`** (npms egen `npm audit fix --force`-rekommendation)
   — löser `brace-expansion`-instansen i `eslint`s EGEN trädgren (den använder därefter
   `minimatch@10.2.5` → `brace-expansion@5.0.8`, verifierat säker), men krashar `eslint-config-next`s
   buntade `eslint-plugin-react@7.37.5`: `TypeError: contextOrFilename.getFilename is not a
   function` — ESLint 10 tog bort `context.getFilename()`. Detta är samma genuina
   verktygsinkompatibilitet som redan dokumenterades i punkt 1 ovan (2026-07-18, då `eslint`
   medvetet pinnades till 9.39.5 av exakt detta skäl) — inte nytt, bara samma vägg mött igen
   från ett annat håll.
3. **Ingen patchad `1.x`/`2.x`/`3.x`/`4.x`-release av `brace-expansion` finns** — projektets
   maintainer fixade buggen enbart i `5.0.8`; `eslint-plugin-import`/`jsx-a11y`/`react`s
   senaste publicerade versioner (2.32.0 / 6.10.2 / 7.37.5) pinnar fortfarande
   `minimatch: ^3.1.2` → `brace-expansion@^1.1.7`, oförändrat i väntan på att de själva
   uppdaterar. `eslint-config-next` har ingen nyare release än `16.2.11` (redan senaste,
   se PR #9) som drar in uppdaterade versioner av dessa.

**Varför detta är en låg verklig risk just nu:** `brace-expansion` nås här uteslutande via
`eslint`s egna dev-beroenden — ett lint-verktyg som körs på betrodd, lokal källkod
(`npm run lint`/CI), aldrig i produktionsbygget eller klientbundeln, och aldrig på
externt/obetrott indata. En DoS i den processen kan i värsta fall krascha en CI-körning,
inte produktionstjänsten.

**Hantering:** `frontend/scripts/check-npm-audit.js` — en liten, daterad, ID-specifik
allowlist (endast GHSA-mh99-v99m-4gvg/advisory-id `1124334`) som körs istället för
`npm audit --audit-level=high` direkt i `npm-audit`-jobbet i `.github/workflows/ci.yml`.
Den blockerar fortfarande CI på VILKET SOM HELST annat högt/kritiskt fynd — det här är inte
en generell avstängning av `npm audit`, bara ett smalt undantag för denna redan utredda,
fix-lösa post. **Ta bort raden i allowlistan** så fort `eslint-config-next` (eller
`eslint-plugin-react`/`import`/`jsx-a11y` var för sig) publicerar en release kompatibel med
`eslint@10.x`, eller `brace-expansion` får en patchad `1.x`-release.

## 4. Kostnadsdata i adminpanelen är uppskattad, inte fakturagrundande

`backend/app/providers/pricing.py` innehåller manuellt underhållna listpriser. De driftar
över tid om leverantörerna ändrar priser. Inte en säkerhetsbrist, men en driftsrisk om någon
förlitar sig på siffran för faktisk fakturering — dokumenterat här för synlighet, inte dolt i
kodkommentarer bara.
