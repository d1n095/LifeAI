# MainAI — Memory Truth Invariant & Inspectable Memory Contract

Founder requirement, verbatim (2026-08-30): **SAID != STORED != PLANNED != IMPLEMENTED !=
VERIFIED.** MainAI ("hon") must never say "I saved that" / "I added that" / "I included that" /
"I've planned it" / "I've built it" unless the corresponding durable system state actually
exists. When the founder gives an addition, MainAI should immediately persist the appropriate
durable representation where policy permits, then VERIFY persistence before claiming success.
The founder also requires the ability to manually inspect MainAI's memory — view, search,
correct, add, remove, supersede, see history.

Companion to `docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md` (the producer of most memory
this contract governs) and `docs/MAINAI_LONG_HORIZON_PLANNING.md` (the consumer of the
IMPLEMENTED/VERIFIED distinction for planning purposes). Unlike that document, **this one is
the founder's explicit "design now, not deferred" item** — see §7.

---

## 1. The invariant is not new — it is scattered and needs one shared name

This session independently found and fixed four real production bugs tonight that were, in
retrospect, all instances of exactly this gap — a system claiming a state that didn't actually
hold:

- `authorize_execution_scope()`'s own docstring claimed a goal-row lock the code never
  actually took (CLAIMED != IMPLEMENTED, at the level of a single function's own contract).
- `run_driver()` treated a step as simply "not yet handled" when authority had actually
  transitioned mid-run — no clean disposition existed to say "this step's effect did NOT
  happen" (STORED/VERIFIED conflated with "the attempt occurred").
- Two test fixtures asserted a `goal.approval_policy` value that was silently reverted by a
  correct-but-unaccounted-for reload (SAID/attempted != what was actually STORED).
- An unbounded replan loop meant a goal could stay `running` — implying "still making
  progress" — indefinitely, with no durable signal distinguishing "actively working" from
  "stuck" (IMPLEMENTED/PROGRESSING falsely implied by the mere absence of a terminal status).

**The pattern already has partial, real enforcement in this codebase** (verified by direct
inspection, not assumed):

| Distinction | Existing partial enforcement |
|---|---|
| PLANNED vs IMPLEMENTED | `MainAITaskStatus` — `ready`/`running` (planned, not yet acted) vs `completed` (acted). Real, but task-status-only; nothing generalizes it to memory/conversation-sourced claims. |
| IMPLEMENTED vs VERIFIED | `MainAICheckpoint.executor_state["verification"]["passed"]` — a SEPARATE record from task status, written only by a real verification run; `task_type="run_tests"` completing is not itself proof, the checkpoint is. |
| CLAIMED vs DURABLE (goal level) | `record_final_report()` — refuses to set `goal.final_outcome`/`status=completed` until EVERY task is genuinely terminal; a goal cannot claim done-ness prematurely (this session's own new `MAX_AUTO_REPLANS` fix reinforces this: a capped-out goal reaches `failed`, never a silent, ambiguous "still running"). |
| RUN vs EFFECT-HAPPENED | `DriverResult.classification` (just extended tonight with `STALE_AUTHORITY`) — distinguishes a step that produced a real effect from one refused before any effect landed. |

**What does not exist anywhere:** a single, shared vocabulary that spans MEMORY claims
specifically (a founder's "add that", a MainAI-generated "I'll remember X") — the enforcement
above is real but per-subsystem (task execution, goal finalization, driver steps), never
applied to the memory layer `docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md` describes.
This document is that missing shared vocabulary.

---

## 2. The five states, defined precisely

```python
class MemoryTruthState(str, Enum):
    SAID = "said"              # a founder utterance or MainAI-proposed addition exists in
                                # RAW form (a message, a ConversationalInterpretationProposal's
                                # raw_expression) -- nothing durable beyond the raw utterance
                                # itself has been created yet.
    STORED = "stored"           # a durable row now exists representing the interpreted claim
                                # (a FounderMemoryNote, a WorkCandidate, an EngineeringLesson) --
                                # but nothing has been PLANNED from it yet.
    PLANNED = "planned"         # the stored item has produced a concrete plan artifact --
                                # a MainAITask exists that traces back to it (via provenance/
                                # memory_threads), OR a WorkCandidate has been authorized into
                                # a MainAIGoal -- but no effect has been attempted.
    IMPLEMENTED = "implemented" # a real effect landed -- a MainAITask reached `completed`
                                # status via the REAL executor chain (dispatch_ready_task ->
                                # run_task_execution_job -> _finalize_task_outcome), never a
                                # direct status write.
    VERIFIED = "verified"       # the implemented effect has a durable verification record
                                # (MainAICheckpoint.executor_state.verification.passed = true,
                                # OR for non-code memory items, an explicit founder confirmation
                                # event) -- IMPLEMENTED alone is never sufficient to claim
                                # "done" for anything with a defined verification_plan.
```

**Monotonic, never skipped, never silently regressed.** A state transition is only ever
recorded going forward (SAID→STORED→PLANNED→IMPLEMENTED→VERIFIED); a later discovery that an
earlier claim was wrong produces a NEW row with `superseded_by`/`corrects_*`, exactly matching
every existing supersession pattern in this codebase (`founder_memory_notes.supersedes_note_id`,
`project_entities.supersedes_entity_id`) — never a backward mutation of the state field itself.

---

## 3. Data model — one new field, reusing every existing table it applies to

**Deliberately not a new `MemoryRecord` table.** `docs/MEMORY_ARCHITECTURE.md` already
establishes the principle this document follows: read the invariant as a property applied
ACROSS existing tables, not a ninth table competing with them. Concretely, add one column
where it doesn't already exist in equivalent form, reusing whatever's already there where it
does:

| Table | Truth-state signal | New column needed? |
|---|---|---|
| `candidate_learning_signals` | Always `SAID` by construction (staging-only, no `authority`) | No — the table's own existence at this stage already means SAID |
| `founder_memory_notes` | Always `STORED` once a row exists (immutable once created — see §1) | No |
| `conversational_interpretation_proposals` (new, `docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md` §1.2) | `SAID` (unreviewed) → `STORED` (promoted) | No — `status` already carries this |
| `work_candidates` | `STORED` (unreviewed) → `PLANNED` (authorized, `authorized_goal_id` set) | No — `status`/`authorized_goal_id` already carry this |
| `mainai_tasks` | `PLANNED` (ready/running) → `IMPLEMENTED` (completed) | No — `status` already carries this |
| `mainai_checkpoints` | `VERIFIED` signal lives in `executor_state.verification.passed` | No |
| `engineering_lessons` | `STORED` on creation; **no existing "this lesson's own fix was actually applied and verified" signal** | **Yes** — add `verification_status: str` (`unverified`\|`verified_by_regression_test`\|`disputed`), defaulting `unverified`. This is the one genuine gap: a lesson's `regression_test` field can name a test that was never actually confirmed to catch the original bug. |

**One genuinely new, small table** — for memory items that don't already live in a
status-bearing table (a bare "MainAI said it would remember X" claim, before it's even become
a `FounderMemoryNote`):

```python
class MemoryTruthClaim(Base):
    """Durable receipt for every claim MainAI makes about its own memory/work state in
    conversation -- "I've saved that", "I'll add that to the plan", "that's already done".
    Exists specifically so a claim can be checked against reality independent of whatever
    table the underlying work actually lands in."""
    __tablename__ = "memory_truth_claims"
    id: UUID
    owner_id: UUID
    claim_text: Text                    # verbatim -- what MainAI actually said
    claimed_state: str                  # one of MemoryTruthState's values -- what MainAI
                                         # is asserting is now true
    target_kind: str                    # closed vocabulary, SAME registry active_context/
                                         # memory_threads already use (SUPPORTED_TYPES) --
                                         # founder_memory_note | work_candidate | mainai_task |
                                         # engineering_lesson | conversational_interpretation_
                                         # proposal
    target_id: UUID | None              # the actual row this claim is about, once it exists --
                                         # null is legal ONLY if claimed_state == "said"
    verified_at: datetime | None        # when a background check (see §5) last confirmed
                                         # target_id's own status ACTUALLY matches claimed_state
    verified_result: bool | None        # true/false/null(not yet checked)
    created_at: datetime
```

`verified_result = false` is not an error state to hide — it is the exact signal this whole
document exists to surface: MainAI claimed X, reality says otherwise. See §5.

---

## 4. Memory → work integration

Founder requirement: a relevant new memory/decision should trigger evaluation of what existing
work it changes — active goals, planned tasks, dependencies, design docs, tests, known risks —
then update affected plans, create/change subordinate work if justified, mark obsolete work,
preserve history, avoid duplicate tasks, and never let memory mutation silently create broader
authority.

### 4.1 The trigger

A `FounderMemoryNote` or resolved `ConversationalInterpretationProposal` reaching `STORED`
(§2) with `note_type` in `{decision, correction, requirement}` (a widened but still closed
set — `requirement` would be a new valid value, matching the pattern `founder_memory`'s own
`note_type` enum already uses for extension) fires a bounded scan, reusing
`docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md` §2's exact orchestration (`active_context`
relevance expansion + `memory_threads` traversal), scoped this time to find **existing** work
referencing the same entity, not to generate new candidates:

```
new memory item (STORED)
  -> memory_threads: find every thread this item's resolved entity is already a member of
  -> for each thread, current_members() filtered to {mainai_goal, mainai_task, mainai_plan}
  -> for each affected item, classify (deterministic, no AI call needed for the classification
     itself):
       - goal/plan already terminal (completed/failed/cancelled) -> no action, historical
       - goal/plan active, task not yet started -> candidate for plan update
       - task already `completed`/`running` -> candidate for a NEW follow-up WorkCandidate
         (never retroactively mutate a task that's already in flight or done)
```

### 4.2 The actual updates — always through real functions, never a direct write

- **"Update affected plan"** — a NEW call to `planner.create_plan()` for the SAME goal (which
  already handles supersession, stale-task cancellation, and re-promoting dependency-free
  tasks — see `docs/MAINAI_V1_STAGE2_STAGE3_ADVERSARIAL_PREP.md`'s own confirmation that this
  path is real, production-wired). This IS a replan, using the EXACT SAME mechanism
  `docs/MAINAI_LONG_HORIZON_PLANNING.md` §3 already designates as the "reality changed,
  re-plan" trigger — memory-driven and failure-driven replanning are the same underlying
  operation with two different trigger sources, not two mechanisms.
- **"Create/change subordinate work"** — a new `WorkCandidate`, exactly as
  `docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md` §2 already specifies; still requires
  separate authorization before it becomes real work.
- **"Mark obsolete work"** — `dismiss_work_candidate()` for an unauthorized candidate the new
  memory item makes moot, OR (for an already-authorized, in-flight task the memory item
  contradicts) the existing cancellation path (`app.mainai_execution.cancellation` — cooperative,
  never a direct status write) — never delete, never silently supersede without a durable
  reason attached.
- **"Preserve history, avoid duplicate tasks"** — before creating anything, query for an
  existing `WorkCandidate`/`MainAITask` already covering the same resolved entity + intent
  (the SAME duplicate-detection discipline `autonomous_gap.service`'s own "Duplicate Gap"
  handling already implements for gap/repair children — reuse that comparison logic, don't
  reinvent it).

### 4.3 Authority — restated

This flow NEVER calls `authorize_execution_scope()`, `authorize_work_candidate()`, or
`grant_task_approval()` itself. It produces plan/candidate DATA a human or an already-
authorized process must still act on — identical authority posture to
`docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md` §6, restated here because this is the one
place memory mutation could plausibly be mistaken for authority (a new plan version LOOKS like
an action) — it is not; `create_plan()` itself has never granted execution authority, only
`authorize_execution_scope()` and `grant_task_approval()` do, and neither is touched here.

---

## 5. Verification — closing the loop, not just declaring the invariant

A stated invariant with no enforcement is exactly the kind of "I saved that" claim this
document exists to prevent MainAI from making about ITSELF. Two concrete mechanisms:

### 5.1 At claim time — synchronous, cheap, mandatory

Every caller that would otherwise say "I've saved/added/planned/built that" MUST, before
producing that sentence, perform ONE of:
- A fresh `SELECT` (not a cached/in-memory read) confirming `target_id` exists with the
  expected `status` — same "the last fence must own its own freshness" doctrine
  `_require_context()`'s own docstring already states verbatim in `development_operator/
  service.py` ("expire_on_commit=False means a plain select() returns the identity-map
  instance without reloading attributes").
- If the write is in the SAME transaction as the claim (the common case), the mandatory check
  is simpler: the write must have already `db.flush()`ed and the returned row's own attributes
  (not an assumed value) are what the claim text is built from.

This is a calling-convention requirement, not new infrastructure — every service function
already returns the row it wrote; the discipline is "build the claim sentence FROM that
returned row, never from what was ASKED for".

### 5.2 Background verification — for the gap between "claimed" and "eventually true"

Some claims (IMPLEMENTED, VERIFIED) can't be confirmed synchronously — a task claimed `ready`
takes real Worker ticks to actually complete. `MemoryTruthClaim.verified_at`/`verified_result`
exist for exactly this: a bounded background check (same operational shape as
`app.cleanup.py`'s existing scheduled jobs — an advisory-locked periodic scan, not a new
infrastructure pattern), reads `target_id`'s CURRENT real status and sets `verified_result`.

**A `verified_result = false` is itself a durable, inspectable fact**, not something to
silently correct — the founder can see, via §6's inspection contract, every case where MainAI's
own stated claim turned out not to match reality. This is what makes the invariant real rather
than aspirational: a founder can query for VIOLATIONS of it, not just trust it's being followed.

---

## 6. Manually inspectable memory

Founder requirement: view, search, correct, add, remove, supersede, see history — without
exposing secrets unnecessarily, and without making hidden model context the canonical memory.

### 6.1 What's already inspectable today (reuse, don't rebuild)

`app.founder_memory.service`'s `get_founder_memory()`/`list_founder_memory()`/
`list_current_founder_memory()` already exist as read paths, filterable by `note_type`/
`status`/`authority`. Per `docs/LIFE_FOUNDER_MEMORY.md`'s own "Explicitly deferred layers", a
UI surface for this was already identified as needed and deliberately deferred — this section
is that deferred layer, now scoped concretely, not a new decision.

### 6.2 The unified read model — a view, not a new store

```python
class InspectableMemoryItem(BaseModel):
    """Read-only projection, NOT a table -- assembled at query time from whichever real
    table the item actually lives in (founder_memory_notes, conversational_interpretation_
    proposals, work_candidates, engineering_lessons). Canonical memory stays in those tables;
    this is the founder's own consistent lens onto them, matching docs/MEMORY_ARCHITECTURE.md
    §1's own "one storage substrate, one retrieval API" principle applied to this narrower
    domain."""
    id: UUID
    kind: str                    # which real table this came from
    raw_statement: str | None    # founder's own words, where applicable
    normalized_interpretation: str
    related_entities: list[UUID] # via memory_threads, resolved live at query time
    confidence: float | None
    factual_status: str          # active | superseded | disputed | dismissed
    truth_state: MemoryTruthState  # from §2/§3
    plan_references: list[UUID]  # mainai_plan ids, via memory_threads
    task_references: list[UUID]  # mainai_task ids, via memory_threads
    dependencies: list[UUID]
    risks: list[str]             # from contradiction_refs / lesson_conflicts, if any
    provenance: dict
    created_at: datetime
    superseded_by: UUID | None
    corrections: list[UUID]      # every row with corrects_proposal_id / supersedes_note_id
                                  # pointing at this one
    implementation_status: str   # mirrors MainAITaskStatus where a task_reference exists
    verification_status: str     # mirrors the checkpoint verification block where relevant
```

### 6.3 API/UI contract — read paths first, write paths narrow and explicit

```
GET  /founder/memory                    -- list, filterable by kind/status/truth_state/
                                            date range, paginated (reuses list_founder_memory
                                            + equivalent list_ fns for the other 3 kinds,
                                            merged and re-sorted at the API layer, not a new
                                            combined query)
GET  /founder/memory/{id}                -- single item + its full correction/supersession
                                            chain (walk supersedes_note_id/corrects_
                                            proposal_id both directions)
GET  /founder/memory/{id}/history         -- every version in the chain, oldest first --
                                            NEVER edited in place, so this is a pure read of
                                            already-durable rows, not a reconstructed audit log
POST /founder/memory/{id}/correct         -- founder-initiated correction. Internally: for
                                            founder_memory_notes, calls record_founder_memory()
                                            with supersedes_note_id=id (existing function,
                                            unchanged). For conversational_interpretation_
                                            proposals, calls promote_conversational_
                                            interpretation() with confirmed_entity_id
                                            overriding the wrong resolution (docs/
                                            MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md
                                            §1.4). NEVER a raw field update.
POST /founder/memory                      -- founder-initiated ADD. Same rule: routes to the
                                            real record_*() function for the target kind,
                                            authority="founder" always explicit, never
                                            defaulted.
POST /founder/memory/{id}/dispute         -- calls mark_founder_memory_disputed() (existing) /
                                            dismiss_work_candidate() (existing) depending on
                                            kind -- "remove" in the founder's own spec means
                                            "mark disputed/dismissed", never a DELETE; nothing
                                            in this contract introduces a hard-delete path
                                            beyond the existing account-erasure mechanism.
```

**Secrets:** `InspectableMemoryItem` never surfaces `provider_spend` credentials, Vault-
governed content, or raw egress-disclosure payloads — those remain governed exclusively by
their own existing access-control layers (`docs/LIFE_VAULT_V4_V5_V7_V8_DESIGN_MEMOS.md`); this
contract is additive read/correct access to founder-language/intent memory specifically, not a
general data-export surface.

**"Hidden model context is never canonical"** — nothing in this contract treats a live LLM
context window, a chat session's in-memory state, or an un-persisted candidate as a source of
truth for anything this document governs. If it isn't in one of the tables §3/§6.1 names, it
does not exist for the purposes of this invariant — which is precisely why §5.1's "build the
claim from the returned row" discipline matters: it forces every claim to originate from
durable state, never from what the model currently believes.

---

## 7. Why this is required now, not deferred to V1.1/V2

Per the founder's own explicit instruction, this document — unlike most of
`docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md` — is a **core architectural invariant to
be designed now**, regardless of V1/V1.1/V2 sequencing for the broader personal-intent work,
because:

1. Every producer of new memory this session's other new document describes (§1's resolution
   proposals, §2's generated candidates, §5's conversational lessons) needs SOMEWHERE
   truthful to land and a truthful way to report having landed there — building those
   producers before this contract exists would recreate exactly the SAID/STORED confusion the
   founder is asking to prevent, in the very first thing built to prevent it.
2. Tonight's own four real bugs (§1) prove this isn't speculative risk — this exact failure
   mode already happened, repeatedly, in code that predates this document.

**What's genuinely deferrable to V1.1/V2:** the UI surface (§6.3's actual frontend), the
background verification job's specific scheduling (§5.2), and `engineering_lessons.
verification_status`'s full backfill for EXISTING lessons (new lessons get it from day one;
backfilling old ones is a bounded, low-urgency migration). **Not deferrable:** the
`MemoryTruthState` vocabulary itself (§2) and the "build claims from returned rows, never from
requests" calling convention (§5.1) — both are cheap to establish now and expensive to retrofit
once dozens of call sites exist that don't follow them.
