# MainAI V2 — Orb Operating Shell (Stage V2-I)

**Status:** design-only, isolated lane. Does not modify PR #245 (SHA `818dfb732da47901eb5ae06ffdd9c829fe00c4c5`)
or `claude/final-blocker-closeout`. Branched from `claude/det-kommer-mer-879lcm`.
Covers the founder's brief Parts A (operating shell), P (UI model), Q (invisible routing),
extending V2-A §1's vocabulary (`MAINAI_ORB`/`WORKSPACE`/`INTENT_OBJECT`/`TAKEOVER_STATE`/
`CONTEXT_STATE`/`VISIBLE_SURFACE`/`BACKGROUND_AGENT_TASK`).

## 1. Intent Object lifecycle

### 1.1 State machine

```
CREATED → CLARIFYING → ACTIVE → (BLOCKED ⇄ ACTIVE) → RESOLVED
                                                     → SUPERSEDED
                                                     → ABANDONED
```

- **CREATED**: an utterance or workspace action produced enough signal to open an intent, but
  MainAI does not yet have enough to act (e.g. "fix this" with no clear `CONTEXT_STATE`
  target). Durable from the first moment — matches this session's own "SAID must become
  STORED before anything else" discipline; an intent is never held only in a conversation
  buffer.
- **CLARIFYING**: MainAI has asked a follow-up and is waiting on the owner. An intent may sit
  here indefinitely; it is not abandoned just because the owner didn't answer immediately.
- **ACTIVE**: enough is known to work on it. May spawn `BACKGROUND_AGENT_TASK`s.
- **BLOCKED**: a real, named blocker exists (`blockers` field, plural — matches the founder's
  own schema). Returns to ACTIVE when the blocker clears, never silently.
- **RESOLVED**: `completion_definition` was actually satisfied — checked the same way this
  session's evidence-semantics work insists on: RESOLVED requires evidence the completion
  definition was met, not just an agent's claim that it was (`EVIDENCE EXISTS != EVIDENCE
  SUPPORTS CLAIM`, reused verbatim from V2-A §4).
- **SUPERSEDED**: a later intent explicitly replaces this one (the owner changed their mind,
  or a duplicate was detected) — the record is kept, never deleted, exactly like this
  session's own `FounderMemoryNote`/`EngineeringLesson` supersession pattern.
- **ABANDONED**: explicit owner action or a durable staleness policy (not silent timeout
  without a trace) closes it without resolution.

### 1.2 Durability

Every transition is an append-only event on the intent's own history (`history` field, per
V2-A §1's schema) — the exact same shape as `app.workforce.failure`'s checkpoint chain
(`record_checkpoint()` always reads the prior via `latest_checkpoint()`, never mutates in
place) and V2-H's Fast Restore durability primitive. A fresh session reconstructing an
owner's intents reads this event history, not a single mutable row — this is the same pattern
this session proved (10 genuine subprocess restarts, zero fidelity decay) generalized to a
new domain, not a new mechanism.

### 1.3 `CONTEXT_STATE` linkage

An intent's `linked_files`/workspace references are resolved through `CONTEXT_STATE` at
creation time and re-resolved (not assumed stale-safe) whenever the intent moves out of
CLARIFYING — reuses the existing personal-language entity-resolution work
(`docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md`), extended so its resolution targets
include workspace objects (open windows/documents) in addition to conversation-mentioned
entities.

## 2. `TAKEOVER_STATE` enforcement

**Requirement restated:** any raw keyboard/mouse/voice input from the owner sets
`TAKEOVER_STATE=OWNER` immediately, without waiting for MainAI's own turn-taking logic.

**Design implication:** this cannot be an application-level flag MainAI polls on its own
schedule — by the time MainAI's own event loop checks a flag, an in-flight automated action
(a click, a keystroke MainAI was about to send) may already have raced the owner's real input.
The correct architecture is an **OS-level input-hook priority**: a low-level input observer
that:

1. Runs at a layer *below* MainAI's own automation dispatch (an accessibility/input-injection
   API observer, not a message MainAI's own process chooses to read).
2. On any genuine owner-originated input event, synchronously sets a shared, MainAI-external
   `TAKEOVER_STATE` primitive (e.g. an OS-level shared memory flag or equivalent low-latency
   IPC) **before** that input is even delivered to whatever application would normally receive
   it.
3. MainAI's automation dispatch layer checks this primitive **before every single simulated
   input action**, not once at the start of a task — a long-running automated sequence must
   re-check before each step, so a mid-sequence takeover halts it at the very next action, not
   at the end of the current one.

**Platform honesty:** this is not equally easy everywhere. macOS (Accessibility API +
CGEventTap), Windows (low-level hooks, UI Automation), and Linux (varies by display server —
X11 makes this tractable, Wayland deliberately restricts exactly this kind of global input
observation for its own security reasons) have genuinely different levels of support. The
architecture must not silently assume parity — a platform where true OS-level pre-emption
isn't available needs its own explicitly-weaker fallback (e.g. polling at the tightest
interval the platform allows) documented as exactly that, not papered over. This is a direct
instance of `SAID != VERIFIED`: claiming instantaneous takeover on a platform where it's
actually best-effort-fast would be exactly the kind of overclaim this whole project's
discipline exists to prevent.

**Resuming after takeover:** MainAI does not resume automated control on its own initiative
after a takeover — that would violate `USER INPUT > AGENT CONTROL` in spirit even if the letter
is satisfied. Resuming requires an explicit new instruction, which re-creates or re-activates
the relevant `INTENT_OBJECT` rather than silently continuing the interrupted automated
sequence.

## 3. Worked utterance examples

| Utterance | Primitives touched | Response construction |
|---|---|---|
| **"What is this?"** | `CONTEXT_STATE` (resolve current focus) | Resolve the currently-focused `WORKSPACE` object via `CONTEXT_STATE`; if ambiguous (nothing focused, or multiple candidates), ask rather than guess — never silently pick the "most likely" one, matching the contradiction-doctrine of "surface ambiguity, don't auto-resolve" |
| **"compare these"** | `CONTEXT_STATE` (resolve selection set) | Requires a genuine multi-item selection in `WORKSPACE`; if only one item is selected, respond that a comparison needs two, don't invent a second target |
| **"continue"** | `INTENT_OBJECT` (most recent ACTIVE or BLOCKED, per owner) | Look up the owner's most recently touched non-terminal intent; if more than one is plausibly "the" thing to continue, ask which — same anti-guessing rule as above |
| **"put this next to that"** | `CONTEXT_STATE` (resolve both "this" and "that"), `WORKSPACE` manipulation | Two independent `CONTEXT_STATE` resolutions (current focus for "this", most-recently-referenced-but-not-current for "that"); then a `WORKSPACE` move action — no new `INTENT_OBJECT` needed for a purely mechanical action with a completion definition that's immediately checkable (did the window actually move) |
| **"show me why"** | `VISIBLE_SURFACE` (evidence/incident reveal) | Opens a `VISIBLE_SURFACE` showing the evidence chain behind MainAI's immediately-prior claim — reuses whatever durable evidence/provenance record backs that claim (e.g. an `EvidenceSupportResult`-shaped object from `app.evidence_claim`, or the equivalent for a workspace action); if no durable evidence exists behind the claim, MainAI must say so rather than construct an after-the-fact justification |
| **"I'll take over"** | `TAKEOVER_STATE` | Sets `TAKEOVER_STATE=OWNER` immediately via the mechanism in §2 — this utterance is actually redundant with real input already having done so, but MainAI still acknowledges it explicitly and halts any in-flight `BACKGROUND_AGENT_TASK` that was manipulating the visible `WORKSPACE` (background tasks not touching the visible workspace may continue — being told "I'll take over" is about the workspace, not necessarily every invisible background task) |

## 4. Invisible agent routing (Part Q)

### 4.1 Orchestration flow for "should I buy this car?"

1. MainAI's own reasoning identifies which departments are plausibly relevant — this is
   MainAI's judgment call, not a fixed rule table, but every department it decides to consult
   is logged as part of the resulting answer's provenance (§4.3).
2. **Parallel dispatch, not sequential**: independent specialists (Vehicle, Finance,
   Insurance in the founder's example) don't need each other's output to do their own
   analysis, so they run as independent `BACKGROUND_AGENT_TASK`s concurrently — sequential
   dispatch would only be justified if one specialist's output were a genuine *input* to
   another's analysis (e.g. Insurance needing Vehicle's specific risk-category finding first),
   decided per-query, not hardcoded.
3. Each specialist returns its own bounded finding plus its own evidence chain (reusing
   `app.evidence_claim`'s discipline — a specialist's claim needs the same "supports the
   exact proposition" rigor as any other evidence-backed claim in this system).
4. **Disagreement handling**: if two specialists' findings genuinely conflict (not just
   differ in emphasis — a real contradiction, e.g. Finance says "affordable," Insurance says
   "this exact model has abnormally high premiums that break the budget"), MainAI does **not**
   silently pick one. This reuses the already-established `app.mainai_execution.
   lesson_conflicts` doctrine verbatim: both findings are surfaced, marked as disputed
   relative to each other, and MainAI's synthesized answer to the owner explicitly names the
   tension ("Vehicle and Finance both look fine on their own, but Insurance flags a real
   conflict with your budget for this specific model — here's why") rather than averaging or
   guessing which specialist "wins."
5. MainAI synthesizes ONE coherent answer for the owner. The synthesis is itself a durable
   artifact (so "who checked this?" — §4.2 — has something real to point at), not a
   throwaway string.

### 4.2 "Who checked this?" provenance reveal

Opens a `VISIBLE_SURFACE` listing: which departments/specialists were consulted, each one's
individual finding, each finding's own evidence chain, and (if applicable) the disagreement
note from §4.1 step 4. This is the same `VISIBLE_SURFACE` mechanism as "show me why" (§3) —
provenance reveal is not a special case, it's the general evidence-surfacing mechanism applied
to a multi-agent synthesis instead of a single claim.

### 4.3 Provenance record shape

```python
@dataclass(frozen=True)
class SpecialistFinding:
    department: str
    specialist_key: str
    finding_summary: str
    evidence_refs: tuple[str, ...]   # points into app.evidence_claim-backed records
    confidence: float | None         # never presented to the owner as certainty on its own

@dataclass(frozen=True)
class SynthesizedAnswer:
    owner_facing_text: str
    findings: tuple[SpecialistFinding, ...]
    disputed_pairs: tuple[tuple[str, str, str], ...]  # (dept_a, dept_b, dispute_description)
    intent_object_id: UUID | None
```

## 5. `VISIBLE_SURFACE` lifecycle

- **Open**: an explicit owner request ("show X") or MainAI proactively surfacing something
  genuinely important (a real security incident, per Sentinel's V2-D design — never a
  routine/low-stakes update, which stays conversational).
- **Auto-close**: a surface with a clear, checkable completion (e.g. a confirmation dialog
  once confirmed) closes itself; an open-ended inspector-style surface (memory browser,
  security dashboard) stays open until the owner dismisses it or explicitly asks to see
  something else, at which point it's replaced, not stacked.
- **No persistent menu**: surfaces are never pinned as a permanent navigation structure the
  owner must learn — every surface is reachable by asking for it in plain language, and the
  orb itself is the only thing that's always present.

## 6. What V2-I deliberately leaves to other stages

- The actual security-incident detection that would trigger a proactive Sentinel surface:
  V2-D.
- The department/specialist competence and contract details referenced in §4: V2-E.
- Guardian's own authority-boundary enforcement over what MainAI is even allowed to route to:
  V2-B.
