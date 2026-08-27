# ACTIVE WORK — Claude (Life Vault / External-AI Egress Control)

**Owner:** Claude
**Branch:** `claude/life-vault-egress-control`
**Integration tip at branch create:** `4261787` (#167 merged) + `bf843cb` (goal-worktree ownership fix)

## Claimed now — DONE, PR pending

| Surface | Purpose |
|---|---|
| `docs/LIFE_VAULT_EGRESS_CONTROL.md` | Threat model, architecture map, blocker map (deliverable step 1) |
| `alembic/versions/0062_provider_disclosure_ledger.py` + `app/models/provider_disclosure.py` | Disclosure ledger — append-only, RLS, real erasure path |
| `app/egress_policy/service.py` | The default-deny gate (`enforce_egress_policy`) |
| `app/provider_planning/service.py` (`plan_with_provider`) | Gate wired in immediately before the real `.propose()` call, after spend reservation |
| `app/rls.py`, `app/account/erasure.py` | Privilege verification + erasure wiring for the new table |
| `tests/backend/mainai/test_egress_policy.py` + 2 new tests in `test_provider_assisted_planning.py` | Adversarial suite (attack-list subset #3/#4/#6/#7/#8/#9 + ledger completeness) |

Evidence: 9/9 new unit tests pass, 17/17 (15 existing + 2 new) `test_provider_assisted_planning.py`
pass (no regression from inserting the gate mid-function), `ruff` clean, alembic round-trip
clean (single head 0062), `test_runtime_table_privileges.py` passes ("privilege state verified
correct"). 3 unrelated `test_account_erasure.py` failures confirmed pre-existing (same failure
with this branch's changes fully stashed) — the same local-Postgres-timezone lease-comparison
artifact documented repeatedly elsewhere this session.

## Not touched

- `app/development_supervisor/production_entry.py`, `app/development_operator/service.py`, `app/development_supervisor/production_worktree.py` — Cursor's #167 goal-worktree ownership lane, explicitly locked per `docs/ACTIVE_WORK_CURSOR_167_GOAL_WORKTREE.md`.
- `app/routers/chat.py`, `app/rag/*` — surveyed and documented as the highest-exposure gaps, but explicitly out of scope for the first PR (see blocker map). Not modified.
- No real external provider activation — fake/local adapters only in tests, matching the standing directive.

## Standing boundaries

- Default egress = DENY.
- No cloud provider treated as trusted vault component.
- Prompt-injection doctrine: retrieved/external content is DATA, never AUTHORITY.
- No founder-private data leaves the process without an explicit, logged policy decision.
