# LIFE ACTIVE CONTEXT INTELLIGENCE

## CONTINUOUS KNOWLEDGE INTEGRATION

Life should continue a subject instead of behaving as though every conversation starts from
zero. Active Context is the deterministic selection layer that identifies which existing
durable objects matter to a current working focus and explains why. It works without an LLM,
provider, external API, embedding, or vector search.

Active Context is selected references, never copied source truth. It does not copy message
text, document content, evidence payloads, decisions, or lessons. The referenced conversation,
Source Foundation, MainAI, knowledge, project-memory, or intelligence-governance record remains
canonical.

## Durable model

An `active_context_set` belongs to one owner and has one explicit anchor: a conversation,
message, source record, MainAI work object, governance object, project object, or a manually
selected topic label. Subject classification records whether its basis is deterministic,
manual, inferred, or unknown. The foundation never invents a semantic topic name.

An `active_context_member` is a typed reference plus activation metadata. It stores no copied
source content. Its reason, basis, authority, deterministic activation path, ordering, timing,
and active/pinned/suppressed/stale/superseded state explain why the object is present and
whether it should be treated as current.

An `active_context_event` is append-only audit evidence for creation, refresh, automatic
addition, pinning, suppression, and state changes. Old and superseded memberships remain
preserved rather than deleted.

## Deterministic activation

The resolver performs bounded breadth-first traversal over explicit database relationships.
Examples include:

- message → conversation
- MainAI task → plan and goal
- MainAI job → its linked task
- message source unit → memory source, message, and conversation
- document source unit or claim → its canonical source records
- intelligence idea/evidence → execution → MainAI task
- an idea explicitly linked to an engineering lesson → that existing lesson

Every discovered member retains the complete typed path and relationship labels. Traversal
deduplicates visited objects, is cycle-safe, and enforces maximum depth, total automatic
members, and per-type counts. These bounds are delivery-neutral: no provider context-window
size is canonical.

No semantic similarity or broad cross-domain traversal occurs. Cross-domain activation is
permitted only through an explicit durable relationship.

## Manual authority and temporal state

Founder/manual context is distinguishable from deterministic, inferred, AI-interpreted, and
unknown context. A manual pin survives refresh even when it is outside automatic bounds. A
suppressed member cannot immediately reappear through deterministic traversal; it becomes
eligible only after explicit unsuppression. Stale and superseded members remain queryable as
history but are excluded from the default current-context view.

The source object's timestamps remain canonical. Context records add only activation-specific
times such as when the reference was added or last activated, plus optional validity/expiry
metadata when a caller has a justified basis.

## Core principles

- Preserve focus rather than loading everything.
- Founder/manual context outranks inferred context.
- Every automatic inclusion must be explainable.
- Context expansion is bounded and cycle-safe.
- Relevant prior decisions, evidence, requirements, failures, lessons, and unresolved work should be recoverable.
- Deterministic resolution works without AI.
- AI may later improve semantic relevance; it does not own source truth.
- Old or superseded knowledge remains preserved.
- Context-window and provider limits belong to delivery layers, not canonical memory.

## Explicitly deferred

- semantic embedding-based context ranking
- automatic topic inference
- LLM summarization
- automatic cross-domain insight generation
- automatic contradiction discovery
- automatic stale-knowledge scoring
- token-budget optimization per provider
- context compression or summarization
- world-model reasoning
- autonomous web research
- automatic question generation
