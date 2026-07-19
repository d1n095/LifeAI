# Byggplan — MainAI 0.1

Avgränsning: endast MainAI-kärnan (chatt, minne, förtroende, säkerhet). Inga andra Life OS-
moduler (Source Hub, UserAI, GlowUp) tas upp i denna version.

> **Historisk plan, delvis omsprungen.** Milstolpe 1:s bootstrap-mekanism beskrivs nedan som
> den skrevs vid den tiden (`ADMIN_EMAIL`/`ADMIN_PASSWORD`, ett "admin-konto"). Sedan Founder-
> only-launch är detta ett fast, ensamt grundarkonto (`FOUNDER_EMAIL`/`FOUNDER_PASSWORD`, se
> `app/founder.py` och `docs/FOUNDER_KNOWLEDGE_BOOTSTRAP.md`) och publik självregistrering är
> avstängd i produktion. Texten nedan är lämnad orörd som historik, inte som aktuell sanning.

## Byggordning och varför

Säkerhet och dataisolering byggs **före** nya AI-funktioner, eftersom allt som läggs till
efteråt (fallback, kostnadslogg, konversationer) ska vara skyddat och spårbart från start
istället för att lappas på i efterhand.

### Milstolpe 1 — Grundsäkerhet (backend)
1. `User`-modell + lösenordshashning (bcrypt) + JWT-inloggning (`/api/auth/login`).
   Bootstrap: en admin-användare skapas automatiskt vid första uppstart från
   `ADMIN_EMAIL`/`ADMIN_PASSWORD` i `.env` om ingen användare finns.
2. `user_id`-ägarskap på `conversations`, `projects`, `tasks`, `documents`.
3. Postgres Row-Level Security-policies på samma fyra tabeller — även om applikationskoden
   har en bugg kan en användare inte läsa en annan användares data på databasnivå.
4. `get_current_user`-dependency som skyddar samtliga API-routrar (utom `/api/health` och
   `/api/auth/login`).
5. Rate limiting (slowapi) — särskilt hårt på `/api/chat` eftersom det är den endpoint som
   kostar pengar hos externa leverantörer.
6. `AuditLog`-tabell + hjälpfunktion som loggar inloggning, dokumentborttagning, providerbyte
   och kontoändringar.

**Varför detta går före funktionerna nedan:** utan detta steg skulle fallback, kostnadslogg
och konversationshistorik byggas ovanpå ett system utan ägarskap — de hade fått göras om.

### Milstolpe 2 — Tillförlitlig AI-motor (backend)
7. Provider-fallback: `resolve_active` utökas till en ordnad kandidatlista (DB-val → övriga
   konfigurerade leverantörer i prioritetsordning). `chat_with_fallback()` försöker nästa
   leverantör om en faller, och loggar varje försök.
8. Normaliserad token-bokföring i varje providers `ChatResult.raw_usage`
   (`prompt_tokens`/`completion_tokens` oavsett leverantörens eget format).
9. `UsageLog`-tabell + prislista per leverantör/modell → verklig kostnad i USD per anrop,
   summerbar i adminpanelen.
10. **Trust Engine** (`app/rag/trust.py`): bedömer confidence ("high"/"medium"/"low"/"none")
    från Qdrant-similarity på hämtade källor. Styr systempromptens instruktioner — vid låg/
    ingen confidence måste modellen säga att underlaget är otillräckligt istället för att
    gissa. Confidence + score följer med i varje chattsvar.
11. `/api/conversations` (lista, hämta en tråd, radera) — gör historiken faktiskt användbar,
    inte bara lagrad.

### Milstolpe 3 — Gränssnitt (frontend)
12. Inloggningssida + JWT lagras i klienten, alla API-anrop skickar `Authorization: Bearer`.
13. Konversationssidopanel i Chat — lista, öppna, ny konversation.
14. Röstinmatning (Web Speech API `SpeechRecognition`) och röstutmatning
    (`speechSynthesis`) — browser-native, ingen extra serverkostnad i 0.1.
15. Animerad AI-boll (`components/Orb.tsx`) med fyra tydliga tillstånd: lyssnar / tänker /
    talar / fel.
16. Confidence- och källvisning i chattgränssnittet (färgkodad badge + källista).
17. Enkel användnings-/kostnadsvy i adminpanelen.

## Säkerhetsprinciper som gäller genomgående

- API-nycklar lever **endast** i backendens miljövariabler. De skickas aldrig till
  frontend, loggas aldrig (varken i applikationsloggar eller audit log), och committas
  aldrig (redan skyddat av `.gitignore`).
- Lösenord lagras endast som bcrypt-hash.
- JWT signeras med `SECRET_KEY` (måste bytas från default i produktion — dokumenteras i
  README).
- Rate limiting och RLS är försvar i lager — även om ett lager missas ska inte hela systemet
  vara öppet.

## Explicit avgränsning för 0.1

- Endast OpenAI, Anthropic och Gemini är i fokus för funktionerna ovan (enligt uppdraget).
  DeepSeek/OpenRouter/Ollama-koden från Fas 0 rörs inte och fortsätter fungera som tidigare,
  men får inte extra 0.1-funktioner (fallback/kostnad) prioriterade i denna omgång.
- Ingen webbcrawler, ingen källkodsindexering, ingen proaktiv förslagsmotor — det är Fas 2–3
  i den generella roadmapen och hör inte till MainAI 0.1.
