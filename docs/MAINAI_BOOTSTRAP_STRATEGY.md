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
