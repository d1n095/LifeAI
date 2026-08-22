# ADVERSARIAL CROSS-STACK REVIEW — Life Cognition/Founder-Learning Foundation (2026-08-18)

## Scope

An independent, evidence-driven adversarial review of the five stacked PRs built this mission
under "LIFE SELF-MODEL, ADAPTIVE COGNITION & CORPUS READINESS": #94 (capability reality,
migration 0048), #96 (founder/user memory, migration 0049), #98 (adaptive cognition boundary,
no migration), #101 (causal diagnosis interface, migration 0050), #102 (corpus trial harness,
no migration). Requested explicitly by the founder, with the instruction to not merely agree
with prior conclusions (including this same author's own) and to be willing to say "this
approach is weaker than another one" or "my previous conclusion was wrong" where the evidence
supports it. Every finding below was checked against the actual running system (a real local
Postgres, real migrations applied, real RLS roles, real tests run), not against memory of what
was intended.

**Overall verdict:** the foundation is structurally sound where it has been exercised. Owner
isolation, provenance vocabulary reuse, supersession/history preservation, and migration
reversibility all hold up under direct verification, including several checks that had never
actually been run before this review (see Finding 3). The single most important finding is not
a bug in what was built, but a fact about what was NOT yet connected: **none of the three new
foundations (capability reality, founder memory, causal diagnosis) are called from any real,
non-test code path yet** (Finding 1). This does not make any individual PR wrong — it makes
"foundation" the literal, current truth, not a soft way of saying "feature." Every previous
report in this mission described these as foundations; this review makes sure that claim is
precise enough to survive scrutiny, not just true in spirit.

## Method

Direct verification, not just re-reading: created a real local Postgres test database, ran
`alembic upgrade head` from a clean state, then `alembic downgrade 0047` and back `upgrade
head` again to prove the three new migrations (0048-0050, plus the 0051 this review adds) are
genuinely, mechanically reversible, not just plausible-looking `downgrade()` functions that
were never executed. Queried `pg_policies`/`pg_class`/`information_schema.role_table_grants`
directly for the live RLS/privilege state rather than trusting the migration source. Grepped
the entire `app/` tree for real (non-test, non-self) callers of each new module. Ran every
affected test suite, including new ones added to close gaps this review found.

## Findings

### Finding 1 (structural, not a defect) — Zero real callers for all three new foundations

`grep`ing the whole `app/` tree for non-test imports of `app.capability_reality`,
`app.founder_memory`, and `app.diagnosis` (beyond their own module boundaries and each
other's) returns **nothing**. All three foundations are fully isolated: extensively tested,
internally correct, but never invoked by any live execution path. This is not automatically a
defect — the same pattern applies to migration 0038's `intelligence_governance` foundation
when it was first built, and it IS now wired into `app/safe_planner/service.py`,
`app/provider_planning/service.py`, `app/development_supervisor/service.py`, and
`app/agent_coordination/service.py`. "Build the deterministic recording/query layer, wire it
into real call sites later" is this codebase's own established, deliberate methodology, not a
shortcut unique to this mission.

What makes this worth stating plainly rather than leaving implicit: **every one of
`intelligence_governance`'s real wiring points is Cursor-owned scope**, explicitly off-limits
under this mission's own stated boundaries (`safe_planner`, `development_supervisor`,
`development_driver`, `development_operator`, `autonomous_gap`). This is the actual, concrete,
structural reason the three new foundations remain unwired — not an oversight, a genuine
cross-workstream boundary. It means the "exact next blocker before a real mixed-corpus trial"
this mission's own reporting format asks for is, precisely: **coordinated wiring into
Cursor-owned execution paths, or an explicit founder decision to wire through a different,
shared entry point** (see Finding 2 for one such candidate already identified).

Action taken: none to the affected files (all Cursor-owned). Documented here, prominently,
so no future reader mistakes "foundation exists and is tested" for "foundation is informing
Life's real decisions." Every one of this mission's prior PR reports already used "foundation"
language and never claimed operational status, but none of them stated this as starkly as
"zero real callers" — this review closes that precision gap in the record, not in the code.

### Finding 2 — A genuine, unwired bridge point exists, but is more complex than it looks

`app.context.resolver` (predates this session) IS wired into real code — `app/routers/chat.py`
calls `resolve_context()` on every chat turn, live, right now. Its own code comment is
explicit: *"Purely observational: it doesn't yet change retrieval or the system prompt —
surfaced on the response so the founder and a future UI can see it and build on it without
this endpoint's core behavior depending on it."* This is precisely the extension point
`app.founder_memory.service`'s own module docstring already anticipated (`INTENT_EXPLICIT_
MEMORY`/`INTENT_CORRECTION`), and both constants genuinely exist in `resolver.py` with
matching semantics — verified directly, not assumed. `chat.py`'s router is gated end-to-end by
`Depends(require_founder)`, so the acting user is structurally guaranteed to be the founder
role, not a general/admin/member account — the "founder preference accidentally becoming
project fact" risk the founder specifically asked this review to check is NOT present in an
authority sense here; `authority="founder"` would be correct for anything this endpoint
records.

**However**, a naive 1:1 wiring (every `INTENT_CORRECTION` classification → a
`founder_memory_note`) would be a real mistake, not just extra work: `resolver.py`'s own
`_CORRECTION_MARKERS` list includes very short, extremely common Swedish words —
`"nej "` (no), `"fel,"` (wrong,) — by the resolver's own docstring, "a heuristic first pass...
with false-positive/negative trade-offs," never claimed to be precise. Blindly persisting
every classification would flood `founder_memory_notes` with low-signal noise ("nej men det
var kul" — "no but that was fun" — would trigger `INTENT_CORRECTION`), degrading exactly the
signal quality this foundation exists to protect. This is a case of "this approach is weaker
than another one," found before it was built rather than after: a real bridge needs an
explicit design decision this review did not make unilaterally (confidence gating on
`CONFIDENCE_HIGH` only? An explicit founder confirmation step? Recording everything but adding
a `noise_candidate` flag for later gardening?) — a product decision, not a "smallest piece to
prevent lock-in." Not implemented in this pass; recorded here as the single most
concretely-specified next increment, precise enough that whoever picks it up does not have to
re-derive the marker-noise risk from scratch.

### Finding 3 — Real gap: no test proved RLS behaviorally for the three new tables (fixed)

Every existing test for `capability_records`, `founder_memory_notes`, and `diagnosis_records`
used `superuser_db`, which bypasses Row-Level Security unconditionally per Postgres semantics
(`BYPASSRLS`). This means every "owner isolation" claim in this mission's own prior reports was
proven only at the Python query-filter level (does `WHERE owner_id = X` work) — never proven
against what happens if application code forgot that filter, or if a bug set the wrong
`app.current_user_id`. `tests/security/test_rls_isolation.py` (this codebase's own established
behavioral-RLS suite, using the restricted `mainai_app` role through the real `db_session`
fixture) never covered any of the three. This is exactly the kind of "test that merely mirrors
implementation" the founder asked this review to hunt for, and it is real — not a documentation
gap, an actual untested code path.

**Fixed in this PR**: `tests/security/test_rls_isolation_cognition_foundation.py`, 7 new tests
mirroring the established pattern exactly (real restricted-role session, real cross-owner
insert/read attempts, real "no session variable set" case). All 7 pass — RLS itself was never
actually broken, but the review's own standard (verify, don't assume) required proving that
rather than inferring it from the policy SQL text matching a generic structural test.

### Finding 4 — `capability_records` was never wired into the shared linking registry (fixed)

`founder_memory_note` (0049) and `diagnosis_record` (0050) were both added to `app.
active_context.service`'s central object-reference registry and to `memory_threads`' own
`member_kind` vocabulary when they were built. `capability_record` (0048, built first) never
was — confirmed by querying the live `ck_active_context_set_anchor_type`/`ck_active_context_
member_object_type`/`ck_memory_thread_member_kind` CHECK constraints directly against Postgres
and finding `capability_record` absent from all three while the other two are present.
`docs/LIFE_CAPABILITY_REALITY.md` never mentions this as a deliberate deferral — it is simply
silent, which reads as an oversight rather than a decision. Practical consequence: a capability
gap discovered while working a specific goal/task could not be linked (via `active_context`/
`memory_threads`) to the task that discovered it, unlike a founder memory note or a diagnosis.

**Fixed in this PR**: migration 0051 (additive only — widens the same three CHECK constraints
by one more value each, no new table/column), `app/active_context/service.py` extended with
the same `capability_record` mapping and one edge (`last_verification_evidence_id` →
`intelligence_evidence`, relation `verified_by`) the other two foundations already use. 4 new
tests prove the wiring actually works (anchoring a context set, linking to a task via a memory
thread, and the evidence edge), not just that the constraint accepts the string.

### Finding 5 — A real naming collision reveals an already-existing, unconnected concept

`app.safe_planner.service.record_capability_gap()` (Cursor-owned, predates this session)
and `app.capability_reality.service.record_capability_gap()` (this mission, PR #94) share an
identical function name for genuinely different purposes: the former writes an ephemeral
`MainAITaskCheckpoint` for ONE planning attempt (`"is a provider capability available for
THIS specific task right now"`); the latter writes a durable, owner-scoped, cross-task FACT
row (`"as of now, can Life do X at all"`). No import collision exists (different modules), but
the concepts ARE related and currently disconnected: every time `safe_planner.
plan_founder_request()` catches `OperatorCapabilityMissing` and calls its own
`record_capability_gap()`, that real, live capability-gap discovery is recorded ONLY as a
task-scoped checkpoint — it never updates `capability_records`, Life's own durable
self-model. This is the clearest concrete evidence for Finding 1: the most natural, realistic
SOURCE of a capability-gap observation already exists in the live system, and is not
connected to the foundation built to hold it.

Not fixed — `app/safe_planner/service.py` is Cursor-owned, off-limits under every mission
boundary stated this session. Documented here as a specific, named coordination item: when
`capability_reality` is eventually wired into live execution, `safe_planner.
plan_founder_request()`'s `OperatorCapabilityMissing` handler is the concrete, already-proven
trigger point to wire it FROM — not a hypothetical one.

### Finding 6 (confirmed correct, not a defect) — Authority/basis have no leakage vector at the schema level

Checked directly against the live database, not the migration source: `authority varchar(40)
NOT NULL DEFAULT 'unknown'` (and the equivalent for `basis`) is enforced at the raw DDL level,
not merely as a SQLAlchemy ORM-side Python default — a raw SQL INSERT that omitted the column
entirely would still get `'unknown'`, never silently get `NULL` or an uncontrolled value, and
the CHECK constraint rejects any value outside the closed vocabulary regardless of insertion
path. `record_diagnosis()`/`record_founder_memory()`'s idempotency-mismatch check compares
ALL fields including `authority`/`basis` (not just content), so replaying an idempotency key
with a silently-upgraded authority is rejected, not silently accepted. This holds for both of
the two append-only foundations. `capability_records`' live, mutable design (by contrast,
explicitly documented as a "current state projection," not append-only) does allow a later
call to overwrite `authority` on the same row without a mismatch check — this is the SAME
"caller responsibility, never inferred" doctrine every module in this mission shares, not a
new or distinct risk; the full history remains reconstructable from the append-only
`capability_observation_events` table regardless.

### Finding 7 (confirmed correct) — Migration ordering, rollback, and constraint sequencing all hold

Full `alembic upgrade head` from a clean database, then `alembic downgrade 0047` (reversing
0048, 0049, 0050 in sequence), then `alembic upgrade head` again — all three completed cleanly
with no manual intervention, no orphaned constraints, no drift in the final schema versus the
first upgrade. The three sequential widenings of `ck_active_context_set_anchor_type`/`ck_
active_context_member_object_type`/`ck_memory_thread_member_kind` (0049 adds `founder_memory_
note`, 0050 adds `diagnosis_record` on top of 0049's list, 0051 in this PR adds `capability_
record` on top of both) were each verified against the CURRENT full vocabulary via direct
Postgres inspection before writing the next migration, not from a stale remembered list — the
live constraint after all three contains exactly the expected 33/32/31-value lists, in the
right order, with no duplicates or omissions.

### Finding 8 (confirmed correct) — Docs do not overclaim

`docs/LIFE_CAPABILITY_REALITY.md`, `docs/LIFE_FOUNDER_MEMORY.md`, `docs/LIFE_CAUSAL_DIAGNOSIS_
INTERFACE.md`, and `docs/LIFE_CORPUS_TRIAL_HARNESS.md` were all re-read specifically hunting
for language that could be misread as "this is live/operational" rather than "this is a tested,
inert foundation." None found — every doc consistently uses "foundation," explicitly lists
what remains deferred, and (in founder_memory's case) explicitly states the module itself
never decides WHEN to write, leaving that to a not-yet-built caller. This finding is included
specifically because the review's own instructions warned against being contrarian for its own
sake — not every check turns up a problem, and it would misrepresent the review's own evidence
to only report findings that read as criticism.

## Not implemented in this pass (grounded future gaps, not vague aspirations)

- **The `chat.py` → `founder_memory` bridge** (Finding 2) — concretely specified above,
  including the exact noise-risk that must be resolved by design before it is safe to build,
  not just "wire it in later."
- **The `safe_planner.record_capability_gap()` → `capability_reality` bridge** (Finding 5) —
  concretely specified above, blocked on Cursor-scope coordination, not on missing design.
- **Stale/superseded knowledge gardening** across all three foundations (a `disputed`
  founder_memory_note or a `ruled_out` diagnosis is never revisited or cleaned up
  automatically) — already explicitly deferred in each foundation's own doc; re-confirmed
  still absent and still correctly labeled as deferred, not silently missing.
- **`app.problem_learning` fixture coverage** in the corpus trial harness — already flagged as
  deferred in `docs/LIFE_CORPUS_TRIAL_HARNESS.md`; unaffected by this review.
- Extending the same behavioral-RLS-test discipline (Finding 3) to any FUTURE owner-scoped
  table this mission adds — should become a standing checklist item, not a one-time fix.

## What this PR changes

No new table. Migration 0051 (additive CHECK-constraint widening only, same mechanism as
0049/0050). `app.active_context.service` gains one new linkable type (`capability_record`).
`app.founder_memory.service` gains `list_current_founder_memory()`, the same safe-by-default
"give me what's currently true" role `list_current_diagnoses()` already plays for its sibling
foundation. 11 new tests (4 proving the active_context wiring works end to end, 7 proving RLS
behaviorally for all three foundations). This document.

## Full stack composition, confirmed

`alembic heads` reports a single head (`0051`) across the complete stack. The full local
`tests/backend/mainai/` + `tests/security/` regression (all 5 prior increments plus this
review's fixes) was run together, in one process, against one migrated database — not five
separate, never-combined test runs. See this PR's own test-plan section for the exact count.
