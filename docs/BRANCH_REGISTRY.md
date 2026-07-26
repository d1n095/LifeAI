# Branch-/PR-register — projektets levande karta

Detta är INTE bara en lista över brancher — det är projektets levande karta, och den
manuella motsvarigheten till vad MainAI själv ska kunna göra en dag (se `CLAUDE.md`s
"Målet"-avsnitt och `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`). Den ska hållas uppdaterad
varje gång en branch/PR skapas, mergas, stängs eller fryses, eller när en konflikt/risk för
dubbelarbete upptäcks — se `CLAUDE.md`s "Branch Registry"-avsnitt för när.

**Senast verifierat mot faktiskt git-/GitHub-läge:** 2026-07-26 (andra passet samma dag),
efter att P1 och P2 uttryckligen integrerades i den verkliga huvudkedjan på grundarens
mandat (commit-SHA:er och PR-nummer nedan hämtade direkt via `git`/
`mcp__github__pull_request_read`, inte memorerade).

## Mergekedjan 2026-07-26 — genomförd i sin helhet

**Pass 1 (fristående fixar + processdokumentation):**
1. **PR #9** (`next` 16.2.10→16.2.11) → mergad, merge-commit `0081e562`.
2. **PR #11** (`brace-expansion`/GHSA-mh99-v99m-4gvg, ny CI-allowlist) → mergad, merge-commit
   `6929b700`. Se `docs/SECURITY_BLOCKERS.md` punkt 3.
3. **PR #10** (`CLAUDE.md` + den här filen) → mergad, merge-commit `403adc06`.

**Pass 2 (P1/P2-integration i huvudkedjan — grundarens explicita mandat):**
4. **PR #7** (P1) mergad i sin bas `claude/life-library-durable-worker-merged`
   (merge-commit `16959661`) — det steget skedde redan i pass 1:s förlängning.
5. `claude/life-library-durable-worker-merged` synkades mot huvudgrenens allra senaste tip
   (`5769cffa`, inkl. PR #12:s registeruppdatering) — ren merge, inga konflikter, ny tip
   `aa4d4b9`.
6. **PR #14** (ny, "Integrate Life Library durable worker + P1 into the main line") —
   `head: claude/life-library-durable-worker-merged` → `base: claude/det-kommer-mer-879lcm`.
   Innehåller BÅDE PR #6:s tidigare aldrig-mergade durable-worker-paket OCH P1, eftersom P1
   byggde direkt ovanpå PR #6:s bas-snapshot och de aldrig kan separeras utan
   historieomskrivning (uttryckligen undvikt). 18/18 CI grönt, `mergeable_state: clean`,
   **mergad** — merge-commit `2ddfeddc`. **Detta är första gången huvudgrenen någonsin
   innehållit P1 (eller PR #6).**
7. **PR #8** (P2) — verifierat via `git merge-base` att `909a5f1` (P1:s innehåll) nu är
   ancestor till den nya huvudgrenen, alltså att en ombasering INTE skulle ändra diffen.
   Bas ombasearad direkt till `claude/det-kommer-mer-879lcm` (utan mellansteg), diff
   bekräftat oförändrat (7 filer, +1225/-63), samma redan gröna CI-körning (huvudet
   oförändrat, `mergeable_state: clean`). **Mergad** — merge-commit `89682a18`.
8. **PR #13** (MainAI Project Memory-loopen, se separat sektion nedan) — bas ombasearad från
   `claude/p2-zip-hardening-plan` direkt till `claude/det-kommer-mer-879lcm` på samma sätt
   (verifierat via `git merge-base`, diff oförändrat: 8 filer, +835/-1). Fortsatt draft,
   fortsatt öppen.
9. Full lokal verifiering körd på den fullständigt integrerade koden (real Postgres+Redis i
   Docker): 411/411 tester gröna (346 backend + 65 security/account), inga regressioner.

P7A rördes inte — fortsatt fryst, se eget avsnitt nedan om dess nu ytterligare föråldrade bas.

## Huvudkedjans nuläge (efter integrationen)

```
claude/det-kommer-mer-879lcm (huvudgren, tip 89682a18)
  innehåller nu: PR #9, #11, #10, #12, PR #14 (durable worker + P1), PR #8 (P2)
  └─ claude/mainai-memory-loop-v1 — PR #13, draft, öppen, @ 0f7aac0
       (ombasearad direkt mot huvudgrenens nya tip, se tabellen nedan)

claude/p7a-governance-ingestion-plan — FRYST, INGEN PR, @ df597f2
  (grenad från en nu mycket föråldrad P2-tip, 15487e2 — inte 76640b0/89682a18)
```

**`claude/life-library-durable-worker-merged` och `claude/founder-knowledge-studio-v1`
(P1:s och PR #6:s branchar) är nu subsumerade** — deras innehåll finns i huvudgrenen via
PR #14, brancharna själva kan städas när grundaren bekräftar (inte gjort automatiskt, se
säkerhetsprotokollet mot destruktiva åtgärder). `claude/p2-zip-hardening-plan` är på samma
sätt subsumerad via PR #8.

## Fristående, orelaterade fixar (grenade direkt från huvudgrenen)

Dessa rör INTE P1/P2/P7A-kedjan och ska inte blandas in i den — se `CLAUDE.md`s
grundprincip för varför de fick egna brancher/PR:er istället för att fogas in i en pågående.

| Branch | PR | Status | Scope | Bas |
|---|---|---|---|---|
| `claude/frontend-npm-audit-next-16-2-11` | [#9](https://github.com/d1n095/LifeAI/pull/9) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `0081e562` | `next` 16.2.10 → 16.2.11 (stänger `npm audit --audit-level=high`, 9 säkerhetsfixar, inga brytande ändringar) | `claude/det-kommer-mer-879lcm` @ a141065 |
| `claude/frontend-npm-audit-brace-expansion` | [#11](https://github.com/d1n095/LifeAI/pull/11) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `6929b700` | `brace-expansion`/GHSA-mh99-v99m-4gvg — daterad, ID-specifik CI-allowlist (`frontend/scripts/check-npm-audit.js`), se `docs/SECURITY_BLOCKERS.md` punkt 3 | `claude/det-kommer-mer-879lcm` @ 0081e562 (efter PR #9) |
| `claude/development-workflow-principles` | [#10](https://github.com/d1n095/LifeAI/pull/10) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `403adc06` | `CLAUDE.md` + den här filen — arbetsprinciper, inget applikationskod | `claude/det-kommer-mer-879lcm` @ 6929b700 (efter PR #11) |
| `claude/branch-registry-post-merge-chain-update` | [#12](https://github.com/d1n095/LifeAI/pull/12) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `5769cffa` | Registerpost-mergekedja | `claude/det-kommer-mer-879lcm` @ 403adc06 |
| `claude/life-library-durable-worker-merged` | [#14](https://github.com/d1n095/LifeAI/pull/14) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `2ddfeddc` | PR #6 (durable worker/lagring) + P1 (provider-verifiering) — landade i huvudgrenen för första gången | `claude/det-kommer-mer-879lcm` @ 5769cffa |
| `claude/p2-zip-hardening-plan` | [#8](https://github.com/d1n095/LifeAI/pull/8) | **Mergad** i `claude/det-kommer-mer-879lcm` (ombasearad dit efter PR #14), merge-commit `89682a18` | P2: nästlad ZIP-hantering, `encrypted`-status, `archive_path`/`archive_chain` | `claude/det-kommer-mer-879lcm` @ 2ddfeddc (efter PR #14) |
| `claude/mainai-memory-loop-v1` | [#13](https://github.com/d1n095/LifeAI/pull/13) | **Öppen, draft.** CI grönt på ursprunglig bas; ombasearad direkt mot huvudgrenens nya tip (diff oförändrat, verifierat via `git merge-base`) | MainAI Project Memory & Coordination Loop — se `CLAUDE.md`s "Målet"-avsnitt. Byggs vidare iterativt, inte redo för merge-beslut än | `claude/det-kommer-mer-879lcm` @ 89682a18 |

## Merge-regeln (se `CLAUDE.md`)

**Ingen branch rebasas eller uppdateras i förväg "för säkerhets skull".** Rebase/"Update
branch" sker FÖRST när branchens faktiska beroende faktiskt har mergats, aldrig tidigare.
Avsnitten nedan skiljer uttryckligen på "väntar på ett beroende" (rör INTE branchen än) och
"kan mergas oberoende" (ingen väntan alls) — de är inte samma sak.

## Rekommenderad merge-ordning (nuläge)

1. ~~PR #9~~, ~~#11~~, ~~#10~~, ~~#7~~, ~~#8~~, ~~#14~~ — samtliga mergade i huvudgrenen.
2. **PR #13** (MainAI Project Memory-loopen) — bygger vidare, iterativt, direkt mot
   `claude/det-kommer-mer-879lcm`. Ingen merge-tidpunkt beslutad än — draft tills loopen är
   verifierad end-to-end.
3. **P7A** → implementation kan börja på `claude/p7a-governance-ingestion-plan` FÖRST efter
   ett separat, uttryckligt beslut (branchen är fryst). Kräver DESSUTOM en egen ombasering
   mot huvudgrenens nya tip innan aktivering — dess bas (`15487e2`) är nu långt bakom både
   P2:s slutliga tip och själva huvudgrenen.

## Vilka brancher blockerar andra

- **Inget öppet PR blockerar ett annat öppet PR just nu.** P1/P2 är i huvudgrenen; PR #13 är
  fristående ovanpå den.
- **P7A:s egen aktivering blockeras** av både ett uttryckligt beslut och en ombasering (se
  ovan) — inte av något annat öppet PR.

## Vilka brancher kan mergas oberoende

**PR #13** kan i princip mergas oberoende när dess innehåll är klart — den är redan grenad
direkt från huvudgrenen (efter ombasering) och rör bara nya filer (migration 0014,
`app/project_memory.py`, `app/routers/memory.py`, tillhörande scheman/tester). Väntar på att
själva Project Memory-loopen blir funktionellt klar (se separat sektion), inte på någon
annan branch.

## Vilka brancher väntar på ett beroende innan de bör uppdateras

Enligt Merge-regeln — dessa ska INTE röras förrän beroendet faktiskt är mergat, inte i
förväg:

- **P7A** väntar på ett separat, uttryckligt beslut om att börja implementation, plus sin
  egen ombasering när det beslutet tas — inte på något annat öppet PR just nu.

## Konflikter

Inga kända filkonflikter. Samtliga integrationssteg (branch-synk, PR #14, PR #8:s
ombasering, PR #13:s ombasering) verifierades konfliktfria via `git merge-base` innan de
utfördes — se "Mergekedjan"-sektionen ovan för detaljer per steg.

Om en verklig filkonflikt upptäcks i framtiden ska den listas här explicit — vilka brancher,
vilka filer, och vilken lösning som föreslås — inte bara upptäckas i förbigående när en merge
misslyckas.

## Risk för dubbelarbete

Ingen känd, aktiv risk för dubbelarbete just nu. P7A:s bas är föråldrad (se ovan) men
branchen är fryst, så ingen aktiv utvecklingsrisk finns förrän ett beslut tas att återuppta
den — vid det laget måste den ombaseras mot huvudgrenens då-aktuella tip, inte mot P2:s
gamla tip.

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
`claude/strato-vps-prep`, `claude/verify-combined-container`,
`claude/frontend-npm-audit-next-16-2-11` (PR #9, mergad),
`claude/frontend-npm-audit-brace-expansion` (PR #11, mergad),
`claude/development-workflow-principles` (PR #10, mergad),
`claude/life-library-durable-worker-merged` (PR #14, mergad — bar PR #6 + P1 in i huvudgrenen),
`claude/founder-knowledge-studio-v1` (PR #7's head, subsumerad via PR #14),
`claude/p2-zip-hardening-plan` (PR #8, mergad).

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
