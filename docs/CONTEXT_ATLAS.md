# Context Atlas — källidentitet, systemkarta, spårbarhet och beslutsstöd

**Branch:** `freebuff/context-atlas-20261124`
**Bas:** `8c7b335e87071b08a60f4c30075ff29c2a7b8c94` (`freebuff/life-workspaces`, lokal)
**Status:** fas 0–5 implementerade och verifierade, plus fas 2-utökning (path-scoped
arbetsägande och glömda krav) och competitor-research. **92 tester** passerar. Ingen push, PR,
merge eller deploy.
`first_cycle_go=false`. Inga VPS-tjänster, produktionsresurser eller
`security/POLICY_LOCK.json` är rörda.

## 1. Vad detta är — och inte är

En läs-only analysyta ovanpå det som redan finns:

- `Document.checksum` (sha256, indexerad) och content-addressed storage i `app/storage/`,
- `KnowledgeVersion` för versionslinjer,
- `SourceRelationship` för inspelade kanter,
- `app/rag/trust.py` som enda tolkare av `duplicates`/`contradicts` vid retrieval,
- checkoutens egen importgraf och Alembic-historik.

Detta är **inte** ett andra minne, en andra vektorindex, en andra kö, en andra
orkestreringsmotor eller en autonom agentdispatcher. Modulerna skriver inga rader, anropar
varken nätverk eller LLM, och varje emitterat beslut har `execution_authorized: false`.

## 2. Fas 0 — leverans och integrationsåterhämtning

### Inventering

| Ref | SHA | Läge |
|---|---|---|
| Atlas-bas | `8c7b335e87071b08a60f4c30075ff29c2a7b8c94` | lokal branch, finns **inte** på origin |
| Integrationshead | `0adb108fa5145ad6b320950fb029bfaf96f003b4` | finns på origin; är ancestor till atlas-basen |
| PR #499-head | `0659f4540be44666f88183466418d3d5c1165402` | publicerad; divergerar från atlas-basen |
| PR #500 | `14eb5f6df1e93f73068cdfe684c758c700a77a71` | öppen draft, 1 commit före `0659f454` |
| PR #501 | `1e6487b5c3e874315bb64715f1f1688e1fd2b80e` | öppen draft, 2 commits före `0659f454` |
| PR #502 | `d298936132b739cc016fb5365cec98fb862b2e43` | öppen draft, 3 commits före `0659f454` |
| PR #503 | `93640a279c58e26c2659ee7d3039efd12462c772` | öppen draft, dokumentation-only |
| PR #504 | `3985b48ccbe9de2a2ad7402865c1f79c5ff9d60f` | öppen draft, 4 commits före `0659f454` |
| PR #505 | `003c1b6efe1e5e533446edc2dbac493006211f3d` | öppen draft (fas A), 1 commit efter #504 |
| PR #506 | `164343a703190958e4be932481d5585f9b1b2afa` | öppen draft (fas B), 2 commits efter #504 |

Mätt med GitHub compare från `0659f454`:

```
integration: behind ahead=0 behind=115 merge_base=0adb108f
PR 500:      ahead  ahead=1 behind=0   merge_base=0659f454
PR 501:      ahead  ahead=2 behind=0   merge_base=0659f454
PR 502:      ahead  ahead=3 behind=0   merge_base=0659f454
PR 503:      ahead  ahead=2 behind=0   merge_base=0659f454
PR 504:      ahead  ahead=4 behind=0   merge_base=0659f454
```

Atlas-basen innehåller alltså integrationsgrenens head men **inte** PR #499-head.
Divergensen mellan `0659f454` och atlas-basen är 75 commits på #499-sidan och 63 på
atlas-sidan, med merge-base `86671468a6dd271b61a3f985434130c4bf470ea8`. Eftersom atlasen
endast lägger till nya filer (plus registry-posten) finns **ingen filöverlapp** med
#499–#506 (mätt med `git diff --name-only` mot varje PR-head). Den semantiska överlappen med
**#501 (document intelligence)** måste ändå prövas före merge: om #501 redan täcker
near-duplicate-detektion ska atlasens identitetsmodul ersättas, inte dubbleras. #502 lägger
till en bred founder-vy över agentkoordinering (`founder_intelligence.py`) och #505/#506 rör
live-prompten; atlasens `ownership.py` svarar en snävare fråga (path-scope för ett konkret
förslag) och återanvänder samma tabeller, så den ersätter dem inte.

### Transferpaket

Den tidigare opushade stacken (4 brancher, 6 commits ovanpå `0659f454`) ligger kvar som en
verifierad bundle:

```
/tmp/context-atlas-transfer/commit-stack.bundle   9 385 188 byte
sha256 d4c44692995eca3aec7fa780cf5364f372ca0da94ecc76ef08464cb898c50426
verifierad i isolerad klon: 4/4 brancher, SHA + tree-hash + git fsck
```

Atlas-branchen exporteras som en **egen** bundle (`atlas/commit-stack.bundle`, bas
`8c7b335e`) i samma katalog, så att de två linjerna aldrig tvingas in i en gemensam bas.
`manifest.json` + `SHA256SUMS` skrivs per bundle. Skriptet vägrar en dirty tracked tree,
kräver ancestry mot angiven bas och skannar nya commits efter hemlighetsmönster.

### Publicering

`git push --dry-run origin freebuff/context-atlas-20261124` → `fatal: could not read
Username for 'https://github.com': terminal prompts disabled` (exit 128). Ingen credential
helper, ingen token, ingen `gh`-CLI. **Publicering är blockerad** och agenten begär inga
secrets. Säker väg: en människa puschar bundlen eller importerar den via den befintliga
V8-vägen (`scripts/integration/`) och öppnar draft-PR själv.

## 3. Fas 1 — enhetlig källidentitet

`identity.py` klassificerar två payloads i en strikt ordning:

| Verdict | Auktoritativ | Föreslår `duplicates` |
|---|---|---|
| `exact_byte_identical` | **ja** | ja |
| `normalized_identical` (whitespace/Unicode/interpunktion) | nej | nej |
| `one_character_edit` | nej | nej |
| `near_duplicate` (Jaccard ≥ 0,9) | nej | nej |
| `similar` (Jaccard ≥ 0,4) | nej | nej |
| `distinct` | nej | nej |
| `not_comparable` (binär/ogiltig UTF-8/över 8 MiB) | nej | nej |

`preserve_originals` är alltid `True`. Jämförelsen är bounded: 8 MiB textmetriker, 200 000
tecken edit-distance, 20 000 tokens.

`candidates.py` kopplar detta till den riktiga dokumentvägen:

- **exakt identitet** slås upp via det indexerade `documents.checksum`,
- **relaterade kandidater** hämtas från ett bounded storleksfönster (±50 %, minst 256 byte)
  och läses från `app/storage/` via `open_read`,
- `limit` och `max_scan` är hårda gränser; kostnaden är proportionell mot `max_scan`, aldrig
  mot korpusstorleken,
- varje fråga filtrerar `uploaded_by` och körs under samma FORCE RLS som resten av appen,
- saknad/korrupt blob ger `unreadable_originals`, inte en krasch,
- ingen `source_relationship`-rad skrivs; likhet blir aldrig identitet.

Mätt integration (riktig Postgres, riktig RLS, riktig storage): 9 tester, inklusive
tvärtenant-nekande (ägare A ser aldrig B:s identiska dokument; utan bunden användare blir
resultatet noll rader) och stabilitet efter omstart (ny session, samma rapport, oförändrat
radantal).

## 4. Fas 2 — självupptäckande systemkarta

`system_map.py` skiljer uttryckligen på sju tillstånd:

| Tillstånd | Betydelse | Källa |
|---|---|---|
| `present` | filen/klassen finns | checkouten |
| `wired` | router inkopplad i `main.py`, eller modulen/paketet importeras | statisk importgraf |
| `test_referenced` | en testfil namnger komponenten | textmatchning |
| `reviewed` | **okänt** utan granskningsbevis | — |
| `integrated` | **okänt** utan merge/ancestry-bevis | — |
| `activated` | **okänt** utan runtime-konfigurationsbevis | — |
| `observed` | **okänt** utan verkligt runtime-bevis | — |

De fyra sista är `None`, inte `False`: frånvaro av bevis är inte bevis på frånvaro. En
checkout kan aldrig bevisa dem.

Mätt på denna commit:

```
moduler 418   wired 403   test_referenced 402
endpoints 220 (founder 177, user 7, none 1, unknown 35)
routers 30    events 21   workers 1   migrationer 94   head 0095   testfiler 327
```

`change_impact(module)` ger direkta och transitiva beroenden (djup 3, tak 50), endpoints,
tester, modellberoenden, migrationsnot och säkerhetsnoter. Exempel för `app.rag.trust`:

```
6 direkta: app.rag.claim_graph, app.rag.epistemics, app.routers.blueprint,
           app.routers.chat, app.routers.library, app.routers.workbench
5 transitiva, 31 endpoints, 8 testfiler
```

Kartan påstår aldrig att en inkopplad router fungerar, att en namngiven test täcker
beteendet, eller att en lokal branch är mergad. Git-evidens är separat: `git_evidence.py`
läser lokala refs via `git` och normaliserar en **av människa fångad** PR-JSON; koden gör
inga nätverksanrop.

### 4.1 Arbetsägande — vem har redan den här ytan?

`ownership.py` svarar på en snävare fråga än `app.agent_coordination.founder_intelligence`:
inte "vad gör varje agent", utan "har en annan icke-terminal assignment redan exakt den path
som den här ändringen skulle röra?". Den återanvänder `agent_work_assignments`,
`agent_scope_leases` och koordinatorns egen `paths_conflict_any` (directory-boundary, inte rå
strängprefix). Inga tabeller, migrationer, leases eller writes tillkommer.

| Rapportstatus | Betydelse |
|---|---|
| `owned` | minst en icke-terminal assignment täcker en efterfrågad path |
| `clear` | ingen icke-terminal assignment överlappar |
| `unclear` | assignment finns men saknar path-scope; granska manuellt |
| `unknown` | inga paths efterfrågades; ägande kan inte härledas |

Terminala statusar (completed/failed/cancelled/superseded) är aldrig ägare. En utgången lease
rapporteras utan `lease_generation`, även om raden fortfarande står som `active` innan sweeps.
En `planned` assignment med live lease beskrivs aldrig som "running". Tvärtenant filtreras både
av `owner_id` och av anroparens RLS-scope. `report.evidence_refs()` matas in i
`EngineeringRequest.active_assignments`, vilket ger WAIT i stället för dubblettarbete.

Mätt med riktig Postgres och riktiga koordineringstabeller: 8 tester (path-överlapp, lease,
directory-boundary, terminal, utgången lease, unscoped, tom begäran, tvärtenant, read-only).

## 5. Fas 3 — historisk kontext och kravavstämning

`traceability.py` modellerar kedjan

```
VISION → REQUIREMENT → DECISION → ARCHITECTURE → IMPLEMENTATION
       → TEST → REVIEW → INTEGRATION → RUNTIME EVIDENCE
```

Varje steg är `evidenced` eller `missing`; `missing_stages` namnges. Klassificeringen härleds
från bevisen (`historical_suggestion`, `open_question`, `proposed_design`,
`founder_approved_decision`, `superseded_decision`, `implemented_capability`,
`verified_capability`, `active_work`, `rejected_approach`, `unknown`).

`canonical_truth` är alltid `None`. Ingen historisk källa befordras automatiskt till gällande
sanning. `reconcile_disagreement()` bevarar båda originalen ordagrant, anger projektomfång,
osäkerhet och en **möjlig** supersession (tidsstämpel eller explicit referens) — aldrig en
vinnare.

### 5.1 Glömda krav

`forgotten_requirements(traces)` returnerar spår som har avsikts-/beslutsbevis men saknar
implementationsbevis: `founder_approved_decision`, `historical_suggestion`, `open_question`
och `proposed_design`. Avvisade och supersederade spår exkluderas, och `missing_stages`
bevaras. Listan är en **granskningskö**, inte ett påstående att ingen minns kravet: det kan
vara implementerat på en annan branch, höra till ett annat projekt eller vara medvetet
tappat utan registrerad supersession. `canonical_truth` är fortfarande alltid `None`.

## 6. Fas 4 — läs-only ingenjörsrekommendation

`recommendation.py` svarar med ett av REUSE / IMPROVE / INVESTIGATE / WAIT / ASK / REPLACE /
REJECT, med evidens, alternativ, för- och nackdelar samt vilka grundarbeslut som krävs.

Exemplet "bygg en ny minnesmotor" mot den verkliga kartan:

```
outcome: investigate   uncertainty: medium
alternativ: improve, reuse, replace
root_problem: felet är overifierat; det kan ligga i retrieval, indexering,
              behörighet, kontextgränser eller stale data
```

Reglerna är fail-closed: aktivt uppdrag (en enskild referens eller refs från `ownership.py`)
→ WAIT, motstridiga källor → ASK, annat projekt →
ASK, tidigare avvisat → REJECT, ej inkopplad komponent → IMPROVE, inkopplad och testad →
REUSE. Ett REUSE/INVESTIGATE är aldrig ett byggbeslut och `execution_authorized` är alltid
`False`.

Aliastabellen (`CAPABILITY_ALIASES`) är kuraterad och granskningsbar. Den gissar inte från
fritext: en omatchad begäran blir ASK, inte en påhittad komponent.

## 7. Fas 5 — acceptans

`92 passed, 136 warnings in 194.31s` över tio filer.

| # | Krav | Test |
|---|---|---|
| 1 | identiska dokument | `test_byte_identical_sources_are_authoritative_and_both_originals_kept`, `test_exact_identity_uses_the_checksum_index` |
| 2 | en-teckensändring | `test_one_character_change_is_not_identity`, `test_one_character_change_is_detected` |
| 3 | whitespace ≠ semantik | `test_whitespace_only_difference_is_normalized_not_identical`, `test_whitespace_variant_is_a_candidate_not_identity` |
| 4 | omskriven text är relaterad, inte identisk | `test_case_4_rewritten_document_is_related_not_identical` |
| 5 | historisk kontext till rätt projekt | `test_case_5_historical_context_is_assigned_to_its_project` |
| 6 | fel historisk stack ≠ nuvarande stack | `test_case_6_misleading_historical_stack_is_not_the_current_stack` |
| 7 | avvisat beslut återupplivas inte | `test_rejected_decision_is_not_silently_revived`, `test_rejected_and_superseded_are_not_revived` |
| 8 | befintlig kapabilitet från kodbevis | `test_case_8_existing_capability_is_read_from_real_code_evidence` |
| 9 | omergad/observerad ≠ deployad | `test_case_9_unmerged_or_unobserved_is_never_called_deployed` |
| 10 | aktivt uppdrag stoppar dubblett | `test_active_assignment_prevents_duplicate_work` |
| 10b | annan agents path-scope stoppar dubblett | `test_path_overlap_reports_the_real_assignment_and_live_lease`, `test_ownership_report_refs_block_duplicate_work` |
| 11 | ändring påverkar beroenden | `test_change_impact_reports_dependents_and_unknowns` |
| 12 | motstridiga källor → osäkerhet | `test_conflicting_sources_force_an_explicit_question`, `test_disagreement_preserves_both_statements` |
| 13 | tvetydig historia kan inte trigga exekvering | `test_case_13_ambiguous_history_cannot_trigger_execution`, `test_no_outcome_grants_execution_authority` |
| 14 | omstart förstör inte verifierat läge | `test_repeated_lookup_after_restart_is_stable_and_read_only` |
| 15 | tvärtenant nekas | `test_cross_tenant_lookup_is_denied` |
| 16 | inga hemligheter i publika artefakter | `test_case_16_no_credentials_or_authorization_in_generated_artifacts` + exportskriptets skanning |

## 7.1 Competitor-research (fas 4)

`docs/COMPETITOR_ENGINEERING_RESEARCH.md` jämför åtta publika system (OpenHands, Letta,
Onyx, Agno AgentOS, OpenClaw, Hermes, NemoClaw, OpenDots/OpenBot, AnythingLLM/Open WebUI)
mot faktisk LifeAI-kod. Slutsatsen är avsiktligt konservativ: de säkra teknikerna finns redan
(eventlogg, provenance, RLS, path-leases, capability-registry); de som saknas skulle antingen
duplicera minnes-/RAG-/orkestreringslagret eller röra Claude- respektive Z.ai-ägt arbete. De
står som förslag med ägare/blockerare, inte som tyst implementerad kod. De två luckor som
stängdes i den här omgången (arbetsägande och glömda krav) kommer från programmets fas 2 och
återanvänder befintliga primitiver.

## 8. Fas 6 — gränssnitt

**Medvetet inte byggt.** `frontend/node_modules` saknas, så en Next.js-yta kan inte byggas
eller verifieras i den här miljön, och att lägga en router i `app/main.py` skulle ändra en
delad fil som #500–#506 också rör. CLI:t emitterar i stället stabil, versionsbar JSON
(`map --deep`, `impact`, `recommend`, `prs`) som en framtida read-only-vy kan konsumera utan
att backend ändras igen. Detta är en uttalad avgränsning, inte en påstådd färdig UI.

## 9. Säkerhet och auktoritet

- Inga produktionsändringar, inga VPS-tjänster, ingen deploy, ingen merge till main.
- `security/POLICY_LOCK.json` orörd; `first_cycle_go=false`.
- Inga hemligheter, tokens eller rå privata grundardata i repo eller artefakter.
- Ingen autonom agentassignering; ingen historisk text blir auktorisation.
- `execution_authorized: false` i varje beslut, rekommendation och genererad JSON — testat.
- Enda skrivningen i repot är nya filer plus `docs/BRANCH_REGISTRY.md`.

## 10. Reproduktion

```bash
cd backend
python -m pytest tests/backend/test_context_atlas_*.py -q

cd ..
python backend/scripts/context_atlas.py map --repo-root . --deep --with-git \
  --out docs/evidence/context-atlas/system-map.json
python backend/scripts/context_atlas.py impact app.rag.trust --repo-root .
python backend/scripts/context_atlas.py recommend --repo-root . \
  --capability memory --text "build a new memory engine" --implies-failure

bash scripts/ops/export_commit_stack.sh export /tmp/atlas \
  8c7b335e87071b08a60f4c30075ff29c2a7b8c94 /tmp/context-atlas-transfer/atlas \
  freebuff/context-atlas-20261124
bash scripts/ops/export_commit_stack.sh verify \
  /tmp/context-atlas-transfer/atlas/commit-stack.bundle \
  /tmp/context-atlas-transfer/atlas/manifest.json
```

## 11. Kvarstående begränsningar

- Kandidatsökningens relaterade fönster använder `size_bytes`, som **inte** är indexerat;
  exakt identitet är indexerad, relaterade kandidater är en bounded scan. En framtida
  normaliserad hash-kolumn skulle göra dem indexerade — men kräver en migration och är
  medvetet inte gjord här.
- Kandidattesterna kör riktig DB/RLS/storage men **inte** hela embedding-workern; det skulle
  kräva provideranrop. Det står uttryckligen i testfilens docstring.
- Systemkartan är statisk: ingen runtime-observation, ingen täckningsmätning, ingen
  gransknings-/merge-/aktiveringsstatus. Endast 1 worker-hook hittades av den statiska
  skanningen; schemaläggningswiring är inte fullständigt parsad.
- Spårbarhetsmodellen är en evidens-API, inte ett färdigt historiskt gap-register. Att fylla
  WS-F med verkliga krav kräver granskning av historiska källor.
- Arbetsägande är en ögonblicksbild av tabellrader, inte en liveness-probe: en `planned`
  assignment utan lease är svagare bevis än `running` med live lease, och en utgången lease
  säger inget om processen faktiskt dog. Ingen eventloggsbaserad `observed`-mätning görs.
- Competitor-researchen är dokumentationsbaserad, inte en benchmark; inget annat system har
  körts mot LifeAI:s korpus.
- Semantisk överlapp med #501 (near-duplicate) och #506 (beslutsstöd i live-prompten) måste
  prövas före merge (se §2).
