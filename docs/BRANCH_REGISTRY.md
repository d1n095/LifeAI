# Branch-/PR-register — projektets levande karta

Detta är INTE bara en lista över brancher — det är projektets levande karta, och den
manuella motsvarigheten till vad MainAI själv ska kunna göra en dag (se `CLAUDE.md`s
"Målet"-avsnitt och `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`). Den ska hållas uppdaterad
varje gång en branch/PR skapas, mergas, stängs eller fryses, eller när en konflikt/risk för
dubbelarbete upptäcks — se `CLAUDE.md`s "Branch Registry"-avsnitt för när.

**Senast verifierat mot faktiskt git-/GitHub-läge:** 2026-07-26, efter att hela den
verifierade mergekedjan (PR #9 → #11 → #10 → #7, PR #8 förberedd) genomfördes i en och samma
session (commit-SHA:er och PR-nummer nedan hämtade direkt via `git`/
`mcp__github__pull_request_read`, inte memorerade).

## Mergekedjan 2026-07-26 — genomförd

1. **PR #9** (`next` 16.2.10→16.2.11) → mergad i `claude/det-kommer-mer-879lcm`,
   merge-commit `0081e562`.
2. **PR #11** (`claude/frontend-npm-audit-brace-expansion`, NY, ej tidigare i registret) —
   upptäckt EFTER PR #9:s merge: en separat, orelaterad `npm audit`-blockerare
   (`brace-expansion`/GHSA-mh99-v99m-4gvg, 9 höga fynd, alla samma advisory). Ingen säker
   direktfix fanns (se `docs/SECURITY_BLOCKERS.md` punkt 3 för fullständig utredning) —
   löst med en liten, daterad, advisory-ID-specifik CI-allowlist
   (`frontend/scripts/check-npm-audit.js`). Mergad, merge-commit `6929b700`.
3. **PR #10** (`CLAUDE.md` + den här filen) → branch uppdaterad mot `6929b700`, CI grönt,
   mergad, merge-commit `403adc06`.
4. **PR #7** (P1) → sin bas (`claude/life-library-durable-worker-merged`) uppdaterades
   FÖRST (mergade in `403adc06`, ingen konflikt, ny bas-tip `6e54b23`), sedan uppdaterades
   PR #7:s egen branch mot den nya basen (ny head `909a5f1`), konverterad från draft till
   ready-for-review (GitHub tillåter inte merge av en draft-PR), CI grönt (18/18), mergad —
   merge-commit `16959661` **in i `claude/life-library-durable-worker-merged`, INTE in i
   huvudgrenen** (se strukturavsnittet nedan för vad det betyder).
5. **PR #8** (P2) → sin bas (`claude/founder-knowledge-studio-v1`, redan vid `909a5f1` sedan
   steg 4) — branchen uppdaterad (ny head `76640b01`), CI grönt (18/18,
   `mergeable_state: clean`), konverterad till ready-for-review. **Ej mergad**, per
   instruktion — väntar på grundarens explicita merge-beslut.

P7A rördes inte alls under kedjan, som instruerat — fortsatt fryst.

## Aktiv kedja: kunskapsimport (P1 → P2 → P7A) — nuläge

Byggordning enligt `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §8. Varje branch är grenad
från föregåendes tip-commit, inte från huvudgrenen direkt — så diffen i varje PR innehåller
bara den fasens eget innehåll. **Viktigt strukturellt faktum:** `claude/det-kommer-mer-879lcm`
har ÄNNU INTE fått P1/P2 mergade in i sig — kedjan nedan är fortfarande en egen gren som
själv nu innehåller huvudgrenens senaste commits (via steg 4 ovan), inte tvärtom.

```
claude/det-kommer-mer-879lcm (huvudgren, tip 403adc0 — PR #9, #11, #10 mergade)
  └─ claude/life-library-durable-worker-merged (uppdaterad @ 6e54b23 → PR #7 mergad in → tip 1695966)
       └─ claude/founder-knowledge-studio-v1  — P1, PR #7 MERGAD (i sin bas, ej huvudgrenen), @ 909a5f1
            └─ claude/p2-zip-hardening-plan    — P2, PR #8, Ready for review, CI grönt, EJ MERGAD, @ 76640b0
                 └─ claude/p7a-governance-ingestion-plan — P7A-PLAN, INGEN PR ÄNNU, FRYST, @ df597f2 (orörd)
```

| Branch | PR | Status | Scope | Bas | Nästa steg |
|---|---|---|---|---|---|
| `claude/life-library-durable-worker-merged` | Ingen egen PR | Uppdaterad @ `6e54b23`, sedan PR #7 mergad in → tip `1695966` | PR #6:s bas-snapshot + nu även huvudgrenens senaste (npm audit-fixar, CLAUDE.md/registry) | `claude/det-kommer-mer-879lcm` @ 403adc0 (mergad in, ej tvärtom) | Måste själv mergas/PR:as in i huvudgrenen innan P1:s kod når `claude/det-kommer-mer-879lcm` — INTE gjort ännu, inget explicit beslut om det heller |
| `claude/founder-knowledge-studio-v1` | [#7](https://github.com/d1n095/LifeAI/pull/7) | **Mergad** (i sin bas, se ovan), `state: closed`, `merged: true` | P1: provider-förhandsverifiering (`app/providers/verification.py`, migration 0013, worker auto-requeue, admin-UI) | `claude/life-library-durable-worker-merged` @ 6e54b23 | Klar — branchen kan städas när grundaren bekräftar |
| `claude/p2-zip-hardening-plan` | [#8](https://github.com/d1n095/LifeAI/pull/8) | Ready for review, **18/18 CI-checkar gröna**, `mergeable_state: clean`. **Ej mergad** — väntar på grundarens explicita beslut (instruerat att INTE merga automatiskt) | P2: nästlad ZIP-hantering, `encrypted`-status, `archive_path`/`archive_chain` (`app/rag/zip_import.py`, `library_import.py`) | `claude/founder-knowledge-studio-v1` @ 909a5f1 (PR #7, mergad) | Grundaren beslutar: merga PR #8 (in i `claude/founder-knowledge-studio-v1`, som i sin tur ännu inte är i huvudgrenen) |
| `claude/p7a-governance-ingestion-plan` | Ingen ännu | **Fryst**, orörd under hela kedjan — planen godkänd på commit `df597f2`, ingen implementation påbörjad | P7A-plan: `governance_documents`/`interpretation_proposals`, tidig ingestion av möjliga styrdokument (ingen aktivering) | `claude/p2-zip-hardening-plan` @ 15487e2 (obs: PR #8:s branch har rört sig till `76640b0` sedan dess — P7A är INTE ombasearad ännu) | Väntar på: PR #8 mergas OCH ett separat, uttryckligt beslut att börja implementation |

**Nytt öppet strukturellt beslut (inte del av den instruerade mergekedjan, upptäckt under
den):** `claude/life-library-durable-worker-merged` i sig är fortfarande INTE en del av
`claude/det-kommer-mer-879lcm`. Hela P1→P2-kedjans kod (även efter att PR #8 en dag mergas)
förblir isolerad från huvudgrenen tills någon uttryckligen beslutar att merga/PR:a
`claude/life-library-durable-worker-merged` (eller ett senare steg i kedjan) in i
`claude/det-kommer-mer-879lcm`. Ingen sådan merge har föreslagits eller genomförts än — ren
observation, inte en åtgärd som tagits.

## Fristående, orelaterade fixar (grenade direkt från huvudgrenen)

Dessa rör INTE P1/P2/P7A-kedjan och ska inte blandas in i den — se `CLAUDE.md`s
grundprincip för varför de fick egna brancher/PR:er istället för att fogas in i en pågående.

| Branch | PR | Status | Scope | Bas |
|---|---|---|---|---|
| `claude/frontend-npm-audit-next-16-2-11` | [#9](https://github.com/d1n095/LifeAI/pull/9) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `0081e562` | `next` 16.2.10 → 16.2.11 (stänger `npm audit --audit-level=high`, 9 säkerhetsfixar, inga brytande ändringar) | `claude/det-kommer-mer-879lcm` @ a141065 |
| `claude/frontend-npm-audit-brace-expansion` | [#11](https://github.com/d1n095/LifeAI/pull/11) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `6929b700` | `brace-expansion`/GHSA-mh99-v99m-4gvg — daterad, ID-specifik CI-allowlist (`frontend/scripts/check-npm-audit.js`), se `docs/SECURITY_BLOCKERS.md` punkt 3 | `claude/det-kommer-mer-879lcm` @ 0081e562 (efter PR #9) |
| `claude/development-workflow-principles` | [#10](https://github.com/d1n095/LifeAI/pull/10) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `403adc06` | `CLAUDE.md` + den här filen — arbetsprinciper, inget applikationskod | `claude/det-kommer-mer-879lcm` @ 6929b700 (efter PR #11) |

## Merge-regeln (se `CLAUDE.md`)

**Ingen branch rebasas eller uppdateras i förväg "för säkerhets skull".** Rebase/"Update
branch" sker FÖRST när branchens faktiska beroende faktiskt har mergats, aldrig tidigare.
Avsnitten nedan skiljer uttryckligen på "väntar på ett beroende" (rör INTE branchen än) och
"kan mergas oberoende" (ingen väntan alls) — de är inte samma sak.

## Rekommenderad merge-ordning (nuläge efter 2026-07-26-kedjan)

1. ~~PR #9~~ — mergad.
2. ~~PR #11~~ — mergad.
3. ~~PR #10~~ — mergad.
4. ~~PR #7~~ — mergad (i sin bas).
5. **PR #8** — Ready for review, CI grönt, `mergeable_state: clean`. Väntar ENDAST på
   grundarens explicita merge-beslut, inget tekniskt kvar.
6. **Nytt, ej tidigare registrerat beslut:** ska `claude/life-library-durable-worker-merged`
   (med P1, och senare P2 när PR #8 mergats) mergas/PR:as in i `claude/det-kommer-mer-879lcm`?
   Ingen sådan PR finns än. Utan den förblir hela P1/P2-kedjans kod isolerad från
   huvudgrenen oavsett vad som händer med PR #8.
7. **P7A** → implementation kan börja på `claude/p7a-governance-ingestion-plan` FÖRST efter
   att PR #8 faktiskt mergat OCH ett separat, uttryckligt beslut tagits (branchen är fryst
   just nu) — inte i förväg. Observera: P7A:s bas (`15487e2`) är inte längre PR #8:s aktuella
   tip (`76640b0`) — P7A behöver en egen ombasering när den väl aktiveras, utöver
   frysnings-beslutet.

## Vilka brancher blockerar andra

- **PR #8 blockerar P7A:s implementation-start** — P7A är grenad från PR #8:s (gamla) tip.
- **`claude/life-library-durable-worker-merged`s status mot huvudgrenen blockerar** att P1/P2
  någonsin når `claude/det-kommer-mer-879lcm` — oavsett PR #8:s öde, utan en egen
  merge/PR för detta steg stannar kedjans kod på sin egen gren.

## Vilka brancher kan mergas oberoende

Inga öppna brancher/PR:er kvar som är helt oberoende just nu — PR #9/#11/#10 (de tre som
var det) är alla mergade. PR #8 är tekniskt klar men väntar på ett grundarbeslut, inte på
någon annan branch.

## Vilka brancher väntar på ett beroende innan de bör uppdateras

Enligt Merge-regeln — dessa ska INTE röras förrän beroendet faktiskt är mergat, inte i
förväg:

- **P7A** väntar på att **PR #8** mergar OCH ett separat beslut innan implementation
  påbörjas (samt en egen ombasering, se ovan).
- **En framtida "merga P1/P2-kedjan till huvudgrenen"-branch/PR** väntar på PR #8:s beslut
  innan den ens bör övervägas — annars riskerar den att behöva göras om.

## Konflikter

Inga kända filkonflikter uppstod under 2026-07-26-mergekedjan — samtliga branch-uppdateringar
(`claude/life-library-durable-worker-merged` ← huvudgrenen, PR #7, PR #8) gick igenom som
rena, konfliktfria auto-merges (verifierat lokalt med `git merge --no-commit --no-ff` innan
push i det första fallet). Ingen aktiv filkonflikt just nu.

Om en verklig filkonflikt upptäcks i framtiden ska den listas här explicit — vilka brancher,
vilka filer, och vilken lösning som föreslås — inte bara upptäckas i förbigående när en merge
misslyckas.

## Risk för dubbelarbete

Ingen känd, aktiv risk för dubbelarbete just nu. Den strukturella risken som tidigare
flaggades här (P7A grenad från ett tip som skulle flytta sig) kvarstår i en ny form: P7A:s
bas (`15487e2`) är redan omkörd av PR #8:s aktuella tip (`76640b0`) — om P7A aktiveras utan
en egen ombasering först riskerar den att bygga vidare på en föråldrad P2-version. P7A
förblir fryst tills ett uttryckligt beslut tas.

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
`claude/development-workflow-principles` (PR #10, mergad).

**INTE i den här listan, trots att PR #7 mergat:** `claude/founder-knowledge-studio-v1` och
`claude/life-library-durable-worker-merged` är fortfarande INTE innehållna i
`claude/det-kommer-mer-879lcm` — PR #7:s merge gick in i `claude/life-library-durable-worker-merged`,
inte huvudgrenen (se "Aktiv kedja"-avsnittet ovan). Ta inte bort dessa brancher, de bär P1:s
enda väg till huvudgrenen just nu.

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
