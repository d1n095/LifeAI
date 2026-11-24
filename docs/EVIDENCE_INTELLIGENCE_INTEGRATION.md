# Evidence-driven MainAI integration (2026-11-24)

Branch: `freebuff/evidence-intelligence-integration-20261124`
Worktree: `/tmp/si3`
Base: `790e2eaf004528e72d249257f3de48fb3b83ece3` (the System Intelligence runtime branch,
which already contains `freebuff/life-workspaces` at `8c7b335e`)

This layer makes the existing executive components answer better questions. It adds no
memory store, no RAG pipeline, no planner, no provider registry, no task queue and no agent
loop. Every new function composes components that already existed.

## What was reused

| Need | Existing component |
| --- | --- |
| Evidence ranking | `app.mainai_school.evidence` (`EvidenceRank`) |
| Assumption state and history | `app.problem_learning.service` (`record_assumption`, `transition_assumption`) |
| Problems, decisions, failed approaches | `app.problem_learning.service` (`open_problems`, `active_decision`, `useful_components_from_failed_approaches`) |
| Capability truth | `app.capability_reality.service` (`list_capability_records`) |
| Engineering lessons | `app.mainai_execution.lessons.lookup_lessons` |
| Provider catalog and selection rules | `app.providers.catalog` (`evaluate_model`, `ModelSpec`, `ModelSelectionPolicy`) |
| Provider configuration and verification | `app.providers.registry` (`get_provider`, exact-model verification) |
| Provider failure state | `app.providers.circuit_breaker.is_open` |
| Spend authorization | `app.provider_spend.service` (`get_current_provider_spend_authorization`, `provider_spend_is_live`) |
| Work overlap, dependencies, untrusted text | `app.context_atlas.runtime.SystemIntelligence` |
| Durable recovery | `app.mainai_executive.continuity` / `resume_executive_cycle` |
| Completion honesty | `app.mainai_executive.completion` |

## New modules

### `app/mainai_executive/evidence_state.py`

Explicit states instead of a percentage:

`unknown → module_located → implementation_inspected → tests_observed → integration_verified → runtime_verified`

- `module_source_path()` resolves a module to a file without importing it.
- `assess_module()` never upgrades a state by itself. `integration_verified` and
  `runtime_verified` exist only when the caller supplies observed refs.
- `assess_modules()` reports both the strongest and the weakest state. The weakest state is
  the honest aggregate.
- `percentage` is always `None`; test references are file mentions, not passing tests.

`app/mainai_executive/missing_piece.py` now uses these states. `likely_coverage_pct` is gone.
`propose_new_subsystem` is true only when a matched capability area has no located module.

### `app/mainai_executive/investigation.py`

`investigate_engineering_request()` runs the read-only path: interpreted objective → existing
implementations → prior problems/decisions → lessons → capability records → overlap →
dependencies → untrusted-source classification → alternatives → recommendation.

The recommendation is one of `reuse_existing`, `extend_or_repair_existing`,
`inspect_before_deciding`, `investigate_gap`, and always carries
`requires_human_decision: true` and `execution_authorized: false`.

### `app/mainai_executive/assumption_review.py`

`review_objection()` evaluates a founder objection against supplied evidence using the
existing hierarchy. A founder objection alone is `unresolved`; model opinion alone is
`unresolved`; only decisive evidence (rank ≤ `DIRECTLY_OBSERVED_OUTCOME`) can return
`revised` or `confirmed`. With `apply_transition=True` the linked assumption moves to the
schema's own `contradicted` status, and the event reason names the evidence.

### `app/mainai_executive/engineering_decision.py`

Compares the six options (`reuse_existing`, `repair_existing`, `extend_existing`,
`replace_component`, `defer`, `no_change`) over the nine criteria. Scores are caller-supplied
and may be `None`; unknown criteria are reported, never imputed. Ties return
`unresolved_tie`. `recommendation_is_not_authorization` and `execution_authorized: false`
are always present.

### `app/mainai_executive/bottleneck.py`

Reads `/proc/loadavg`, `/proc/meminfo`, `/proc/cpuinfo` and `shutil.disk_usage` only. No
psutil, no Docker socket, no network, no restart. The payload separates `observations`,
`candidate_causes`, `diagnosis` (always `None` from one sample) and
`hardware_upgrade_justified` (always `false`).

### `app/mainai_executive/provider_eligibility.py`

Deterministic filter order: catalog policy → provider configured → exact-model verification
(when required) → circuit breaker → spend authorization. `policy_eligible` and
`callable_models` are separate facts; neither is execution authorization. `fallback_plan()`
orders eligible models local-first and leaves selection to the caller.
`coding_agent_eligibility()` states explicitly that a chat provider does not grant Codex CLI
or the Z.ai development agent.

## Scenarios

`backend/tests/backend/mainai/test_evidence_intelligence_integration.py`

| # | Scenario | Verification |
| --- | --- | --- |
| 1 | Existing capability found before building | real DB + static scan |
| 2 | Incorrect assumption revised by decisive evidence | real DB, assumption `contradicted` |
| 2b | Objection alone / model opinion alone | deterministic |
| 3 | Located module does not claim capability | static |
| 4 | Competing solutions compared, tie unresolved | deterministic |
| 5 | Interrupted cycle resumes from checkpoint | real DB |
| 6 | Memory pressure is observation, not diagnosis | synthetic sample |
| 7 | Unavailable provider is not eligible | stubbed provider config |
| 8 | Newer decision supersedes older | real DB |
| 9 | Untrusted instruction treated as data | real runtime helper |
| 10 | Overlap reported without interruption | mocked overlap payload |

Final run: **24 passed** across the new file plus the existing executive loop and scenario
modules (`46.84s`). Ruff passes on every changed file. The three pre-existing ruff findings in
`continuity.py` and `retrieval_quality.py` were left untouched.

Evidence: `docs/evidence/evidence-intelligence-integration/`.

## Workstream G — new VPS readiness

- **Dependencies:** Python standard library only for the bottleneck sampler. No new package,
  no new service, no new container.
- **Database:** no table, column or migration. Migration head remains `0095`. The new code
  reads existing tables through existing services.
- **Resources:** investigation and evidence scans are file reads plus bounded SQL queries.
  The bottleneck sample is four small file reads. No heavy inference, no benchmark.
- **Startup dependencies:** Postgres and Redis, exactly as the existing backend. No new
  startup ordering.
- **Safe test command:**

  ```bash
  cd backend
  DATABASE_URL=postgresql://<db-user>@127.0.0.1:55432/lifeos_test_si3 \
  APP_DATABASE_URL=postgresql://<app-user>@127.0.0.1:55432/lifeos_test_si3 \
  REDIS_URL=redis://127.0.0.1:56379/3 ENVIRONMENT=test \
  python -m pytest tests/backend/mainai/test_evidence_intelligence_integration.py -q
  ```

- **Integration order:** runtime branch `790e2eaf` first (or together with this branch),
  then this branch, then rebase any PR that also edits `providers/registry.py`.
- **Rollback:** revert the two commits. No schema change, no data migration, no service
  configuration change.
- **Compatibility:** no change to `app/main.py`, no change to Claude-owned conversational
  routes, no change to Z.ai security paths. The only shared-file edit is a 10-line additive
  public wrapper in `backend/app/providers/registry.py`.

## Ownership and overlap

All 20 open PRs returned by GitHub (#487–#506) also touch
`backend/app/providers/registry.py`. This branch's edit there is additive
(`model_is_freshly_verified`), but a textual conflict is possible. Do not rebase this branch
pre-emptively: follow the merge rule and rebase only after the first of those PRs merges.

No PR overlaps any other changed file in this branch.

## Unresolved / not claimed

- No HTTP route is wired. `app/main.py` is Claude-owned and untouched.
- No live provider call was made; scenario 7 stubs provider configuration.
- Scenario 10 uses a mocked overlap payload; it proves composition and no interruption, not
  a live cross-agent conflict.
- No CI run, no browser journey, no VPS deployment, no Docker verification.
- GitHub write access is still unavailable: no push, no branch on the remote, no PR.
- `first_cycle_go=false`; `security/POLICY_LOCK.json` unchanged; no founder-wide authority
  assumption; no automatic agent dispatch.
