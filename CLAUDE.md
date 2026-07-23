# Arbetsprinciper för det här repot

Detta är en stående instruktion för varje session som arbetar i LifeAI/MainAI-repot —
oavsett om det är en människa eller Claude Code. Det handlar inte bara om brancher, utan om
hur hela projektet ska utvecklas. Den kompletterar (skriver inte över) den befintliga
säkerhets-/git-disciplinen i systemprompten (never force-push utan tillåtelse, alltid
`git status` före destruktiva kommandon, etc.).

## Grundprincip — Isolera, verifiera, samordna och granska

Detta är en av projektets absolut viktigaste grundprinciper:

- **En funktion = ett tydligt syfte.**
- **Ett tydligt syfte = en egen branch eller PR** när det är praktiskt möjligt.
- **Varje förändring ska vara lätt att förstå, granska, testa och vid behov återställa.**
- **Orelaterade ändringar ska inte blandas in** bara för att de upptäcktes samtidigt.
- **Alla förändringar ska granskas mot tidigare beslut, arkitektur och projektets
  långsiktiga mål** — inte bara mot "fungerar det tekniskt".

Innan något mergas ska det:
1. **Vara tydligt avgränsat** — diffen innehåller bara det branchen/PR:n säger sig göra.
2. **Granskas mot scope creep** — jämför diffen mot basens tip, inte bara "ser rimligt ut".
3. **Verifieras med tester och verkliga scenarier** — inte bara enhetstester i isolation; en
   riktig väg genom systemet (worker/pipeline/E2E) där det är relevant, inte en gissning.
4. **Säkerhetsgranskas** — särskilt vid ändringar i import, auth, RLS, providernycklar eller
   annat som rör förtroendegränser.
5. **Dokumenteras** — commit-meddelande som förklarar VARFÖR, PR-beskrivning som en
   granskare kan läsa utan att redan sitta i kontexten.
6. **Granskas mot tidigare beslut och arkitektur** — inte bara mot sin egen branch, utan mot
   den samlade planen (`docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`) och redan fattade beslut
   i tidigare PR:er.
7. **Inte blanda in orelaterade ändringar** — även små, uppenbart korrekta "medan jag ändå
   var här"-fixar.

**Om en orelaterad förbättring eller bugg upptäcks under arbetets gång** ska den normalt
flyttas till en egen branch och egen PR — inte fogas in i den pågående. Det konkreta
exemplet: när `PR #8`:s `npm audit`-check misslyckades på en sårbarhet som visade sig vara
helt orelaterad till PR #8:s eget innehåll (en redan existerande `next`-pinning på
huvudgrenen), fixades den på en egen branch (`claude/frontend-npm-audit-next-16-2-11`,
grenad från huvudgrenen — INTE från PR #8:s branch) och en egen PR (#9), utan att röra PR
#8:s diff. Det mönstret är regeln framöver, inte undantaget.

## Branch Registry — projektets levande karta

`docs/BRANCH_REGISTRY.md` är INTE bara en lista över brancher. Det är projektets levande
karta och ska alltid visa:

- vilka brancher som finns,
- vilka PR:er som är öppna,
- vad varje branch ansvarar för,
- beroenden mellan brancher,
- rekommenderad merge-ordning,
- konflikter,
- risk för dubbelarbete,
- vilka brancher som blivit inaktuella,
- vilka brancher som kan städas bort efter merge.

**`docs/BRANCH_REGISTRY.md` ska hållas uppdaterad varje gång:**
- en ny branch/PR skapas,
- en branch/PR mergas eller stängs,
- en branch fryses (planerad men medvetet pausad, som `claude/p7a-governance-ingestion-plan`
  är just nu),
- beroenden mellan brancher ändras (t.ex. en branch rebasas mot en ny bas),
- en konflikt eller risk för dubbelarbete upptäcks — även om den inte löses direkt, ska den
  synas i registret tills den är löst.

Registret ska verifieras mot faktiskt git-läge och GitHub API
(`mcp__github__pull_request_read`, `git merge-base`) — aldrig memorerat eller gissat.

## Standardbeteende — kontrollera innan du börjar

**Innan en ny branch eller implementation påbörjas ska alltid `docs/BRANCH_REGISTRY.md`,
öppna PR:er, tidigare beslut och den långsiktiga planen kontrolleras.** Om samma problem
redan håller på att lösas någon annanstans ska arbetet byggas vidare på det redan pågående
istället för att en ny, konkurrerande lösning skapas. Det här är standardbeteendet i hela
projektet, inte ett undantagsfall.

Konkret:
1. Läs `docs/BRANCH_REGISTRY.md` — den levande översikten över aktiva brancher, PR:er,
   deras scope, beroenden och rekommenderad merge-ordning.
2. Om registret är föråldrat (en branch/PR saknas, eller status stämmer inte) — uppdatera
   det INNAN du fortsätter, inte efteråt. Verifiera mot faktiskt git-läge och GitHub API,
   gissa inte.
3. Om det du ska bygga overlappar med något som redan pågår på en annan branch — stanna och
   föreslå att bygga vidare på det arbetet, eller fråga, istället för att duplicera det.

## Målet: MainAI som projektets tekniska minne och samordnare

Det här är i dag en process grundaren/Claude Code följer manuellt. När MainAI:s eget
minnes-/kunskapssystem är färdigt (se `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`, byggt
ovanpå §4:s lager — källminne, projektminne, m.fl.) ska den här processen bli en naturlig,
kontinuerlig del av MainAI själv, inte något grundaren behöver göra manuellt. MainAI ska då
kontinuerligt:

- hålla reda på hela projektets struktur,
- upptäcka när arbete överlappar,
- föreslå återanvändning innan ny kod skrivs,
- analysera beroenden mellan brancher och PR:er,
- föreslå säkraste merge-ordning,
- hitta arkitekturproblem innan de blir stora,
- upptäcka när tidigare beslut riskerar att brytas,
- hjälpa till att hålla projektet konsekvent över tid.

MainAI ska fungera som projektets tekniska minne och samordnare — inte bara som en
kodgenerator.

**Detta är INTE något som byggs nu.** Det förutsätter att minnes-/kunskapsgrunden (P1–P7,
se plandokumentets §8 byggordning) redan finns. Tills dess är `docs/BRANCH_REGISTRY.md` och
den här filen den manuella motsvarigheten — och de bör skrivas så att de senare kan ge
MainAI samma information strukturerat, inte kastas när den dagen kommer.

## Se även

- `docs/BRANCH_REGISTRY.md` — levande branch-/PR-översikt, uppdateras löpande.
- `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` — arkitektur, datamodell, byggordning (§8),
  och den fullständiga visionen för MainAI:s minnessystem.
