# System Intelligence Runtime — en gemensam read-only yta

**Branch:** `freebuff/system-intelligence-runtime-20261124`
**Bas:** `d1c631be26985a8b2ed19f833ecbfe6a1c076a6d`
(`freebuff/system-intelligence-2-20261124`)
**Läge:** runtime-integration, spårbarhet, självkännedom, recovery/overlap och
felhanteringstester implementerade. Ingen push, PR, merge eller deploy.
`first_cycle_go=false`. Inga VPS-/produktionsresurser, inga hemligheter och ingen ändring av
`security/POLICY_LOCK.json`.

## 1. Syfte

Context Atlas hade redan små, deterministiska analysmoduler, men ingen gemensam yta som
befintliga LifeAI-tjänster kunde anropa utan att själva limma ihop dem eller parsa CLI-JSON.
`backend/app/context_atlas/runtime.py` är den ytan.

Det är en **kompositionsmodul**, inte en ny motor:

- den återanvänder `scan_system_map`, `scan_capabilities`, `check_consistency`, `recommend`,
  `build_trace`, `recover_development_state`, `load_development_state`, `find_work_owners` och
  `scan_local_git`;
- den lägger till en processlokal cache, strukturerade fel/`unknown`-svar och en gemensam
  `execution_authorized: false`-kontrakt;
- den lägger **inte** till någon tabell, migration, kö, vektorindex, modellkedja eller
  agentdispatcher.

## 2. Programmatiskt gränssnitt

```python
from app.context_atlas.runtime import SystemIntelligence

si = SystemIntelligence("/path/to/LifeAI")

answer = si.capability("app.context_atlas.runtime")
deps = si.dependencies("app.context_atlas.runtime")
trace = si.trace_requirement(
    "SI2-RUNTIME",
    "Runtime interface for System Intelligence",
    "lifeai",
    doc_paths=("docs/SYSTEM_INTELLIGENCE_RUNTIME.md",),
    code_paths=("backend/app/context_atlas/runtime.py",),
    test_paths=("backend/tests/backend/test_context_atlas_runtime.py::test_capability_reads_real_code_and_test_evidence",),
)
assessment = si.assess(
    EngineeringRequest("build retrieval from scratch", capability_aliases=("retrieval",))
)
status = si.integration_status()
knowledge = si.self_knowledge("which components are tested")
```

| Metod | Svar | Återanvänder |
|---|---|---|
| `capability(component)` | implementation, wiring, testreferenser, maturity, limitations | `scan_system_map` |
| `dependencies(module)` | direkta/transitiva dependents, endpoints, tester, säkerhetsnoter | `SystemMap.impact` |
| `trace_requirement(...)` | krav→implementation→test, saknade steg, overifierade refs | `build_trace` |
| `assess(request, ...)` | reuse/improve/investigate/wait/ask/replace/reject med evidens | `recommend` |
| `integration_status()` | lokal HEAD/branch vs captured PR, utan nätverk | `scan_local_git` |
| `recovery(db, ...)` | persisted assignment/lease/event/dispatch/checkpoint | `recover_development_state` |
| `overlap(db, ...)` | path-scoped assignment + lokal/captured PR-evidens | `find_work_owners` |
| `self_knowledge(question, ...)` | deterministiskt svar med fakta, evidens och tolkning separat | ovanstående |
| `inspect_untrusted_source(ref, text)` | text behandlas som data; aldrig instruktion | deterministisk mönstergranskning |
| `snapshot()` | liten statuspayload för en tjänst/health check | cache + integration |

Alla `to_dict()`-payloads innehåller `execution_authorized: false`.

### Varför ingen ny HTTP-router

`app/main.py` är en delad entry point som Claude äger. Att lägga en ny router där utan
koordinering skulle riskera exakt den typ av konflikt som projektets branch-regler varnar för.
Därför är den stabila ytan en intern Python-service plus CLI. Den kan senare kopplas till en
router av den som äger entry pointen, utan att analyslogiken ändras. Detta är ett medvetet
integrationskontrakt, inte en påstått färdig HTTP-yta.

## 3. Vad som faktiskt återanvänds

- `app.context_atlas.system_map` — moduler, endpoints, importgraf, bounded impact.
- `app.context_atlas.capability_map` — top-level capability inventory och migrationshead.
- `app.context_atlas.consistency` — fem deterministiska arkitekturkontroller.
- `app.context_atlas.recommendation` — reuse/improve/investigate/wait/ask/replace/reject.
- `app.context_atlas.traceability` — nio steg, missing stages och canonical_truth=None.
- `app.context_atlas.project_scope` — explicit projektgräns, fail-closed.
- `app.context_atlas.ownership` — samma path-prefixregel som koordinatorn.
- `app.context_atlas.development_state` — persisted recovery, inte conversationsminne.
- `app.context_atlas.git_evidence` — lokal git + **inspelad** PR-metadata, aldrig nätverk.

## 4. Cache och prestanda

Cachen är processlokal och invalideras när lokal HEAD, dirty tracked-tree eller
captured-PR-filens mtime/size ändras. `refresh()` tvingar en ny scanning. Det finns ingen
persistent cachefil, så det finns inget cache-entry att korrumpera mellan processer.

Uppmätt på denna checkout i testmiljön (2026-11-24, en mätning — inte ett
produktionsbenchmark):

```
first_scan_ms=17412.9
cached_capability_100_ms=0.63
cached_self_knowledge_100_ms=3.63
cached_capability_per_call_ms=0.006
cached_self_knowledge_per_call_ms=0.036
```

Första anropet gör en full `scan_system_map` + `scan_capabilities`. Därefter är upprepade
frågor på samma instance i praktiken gratis jämfört med scanningen. Detta är en minskning av
**upprepade fullscans**, inte ett påstående om inkrementell indexering per fil. En framtida
per-fil-hashad indexering kräver egen design och är medvetet inte byggd här.

## 5. Acceptans A–H

| Scenario | Test | Vad som verifieras |
|---|---|---|
| A — befintlig funktion | `test_assessment_reuses_existing_retrieval_instead_of_rebuilding` | `app.rag` hittas och rekommendationen blir `reuse`, inte duplikat |
| B — godkänt krav utan implementation | `test_approved_requirement_without_implementation_is_forgotten` | `founder_approved_decision` + missing implementation + forgotten queue |
| C — motstridiga dokument | `test_mixed_project_scope_forces_an_explicit_question` | mixed scope → `ask`; båda källorna bevaras |
| D — avbruten utveckling | `test_overlap_and_recovery_use_persisted_coordination_rows` | recovery från assignment/lease/event, inte sammanfattning |
| E — säkerhetsgräns | `test_untrusted_source_is_data_not_an_instruction` | prompt-liknande text blir `data`, aldrig `execute` |
| F — beroendeförändring | `test_dependencies_show_real_consumers_and_unknowns` | direkta dependents, tester och unknowns |
| G — projektförväxling | `test_mixed_project_scope_forces_an_explicit_question` | LifeAI + LifeMusic blandas inte |
| H — opublicerad implementation | `test_captured_pr_is_distinguished_from_local_only`, `test_local_implementation_without_matching_pr_is_not_integrated` | lokal HEAD ≠ captured PR; `local_only` |

Dessutom: `test_missing_refs_are_excluded_not_counted_as_implementation`,
`test_missing_or_corrupt_pr_capture_degrades_to_unknown`,
`test_unknown_module_is_structured_not_an_exception` och
`test_all_runtime_payloads_deny_execution_authority`.

## 6. Felmodell

| Fel | Beteende |
|---|---|
| git saknas/HEAD okänt | `integration_status.state=unknown`, `local_only=None`, varning |
| PR-fil saknas | `unknown`; ingen gissning om remote state |
| PR-fil är korrupt | `unknown`; ingen krasch |
| modul finns inte | `not_found` med förklaring |
| doc/code/test-ref finns inte | exkluderas och listas i `unverified_refs`; räknas inte som implementation |
| projektregister saknas | `unknown`; ingen implicit ägare |
| två projekt/okänd scope | `ask`; ingen implementation |
| aktiv assignment | `wait`; ingen duplikatdispatch |
| stale `running` utan lease | `stale`; inte `recovered` |
| opålitlig källtext | `treated_as_data=true`; endast `cite/review/ask` |

Ingen modell leverantör behövs för något av dessa svar. Om en framtida AI-tolkning läggs ovanpå
ska den ligga i ett separat fält och aldrig skriva över `facts`/`evidence`.

## 7. Verifiering

```
18 passed    # tests/backend/test_context_atlas_runtime.py
7 passed     # tests/backend/test_context_atlas_system_map.py
132 passed   # hela atlas-sviten, inkl. runtime
ruff check   # All checks passed
```

CLI:

```bash
python backend/scripts/context_atlas.py query \
  "which components are tested" --repo-root .
python backend/scripts/context_atlas.py status --repo-root .
python backend/scripts/context_atlas.py trace \
  --requirement-id SI2-RUNTIME --title "Runtime interface" --project lifeai \
  --doc docs/SYSTEM_INTELLIGENCE_RUNTIME.md \
  --code backend/app/context_atlas/runtime.py \
  --test 'backend/tests/backend/test_context_atlas_runtime.py::test_capability_reads_real_code_and_test_evidence'
python backend/scripts/context_atlas.py assess \
  --repo-root . --capability retrieval --text "build retrieval from scratch"
```

## 8. Evidens

- `docs/evidence/system-intelligence-runtime/capability-runtime.json`
- `docs/evidence/system-intelligence-runtime/dependencies-runtime.json`
- `docs/evidence/system-intelligence-runtime/trace-runtime.json`
- `docs/evidence/system-intelligence-runtime/assessment-reuse.json`
- `docs/evidence/system-intelligence-runtime/integration-status.json`
- `docs/evidence/system-intelligence-runtime/self-knowledge.json`
- `docs/evidence/system-intelligence-runtime/untrusted-source.json`
- `docs/evidence/system-intelligence-runtime/snapshot.json`

Artefakterna är deterministiska funktioner av checkouten (förutom git-tidsstämplar i
`integration-status`/`snapshot`) och innehåller inga hemligheter eller exekveringsrättigheter.

## 9. Kvarstående begränsningar

- Statisk analys: `wired`/`test_referenced` är inte runtime-observation eller täckning.
- `integration_status` gör inget nätverksanrop; remote PR/merge/deploy är okänt utan inspelad
  metadata.
- `trace_requirement` verifierar path-refs i checkouten. PR/runtime/founder-decision-refs är
  caller-supplied och verifieras inte av tjänsten.
- `self_knowledge` är en granskad intent-router, inte fri textgenerering. Okänd formulering
  returnerar `unknown` och listan över stödda frågor.
- `recovery` använder dispatch-heartbeat, inte OS-processprobe; 15-minutersfönstret kan göra
  en nyligen död process synlig som levande.
- Ingen HTTP-router, UI eller automatisk dispatch/merge/deploy.
- Ingen persistent cache eller inkrementell per-fil-indexering.
- Prestandasiffrorna är en lokal mätning i testmiljön, inte ett produktionsbenchmark.
- `Task #61` och founder-wide authorization assumptions är inte implementerade.
