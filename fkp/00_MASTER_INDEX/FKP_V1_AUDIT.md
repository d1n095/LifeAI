# FKP v1 — oberoende granskningsrapport
**Källa:** Grundarens FKP v1-granskningslager (`FKP_v1_REVIEW_OVERLAY.zip`), infogat oförändrat i FKP v1.1 enligt grundarens instruktion att integrera review-overlayen i det korrigerade paketet. Detta är den granskning som identifierade K-01 till K-04 och H-01 till H-05 nedan — grunden för de flesta korrigeringar i denna FKP v1.1-version. Se `00_MASTER_INDEX/CHANGELOG_FROM_V1.md` för hur varje åtgärdspunkt konkret genomfördes.

**Granskad:** 2026-07-19
**Omfattning:** de tre levererade FKP v1-arkiven
**Bedömning:** god källsamling, bristfällig exekverings-bootstrap

## 1. Vad som är bra och ska behållas

- Råoriginalen är separerade från kuraterade slutsatser.
- Proveniens, statusord och konflikter används konsekvent i stora delar av paketet.
- Founder Constitution och de icke förhandlingsbara principerna fångar visionen väl.
- Separationen MainAI / Founder UserAI / framtida UserAI finns tydligt beskriven.
- Kontextregeln med aktuellt meddelande, 3–5 senaste ordagrant och 15 senaste som tidslinje finns med.
- Säkerhetsreglerna om hemligheter, prompt injection, användarisolering och founder-gates är starka.
- Agentdiagnostik, resursmedveten routing, checkpoints och evidensbaserad Definition of Done är användbara riktningar.
- Designidéer som Update Center och Decision Lab är korrekt märkta som kandidater i vissa filer.

## 2. Kritiska fel som måste korrigeras före fortsatt kodarbete

### K-01 — fel kodbas presenteras som aktuell arkitektur

`CURRENT_ARCHITECTURE.md`, `BUILT_VS_DESIGNED.md`, `MODULE_REGISTER.md`, `IMPLEMENTED_AND_VERIFIED.md` och `SESSION_BOOTSTRAP.md` beskriver TanStack Start, Bun, `src/routes`, `src/modules/salary` och 9–11 Supabase-migrationer som LifeAI:s verifierade nuläge.

Detta är verifierat material från `savings-story-scanner` / My Money Master, inte den aktuella LifeAI-kodbasen. GitHub-sökning i `d1n095/LifeAI` på commit `f0a19752de...` visar i stället bland annat:

- `frontend/` med Next.js App Router,
- `backend/` med FastAPI,
- kombinerad container (`Dockerfile.combined`),
- dokumenterad Postgres/Redis/Qdrant→pgvector-utveckling,
- CI, Render-konfiguration och E2E-tester för denna stack.

**Åtgärd:** flytta savings-story-scanner-beskrivningen till "historiskt/annat produktunderlag". Bygg en ny nulägesinventering direkt från `d1n095/LifeAI`.

**✅ Genomfört i FKP v1.1:** se `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md`, `BUILT_VS_DESIGNED.md`, `04_PRODUCT_AND_MODULES/MODULE_REGISTER.md`, `06_PROJECT_STATUS/`; gammalt material i `10_HISTORICAL_DOMAIN_MATERIAL/`.

### K-02 — fel nästa steg

`NEXT_RECOMMENDED_STEPS.md` och handovern pekar mot `UPGRADE_26 v2` och Lovable-promptserien som nästa implementeringsspår. Dessa artefakter är framtagna för den äldre Supabase/TanStack-modellen.

**Åtgärd:** behandla dem som designalternativ. Jämför tabell för tabell och capability för capability mot LifeAI:s nuvarande FastAPI/SQLAlchemy/Alembic-modell innan någon SQL körs. Ingen migration får appliceras bara för att den är utförligt dokumenterad.

**✅ Genomfört i FKP v1.1:** caveat tillagd i `03_ARCHITECTURE/TARGET_ARCHITECTURE.md`; `06_PROJECT_STATUS/NEXT_RECOMMENDED_STEPS.md` och `09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md` pekar i stället mot att bygga vidare på LifeAI:s befintliga `document_chunk`-modell.

### K-03 — bootstrapen kan styra Claude till fel repo och fel mönster

`SESSION_BOOTSTRAP.md` säger att projektet är "formerly My Money Master / savings-story-scanner" och ger TanStack/Supabase-mönster som bindande.

**Åtgärd:** ersätt bootstrapens operativa innehåll med `CORRECTED_SESSION_BOOTSTRAP.md` i detta lager.

**✅ Genomfört i FKP v1.1:** `08_HANDOVER/SESSION_BOOTSTRAP.md` är nu den korrigerade versionen, vidare uppdaterad med verifierade 2026-07-19-fakta.

### K-04 — den kuraterade ZIP:ens integritetsmetadata är självmotsägande

- `PACKAGE_MANIFEST.json` och `SHA256SUMS.txt` listar 22 filer under `99_RAW_ORIGINALS/` som inte finns i den kuraterade ZIP-filen.
- Checksummerna för `PACKAGE_MANIFEST.json` och `SHA256SUMS.txt` verifierar inte mot filerna i arkivet.
- De 24 egentliga kuraterade Markdown-filerna verifierar däremot korrekt.

Råfilerna finns i de två separata råarkiven, så materialet verkar inte förlorat.

**Åtgärd:** skapa en FKP v1.1-manifest som bara listar filer som faktiskt ingår i respektive arkiv. Skapa checksummor sist och låt inte checksumfilen checksumma sig själv.

**✅ Genomfört i FKP v1.1:** `00_MASTER_INDEX/PACKAGE_MANIFEST.json` och `SHA256SUMS.txt` genererades sist, efter att allt övrigt innehåll var klart, och listar endast filer som faktiskt finns i `fkp/`.

## 3. Höga risker och logiska konflikter

### H-01 — "fråga eller stoppa" är för stelt

`AGENT_CONTEXT_RULES.md` och `ADAPTIVE_WORK_ORCHESTRATION.md` kräver stopp vid varje informationslucka. Det krockar med grundarens krav att AI:n ska kunna arbeta ostört, tänka vidare och undvika onödiga frågor.

**Korrigerad princip:** fortsätt med tydligt märkta, säkra och reversibla antaganden inom godkänd riktning; fråga bara när svaret förändrar vision, säkerhet, kostnad, extern handling, oåterkallelighet eller ett väsentligt designval; separera "behöver svar nu" från "kan verifieras senare"; spara antaganden och osäkerheter i en öppen lista.

**✅ Genomfört i FKP v1.1:** se "H-01 amendment" i `08_HANDOVER/AGENT_CONTEXT_RULES.md`.

### H-02 — beslut, nuvarande driftval och permanenta arkitekturbeslut blandas

Exempel: Render/Supabase/Upstash/Strato kan vara bekräftad **MVP-drift**, men är inte därmed permanent målarkitektur. Update Center, stationer, stad, orb-karta och Mirror City är idéer/kandidater, inte beslut.

**Åtgärd:** använd separata statusfält: observation, idé, kravkandidat, hypotes, lösningsalternativ, jämförd, testad, rekommenderad, grundarbeslutad, implementerad, verifierad.

**✅ Genomfört i FKP v1.1:** se statusordlistan i `07_CONFLICTS_AND_GAPS/CONFLICT_REGISTER.md`, tillämpad genomgående (CONFIRMED/DESIGNED/BUILT/CI-GREEN/DEFERRED/UNDER ANALYSIS).

### H-03 — designlaboratoriet saknas nästan helt

Paketet nämner Design Exploration & Decision Lab på en rad, men fångar inte dess arbetssätt. Detta är inte en dekorativ framtidsmodul — det är metoden som ska hindra projektet från att hoppa mellan lösningar och låsa fel arkitektur för tidigt.

**Status i FKP v1.1:** oförändrad kandidat, se `02_DECISIONS/CANDIDATE_REQUIREMENTS.md` §3. Inte byggt.

### H-04 — saknade krav efter FKP v1:s frystidpunkt

Se `NEW_REQUIREMENTS_CAPTURE.md` (nu `02_DECISIONS/CANDIDATE_REQUIREMENTS.md` i FKP v1.1).

### H-05 — "ingen cross-conversation leakage" behöver preciseras

**Korrigering:** "Ingen obehörig cross-user/cross-tenant/cross-scope-läckage. Godkänd durable memory inom samma identitet och scope ska fungera och vara synlig, korrigerbar och raderbar."

**Status i FKP v1.1:** korrigeringen är införd i andan av `08_HANDOVER/AGENT_CONTEXT_RULES.md`s dataisoleringsavsnitt.

## 4. Medelhöga problem

- 29 DOCX/binära filer textanalyserades inte. Fortfarande sant i FKP v1.1 — se `07_CONFLICTS_AND_GAPS/MISSING_INFORMATION.md`.
- Styrkeprofiler för Claude, ChatGPT, Gemini, Kimi och Lovable är hypoteser tills samma diagnostik körts. Oförändrat, se `03_ARCHITECTURE/AI_RESOURCE_ORCHESTRATION.md`.
- Kategorin "DEFERRED" är för grov. Delvis adresserat genom H-02:s statusordlista i FKP v1.1.
- Handoverfiler behöver `valid_for_repo`, `valid_for_commit`, `supersedes` och `expires_when`. Delvis adresserat: FKP v1.1:s handover-filer anger explicit branch/tip och "gäller tills direkt kodinspektion visar något annat" — inget formellt schema är byggt.

## 5. Säkerhetskontroll av råmaterialet

En automatisk kontroll hittade inga privata nyckelmarkörer eller rader med uppenbara känsliga miljövariabelnamn och värden i det läsbara råmaterialet. Det är inte en fullständig hemlighetsskanner och är ingen garanti. Råarkivet innehåller en fil med namnet `.env`; den ska fortsatt behandlas som känslig och aldrig pushas eller delas offentligt utan separat manuell kontroll. **Oförändrat i FKP v1.1** — ingen ny hemlighetsskanning har körts i denna omgång, och inga hemligheter har hanterats i denna session.

## 6. Rekommenderad verklig nästa trappa (ur v1-granskningen)

### Steg A — kunskapskorrigering, ingen produktkod
**✅ Genomfört** — detta är FKP v1.1.

### Steg B — verifiera exakt kodläge
**✅ Genomfört** — se `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md`, `06_PROJECT_STATUS/`.

### Steg C — slutför endast Step 1
**✅ Genomfört (kod), väntar på grundargodkännande (merge/deploy)** — se `claude/founder-only-launch`, `06_PROJECT_STATUS/CURRENT_STATUS.md`.

### Steg D — designlaboratorium före bred expansion
**Inte påbörjat.** Kvarstår som framtida arbete, se `09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md` steg 6+.

## 7. Slutlig dom

**Använd FKP v1 som bibliotek, inte som autopilot.** Visionen och många regler är starka. Den operativa bootstrapen och tekniknuläget har nu bytts ut i FKP v1.1 enligt rekommendationerna ovan.
