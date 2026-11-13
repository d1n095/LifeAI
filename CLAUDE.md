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
- **rekommenderad merge-ordning,**
- **vilka brancher som blockerar andra,**
- **vilka brancher som kan mergas oberoende,**
- **vilka brancher som väntar på ett beroende innan de bör uppdateras** (se "Merge-regeln"
  nedan — det här är INTE samma sak som att uppdatera dem i förväg),
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

## Merge-regeln — rebasa inte i förväg "för säkerhets skull"

**En branch får inte rebasas eller uppdateras i förväg "för säkerhets skull".** Rebase eller
GitHubs "Update branch" ska normalt ske FÖRST när branchens faktiska beroende faktiskt har
mergats — inte tidigare, inte som en förebyggande åtgärd. På så sätt undviks upprepade
rebases, onödiga mergekonflikter och dubbelarbete: en branch som rebasas mot en bas som
sedan ändras igen innan den egna branchen mergat har bara flyttat problemet, inte löst det.

Konkret: om branch B beror på branch A (A måste mergas eller uppdateras innan B blir
konfliktfri), rör INTE B förrän A faktiskt är mergad — även om det "känns säkrare" att
uppdatera B:s bas redan nu. `docs/BRANCH_REGISTRY.md` ska explicit visa vilka brancher som
väntar på ett beroende (se nästa avsnitt) så att det här inte behöver gissas fram varje
gång.

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

## Endast fjärrutveckling — grundarens Mac ska inte belastas

**Grundaren har upprepade gånger instruerat att LifeAI-utveckling inte får förbruka lagring,
minne eller CPU på grundarens egen dator.** Det här gäller varje session/agent som arbetar i
detta repo, oavsett om det är en människa eller Claude Code, och oavsett vilket verktyg som
startade sessionen.

Konkret, för varje ny session/agent som börjar arbeta i LifeAI/MainAI-repot:

1. **Kör utveckling fjärrstyrt** — i en molnmiljö (som denna sessionstyp redan gör) eller på
   den godkända Strato-VPS:en — aldrig som en tung, varaktig process direkt på grundarens
   Mac. En molnsession i en isolerad container är redan compliant per definition; frågan
   gäller separata processer/CLI-körningar som körs DIREKT på grundarens dator.
2. **Pusha färdigt arbete till GitHub** löpande, med exakt commit-SHA rapporterad — arbete
   som bara finns lokalt (okommitterat, i en worktree, på en opushad branch) är i praktiken
   oskyddat och ska inte lämnas kvar.
3. **Undvik varaktiga worktrees och tunga builds** på grundarens Mac. En agent som behöver en
   isolerad kopia av repot för en deluppgift ska städa den (eller flytta den till en molnmiljö/
   VPS) så snart deluppgiften är klar — inte lämna den kvar som ett permanent, växande
   lagringsavtryck.
4. **Verifiera backup innan städning** — ett lokalt lagrat projektartefakt (worktree, branch,
   byggcache) får bara tas bort efter att dess innehåll är verifierat identiskt med eller
   redundant mot en redan pushad GitHub-referens (`git diff <lokal-sha> <fjärr-sha>` tomt,
   eller motsvarande). Gissa aldrig att något är säkert att ta bort.
5. **Rapportera miljö och exakt SHA** — varje leverans ska ange var koden kördes (vilken
   molnmiljö/VPS) och den exakta commit-SHA:n som verifierats pushad, inte bara att "arbetet
   är klart".
6. **Destruktiv städning kräver grundarens godkännande av exakta sökvägar** — även när en
   agent själv är säker på att något är redundant (se punkt 4), ska de exakta
   borttagningssökvägarna listas och godkännas av grundaren innan borttagning, aldrig
   blind rekursiv radering.

En agent som saknar åtkomst till grundarens Mac eller till VPS:en från sin egen körmiljö ska
säga det explicit — aldrig påstå att en migrering eller städning skett när den bara kunnat
verifiera och pusha det som fanns tillgängligt i den egna arbetsytan.

### Dokumenterad exponering: grundarens Mac-hostname i git-commit-identitet

En oberoende granskning (2026-11-13) hittade **165 commits** i repots fullständiga historik
(2026-08-27 till 2026-11-07) vars author-email är `dennistorildson@MacBook-Air-som-tillhor-Dennis.local`
— git/macOS:s automatiskt genererade standardidentitet (`användarnamn@datornamn.local`) när
`user.email` aldrig satts explicit i den lokala git-konfigurationen. Datornamnet
("MacBook-Air-som-tillhor-Dennis") är synligt för alla som kan se commit-historiken, inklusive
via GitHubs eget UI och API (`git log --all --format='%ae' | grep -c '\.local'` bekräftar
antalet; sökningen görs på redan pushad, publik commit-metadata — inget nytt avslöjas här som
inte redan var synligt på GitHub).

**Detta är INTE ett skäl att skriva om publicerad commit-historik.** Att göra det utan
grundarens explicita godkännande är uttryckligen förbjudet (se den här filens och
systemets stående git-säkerhetsregler om att aldrig force-pusha eller skriva om historik utan
tillåtelse) — och skulle under alla omständigheter kräva en force-push till varje berörd
branch, vilket är en betydligt större och mer riskabel åtgärd än själva exponeringen. Detta
dokumenterar fyndet och förhindrar återfall; det åtgärdar inte retroaktivt redan publicerade
commits.

**Förhindra återfall, konkret:**
1. Varje molnsession som skapar commits i det här repot ska använda en explicit, icke-
   hostname-baserad `user.email` (redan default-beteendet för en molnsession som själv sätter
   `git config user.email` innan commit — denna sektion gör det explicit krav, inte bara
   underförstått).
2. Om grundaren någonsin committar lokalt från sin egen Mac igen (vilket avsnittet ovan redan
   avråder från), bör `git config --global user.email` först sättas till en adress som inte
   läcker datornamnet — t.ex. GitHubs egen "keep my email address private"-adress
   (`<id>+<username>@users.noreply.github.com`, se GitHub-inställningarna under Settings →
   Emails) eller en vanlig adress utan maskinnamn. `git config --global user.useConfigOnly true`
   förhindrar dessutom att git någonsin tyst faller tillbaka till den automatiskt genererade
   `användare@dator.local`-identiteten om `user.email` glöms bort.
3. Det här är grundarens egen Mac-konfiguration, inte något en molnsession kan sätta åt
   honom — en agent utan åtkomst till grundarens dator ska inte påstå att den ändrat den,
   bara dokumentera fyndet och rekommendationen (se föregående stycke i denna sektion om att
   aldrig påstå åtgärder utanför den egna körmiljön).

## Se även

- `docs/BRANCH_REGISTRY.md` — levande branch-/PR-översikt, uppdateras löpande.
- `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` — arkitektur, datamodell, byggordning (§8),
  och den fullständiga visionen för MainAI:s minnessystem.
