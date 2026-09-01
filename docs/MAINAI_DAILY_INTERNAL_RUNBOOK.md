# MainAI Daily Internal Runbook — SAFE INTERNAL mode

**Milestone:** `READY_FOR_SAFE_INTERNAL_RUN`  
**Provider invoke:** DISABLED until independent Claude gates say otherwise.  
**This is operator contract, not tribal knowledge.**

Integration tip after #236: record SHA from `git rev-parse origin/claude/det-kommer-mer-879lcm`.

---

## HOW TO START HER

From repo `backend/` (local ops/test DB — never silently prod):

```bash
cd backend
# Creates/migrates lifeos_safe_internal on localhost:5433 if needed.
python scripts/mainai/safe_internal_boot.py --json /tmp/mainai-boot-receipt.json
```

Success requires: `BOOT_SUCCESS=True`, `provider_call_count=0`.

Library entrypoint:

```python
from app.mainai_executive.internal_start import run_first_real_internal_boot
```

---

## HOW TO CHECK STATUS

```python
from app.mainai_executive.internal_start import startup_status_surface
# or founder dashboard:
from app.mainai_executive.dashboard import founder_executive_dashboard
```

Surface fields: MAINAI_STATUS, READINESS, CURRENT_GOAL, CURRENT_PLAN, CURRENT_TASK,
ACTIVE_LOCAL_AGENTS, BLOCKED_WORK, LAST_CHECKPOINT, LAST_FAILURE, LAST_RECOVERY,
SCHOOL_STATUS, LOCAL_VS_EXTERNAL_DEPENDENCY, KILL_SWITCH, PROVIDER_ENABLED.

No chain-of-thought.

---

## HOW TO GIVE HER A SAFE INTERNAL TASK

```bash
python scripts/mainai/safe_internal_boot.py \
  --task "Classify this public museum weekend notice for research; park NOW follow-ups unreviewed." \
  --json /tmp/mainai-task-receipt.json
```

Or call `run_executive_cycle(..., need_capability="low_risk_classification", run_workforce_dry=True)`.

---

## HOW TO SEE CURRENT GOAL / PLAN

Status surface `CURRENT_GOAL` / `CURRENT_PLAN`, or continuity checkpoint via
`load_continuity_checkpoint(db, owner_id=..., session_id=...)`.

---

## HOW TO SEE MEMORY / WHY / DECISION DEBT

- Inspectable memory: `app.inspectable_memory`
- Why / debt: `app.mainai_executive.why_graph.list_decision_debt`
- Dashboard: `founder_executive_dashboard`

---

## HOW TO SEE LOCAL AGENTS

`founder_executive_dashboard` → `WHAT_AGENTS_ARE_WORKING` / org snapshot.
Also: `app.workforce.org_view.organization_snapshot`.

---

## HOW TO SEE SCHOOL / LEARNING STATUS

Status surface `SCHOOL_STATUS`, or after a cycle: `result.school_path`.
Independence: `app.mainai_school.metrics.snapshot_domain("research")`.

---

## HOW TO SEE READINESS

```python
from app.mainai_startup_readiness import evaluate_startup_readiness
evaluate_startup_readiness(claude_reviews_satisfied=None).level
```

Expected for this phase: `READY_FOR_SAFE_INTERNAL_RUN`.

---

## HOW TO PAUSE

Do not issue new `run_executive_cycle` calls. Durable checkpoint remains.
Attention helpers: `app.mainai_executive.attention.decide_attention` (pause/defer).

---

## HOW TO RESUME

```python
from app.mainai_executive import resume_executive_cycle
resume_executive_cycle(db, owner_id=..., session_id=..., continue_work=True)
```

Authority after resume remains **invalid** until founder confirmation — by design.

---

## HOW TO STOP

Stop the process after checkpoint is written. Boot script records shutdown then proves restart.
Do not rely on process RAM.

---

## HOW TO ACTIVATE KILL SWITCH

Owner stop (one owner only):

```python
from app.workforce.kill_switch import activate_owner_stop
activate_owner_stop(db, owner_id=..., reason="operator_stop")
```

Global emergency (everyone):

```python
from app.workforce.kill_switch import activate_global_emergency_stop
activate_global_emergency_stop(
    db, reason="system_emergency",
    founder_authority_ref="founder_ack:declare-global-stop",
)
```

## HOW TO CLEAR (NEVER AUTOMATIC / NEVER FROM BOOT)

```python
from app.workforce.kill_switch import clear_owner_stop
clear_owner_stop(
    db, owner_id=...,
    founder_ack="founder_ack:explicit-reason-text",
    clear_request_id=uuid.uuid4(),  # unique; replay rejected
)
```

Fabricated strings like `composed_safe_internal_clear` are **rejected**.
Boot must surface `BLOCKED_BY_KILL_SWITCH` and must not clear.

---

## HOW TO RECOVER FROM FAILURE

1. Check kill switch and readiness.
2. `load_continuity_checkpoint` — do not invent missing state.
3. `resume_executive_cycle` with `needs_founder_confirmation=True` semantics.
4. Re-run boot only if checkpoint missing and founder accepts new session.

---

## HOW TO RESTART

Stop process → start again → `resume_executive_cycle` with same `session_id`,
or re-run `safe_internal_boot.py` for a fresh session.

---

## HOW TO VERIFY NO PROVIDER WAS CALLED

- Boot receipt: `provider_call_count == 0`
- Cycle: `workforce_dry_run["provider_invoked"] is False`
- Status: `PROVIDER_ENABLED is False`
- Activation gates remain unverified for provider path

---

## First-week internal mode

Run safe-internal sessions across several days. Track:

- successful sessions
- restart recoveries
- local completion rate
- local verification failures
- founder corrections
- memory retrieval misses
- duplicate work
- school lessons
- agent performance
- **API calls = expected zero**

See also: `docs/MAINAI_SAFE_INTERNAL_BOUNDARY.md`.
