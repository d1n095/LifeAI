# Founder Knowledge Bootstrap — designförslag (blockerande)

**Status: DESIGNFÖRSLAG, väntar på godkännande. Ingen kod, RLS-policy, `render.yaml` eller
deploy är ändrad av detta dokument.** Detta är steg 1 (inventering + design) av den begärda
arkitekturkorrigeringen. Inget i punkt 2–8 nedan implementeras förrän det är uttryckligen
godkänt.

## 0. Varför detta är en korrigering, inte en nyhet

Den korrigerade hierarkin är:

- **MainAI** = Founder AI / root control plane — singleton, strikt låst till grundaren.
- **Founder Agents** = MainAI:s privata specialistagenter.
- **UserAI** = separat, RLS-isolerad personlig AI per registrerad användare.
- **LifeWeb Agent** = webbläsar-/stödagent under respektive UserAI.
- **Trust/Security/Memory/Orchestration** = kontrollerande systemagenter (delas som
  infrastruktur, inte som en gemensam AI-identitet).

Det här är **inte en ny idé som läggs på ovanpå**, det är en efterlevnad av något som redan
stod skrivet men aldrig byggdes:

> "Inga andra Life OS-moduler (Source Hub, **UserAI**, GlowUp) tas upp i denna version."
> — `docs/MAINAI_0.1_PLAN.md`, rad 4, skriven innan MainAI 0.1-arbetet ens påbörjades.

`UserAI` var alltså redan namngiven som en **separat, framtida modul** från dag ett. Det som
faktiskt byggdes i MainAI 0.1 är dock en enda delad AI-instans för alla registrerade
användare — precis den sammanblandning arkitekturkorrigeringen nu river upp. Tre konkreta
bevis i befintlig kod/dokumentation:

1. **`backend/app/models/user.py`** — `UserRole` har bara `admin`/`member`. Inget
   `founder`-koncept finns. En admin-användare är fortfarande bara en användare av samma
   delade chattmotor, inte ägare av en separat kontrollplan.
2. **`backend/app/rls.py`** — `documents`/`projects`/`tasks` är **explicit delad
   företagskunskap**, inte per-användare-isolerad (bara `conversations` och
   `document_chunks` har RLS-policyer idag). Det är rätt beteende för en delad kunskapsbas,
   men fel beteende om varje användare ska ha en egen, isolerad UserAI — det kräver att
   RLS-gränsen flyttas, inte bara att ett ord byts i ett dokument.
3. **`docs/MAINAI_ARCHITECTURE.md` rad 34 och 249** säger uttryckligen: *"MainAI är en
   enanvändar-/enföretags-AI-plattform (idag) som fungerar som en organisations centrala
   AI-hjärna"* — dvs. dokumentationen beskriver MainAI som *organisationens* (läs: alla
   registrerade användares) delade AI. Det är exakt den formuleringen som ska bort.

Med andra ord: arkitekturen har redan namngivit rätt separation (`UserAI` i
`MAINAI_0.1_PLAN.md`), men implementationen kollapsade den till en enda delad AI, och
dokumentationen (`MAINAI_ARCHITECTURE.md`) skrev sedan ner den kollapsade versionen som om
den vore avsiktlig. Den här bootstrap-planen är alltså en åter-isärdelning, inte en
omdesign från noll.

---

## 1. Inventering — befintliga filer

### 1.1 Vision

| Fil | Innehåll | Vision-relevans |
|---|---|---|
| `README.md` | Produktöversikt, snabbstart | Beskriver plattformen som "ett företags centrala kunskaps- och arbetshjärna" — samma enanvändar-framing som måste korrigeras (se §7). |
| `docs/ARCHITECTURE.md` | Fas 0-skelettarkitektur, delvis föråldrad | Rad 4–5: samma "företagets centrala AI-hjärna"-formulering. |

### 1.2 Arkitektur

| Fil | Storlek | Innehåll |
|---|---|---|
| `docs/MAINAI_ARCHITECTURE.md` | 47 KB | Kärnarkitektur, tjänstegränser, domänmodell, händelseflöde, säkerhets-/behörighetsmodell, Trust Engine. **Innehåller mischaracteriseringen, se §7.** |
| `docs/AI_PROVIDER_ARCHITECTURE.md` | 29 KB | Provideroberoende AI-lager, kapabilitetsbaserade interface, felmängd, orkestrering. |
| `docs/MEMORY_ARCHITECTURE.md` | 36 KB | Minnessystem: kort-/långtid, episodiskt/semantiskt, projekt/org/user/delat minne, trust scoring. |
| `docs/AI_ORCHESTRATION_ENGINE.md` | 34 KB | Modellrouting, poängsättning, konsensus, eskalering, observability. |
| `docs/CUSTOM_DOMAIN.md` | 7 KB | Strato-DNS-plan (SPF/DKIM/DMARC). |
| `docs/RENDER_DEPLOY.md` | 14 KB | Render Blueprint-driftsättning. |
| `docs/OPERATIONS.md` | 12 KB | Drift, backup, rollback, incidenthantering. |
| `docs/LIFE_LIBRARY_PLAN.md` | 17 KB | Framtida multimodal kunskapsmodul (planerad, ej påbörjad). |

### 1.3 Roadmap

| Fil | Innehåll |
|---|---|
| `docs/ROADMAP.md` | Fasindelad plan (Fas 0–5). Fas 3 nämner redan **"Founder AI säger"** som en notifieringskälla (rad 40) — ytterligare ett existerande spår av rätt hierarki som aldrig formaliserades. |
| `docs/MAINAI_0.1_PLAN.md` | Byggplan för MainAI 0.1-leveransen, namnger `UserAI` explicit som separat modul (se §0). |
| `docs/NEXTJS_UPGRADE_PLAN.md` | Taktisk migrationsplan (avslutad/klar), inte strategisk roadmap. |

### 1.4 Beslut

**Gap identifierad.** Det finns inget dedikerat beslutslogg-/ADR-format (Architecture Decision
Record). Beslutsmotiveringar finns idag inbäddade i löptext i arkitekturdokumenten och i
`docs/AUTH_THREAT_MODEL.md` (som explicit kallar sig *"Hotmodell och designbeslut"*), men
ingen av dem är ett strukturerat, versionshanterat objekt — de är prosa i ett dokument som kan
skrivas över utan spårbarhet. Se §4 för designförslag.

### 1.5 Handover

**Gap identifierad — finns inte.** `docs/STATUS.md` är närmast (en engångsgranskning av
commit `f7d75bf` innan MainAI 0.1 påbörjades), men den är skriven som en punktinsats, inte som
ett återkommande, versionshanterat handover-objekt som skapas efter varje viktig session. Se
§4 för designförslag.

### 1.6 Säkerhet

| Fil | Innehåll |
|---|---|
| `docs/SECURITY_BLOCKERS.md` | Kända, avsiktligt uppskjutna säkerhetsluckor med åtgärdsstatus. |
| `docs/AUTH_THREAT_MODEL.md` | Hotmodell för cookie-baserad session, CSRF, tillgångar. |

### 1.7 Sammanfattning: vad finns, vad saknas

| Kategori | Status |
|---|---|
| Vision | Finns, men behöver §7-korrigering |
| Arkitektur | Finns (djup och omfattande), behöver §7-korrigering i 1 fil |
| Roadmap | Finns |
| Beslut | **Saknas som strukturerat format** — bara inbäddad prosa |
| Handover | **Saknas helt** |
| Säkerhet | Finns |
| Founder Knowledge Vault | **Saknas helt** — inget index, ingen checksumma, ingen versionering av källfilerna själva |
| Context Bundle | **Saknas helt** |
| Founder-roll i datamodellen | **Saknas** — `UserRole` har bara `admin`/`member` |
| UserAI-isolering på DB-nivå | **Saknas delvis** — `documents`/`projects`/`tasks` är idag explicit delad, inte per-användare-RLS |

---

## 2. Founder Knowledge Vault — design

### 2.1 Syfte

En enda, auktoritativ, versionshanterad samling av grundarens privata kunskap och beslut,
skild från den globala produktkonstitutionen (se §5) och otillgänglig för UserAI (se §6).

### 2.2 Struktur (målarkitektur, ej implementerad)

```
founder_vault/
├── index.yaml                  # Master-index — se 2.3
├── constitution/                # Symlänkar/referenser till globala dokument (se §5)
├── decisions/                   # Versionshanterade beslutsobjekt (se §4.2)
│   └── 2026-07-19_smtp-implicit-tls.md
├── handovers/                   # Versionshanterade session-handovers (se §4.3)
│   └── 2026-07-19_session-016d4iix.md
├── private/                     # Grundarens privata filer, aldrig delade med UserAI
│   └── ...
└── context_bundle/
    └── latest.md                # Genererad, se §3
```

### 2.3 Metadataschema per objekt (`index.yaml`)

Varje fil i valvet registreras med:

| Fält | Beskrivning |
|---|---|
| `id` | Stabil identifierare (UUID eller slug), oberoende av filnamn |
| `original_file` | Sökväg till källfilen vid importtillfället |
| `checksum` | SHA-256 av innehållet vid senaste versionering — upptäcker tyst drift mellan valvet och den faktiska filen i repot |
| `version` | Monotont ökande heltal, bumpas vid varje meningsfull ändring |
| `source` | Var kunskapen kom ifrån: `session` (denna konversation), `human` (grundaren skrev det direkt), `import` (migrerad från befintligt repo-dokument) |
| `owner` | `founder` (alltid, per definition — valvet är strikt grundarens) |
| `access_class` | Se 2.4 |
| `created_at` / `updated_at` | Tidsstämplar |

### 2.4 Åtkomstklasser

| Klass | Vem får läsa | Exempel |
|---|---|---|
| `constitution` | MainAI, Founder Agents, UserAI (läs-only, se §5) | Global produktvision, säkerhetsmodell som gäller alla |
| `founder_private` | Endast MainAI + Founder Agents | Grundarens privata anteckningar, obekräftade idéer, känsliga affärsbeslut |
| `session_artifact` | Endast MainAI + Founder Agents | Beslut/handover-objekt från denna och tidigare sessioner |

UserAI har **ingen** åtkomstklass i denna tabell den kan läsa från Founder Vault — se §6 för
den tekniska spärren, inte bara policybeskrivningen.

---

## 3. Context Bundle — design

### 3.1 Syfte

Ett automatiskt genererat, sammanslaget kunskapspaket som **varje** MainAI-/Founder
Agent-session måste läsa vid start — så att ingen session börjar från noll eller riskerar att
motsäga ett redan fattat beslut.

### 3.2 Innehåll (i prioritetsordning)

1. Global produktkonstitution (§5) — orörlig, versionerad.
2. De N senaste beslutsobjekten (§4.2), nyast först.
3. Den senaste session-handoveren (§4.3).
4. Öppna säkerhetsblockerare (`docs/SECURITY_BLOCKERS.md` — filtrerat till `[ÖPPEN]`-poster).
5. Aktuell roadmap-position (vilken fas, vilka öppna punkter i `docs/ROADMAP.md`).

### 3.3 Genereringsmekanism (målarkitektur)

Ett skript (`scripts/build_context_bundle.py`, ej skrivet ännu) som:

- Läser `founder_vault/index.yaml`.
- Verifierar varje registrerad fils checksumma mot den faktiska filen — **blockerar** och
  flaggar avvikelse istället för att tyst servera inaktuell kontext.
- Konkatenerar innehåll enligt 3.2 till `founder_vault/context_bundle/latest.md`.
- Körs (a) manuellt on-demand, (b) automatiskt efter varje ny `decisions/`- eller
  `handovers/`-post.

### 3.4 Blockerande kontrakt

En MainAI-/Founder Agent-session som inte kan verifiera att Context Bundle är laddad och
checksumma-konsistent **ska inte agera på grundarens vägnar** förrän den är det — samma
fail-closed-princip som redan används för SMTP i produktion
(`app/main.py:_check_smtp_configured`) och Redis (`_check_redis_reachable`). Detta är ett
mönster som redan finns i kodbasen, inte ett nytt koncept.

---

## 4. Beslut & session-handover som versionshanterade objekt

### 4.1 Varför inte bara commit-meddelanden

Git-historik fångar *vad* som ändrades, inte *varför ett beslut fattades* eller *vad som
återstår*. `docs/AUTH_THREAT_MODEL.md` visar redan det rätta mönstret (skriven som ett
designbeslutsdokument innan implementation) — §4.2 formaliserar det mönstret till en
återkommande struktur istället för ett engångsdokument.

### 4.2 Beslutsobjekt (`founder_vault/decisions/<datum>_<slug>.md`)

Obligatoriska fält:

```yaml
date: 2026-07-19
decision: "Implicit SSL/TLS-stöd för SMTP (port 465)"
context: "Strato smtp.strato.com kräver implicit TLS, befintlig kod stödde bara STARTTLS"
alternatives_considered: ["Endast STARTTLS (avvisat — fungerar inte mot Strato)"]
outcome: "Ny smtp_use_ssl-inställning, ömsesidigt uteslutande mot smtp_use_tls"
verification: "3 mockade tester + full backend-svit (80 tester) grön"
status: merged  # draft | approved | merged | superseded
supersedes: null
```

### 4.3 Session-handover (`founder_vault/handovers/<datum>_<session-id>.md`)

Skapas **efter varje viktig session** (inte varje session — triggas av samma sorts
milstolpar som redan producerat de senaste sex arkitekturcommitten och de två PR-mergarna).
Obligatoriska fält:

```yaml
session_id: session_016d4iixQnxxqNhDrxHHxV4Q
date: 2026-07-19
completed: ["PR #1 och #2 mergade till claude/det-kommer-mer-879lcm", "SMTP SSL-stöd"]
in_progress: []
blocked_on: ["Grundarens godkännande av denna bootstrap-plan"]
next_step: "Vänta på godkännande, sedan implementera §2–§8"
```

### 4.4 Versionering

Bägge objekttyperna är append-only inom `founder_vault/`: en ändring skapar en ny version
(`status: superseded` + `supersedes: <id>` på den gamla), aldrig en tyst overwrite. Detta
speglar samma "aldrig skriv över, alltid ny commit"-princip som redan gäller för git-arbetet
i den här sessionen (se stående instruktioner om `git commit --amend` endast efter explicit
begäran).

---

## 5. Global produktkonstitution vs. privata grundarfiler

### 5.1 Separationsprincip

**Konstitutionen** är det som gäller *alla* AI-identiteter i systemet (MainAI, Founder
Agents, UserAI, LifeWeb Agent) och som UserAI får läsa (read-only). **Privata grundarfiler**
är allt som bara MainAI/Founder Agents får se.

### 5.2 Klassificering av befintliga dokument

| Dokument | Föreslagen klass | Motivering |
|---|---|---|
| `docs/MAINAI_ARCHITECTURE.md` (efter §7-korrigering) | Konstitution | Beskriver systemets grundstruktur — alla AI-identiteter behöver känna sina egna gränser |
| `docs/AI_PROVIDER_ARCHITECTURE.md` | Konstitution | Providerlagret används av både MainAI och UserAI |
| `docs/AI_ORCHESTRATION_ENGINE.md` | Konstitution | Orkestrering är delad infrastruktur |
| `docs/MEMORY_ARCHITECTURE.md` | Konstitution, med en not: minnesnivåerna `user`/`shared` i dokumentet måste läsas om i ljuset av UserAI-isolering (§8, punkt 4) | Minnesarkitekturen gäller alla, men dess nuvarande `user memory`-avsnitt beskriver ett scenario närmare den gamla delade modellen |
| `docs/SECURITY_BLOCKERS.md`, `docs/AUTH_THREAT_MODEL.md` | Konstitution | Säkerhetsmodellen gäller alla identiteter |
| `docs/ROADMAP.md`, `docs/MAINAI_0.1_PLAN.md`, `docs/STATUS.md`, `docs/NEXTJS_UPGRADE_PLAN.md` | Konstitution (historik) | Offentlig produkthistorik, inget känsligt |
| `docs/LIFE_LIBRARY_PLAN.md` | Konstitution | Framtida produktmodul, inte grundarhemlighet |
| `docs/CUSTOM_DOMAIN.md`, `docs/RENDER_DEPLOY.md`, `docs/OPERATIONS.md` | Konstitution | Driftdokumentation, ingen hemlig data (inga credentials i filerna) |
| `docs/ARCHITECTURE.md` | Konstitution (markerad föråldrad, se dokumentets egen rad 4–5) | Historisk, delvis ersatt av `MAINAI_ARCHITECTURE.md` |
| *(nya)* beslut/handover om affärsstrategi, ej-tekniska grundaravsikter | Privat | Hör inte hemma i en delad konstitution som UserAI kan läsa |

**Observation:** nästan alla befintliga dokument är redan lämpliga för konstitutionsklassen
rakt av — de beskriver systemet, inte grundarens privata avsikter. Det betyder att Founder
Vault-migreringen mest handlar om att **lägga till** en ny privat kategori (beslut/handovers
framåt) snarare än att gräva ut hemligheter ur befintliga filer.

---

## 6. Teknisk spärr: UserAI kan aldrig läsa Founder Vault eller andra användares data

Policy räcker inte — det ska vara **strukturellt omöjligt**, inte bara odokumenterat.

### 6.1 Nuläge (verifierat i kod)

- `conversations` och `document_chunks`: RLS-isolerade per `user_id`/`owner_id`
  (`backend/app/rls.py`) — teknisk spärr finns redan för dessa två tabeller.
- `documents`/`projects`/`tasks`: **explicit delad**, ingen RLS-policy alls
  (`apply_rls()`:s egen docstring bekräftar detta som avsiktligt för dagens
  enanvändarmodell).
- Founder Vault: existerar inte ännu — ingen spärr att verifiera.

### 6.2 Målarkitektur

1. **Founder Vault lever utanför applikationsdatabasen** (t.ex. i repot under
   `founder_vault/`, eller en helt separat lagringsyta) — inte en Postgres-tabell UserAI:s
   databasroll (`mainai_app`, se `backend/app/config.py`) någonsin har connection-behörighet
   till. Detta är starkare än en RLS-policy: en bugg i en RLS-`USING`-sats kan i värsta fall
   läcka rader, men UserAI:s DB-roll kan aldrig läcka en anslutning den saknar credentials
   till.
2. **`documents`/`projects`/`tasks` måste RLS-isoleras per användare** samma sätt som
   `conversations` redan är, **om** dessa tabeller ska bli en del av respektive UserAI:s
   privata kunskap istället för fortsatt delad företagskunskap. Detta är ett formellt
   arkitekturbeslut som §8 flaggar som en öppen fråga — inte en självklar konsekvens av
   Founder Vault-arbetet, eftersom "delad företagskunskap som alla UserAI:er får söka i" och
   "strikt per-användare-privat" är två olika, giltiga produktbeslut.
3. **`UserRole`-enumet behöver en `founder`-roll** (eller en helt separat identitetsmodell
   utanför `users`-tabellen — se öppen fråga i §8) så att "vem är grundaren" är en
   verifierbar databasfaktum, inte en konvention.
4. **Context Bundle-generatorn (§3.3) körs aldrig med UserAI:s databehörigheter** — endast
   med en process/roll som har läsrättighet till Founder Vault-lagringen, strikt skild från
   `mainai_app`-rollen.

---

## 7. Dokument som felaktigt beskriver MainAI som användarnas AI

Konkret, verifierat via grep — inga gissningar:

| Fil | Rad | Nuvarande text | Problem |
|---|---|---|---|
| `docs/MAINAI_ARCHITECTURE.md` | 34 | "MainAI är en enanvändar-/enföretags-AI-plattform (idag) som fungerar som en organisations centrala AI-hjärna" | Beskriver MainAI som den delade AI:n *alla registrerade användare* pratar med. Ska bli: MainAI är Founder AI; det registrerade användarna pratar med är UserAI. |
| `docs/MAINAI_ARCHITECTURE.md` | 249 | "Idag är MainAI en enanvändar-/enföretagsmodell" | Samma sammanblandning, i avsnittet om multi-tenant-målarkitektur. |
| `docs/ARCHITECTURE.md` | 4–5 | "LifeOS är en lokalt körd AI-plattform som fungerar som ett företags centrala kunskaps- och arbetshjärna" | Samma mönster, i det äldre (redan markerat delvis föråldrat) dokumentet. |
| `README.md` | 3 | "Lokal AI-plattform som samlar företagets kunskap... med ett leverantörsoberoende AI-lager" | Produktbeskrivningen på toppnivå ärver samma oprecisa "en AI för alla"-framing. |
| `docs/LIFE_LIBRARY_PLAN.md` | 38 | "repot är enanvändar-/enföretagsmodell idag" | Korrekt beskrivning av **dagens kod**, men bör kompletteras med en hänvisning till att detta är exakt vad Founder/UserAI-separationen ska ersätta, inte en permanent sanning. |

**Inte en textfix i isolation:** rad 34/249 i `MAINAI_ARCHITECTURE.md` sitter i samma
dokument som §10 (Trust Engine) och hela behörighetsmodellen — att ändra dessa två meningar
utan att gå igenom resten av dokumentets konsekvenser (särskilt minnesarkitektur-avsnittet
och multi-tenant-avsnittet, som båda direkt bygger vidare på "enanvändarmodell") riskerar att
lämna dokumentet internt motsägelsefullt. §8 nedan föreslår en fullständig genomläsning, inte
en sök-och-ersätt.

---

## 8. Redovisning: vad finns, vad saknas, hur importeras det

### 8.1 Finns redan, kan importeras rakt av (efter §7-korrigering där tillämpligt)

Alla 15 filer i `docs/` + `README.md` (se §1) — klassificeras enligt §5.2, registreras i
`founder_vault/index.yaml` med `source: import`, `version: 1`, checksumma av nuvarande
innehåll.

### 8.2 Saknas helt, måste skapas

| Artefakt | Beskrivning | Beroende |
|---|---|---|
| `founder_vault/index.yaml` | Master-index (§2.3) | Inget |
| `founder_vault/decisions/` | Första posten kan bakåtkonstrueras från denna sessions arbete (SMTP SSL-beslutet, PR-mergeordningen) | `index.yaml` |
| `founder_vault/handovers/` | Första posten: denna sessions handover | `index.yaml` |
| `scripts/build_context_bundle.py` | Context Bundle-generator (§3.3) | `index.yaml`, minst en decision/handover-post |
| `founder` (eller motsvarande) roll/identitet | Teknisk grund för §6 punkt 3 | Kräver en Alembic-migration — **kodändring, inte dokumentation** |
| RLS-beslut för `documents`/`projects`/`tasks` | Öppen fråga, se nedan | Kräver formellt beslut innan implementation |

### 8.3 Öppna frågor som kräver ditt beslut innan implementation (blockerar §6 punkt 2)

1. **Ska `documents`/`projects`/`tasks` förbli delad kunskap (en gemensam kunskapsbas alla
   UserAI:er kan söka i, t.ex. företagsdokumentation) eller bli strikt per-UserAI-privata?**
   Båda är giltiga produktbeslut med olika RLS-konsekvenser — detta dokument tar inte
   ställning åt dig.
2. **Ska grundarrollen modelleras som en rad i `users`-tabellen med `role=founder`, eller som
   en helt separat identitet utanför den tabellen?** Det första är enklast att bygga på
   befintlig auth-kod; det andra ger en starkare strukturell garanti (grundaren kan aldrig av
   misstag behandlas som "bara en admin-användare" i en query som glömmer att filtrera på
   roll).
3. **Var ska Founder Vault fysiskt lagras?** I repot (versionerat med git, synligt i varje
   klon) vs. en separat, åtkomstbegränsad lagringsyta (starkare isolering, men kräver ny
   infrastruktur utanför nuvarande Supabase/Upstash-uppsättning).

### 8.4 Importordning (föreslagen, efter godkännande)

1. Skapa `founder_vault/`-strukturen och `index.yaml` (tomt skelett).
2. Importera de 15+1 befintliga dokumenten in i indexet (ingen textändring ännu).
3. Genomför §7-korrigeringen i `MAINAI_ARCHITECTURE.md`, `ARCHITECTURE.md`, `README.md`,
   `LIFE_LIBRARY_PLAN.md` som en sammanhållen, granskningsbar ändring (inte separata
   halvfärdiga patchar).
4. Bakåtkonstruera de första decision-/handover-posterna för denna session.
5. Skriv `scripts/build_context_bundle.py` och generera första bundlen.
6. Adressera §8.3:s tre öppna frågor med dig, en i taget.
7. Implementera den tekniska spärren i §6 (databas-/rollnivå) baserat på svaren i steg 6.

Inget av 1–7 påbörjas förrän du godkänt denna plan.
