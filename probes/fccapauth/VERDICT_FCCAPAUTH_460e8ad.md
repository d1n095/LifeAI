# Founder Capability & Authority-Change Core — `460e8ad8` (migration 0094)

- **Subject:** the PostgreSQL authority for the founder capability / authority-change core on
  candidate head `460e8ad8cbd40b7fb0d62702f28c14412688e298`:
  - `founder_authority_change_proposals` — proposals; one terminal transition
    `proposed → {applied,rejected,cancelled}`; owner CHECK = FOUNDER.
  - `founder_step_up_receipts` — step-up receipts; one-way consumption.
  - `founder_capability_grants` — capability grants; one terminal transition
    `active → {consumed,cancelled,expired}`; `UNIQUE(owner, idempotency_key)`.
  - `founder_capability_policies` — versioned policies; authority fields immutable; at most one
    ACTIVE policy per `(owner, capability_key)` (partial unique index); `version > 0`.
  - `founder_authority_audit_receipts` — immutable, hash-chained audit.
  - All tables: `owner_id = FOUNDER` CHECK + FORCE owner-scoped RLS; `founder_authority_immutable_guard`
    fires BEFORE UPDATE. Runtime (0095) gets SELECT on all + INSERT on proposals only.
- **Suite:** `probes/fc_capauth_v1` — **10 passed** (`RESULTS_460e8ad.txt`). DB-backed via the
  target's own `tests.conftest`; authority rows built with privileged raw INSERTs (owner = FOUNDER,
  in dependency order) since 0094 installs no INSERT-time trigger. Loopback-only egress guard;
  no paid calls; no production mutation; target worktree unchanged. `first_cycle_go=false`.

## Scope / non-duplication
None of the 0094 guard messages exercised here is asserted anywhere in `tests/` (verified by
grep). This is the unexamined capability/authority-change DB boundary.

## Examiner findings — all PASS

| ID | Property examined | Result |
|---|---|---|
| FC1 | **Privilege escalation blocked**: a non-Founder cannot create an authority proposal — owner = attacker fails the owner CHECK (`ck_founder_proposal_owner`); owner = FOUNDER while acting as the attacker fails the FORCE RLS `WITH CHECK` (`row-level security`) | PASS |
| FC2 | The runtime **cannot mint** a capability grant or policy — SELECT-only privilege → INSERT denied on both tables | PASS |
| FC3 | Audit receipts are **immutable** and the runtime cannot write them — runtime INSERT privilege-denied; a privileged UPDATE is refused by the guard (`Founder authority audit receipts are immutable`) | PASS |
| FC4 | Step-up receipts permit **only one-way consumption** — `consumed_at` NULL→set with nothing else changed is allowed; changing any other field while consuming is refused (`step-up receipts permit only one-way consumption`) | PASS |
| FC5 | Capability grants permit **only one terminal transition** — `active→consumed` (bindings unchanged) allowed; a second transition off the terminal state, and a transition that mutates a binding, are both refused (`capability grants permit only one terminal transition`) — no replay, no resurrection | PASS |
| FC6 | **One ACTIVE policy per capability** — a second active policy for the same `(owner, capability_key)` is rejected by the partial unique index; a policy's authority fields are immutable (`active→superseded` unchanged allowed; mutating `decision` on transition refused: `capability policy authority fields are immutable`) | PASS |
| FC7 | Authority proposals permit **only one terminal transition** — `proposed→applied` (unchanged) allowed; a second transition off `applied`, and a transition mutating a binding, are refused (`authority proposals permit only one terminal transition`) | PASS |
| FC8 | **Owner isolation** — a foreign owner sees zero of the Founder's proposals, grants, and policies | PASS |
| FC9 | **Concurrent** terminal transition on a grant — two racing `active→consumed` updates → exactly one commits, the other is refused by the guard (`one terminal transition`); final status `consumed` | PASS |
| FC10 | **Replay** — a duplicate grant `idempotency_key` for the owner is rejected by `uq_founder_capability_grant_idem` | PASS |

### Interpreted result
On its exact SHA, the founder capability/authority-change core holds along every axis the task
named:

- **Capability grants** — FC2 (runtime cannot mint), FC5 (single terminal transition), FC9
  (concurrent), FC10 (idempotency/replay).
- **Proposals** — FC1 (only the Founder may create), FC7 (single terminal transition).
- **Privilege escalation** — FC1 (owner CHECK = FOUNDER + RLS), FC2 (SELECT-only runtime).
- **Stale authority** — FC5/FC7 (a consumed/applied authority cannot be re-driven), FC4
  (a consumed step-up cannot be reused).
- **Concurrent changes** — FC9 (exactly one winner).
- **Replay** — FC10 (idempotency unique), FC5/FC4 (no re-consume).
- **Owner isolation** — FC8 (FORCE RLS), FC3 (audit immutable + runtime-denied).

**No defect found in the founder capability / authority-change core on `460e8ad8`.** Every
authority row is Founder-owned and owner-isolated; capability grants, policies, step-ups and
proposals are single-use / single-terminal-transition with immutable bindings; the audit trail
is immutable; and the runtime can only propose, never mint or transition.

## Scope / discipline
Read-only examiner; drove the real CHECK constraints, the `founder_authority_immutable_guard`,
the unique indexes and FORCE RLS through the target's own conftest; authority rows built via
privileged raw INSERTs. No paid calls; loopback only; no publication/deployment/autonomy.
Candidate materialised detached at the exact SHA and left unchanged. `first_cycle_go=false`.

## Reproduce
```
probes/fc_capauth_v1/run.sh ../../wtcert-460e8ad      # 10 passed
```

## Continuation status
Priorities 1–5 of the continuous DB-authority examination are complete on `460e8ad8`, each
with an executable suite, verdict and examiner commit, and no defect found. The effect-specific
DB authority family (envelope 0099, spend, transport, Recall), account-erasure operation
authority, and the founder capability/authority-change core have now all been independently
examined end-to-end on this lineage. Awaiting either a new corrected engineering candidate
(whose outstanding acceptance gates would be prioritized by security severity and release
impact) or direction on a further boundary.
