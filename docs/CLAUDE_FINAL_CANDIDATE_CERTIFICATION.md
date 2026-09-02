# FINAL COMPOSED CERTIFICATION TARGET

**Cursor has stopped.** Do not wait for more builder work on this tip.

Do not certify #237, #240 or #243 separately anymore.

There is now ONE composed pre-start candidate.

**Exact SHA:** `beae39c78a07e8d78b0a2df3036c872697bf3aab`  
**Branch:** `cursor/mainai-pre-start-reconciliation`  
**PR:** https://github.com/d1n095/LifeAI/pull/244

This candidate explicitly reconciles the overlapping safety PRs.

If HEAD moves past this SHA, record the new SHA and re-run only changed surfaces.

---

```text
FINAL COMPOSED CERTIFICATION TARGET

Do not certify #237, #240 or #243 separately anymore.

There is now ONE composed pre-start candidate.

Exact SHA:

beae39c78a07e8d78b0a2df3036c872697bf3aab

Branch:
cursor/mainai-pre-start-reconciliation

PR:
#244

This candidate explicitly reconciles the overlapping safety PRs.

==================================================
CANONICAL ARCHITECTURE
==================================================

Authority source of truth:

workforce_authority_epoch

Grant:
FOR SHARE

Stop:
FOR UPDATE

There must be NO second competing stop-authority source.

Migration chain:

... -> 0068 -> 0069_workforce_authority_epoch

Single head expected.

Readiness:

#237 receipts.py semantics
+
#240 activation / department evidence fixes.

Memory→work:

#238 TOCTOU/idempotency semantics.

Boot:

never auto-clears stop.

==================================================
YOUR JOB
==================================================

Attack the COMPOSED system.

Do not simply repeat isolated PR reviews.

==================================================
P0 — AUTHORITY COMPOSITION
==================================================

Re-run:

grant-first vs stop
stop-first vs grant
same-millisecond race
many concurrent grants
owner stop
global stop
separate-process restart
grant after committed stop.

Required:

STOP COMMIT
→ ZERO reusable live authority.

Use prove_no_reusable_live_authority().

Also verify there is no second stop truth path left.

==================================================
P0 — CLEAR VS GRANT
==================================================

Run the empirical tests that were previously missing.

Cases:

clear waits while grant starts
grant waits while clear starts
stale clear after newer stop
owner clear while global stop active
global clear while owner stop active.

Required:

clear may only restore authority for the exact scope and epoch intended.

No stale resurrection.

==================================================
P0 — READINESS
==================================================

Test composed readiness:

wrong migration → BLOCKED
multiple heads → BLOCKED
migration error → BLOCKED/UNKNOWN
correct head → may proceed

blocker lists accumulate
IMPORTABLE != HEALTHY

No caller boolean alone may unlock readiness.

==================================================
P0 — SAFE COMPOSED RUN
==================================================

Run with:

no stop
owner stop
global stop
restart after stop
clear then run.

Required:

no TypeError
no auto-clear
no bypass.

==================================================
P0 — EVIDENCE SEMANTICS
==================================================

Re-run the remaining 6/8 + known missing cases.

Especially:

same owner but wrong subject
similar capability but different skill
failed evidence
rejected evidence
superseded evidence
new failure after old success.

EVIDENCE EXISTS != EVIDENCE SUPPORTS EXACT CLAIM.

==================================================
P0 — HIGH-RISK VERIFICATION GATE
==================================================

Re-test workforce/verification.py.

HIGH-RISK task must not pass with:

missing verification
failed verification
wrong-task verification
wrong-owner verification
stale verification
unknown status.

Fail closed.

==================================================
P1 — MEMORY→WORK / CONCURRENCY
==================================================

Re-run:

self replay
SAME-collapse
two-connection race
transaction remains usable after loser path
canonical candidate identity preserved.

==================================================
P1 — CONTINUITY
==================================================

Re-run on THIS composed SHA:

existing-state boot
fresh-process A→B→C
10-process continuity if practical
backup→mutate→restore
provider-zero ledger.

Previous results are useful but do not automatically certify composition.

==================================================
P1 — #218 SECURITY SEMANTICS
==================================================

Verify current composed code still preserves:

consequential request
→ always surfaced for current confirmation

even after learned phrasing/history/restarts.

==================================================
MIGRATIONS
==================================================

Verify:

single head 0069
upgrade
downgrade
upgrade again
no hidden parallel migration lineage.

==================================================
FINAL VERDICT
==================================================

Return:

TARGET SHA TESTED

AUTHORITY:
VERIFIED/PARTIAL/REJECTED

CLEAR VS GRANT:
VERIFIED/PARTIAL/REJECTED

READINESS:
VERIFIED/PARTIAL/REJECTED

SAFE COMPOSED RUN:
VERIFIED/PARTIAL/REJECTED

EVIDENCE SEMANTICS:
VERIFIED/PARTIAL/REJECTED

HIGH-RISK VERIFICATION:
VERIFIED/PARTIAL/REJECTED

MEMORY→WORK:
VERIFIED/PARTIAL/REJECTED

CONTINUITY:
VERIFIED/PARTIAL/REJECTED

MIGRATIONS:
VERIFIED/PARTIAL/REJECTED

#218 CONSEQUENTAL CONFIRMATION:
VERIFIED/PARTIAL/REJECTED

NEW BUGS

OPEN P0
OPEN P1

FINAL SAFE-INTERNAL CERTIFICATION:
YES / NO

MERGE RECOMMENDATION:
MERGE #244 / DO NOT MERGE

If YES:
state exact verified SHA.

Do not merge it yourself.
```

Also see: `docs/MAINAI_PRE_START_RECONCILIATION.md`
