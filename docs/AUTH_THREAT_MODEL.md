# Hotmodell och designbeslut — cookie-baserad session (ersätter JWT i localStorage)

Skriven innan implementation. Ersätter `docs/SECURITY_BLOCKERS.md` post 2.

## Kontext som styr designen

Frontend (Next.js, t.ex. Vercel) och backend (FastAPI) körs som **separata tjänster på
separata origins** (olika domän och/eller port). Det är inte en förenkling vi kan anta bort —
lösningen måste fungera cross-origin, inte bara i en lokal same-origin-uppställning. Detta
avgör cookie-strategin mer än något annat: en cross-site cookie kräver `SameSite=None` +
`Secure`, vilket i sin tur betyder att `SameSite` **inte** ger oss något CSRF-skydd på köpet
(till skillnad från `Lax`/`Strict`) — ett explicit CSRF-skydd är därför obligatoriskt, inte
valfritt härdning.

## Tillgångar

- Användarens session/identitet (åtkomst till egna konversationer, RLS-skyddade).
- Adminbehörighet (providerbyte, användningsdata för hela företaget).
- Företagets kunskapsbibliotek och dokument (delad data, se `docs/MAINAI_0.1_PLAN.md`).

## Hotaktörer och attacker

| Hot | Beskrivning | Skydd i denna design |
|---|---|---|
| **XSS-attackerare** | Injicerad skript i frontend försöker läsa sessionstoken för att imitera användaren. | Access- och refresh-token ligger i `HttpOnly`-cookies — JavaScript kan aldrig läsa dem, oavsett XSS. Endast CSRF-värdet (i sig meningslöst utan de HttpOnly-cookies) är JS-läsbart. |
| **CSRF-attackerare** | Extern sajt får offrets webbläsare att skicka en autentiserad state-changing request mot vårt API utan offrets vetskap. | Double-submit CSRF-token: ett separat, oförutsägbart värde i en icke-HttpOnly-cookie som måste upprepas i en `X-CSRF-Token`-header. En extern sajt kan inte läsa vår cookie (Same-Origin Policy) och kan därför inte konstruera en giltig header, även om webbläsaren automatiskt bifogar autentiseringscookien. |
| **Nätverksattackerare (MITM)** | Avlyssnar trafik för att stjäla token. | `Secure`-flaggan tvingar HTTPS i produktion (webbläsare skickar aldrig `Secure`-cookies över klartext HTTP, förutom det särskilda undantaget för `localhost`/`127.0.0.1` som webbläsare behandlar som en "secure context" — dokumenterad W3C/webbläsarstandard, inte en egen genväg vi hittat på). |
| **Token-stöld + återuppspelning** | En refresh-token läcker (t.ex. loggfil, komprometterad enhet) och återanvänds efter att den legitima klienten redan roterat vidare. | Refresh-token-rotation: varje refresh-anrop ogiltigförklarar den använda token och utfärdar en ny. Om en redan återkallad token presenteras igen tolkas det som ett stöldsignal — **hela token-familjen** (alla token i samma rotationskedja) återkallas omedelbart, vilket tvingar total omlogin och loggas som säkerhetshändelse. |
| **Session fixation** | Attackeraren tvingar offret att använda en token attackeraren redan känner till. | Vi accepterar aldrig ett klient-tillhandahållet sessions-ID som giltigt innan autentisering — alla token genereras server-side, uteslutande vid lyckad inloggning. Ingen "anonym session" existerar före login som senare skulle kunna "upphöjas". |
| **Cross-tenant-läckage** | En användare ser en annan användares data. | Oförändrat: Postgres RLS på `conversations` (se `docs/MAINAI_0.1_PLAN.md`). Transportmekanismen för autentisering (cookie vs header) påverkar inte RLS — `get_current_user` sätter samma RLS-sessionsvariabel som tidigare, oavsett varifrån token lästes. |

## Designval

1. **Två token, olika livslängd:**
   - **Access-token** (JWT, 15 min): `HttpOnly`, `Secure`, `SameSite=None`, `Path=/`. Skickas
     automatiskt på varje API-anrop. Kort livslängd begränsar skadan av ett läckt token
     kraftigt utan att kräva databasslagning per request.
   - **Refresh-token** (ogenomskinlig slumpsträng, 14 dagar): `HttpOnly`, `Secure`,
     `SameSite=None`, **`Path=/api/auth`** (skickas bara till auth-endpoints, inte till varje
     API-anrop — minskar exponeringsytan). Lagras aldrig i klartext server-side, bara som
     SHA-256-hash (samma princip som lösenord, fast utan behov av bcrypt eftersom värdet
     redan är hög-entropi slump, inte mänskligt valt).

2. **Refresh-rotation med återanvändningsdetektion:** varje lyckad refresh markerar den gamla
   token som återkallad och skapar en ny i samma "familj" (`family_id`). Presenteras en redan
   återkallad token igen (= troligen stulen och redan använd av offret, eller stulen och nu
   använd av attackeraren efter offret) återkallas **hela familjen** — inte bara den enskilda
   token — och händelsen auditloggas som `refresh_token_reuse_detected`.

3. **CSRF: double-submit cookie**, inte enbart `SameSite` (som ändå måste vara `None` här).
   Ett tredje, slumpmässigt värde i en **icke**-HttpOnly cookie (`csrf_token`) måste upprepas
   i headern `X-CSRF-Token` på varje muterande (icke-GET/HEAD/OPTIONS) `/api/*`-anrop.
   Verifieras med konstant-tidsjämförelse (`secrets.compare_digest`). Roteras vid varje
   inloggning och varje refresh.

4. **Strikt CORS:** `allow_origins` är en explicit lista (miljövariabel, kommaseparerad) —
   aldrig `*` (också inkompatibelt med `allow_credentials=True`, som krävs för att cookies
   ska skickas cross-origin över huvud taget).

5. **Ingen Authorization-header-fallback.** Tidigare Bearer-token-mekanism tas bort helt,
   inte bara kompletteras — två parallella auktoriseringsvägar hade varit svårare att
   resonera säkert om och en större attackyta. Konsekvens: Swagger UI:s "Authorize"-knapp
   fungerar inte längre för manuell test; testning sker med en riktig cookie-jar (se
   testsviten) eller en inloggad webbläsare.

6. **`localhost`/`127.0.0.1` är en webbläsar-definierad secure context** — `Secure`-cookies
   fungerar där utan HTTPS, så samma cookie-konfiguration (`Secure=true`, `SameSite=None`)
   används i både lokal utveckling och produktion. Inga separata "svagare" devinställningar
   som av misstag kan hamna i produktion.

## Vad som INTE ändras

- RLS-policyn och isoleringen mellan användare (`docs/MAINAI_0.1_PLAN.md`) — oförändrad.
- Lösenordshashning (bcrypt) — oförändrad.
- Rate limiting-infrastrukturen (slowapi) — återanvänds, utökas med nya gränser för
  `/api/auth/refresh` och `/api/auth/logout`.
- Audit-loggen — återanvänds, utökas med nya händelsetyper.

## Kvarstående, medvetet accepterad risk

- Tredjeparts-cookie-restriktioner i vissa webbläsare (Safari ITP m.fl.) kan i framtiden
  påverka `SameSite=None`-cookies mellan helt orelaterade domäner. Om frontend och backend
  någon gång delar registrerbar domän (t.ex. `app.exempel.se` och `api.exempel.se`) bör
  `COOKIE_DOMAIN=.exempel.se` sättas för att göra cookien first-party och undvika detta helt
  — konfigurerbart, inte hårdkodat, se `backend/app/config.py`.
