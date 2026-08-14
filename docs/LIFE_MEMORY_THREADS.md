# LIFE MEMORY THREADS + CROSS-CONVERSATION CONTINUITY

Memory Threads are Life Core's durable continuity index. A thread says that existing objects
belong to one continuing subject, project, problem, or history. It is not a summary, copied
source content, an LLM conversation, a new provenance store, or a generic knowledge graph.

Conversations remain immutable historical containers. A thread can span multiple conversations,
individual messages, documents and source units, knowledge claims, projects, MainAI work,
engineering lessons, intelligence-governance evidence and ideas, and Active Context sets. Every
member is a typed reference to its canonical record. The same object may legitimately belong to
multiple threads.

## Deterministic foundation

Threads may exist without a name. Manual and system labels are separate, and their classification
basis is `manual`, `deterministic`, `inferred`, or `unknown`. This lets a founder correction become
the current authoritative label without erasing an older inference. Lifecycle states are active,
dormant, completed, superseded, and archived; superseded and merged histories remain queryable.

Membership records explain why an object belongs: founder addition, deterministic relationship,
imported structure, common project/goal, continuation, explicit reference, inference, or unknown.
They store provenance and index timing, never source text. Deactivation changes membership state
instead of deleting it. Append-only events audit creation, membership changes, labels, lifecycle,
relationships, merges, and branches.

Thread relationships are deliberately narrow: related, parent, child, branch, continuation,
supersedes, and merged-into. Self-links and cross-owner links fail at both service and database
boundaries. A merge copies active references into a destination, marks the source superseded, and
preserves both histories. A branch creates a separate thread and copies only explicitly selected
members. Neither operation rewrites canonical objects or old membership.

Explicit deterministic expansion reuses Active Context's existing typed-reference ownership and
relationship resolver. It is bounded by depth, total members, and per-type counts, and is cycle-safe
and replayable. It never performs semantic crawling. Idempotency keys converge equivalent retries
and fail closed if reused for different semantics.

## Active Context bridge

Active Context answers “what is relevant right now?”; Memory Threads answer “what durable history
belongs to this continuing subject?” Active Context may pin a `memory_thread` reference. That bridge
does not implicitly load the thread's members. Future selection can choose a bounded subset from the
thread, preserving Active Context's focus and explainability.

## Authority, ownership, and independence

Founder/manual facts remain distinguishable from deterministic and inferred links. Old history is
preserved. Composite owner foreign keys protect thread membership and relationships even for
privileged callers; RLS and least-privilege runtime grants isolate normal access. Polymorphic member
references are checked against a closed vocabulary and validated for existence and ownership before
insertion.

All creation, membership, expansion, merge, branch, relationship, retrieval, audit, and Active
Context bridge operations run without OpenAI, Claude, Codex, embeddings, external APIs, or any model.
AI may later suggest links, but cannot silently rewrite canonical membership or history.

## Explicitly deferred

- automatic semantic thread discovery and embedding-based clustering
- automatic thread naming or LLM summaries
- automatic merge/split decisions
- automatic contradiction or cross-domain connection discovery
- autonomous corpus classification
- world-model reasoning
- automatic knowledge compression
