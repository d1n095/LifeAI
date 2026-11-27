# LifeAI – överlämning 2026-11-27 (sanerad för publikt GitHub)

> Statusnotering från grundarens arbetskonversation. **Inte** en releaseattest, säkerhetsacceptans eller oberoende verifiering av lokal VPS. Läs detta tillsammans med `docs/BRANCH_REGISTRY.md`, `docs/MAINAI_ARCHITECTURE.md` och `docs/SECURITY_BLOCKERS.md`. Den här noteringen ersätter inte dessa filer.

## Mål och arkitekturbeslut
- Företaget 4ThePeople (4TP) är avsedd juridisk ägare av ekosystemet **L4TP = Life 4ThePeople**. **LifeAI** är AI-systemet; MainAI dess långlivade orkestrerande kärna.
- Prioritet: få **MainAI + minne + Workbench + godkända arbetsflöden** körbara och verifierade innan nya sidoprodukter eller Orb/UI-utbyggnad.
- **Local-first, multi-model, API-independent**: egen drift, minne, verktyg och modellrouter; lokal inferens via t.ex. Ollama. Molnmodeller får vara valfri tillfällig resurs, **inte obligatoriskt beroende**. Noll obligatoriska externa AI-API-avgifter är ett designmål, inte noll driftkostnader.
- Systemets identitet, minne och uppgifter får inte ligga i en enskild modell. Modellbyte ska inte kräva ombyggnad av MainAI. Bedöm modellens faktiska kodförmåga, latens, RAM, retrieval och verktygskörning med reproducerbara tester före byte.
- Långsiktigt nattarbete: persistent arbetskö, checkpoints, återupptagning efter omstart, idempotenta uppgifter, behörighetsstyrd isolerad exekvering, testslingor, revisionsspår och mänskliga godkännanden vid privilegierade/riskfyllda åtgärder.
- MainAI:s minne ska knyta ihop projekt, källkod, dokument, bilder och videor med proveniens. Egress/dataminimering och tenant-separation först. Ingen generell obegränsad autonomi.

## Infrastruktur och isolering
- Det finns **gammal VPS** (2 GB RAM; ska lämnas ostörd) och **ny VPS** (16 GB RAM / 8 vCPU / ~480 GB).
- Ny VPS har befintlig, isolerad LifeAI-staging och en **separat utvecklings-/säkerhetssandlåda** under eget Unix-konto. Dessa får inte blandas, särskilt inte Docker-volymer, Postgres-data, portar, autentiseringsmaterial eller secrets.
- Lokal Ollama finns i projektets integrationsspår; befintlig staging har tidigare rapporterats använda `gemma3:4b`. Modell och prestanda måste verifieras i varje aktuell runtime.
- Separata repo-begränsade GitHub-deploy-nycklar används för agenters publicering. En SSH read-kontroll från sandlådan har lyckats och `origin` push har ställts om till SSH. **En faktisk push som bevis på skrivbehörighet var ännu inte rapporterad vid denna överlämning.**
- Lägg aldrig privata SSH-nycklar, tokens, tillfälliga inloggnings-URL:er, .env-filer eller produktionsuppgifter i det publika repot.

## Agenter och leveranser (rapporterade, inte generella releasegodkännanden)

### Freebuff – isolerat nattarbetsflöde
- Har rapporterat en isolerad LifeAI-kandidat med kort-SHA `85a1c10e` (lokal, inte hittad på GitHub vid kontroll).
- Levererade under `delivery/night-worker/`: `README.md`, `nightly-e2e.sh`, `local-stack-config.md` och två patchfiler; `docs/BRANCH_REGISTRY.md` uppdaterades i lokal arbetskopia.
- Rapporterade separat lokal stack bunden till loopback, men **ingen verifierad produktionsaktiverad nattarbetare**, Docker/staging-test eller oberoende säkerhetsacceptans.
- GitHub-skrivåtkomst saknades under leverans; separat deploy-nyckel registrerades därefter och SSH-läsning lyckades. Invänd faktisk push-SHA/branch innan innehållet betraktas som publicerat.
- Status: senaste avgränsade utvecklingsrunda **KLAR**, integration och nattarbetardrift **BLOCKERAD/ej verifierad**. Avbryt inte pågående nytt arbete utan konkret skäl.

### Codex – Founder Alpha release consolidation
- Lokal kandidat `codex/founder-alpha-release-consolidation`, SHA `26bbc2ab554bb33407c1f7852bf7a57d72abdb93`; **inte bekräftat publicerad på GitHub**.
- Rapporterat Cloud-resultat: backend **3 472 PASS, 0 FAIL, 14 SKIP**; verklig Postgres-restore, Ollama, konversation/RAG/minne efter restore och nio Playwright-fall PASS; paketets 18 skyddstester PASS.
- Rapporterat paket `LifeAI-vps-candidate-26bbc2ab.tar.gz` + SHA256-fil i Codex-miljö, **inte tillgängligt i GitHub** vid rapporttillfället.
- BLOCKED: betrodd VPS-åtkomst, säker CI mot exakt commit, Vercels effektiva deploy-policy, kernel-original, oberoende säkerhetsacceptans, oskyddad default branch. Befintlig staging orörd.

### Z.ai – R31 Round 4 och security closure
- Tidigare R31 Round 4-leverans rapporterad som ZIP/bundle/patchserie; uppgivet tip `3b5a1184…`, 314 PASS / 6 FAIL / 6 NOT_RUN och fem audit-original saknades.
- Ny körning `R31 SECURITY CLOSURE & INTEGRATION READINESS` blockerad av **"The sandbox failed to start"**. Inga nya tester eller filer i den rundan. Återuppta från verifierad checkpoint om miljön blir tillgänglig.
- Fokus fortsatt på sex FAIL, fem audit-original, migrationsverifiering (0061/0097), ny bundle och hemlighetsskanning. Ingen rekonstruktion av saknade original.

## Kritiska säkerhetsgrindar
- **`first_cycle_go=false`** tills separat säkerhets- och releaseprocess godkänt annat.
- Ingen automatisk merge till default, deployment, staging-ersättning, destruktiv databasrollback, policy-lock-uppdatering eller agent-självattest.
- A08 / `POLICY_LOCK` har oavslutad verifiering; de 28 policylåsen uppges oförändrade i Codex-kandidaten. Detta är **inte** ett PASS för release.
- Skydda standardgrenen, verifiera exact-head CI och effektiv Vercel-deploy-policy; oberoende säkerhetsgranskning krävs.
- GitHub-observation kan **inte** intyga lokala filer som ännu inte pushats.

## Arbetsdisciplin och nästa verifierbara milstolpe
1. Läs senaste agentrapporten: klassificera **ARBETAR / BLOCKERAD / KLAR**. Ge bara ny instruktion till agenten i rapporten om en konkret åtgärd behövs; avbryt inte arbetande agent.
2. Verifiera att Freebuff faktiskt publicerat rätt nattarbetsfiler på **separat gren**; jämför SHA och diff utan merge. Om inte, konstatera lokal-only, inte ett påstått GitHub-fel.
3. Läs fullständig `delivery/night-worker/README.md` först när den verkligen är publicerad. Kontrollera beständig arbetskö, restart/resume, deduplicering, isolerad kodexekvering och att resultat sparas. HTTP 200 räcker inte.
4. För Codex/Z.ai: importera bara originalleveranser med kontrollsummor och proveniens när de faktiskt finns tillgängliga. Ingen syntetisk reconstruction.
5. Förbered isolerade verkliga end-to-end-tester, men lämna nuvarande staging och gamla VPS orörda. Håll `first_cycle_go=false`.
6. När nattarbetaren bevisligen fungerar i testmiljö kan den ta **förhandsgodkända, begränsade** utvecklingsuppgifter över natten utan aktiv chatt.

## Arbetssätt
Svenska; ett konkret Mac/VPS-steg i taget; verifierad output före slutsats; inga irrelevanta sidoprojekt. Mer detaljerad privat överlämning för nästa chatt förvaras separat, **inte** i publikt GitHub.

_Notering skapad 2026-11-27 från användarens chatt, agentrapporter och GitHub-läsning. Kan innehålla äldre agentuppgifter tills nyare exakt-head-bevis finns._
