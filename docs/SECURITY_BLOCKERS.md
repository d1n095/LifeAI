# Kända säkerhetsblockerare före produktion

Saker som medvetet INTE är lösta i MainAI 0.1 och som måste åtgärdas innan systemet
exponeras utanför en betrodd, intern miljö. Varje post har en tydlig åtgärd — inget här är
"glömt", det är avsiktligt uppskjutet med en anledning.

## 1. [BLOCKERANDE] Next.js-versionen har kända säkerhetsbrister

`frontend/package.json` låser `next@14.2.15`. `npm audit` (kört 2026-07-17) flaggar den som
**kritisk** — bland annat DoS via Server Actions, informationsläckage i dev-servern,
cache-poisoning, SSRF via middleware-redirects och flera XSS-vägar i App Router. Fullständig
lista: kör `npm audit` i `frontend/`.

**Varför det inte är fixat nu:** `npm audit fix --force` vill installera `next@16.2.10` —
en major-uppgradering över två versionssteg (14 → 15 → 16). Det är inte en säker
"blind"-uppgradering:

- App Router-beteenden, `next.config.mjs`-alternativ (t.ex. `output`-hanteringen som just
  fixades för Vercel, se `docs/STATUS.md`) och middleware-API:er har ändrats mellan
  major-versionerna.
- React-versionskravet ändras (Next 15+ kräver React 19).
- `output: "standalone"`-hanteringen i vårt Dockerfile och Vercel-flödet måste
  regressionstestas efter uppgraderingen, inte bara antas fungera likadant.

**Vad som krävs innan detta görs:**
1. Läs Next.js migrationsguiderna 14→15 och 15→16 i sin helhet.
2. Uppgradera i en egen branch, inte som en delrad i en funktionsleverans.
3. Kör hela E2E-sviten (se `docs/STATUS.md`/commit-historik för hur den sattes upp: riktig
   Postgres + riktig Chromium-browser via Playwright) mot den uppgraderade koden.
4. Verifiera explicit: Docker-bygget (`output: standalone`) OCH Vercel-bygget
   (`VERCEL`-villkoret i `next.config.mjs`) separat — de har olika kodvägar och båda måste
   verifieras, inte bara en av dem.
5. Kör `npm audit` igen efteråt och bekräfta att den kritiska varningen är borta.

**Rekommenderad tidpunkt:** egen, dedikerad arbetsinsats innan produktionsdrift — inte
ihopblandad med funktionsarbete, så att en eventuell regression är lätt att isolera.

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
