# Arbetsprinciper för det här repot

Detta är en stående instruktion för varje session som arbetar i LifeAI/MainAI-repot —
oavsett om det är en människa eller Claude Code. Den kompletterar (skriver inte över) den
befintliga säkerhets-/git-disciplinen i systemprompten (never force-push utan tillåtelse,
alltid `git status` före destruktiva kommandon, etc.).

## 1. Isolera, verifiera, granska

**Varje funktion, förbättring eller säkerhetsfix utvecklas i sin egen branch/PR när det är
praktiskt möjligt.** En branch/PR ska ha ETT syfte, inte flera.

Innan något mergas ska det:
1. **Vara tydligt avgränsat** — diffen innehåller bara det branchen/PR:n säger sig göra.
2. **Granskas mot scope creep** — jämför diffen mot basens tip, inte bara "ser rimligt ut".
3. **Verifieras med tester och verkliga scenarier** — inte bara enhetstester i isolation; en
   riktig väg genom systemet (worker/pipeline/E2E) där det är relevant, inte en gissning.
4. **Säkerhetsgranskas** — särskilt vid ändringar i import, auth, RLS, providernycklar eller
   annat som rör förtroendegränser.
5. **Dokumenteras** — commit-meddelande som förklarar VARFÖR, PR-beskrivning som en
   granskare kan läsa utan att redan sitta i kontexten.
6. **Inte blanda in orelaterade ändringar** — även små, uppenbart korrekta "medan jag ändå
   var här"-fixar.

**Om en orelaterad förbättring eller bugg upptäcks under arbetets gång** ska den normalt
flyttas till en egen branch och egen PR — inte fogas in i den pågående. Det konkreta
exemplet: när `PR #8`:s `npm audit`-check misslyckades på en sårbarhet som visade sig vara
helt orelaterad till PR #8:s eget innehåll (en redan existerande `next`-pinning på
huvudgrenen), fixades den på en egen branch (`claude/frontend-npm-audit-next-16-2-11`,
grenad från huvudgrenen — INTE från PR #8:s branch) och en egen PR (#9), utan att röra PR
#8:s diff. Det mönstret är regeln framöver, inte undantaget.

## 2. Samordning — undvik dubbelarbete mellan parallella brancher

Innan en ny implementation påbörjas: **kontrollera om funktionaliteten redan finns, är
planerad, eller pågår i en annan branch.** Om så är fallet — föreslå återanvändning,
samordning eller senareläggning istället för att bygga en dublett.

Konkret, i den här sessionens ordning:
1. Läs `docs/BRANCH_REGISTRY.md` — den levande översikten över aktiva brancher, PR:er,
   deras scope, beroenden och rekommenderad merge-ordning.
2. Om registret är föråldrat (en branch/PR saknas, eller status stämmer inte) — uppdatera
   det INNAN du fortsätter, inte efteråt. Verifiera mot faktiskt git-läge och GitHub API
   (`mcp__github__pull_request_read`), gissa inte.
3. Om det du ska bygga overlappar med något som redan pågår på en annan branch — stanna och
   fråga innan du börjar duplicera arbete.

**`docs/BRANCH_REGISTRY.md` ska hållas uppdaterad varje gång:**
- en ny branch/PR skapas,
- en branch/PR mergas eller stängs,
- en branch fryses (planerad men medvetet pausad, som `claude/p7a-governance-ingestion-plan`
  är just nu),
- beroenden mellan brancher ändras (t.ex. en branch rebasas mot en ny bas).

## 3. Målet: MainAI som eget tekniskt minne och samordnare

Det här är i dag en process grundaren/Claude Code följer manuellt. Det långsiktiga målet
(se `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`) är att MainAI själv, byggt ovanpå sitt eget
minnes-/kunskapssystem (§4 i den planen — källminne, projektminne, m.fl. lager), ska kunna:

- hålla reda på projektets status,
- analysera brancher och PR:er,
- upptäcka dubbelarbete,
- granska kod mot tidigare beslut,
- hitta konflikter innan de uppstår,
- hjälpa till att planera merge-ordningen.

**Detta är INTE något som byggs nu.** Det förutsätter att minnes-/kunskapsgrunden (P1–P7,
se plandokumentets §8 byggordning) redan finns. Tills dess är `docs/BRANCH_REGISTRY.md` och
den här filen den manuella motsvarigheten — och de bör skrivas så att de senare kan ge
MainAI samma information strukturerat, inte kastas när den dagen kommer.

## Se även

- `docs/BRANCH_REGISTRY.md` — levande branch-/PR-översikt, uppdateras löpande.
- `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` — arkitektur, datamodell, byggordning (§8),
  och den fullständiga visionen för MainAI:s minnessystem.
