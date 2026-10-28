# Effect-Specific Database Authority — Codex `460e8ad8` (migration 0099)

- **Candidate:** `460e8ad8cbd40b7fb0d62702f28c14412688e298`
  (branch `codex/effect-specific-db-authority`), tree `cfaeade4…`.
- **Direct parent:** `e8d6fc8ef70c3e51051c7fdbbcdaa6e8c3ba4832` ("fully-tested backend");
  `460e8ad8` over it is only `docs: fix authority contract whitespace` (one blank-line
  deletion). `backend/` is **byte-identical** to `e8d6fc8` (`git diff e8d6fc8 460e8ad8 --
  backend` empty).
- **Frozen examined parent:** `078312d7abce3f5c0f87c9e7b30161973e55d532` (the FA-PRIV/STOP
  closure, previously certified) — confirmed an ancestor.
- **Suite:** `probes/fc_effauth_v1` — **7 passed** (`RESULTS_460e8ad.txt`). DB-backed via the
  target's own `tests.conftest` (`alembic upgrade head` through `0099_effect_db_authority`
  + `apply_mainai_execution_privileges`), loopback-only egress guard, no paid calls, no
  production mutation, target worktree unchanged. `first_cycle_go=false`.

## Archive integrity (independently verified, read-only)

| Check | Expected | Result |
|---|---|---|
| Outer tarball SHA-256 | `7a769d8c…33ca52` | **MATCH** |
| Inner `SHA256SUMS` (8 files) | — | all **OK** |
| `git bundle verify` | complete history | **okay**, single ref |
| HEAD after checkout | `460e8ad8…` | **MATCH** |
| HEAD tree | `cfaeade4…` | **MATCH** |
| Frozen parent ancestry | `078312d7` ancestor of `460e8ad8` | **YES** |
| `backend` vs `e8d6fc8` | byte-identical | **empty diff** |
| Alembic head | single `0099_effect_db_authority`, down_revision `0098_fa_priv_stop_boundary` | **confirmed** |

Verification was done in an isolated throwaway repo with `GIT_CONFIG_GLOBAL=/dev/null`,
`GIT_CONFIG_NOSYSTEM=1`, tokens unset — not by running the archive's own
`verify-handoff.sh` (read, not executed). The archive's own evidence claim (focused 50
passed; builder's new `test_effect_specific_database_authority.py`) was **independently
reproduced**: that file's 4 tests pass on the exact SHA here.

## What migration 0099 actually changes (new work over `078312d7`)

- `REVOKE UPDATE ON execution_authorization_envelopes FROM mainai_app` (table-level, replacing
  the former `GRANT UPDATE(status,revoked_at,revocation_reason)`).
- New `security_kernel_revoke_execution_envelope(p_owner_id,p_envelope_id,p_expected_global_epoch,
  p_expected_owner_epoch,p_reason)` — `SECURITY DEFINER`, `search_path=pg_catalog`,
  `REVOKE ALL … FROM PUBLIC, mainai_app`, with an in-body `session_user/current_user='mainai_app'`
  refusal, complete-binding + reason-length validation, canonical GLOBAL-then-owner epoch
  fencing under `FOR SHARE`/`FOR UPDATE`, exact owner+envelope row lock, idempotent on already-
  `revoked`, and a one-way `active→revoked` guard.
- `rls.py`: registers the new function with `mainai_app_execute=False` and broadens the
  envelope REVOKE in `apply_mainai_execution_privileges`.
- Router: `revoke_execution_scope_route` switched from `get_db` (runtime) to
  `get_security_kernel_db` (privileged control-plane session) behind `require_founder`.
- Service: `revoke_execution_authorization_envelope` reads epochs as comparison inputs and
  delegates the transition to the privileged function (the function re-reads under locks).

The "one audit event" is a row in append-only `security_kernel_authority_events`, written by
the pre-existing **`execution_authorization_envelopes_audit` AFTER UPDATE trigger**
(`security_kernel_record_authority_event`, event_type `execution_envelope_revoked`) — i.e. in
the **same transaction** as the status UPDATE. Neither the function nor the service writes it
directly; the coupling is the trigger's.

## Examiner findings — all PASS (new boundary, not builder duplication, not re-run certified controls)

| ID | Property examined | Result |
|---|---|---|
| EA1 | Runtime role has **no UPDATE at all** on the envelope table — both a formerly-granted transition column (`revocation_reason`) and a never-grantable column (`goal_id`) are denied; envelope stays `active` | PASS — proves the table-level REVOKE, broader than the old 3-column grant |
| EA2 | **Defense in depth:** even after a hypothetical future `GRANT EXECUTE … TO mainai_app` (granted then restored in `finally`), calling through the real runtime LOGIN role is refused by the body (`runtime role cannot revoke execution authority`); EXECUTE confirmed re-denied afterwards | PASS — the exact scenario the migration comment claims |
| EA3 | Incomplete/malformed binding fails closed **inside the function**: NULL envelope, NULL owner, negative global epoch → `complete execution-revocation binding is required`; blank and 1025-char reason → `reason must contain 1..1024 characters` | PASS (builder covered cross-owner + stale-owner epoch, not these input guards) |
| EA4 | **GLOBAL** authority-epoch staleness is fenced (builder covered the OWNER epoch): wrong global epoch → `authority epoch is stale`; envelope stays `active` | PASS |
| EA5 | **Transition ⇄ audit atomicity:** a rolled-back revoke leaves the envelope `active` **and zero** `execution_envelope_revoked` audit rows (verified in-txn the transition + audit row are both present before rollback); a committed revoke leaves **exactly one** transition and **exactly one** audit event | PASS — no half-state, no orphan audit, no missing audit |
| EA6 | **Deterministic TOCTOU close:** holding a concurrent owner STOP's epoch-row lock uncommitted while a Founder revoke carrying the pre-STOP epoch is in flight — the revoke **blocks** on the function's `FOR UPDATE` re-read, then fails `authority epoch is stale` once the STOP commits. Net effect of the race: **exactly one** envelope transition and **one** audit event (the STOP's cascade); the racing revoke adds neither | PASS — the read-then-act window is genuinely closed by the lock + epoch re-read, not just by a sequential check |
| EA7 | **Compatibility:** after 0099, `mainai_app` has no table UPDATE and no EXECUTE on the new function; the certified FA-PRIV/STOP op (`security_kernel_revoke_authority_for_stop`) is **still** runtime-denied (no regression); the runtime keeps SELECT-only on the audit table; the audit-write, one-way-transition and append-only triggers are all still installed | PASS |

### Interpreted result
On its exact SHA, migration 0099 moves execution-envelope revocation from a
runtime-reachable column UPDATE to a **Founder-authenticated, migration-owner, epoch-fenced,
row-locked** privileged operation, and the examiner's adversarial probes confirm the
boundary holds along the axes the task named:

- **Direct runtime mutation denial** — EA1 (table-level), EA7 (privileges).
- **Owner/envelope/epoch binding** — EA3 (binding completeness); builder EA covers exact
  owner match.
- **Forged authorization context & stale authority** — EA2 (forged runtime identity even
  with EXECUTE), EA4 (stale GLOBAL epoch). A forged `app.current_user_id` GUC is now only
  client input; the function trusts explicit parameters + the login role + locked epochs.
- **Concurrent revocation & replay** — EA6 (deterministic STOP-vs-revoke TOCTOU → one
  transition, loser fails closed); builder covers two-thread idempotency.
- **Transaction rollback & audit consistency** — EA5 (atomic transition⇄audit coupling).
- **Privileged procedure restrictions** — EA1/EA2/EA7.
- **Compatibility with STOP-clear & FA-PRIV** — EA7 (prior controls still enforced; not
  re-run).

**No defect found in the new effect-specific authority boundary on `460e8ad8`.** No merge,
deployment, Recall activation, provider spending, publication or autonomy GO was performed
or implied; `first_cycle_go=false` is respected. This is an independent read-only
examination and acceptance of the new boundary, not a certification of the whole candidate
and not a production-operation sign-off.

## Scope / discipline
Read-only examiner. Archive verified with isolated git config and matched hashes; candidate
materialised at the exact SHA in a detached worktree and left unchanged. Tests drive the
real production function, privileges and triggers through the target's own conftest. No paid
calls; loopback-only. Codex/Cursor branches unmodified.

## Reproduce
```
# archive (untrusted input, isolated dirs):
shasum -a 256 effect-specific-db-authority-460e8ad.tar.gz        # == 7a769d8c…33ca52
tar -xzf … -C <empty dir>; (cd … && shasum -a 256 -c SHA256SUMS)
git -C <throwaway> bundle verify effect-specific-db-authority.bundle

# examiner suite on the exact SHA:
probes/fc_effauth_v1/run.sh ../../wtcert-460e8ad                 # 7 passed
```

## Next actionable blocker
None gating this examination. Standing re-run triggers unchanged:
- a corrected #315 successor → `fc315crash_v1` (CC1 must flip to PASS), `fc315http_v1`;
- a production operating-cycle driver / authenticated provider-webhook route → `fc_lifecycle_v1`
  RT3 flips to PASS.
Beyond those, the next distinct unexamined boundaries with concrete evidence available are
the **spend / transport / Recall-ownership integration contracts** that EVIDENCE.md states
0099 deliberately left unchanged (documented, not silently altered) — candidates for a
separate authorized examination.
