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

Legend: **IMPL** = data+service committed to default; **TEST(local)** = committed backend tests
exercise it against local/CI Postgres with synthetic data — **a reviewed local test is NOT
production readiness**; **OPER** = exercised through a real running path today (**none are,
default is unactivated, and the whole chain is unverified end-to-end — §2.a**); **BLOCK** = built
but gated on a merge/authorization/activation; **PARTIAL** = foundation exists but has a concrete
capability gap, not just an activation gap.

| # | Chain link | Existing owner (reuse) | On default `64c07d5b`? | Class | Blocking dependency |
|---|---|---|---|---|---|
| 1 | Founder instruction (authority) | `app.execution_envelopes`, `app.provider_spend`, Founder auth; passkeys PR #316/#318 | Yes (envelopes/spend); passkeys unmerged | IMPL+TEST(local) | Founder grant at run time (decision) |
| 2 | Authorized document retrieval | `app.rag`, `app.storage`, shared-original PR #329/#330, DOCX PR #327 | Core yes; #327/#329/#330 unmerged | IMPL+TEST(local) | Merge #329 (+ #369 importer = future) |
| 3 | Project-wide understanding | `app.inspectable_memory`, `app.founder_memory` (0049), `app.capability_reality` (0048), `app.memory_threads`, `app.project_entities` | Yes | IMPL+TEST(local) | Session-start context assembly (design-only, see FS-4) |
| 4 | Dependency-aware priorities | `app.life_intents` (0041), `app.safe_planner`, `app.work_candidates`, `app.mainai_executive.priority`/`missing_piece` | Yes | IMPL+TEST(local) | Cross-PR dependency reasoning still manual (`BRANCH_REGISTRY.md`) |
| 5 | Authorized agent communication | `app.agent_coordination` (adapters/dispatch/routing), `app.workforce` | Coordination yes; **real adapter is single-shot** | **PARTIAL**+BLOCK | **Agent runtime gap** (`LocalCLIAdapter` non-interactive, output *refs* only) + merge #338→#339 + provider/workforce activation |
| 6 | Real execution evidence (usable results) | `app.mainai_execution` (executor/verify), model-backed workers PR #336, real-local-AI CI PR #334 | Execution loop yes; #336/#334 unmerged | **PARTIAL**+BLOCK | "Usable results" blocked by link-5 runtime gap; merge #335→#336; provider activation |
| 7 | Independent review | Process: Copilot/Codex review lanes (PR #367 closeout); gate-level `app.mainai_startup_readiness` | Yes (process + readiness levels) | IMPL+TEST(local) | Keep review independent (never self-certify) |
| 8 | Persistent project memory | `app.founder_memory`, `app.memory_threads`, `app.inspectable_memory`, handovers | Yes | IMPL+TEST(local) | Record first-run results as capability records (after run) |
| 9 | Next-step explanation | `app.mainai_executive` (`attention`/`priority`/`why_graph`), `app.safe_planner` | Yes | IMPL+TEST(local) | Wire into composed loop (activation) |
| 10 | Future Blueprint proposals | **PR #340** Blueprint draft (migration 0070) + `app.provider_planning`; egress #360/#361 | **No — only in PR #340** | **BLOCK / IN-FLIGHT** | Merge #340 (+#360/#361) behind review |
| 6b | GitHub activity tracking (feeds links 3/4) | `app.integrations.github_client` | Partial read-surface | **PARTIAL** | **Needs new read methods** (branches/open-PRs/reviews/workflow-runs/agent-sessions), not activation alone |

**Reading of the matrix (corrected 2026-11-09):** links 1–4 and 7–9 have foundations **on default
and locally tested**, but local tests ≠ production readiness. Links 5 and 6 are **PARTIAL** — not
merely gated on activation but missing a real agent runtime (single-shot `LocalCLIAdapter`
returning output *references*, not usable results). Link 6b (GitHub tracking) needs **more read
capability**, not activation. Link 10 is off default (only PR #340). **No stage is OPERATIONAL,
and the complete chain is unverified end-to-end (§2.a).**

### 2.a The complete autonomous coordination chain is unverified end-to-end
No continuous, authorized pass has ever run Founder instruction → … → real agent dispatch →
usable results → CI/review correlation → persisted continuity → next-step. Per-stage local tests
exist; the whole chain under real providers/agents/CI has not run and cannot pre-activation
(`first_cycle_go=false`). This is why §2 classifies stages as IMPL/TEST(local)/PARTIAL, never
OPERATIONAL.

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
| **FS-4** | Session-start context assembly not implemented | Design-only (deferred) | **Design** reusing existing canonical sources — `docs/LIFE_SESSION_CONTEXT_ASSEMBLY_DESIGN_2026-11-09.md`. NOT the older `founder_vault/` script (that tree is absent on default). Implementation deferred until prerequisites + owner confirmed. |
| **FS-5** | Inherited CI reds on default-based PRs (npm-audit, E2E Playwright) | CI hygiene | #323 (npm-audit unblocker) + #344 (runner pin) — already scoped |
| **FS-6** | Independent review still pending on the security P1s (#371/#372) and others | Review | Keep review independent; do not self-certify (gate principle) |
| **FS-7** | **DB-role bootstrap password unvalidated** — `backend/scripts/security/ensure_app_role.py` uses `MAINAI_APP_PASSWORD` verbatim to `CREATE/ALTER ROLE`; `.env.example`'s `change-me` would provision prod `mainai_app` with a known password. Not covered by #371. | **Security P1 — FIX SUBMITTED** | **PR #374** (`claude/db-bootstrap-password-guard`, draft, base=default): rejects empty/placeholder/weak prod DB-role passwords, fails before touching the role, preserves dev/test + rotation; 29/29 tests, ruff clean; Copilot exact-SHA review requested. |
| **FS-8** | **Non-durable external-disclosure receipts** — durability gap in the `provider_disclosure_events` ledger write path (Codex-owned egress/receipt code). | **Security P1** | **Codex-owned — recorded, not modified here** (directive: "Do not change Codex-owned egress/receipt code"). Handed off. |
| **FS-9** | GitHub read-surface insufficient for comprehensive tracking (capability 6b) | Bounded code | New client read methods (branches/open-PRs/reviews/workflow-runs/agent-sessions) — its own reviewed PR; not activation. |

**FS-2 and FS-3 are Founder/operational decisions, not code gaps.** FS-1, FS-5, FS-6 are
sequencing/hygiene. **FS-7 is a now-owned security P1; FS-8 is a Codex-owned security P1 (handed
off).** FS-4 is design-only; FS-9 is bounded read-surface code. None invents product architecture.

---

## 5. Next feasible development steps (safe, ordered, no activation)

1. **Land the security P1s** (#371, #372, the new DB-role candidate FS-7) and **#323** behind
   independent review — independent of the activation stack, reduce risk/CI noise immediately.
2. **Record this readiness matrix's classifications as capability records** (`app.capability_reality`)
   once a first run is authorized — making capability #4 operational for the whole project.
3. **Session-start context assembly: design first, build later** (FS-4) — see
   `docs/LIFE_SESSION_CONTEXT_ASSEMBLY_DESIGN_2026-11-09.md`; implementation deferred until
   prerequisites + owner are confirmed. Do **not** build the older `founder_vault/` script.
4. **(Bounded code, own PRs when owned/authorized):** GitHub client read-surface (FS-9); an
   interactive/result-returning agent runtime beyond single-shot (capability 7). Not started here.
5. **Sequence the activation stack** per §3 only as each dependency actually merges (merge-rule).
6. **Founder decision point:** authorize the first egress-gated, spend-bounded cycle (FS-2/FS-3),
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
