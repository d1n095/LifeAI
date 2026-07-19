# MainAI — arkitektur

**Syfte:** ett enda, auktoritativt arkitekturdokument för MainAI (produktnamnet — repot heter
`LifeAI`/"LifeOS" av historiska skäl, se `README.md`). Detta ersätter inte
`docs/ARCHITECTURE.md` (som beskriver Fas 0-skelettet och är delvis föråldrat — Qdrant är t.ex.
ersatt av pgvector sedan dess) utan är den nuvarande, fullständiga bilden: vad som är byggt,
vad som är designat men inte byggt, och var gränsen mellan de två går — **explicit, inte
underförstådd**, i varje avsnitt nedan. Ren arkitektur: inget här är en instruktion att
implementera något omedelbart.

Relaterade dokument som detta bygger vidare på snarare än duplicerar:
`docs/AUTH_THREAT_MODEL.md` (hotmodell för sessionshanteringen), `docs/RENDER_DEPLOY.md`
(driftarkitektur för den kombinerade Render-containern), `docs/LIFE_LIBRARY_PLAN.md`
(nästa fas: Universal Object Model, utökad Trust Engine, multimodal inhämtning — refereras
under respektive avsnitt nedan, inte återgiven i sin helhet här).

**Notation:** varje avsnitt separerar tydligt **Idag** (finns i koden, verifierat) från
**Målarkitektur** (designat här, inte byggt). Att inte skilja på de två är exakt den sortens
implementationsgenväg uppdraget ber om att undvika.

---

## 1. MainAI core architecture

### Vad MainAI är

MainAI är en enanvändar-/enföretags-AI-plattform (idag) som fungerar som en organisations
centrala AI-hjärna: ett leverantörsoberoende chattlager ovanpå ett permanent, sökbart minne
(RAG) av organisationens egna dokument, projekt och konversationer, med ett inbyggt
tillförlitlighetslager (Trust Engine) som gör att AI:n aldrig framställer gissningar som fakta.
Målet är produktionsdrift från dag ett — inte en prototyp som "blir" produktionsklar senare:
autentisering, radernivåisolering (RLS), granskningsloggning, rate limiting och kostnadsspårning
finns redan i grundarkitekturen, inte som ett separat säkerhetstillägg.

### Styrande principer (gäller alla avsnitt nedan)

1. **Backend är den enda sanningskällan för behörighet.** Frontend renderar utifrån vad
   backend tillåter; den fattar aldrig egna behörighetsbeslut. Se §8–9.
2. **Försvar i djupet, inte ett enda lager.** Varje känslig operation kontrolleras minst två
   gånger på olika nivåer (t.ex. explicit `owner_id`-filter OCH Postgres RLS — se
   `backend/app/rag/vector_store.py`) — ett lager som fallerar tyst får inte bli ett
   säkerhetshål.
3. **Leverantörsoberoende genom abstraktion, inte genom villkorssatser.** Allt som pratar med
   en extern AI-tjänst går genom ett gemensamt interface (`LLMProvider`) — resten av systemet
   vet aldrig vilken leverantör som faktiskt svarade. Se §7.
4. **Confidence är en mätbar signal, inte en känsla modellen rapporterar om sig själv.**
   Trust Engine bygger på verifierbara signaler (retrieval-likhet idag; fler i
   målarkitekturen, §10) — aldrig på att be modellen självskatta sin egen säkerhet.
5. **Data isoleras per rad (RLS), inte bara per fråga.** En bugg i en router som glömmer ett
   `WHERE user_id = ...`-villkor får inte läcka data — Postgres Row-Level Security är den
   icke-kringgåbara sista linjen. Se §9.
6. **Determinism i test, inte i produktion.** All extern I/O (AI-leverantörer, e-post) är
   utbytbar bakom ett interface specifikt så att CI kan köra hela systemet deterministiskt
   utan externa beroenden (`backend/scripts/run_e2e_backend.py`) — produktionskoden självt
   gör aldrig antaganden om att vara i testläge.

### Högnivådiagram — idag (kombinerad Render-container, se `docs/RENDER_DEPLOY.md`)

```
                              Internet (HTTPS, TLS av Render)
                                        │
                        ┌───────────────▼───────────────┐
                        │   Render Free — EN webbtjänst   │
                        │                                 │
                        │  ┌───────────────────────────┐  │
                        │  │  Next.js (0.0.0.0:$PORT)   │  │   publikt nåbar
                        │  │  UI + /api/[...path] proxy │  │
                        │  └──────────────┬──────────────┘  │
                        │                 │ HTTP, loopback   │
                        │  ┌──────────────▼──────────────┐  │
                        │  │  FastAPI (127.0.0.1:8000)   │  │   ALDRIG publikt nåbar
                        │  │  routers · RAG · providers  │  │   (se §8, Dockerfile.combined)
                        │  │  · Trust Engine · auth       │  │
                        │  └───┬───────────────────┬──────┘  │
                        └──────┼───────────────────┼─────────┘
                               │                    │
                     ┌─────────▼────────┐  ┌────────▼─────────┐
                     │ Supabase Free     │  │  Upstash Free    │
                     │ Postgres+pgvector │  │  Redis           │
                     │ (strukturerad     │  │  (rate limiting) │
                     │  data + vektorer) │  │                  │
                     └─────────┬─────────┘  └──────────────────┘
                               │
                     ┌─────────▼─────────────────────────┐
                     │  Externa LLM-API:er (OpenAI,       │
                     │  Anthropic, Gemini, DeepSeek,       │
                     │  OpenRouter) / lokal Ollama          │
                     └──────────────────────────────────────┘
```

Detta är en **modulär monolit** medvetet, inte mikrotjänster — se §2 för exakt var
gränsytorna redan är förberedda för en framtida uppdelning, och varför den uppdelningen inte
görs nu (en enda Render Free-tjänst, 0 kr/mån, se `docs/RENDER_DEPLOY.md`).

### Målarkitektur: skalning utan omskrivning

Ingen del av design nedan förutsätter den kombinerade containern. Path till skala, i ordning
efter vad som faktiskt blir en flaskhals:

1. **Fler repliker av samma kombinerade image** bakom Renders egen load balancer (webbtjänster
   skalar horisontellt utan kodändring — statslöst förutom Postgres/Redis, som redan är
   externa).
2. **Separera FastAPI till en egen tjänst** (kräver en betald Render-plan för Private Service,
   eller motsvarande nätverksisolering på annan plattform) — `Dockerfile.combined` och
   `scripts/entrypoint-combined.sh` är avsiktligt separata filer från `backend/Dockerfile`
   just för att den här uppdelningen är en filbyte, inte en omskrivning.
3. **Bryt ut ingestion/embedding som en egen bakgrundsworker** (se §5, §7) när
   dokumentvolymen gör att synkron/BackgroundTasks-baserad indexering blir en flaskhals för
   request-svarstider.

---

## 2. Service boundaries

### Idag — moduler inom en process, med redan dragna gränser

| Modul | Ansvar | Får prata med | Får ALDRIG prata direkt med |
|---|---|---|---|
| **Frontend (Next.js)** | UI-rendering, sessionens cookie-hantering (via webbläsaren, aldrig JS), same-origin-proxy | Sin egen `/api/*`-proxy-route | Postgres, Redis, AI-leverantörer, FastAPI direkt (bara via loopback-proxyn) |
| **API-proxy** (`frontend/app/api/[...path]/route.ts`) | Byte-för-byte-vidarebefordran av `/api/*` till backend, server-side | FastAPI via `INTERNAL_API_URL` | Databasen, AI-leverantörer — den är en ren transportsträcka, ingen affärslogik |
| **Auth/Session** (`app/routers/auth.py`, `app/deps.py`, `app/security.py`, `app/cookies.py`) | Inloggning, tokenrotation, CSRF, kontolivscykel | Users-tabellen, Redis (rate limit), e-postlagret | RAG-lagret, AI-providers — auth vet inget om chatt eller dokument |
| **Konversation/Chat** (`app/routers/chat.py`, `app/routers/conversations.py`) | Orkestrerar ett chattanrop: hämta kontext → bygg prompt → anropa provider → spara | RAG-lagret, Trust Engine, providerlagret, `UsageLog` | Auth-internals (tar bara emot en redan autentiserad `User` via dependency injection) |
| **RAG/Kunskap** (`app/rag/`, `app/routers/knowledge.py`, `app/routers/documents.py`) | Chunkning, embedding, lagring/sökning i pgvector, dokumentets livscykel | pgvector, providerlagret (embedding-roll) | Chatt-routern anropar RAG, inte tvärtom — RAG känner inte till konversationer |
| **Providerlager** (`app/providers/`) | Enhetligt interface mot 6 AI-leverantörer, fallback, prissättning | Externa LLM-API:er (`httpx`) | Databasen direkt (returnerar bara resultat till anroparen, som i sin tur loggar) |
| **Admin** (`app/routers/admin.py`) | Providerkonfiguration, användningssammanställning | `ProviderConfig`, `UsageLog` (läs) | Ingenting utanför sitt eget ansvar — rollkontrollerad (§9) |
| **Projekt/uppgifter** (`app/routers/projects.py`) | Delad organisationsdata (ej per-användare-isolerad — se §9) | Egna tabeller | RAG, providerlager |
| **Bakgrundsjobb** (`BackgroundTasks` i `documents.py`; schemalagd cleanup, `app/cleanup.py`) | Asynkron indexering, tokenstädning | Samma databas, egen `SessionLocal()` | Kräver egen RLS-kontext (`SET LOCAL app.current_user_id`) explicit — se §5, §9 |

**Regeln som håller detta ihop:** varje modul har EN ägare av sin data (en modul, ett schema
den skriver till) och pratar med andra moduler genom Python-funktionsanrop inom samma process
idag — inte genom en delad databas-tabell flera moduler skriver till, och inte genom HTTP
mellan varandra. Det gör uppdelning till separata processer senare (§2, målarkitektur) till en
transportbytesfråga, inte en omskrivning av affärslogik.

### Målarkitektur: seams för framtida uppdelning

Redan idag är dessa gränser dragna PRECIS där en framtida tjänsteuppdelning skulle skäras:

- **Ingestion-workern** (§5, §7): `app/rag/ingest.py`s `index_document()`-funktion tar redan
  emot allt den behöver som parametrar (inget dolt globalt tillstånd) — att flytta den bakom
  en meddelandekö istället för `BackgroundTasks` kräver ingen ändring i RAG-lagrets publika
  interface, bara i vem som anropar det.
- **Providerlagret**: redan ett eget paket utan beroenden mot routers — kan brytas ut till en
  egen tjänst (t.ex. för att dela rate limits/cache mellan flera backend-repliker) utan att
  röra anroparkoden, förutsatt att interfacet (`LLMProvider`) exponeras via samma abstraktion
  fast bakom ett internt HTTP-anrop istället för ett direkt Python-anrop.
- **Trust Engine** (§10): en ren funktion (`assess_confidence`) utan sidoeffekter eller egen
  lagring idag — kan bli en egen tjänst när den växer till multi-signal-bedömning (§10,
  målarkitektur) utan att chatt-routern behöver veta skillnaden.

---

## 3. Repository structure

### Idag

```
LifeAI/
├── backend/
│   ├── app/
│   │   ├── routers/          # Tunna HTTP-lager — ETT ansvar per fil, ingen affärslogik
│   │   │                     # som inte kan testas utan HTTP (den logiken hör hemma i
│   │   │                     # models/ eller rag/ eller providers/)
│   │   ├── models/           # SQLAlchemy-modeller, 1:1 med databastabeller. Ingen modell
│   │   │                     # utan en motsvarande Alembic-migration.
│   │   ├── rag/               # Chunkning, embedding, retrieval, Trust Engine — RAG-lagret
│   │   ├── providers/         # LLMProvider-interfacet + en implementation per leverantör
│   │   ├── deps.py            # Autentisering/dependency-injection (Fastapi Depends)
│   │   ├── rls.py             # Row-Level Security-policyn — en fil, en sanningskälla
│   │   ├── cookies.py         # Sessions-cookiens attribut — EN plats, inte utspritt
│   │   ├── security.py        # Lösenordshash, JWT-kodning/avkodning, tidshantering
│   │   ├── config.py          # All konfiguration via env-variabler, en Pydantic Settings-klass
│   │   ├── limiter.py         # Redis-baserad rate limiting
│   │   └── main.py            # App-uppstart, middleware, startup-checks (SMTP-tvång m.m.)
│   ├── alembic/versions/      # Enda sanningskällan för schemat — ALDRIG create_all i produktion
│   ├── db-init/               # Lokal Docker Compose-rollprovisionering (mainai_app-rollen)
│   ├── scripts/                # ensure_app_role.py (Render), run_e2e_backend.py (CI-determinism)
│   └── tests/{backend,security,account}/  # pytest, tre svit-kategorier — se docs/OPERATIONS.md
├── frontend/
│   ├── app/                   # Next.js App Router — en route per sida, api/[...path] är proxyn
│   ├── lib/                   # api.ts (den enda platsen som anropar backend), auth.ts (CSRF-minne)
│   └── e2e/                   # Playwright — specs + delade helpers (urls.ts, helpers.ts)
├── docs/                       # Arkitektur, drift, hotmodell, planer — se korsreferenser överst
├── scripts/                    # Repo-rotens entrypoint-combined.sh (kombinerad container)
├── Dockerfile.combined          # Render-driftsättningens image
├── docker-compose.yml           # Lokal multi-container-utveckling
└── render.yaml                  # Render Blueprint
```

**Konvention som hålls hårt:** en router-fil får aldrig innehålla en databasfråga som en annan
router också behöver — den flyttas till en delad modul (`rag/`, ett framtida `services/`, se
nedan) istället för att kopieras. Det finns ingen `app/services/`-katalog idag eftersom
affärslogiken hittills varit tillräckligt tunn för att bo direkt i `rag/`/`providers/`/
routers — det är en medveten "inte i förtid"-avvägning, inte en förbisedd struktur.

### Målarkitektur: när `app/services/` blir motiverat

Införs när (inte innan) en router börjar innehålla >1 orsak att ändras — t.ex. om
Trust Engine växer till multi-signal-bedömning (§10) med egen konfiguration och egna
databastabeller, bryts den ut till `app/services/trust/` med sitt eget interface, importerat
av `routers/chat.py` precis som `app/rag/trust.py` importeras idag. Strukturen växer genom
extraktion ur befintliga moduler, inte genom att förhandsbygga tomma mappar.

---

## 4. Domain model

### Idag — kärnentiteter och relationer

```
User (1) ──── (N) Conversation ──── (N) Message
  │                                    (role, content, provider, model — spårbar per-svar)
  │
  ├──── (N) RefreshToken            (rotation + reuse-detection, se AUTH_THREAT_MODEL.md)
  ├──── (N) RevokedAccessToken       (jti-blocklist för omedelbar återkallning)
  ├──── (N) EmailVerificationToken
  ├──── (N) PasswordResetToken
  │
  ├──── (N) Document [uploaded_by]   (delad organisationsdata — se §9, inte RLS-isolerad)
  │           │
  │           └──── (N) DocumentChunk [owner_id]   (RLS-isolerad — se §9)
  │
  ├──── (N) Project [created_by]     (delad organisationsdata)
  │           └──── (N) Task
  │
  ├──── (N) UsageLog                 (append-only, kostnad/tokens per providerkall)
  └──── (N) AuditLog                 (append-only, säkerhetshändelser)

CompanyInfo   (global nyckel/värde — EN organisation idag, inte per-tenant, se §9)
ProviderConfig (global — vald aktiv leverantör/modell per roll: "chat" | "embedding")
```

**Viktig, avsiktlig asymmetri:** `Conversation`/`DocumentChunk` är strikt ägda av en
`User` (RLS-isolerade). `Document`/`Project`/`Task` är delad organisationskunskap — de har en
`uploaded_by`/`created_by`-kolumn för attribution, inte för åtkomstkontroll. Det här är inte en
lucka som ska täppas till reflexmässigt — det är en medveten produktdesign (ett företags
kunskapsbibliotek ska vara sökbart av alla i företaget) som §9 gör explicit istället för att
lämna den underförstådd.

### Målarkitektur: Organization som formell entitet (multi-tenant)

Idag är MainAI en enanvändar-/enföretagsmodell: `CompanyInfo` är en global tabell, inte
`organization_id`-scopead. Att bli en multi-tenant SaaS-plattform (flera separata företag på
samma installation) kräver:

```
Organization (NY)
  ├── (N) User [organization_id]           — nuvarande User FÅR en organization_id-FK
  ├── (N) Document [organization_id]        — nuvarande "delad = alla i systemet" blir
  ├── (N) Project [organization_id]           "delad = alla i DENNA organisation"
  ├── (N) CompanyInfo [organization_id]      — global key/value blir per-organisation
  └── (N) ProviderConfig [organization_id]   — varje organisation kan välja egen leverantör
```

Detta är en additiv migration (lägg till `organization_id`, backfilla en enda default-
organisation för befintlig data, uppdatera RLS-policyn i §9 till att inkludera
`organization_id = current_setting('app.current_organization_id')` utöver `user_id`) — inte en
omskrivning av datamodellen. Görs inte förrän en verklig andra-kund-drivkraft finns; att bygga
det i förväg utan en verklig andra tenant vore precis den sortens spekulativa abstraktion
uppdragets "inga implementationsgenvägar" INTE efterfrågar (det efterfrågar rätt design NÄR
den behövs, inte allt byggt i onödan idag).

### Målarkitektur: Universal Object Model (UOM)

`docs/LIFE_LIBRARY_PLAN.md` §3 beskriver en betydligt bredare framtida generalisering av
`Document` — `KnowledgeObject` som abstrakt bas för `SourceObject`/`ExtractionObject`/
`GeneratedProduct`/`PodcastEpisode`, med proveniens, versionskedja och samtyckesflaggor.
Refereras här som den auktoritativa källan för den designen — den upprepas inte i sin helhet i
det här dokumentet för att undvika att två dokument driver isär över tid. Byggspärren i det
dokumentet gäller fortfarande: inte påbörjad.

---

## 5. Event flow

### Idag — de faktiska request/svars- och bakgrundsflödena

**A. Chattanrop (synkront, request/response)**

```
1. Browser → Next.js /api/chat (same-origin, cookie medskickad automatiskt)
2. Next.js-proxy → FastAPI (loopback, INTERNAL_API_URL)
3. app/deps.py: verifiera access-cookie → SET LOCAL app.current_user_id (RLS-kontext)
4. app/rag/retrieve.py: embedda frågan → pgvector-sökning (RLS-scopead, owner_id + policy)
5. app/rag/trust.py: assess_confidence(hits) → trust-nivå (high/medium/low/none)
6. Bygg systemprompt: konversationshistorik (senaste 20) + hämtad kontext + trust-instruktion
7. app/providers/registry.py: chat_with_fallback() → försök primär leverantör, fall tillbaka
   vid fel (se §7)
8. Spara Message (user + assistant) i Postgres, spara UsageLog (kostnad/tokens)
9. Svar → proxy → browser: { reply, sources, confidence, providers_attempted }
```

**B. Dokumentinhämtning (synkront upload + asynkron indexering)**

```
1. Browser → POST /api/documents/upload → Document-rad skapas (status: pending)
2. FastAPI schemalägger BackgroundTasks._index_in_background() → svar returneras direkt
   (uppladdaren väntar inte på embedding)
3. Bakgrundsuppgiften kör i EGEN SessionLocal()/event loop — går ALDRIG genom app/deps.py,
   så app.current_user_id måste sättas explicit från document.uploaded_by (en beständig
   DB-kolumn), inte från kontextvariabeln request-flödet normalt använder — se §9 för varför
   detta är en RLS-kritisk detalj, inte en implementationsdetalj.
4. Text extraheras → chunkas → embeddas (providerlager, "embedding"-roll) → skrivs till
   pgvector (owner_id = uppladdarens id, se DocumentChunk i §4)
5. Document.status → indexed | failed
```

**C. Autentiseringsflöde (login → refresh → logout)**

```
Login:    POST /api/auth/login → verifiera lösenord (Argon2id) → utfärda access-JWT (kort
          livslängd) + refresh-token (databaslagrad, roterande familj) → båda som HttpOnly
          Secure SameSite=None-cookies → csrf_token i JSON-svaret (aldrig i en cookie —
          se docs/AUTH_THREAT_MODEL.md)
Refresh:  POST /api/auth/refresh (kräver giltig X-CSRF-Token-header) → gammal refresh-token
          markeras förbrukad, ny utfärdas (rotation) → återanvändning av en redan roterad
          token återkallar HELA tokenfamiljen (misstänkt stöld, se AUTH_THREAT_MODEL.md)
Logout:   POST /api/auth/logout → access-tokenets jti läggs i RevokedAccessToken (omedelbar
          återkallning, inte bara "sluta förnya") + refresh-token markeras återkallad
```

**D. Schemalagd städning** (`app/cleanup.py`, körs periodiskt om `ENABLE_SCHEDULED_CLEANUP=true`)

```
Idempotent jobb, PostgreSQL advisory lock förhindrar samtidig körning från flera repliker:
  - Utgångna/återkallade refresh-tokens äldre än retention-period → raderas
  - Förbrukade verifierings-/återställningstokens äldre än retention-period → raderas
  - RevokedAccessToken-rader vars naturliga JWT-utgång redan passerat → raderas
    (token är redan meningslös efter naturlig utgång — blocklisten behöver inte växa oändligt)
```

### Målarkitektur: händelsedriven arkitektur för skala

Idag är ALLA flöden ovan antingen synkrona (request/response) eller `BackgroundTasks`
(processlokal, inte överlevande en omstart mitt i). Det är rätt avvägning för nuvarande volym
(en Render Free-container). Vid skala (§1, målarkitektur) blir följande en meddelandekö
(t.ex. Redis Streams — redan en beroende i stacken, ingen ny infrastruktur):

- **Dokumentindexering** (B ovan): en kö-post istället för `BackgroundTasks`, så indexering
  överlever en containeromstart och kan skalas oberoende av request-hanterande repliker.
- **Kostnadsloggning** (`UsageLog`-skrivning i A, steg 8): kan flyttas till "fire and forget"
  via kö om skrivfrekvensen någonsin blir en flaskhals för chattens svarstid — inte motiverat
  idag (en enda `INSERT`).
- **Audit-logg-skrivning**: samma resonemang som kostnadsloggning.

Ingen av dessa görs förrän en mätning visar att de faktiskt är en flaskhals — spekulativ
köinfrastruktur för volymer systemet inte har är precis den sortens förtida optimering som
inte hör hemma i "produktion från dag ett"; "produktion från dag ett" betyder rätt
säkerhets-/isoleringsdesign från start, inte all möjlig skalningsinfrastruktur förbyggd.

---

## 6. Memory architecture

MainAI har fyra distinkta minnesnivåer idag, med olika livslängd, isolering och åtkomstmönster
— att blanda ihop dem (t.ex. behandla RAG-kontext som om den vore konversationshistorik) är en
vanlig AI-systemdesignsbugg det här avsnittet är explicit för att undvika.

| Nivå | Vad | Lagring | Livslängd | Isolering |
|---|---|---|---|---|
| **1. Arbetsminne** | De senaste ~20 meddelandena i EN pågående konversation, skickade till LLM:en i varje anrop | Inget separat — hämtas ur nivå 2 vid varje request | Ett request | Ärver konversationens RLS |
| **2. Episodiskt minne** | Full konversationshistorik, alla meddelanden, för alltid (tills raderad) | `conversations`/`messages` i Postgres | Permanent (explicit radering) | RLS: strikt per `user_id` (§9) |
| **3. Semantiskt/långtidsminne** | Embeddad organisationskunskap, sökbar via likhet, oberoende av VILKEN konversation som en gång refererade den | `document_chunks` (pgvector, HNSW-index) | Permanent (till dokument raderas) | RLS: strikt per `owner_id` (§9) — se not nedan |
| **4. Strukturerat minne** | Explicita fakta som inte är fritext: företagsinfo (`CompanyInfo`), projekt/uppgifter | Postgres, vanliga tabeller | Permanent | Delad organisationsdata (§9) — INTE RLS-isolerad, avsiktligt |

**Not om nivå 3:s isoleringsnivå:** `DocumentChunk` (det faktiska embeddade/sökbara
innehållet) är strikt per-uppladdare, MEN `Document` (metadatan chunken hör till) är delad. Det
betyder att idag är "organisationens kunskap" i praktiken uppdelad i lika många privata
kunskapsbibliotek som det finns användare, trots att `Document`-listan ser gemensam ut — se
§9:s öppna spänning kring detta, som är flaggad där som en medveten, ännu olöst avvägning, inte
tyst gömd.

### Hur minnesnivåerna samverkar i ett chattanrop

```
Fråga in
   │
   ├─► Nivå 1 (arbetsminne): senaste 20 meddelanden ur DENNA konversation
   │
   ├─► Nivå 3 (semantiskt): embedda frågan → topp-K mest lika chunks ur HELA
   │        användarens kunskapsbibliotek (oberoende av konversation)
   │
   └─► Sammanställs till EN prompt: [trust-instruktion] + [nivå 3-kontext] + [nivå 1-historik]
              → LLM → svar → sparas till nivå 2 (blir framtida nivå 1 för nästa fråga
                i samma konversation)
```

### Målarkitektur: explicit minnespromotion ("kom ihåg det här")

Nämnt redan i det ursprungliga `docs/ARCHITECTURE.md` §4 steg 5 men aldrig byggt: en
konversation kan innehålla ett beslut eller en fakta som borde bli en del av det permanenta
kunskapsbiblioteket (nivå 3), inte bara ligga kvar i just den konversationens historik
(nivå 2). Målarkitektur:

```
Användare markerar ett meddelande ("kom ihåg det här")
   → skapar ett SYNTETISKT Document (source = "conversation_extract", inte "upload")
   → går genom SAMMA chunknings-/embeddingpipeline som ett uppladdat dokument (§1, princip 3
     — ingen parallell mekanism)
   → blir sökbar i nivå 3 för framtida konversationer, med proveniens tillbaka till den
     ursprungliga konversationen (`source_conversation_id`, `source_message_id`)
```

Detta är den naturliga bryggan till `docs/LIFE_LIBRARY_PLAN.md`s `KnowledgeObject`-modell
(§4 ovan) — en konversationsextraktion är bara ännu en `object_type`.

### Cachelager (Redis) — inte minne i AI-bemärkelse

Redis används idag uteslutande för rate limiting (`app/limiter.py`) — inte som cache för
embeddings, providersvar eller sessionsdata. Målarkitektur för en framtida
svarstids-/kostnadsoptimering: cache identiska (fråga, kontext)-par korta perioder för att
undvika dubbla providerkostnader vid t.ex. dubbelklick eller nätverksretries på klientsidan —
inte byggt, inte motiverat av nuvarande belastning.

---

## 7. AI orchestration

### Idag

**Providerabstraktionen** (`app/providers/base.py`): varje leverantör implementerar
`LLMProvider` (`chat()`, `embed()`, `is_configured()`). Sex implementationer finns: OpenAI,
Anthropic, Gemini, DeepSeek, OpenRouter, lokal Ollama. Ingen kod utanför `app/providers/`
känner till leverantörsspecifika API-format.

**Rolluppdelning:** varje leverantör kan väljas oberoende per roll — `"chat"` och
`"embedding"` — via `ProviderConfig` (databaskonfiguration, admin-ändringsbar utan omstart)
eller env-variabel-default. En organisation kan t.ex. köra chatt via Anthropic och embeddings
via OpenAI samtidigt.

**Fallback-kedja:** `chat_with_fallback()` (`app/providers/registry.py`) försöker den
konfigurerade primära leverantören först; vid fel (nätverksfel, rate limit, ogiltig nyckel)
provas nästa leverantör i `CHAT_FALLBACK_ORDER` (konfigurerbar, t.ex.
`openai,anthropic,gemini`). Svaret innehåller `providers_attempted` så frontend/admin kan se
exakt vilken kedja som faktiskt kördes — ingen tyst leverantörsväxling utan spårbarhet.

**Kostnadsspårning:** varje lyckat providerkall loggas i `UsageLog` (append-only): leverantör,
modell, roll, prompt-/completion-tokens, beräknad kostnad (`app/providers/pricing.py` — `NULL`
om priset inte är känt, aldrig en påhittad nolla). Adminvyn (`/api/admin/usage/summary`)
summerar detta — se §9 för vem som får läsa den vyn.

**Prompt-konstruktion** (§5, §6): systemprompt = Trust Engine-instruktion (§10) + hämtad
kontext (nivå 3-minne) + konversationshistorik (nivå 1-minne) + användarens fråga. Detta sker
på ETT ställe (`app/routers/chat.py`) — inte dupliceras per leverantör, eftersom
leverantörerna delar samma `Message`-format via `LLMProvider`-interfacet.

**Determinism i test:** `backend/scripts/run_e2e_backend.py` byter ut `OpenAIProvider.chat`/
`.embed` med deterministiska fejkfunktioner via monkey-patching av samma klassmetoder resten
av systemet anropar — CI:s E2E-svit (och den nya `combined-container-verify`-CI-jobbet, se
`docs/RENDER_DEPLOY.md`) kör alltså den RIKTIGA orkestreringslogiken (fallback, kostnadsloggning,
Trust Engine) mot en fejkad sista länk, inte en fejkad orkestrering.

### Målarkitektur: fler modaliteter, samma mönster

`docs/LIFE_LIBRARY_PLAN.md` §7 planerar fler providerroller utöver `chat`/`embedding`:
`transcription` (tal-till-text), `vision` (bildförståelse/OCR), `tts` (talsyntes),
`diarization` (talaridentifiering). Målarkitekturen är EXAKT samma mönster som idag — ett nytt
`LLMProvider`-liknande interface per modalitet, samma `ProviderConfig`-rollmekanism, samma
`UsageLog`-tabell (bara fler värden i `role`-kolumnen) — inte en ny abstraktionsstil. Detta är
varför providerlagrets nuvarande design räknas som en investering som redan betalar av sig,
inte något som behöver skrivas om för att bära framtida modaliteter.

### Målarkitektur: orkestrering bortom en enda provider per anrop

Idag är varje chattanrop EN leverantör (med fallback vid fel, inte konsensus vid framgång).
Två utbyggnader värda att designa för, inte bygga än:

- **Multi-provider-konsensus** för hög-insats-svar (t.ex. juridiska/finansiella frågor): fråga
  flera leverantörer parallellt, jämför svaren, flagga avvikelse som en extra Trust
  Engine-signal (§10, målarkitektur — "cross-source agreement").
- **Modellval baserat på frågekomplexitet**: enklare frågor till en billigare/snabbare modell,
  komplexa till en dyrare — kräver en klassificeringssignal innan providerval, inte bara en
  statisk admin-konfigurerad modell.

---

## 8. Security model

### Idag — lagren, i den ordning en request faktiskt passerar dem

1. **Transportisolering:** backend är loopback-only (`127.0.0.1:8000`) inuti den kombinerade
   containern — inte publikt nåbar oavsett vad Render gör med tjänstens publika URL på
   plattformsnivå (verifierat i `combined-container-verify`-CI-jobbet, se
   `docs/RENDER_DEPLOY.md`). Browsern pratar ENDAST med Next.js, som proxar server-side.
2. **TLS:** Render provisionerar TLS automatiskt (Let's Encrypt) för både standard- och
   anpassad domän (se `docs/CUSTOM_DOMAIN.md`). `COOKIE_SECURE=true` gör att sessionscookien
   aldrig sänds okrypterat.
3. **Autentisering:** JWT-access-token (kort livslängd, HttpOnly-cookie) + databaslagrad,
   roterande refresh-token-familj (HttpOnly-cookie, path-scopead till `/api/auth`) — ALDRIG
   ett `Authorization`-headerbaserat alternativ (JS kan aldrig läsa tokens, se
   `docs/AUTH_THREAT_MODEL.md`).
4. **CSRF:** `csrf_token` levereras bara i JSON-svarskroppen (login/refresh/me), hålls i minnet
   klient-side (aldrig en cookie) — varje muterande request måste bära den som
   `X-CSRF-Token`-header. En attackerares sida kan aldrig ha fått tag i värdet (se §9:s
   CSRF-attacktest i `frontend/e2e/security.spec.ts`).
5. **Lösenordshantering:** Argon2id (inte bcrypt/PBKDF2), explicit styrkepolicy
   (`app/password_policy.py`), e-postnormalisering mot registreringsenumerering.
6. **Sessionsåterkallning:** omedelbar (`RevokedAccessToken`, jti-blocklist) för
   utloggning/misstänkt tokenåteranvändning; bulk (`sessions_valid_after`-kolumnen) för
   "logga ut överallt"/lösenordsåterställning — se `app/deps.py`s `<=`-jämförelselogik och dess
   dokumenterade skäl (samma-sekund-tvetydighet löses genom att fail-closed, inte fail-open).
7. **Rate limiting:** Redis-baserad, per-endpoint konfigurerbara gränser
   (`RATE_LIMIT_LOGIN_PER_MINUTE` m.fl.) — skyddar mot brute-force och mot okontrollerad
   AI-providerkostnad från en enda klient.
8. **Radnivåisolering (RLS):** se §9 — den icke-kringgåbara sista linjen, oberoende av om ett
   lager ovanför den har en bugg.
9. **Generiska felresponser:** `/api/health` returnerar `{"status": "unavailable"}` + HTTP 503
   vid beroendefel — ALDRIG databasadresser, Redis-fel eller stacktraces till klienten; den
   fullständiga exceptionen loggas bara server-side (`logger.exception`).
10. **SMTP fail-closed i produktion:** backend vägrar starta om `ENVIRONMENT=production` utan
    `SMTP_HOST` — e-postverifiering/lösenordsåterställning kan aldrig tyst sluta fungera.
11. **Granskningsloggning:** `AuditLog` — append-only, säkerhetsrelevanta händelser
    (inloggning, radering, providerbyte).
12. **Hemligheter:** aldrig i kod eller databasen i klartext — miljövariabler/Render secrets,
    `sync: false` i `render.yaml` för allt som inte är en literal, icke-känslig konfiguration
    (se `docs/RENDER_DEPLOY.md`).
13. **Beroendesäkerhet:** `npm audit --audit-level=high` i CI (blockerar high/critical), känd
    baslinje för moderate-och-under i `docs/SECURITY_BLOCKERS.md`.
14. **Databasroll-separation:** backend kör ALLA runtime-frågor genom en begränsad,
    icke-superuser-roll (`mainai_app`) — aldrig admin-/migrationsrollen, eftersom en
    superuser-roll kringgår RLS per definition (se `docs/RENDER_DEPLOY.md`).

### Målarkitektur: vad som ännu inte är byggt

- **Multi-faktor-autentisering.** Inte designad än — skulle hänga på samma sessionsmekanism
  (en extra verifieringssteg innan access-token utfärdas), inte en parallell auth-väg.
- **Secrets-rotation-automation.** Idag manuell (Render-dashboarden). En verklig
  produktionsskala bör ha schemalagd rotation av `SECRET_KEY`/`MAINAI_APP_PASSWORD` — kräver
  design för hur pågående sessioner överlever en `SECRET_KEY`-rotation (troligen: stöd för att
  verifiera JWT:er signerade med föregående nyckel under en övergångsperiod).
- **Automatiserad säkerhetsscanning bortom `npm audit`.** Ingen SAST/dependency-scanning för
  Python-sidan i CI idag (`pip-audit` eller motsvarande) — en verklig lucka, inte en medveten
  avgränsning, flaggad här explicit istället för dold.

---

## 9. Permission model

### Idag — tre lager, i ordning

```
1. AUTENTISERING  — vem är du?        (app/deps.py: giltig, oåterkallad session)
2. AUTORISERING   — vilken rollklass? (routerbeslut: role == "admin" för adminendpoints)
3. RLS            — vilka RADER?      (Postgres: kan inte kringgås av en bugg i lager 1–2)
```

Att ha tre separata lager, inte ett, är avsiktligt: en bugg i lager 2 (t.ex. en router som
glömmer rollkontrollen) läcker fortfarande inte data den inte borde, EFTERSOM lager 3
(databasens egen RLS-policy) filtrerar oberoende av vad applikationskoden tror att den redan
har kontrollerat. Det här är samma princip som §1 princip 2 ("försvar i djupet") applicerad
konkret på behörighet.

**Roller idag:** `admin` | `member` (binär, `app/models/user.py`). Adminroll krävs för
providerkonfiguration (`PUT /api/admin/providers/config`) och användningssammanställning
(`GET /api/admin/usage/summary`) — kontrollerat i routern, inte bara i frontend-UI:t (frontend
döljer admin-länkar av UX-skäl, men backend skulle avvisa anropet ändå om en `member` försökte
direkt).

**RLS-policy idag** (`app/rls.py`, se §4/§6 för vilka tabeller):

| Tabell | Isolering | Skäl |
|---|---|---|
| `conversations` | Strikt per `user_id` | Privata konversationer — aldrig delade |
| `document_chunks` | Strikt per `owner_id` | Sökbart/embeddat innehåll — se öppen spänning nedan |
| `documents` | INGEN (delad) | Avsiktligt: organisationens kunskapsbibliotek ska vara sökbart av alla |
| `projects`/`tasks` | INGEN (delad) | Samma resonemang — ett företags projektöversikt är gemensam |

**Sessionskontext-mekanismen:** varje databastransaktion får `app.current_user_id` satt via
`SET LOCAL` (en Postgres-sessionsvariabel, transaktionsscopead) — antingen automatiskt via
`app/deps.py`s dependency-injection för vanliga requests, eller explicit i bakgrundsuppgifter
som inte går genom den vägen (§5, punkt B — `document.uploaded_by`, inte en kontextvariabel,
eftersom bakgrundsuppgiften kör i en helt egen event-loop). En session utan
`app.current_user_id` satt (t.ex. en rå admin-/migrationsanslutning som råkar användas fel)
matchar INGEN rad — default-deny, inte default-allow (se `NULLIF`-kommentaren i `app/rls.py`).

### Öppen, medvetet oöst spänning (flaggad, inte gömd)

`Document` är delad men `DocumentChunk` (dess sökbara innehåll) är strikt per-uppladdare. En
icke-uppladdare som raderar ett delat dokument kan därför inte cascade-radera den ursprungliga
uppladdarens chunk-rader (RLS blockerar den raderande sessionen från att röra chunks den inte
äger) — se kommentaren i `app/routers/documents.py`s `delete_document`. Det här kräver ett
produktbeslut (ska raderingsrätt följa uppladdaren, en adminroll, eller organisationen som
helhet — se §4:s Organization-målarkitektur för den naturliga lösningen), inte en teknisk fix
som görs i förbifarten.

### Målarkitektur: från binär roll till resursnivå-behörighet

Produktionsmålet, i takt med att organisationer växer bortom "alla ser allt":

```
Idag:      User.role ∈ {admin, member}  — en global switch
Målbild:   Resursnivå-behörighet per KnowledgeObject (§4, UOM):
             visibility: private | team | organization
             collaborators: User[] med explicit roll (viewer | editor | owner)
           + Organization-scopead RLS (§4): app.current_organization_id läggs till varje
             policy-uttryck, inte bara app.current_user_id
           + consent_flags / rights_basis per objekt (docs/LIFE_LIBRARY_PLAN.md §9) — för
             material som inte är organisationens eget (t.ex. importerat med källrättigheter
             som måste respekteras i vem som får se/generera från det)
```

Detta är en RLS-policy-utökning (fler kolumner i uttrycket, inte en ny mekanism) — §1 princip
5 håller: radnivåisolering förblir den icke-kringgåbara sista linjen även när
behörighetsmodellen blir rikare.

---

## 10. Trust Engine

### Idag

`app/rag/trust.py` — en enda funktion, `assess_confidence(hits)`, som bygger confidence
uteslutande på **retrieval-likhet**: det högsta similarity-scoret bland de chunks som faktiskt
hämtades för frågan (pgvectors `1 - cosine_distance`, se `docs/RENDER_DEPLOY.md`s förklaring av
konverteringen). Fyra nivåer:

| Nivå | Tröskel (top-score) | Prompt-instruktion till modellen |
|---|---|---|
| `high` | ≥ 0.75 | Svara normalt utifrån källorna, ange alltid vilka |
| `medium` | ≥ 0.55 | Svara men markera tydligt vad som är osäkert |
| `low` | < 0.55 | Får INTE presentera gissningar som fakta — säg det uttryckligen |
| `none` | inga träffar | Får INTE hitta på ett faktapåstående — säg att underlag saknas |

**Varför detta är en verkningsfull mekanism, inte kosmetik:** confidence-nivån injiceras som en
EXPLICIT instruktion i systemprompten (`build_trust_instructions`) — det är den faktiska
verkställande mekanismen, inte bara en etikett som visas i UI:t efteråt. Den kan inte garantera
efterlevnad (en LLM kan i princip fortfarande ignorera instruktionen), men den gör osäkerhet
till standardramen istället för något modellen måste välja att flagga själv — se §1 princip 4:
confidence bygger på en mätbar signal (retrieval-likhet mot den faktiska kunskapsbasen), inte
på att modellen självrapporterar sin egen säkerhet.

**Var det syns:** `providers_attempted`, `confidence`, `confidence_score` och käll-referenser
returneras i chattens API-svar (`ChatResponse`, `app/lib/api.ts`) och renderas i chattens UI
(confidence-UI, M3 i `docs/MAINAI_0.1_PLAN.md`).

### Målarkitektur: multi-signal trust-bedömning

En enda signal (retrieval-likhet) fångar "hittade vi relevant material" men inte flera andra
verkliga tillförlitlighetsdimensioner. Målarkitektur — en sammansatt score istället för en
enskild:

```
TrustAssessment (utökad)
  ├── retrieval_score        (dagens signal — likhet mot frågan)
  ├── source_recency         (hur gammalt är källmaterialet? ett 3 år gammalt prisdokument
  │                            ska vägas ner även vid perfekt semantisk träff)
  ├── source_authority        (proveniensbaserad vikt — ett dokument uppladdat av en admin
  │                            eller markerat som "officiellt" väger tyngre än en anteckning
  │                            — kräver Document-metadata som inte finns idag)
  ├── cross_source_agreement  (säger flera OBEROENDE hämtade chunks samma sak, eller
  │                            motsäger de varandra? motsägelse sänker confidence även om
  │                            varje enskild träff har hög likhet)
  ├── citation_completeness   (kan varje sakpåstående i svaret faktiskt spåras till en
  │                            specifik hämtad chunk, eller "läcker" modellen in egen
  │                            allmän kunskap omärkt? kräver per-påstående-analys, inte
  │                            bara per-svar — se nedan)
  └── composite_score         (viktad sammanvägning → samma fyra nivåer som idag, så att
                                UI/prompt-instruktionslagret INTE behöver skrivas om — bara
                                hur composite_score beräknas ändras)
```

**Viktig designprincip för utökningen:** de fyra nivåerna (`high`/`medium`/`low`/`none`) och
prompt-instruktionsmekanismen (§10, idag) är redan RÄTT gränssnitt mellan Trust Engine och
resten av systemet — utökningen byter bara ut VAD som producerar nivån, inte hur nivån
används nedströms. Det är samma "abstraktion redan rätt draget"-mönster som §7:s
providerlager.

### Målarkitektur: per-påstående trust, inte bara per-svar

Idag är confidence EN nivå för HELA svaret. `docs/LIFE_LIBRARY_PLAN.md` §5 (Trust
Engine-utökningen för Life Library/Studio) kräver granulär bedömning: ett genererat dokument
(sammanfattning, rapport) måste kunna märka VARJE enskilt påstående med sin egen källa och sin
egen tillförlitlighet, inte en enda confidence-etikett för hela texten. Det här är en
förutsättning för Studio/LifeCast (refererat i §4) — inte byggt, designad där, inte upprepad
här.

### Målarkitektur: feedbackloop

Ingen mekanism idag för att en användares explicita "detta svar var fel"/"detta svar var
korrekt" ska påverka framtida `source_authority`-vikter (ovan) för samma källmaterial. Målbild:
ett omdöme kopplat till specifika chunk-referenser i det bedömda svaret justerar den källans
framtida vikt i `cross_source_agreement`/`source_authority`-beräkningen — en sluten loop mellan
Trust Engine och verklig användarvaliderad korrekthet, inte bara en statisk formel.

---

## Sammanfattning: vad som är byggt kontra designat

| Avsnitt | Byggt idag | Designat, inte byggt |
|---|---|---|
| 1. Core architecture | Kombinerad Render-container, modulär monolit | Fler repliker, tjänsteuppdelning |
| 2. Service boundaries | Moduler med redan dragna gränser | Formell tjänsteuppdelning |
| 3. Repo structure | Router-centrisk struktur | `app/services/`-extraktion vid behov |
| 4. Domain model | User/Conversation/Document/Project/... | Organization (multi-tenant), UOM |
| 5. Event flow | Synkrona flöden + BackgroundTasks | Meddelandekö vid skala |
| 6. Memory architecture | 4 nivåer, RLS-isolerade där relevant | Explicit minnespromotion, cache |
| 7. AI orchestration | 6 providers, fallback, kostnadsloggning | Fler modaliteter, multi-provider-konsensus |
| 8. Security model | 14 lager, se listan | MFA, secrets-rotation, Python SAST |
| 9. Permission model | 3-lagers (auth/roll/RLS), binär roll | Resursnivå-behörighet, Organization-RLS |
| 10. Trust Engine | Enkel-signal (retrieval-likhet), 4 nivåer | Multi-signal, per-påstående, feedbackloop |

Varje rad i högerkolumnen har en tydlig, redan dragen gräns mot vänsterkolumnen att bygga
vidare från — det är hela poängen med att skriva ner målarkitekturen nu: nästa
implementationsfas har en design att följa, inte en tom sida.
