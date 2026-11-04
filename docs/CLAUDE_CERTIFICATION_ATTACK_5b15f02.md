# Claude independent exam — P0 @ 5b15f02 then P1 @ 81f9b07

**DO NOT MERGE #237.** Cursor’s pre-start build is done; Claude is the examiner.

| Track | Exact SHA |
|---|---|
| **P0 attack first** | `5b15f02a581fcb12ddd4c37f569175fcfb44a5d9` |
| **Then P1 / composed head** | `81f9b0742a85be3c46014a38c3004b8bcc36905c` |
| **PR** | https://github.com/d1n095/LifeAI/pull/237 |

Do **not** start over. Finish P0 verdict on `5b15f02`, then inspect `5b15f02..81f9b07` and re-run only affected surfaces + composition.

Original P0 brief (kill switch / readiness / evidence / memory→work) remains binding for `5b15f02` — see prior sections in git history / PR comments if needed; the update below adds P1 and final certification gates.

---

```text
IMPORTANT UPDATE — CURSOR P1 HAS LANDED, #237 STILL MUST NOT MERGE

Cursor has completed the parallel P1 certification work.

P0 exact target you were given remains:

5b15f02a581fcb12ddd4c37f569175fcfb44a5d9

for the original independent P0 attack.

Cursor has now advanced #237 to:

81f9b0742a85be3c46014a38c3004b8bcc36905c

P1 additions on top:

- existing-state rich-history boot
- CURRENT/SUPERSEDED inspection
- independent provider ledger cross-check in boot receipt
- backup → mutate → restore proof
- true fresh-process A→B→C continuity proof
- dashboard/observability now reads durable query_stop_status
- boot refuses to migrate PostgreSQL maintenance DB

DO NOT START OVER FROM SCRATCH.

Complete the P0 verdict on 5b15f02 first.

Then inspect the diff:

5b15f02..81f9b07

and re-run only affected/integrated surfaces plus necessary composition tests.

==================================================
P1 — EXISTING-STATE BOOT INDEPENDENT ATTACK
==================================================

Do not accept seeded richness merely because rows exist.

Verify that the boot actually reconstructs correct semantics.

Seed/inspect at minimum:

old memory
current correction
superseded decision
disputed claim
verified capability
failed/regressed capability
open work
completed work
continuity checkpoint
decision debt.

Required after boot:

CURRENT does not include superseded truth.
SUPERSEDED remains inspectable.
failed/regressed capability does not appear verified.
completed work is not revived.
open work remains recoverable.
authority is not reconstructed from memory/history.
provider remains disabled.

Then repeat through fresh processes B and C if the P1 path supports it.

==================================================
P1 — PROVIDER LEDGER CROSS-CHECK
==================================================

Cursor claims boot now compares an independent provider ledger.

Attack the independence claim.

Check exactly what it counts.

It must not simply read the same boolean/receipt that MainAI writes.

Before boot capture:

provider attempts
spend reservations
workforce/provider receipts
other broker-level provider traces available.

After boot compare.

Required:

MainAI says 0
AND
independent ledger unchanged.

Attack:

missing table
schema mismatch
query error
SAVEPOINT recovery
ledger unavailable
partial ledger availability.

A failure to observe provider state must not silently become:

UNCHANGED = TRUE.

UNKNOWN != ZERO.

This is important.

==================================================
P1 — BACKUP/RESTORE ATTACK
==================================================

Verify Cursor's proof is real:

backup created
canonical pre-backup state recorded
state intentionally mutated
mutation confirmed
restore executed
new DB connection/session opened
original canonical state recovered.

Check multiple truth classes:

memory
work
capability
stop state
continuity if included.

Do not accept a restore proof that only checks one arbitrary row.

Also verify:

proof is restricted to disposable/local database
cannot point accidentally at prod/maintenance DB without explicit safeguards.

==================================================
P1 — FRESH-PROCESS A→B→C
==================================================

This is important because you previously identified same-process resume as insufficient.

Prove these are genuinely separate OS processes / Python interpreters.

Process A:
writes/checkpoints, exits.

Process B:
starts after A exits,
no shared Python globals,
new engine,
new session,
reconstructs.

B writes another state transition and exits.

Process C:
new process,
reconstructs both durable phases.

Check:

active/current goal
completed vs remaining
corrections/supersession
capability truth
decision debt
stop state
continuity.

PROCESS MEMORY != DURABLE STATE.

==================================================
P1 — MAINTENANCE DB GUARD
==================================================

Cursor's fresh-process run discovered that an initial script could migrate the PostgreSQL `postgres` maintenance DB.

He added a guard afterward.

Attack that guard.

Inputs:

postgres
postgres/
empty DB path
URL with query parameters
APP_DATABASE_URL mismatch
DATABASE_URL=maintenance while LIFEAI_BOOT_DATABASE_NAME is safe
encoded/surprising URL forms if relevant.

Required:

safe refusal/reroute according to explicit contract.

No silent migration of maintenance DB.

Check whether the same unsafe pattern exists in:

backup/restore
other startup scripts
migration helpers.

Generalize the lesson if so.

==================================================
REGRESSION — DURABLE STOP OBSERVABILITY
==================================================

Cursor changed dashboard/observability away from process cache.

Verify:

Owner A status shown only for A.
Owner B unaffected.
Global stop shown globally.
restart preserves displayed stop state.
dashboard cannot disagree with workforce enforcement.

DISPLAYED SAFETY STATE == ENFORCED SAFETY STATE.

==================================================
FULL FINAL #237 ATTACK
==================================================

After the P0 and P1 independent passes:

review current HEAD 81f9b07 as the composed candidate.

Especially look for bugs caused by interaction between P0/P1 fixes.

Examples:

receipt-backed readiness depends on provider ledger which errors
existing-state seed affects evidence truth
backup captures active unsafe transient state
fresh restart reconstructs stale kill switch
dashboard says stopped while executor runs
boot creates new owner instead of resuming intended owner
CURRENT/SUPERSEDED query itself has oldest-row/current-truth bug.

==================================================
CERTIFICATION REQUIREMENT
==================================================

Final startup certification must require:

P0 independent verification
P1 independent verification
exact-head CI green
then merge
then post-merge exact-tip proof.

Do not certify branch head as final deployed truth.

==================================================
FINAL REPORT
==================================================

Return:

P0 TARGET TESTED:
5b15f02 yes/no

P0 KILL SWITCH
P0 READINESS
P0 EVIDENCE
P0 MEMORY→WORK

P1 TARGET TESTED:
81f9b07 yes/no

EXISTING-STATE BOOT
PROVIDER LEDGER
BACKUP/RESTORE
FRESH PROCESS A→B→C
MAINTENANCE DB GUARD
DURABLE STOP OBSERVABILITY

NEW BUGS
PARTIAL FIXES
VERIFIED FIXES

CURRENT #237 HEAD
CI STATUS

OPEN P0
OPEN P1

SAFE-INTERNAL CERTIFICATION:
YES / NO

MERGE RECOMMENDATION:
MERGE / DO NOT MERGE

If YES:
state the exact SHA that was independently verified.

Do not merge it yourself.
```

---

## Founder gate (after Claude)

Only if Claude returns SAFE-INTERNAL CERTIFICATION = YES + MERGE:

1. Exact-head CI green on verified SHA  
2. Merge #237  
3. **One** post-merge exact-tip boot + fresh-process check  
4. Then claim `READY_FOR_SAFE_INTERNAL_CERTIFIED`

Cursor does not self-certify and does not merge.
