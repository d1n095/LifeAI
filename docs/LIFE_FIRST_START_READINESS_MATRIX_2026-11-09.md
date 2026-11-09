# Life Founder Alpha — End-to-End Readiness Matrix (2026-11-09)

**Status: documentation only. No merge, deploy, activation, or production credential.
`first_cycle_go=false`.** Base of record: default `claude/det-kommer-mer-879lcm` @ `64c07d5b`.
PR states verified against the GitHub API on 2026-11-09.

This is Mission 4 of the GitHub-first Founder directive: a GitHub-based readiness matrix showing
what exists, what is missing, what depends on what, and the next feasible steps — **without
inventing new product architecture.** It is the companion of
`docs/LIFE_OPERATIONAL_PRIORITY_2026-11-09.md` (the capability→module audit) and should be read
with it.

---

## 1. The required operational chain (from the directive)

```
Founder instruction
  → authorized document retrieval
  → project-wide understanding
  → dependency-aware priorities
  → authorized agent communication
  → real execution evidence
  → independent review
  → persistent project memory
  → next-step explanation
  → future Blueprint proposals
```

## 2. Readiness matrix — chain link by chain link

Legend: **IMPL** = data+service committed to default; **TEST** = committed backend tests exercise
it; **OPER** = exercised through a real running path today (**none are, default is unactivated**);
**BLOCK** = built but gated on a merge/authorization/activation.

| # | Chain link | Existing owner (reuse) | On default `64c07d5b`? | Class | Blocking dependency |
|---|---|---|---|---|---|
| 1 | Founder instruction (authority) | `app.execution_envelopes`, `app.provider_spend`, Founder auth; passkeys PR #316/#318 | Yes (envelopes/spend); passkeys unmerged | IMPL+TEST | Founder grant at run time (decision) |
| 2 | Authorized document retrieval | `app.rag`, `app.storage`, shared-original PR #329/#330, DOCX PR #327 | Core yes; #327/#329/#330 unmerged | IMPL+TEST | Merge #329 (+ #369 importer = future) |
| 3 | Project-wide understanding | `app.inspectable_memory`, `app.founder_memory` (0049), `app.capability_reality` (0048), `app.memory_threads`, `app.project_entities` | Yes | IMPL+TEST | Enforced session-start Context Bundle (unbuilt script) |
| 4 | Dependency-aware priorities | `app.life_intents` (0041), `app.safe_planner`, `app.work_candidates`, `app.mainai_executive.priority`/`missing_piece` | Yes | IMPL+TEST | Cross-PR dependency reasoning still manual (`BRANCH_REGISTRY.md`) |
| 5 | Authorized agent communication | `app.agent_coordination` (adapters/dispatch/routing), `app.workforce` | Yes (coordination); dispatch-activation PR #339 | IMPL+TEST, **BLOCK** at dispatch | Merge #338→#339; provider + workforce activation gates |
| 6 | Real execution evidence | `app.mainai_execution` (executor/verify), model-backed workers PR #336, real-local-AI CI PR #334 | Execution loop yes; #336/#334 unmerged | IMPL+TEST, **BLOCK** | Merge #335→#336; provider activation (`first_cycle_go`) |
| 7 | Independent review | Process: Copilot/Codex review lanes (PR #367 closeout); gate-level `app.mainai_startup_readiness` | Yes (process + readiness levels) | IMPL+TEST | Keep review independent (never self-certify) |
| 8 | Persistent project memory | `app.founder_memory`, `app.memory_threads`, `app.inspectable_memory`, handovers | Yes | IMPL+TEST | Record first-run results as capability records (after run) |
| 9 | Next-step explanation | `app.mainai_executive` (`attention`/`priority`/`why_graph`), `app.safe_planner` | Yes | IMPL+TEST | Wire into composed loop (activation) |
| 10 | Future Blueprint proposals | **PR #340** Blueprint draft (migration 0070) + `app.provider_planning`; egress #360/#361 | **No — only in PR #340** | **BLOCK / IN-FLIGHT** | Merge #340 (+#360/#361) behind review |

**Reading of the matrix:** links 1–4 and 7–9 have their foundations **on default already** and
tested; links 5, 6, 10 are **built but off default or gated** (coordination dispatch, model-backed
execution, Blueprint primitives). No link requires new architecture — every one maps to an
existing module or an open, reviewed PR.

---

## 3. Dependency / merge order (source-grounded, not guessed)

Verified from the GitHub API 2026-11-09. Security P1s are independent of the activation stack and
should land first.

```
SECURITY (independent, base = default):
  #371 prod-secret-validation ──┐
  #372 ownerless-egress        ──┤ (both draft, isolated from default, review-pending)
  #370 backup crash-consistency ┘ (frozen for Copilot review)

CI UNBLOCKER (independent, base = default):
  #323 frontend npm-audit baseline  ← unblocks inherited npm-audit red on every default-based PR
  #344 E2E runner pin (base = #340) ← waits on #323 + its base

ACTIVATION STACK (ordered; each stacked on the previous — do NOT rebase pre-emptively):
  #333 embedding-space ─→ #340 Blueprint draft (0070) ─→ (#342/#343 probes, #344, #345)
  #314 ─→ #338 GitHub-fed operating cycle ─→ #339 authorized dispatch
  #335 executive business workflow ─→ #336 model-backed workers
  #360/#361 Blueprint egress remediation (Codex/Copilot-owned)
```

**Merge-rule reminder (`CLAUDE.md`):** a stacked branch is updated/rebased only **after** its real
dependency has actually merged — never pre-emptively. `docs/BRANCH_REGISTRY.md` is the living map
of these dependencies and should be the source of truth when sequencing.

---

## 4. First-start blockers (what actually stops the first authorized cycle)

| ID | Blocker | Kind | Owner / resolution |
|---|---|---|---|
| **FS-1** | Nothing is merged — default `64c07d5b` has the foundations but no activation PR | Sequencing | Founder-gated merge program (security P1s first, then activation stack) |
| **FS-2** | `first_cycle_go=false` / provider activation disabled | **Founder decision** | Founder authorizes an egress-gated, spend-bounded first cycle |
| **FS-3** | No configured external/local provider for a real run | Config/decision | Provider key or local Ollama (PR #345 VPS Ollama; #334 real-local CI) + egress gate |
| **FS-4** | Enforced session-start Context Bundle unbuilt (`scripts/build_context_bundle.py`) | Small glue (future) | Build the generator per `FOUNDER_KNOWLEDGE_BOOTSTRAP.md` §3 — glue, not a subsystem |
| **FS-5** | Inherited CI reds on default-based PRs (npm-audit, E2E Playwright) | CI hygiene | #323 (npm-audit unblocker) + #344 (runner pin) — already scoped |
| **FS-6** | Independent review still pending on the security P1s (#371/#372) and others | Review | Keep review independent; do not self-certify (gate principle) |

**FS-2 and FS-3 are Founder/operational decisions, not code gaps.** FS-1, FS-5, FS-6 are
sequencing/hygiene. FS-4 is the only genuinely *new* artifact worth building, and it is a small,
source-grounded script — explicitly glue, not product architecture.

---

## 5. Next feasible development steps (safe, ordered, no activation)

1. **Land the security P1s** (#371, #372) and **#323** behind independent review — these are
   independent of the activation stack and reduce risk/CI noise immediately.
2. **Record this readiness matrix's classifications as capability records** (`app.capability_reality`)
   once a first run is authorized — making capability #4 operational for the whole project.
3. **Build `scripts/build_context_bundle.py`** (FS-4) as its own small, reviewed PR — the one
   missing piece of glue for capabilities #2/#9. (Not started here; flagged as the recommended
   next docs→code step.)
4. **Sequence the activation stack** per §3 only as each dependency actually merges (merge-rule).
5. **Founder decision point:** authorize the first egress-gated, spend-bounded cycle (FS-2/FS-3),
   then prove the Mission-4 chain end-to-end once with real evidence + independent review.

---

## 6. Evidence & non-duplication

- PR list/states: GitHub API, 2026-11-09 (open/draft/base all verified).
- Module paths/migrations: default `64c07d5b` working tree.
- This matrix complements and does not restate: `LIFEAI_STATUS.md` (PR #331),
  `FOUNDER_KNOWLEDGE_INGESTION_READINESS_2026-11-08.md` (PR #368),
  `docs/FIRST_AUTONOMOUS_TASK_BLOCKER_MAP.md` (older, execution-chain-level),
  `docs/LIFE_CAPABILITY_REALITY.md` (the mechanism), and
  `docs/LIFE_OPERATIONAL_PRIORITY_2026-11-09.md` (the capability audit, this package).
