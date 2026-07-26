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
| `claude/mainai-core-orchestration-v1` | [#16](https://github.com/d1n095/LifeAI/pull/16) | **Öppen, draft.** CI grönt (20 nya tester, 454/1 totalt, inga regressioner), `mergeable_state: clean`. Väntar på LLM Coupling & Failure-Boundary Audit-fixen nedan (minimal `ProviderError`-hantering i dispatch) innan draft-status kan lyftas | MainAI Core v0: kategoriserad retrieval + systemkarta, agentorkestrering (`agent_tasks`/`agent_task_events`, migration 0016), minimal GitHub-klient (läsning/branch/commit/PR, ALDRIG merge i klienten), founder-UI `/admin/agents`. Genomsökningsbaserad retrieval, inte semantisk | `claude/det-kommer-mer-879lcm` @ 7afb01f |

## LLM Coupling & Failure-Boundary Audit — pågående kedja (2026-07-26)

En extern audit av grundaren identifierade två verkliga fel — inte hypotetiska — där ett
AI-providerfel kunde få oavsiktliga konsekvenser för icke-AI-funktionalitet: (1) chatt
tappade det redan sparade användarmeddelandet om providern misslyckades efteråt, (2)
biblioteks-sökningen 500:ade helt om embedding-providern var otillgänglig trots att dess
textmatchningskanal inte behöver någon provider alls. Grundaren godkände audit-splitten och
skärpte båda kraven: PR A måste skilja explicit mellan "meddelande sparat" och "AI-svar
misslyckades" i kontraktet (inte en slentrianmässig 200:a), och PR B måste ha en RIKTIG
lokal fallback (inte bara fånga felet och returnera tomt). Ordning: PR A → PR B → minimal
dispatch-fix i PR #16 → PR C. Var och en är en egen branch/PR per grundprincipen — ingen av
dem blandas in i någon annan pågående kedja.

| Branch | PR | Status | Scope | Bas |
|---|---|---|---|---|
| `claude/chat-message-persistence-fix` | [#17](https://github.com/d1n095/LifeAI/pull/17) | **Öppen, redo för merge.** 18/18 CI grönt (inkl. E2E Playwright, E2E same-origin proxy, backend unit/integration, migrationskontroll), inga olösta review-kommentarer | PR A: användarmeddelandet persisteras och committas OBEROENDE av providerkallet; ny `MessageStatus`/`in_reply_to_id`/`error_category` på `Message`; `POST /messages/{id}/retry`; `ChatMessageOut` skiljer explicit `user_message_saved` från `assistant_status` (pending/succeeded/failed) + `retryable` + säker `error_category` (aldrig råa provider-secrets, se `classify_provider_exception`). Partial unique index gör dubbel-retry strukturellt omöjligt, inte bara logiskt undvikt. 8 nya tester (provider lyckas/misslyckas/timeout, retry dupliceras inte, reload behåller meddelandet, idempotent retry) | `claude/det-kommer-mer-879lcm` @ 7afb01f |
| `claude/search-embedding-failure-fallback` | [#18](https://github.com/d1n095/LifeAI/pull/18) | **Öppen, redo för merge.** 18/18 CI grönt, inga olösta review-kommentarer | PR B: `hybrid_search()` accepterar `vector: list[float] \| None` och hoppar över den semantiska kanalen (inte en fejkad nollvektor) när providern saknas — textmatchningskanalen (ILIKE) svarar alltid ändå. `search_library()` fångar embed-felet via samma `classify_provider_exception`. Ny `LibrarySearchResponseOut` med `semantic_search_available`/`degraded_reason` — aldrig en tyst påstådd semantisk sökning som bara "hittade inget". 4 nya tester (provider otillgänglig, timeout, tomt semantiskt resultat = INTE degraderat, normal hybrid-sökning) | `claude/det-kommer-mer-879lcm` @ 7afb01f |
| `claude/mainai-local-first-principle` | (docs-only, PR ej öppnad än) | Under arbete | Skriver ned grundarens arkitekturprincip "MainAI är systemets intelligens, inte en extern tjänst" i `docs/MAINAI_ARCHITECTURE.md` §1 (ny grundprincip 7 + System Core/MainAI/Externa modeller-ansvarstabell + explicit offline-kapacitetslista) och en korsreferens i §7. Ren dokumentation, ingen kodändring | `claude/det-kommer-mer-879lcm` @ 7afb01f |

**Kvar i kedjan (inte påbörjat):** PR #16:s minimala `ProviderError`-hantering i dispatch +
dokumentation av att retrieval redan är helt lokal / orkestrering är leverantörsoberoende men
INTE ännu local-first / Local MainAI Capability Layer är planerat, inte byggt (se
`docs/MAINAI_ARCHITECTURE.md`s nya grundprincip-avsnitt ovan) — görs direkt på PR #16:s
befintliga branch (`claude/mainai-core-orchestration-v1`), inte en ny branch, eftersom PR #16
redan äger dispatch-koden. Därefter PR C (`ai_analysis_status` + retry för den äldre
`/api/documents/upload`-vägen), egen ny branch.

## Merge-regeln (se `CLAUDE.md`)

**Ingen branch rebasas eller uppdateras i förväg "för säkerhets skull".** Rebase/"Update
branch" sker FÖRST när branchens faktiska beroende faktiskt har mergats, aldrig tidigare.
Avsnitten nedan skiljer uttryckligen på "väntar på ett beroende" (rör INTE branchen än) och
"kan mergas oberoende" (ingen väntan alls) — de är inte samma sak.

## Rekommenderad merge-ordning (nuläge)

1. ~~PR #9~~, ~~#11~~, ~~#10~~, ~~#7~~, ~~#8~~, ~~#14~~ — samtliga mergade i huvudgrenen.
2. **PR #17** (chat data-loss, PR A) → **PR #18** (search failure boundary, PR B) — i den
   ordningen enligt grundarens explicita instruktion, men de rör olika filer (chat.py/
   conversation.py/schemas.py respektive library.py/vector_store.py/schemas.py — endast
   `schemas.py` delas, och de lägger till olika, icke-överlappande klasser i den filen) så
   ordningen är en processfråga, inte en teknisk beroendekedja. Båda CI-gröna, redo för
   grundarens merge-beslut.
3. **PR #16:s minimala dispatch-fix** — görs EFTER att PR #17/#18 är mergade (samma
   `classify_provider_exception`-mönster återanvänds där; ingen anledning att duplicera
   innan de landat), på PR #16:s befintliga branch.
4. **PR C** (upload `ai_analysis_status` + retry) — efter PR #16:s fix, egen ny branch.
5. `claude/mainai-local-first-principle` (dokumentation) — fristående, ingen kodberoende,
   kan mergas när som helst i kedjan ovan utan att blockera eller blockeras av någon av dem.
6. **PR #13** (MainAI Project Memory-loopen) — bygger vidare, iterativt, direkt mot
   `claude/det-kommer-mer-879lcm`. Ingen merge-tidpunkt beslutad än — draft tills loopen är
   verifierad end-to-end.
7. **P7A** → implementation kan börja på `claude/p7a-governance-ingestion-plan` FÖRST efter
   ett separat, uttryckligt beslut (branchen är fryst). Kräver DESSUTOM en egen ombasering
   mot huvudgrenens nya tip innan aktivering — dess bas (`15487e2`) är nu långt bakom både
   P2:s slutliga tip och själva huvudgrenen.

## Vilka brancher blockerar andra

- **Inget öppet PR blockerar ett annat öppet PR just nu.** P1/P2 är i huvudgrenen; PR #13 är
  fristående ovanpå den.
- **PR #16:s dispatch-fix blockeras processmässigt** (inte tekniskt) av PR #17/#18 — se
  Merge-ordningen ovan för varför.
- **P7A:s egen aktivering blockeras** av både ett uttryckligt beslut och en ombasering (se
  ovan) — inte av något annat öppet PR.

## Vilka brancher kan mergas oberoende

**PR #17** och **PR #18** kan båda mergas oberoende av varandra rent tekniskt (ingen delar
någon rad kod, bara samma fil i olika sektioner) — ordningen mellan dem är grundarens
processval, inte ett tekniskt krav. **`claude/mainai-local-first-principle`** (docs-only) kan
mergas oberoende av allt ovan. **PR #13** kan i princip mergas oberoende när dess innehåll är
klart — den är redan grenad direkt från huvudgrenen (efter ombasering) och rör bara nya filer
(migration 0014, `app/project_memory.py`, `app/routers/memory.py`, tillhörande scheman/
tester). Väntar på att själva Project Memory-loopen blir funktionellt klar (se separat
sektion), inte på någon annan branch.

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
