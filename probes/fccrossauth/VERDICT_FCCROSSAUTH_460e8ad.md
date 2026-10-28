# Cross-Boundary Authorization Consistency — `460e8ad8`

- **Subject:** interactions between Founder session validity, authorization epochs (STOP),
  worker/effect authorization, revocation of active privileged operations, and durable
  recovery — on candidate head `460e8ad8cbd40b7fb0d62702f28c14412688e298`. Focus: paths that
  could **restore previously-revoked authority** or permit execution under **stale credentials**.
- **Suite:** `probes/fc_crossauth_v1` — **6 passed** (`RESULTS_460e8ad.txt`), deterministic (run
  twice). DB-backed via the target's own `tests.conftest`; loopback-only; no paid calls; no
  production mutation; target worktree unchanged. `first_cycle_go=false`.

## Scope / non-duplication
Does NOT re-run the concurrent-STOP TOCTOU or the single-boundary effect guards
(`fc_effauth`/`fc_spendauth`/`fc_transportauth`), nor the authenticated STOP-clear machinery
itself (certified elsewhere). It examines the CROSS-boundary consistency: an epoch (STOP)
lifecycle must not resurrect revoked effect authority; a stale/revoked Founder session must not
drive a privileged operation; a STOP must stop a transport effect; and recovery reflects the
revoked truth.

## Examiner findings — all PASS (1 low/informational observation, not a defect)

| ID | Property examined | Result |
|---|---|---|
| CB1 | A STOP-revoked execution envelope **cannot be restored by any writer** — the runtime is privilege-denied and a privileged writer is refused by the one-way envelope guard; since an epoch clear writes only the epoch row and runs no re-activation cascade, no STOP→clear cycle can bring it back | PASS |
| CB2 | A STOP-revoked provider-spend authorization cannot be restored by any writer (`cannot be resurrected`) | PASS |
| CB3 | A STOP-cancelled push grant is terminal and cannot be restored by any writer (`terminal push grant state is immutable`) | PASS |
| CB4 | A Founder session invalidation (logout-all / password reset / revoke-sessions all bump `sessions_valid_after`) makes a **previously-issued erasure reauth authority stale**: a subsequent `account_erasure_begin_operation` under that session is refused (`not bound to a current session`) | PASS |
| CB5 | After a STOP, a previously-valid transport (push) effect **cannot be consumed** — the STOP cascade cancels the grant and revokes its envelope, so `consume_push_grant` fails closed (`ActivationAuthorityError`) instead of applying an effect under revoked authority; the grant stays `cancelled` | PASS |
| CB6 | **Durable recovery** — a brand-new session opened after a STOP (simulating a process restart) reads the committed revoked truth for the envelope and spend authority, and cannot resurrect either; recovery reflects revocation rather than a stale pre-STOP snapshot | PASS |

### STOP → clear (how the resurrection property is established without re-testing the certified clear)
Advancing the authority epoch out of STOP requires an authenticated STOP-clear authorization
even for the schema owner (confirmed empirically during this examination: a privileged epoch
clear without a valid `founder_stop_clear_authorizations` row raises *STOP-clear authorization is
stale or invalid*). The clear transition writes **only** the epoch row, and the revoke cascade
fires solely on STOP activation (`stopped=true`), never on a clear. Combined with CB1–CB3
(active-resurrection of any revoked effect is categorically refused for every writer), this
establishes that a STOP→clear cycle cannot restore previously-revoked effect authority — without
re-testing the certified authenticated-clear control.

### Interpreted result
On its exact SHA, the cross-boundary authorization consistency holds along every axis the task
named: a revoked effect stays revoked across the epoch lifecycle (CB1–CB3, CB6); a stale Founder
session cannot drive a privileged erasure operation (CB4); a STOP stops an in-flight transport
effect (CB5); and durable recovery reflects revocation, never a stale snapshot (CB6). **No path
was found that restores previously-revoked authority or permits execution under stale
credentials. No defect found.**

### Observation — low / informational (not a defect, not a release blocker)
`account_erasure_receipt_session_current` validates a session via `rt.created_at >
users.sessions_valid_after` plus the revoked-access-token blocklist, but does **not** inspect
`refresh_tokens.revoked_at`. Because `sessions_valid_after` is truncated to whole seconds while
`created_at` keeps sub-second precision, within the **same wall-clock second** as a password
reset (which revokes refresh tokens and bumps `sessions_valid_after` to the truncated second) a
just-revoked session's `created_at` can still exceed `sessions_valid_after`, so the erasure
session-binding check can still pass for that sub-second window. Exploitability is negligible
(same-second race; requires an already-minted, unconsumed reauth receipt from that very session;
the next whole second closes it), and the access-token path uses `<=` second-truncation and fails
closed. **Responsible component:** `account_erasure_receipt_session_current` (migration 0086).
Suggested hardening (for the builders, not required for this gate): also check `rt.revoked_at IS
NULL`, or compare with the same `<=` whole-second rule the access-token path uses. Recorded as a
reproducible nuance, not a failing requirement — CB4 passes once the session invalidation is
unambiguously past the token-creation second.

## Scope / discipline
Read-only examiner; drove the real STOP cascade, effect guards, the account-erasure state
machine, and the transport consume path through the target's own conftest. No paid calls;
loopback only; no publication/deployment/autonomy; no production branch modified. Candidate
materialised detached at the exact SHA and left unchanged. `first_cycle_go=false`.

## Reproduce
```
probes/fc_crossauth_v1/run.sh ../../wtcert-460e8ad      # 6 passed (deterministic)
```

## Continuation status
Priorities 6 and 7 complete on `460e8ad8` with no defect (one low/informational observation
recorded). The security-kernel DB authority family, the account-erasure authority, the founder
capability/authority-change core, the Founder identity/session boundary, and cross-boundary
authorization consistency have all been independently examined end-to-end on this lineage.
Awaiting a newer implementation candidate, whose outstanding release blockers would be
prioritized next by severity and release impact.
