# Independent certification — coordination PRs #300 / #301 / #303 / #314

Examiner: Claude (independent certification role, as the frozen PR bodies designate). Executed
2026-11-04 against each candidate's exact frozen SHA in a detached worktree, Python 3.12, real
Postgres (pgvector/pg16) + RLS. No frozen candidate was modified; all probes are examiner-owned
(`docs/examiner_probes/`), run against the frozen worktree and never committed into a candidate.

**Verdicts:** #300 PASS · #301 **FAIL** (DEF-1/GH-4) · #303 PASS (repairs #301) · #314 PASS.

Boundaries held throughout: no GitHub writes, no merge, no paid-provider activation, no STRATO,
`first_cycle_go=false`. This is engineering evidence, not an autonomy GO.

---

## #300 — Read-only GitHub development intake — **PASS**

- **Frozen SHA** `2c9dd7e06dd3072f8f1f31531437f285ae978e5f` (matches PR body). Base
  `cursor/durable-coordination-acceptance-91c1` (#299). Draft; Claude-owned certification.
- **Candidate tests:** `test_github_readonly_intake.py` + `tests/backend/mainai/
  test_durable_agent_coordination_integration.py` = **14 passed** on real Postgres (includes
  ledger persist→reload and three-agent-survives-reload = restart recovery).
- **Examiner adversarial probes** (`zz_examiner_probe_300.py`, 6 passed):
  - GET-only client; no `Authorization` header sent when public (read-only authorization).
  - Rate-limit (429/403+remaining=0) and forbidden (401/403) → **BLOCKED** with a named missing
    capability (`github_read_rate_limit` / `github_read_permission`); nothing ingested.
  - Forged `head_sha` (not a git object) → `forged_rejected`, no obligation created.
  - Duplicate PR ingest → examiner obligation is idempotent (same id, `duplicate_examiner=True`).
  - Webhook replay → `replay_ignored`; stale/foreign SHA → `stale_rejected`; required-check
    impersonation (non-`github-actions` app) → `forged_rejected`.
  - Never claims agent delivery or GitHub publication (`delivery_claimed=False`,
    `write_attempted=False`); `first_cycle_go=False`.
- **Conclusion:** read-only intake is sound — authorization, availability degradation,
  anti-spoofing, idempotency and durability all hold.

## #301 — Agent-lifecycle ingestion + autonomous coordination — **FAIL (DEF-1/GH-4)**

- **Frozen SHA** `a786dac9f32bc3120fa2176e18041a75f1cf6c56` (matches PR body). Base
  `cursor/github-readonly-intake-91c1` (#300). Draft; Claude-owned certification.
- **Candidate tests:** `test_lifecycle_autonomous_coordination.py` = **5 passed**. Lifecycle
  ingestion is restart-safe via a persisted-event watermark; actor authorization and
  no-delivery-claim hold.
- **Defect (confirmed, executable):** `zz_examiner_probe_301.py` reproduces DEF-1/GH-4 — a
  GitHub discovery with **no pre-existing `AgentWorkAssignment`** is not durable. `_persist_ledger`
  early-returns when the owner has zero assignment rows (line ~605), and GitHub-discovered
  builder/examiner obligations are in-memory `service.assign` records with no backing row, so
  nothing is written. Probe result: `assignment_rows_after_persist=0 reloaded_github_candidates=0`
  — the discovered candidate is lost on reload, and a later refresh recreates it with new ids.
- **Conclusion:** the core "Life reads GitHub and durably tracks obligations" capability is not
  durable in the cold-start case. **Not certifiable as-is.** Remediated by #303 (below) — the
  narrowly-scoped repair already exists, so no new repair is warranted from the examiner.

## #303 — Coordination handover + authorized transports (repairs #301) — **PASS**

- **Frozen SHA (body)** `74ebda974ff6952c7c7ca15f837430a73c594b43`; examined at that SHA.
  **Discrepancy to resolve before merge:** the branch head has since moved to
  `296de97fa…` — re-confirm/re-freeze the certified SHA. Base
  `cursor/agent-messaging-delivery-gateway-91c1` (#302).
- **Candidate tests:** `test_agent_messaging_delivery_gateway.py` +
  `test_coordination_operational_handover.py` = **10 passed** (incl. three-agent real-bridge
  restart round-trip and authorized local-CLI-uses-existing-gateway).
- **Fix verification** (`zz_examiner_probe_303.py`, passed): the same cold-start scenario that
  failed on #301 now survives. `persist.py::persist_owner_ledger` creates exactly one synthetic
  owner-scoped "ledger backing" assignment (`ledger_backing`/`created_without_manual_assignment`)
  and writes the full ledger (incl. GitHub intake) onto it; `reconcile_owner` skips that row's
  bookkeeping events. Probe result: `backing_rows=1 cold_backing=True reloaded_candidates=['299']`,
  and a second cold discovery reuses the same backing row (`total_rows=1 backing_rows=1` — no
  duplicate).
- **Conclusion:** DEF-1/GH-4 is fixed; durable, idempotent, restart-safe. Certifiable at the
  examined SHA once the head-drift is re-frozen.

## #314 — Authorized operating cycle + SPEND-STOP + absence — **PASS**

- **Frozen SHA** `3aa2bec84665db3225c4fc0f99598d56747a0f7e` (matches PR body "head after registry
  naming"). Base `cursor/provider-integration-stop-icv-91c1` (#312); composes #310 handover.
- **Candidate tests:** `test_delegate_wiring_spend_stop.py` (7) +
  `test_operational_handover_self_improve.py` (6) = **13 passed**.
- **Examiner adversarial probes** (`zz_examiner_probe_314.py`, 2 passed):
  - `run_authorized_operating_cycle` realizes the full loop (goal → task selection →
    capability-based agent selection → authorized delegation → authenticated provider ingestion
    → independent examination → next eligible) and stamps `first_cycle_go=False`,
    `live_paid_mutate=False`, `vendor_delivery_claimed=False` on every result.
  - Kill-switch/STOP → `assert_grant_allowed` raises and the cycle returns `stop_rejected` with
    **0 obligations** (fail-closed); the SPEND-STOP hook in `provider_spend.reserve_provider_spend_call`
    refuses the reservation under STOP/unestablished authority. Interactive Founder work stays
    admitted even when background is starved.
- **Conclusion:** the operating cycle is authority-gated, spend-bounded and fail-closed.

---

## Phase 2 — integration base + the one missing integration

Two independently-verified lineages exist:

- **A — durable coordination / GitHub intake / delivery / handover**, culminating in **#303**
  (which supersedes #301's DEF-1/GH-4 defect and adds the messaging/delivery gateway).
- **B — provider integration / STOP / operating cycle**, culminating in **#314**
  (`run_authorized_operating_cycle` = the required development-management loop).

The required loop (Founder objective → task selection → agent assignment → execution →
independent review → correction → GitHub evidence → next eligible task) is **realized by #314**,
and real GitHub state + durable obligation persistence are **realized by #303**.

**The single missing integration:** #314's base (#312) does not include #303's GitHub-intake +
`persist.py` durable backing, so the operating cycle's "task selection" is not yet fed by real
GitHub state in one durably-persisted ledger. The smallest coherent missing integration is a
compose branch that brings #303's intake+persist onto #314's operating-cycle base (superseding
#301 with #303), then wires `run_authorized_operating_cycle`'s task selection to the
GitHub-intake-fed, durably-persisted ledger.

**Why the examiner does not perform that merge here:** both lineages are frozen candidates owned
by the cursor agent; the directive requires not silently modifying frozen candidates, and the
repo's standing discipline forbids duplicating active agent work. The integration is therefore
specified as the precise next branch for the owning agent / Founder decision, rather than a
unilateral cross-lineage merge. #301 is the only FAIL, and its repair (#303) already exists, so
no new narrowly-scoped repair is needed from the examiner.

### Recommended merge order
1. Re-freeze #303 at a confirmed SHA (resolve the head drift noted above), merge the A-lineage
   up to #303 (dropping #301 as superseded).
2. Merge the B-lineage up to #314.
3. Open the compose/integration branch wiring #314's operating cycle to #303's GitHub-intake +
   durable ledger — the first point at which the Founder's manual message-passing is replaced by
   Life's own supervised loop over real GitHub evidence.

All verdicts are reproducible with the examiner probes in `docs/examiner_probes/` (run each
against the corresponding frozen SHA in a detached worktree).
