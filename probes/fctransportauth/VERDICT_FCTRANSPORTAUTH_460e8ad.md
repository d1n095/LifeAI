# Transport Database Authority — `460e8ad8` (push / draft-PR / remote-effect)

- **Subject:** the PostgreSQL authority for transport effects on candidate head
  `460e8ad8cbd40b7fb0d62702f28c14412688e298`:
  - `mainai_push_grants` — guard `restrict_push_grant_mutation` (migrations 0090/0092, final
    0095 redefinition), partial unique index `uq_mainai_push_grants_one_active_task`,
    `uq_mainai_push_grants_preflight_once` (0093), CHECK `ck_push_grant_consumption`;
    runtime column-UPDATE grant on `(status, consumed_at, consumed_by_job_id)` only; DELETE
    revoked; FORCE RLS owner isolation.
  - `mainai_draft_pr_grants` — guard `restrict_draft_pr_grant_mutation` (0091); runtime
    column-UPDATE grant on `(status, consumed_at)` only; DELETE revoked; FORCE RLS.
  - `mainai_remote_effect_attempts` — append-only receipt of the actual remote effect, guard
    `restrict_remote_effect_attempt_mutation` (0091): the DB record coupling *committed
    authorization* to a *remote effect*, one-way reconcilable from `claimed`.
- **Suite:** `probes/fc_transportauth_v1` — **9 passed** (`RESULTS_460e8ad.txt`). DB-backed via
  the target's own `tests.conftest`; grants built through the product's own
  authorize→preflight→grant chain (faithful replica of
  `test_activation_authority_substrate.authority_graph`), then attacked at the DB level.
  Loopback-only egress guard; no paid calls; no production mutation; target worktree
  unchanged. `first_cycle_go=false`.

## Scope / non-duplication
Does **not** repeat the completed GitHub publication tests
(`test_founder_review_publication`, `test_publication_readiness`) nor the builder's own
authority-state-machine suite (`test_activation_authority_substrate`, which covers the
service consume/expiry/base-SHA paths and the single `terminal push grant state is immutable`
assertion). The push/draft/remote guard messages, CHECK, unique indexes and append-only
behaviours exercised here are asserted nowhere else in `tests/` (verified by grep). The
builder's DB-boundary suite proves only that the runtime has *no table-level UPDATE* and
cannot change binding columns (permission-denied); this suite proves the **trigger/constraint
backstop** and the single-use/append-only/RLS behaviour.

## Examiner findings — all PASS

| ID | Property examined | Result |
|---|---|---|
| TR1 | Push grants are auditable — **neither** the runtime (DELETE privilege revoked → permission denied) **nor** a privileged writer (trigger `push grants are auditable and cannot be deleted`) can delete one | PASS (dual layer) |
| TR2 | A push grant may only leave `active` into a terminal state — a no-op/return to `active` is rejected (`invalid push grant transition`) | PASS |
| TR3 | Push-grant **authority bindings are immutable** even for a privileged writer; only `(status, consumed_at, consumed_by_job_id)` may change — a `branch` change alongside a consume is rejected (`push grant authority bindings are immutable`) | PASS |
| TR4 | **Consumption integrity**: `status='consumed'` requires BOTH `consumed_at` and `consumed_by_job_id` (`ck_push_grant_consumption`); a consume with both set is accepted | PASS |
| TR5 | **Single-use under concurrency**: two racing `consume_push_grant` calls on the same active grant → exactly one consumes it, the other fails closed (`PushGrantConsumed`); final status `consumed` | PASS — atomic `UPDATE … WHERE status='active'` + row lock |
| TR6 | **At most one ACTIVE grant per task** is a DB invariant — a second active grant for the same `(owner, task)` is rejected by a unique index (`one_active_task` / `preflight_once`) | PASS |
| TR7 | Draft-PR grants are auditable (runtime DELETE denied; trigger `draft PR grants cannot be deleted`) and their **bindings immutable** (`draft PR grant bindings are immutable`) | PASS (dual layer) |
| TR8 | **Committed authorization → remote effect** is an append-only receipt: it cannot be deleted (`remote effect receipts cannot be deleted`), its bindings are immutable (`remote effect bindings are immutable`), it reconciles ONE WAY from `claimed` to a terminal observed outcome, after which it is frozen (`terminal remote effect receipt is immutable`) | PASS |
| TR9 | **RLS owner isolation**: a runtime authenticated as owner A targeting owner B's push grant matches **zero rows**; B's grant is unchanged | PASS — no cross-owner escalation via a forged `app.current_user_id` |

### Interpreted result
On its exact SHA, the transport DB authority holds along every axis the task named:

- **Effect-specific permissions** — the runtime's column-scoped UPDATE grant plus the guards
  (TR1/TR3/TR7) form a least-privilege surface; bindings and deletion are refused even to a
  privileged writer.
- **Single-use consumption** — TR4 (consumption CHECK), TR5 (atomic concurrent consume),
  TR6 (one-active-per-task / preflight-once unique indexes).
- **Stale grants** — the service `consume_push_grant` already fences expiry / base-SHA-moved /
  authority-version / superseded-envelope (builder-covered; confirmed as the consume path
  used in TR5, not re-asserted here).
- **Concurrent claims** — TR5 (exactly one winner).
- **Runtime role restrictions** — TR1/TR7 (runtime DELETE denied), TR9 (RLS isolation).
- **Committed authorization vs remote effects** — TR8 (append-only, one-way-reconcilable
  receipt binding a consumed push grant to its actual remote effect).

**No defect found in the transport database authority boundary on `460e8ad8`.** The authority
is removed from, or made immutable to, the runtime at the database layer; a push/draft grant
is single-use and bound; and the remote-effect receipt is an append-only audit of what was
actually attempted. No merge/deploy/publication/autonomy was performed or implied.

## Scope / discipline
Read-only examiner; drove the real guards, CHECK constraints, unique indexes, RLS and the
`consume_push_grant` service path through the target's own conftest; grants constructed via
the product's own authorize→preflight→grant chain. No paid calls; loopback only; no GitHub
operation, publication, deployment or autonomy. Candidate materialised detached at the exact
SHA and left unchanged. `first_cycle_go=false`.

## Reproduce
```
probes/fc_transportauth_v1/run.sh ../../wtcert-460e8ad      # 9 passed
```

## Next (continuation)
Priority 3 — Recall security boundary (`personal_recall_owner_keys` / `personal_recall_grants`
/ protected data): identity binding, revocation, erasure permissions, protected-data
isolation, fail-closed — with Recall kept inactive and no changed identity certified by prior
certification.
