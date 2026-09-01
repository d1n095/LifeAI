# Claude independent exam — exact #237 head

**DO NOT MERGE.** Cursor continues P1 in parallel; this doc is Claude’s attack brief.

**Exact target SHA:** `5b15f02a581fcb12ddd4c37f569175fcfb44a5d9`  
**PR:** https://github.com/d1n095/LifeAI/pull/237

If HEAD moves past this SHA, record the new SHA and re-run only changed surfaces (section F).

---

```text
NEW EXACT CERTIFICATION TARGET

Cursor has continued the pre-start certification campaign.

DO NOT review an older #237 head.

Exact current target:

PR #237
HEAD:
5b15f02a581fcb12ddd4c37f569175fcfb44a5d9

DO NOT MERGE.

==================================================
WHAT CURSOR CLAIMS IS FIXED ON THIS HEAD
==================================================

P0:

1. KILL SWITCH
- owner stop separated from global emergency stop
- durable stop state
- boot cannot auto-clear
- fabricated founder ack rejected
- explicit clear request id
- restart should preserve stop semantics

2. STARTUP READINESS
- IMPORTABLE != HEALTHY
- receipt-oriented readiness
- migration actually checked
- blockers accumulate rather than overwrite
- claude_reviews_satisfied=True alone cannot unlock provider tier

3. EVIDENCE TRUTH
- shared evidence_supports_claim validator
- failed evidence rejected
- unrelated evidence rejected
- school specialization no longer marks verified_available merely from school status

4. MEMORY→WORK REPLAY
- canonical candidate identity retained
- replay distinguished from created_now
- idempotent record_work_candidate park-path gap patched in latest commit

==================================================
YOUR JOB — INDEPENDENT EXAM
==================================================

Do not inspect only tests.

Re-run the original reproductions and invent new variants.

Return only empirical verdicts:

VERIFIED
PARTIAL
REJECTED
UNKNOWN.

==================================================
A. KILL-SWITCH ATTACK
==================================================

Use real DB sessions and at least two owners.

Prove:

owner A stop → A blocked
owner A stop → B unaffected

owner B stop → B blocked
owner B stop → A unaffected

global emergency → both blocked

clear A → does not clear B/global

clear global → does not clear owner A/B

boot while owner stop active:
MUST NOT CLEAR

boot while global stop active:
MUST NOT CLEAR

process restart:
state survives according to contract

MainAI cannot generate valid founder/operator acknowledgement for herself.

Attack clear_request_id:

same request replay
older clear after newer stop
different owner
wrong scope
empty/weak ack
forged-looking ack string.

Check whether ack is merely a string convention or actually tied to authority/authentication.

If the implementation only checks prefixes such as:

founder_ack:
operator_ack:

then determine whether this is actually security or merely syntax.

That distinction matters.

==================================================
B. READINESS ATTACK
==================================================

Attack exact current readiness implementation.

Scenarios:

module import succeeds but runtime dependency broken
wrong migration head
multiple migration heads
migration command failure
stale receipt from older SHA
receipt for different branch
old success followed by new failure
missing receipt
invalid receipt
multiple simultaneous blockers
provider gates unknown
claude attestation True but no durable evidence.

Required:

READY means CURRENT EVIDENCE for CURRENT SHA.

Not:
old evidence
string assertion
caller boolean
import success.

Check whether receipts themselves have provenance/integrity or can be fabricated by arbitrary internal caller.

==================================================
C. EVIDENCE_SUPPORTS_CLAIM ATTACK
==================================================

Use the shared function directly and through real callers.

Matrix:

verified success for same capability → accept
failed test → reject
rejected verification → reject
unrelated capability → reject
wrong owner → reject
wrong subject → reject
superseded evidence → reject
stale evidence → policy-correct
older success + newer failure → NOT currently verified
older failure + newer success → current state according to explicit policy
mixed contradictory evidence → no naive existential proof.

Then test integration through:

capability_reality
self-model
prediction learning
school routing.

Main question:

did Cursor fix the BUG CLASS
or only the known examples?

==================================================
D. MEMORY→WORK REPLAY ATTACK
==================================================

Exact original operation:

same note
same args
park enabled.

Run repeatedly:

call 1
call 2
call 3

Then:

commit
new Session
replay

Then:

fresh process if practical
replay.

Required:

one canonical candidate only.

Result semantics must distinguish:

created_now
canonical_existing
replayed.

Canonical candidate ID must remain recoverable.

No duplicate notifications/work due to result ambiguity.

Then attack equivalent wording/SAME-collapse path.

Also real concurrency:

two sessions race at the exact boundary.

Verify:

no duplicates
no raw IntegrityError
no poisoned transaction
canonical result recoverable by both callers.

==================================================
E. CHECK #237 FOR CERTIFICATION-SPECIFIC REGRESSIONS
==================================================

Search exact diff for:

*_for_tests used in runtime
reset_* used in runtime
test-only helpers
module globals representing durable truth
fake/synthetic founder authority
caller-controlled booleans that become trust
receipt generation immediately followed by self-consumption without independence
“verified” statuses created by the subsystem being verified.

==================================================
F. NEW HEAD WATCHER
==================================================

Cursor is continuing P1.

Keep watching #237.

If HEAD moves from 5b15f02:

record new SHA.

Do not automatically discard completed proofs if untouched code remains identical, but re-run anything whose code/dependencies changed.

==================================================
P1 WHEN CURSOR LANDS IT
==================================================

Independently attack:

EXISTING-STATE BOOT
PROVIDER LEDGER CROSS-CHECK
BACKUP/RESTORE.

Existing-state boot must include a rich history, not only a fresh DB.

Provider proof must be independent of MainAI's own provider_invoked boolean.

Backup/restore must demonstrate:

backup made
state changed
restore performed
canonical memory/work/authority state recovered.

==================================================
IMPORTANT — FRESH PROCESS
==================================================

Still require the previously identified fresh-process proof.

Same-process new SQLAlchemy session is not enough.

At least:

Process A writes state and exits.
Process B reconstructs from DB.
Process C reconstructs again after more work.

No inherited Python globals.

==================================================
FINAL RESPONSE
==================================================

After a substantial batch report:

TARGET SHA ACTUALLY TESTED

KILL SWITCH:
VERIFIED/PARTIAL/REJECTED
new bugs

READINESS:
VERIFIED/PARTIAL/REJECTED
new bugs

EVIDENCE SEMANTICS:
VERIFIED/PARTIAL/REJECTED
new bugs

MEMORY→WORK REPLAY:
VERIFIED/PARTIAL/REJECTED
concurrency result

FRESH PROCESS:
status

#237 REGRESSIONS:
findings

P1:
status

NEW HEAD:
if changed

OPEN P0
OPEN P1

FINAL START CERTIFICATION:
YES or NO

Do not merge anything.
Do not certify your own fixes.
```
