# Claude independent exam — #237 @ ecae232

**DO NOT MERGE.** Cursor continues P1 in parallel.

**Exact target SHA:** `ecae232b78d58afc93211ada427118f2fba682d8`  
**PR:** https://github.com/d1n095/LifeAI/pull/237

If HEAD moves past this SHA, record the new SHA and re-run only affected surfaces.

---

```text
NEW EXACT INDEPENDENT CERTIFICATION TARGET

Cursor has resumed blocker eradication.

Current #237 HEAD:

ecae232b78d58afc93211ada427118f2fba682d8

DO NOT review an older head as final.

Cursor claims the following P0 fixes are now present:

- authority stop/grant serialization
- migration readiness actually gates
- boot-block audit identity fixed
- #236 six merged bugs addressed
- #218 consequential-confirmation fix propagated across #219–#227
- shared evidence semantics fixed on #237
- #235 stale-assumption replan fixed
- #235 same-owner composed-cycle concurrency serialized

Your job is to independently try to DISPROVE each claim.

==================================================
1. AUTHORITY STOP/GRANT RACE — HIGHEST PRIORITY
==================================================

Re-run the original two-connection authority reproducer.

Then expand it.

Cases:

grant begins first
stop begins first
owner stop
global stop
many grants racing one stop
many stops racing grants
different workers/processes
restart after stop
grant attempt after stop commit
clear then grant
newer stop after stale clear request.

Required:

after STOP COMMIT:
ZERO reusable live execution authority.

Check both:

DB rows
prove_no_reusable_live_authority()

Also inspect whether the FOR UPDATE/fence mechanism has any gap if the stop-state row does not yet exist.

Attack first-use races.

==================================================
2. MIGRATION READINESS GATING
==================================================

Prove:

wrong head → BLOCKED
multiple heads → BLOCKED
migration verification error → BLOCKED/UNKNOWN according to contract
correct current head → may proceed.

Check that the migration receipt actually participates in readiness derivation, not just reporting.

==================================================
3. BOOT AUDIT IDENTITY
==================================================

Create owner + global stop combinations with different sequences/reasons.

Boot while blocked.

Verify audit receipt:

scope
sequence
reason
owner/global identity
event/state id

all refer to the SAME exact durable stop record.

No mixed-row audit identity.

==================================================
4. #236 SIX FINDINGS
==================================================

Independently verify each one against current head.

A. safe_composed_run does not auto-clear/bypass stop
B. metrics are owner-scoped
C. verification status handling is case/semantics correct
D. curriculum idempotency stable
E. executive school routing happens once
F. registry no longer contradicts runtime truth

For metrics:
Owner A activity must not alter Owner B snapshot.

==================================================
5. #218 PROPAGATION
==================================================

Check every relevant open branch #219–#227.

Confirm none still contains the pre-fix:

must_surface = consequential AND NOT auto_resolved

or equivalent weakening.

Then run at least one branch-level regression where learned phrasing exists but current consequential request still surfaces.

==================================================
6. #213/#220 EVIDENCE SEMANTICS
==================================================

Attack shared evidence_supports_claim.

Cases:

successful supporting evidence
failed evidence
rejected evidence
unrelated capability
wrong owner
wrong subject
superseded evidence
old success + new failure
old failure + new success
conflicting evidence.

Then check actual callers:

capability_reality
self_model
prediction_learning
school route if relevant.

Main question:

DID CURSOR FIX THE SHARED BUG CLASS?

==================================================
7. #235 REPLAN
==================================================

Resolved parent problem + stale assumption:
must NOT force endless replan.

Open unresolved problem + active contradiction:
may replan according to bound/policy.

Check MAX/bounded behavior.

==================================================
8. #235 SAME-OWNER CONCURRENCY
==================================================

Run two real concurrent executive cycles for the same owner.

Verify:

no deadlock
no uncaught crash
no duplicate/contradictory work
advisory lock scope is exactly correct.

Also run different owners concurrently:
they should not unnecessarily serialize each other.

==================================================
9. WATCH HEAD MOVES
==================================================

Cursor is still fixing P1.

If #237 moves beyond ecae232:

record new SHA.

Preserve completed proof where untouched, but re-run affected surfaces.

==================================================
FINAL VERDICT FOR THIS PASS
==================================================

Report:

TARGET SHA

AUTHORITY RACE
VERIFIED/PARTIAL/REJECTED

MIGRATION GATING
VERIFIED/PARTIAL/REJECTED

BOOT AUDIT
VERIFIED/PARTIAL/REJECTED

#236 six findings
status each

#218 propagation
status

EVIDENCE SEMANTICS
status

#235 replan
status

#235 concurrency
status

NEW BUGS

OPEN P0
OPEN P1

READY_FOR_INDEPENDENT_CERTIFICATION?
YES/NO

Do not merge anything.
```

---

Founder: Cursor continues sibling-races / #229 / #219 until `READY_FOR_INDEPENDENT_CERTIFICATION`; Claude owns disproof of `ecae232` (and later heads for changed surfaces only).
