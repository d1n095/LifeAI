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
| **CSRF-attackerare** | Extern sajt får offrets webbläsare att skicka en autentiserad state-changing request mot vårt API utan offrets vetskap. | Ett separat, oförutsägbart CSRF-värde måste upprepas i en `X-CSRF-Token`-header på varje muterande anrop. Värdet levereras **en gång, i JSON-svarskroppen** från login/refresh/me — inte via en cookie (se Designval 3 nedan för varför). En extern sajt kan aldrig ha fått tag i det värdet och kan därför inte konstruera en giltig header, även om webbläsaren automatiskt bifogar sessionscookien. |
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

3. **CSRF: inte klassisk double-submit cookie** (ursprunglig plan, ändrad under
   implementation). Ett klassiskt double-submit-mönster förutsätter att frontendens JS kan
   läsa CSRF-cookien via `document.cookie` och eka tillbaka den — verifierat i praktiken
   (Playwright) att detta **inte fungerar cross-origin**: en cookie satt av backendens origin
   är aldrig läsbar från frontendens origin via `document.cookie`, oavsett `HttpOnly`-flaggan
   (Same-Origin Policy, inget vi kan konfigurera bort). Löst genom att CSRF-värdet i stället
   skickas **en gång i JSON-svarskroppen** från `/login`, `/refresh` och `/me` (läsbart
   cross-origin tack vare det explicita CORS-allowlistet), hålls i en ren in-memory
   JS-variabel på frontend (`frontend/lib/auth.ts`, nollställs vid varje sidladdning) och
   verifieras server-side mot ett databaslagrat värde. Måste upprepas i headern
   `X-CSRF-Token` på varje muterande (icke-GET/HEAD/OPTIONS) `/api/*`-anrop, verifieras med
   konstant-tidsjämförelse (`secrets.compare_digest`), och roteras vid varje inloggning och
   varje refresh.

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
- Rate limiting-infrastrukturen (slowapi) — återanvänds, utökas med nya gränser per endpoint.
- Audit-loggen — återanvänds, utökas med nya händelsetyper.

(Lösenordshashning uppgraderades från bcrypt till Argon2id i samband med kontoflödet nedan —
se den sektionen.)

## Tillägg 2026-07-18: fullständigt kontoflöde (registrering, verifiering, återställning, radering)

Byggt i samma säkerhetsmilstolpe, ovanpå cookie-sessionen ovan. Kort hotmodell för de nya
ytorna:

| Hot | Beskrivning | Skydd |
|---|---|---|
| **Kontoräkning (email enumeration)** | Attackeraren avgör om en e-postadress har ett konto genom att observera skillnader i svar. | `/register`, `/forgot-password` och `/resend-verification` svarar **identiskt** oavsett om adressen finns, redan är verifierad, eller inte — se `NEUTRAL_*_RESPONSE` i `backend/app/routers/auth.py`. Enda undantaget är efter lyckad lösenordsinloggning (attackeraren bevisar då redan att de känner till ett giltigt lösenord, vilket gör kvarvarande enumeration-risk försumbar) — se designval 7 nedan. |
| **Automatiserad massregistrering** | Bot skapar konton i bulk för spam/missbruk. | Honeypot-fält (`website`) osynligt för riktiga användare och skärmläsare, striktare rate limit (5/min/IP) på `/register` än på `/login`. Inget CAPTCHA-beroende till tredje part i denna sandlåda — se kvarstående risk nedan. |
| **Länkstöld ur mejlkorg/loggar** | Verifierings- eller återställningslänken läcker (delad inkorg, mejlserver-logg, skärmdump). | Kortlivad (24h resp. 1h), engångsanvändbar (`used_at`), 256-bitars slumpvärde — brute force är beräkningsmässigt omöjligt. Återställningslänken är kortare livslängd än verifieringslänken eftersom den ger direkt kontoövertagande, inte bara e-postbekräftelse. |
| **Kontoövertagande via lösenordsåterställning** | Attackeraren som lyckas trigga/avlyssna en återställning måste inte kunna behålla åtkomst om den legitima ägaren agerar. | Lösenordsåterställning återkallar **alla** aktiva sessioner för kontot omedelbart (`revoke_all_sessions_for_user`) — en redan inloggad attackerares session dör i samma ögonblick som lösenordet byts, inte vid nästa naturliga utgång. |
| **Brute force mot ett specifikt konto** | Många lösenordsgissningar mot samma konto, ev. spridda över flera IP-adresser för att kringgå IP-baserad rate limiting. | Separat räknare **per e-postadress** (inte bara per IP): 10 misslyckade `login_failed`-händelser inom 15 minuter blockerar vidare försök mot just det kontot, oavsett varifrån de kommer. `login_failed` loggas identiskt för "kontot finns inte" och "fel lösenord", så räknaren själv läcker ingen ny information. |
| **Ofullständig radering (GDPR)** | Ett konto "raderas" men personuppgifter blir kvar och läckbara. | `DELETE /api/account` tar bort kontot, konversationer och meddelanden permanent (inte mjukradering) samt alla sessions-/tokenrader. Delat företagsinnehåll (dokument/projekt/uppgifter) behålls men frikopplas (`created_by`/`uploaded_by` → NULL) — se motivering i `backend/app/routers/account.py`. Kräver lösenordsbekräftelse: sessionscookien ensam räcker inte för en oåterkallelig åtgärd. |
| **Otillräcklig lösenordsstyrka** | Svaga eller återanvända lösenord gör kontot lätt att gissa/knäcka offline vid en eventuell databasläcka. | Minst 12 tecken, lokal denylist för vanliga svaga lösenord, får inte innehålla e-postadressens lokal-del (`backend/app/password_policy.py`) — samt Argon2id (OWASP-rekommenderad, minneshård) istället för bcrypt för själva hashningen, vilket också höjer kostnaden för offline-attacker mot en läckt hash. |

**Designval:**

7. **Inloggning blockeras helt för overifierade konton**, inte bara kosmetiskt efter
   inloggning. Ingen session utfärdas förrän `email_verified=true` — enklare och säkrare än
   att låta en overifierad användare bli inloggad och sedan spärra enskilda routes, vilket
   hade krävt en parallell auktoriseringsväg att hålla i synk. Ett korrekt lösenord men
   overifierat konto svarar med ett distinkt `403` (inte `401`) så frontend kan visa "skicka
   bekräftelsemail igen" istället för en återvändsgränd — detta läcker i praktiken inget nytt:
   den som redan bevisat att de känner till rätt lösenord är per definition inte en
   blind-gissande angripare.

8. **Massrevocation via en tidsstämpel, inte en enumererad blocklista.** Både
   lösenordsåterställning och "logga ut från alla enheter" sätter bara
   `User.sessions_valid_after = now()` och markerar alla `refresh_tokens`-rader som
   återkallade. `app/deps.py` avvisar varje access-token vars `iat`-claim föregår den
   tidsstämpeln — en enda kolumnuppdatering ogiltigförklarar samtliga sessioner på alla
   enheter direkt, utan att räkna upp eller blocklista enskilda `jti`.

9. **Explicit, manuell cascade-städning vid kontoradering**, inte databasens
   `ON DELETE CASCADE`. Ingen av de relevanta främmande nycklarna är deklarerade med
   CASCADE/SET NULL på databasnivå (se `backend/alembic/versions/`) — bara
   `usage_log.user_id`/`conversation_id` är det, av en annan, redan dokumenterad anledning
   (`app/models/usage.py`). Radering görs därför explicit och transaktionellt i
   `backend/app/routers/account.py` (med uttrycklig rollback vid fel — se
   `docs/SECURITY_BLOCKERS.md`), vilket dessutom håller den exakta uppdelningen mellan
   "raderas" (personuppgifter) och "frikopplas" (delat företagsinnehåll) på ett ställe.

**Kvarstående, medvetet accepterad risk:**

- **Inget CAPTCHA-beroende till tredje part.** Skyddet mot automatiserad registrering är ett
  honeypot-fält plus rate limiting — svagare än t.ex. hCaptcha/Turnstile mot en målmedveten
  botoperatör, men undviker ett beroende till en extern tjänst (nätverksanrop, spårning,
  driftberoende) som inte är motiverat i nuvarande skala. Lägg till ett riktigt CAPTCHA om
  missbruk faktiskt observeras i produktion.
- **SMTP måste konfigureras explicit i produktion** (`SMTP_HOST` m.fl., se `.env.example`) —
  utan det loggas verifierings-/återställningsmejl bara till backend-loggen istället för att
  skickas (samma gracefulla nedgradering som en okonfigurerad AI-leverantör). Ett
  produktionsdeploy som glömmer detta skulle tyst sluta kunna verifiera nya konton eller
  återställa lösenord — synligt i loggarna, men inte som ett fel som stoppar uppstart.
- **`email_verification_tokens`/`password_reset_tokens` växer obegränsat** — samma
  ackumulerande-tabell-risk som redan noterad för `refresh_tokens` i `docs/SECURITY_BLOCKERS.md`
  post 2, inte en ny risk men värd att lösa med samma periodiska städjobb.

## Kvarstående, medvetet accepterad risk

- Tredjeparts-cookie-restriktioner i vissa webbläsare (Safari ITP m.fl.) kan i framtiden
  påverka `SameSite=None`-cookies mellan helt orelaterade domäner. Om frontend och backend
  någon gång delar registrerbar domän (t.ex. `app.exempel.se` och `api.exempel.se`) bör
  `COOKIE_DOMAIN=.exempel.se` sättas för att göra cookien first-party och undvika detta helt
  — konfigurerbart, inte hårdkodat, se `backend/app/config.py`.
