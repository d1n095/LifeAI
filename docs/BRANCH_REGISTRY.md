# Branch-/PR-register — projektets levande karta

Detta är INTE bara en lista över brancher — det är projektets levande karta, och den
manuella motsvarigheten till vad MainAI själv ska kunna göra en dag (se `CLAUDE.md`s
"Målet"-avsnitt och `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`). Den ska hållas uppdaterad
varje gång en branch/PR skapas, mergas, stängs eller fryses, eller när en konflikt/risk för
dubbelarbete upptäcks — se `CLAUDE.md`s "Branch Registry"-avsnitt för när.

**Senast verifierat mot faktiskt git-/GitHub-läge:** 2026-07-26 (tredje passet samma dag),
efter att PR #13 (MainAI Project Memory & Coordination Loop, Fas 1–4) mergades och arbetet
utökades till MainAI Core-orkestrering på grundarens uttryckliga mandat (commit-SHA:er och
PR-nummer nedan hämtade direkt via `git`/`mcp__github__pull_request_read`, inte memorerade).

## Pass 3 (2026-07-26): PR #13 mergad, MainAI Core-orkestrering påbörjad

9. **PR #13** (MainAI Project Memory & Coordination Loop, Fas 1–4) — 18/18 CI grönt, markerad
   ready-for-review och **mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `7afb01f`.
   Detta är första gången huvudgrenen innehåller MainAI:s eget projektminne (`project_notes`,
   `project_checkpoints`, `project_sources`, `project_branch_pr_status`) och den founder-only
   `/admin/memory`-vyn.
10. Grundaren utökade uppdraget samma dag från "bygg ett checkpointsystem" till "bygg första
    operativa kärnan av MainAI" — se `CLAUDE.md`s "MainAI Core"-riktning: konversations-/
    kunskapsretrieval, en lätt systemkarta, agentorkestrering (kod-/granskningsagent via
    befintliga provider-adaptrar), och en GitHub-integration med verklig skriv-behörighet
    bakom hårda säkerhetsgrindar (ingen merge-kapacitet implementerad alls i denna fas).
11. **`claude/mainai-core-orchestration-v1`** grenad direkt från huvudgrenens nya tip
    (`7afb01f`, alltså EFTER att PR #13 mergat — inte i förväg, se Merge-regeln). Bygger:
    migration 0016 (`agent_tasks`/`agent_task_events`), `app/agent_orchestration.py`,
    `app/integrations/github_client.py` (read/branch/commit/PR — medvetet ingen merge-metod),
    `retrieve_relevant_context()`/systemkarta i `app/project_memory.py`, ny `NoteKind.idea`,
    founder-UI `/admin/agents`. Se separat sektion nedan för scope och verifiering.

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

## Huvudkedjans nuläge (efter PR #13)

```
claude/det-kommer-mer-879lcm (huvudgren, tip 7afb01f)
  innehåller nu: PR #9, #11, #10, #12, PR #14 (durable worker + P1), PR #8 (P2), PR #13
  (MainAI Project Memory & Coordination Loop, Fas 1-4)
  └─ claude/mainai-core-orchestration-v1 — MainAI Core-orkestrering, öppen (ingen PR
       skapad än vid senaste verifiering), @ se lokal branch
       (grenad EFTER PR #13:s merge, inte i förväg — se Merge-regeln)

claude/p7a-governance-ingestion-plan — FRYST, INGEN PR, @ df597f2
  (grenad från en nu mycket föråldrad P2-tip, 15487e2 — inte 7afb01f)
```

**`claude/life-library-durable-worker-merged`, `claude/founder-knowledge-studio-v1`,
`claude/p2-zip-hardening-plan` och `claude/mainai-memory-loop-v1` är nu subsumerade** —
deras innehåll finns i huvudgrenen via PR #14/#8/#13. Brancharna själva kan städas när
grundaren bekräftar (inte gjort automatiskt, se säkerhetsprotokollet mot destruktiva
åtgärder).

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
| `claude/mainai-memory-loop-v1` | [#13](https://github.com/d1n095/LifeAI/pull/13) | **Mergad** i `claude/det-kommer-mer-879lcm`, merge-commit `7afb01f`. 18/18 CI grönt. | MainAI Project Memory & Coordination Loop, Fas 1–4: `project_notes`/`project_checkpoints`/`project_sources`/`project_branch_pr_status`, resumption-brief, founder-UI `/admin/memory` | `claude/det-kommer-mer-879lcm` @ 89682a18 |
| `claude/mainai-core-orchestration-v1` | Ingen ännu | **Öppen, ej PR ännu vid senaste verifiering.** Full lokal testsvit grön (454 tester), migrationsrundtripp verifierad, frontend build/tsc/eslint gröna, UI verifierad i riktig webbläsare. | MainAI Core: konversations-/kunskapsretrieval (`retrieve_relevant_context`), lätt systemkarta (`build_system_map`/`ingest_system_map`), ny `NoteKind.idea`, agentorkestrering (migration 0016: `agent_tasks`/`agent_task_events`, `app/agent_orchestration.py`, `app/integrations/github_client.py`), founder-UI `/admin/agents`. Se separat scope-sektion nedan. | `claude/det-kommer-mer-879lcm` @ 7afb01f (grenad EFTER PR #13:s merge) |

## MainAI Core: agentorkestrering (`claude/mainai-core-orchestration-v1`) — scope och verifiering

Bygger vidare på Fas 1–4 (nu i huvudgrenen), enligt CLAUDE.md's 2026-07-26 "MainAI Core"-
riktning: inte bara ett checkpointsystem, utan första vertikala kedjan samtal → minne →
helhetsbild → problem → agentuppdrag → kod → granskning → GitHub-PR → checkpoint.

**Byggt i denna omgång:**
- `retrieve_relevant_context()` — kategoriserad, relevans-rankad hämtning (heuristisk
  token-overlap, samma metod som `detect_conflicts_and_duplicates()` redan använder) i
  stället för att dumpa hela minnet. Skiljer uttryckligen `verifierade_fakta_och_status` /
  `grundarens_beslut` / `ej_beslutade_ideer` (ny `NoteKind.idea`) / `blockerare` /
  `nasta_steg` / `osakra_eller_motstridiga` / `historik`.
- `build_system_map()`/`ingest_system_map()` — lätt, textbaserad skanning av routers/
  modeller/migrationer/frontend-routes under `PROJECT_ROOT`, lagrad via samma
  content-addressed storage som allt annat i modulen (`ProjectSource(source_type="system_map")`).
  Medvetet smal denna omgång — dupliceras inte mot redan befintlig admin-status
  (`/api/admin/library/ops`, `/api/admin/providers`).
- Migration 0016: `agent_tasks` (ett avgränsat uppdrag — titel, filer, begränsningar,
  acceptanskriterier, krävda tester) + `agent_task_events` (append-only historik: dispatch,
  resultat, testresultat, granskning, GitHub-operationer).
- `app/agent_orchestration.py` — `create_agent_task`/`dispatch_task` (kodagent via befintlig
  `chat_with_fallback`)/`record_test_results`/`review_task` (granskningsagent, BLOCKERAD utan
  registrerade testresultat, kan aldrig godkänna på röda tester även om modellen säger
  "approved")/`prepare_github_pr`/`attempt_auto_merge` (ALLTID blockerad — se nedan).
- `app/integrations/github_client.py` — minimal REST-klient (läsning, branch, commit, PR).
  **Ingen merge-metod finns i klienten alls** — inte bara avstängd bakom en flagga, genuint
  frånvarande som kod. `github_write_enabled` (default `False`) styr om `prepare_github_pr()`
  bara FÖRESLÅR exakt PR-innehåll (branch/commit/PR-text, ingen GitHub-anrop) eller faktiskt
  skapar branch/commit/PR. `github_auto_merge_enabled` finns som konfigurationsflagga men
  gatear ingen faktisk kapacitet ännu.
- Founder-UI `/admin/agents` — uppdragslista, detaljvy med händelsehistorik, knappar för
  varje steg i kedjan. Verifierat i riktig Chromium-webbläsare mot en riktig backend: login,
  uppdragsskapande, dispatch (korrekt, ren felyta när ingen provider-nyckel är konfigurerad —
  exakt samma beteende som resten av appen), och det alltid-blockerade merge-försöket.

**Explicit avgränsat bort denna omgång** (registrerat, inte bortglömt):
- Verklig GitHub-skrivbehörighet är byggd och testad (mockad HTTP-nivå) men aldrig körd mot
  ett riktigt repo i denna session — kräver en riktig `GITHUB_TOKEN` som grundaren
  provisionerar separat (samma mönster som providernycklarna, aldrig i chatten).
- Semantisk (embedding-baserad) retrieval — nuvarande implementation är medvetet en
  heuristik, inte en ny vektorpipeline för noteringar.
- Hela "Autonomous Verification & Interaction Layer" (webbläsarstyrd funktionstestning,
  persona-simulering, digital tvilling av grundaren) — en betydligt större, separat
  initiativ som inte påbörjats.

**Verifiering:** 15 nya tester i `test_agent_orchestration.py` (inkl. ett fullständigt
vertikalt bevis: note → task → dispatch → test → review → PR-förslag → checkpoint → kall
läsning), 5 nya i `test_project_memory.py` (retrieval + systemkarta). Full befintlig svit
omkörd: 454 gröna, 1 medvetet skippad, inga regressioner. Migrationsrundtripp (0013→0016→
nedgradering×2→uppgradering) verifierad fristående. Frontend: `tsc`/`eslint`/`next build`
gröna, UI verifierad i riktig webbläsare mot riktig backend+Postgres.

## Merge-regeln (se `CLAUDE.md`)

**Ingen branch rebasas eller uppdateras i förväg "för säkerhets skull".** Rebase/"Update
branch" sker FÖRST när branchens faktiska beroende faktiskt har mergats, aldrig tidigare.
Avsnitten nedan skiljer uttryckligen på "väntar på ett beroende" (rör INTE branchen än) och
"kan mergas oberoende" (ingen väntan alls) — de är inte samma sak.

## Rekommenderad merge-ordning (nuläge)

1. ~~PR #9~~, ~~#11~~, ~~#10~~, ~~#7~~, ~~#8~~, ~~#14~~, ~~#13~~ — samtliga mergade i
   huvudgrenen.
2. **`claude/mainai-core-orchestration-v1`** (MainAI Core: retrieval, systemkarta,
   agentorkestrering, GitHub-integration) — öppen mot `claude/det-kommer-mer-879lcm`, redo
   för PR/granskning. Ingen merge-tidpunkt beslutad än.
3. **P7A** → implementation kan börja på `claude/p7a-governance-ingestion-plan` FÖRST efter
   ett separat, uttryckligt beslut (branchen är fryst). Kräver DESSUTOM en egen ombasering
   mot huvudgrenens nya tip innan aktivering — dess bas (`15487e2`) är nu långt bakom både
   P2:s slutliga tip och själva huvudgrenen.

## Vilka brancher blockerar andra

- **Inget öppet PR blockerar ett annat öppet PR just nu.** P1/P2/PR #13 är i huvudgrenen;
  `claude/mainai-core-orchestration-v1` är fristående ovanpå den.
- **P7A:s egen aktivering blockeras** av både ett uttryckligt beslut och en ombasering (se
  ovan) — inte av något annat öppet PR.

## Vilka brancher kan mergas oberoende

**`claude/mainai-core-orchestration-v1`** kan i princip mergas oberoende när den granskats —
den är grenad direkt från huvudgrenens nya tip (EFTER PR #13:s merge) och rör bara nya filer
plus additiva utökningar av redan mergad Fas 1–4-kod (ny migration 0016, ny `NoteKind.idea`,
nya routers/moduler). Väntar inte på någon annan öppen branch.

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
`claude/p2-zip-hardening-plan` (PR #8, mergad),
`claude/mainai-memory-loop-v1` (PR #13, mergad).

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
