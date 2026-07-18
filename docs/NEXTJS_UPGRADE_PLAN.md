# Next.js-säkerhetsuppgradering — kompatibilitetsanalys och migrationsplan

Skriven innan någon kod ändrades. Mål: gå från `next@14.2.15` (kritiska kända sårbarheter,
se `docs/SECURITY_BLOCKERS.md` post 1) till senaste stabila, säkra version.

## Målversion — verifierad mot officiell källa, inte antagen

- **Verifieringsmetod:** `npm view next dist-tags` mot npmjs.org (auktoritativ källa — samma
  register `npm install` faktiskt hämtar från), kompletterat med Next.js egna blogg-/
  supportsidor och GitHub Security Advisories för att bekräfta att versionen är säkerhetspatchad.
- **Resultat:** `latest`-taggen pekar på **`16.2.10`**. En underhållen 15.x-linje finns också
  (`backport`-taggen, `15.5.20`), men inget i vårt projekt (inga andra beroenden) låser oss
  till 15.x — vi går direkt till senaste major enligt uppdragets instruktion "senaste stabila
  säkra version".
- **Säkerhetsstatus:** En koordinerad säkerhetsrelease (13 CVE/GHSA-poster: middleware/proxy-
  bypass, XSS, SSRF, cache poisoning, DoS) gick ut i maj 2026 och täcker 15.x/16.x — **inte**
  14.x, som Vercel uttryckligen inte planerar att patcha längre. `16.2.10` ligger efter de
  patchversioner (16.2.5/16.2.6) som första gången åtgärdade den omgången, så samtliga kända
  publicerade sårbarheter är täckta.
- **Observera:** Nästa koordinerade säkerhetsrelease är annonserad men ännu inte publicerad vid
  tidpunkten för denna uppgradering. Det är inte skäl att vänta — vi uppgraderar till senaste
  *tillgängliga* säkra version nu, och `npm audit` körs igen som en vanlig rutin vid nästa
  release (inte unikt för detta projekt).

## React- och Node-krav (peer dependencies, verifierat via `npm view next@16.2.10 peerDependencies`)

| Krav | Nuvarande | Krävs av 16.2.10 | Åtgärd |
|---|---|---|---|
| React / React DOM | 18.3.1 | `^19.0.0` | Uppgradera till React 19 |
| Node.js | Docker: `node:20-slim` (aktuell patch, 22 lokalt) | ≥ 20.9 | Redan uppfyllt, ingen ändring krävs |
| TypeScript | 5.5.3 | ≥ 5.1 | Redan uppfyllt |

## Kodbasgenomgång — vad påverkas faktiskt (verifierat med grep, inte antaget)

| Breaking change (14→15→16) | Berör oss? | Belägg |
|---|---|---|
| `cookies()`/`headers()`/`draftMode()`/`params`/`searchParams` blir asynkrona | **Nej** | `grep` efter `cookies(`, `headers(`, `draftMode(` i `app/`, `components/`, `lib/` gav 0 träffar. Inga dynamiska routesegment (`[id]` etc.) finns. |
| `fetch`/Route Handlers cachas inte längre som standard | **Nej** | Inga `app/**/route.ts`-filer finns. All datahämtning sker i `"use client"`-komponenter via `fetch()` i webbläsaren mot vårt separata FastAPI-backend — aldrig Next.js Data Cache. |
| `middleware.ts` → `proxy.ts` | **Nej** | Ingen `middleware.ts` finns i projektet. |
| `next lint` borttaget | **Ja** | `package.json` har `"lint": "next lint"`. Måste bytas till en riktig ESLint-uppsättning. |
| AMP borttaget | **Nej** | AMP används inte. |
| React 19: legacy Context API, sträng-refs, `ReactDOM.render` borttaget | **Nej** | `grep` efter `defaultProps`, `ReactDOM.render`, `useFormState`, `forwardRef` gav 0 träffar. Ingen komponent använder Context API. |
| `output: "standalone"`-läget | **Nej förändring väntad** | Fortsatt dokumenterat och stödd funktion i v16 — vår villkorade `next.config.mjs` (`docs/STATUS.md`/commit `0269f49`) förväntas fungera oförändrat, men verifieras ändå explicit i testfasen nedan eftersom det är precis den mekanism som redan orsakat ett produktionsfel en gång. |

## Plan

1. Uppgradera `next` → `16.2.10`, `react`/`react-dom` → `^19.0.0`, matchande `@types/react`/
   `@types/react-dom`.
2. Ersätt `"lint": "next lint"` med en fristående ESLint-konfiguration (`eslint-config-next`,
   som fortsatt underhålls och stödjer Next 16, körd via `eslint` direkt).
3. Kör `npm audit` — ska inte längre visa den kritiska Next.js-varningen.
4. Kör en fullständig ren produktionsbuild i båda lägena (Docker/standalone och Vercel), samma
   sätt som verifierades i föregående milstolpe.
5. Kör hela backend-, RLS- och säkerhetstestsviten (ingen backend-kod ändras av detta arbete,
   men den ska fortsätta vara grön — regression någon annanstans vore ett tecken på att något
   gått fel i frontend/backend-kontraktet).
6. Kör alla 19 Playwright E2E-kontroller mot den uppgraderade frontend-koden.
7. Manuell kontroll av `/`, `/login`, `/chat`, `/admin`.
8. Kontrollera att inga hemligheter läcker i byggloggar eller till klientbundlen (samma princip
   som alltid, men extra viktigt att verifiera igen efter ett stort beroendebyte).
9. Uppdatera `docs/SECURITY_BLOCKERS.md` (ta bort/uppdatera post 1), committa, pusha.

## Beslutspunkt att stanna vid, om den uppstår

Om något av grep-fynden ovan visar sig vara ofullständigt under den faktiska uppgraderingen
(t.ex. ett beteende som bara syns vid körning, inte vid statisk sökning) och kräver en
avvägning mellan att skriva om appens datamodell kontra att stanna på en äldre patchversion,
stannar jag och beskriver alternativen istället för att gissa. Ingen sådan situation är känd
just nu utifrån analysen ovan.
