# System Intelligence 2.0 — projektgränser, utvecklingstillstånd och konsistens

**Branch:** `freebuff/system-intelligence-2-20261124`
**Bas:** `5b8c4630ddaab96fadc580de7b1396c719a7a982` (`freebuff/context-atlas-20261124`)
**Läge:** C3, E1, E3 och B3 implementerade och testade. Ingen push, PR, merge eller deploy.
`first_cycle_go=false`. Inga VPS-/produktionsresurser, inga hemligheter och ingen ändring av
`security/POLICY_LOCK.json`.

## 1. Vad detta är — och inte är

En utökning av den befintliga Context Atlas, inte ett parallellt system. Den återanvänder:

- `agent_work_assignments`, `agent_work_assignment_events`, `agent_scope_leases` och
  `agent_dispatch_executions` för arbetsägande, historik och observerad processstatus,
- `mainai_goals` / `mainai_tasks` för det auktoriserade målet och approval-gränser,
- `project_checkpoints` / `project_notes` för den mänskligt läsbara återupptagningsbilden,
- `app.agent_coordination.service.paths_conflict_any` och `is_lease_stale` för samma
  deterministiska regler som koordinatorn redan använder,
- `SystemMap` för statisk arkitekturevidens.

Detta är **inte** en ny minnes-, RAG-, kö- eller agentmotor. Modulerna skriver inga rader,
skickar inga agentinstruktioner, anropar varken GitHub eller en LLM och sätter
`execution_authorized: false` i varje emitterat beslut.

## 2. C3 — projektisolering

`backend/app/context_atlas/project_scope.py` löser **explicita** projektreferenser mot ett
granskat projektregister. Den gissar aldrig projekt från fritext och använder aldrig liknande
ordalydelse som ett gränssignal.

| Status | Betydelse | Får implementeras? |
|---|---|---|
| `isolated` | varje claim pekar på samma kända projekt | ja |
| `mixed` | claims pekar på fler än ett projekt | nej |
| `partial` | ett projekt är känt, men några claims saknar referens | nej |
| `ambiguous` | en alias matchar fler än ett projekt | nej |
| `unknown` | ingen referens kunde placeras, eller registret saknas | nej |

`canonical_truth` är alltid `None`. `ProjectScopeReport.to_reconciliation_observation()` matar
den befintliga `reconcile()`-funktionen: endast `isolated` blir `this_project`; allt annat blir
`unknown` + `ambiguous` + `evidence_sufficient=False`, vilket ger ASK i stället för arbete.

Testade exempel: LifeAI/L4TP/LifeMusic/LifeInvest/4ThePeople, ett arkiverat historiskt projekt,
delad alias, samma ordalydelse i två projekt, saknad referens och okänt register.

## 3. E1/E3 — hållbart utvecklingstillstånd och återhämtning

`backend/app/context_atlas/development_state.py` läser en assignment och sammanställer:

- mål, goal/task-status och approval-gates,
- branch, worktree, base/head-SHA och persisted upstream-PR-ref,
- live lease och lease-generation,
- senaste append-only event och statusmilstolpar,
- dependency-status, öppna blockers och gällande beslut från projektminnet,
- senaste checkpoint, dess SHA och om den är stale,
- `observed`: endast sant när en dispatch-rad är i `starting`/`running` och har heartbeat
  inom 15 minuter.

En `running`-rad utan live lease är **stale**, inte levande. En `planned`-rad med lease är
fortfarande bara planerad. `next_authorized_action` är deterministisk och kan aldrig bli en
automatisk dispatch.

`recover_development_state()` väljer den assignment en ny session ska återuppta:

1. live lease vinner över enbart observerad process,
2. observerad process vinner över senaste aktivitet,
3. exakt lika evidens eller fler än en live lease → `ambiguous` och `requires_founder=True`,
4. `running` utan live lease → `stale`, med uppmaning att verifiera processen,
5. inga icke-terminala assignments → `none`, aldrig en påhittad uppgift.

`github_observed` är alltid `false`: en persisted `upstream_pr_ref` rapporteras som inspelad
evidens, inte som ett nätverkssvar. Lokala commits (`head_sha != base_sha`) rapporteras med
”push state is unknown”.

Testade scenarier: live lease + dispatch, utgången lease, två live claims, branch-filter,
terminalt arbete, checkpoint + blocker, lokala commits utan push-antagande, samt tvärtenant.

## 4. B3 — arkitekturkonsistens

`backend/app/context_atlas/consistency.py` rapporterar deterministiska motsägelser:

- samma `METHOD path` exponerad av fler än en router,
- router inkluderad i `app/main.py` men filen saknas,
- endpoint utan skannad routermodul,
- två Alembic-migrationer med samma fyrsiffriga prefix,
- två ORM-modeller med samma `__tablename__`.

Varje finding har `code`, `severity`, `evidence` och `review_action`. Ingen finding raderar,
skriver om eller mergar något; `execution_authorized` är alltid `False`. Dokumentation-mot-kod
och “deprecated implementation fortfarande refererad” kräver en granskad källista och lämnas
medvetet utanför den statiska detektorn.

På denna checkout: **0 findings** i `consistency.json`. Det betyder att de fem deterministiska
kontrollerna inte hittade någon motsägelse — inte att arkitekturen är bevisat felfri.

## 5. Verifiering

```
114 passed, 334 warnings in 223.85s   # hela atlas-sviten, inkl. 22 nya tester
9 passed                              # E1/E3 omkörning efter sista refaktoreringen
ruff check                            # All checks passed
```

Nya filer:

- `backend/app/context_atlas/project_scope.py`
- `backend/app/context_atlas/development_state.py`
- `backend/app/context_atlas/consistency.py`
- `backend/tests/backend/test_context_atlas_project_scope.py` (8 tester)
- `backend/tests/backend/test_context_atlas_consistency.py` (5 tester)
- `backend/tests/backend/test_context_atlas_development_state.py` (9 tester)

CLI:t har två nya read-only kommandon:

```bash
python backend/scripts/context_atlas.py consistency --repo-root . --out consistency.json
python backend/scripts/context_atlas.py scope projects.json claims.json --out scope.json
```

## 6. Evidens

- `docs/evidence/system-intelligence-2/consistency.json` — 0 findings på checkouten.
- `docs/evidence/system-intelligence-2/project-scope-mixed.json` — LifeAI + LifeMusic med
  identisk ordalydelse blir `mixed`, `requires_review=true`, `execution_authorized=false`.
- `docs/evidence/system-intelligence-2/project-scope-isolated.json` — LifeAI + LifeAI Platform
  blir `isolated`.
- `docs/evidence/system-intelligence-2/inputs/` — de granskade register-/claim-underlagen.

Alla artefakter är deterministiska funktioner av checkouten/JSON-underlagen och innehåller
inga hemligheter eller exekveringsrättigheter.

## 7. Kvarstående begränsningar

- Ingen ny persistence: E1 sammanställer befintliga rader. Det gör tillståndet hållbart så
  länge de befintliga tabellerna är det, men det är inte en ny eventlogg.
- `observed` bygger på dispatch-radens heartbeat, inte på en OS-processprobe. En process som
  dog utan att senaste heartbeat hann uppdateras kan se levande ut inom 15-minutersfönstret.
- Återhämtningen gör inget nätverksanrop. GitHub-läget är okänt om det inte redan finns som
  persisted `upstream_pr_ref`.
- B3 täcker fem deterministiska kontroller, inte semantisk dokumentation-mot-kod-analys.
- Projektisoleringen kräver ett granskat register. Utan register blir resultatet `unknown`,
  aldrig en gissning.
- Ingen UI (fas 6) och ingen automatisk dispatch, merge eller deploy.
