# Agent Context Rules
**Sources:** `04_MEMORY_CONTEXT_AND_MEANING_ENGINE.md`, `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` (MainAI_Conversation_Knowledge_Pack_2026-07-19), and the review overlay's `FKP_V1_AUDIT.md` finding H-01 (integrated below as an explicit amendment, per the founder's instruction to integrate the review overlay into FKP v1.1).
**Status:** Binding standard for MainAI and all future agents (Conversation Context Resolver), **as amended by H-01 below**.

---

## H-01 amendment — stop-and-ask is narrowed, not removed

FKP v1.0's version of this document (and `ADAPTIVE_WORK_ORCHESTRATION.md`) required stopping at essentially every information gap or two-candidate ambiguity. The review overlay (`FKP_V1_AUDIT.md` §H-01) correctly flagged this as too rigid, and the founder's own 2026-07-19 instruction to this session ("don't throw decisions back — keep working independently, don't ask me to pick between options, ask only if you are truly blocked by secrets, cost, an irreversible action, or a foundational product decision") is a live instance of exactly this correction. The amended rule, replacing the "Reference resolution" and "Absolute prohibitions" stop-conditions below wherever they said "ask" or "never fill a gap":

- Continue with clearly labeled, safe, reversible assumptions within the approved direction.
- Only stop and ask when the answer would change vision, security, cost, an external/irreversible action, or a genuinely foundational design choice.
- Separate "needs an answer now" from "can be verified later" — the latter goes in `07_CONFLICTS_AND_GAPS/MISSING_INFORMATION.md`, not a blocking question.
- Keep an open, visible list of assumptions made this way (this document, decision registers, and status notes in each deliverable serve that role in this package).

This amendment does **not** weaken any founder-approval gate in `01_FOUNDER_VISION/NON_NEGOTIABLE_PRINCIPLES.md` or the trust/security gates in `03_ARCHITECTURE/ADAPTIVE_WORK_ORCHESTRATION.md` — merge, deploy, spend, irreversible migration, and permission escalation still always stop and wait. It only narrows the instinct to stop for *ambiguity that doesn't touch those gates*.

---

## Context assembly order (every message, every session)

1. Current message and explicit command — **highest priority**
2. Explicit corrections, withdrawals, and approvals in the current turn
3. Last 3–5 messages verbatim with roles and timestamps
4. Last 15 messages as structured timeline with timestamps
5. Active task, current page, current project phase, pending action
6. Applicable confirmed decisions (from `02_DECISIONS/DECISION_REGISTER.md`)
7. Relevant durable memory selected by identity and scope
8. Supporting documents, code, tests, external evidence
9. Older summaries — only if not contradicted by above

## Message classification

For every new message, determine whether it is:
- continuation of current topic
- addition to active request
- correction (supersedes earlier belief)
- replacement of earlier instruction
- status request
- request for explanation
- request for action
- new topic
- temporary interruption
- withdrawal of approval

Short messages like `nu då`, `nästa`, `den`, `han`, `fortsätt`, `lägg till i förra meddelandet` are NOT content-free. Resolve them against the active action and most recent unanswered state.

## Reference resolution

Resolve pronouns and compressed phrases by scoring candidates using:
- recency
- grammatical fit (Swedish grammar applies)
- active topic
- active page/UI context
- current task
- user corrections
- semantic compatibility
- previous direct references

If two candidates remain equally plausible **and the choice affects a founder-approval gate or is otherwise irreversible**, ask one short clarification question. Otherwise, per the H-01 amendment above, proceed on the best-supported reading and note the assumption rather than stopping. Never choose silently on a gate-relevant ambiguity.

## Corrections and belief lifecycle

Every stored belief needs:
- valid-from time
- optional valid-until time
- source
- confidence
- status: active | superseded | disputed | expired | withdrawn
- pointer to the correcting statement

A correction from the user immediately supersedes the older belief. Do not continue applying the superseded belief. Do not delete the history — mark it superseded and preserve provenance.

## Context-carrying responses

Every useful response must contain enough context that another agent can reconstruct:
- what problem was being solved
- what the user wanted
- what was decided
- what remains open
- what step is active
- what cannot be done without approval

This is how project continuity is maintained without depending on a single chat session.

## Absolute prohibitions

- Never present reconstructed context as the user's exact words
- Never present an inference as a verified fact
- Never treat a superseded instruction as still active
- Never fill a gap that touches a founder-approval gate with an assumption — mark it as missing and ask or stop. For gaps that don't touch a gate, see the H-01 amendment above: continue with a labeled assumption instead of stopping.
- Never claim access to a file, page, or source that was not actually inspected in this session
- Never use a prior AI session's claims as proof — require independent verification

## Data isolation

All context, memory, and conversation data is strictly isolated:
- by user_id (RLS-equivalent — see `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md` for LifeAI's actual mechanism, `app/rls.py`, not literal Supabase RLS)
- by conversation_id
- by identity scope (MainAI, Founder UserAI, future UserAI)

No information may leak between scopes. No cross-user inference is permitted. No agent may access another user's memory, history, or documents.

## Memory write policy

Write durable memory only for:
- explicit decisions
- stable preferences
- project constraints
- verified state important to future work
- repeated confirmed requirements
- corrections needed to prevent recurrence

Do not write: transient emotions, speculative personal judgments, secrets, or unverified external claims.

## User control

The user must always be able to:
- inspect what was remembered and its source
- correct it
- restrict its scope
- export it
- delete it (when technically and legally permitted)
- see which agents accessed or changed it
