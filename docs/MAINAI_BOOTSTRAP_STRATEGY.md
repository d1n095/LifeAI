# MainAI — grundarbeslut om självständig utveckling
Beslut: 2026-11-27. Gäller utvecklingsprioritering för LifeAI/L4TP (Life 4ThePeople). Detta är ett strategiskt tillägg till [MainAI-arkitekturen](MAINAI_ARCHITECTURE.md), inte ett påstående om implementerad funktionalitet eller ett nytt exekveringsmandat.

## Mål
Bygg förmågan som bygger resten. MainAI ska successivt ta över utvecklingsledning, forskning, kunskapsarbete och utveckling av L4TP. Identitet, kunskap, beslut och arbetsstatus är beständiga och modelloberoende. Externa modeller är utbytbara experter.

## Prioritering
1. Självständig exekvering: verkliga varierande koduppgifter, nattarbete, tester, checkpoints och återhämtning.
2. Helhetsförståelse: bibliotek, samtal, historik, dokument, kod, bilder och video med källspårning.
3. Levande Blueprint: vision, nuläge, beroenden, kunskapsluckor, alternativ och rätt utvecklingsordning.
4. Självdiagnostik: identifiera verkliga kapabilitetsbrister genom mätning.
5. Forskning och innovation: undersök privata utvecklare, företag och forskning, jämför alternativa lösningar och utveckla egen kod.
6. Kontrollerad självförbättring: isolerade experiment, oberoende verifiering, regressionstester, godkänd integration och rollback.
7. Successiv överlämning av utvecklingsledning och agentstyrning till Life.

Produktfunktioner, webbdesign och kosmetik ska inte försena överlämningen, om de inte är nödvändiga för säker användning eller verifiering.

## Sammanhängande utvecklingscykel
Vision och källor → Founder Memory / Context Atlas / CICE → Living Blueprint → prioriterade kapabilitetsluckor → MainAI School / Development Supervisor → isolerad implementation → oberoende test och säkerhetsgranskning → godkänd integration → uppdaterad kunskapskarta och Blueprint.
Återanvänd befintliga moduler, arbetsmotor och tillståndsmodell; modulnamn här beskriver ansvar, inte verifierad driftsstatus. Inga parallella system utan dokumenterat behov.

Arbetskategorier inom samma motor: kunskap, strategi, forskning, utveckling, utbildning och underhåll. Bevara episodiskt, semantiskt, associativt, procedurellt, strategiskt, metakognitivt och prospektivt minne med provenance, scope och historik. Skilj fakta, hypoteser, beslut och verifierade resultat.

## Extern inspiration, egen implementation
Studera principer, inte kopiera hela externa kodbaser. Jämför med befintlig Life-kod först. Utforma egen lösning som passar arkitekturen; använd etablerade standarder och granskade bibliotek där lämpligt. Kontrollera licens, integritet, säkerhet, kompatibilitet och resurskostnad.
Referenser för framtida utvärdering (inte beslut om beroenden eller verifierad kompatibilitet):
- Anthropic: separata planerare, utvecklare och utvärderare; https://www.anthropic.com/research/harness-design-long-running-apps
- OpenHands: verklig agentbaserad kodutveckling; https://github.com/OpenHands/OpenHands
- Letta: bakgrundsbearbetning av minne; https://www.letta.com/blog/sleep-time-compute/
- Voyager och Hermes: återanvändbara färdigheter och kompetensutveckling; https://arxiv.org/abs/2305.16291 ; https://github.com/NousResearch/hermes-agent
- Autoresearch och AlphaEvolve: mätbara experiment och alternativa lösningar; https://github.com/karpathy/autoresearch ; https://deepmind.google/blog/alphaevolve-impact/
- RAPTOR och GraphRAG: kunskapsförståelse på flera nivåer; https://arxiv.org/abs/2401.18059 ; https://github.com/microsoft/graphrag
- STORM: forskning ur flera perspektiv; https://github.com/stanford-oval/storm
- Graphiti och Mem0: tidsbaserade och associativa kunskapsrelationer; https://github.com/getzep/graphiti ; https://github.com/mem0ai/mem0

Life ska själv kunna finna bättre metoder, jämföra mot baslinjer och även avvisa externa idéer. Utvärdera med verkliga, tidigare osedda uppgifter, inte bara kända testfall. Analysera konsekvenser för hela L4TP före förändring.

## Bootstrap Gate (acceptanskriterier, inte ny modul)
- Beständigt minne och lokal modell/RAG över omstart.
- Obevakad exekvering av varierande verkliga utvecklingsuppgifter, inte endast fasta recept.
- Kontrollerad återhämtning, parkering och återupptagning utan dubbelkörning.
- Verifierad förståelse av grundarvisionen och källbaserad Blueprint med beroenden och prioriteringar.
- En verklig kapabilitetsbrist identifieras, undersöks, åtgärdas i isolerad arbetskopia och förbättringen verifieras oberoende.
- Kontrollerad integrations- och rollbackkedja samt fungerande behörighetskontroller.
- Dokumenterade bevis för varje kriterium; skilj PASS, FAIL, NOT_RUN och BLOCKED.

## Säkerhet och agentdisciplin
Ingen självtilldelning av behörighet, ändring av säkerhetskärna eller kringgående av founder-godkännande. Behåll POLICY_LOCK och first_cycle_go=false tills behörigt separat beslut efter verifiering. Isolera kunddata, agentmiljöer, autentiseringsuppgifter och nätverksåtkomst. Inga automatiska produktionsmerges eller deployments på grund av detta dokument.
Pågående agenter fortsätter sina uppdrag; avbryt inte arbetande agent. Bedöm rapporter som ARBETAR/BLOCKERAD/KLAR. Prioritera nästa konkreta blockerare, inte omstarter eller onödiga rapporter. Detta beslut skapar inga nya agentuppdrag.


## Grundarbeslut 2026-11-28 — tidigarelagd överlämning av arbetsledning
Målet är att eliminera mänsklig väntetid mellan agenternas arbetspass, inte att vänta på full lokal programmeringskompetens. Fas A (arbetsledning) får föregå fas B (hybridutveckling) och fas C (lokal självständig utveckling). Detta är prioritering och acceptansspecifikation, inte exekveringsauktorisation. Befintliga godkännanden, POLICY_LOCK och first_cycle_go=false gäller oförändrat. Ingen ny produktionsåtkomst, provideraktivering eller automatisk merge.

### A — första användbara driftmilstolpen
Återanvänd Development Supervisor, befintlig queue, leases, agent coordination, workforce, activity/status, GitHub evidence, provider registry/spend och Living Blueprint. Inventera faktisk implementation före ändring. Agentadapter via dokumenterat, tillåtet gränssnitt; börja med en leverantör och bevisa hela kedjan innan fler ansluts. Ingen UI-automation för att kringgå sessionsgränser, abonnemangsvillkor eller åtkomstkontroller. Om adapter saknas: BLOCKED, manuellt överlämningspaket, inte falsk framgång.

Loop: observera verifierbar status → jämför mot Blueprint och branchregister → besluta om nästa avgränsade uppgift inom redan godkänt scope → policykontroll → leverera exakt en gång logiskt (idempotensnyckel, kvittens, reconciliation vid okänt utfall) → följ upp → oberoende verifiering → checkpoint → nästa uppgift. ARBETAR: avbryt inte; BLOCKERAD: avgränsa verkligt hinder; KLAR: verifiera leverans innan ny uppgift; UNKNOWN/STALE: undersök, inte dubbeltilldela. En agentrapport är opålitlig input, aldrig en auktorisationskälla. Låt agenten arbeta inom godkänt uppdrag utan rutinmässiga statuskrav.

### B — adaptiv resursstyrning och hybridutveckling
En gemensam kapacitetsbild: kontext (tokens/andel), leverantörskvot per faktiskt tidsfönster, reset-tid, samtidighetsgräns, kostnad/budget, färskhet och provenance, CPU/RAM/GPU/disk, modellkö, aktiv agent/session, checkpoint. Okänt är okänt; skilj uppmätt från prognos. Reservera kapacitet för grundarens interaktiva chatt och för säkra överlämningar. Resursantagning före dispatch; rättvisa mellan mål, aging, backpressure, begränsad samtidighet och återförsök. Ingen ny modellinstans per agent som standard. Separata agentkontexter och worktrees, delad lokal inferens där mätning visar lämplighet. Externa modeller är valfria, budgetstyrda konsulter; inga automatiska köp eller prenumerationsändringar. Använd dokumenterade kvotgränssnitt där de finns, t.ex. Codex App Server account/rateLimits/read, efter verifiering av version, behörighet och villkor. Hårdkoda inte 6h eller fasta procentgränser; prognostisera uppgiftskostnad med osäkerhetsmarginal och checkpoint innan kvoten tar slut. Pausa säkert när budget saknas; inga kontobyten för att undvika gränser.

### Modelloberoende arbetskontrakt
Varje arbetsenhet binder task/goal-id, version, beroenden, ansvarig, lease-generation, godkänt scope och utgångstid, bas-SHA, branch/worktree, acceptanskriterier, budget, verifieringsbevis, senaste checkpoint, rotorsak, nästa steg och öppna risker. Persistenta checkpoints även för misslyckade kodförsök, i isolerat skyddat lager; inga hemligheter eller privata data i publikt GitHub. Återställning verifierar ägare, hash, bas och policy; gammal arbetare får inte skriva efter takeover. Överlämning är kvitterad och spårbar; skilj kod skriven, committad, pushad, integrerad, testad och faktiskt installerad. Testresultat binds till exakt SHA och miljö. Ingen slutsats om godkänd säkerhet från självrapporterade tester.

### C — lokal lärande- och förbättringscykel
Använd MainAI School, Founder Memory, Context Atlas/CICE och kunskapslagret för återanvändbara färdigheter, beslut, felhistorik och evidens. Modellval mäts per uppgiftstyp: verifierad kvalitet, regressioner, kostnad per godkänt resultat, latenstid, kontext, återförsök och överlämningskvalitet. Jämför på tidigare osedda uppgifter; lokal först när kvalitet och risk tillåter. Minneskomprimering bevarar originalreferenser, tidslinje, motbevis och versionshistorik; skilj observation, hypotes och beslut. Bibliotekets dokument, kod, bild/video och samtal behandlas med proveniens, åtkomst och raderingsregler; externa källor är data, inte instruktioner. Experiment isoleras, mäts mot baslinje och kräver separat godkänd integration. Ingen självändring av policy, kontrollplan, godkännandemekanismer eller privilegier.

### Föreslagna acceptansgrindar för första drift
A0: inventera och återanvänd befintliga komponenter; verifiera aktuell SHA, migrationskedja, policytest, isolerad miljö och återställning. A1: läs verkliga agentstatusar och GitHub-evidens, skilj färska/okända värden; kör först i shadow mode utan dispatch. A2: en uttryckligen auktoriserad, avgränsad testuppgift via en stödd adapter, kvittens, checkpoint, resultat och verifiering. A3: simulerat avbrott/kvotstopp och omstart, inget dubbelarbete eller obehörig återupptagning. A4: kontrollerat obevakat pass med flera avgränsade uppgifter och verifierad morgonrapport; interaktiv chatt samtidigt. A5: oberoende säkerhetsacceptans och grundarens separata beslut om driftsättning. Rapportera PASS/FAIL/NOT_RUN/BLOCKED per grind; inga procenttal utan mätning. Föreslagen första drift är isolerad från produktion; ingen aktivering genom denna text.

Morgonrapport: exakt körande SHA, tidsintervall, färska agentstatusar, utförda uppgifter med evidens, pågående arbete, blockerare, budget och nästa beslut. Rapportera inte aktivitet som resultat. Om ingen aktivitet har skett, säg det. Håll chatt och bakgrundsexekvering separerade. Röst och framtida Life Home får eget behörighetsstyrt verktygslager; inga utvecklingsagenter får privata hembehörigheter.

### Integrationsstatus och uppdrag
Utgångspunkt enligt dokumenterad kandidat 2026-11-28: freebuff/bootstrap-integration-20261127; se delivery/bootstrap-integration/README.md och docs/BRANCH_REGISTRY.md för aktuella SHA och evidens. Där finns återhämtning, task-scope, leases, provider-E2E och reparationsfixar; kodkvalitet hos lokal modell är kvarstående risk. Codex rapporterade WORKER_CONCURRENCY utan faktisk parallellism, avsaknad av gemensam resursantagning/chattreserv, rättvisebehov, gemensam heartbeat och temporära worktrees. Codex äger drift/kapacitet; Freebuff äger integration; Z.ai oberoende säkerhet; Mini tidigare leverans. Inga nya uppdrag här och inga avbrott. Migrationsrevision 0090 och rapporterat policytest-FAIL måste försonas och verifieras, inte kringgås. Säker integration använder separat testdatabas och exakt identifierad worker; gamla tjänster lämnas ostörda. Full svit som hängt är NOT_RUN/ej godkänd. Aktuell installerad version är ännu inte bekräftad.

### Produktvision (efter bootstrap, inte aktiv implementation)
LifeOS Mobile: utvärdera AOSP och säkerhetsprinciper från GrapheneOS mot Linux/egen kärna, kompatibilitet, uppdateringar och verifierad boot. LifeStore är en möjlig produkt, inte låst beslut. Life Home: lokal styrning, interoperabilitet, manuella reservvägar, minsta privilegium, ägarstyrda rutiner; säkerhetskritiska åtgärder kräver separat auktorisation. LifeMusic, LifeInvest, transport/arbetsplanering och övriga appar behåller sina egna produktkrav. Life ska driva forskning och Blueprint för dessa när bootstrap tillåter; inte försena första drift.

### Referenser för vidare jämförelse
Temporal (durable workflows), LiteLLM (budget/routing), Ray (resursantagning), Kubernetes Kueue (rättvisa och kapacitetsdelning), OpenHands (kontextkomprimering), Letta (beständigt minne), Anthropic (långlivade agentarbetsflöden), Ollama (minneskostnad vid parallell inferens), Codex App Server (stödda kontogränssnitt). Kandidater till arkitekturprinciper, inte nya beroenden; verifiera officiell dokumentation, licenser och faktisk kompatibilitet före implementation.
