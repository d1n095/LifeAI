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

## 2. JWT lagras i `localStorage`, inte i en httpOnly-cookie

Se kommentaren i `frontend/lib/auth.ts`. Fungerar för 0.1 (ingen server-side
sessionsinfrastruktur finns), men gör frontend beroende av att aldrig ha en XSS-lucka —
en cookie-baserad lösning har inte samma svaghet. Uppgradera när backend får en riktig
sessionsmodell (Fas 1 i `docs/ROADMAP.md`).

## 3. Kostnadsdata i adminpanelen är uppskattad, inte fakturagrundande

`backend/app/providers/pricing.py` innehåller manuellt underhållna listpriser. De driftar
över tid om leverantörerna ändrar priser. Inte en säkerhetsbrist, men en driftsrisk om någon
förlitar sig på siffran för faktisk fakturering — dokumenterat här för synlighet, inte dolt i
kodkommentarer bara.
