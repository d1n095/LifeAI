# Branch-/PR-register — projektets levande karta

Detta är INTE bara en lista över brancher — det är projektets levande karta, och den
manuella motsvarigheten till vad MainAI själv ska kunna göra en dag (se `CLAUDE.md`s
"Målet"-avsnitt och `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`). Den ska hållas uppdaterad
varje gång en branch/PR skapas, mergas, stängs eller fryses, eller när en konflikt/risk för
dubbelarbete upptäcks — se `CLAUDE.md`s "Branch Registry"-avsnitt för när.

**Senast verifierat mot faktiskt git-/GitHub-läge:** 2026-07-23 (commit-SHA:er och
PR-nummer nedan är hämtade direkt via `git`/`mcp__github__pull_request_read`, inte
memorerade).

## Aktiv kedja: kunskapsimport (P1 → P2 → P7A)

Byggordning enligt `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §8. Varje branch är grenad
från föregåendes tip-commit, inte från huvudgrenen direkt — så diffen i varje PR innehåller
bara den fasens eget innehåll.

```
claude/det-kommer-mer-879lcm (huvudgren, tip a141065)
  └─ claude/life-library-durable-worker-merged (PR #6:s fasta bas-snapshot, @ 8cd926a)
       └─ claude/founder-knowledge-studio-v1  — P1, PR #7, DRAFT, @ 5690aa2
            └─ claude/p2-zip-hardening-plan    — P2, PR #8, DRAFT, @ 15487e2
                 └─ claude/p7a-governance-ingestion-plan — P7A-PLAN, INGEN PR ÄNNU, FRYST, @ df597f2
```

| Branch | PR | Status | Scope | Bas | Blockerar merge på |
|---|---|---|---|---|---|
| `claude/founder-knowledge-studio-v1` | [#7](https://github.com/d1n095/LifeAI/pull/7) | Draft, granskad, godkänd som "färdig P1-leverans i draftläge" | P1: provider-förhandsverifiering (`app/providers/verification.py`, migration 0013, worker auto-requeue, admin-UI) | `claude/life-library-durable-worker-merged` @ 8cd926a | Väntar på grundarens merge-beslut |
| `claude/p2-zip-hardening-plan` | [#8](https://github.com/d1n095/LifeAI/pull/8) | Draft, granskad, godkänd som "färdig P2-leverans i draftläge" | P2: nästlad ZIP-hantering, `encrypted`-status, `archive_path`/`archive_chain` (`app/rag/zip_import.py`, `library_import.py`) | `claude/founder-knowledge-studio-v1` @ 5690aa2 (PR #7) | PR #7 måste mergas/uppdateras först (samma kedja); `Frontend — npm audit` gick tidigare rött av en OrelaterAD orsak — se PR #9 nedan |
| `claude/p7a-governance-ingestion-plan` | Ingen ännu | **Fryst** — planen godkänd på commit `df597f2`, ingen implementation påbörjad, inga fler ändringar tills nytt beslut | P7A-plan: `governance_documents`/`interpretation_proposals`, tidig ingestion av möjliga styrdokument (ingen aktivering) | `claude/p2-zip-hardening-plan` @ 15487e2 (PR #8) | Väntar på beslut att gå vidare till implementation |

**Beroende att komma ihåg:** eftersom varje branch i kedjan är grenad från föregåendes tip
(inte huvudgrenen), måste PR #7 mergas (eller sin bas uppdateras) INNAN PR #8 kan bli
konfliktfri mot en uppdaterad huvudgren, och PR #8 på samma sätt innan P7A:s implementation
kan börja på riktigt.

## Fristående, orelaterade fixar (grenade direkt från huvudgrenen)

Dessa rör INTE P1/P2/P7A-kedjan och ska inte blandas in i den — se `CLAUDE.md`s
grundprincip för varför de fick egna brancher/PR:er istället för att fogas in i en pågående.

| Branch | PR | Status | Scope | Bas |
|---|---|---|---|---|
| `claude/frontend-npm-audit-next-16-2-11` | [#9](https://github.com/d1n095/LifeAI/pull/9) | Öppen, **18/18 CI-checkar gröna**, `mergeable_state: clean`. Ej mergad. | `next` 16.2.10 → 16.2.11 (stänger `npm audit --audit-level=high`, 9 säkerhetsfixar, inga brytande ändringar) | `claude/det-kommer-mer-879lcm` @ a141065 |
| `claude/development-workflow-principles` | [#10](https://github.com/d1n095/LifeAI/pull/10) | Öppen, innehåll godkänt i sak. 16/18 CI-checkar gröna — de 2 som fallerar (`Frontend — npm audit`, `All required checks passed`) beror på samma ärvda, orelaterade problem som PR #9 löser (basen har ännu inte fixen). `mergeable_state: unstable`. Ej mergad, ska INTE uppdateras förrän PR #9 faktiskt mergat (se Merge-regeln). | Den här filen + `CLAUDE.md` — arbetsprinciper, inget applikationskod | `claude/det-kommer-mer-879lcm` @ a141065 |

**Efter att PR #9 mergas:** `claude/life-library-durable-worker-merged`/`claude/founder-knowledge-studio-v1`
(PR #7) och därefter `claude/p2-zip-hardening-plan` (PR #8) behöver var sin uppdatering
(rebase eller GitHubs "Update branch") mot den nya huvudgrenen för att själva ärva
npm-audit-fixen och bli gröna på den punkten — inte automatiskt, ett separat, senare steg.

## Merge-regeln (se `CLAUDE.md`)

**Ingen branch rebasas eller uppdateras i förväg "för säkerhets skull".** Rebase/"Update
branch" sker FÖRST när branchens faktiska beroende faktiskt har mergats, aldrig tidigare.
Avsnitten nedan skiljer uttryckligen på "väntar på ett beroende" (rör INTE branchen än) och
"kan mergas oberoende" (ingen väntan alls) — de är inte samma sak.

## Rekommenderad merge-ordning (just nu)

1. **PR #9** (dependency-fix) → `claude/det-kommer-mer-879lcm`. Inga beroenden, redan grön.
2. **PR #10** (den här filen + `CLAUDE.md`) → `claude/det-kommer-mer-879lcm`. Uppdateras mot
   ny huvudgren FÖRST efter att PR #9 faktiskt mergat (se Merge-regeln) — inte innan.
3. **PR #7** (P1) → sin bas. Uppdateras/rebasas mot ny huvudgren FÖRST efter att PR #9
   faktiskt mergat, för att ärva npm-audit-fixen — inte i förväg.
4. **PR #8** (P2) → sin bas (PR #7:s branch). Rebasas FÖRST efter att PR #7 faktiskt mergat
   — inte innan, även om PR #9 redan mergat.
5. **P7A** → implementation kan börja på `claude/p7a-governance-ingestion-plan` (redan rätt
   grenad) FÖRST efter att PR #8 faktiskt mergat OCH ett separat, uttryckligt beslut tagits
   (branchen är fryst just nu) — inte i förväg.

## Vilka brancher blockerar andra

- **PR #9 blockerar (CI-mässigt) att PR #10:s och PR #7:s egna `npm audit`-checkar blir
  gröna** — inte innehållsmässigt (PR #10/#7 rör ingen frontend-fil), men strukturellt tills
  de ärver fixen.
- **PR #7 blockerar PR #8** strukturellt — PR #8 är grenad från PR #7:s tip, så en ren,
  konfliktfri diff för PR #8 förutsätter att PR #7 är mergad eller stabil.
- **PR #8 blockerar P7A:s implementation-start** — samma strukturella skäl, P7A är grenad
  från PR #8:s tip.

## Vilka brancher kan mergas oberoende

- **PR #9** — helt oberoende. Kan mergas när som helst, väntar på ingenting.
- **PR #10** — innehållsmässigt oberoende av samtliga andra brancher (rör bara `CLAUDE.md`/
  `docs/BRANCH_REGISTRY.md`, ingen applikationskod). CI-gaten är dock delad med PR #9:s
  problem (se ovan) — själva MERGEN är inte beroende av något, men att bli GRÖN i CI är det.

## Vilka brancher väntar på ett beroende innan de bör uppdateras

Enligt Merge-regeln — dessa ska INTE röras förrän beroendet faktiskt är mergat, inte i
förväg:

- **PR #10** väntar på att **PR #9** mergar innan dess bas ska uppdateras.
- **PR #7** väntar på att **PR #9** mergar innan dess bas ska uppdateras (för att ärva
  npm-audit-fixen).
- **PR #8** väntar på att **PR #7** mergar innan dess bas ska rebasas.
- **P7A** väntar på att **PR #8** mergar OCH ett separat beslut innan implementation
  påbörjas.

## Konflikter

Inga kända filkonflikter mellan aktiva brancher just nu. `PR #9` och `PR #10` är båda
grenade direkt från `claude/det-kommer-mer-879lcm` @ a141065 och rör helt olika filer
(`frontend/package.json`/`package-lock.json` respektive `CLAUDE.md`/`docs/BRANCH_REGISTRY.md`)
— ingen konflikt mellan dem, oavsett mergeordning. Den enda kända, redan dokumenterade
beroendekonflikten är strukturell, inte en filkonflikt: P1→P2→P7A-kedjan måste mergas i sin
egen ordning (se "Rekommenderad merge-ordning" ovan) eftersom varje branch bygger på
föregåendes tip-commit.

Om en verklig filkonflikt upptäcks i framtiden ska den listas här explicit — vilka brancher,
vilka filer, och vilken lösning som föreslås — inte bara upptäckas i förbigående när en merge
misslyckas.

## Risk för dubbelarbete

Ingen känd, aktiv risk för dubbelarbete just nu — verifierat genom att jämföra varje aktiv
branch/PR:s scope (tabellerna ovan) mot varandra: inga två brancher bygger samma
funktionalitet parallellt. Den närmaste risken är strukturell: om P7A:s implementation
påbörjas innan PR #7/#8 mergats, riskerar den branchen (redan grenad från PR #8:s tip) att
behöva en omfattande rebase när PR #7/#8 väl mergas — därför är P7A medvetet fryst tills ett
uttryckligt beslut tas (se `claude/p7a-governance-ingestion-plan` i tabellen ovan).

Innan en ny branch/implementation påbörjas: jämför dess tilltänkta scope mot ALLA rader i
tabellerna ovan, inte bara den senaste. Om något överlappar, uppdatera det här avsnittet
INNAN arbetet påbörjas, inte efteråt.

## Stale/redan sammanslagna brancher (kandidater för städning, INTE raderade)

Verifierat via `git merge-base --is-ancestor` mot `claude/det-kommer-mer-879lcm` — dessa är
redan fullt innehållna i huvudgrenen. Listade här som referens, inte raderade utan explicit
tillåtelse (destruktiv åtgärd, se säkerhetsprotokollet):

`claude/fix-entrypoint-startup-race`, `claude/fix-pooler-auth-hardening`,
`claude/fix-render-public-port`, `claude/fix-supabase-pooler-role`,
`claude/fix-supabase-pooler-tenant-suffix`, `claude/founder-only-launch`,
`claude/integrate-founder-vps`, `claude/mainai-architecture-designs`,
`claude/night-shift-mainai-web`, `claude/render-service-name-fix`,
`claude/strato-vps-prep`, `claude/verify-combined-container`.

## Subsumerade i den aktiva kedjan (inte längre fristående)

Dessa branchars innehåll finns redan helt inom `claude/founder-knowledge-studio-v1` (P1)
högre upp i kedjan — de behöver inget eget beslut, bara noteras som vad de blev:

- `claude/life-library-upload-queue` → innehåll i → `claude/life-library-durable-worker` →
  innehåll i → `claude/life-library-durable-worker-merged` (PR #7:s bas-snapshot).

## Orphaned — kräver ett beslut, inte del av någon aktiv kedja

- `claude/fkp-v1.1` — 1 commit ("FKP v1.1: docs-only korrigering och integration av
  review-overlay + samtalsregister"), varken mergad i huvudgrenen eller del av P1/P2/P7A-
  kedjan. Status okänd tills grundaren tar ställning: merga, revidera, eller överge.

## Ej fullständigt granskade

Denna lista byggdes från en snabb `git merge-base`-genomgång av samtliga fjärrbranchar vid
tillfället ovan — den täcker ANCESTRY (är X en förfader till Y), inte innehållet i varje
branch i detalj. Om en branch saknas här, eller om något ser fel ut, uppdatera det här
dokumentet efter verifiering — gissa inte.
