# Branch-/PR-register — levande översikt

Detta dokument är den manuella motsvarigheten till vad MainAI själv ska kunna göra en dag
(se `CLAUDE.md` §3 och `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`). Det ska hållas
uppdaterat varje gång en branch/PR skapas, mergas, stängs eller fryses — se `CLAUDE.md` §2
för när.

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

Dessa rör INTE P1/P2/P7A-kedjan och ska inte blandas in i den — se `CLAUDE.md` §1 för
varför de fick egna brancher/PR:er istället för att fogas in i en pågående.

| Branch | PR | Status | Scope | Bas |
|---|---|---|---|---|
| `claude/frontend-npm-audit-next-16-2-11` | [#9](https://github.com/d1n095/LifeAI/pull/9) | Öppen, **18/18 CI-checkar gröna**, `mergeable_state: clean`. Ej mergad. | `next` 16.2.10 → 16.2.11 (stänger `npm audit --audit-level=high`, 9 säkerhetsfixar, inga brytande ändringar) | `claude/det-kommer-mer-879lcm` @ a141065 |
| `claude/development-workflow-principles` | Skapas i den här sessionen | Under uppbyggnad | Den här filen + `CLAUDE.md` — arbetsprinciper, inget applikationskod | `claude/det-kommer-mer-879lcm` @ a141065 |

**Efter att PR #9 mergas:** `claude/life-library-durable-worker-merged`/`claude/founder-knowledge-studio-v1`
(PR #7) och därefter `claude/p2-zip-hardening-plan` (PR #8) behöver var sin uppdatering
(rebase eller GitHubs "Update branch") mot den nya huvudgrenen för att själva ärva
npm-audit-fixen och bli gröna på den punkten — inte automatiskt, ett separat, senare steg.

## Rekommenderad merge-ordning (just nu)

1. **PR #9** (dependency-fix) → `claude/det-kommer-mer-879lcm`. Inga beroenden, redan grön.
2. **`claude/development-workflow-principles`** → `claude/det-kommer-mer-879lcm`. Inga
   beroenden, ren dokumentation.
3. **PR #7** (P1) → sin bas. Uppdatera/rebasa mot ny huvudgren först om PR #9 redan mergat,
   för att ärva npm-audit-fixen.
4. **PR #8** (P2) → sin bas (PR #7:s branch), efter att PR #7 mergat/uppdaterats.
5. **P7A** → implementation kan börja på `claude/p7a-governance-ingestion-plan` (redan rätt
   grenad) när PR #8 är i ett stabilt läge — men det kräver ett separat, uttryckligt beslut
   (branchen är fryst just nu).

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
