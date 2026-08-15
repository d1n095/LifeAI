# LIFE GOALS / DREAMS / DEPENDENCIES + BLOCKER & OPPORTUNITY CONTINUITY

Life Core preserves what the founder wants, needs, dreams about, intends, or must do over
time. Dreams and long-term intentions are not automatically executable tasks. Existing
MainAI goals, plans, tasks, jobs, approvals, retries, and V0.3 waits remain the execution
system; a broader life intent may reference a MainAI goal but never replaces it.

An intent has an explicit kind, lifecycle, classification basis, authority, and provenance.
`unknown` is valid. Completed, abandoned, blocked, waiting, future, and superseded records
remain historical truth. State transitions and blocker changes are append-only audit events.

Blockers use a narrow category vocabulary and retain resolution evidence. Waiting is distinct
from blocking and permanent failure. Execution-specific waits continue to use MainAI's V0.3
wait records; broader intents may reference such canonical objects rather than duplicating
their state.

Dependencies are explicit `requires`, `blocks`, `enables`, `milestone_of`, `supports`,
`conflicts_with`, or `supersedes` edges. Deterministic feasibility follows only required
dependencies, reports active blockers and state with explanation paths, detects cycles, and
enforces depth/node bounds. One blocked branch never freezes unrelated feasible work.

Memory Threads retain surrounding history and intents reference them without copying content.
Active Context may select individual intent or blocker references; nothing automatically
loads all goals into context. Composite owner constraints, RLS, typed-reference validation,
and idempotency protect isolation and replay. All behavior works without AI or providers.

Deferred layers: automatic goal/dream extraction or inference, AI priority scoring,
autonomous prioritization/scheduling, resource optimization, completion forecasts, automatic
dependency/blocker discovery, negotiation between conflicting goals, budget allocation,
world-model planning, and automatic long-horizon strategy generation.
