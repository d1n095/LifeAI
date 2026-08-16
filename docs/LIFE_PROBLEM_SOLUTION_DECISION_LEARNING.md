# LIFE PROBLEM / SOLUTION / DECISION / LESSON INTELLIGENCE

## Purpose

Life Core preserves the deterministic history of a problem: observations, attempted
approaches, reusable components, explicit assumptions, decisions, observed outcomes, and
linked engineering lessons. The foundation works with every provider disabled. It records
manual and deterministic facts; it does not perform model-driven extraction, decomposition,
ranking, or synthesis.

This layer extends existing systems instead of replacing them:

- MainAI goals, plans, tasks, and jobs remain the execution system.
- Intelligence Governance executions, raw evidence, and ideas retain agent/model provenance.
- `engineering_lessons` remains the one reusable engineering-lesson store.
- Memory Threads provide durable history and Active Context selects current references.
- Source records, evidence payloads, tasks, and thread members remain canonical in their own
  tables. Problem-learning records reference them and never copy or rewrite their content.

## Deterministic model

A `life_problem` records an explicit problem formulation and its lifecycle independently of
task completion. A problem may be open, investigating, blocked, resolved, partially resolved,
invalidated, superseded, or unknown. Its provenance and authority distinguish founder truth,
deterministic/source-backed recording, inference, AI interpretation, and unknown origin.

Each `life_problem_approach` preserves a proposed or attempted approach, including failures and
rejections. `intended_outcome` is an intention only. Observed results are separate append-only
`life_approach_outcomes`; recording an outcome cannot overwrite the intention.

Each approach can contain independently reusable `life_solution_components`. Append-only
component evaluations can judge a component verified-useful, useful with changes,
context-specific, neutral, incorrect, unsafe, disproven, unverified, or unknown regardless of
the parent approach status. Therefore a failed overall approach may still contribute a strong
component.

Explicit assumptions remain separate records with untested, supported, contradicted,
disproven, superseded, or unknown state. A transition records an audit event and may reference
existing immutable Intelligence Governance evidence. Assumptions never silently become facts.

Decisions are not ideas. They preserve authority, alternatives, selected approach/component,
and supersession. At most one active decision exists per owner and problem; replacement is
explicit and old decisions remain queryable. Founder decisions remain distinguishable from AI
suggestions.

Solution selections prepare later synthesis by referencing components from multiple approaches
without copying them. Narrow component relationships can explicitly record conflicts,
incompatibility, support, or derivation. No synthesis is performed in this foundation.

## Evidence, lessons, history, and security

Raw Intelligence Governance evidence is never replaced by an interpretation. Component
evaluations, observed outcomes, lesson links, solution selections, component links, and audit
events are append-only. Mutable lifecycle records use explicit transition services and row
locks; idempotency-key reuse with different semantics fails closed.

Engineering Lessons are linked by identity. Case-specific attempts and outcomes do not become
reusable lessons automatically. Both positive patterns and failure chains can link to the
canonical lesson once a reusable rule is justified.

All personal records carry `owner_id`, use owner-composite foreign keys where applicable, and
have forced PostgreSQL RLS. Polymorphic Memory Thread and Active Context references use their
existing closed vocabularies plus service-level ownership/existence validation. Restricted
runtime privileges prohibit deletion and prohibit updates to append-only records.

The deterministic query layer answers open problems, attempted approaches by state, unverified
assumptions, the active decision, and useful components from failed/rejected approaches. It
uses no opaque score and invokes no provider.

## Principles

- Life learns from failures as evidence.
- Overall result quality and component value are separate.
- A weak solution may contain reusable strong components.
- Decisions are not ideas.
- Assumptions are not facts.
- Intended outcomes are not observed outcomes.
- Founder authority remains distinguishable from inference.
- Existing Engineering Lessons are reused.
- Cross-agent provenance is preserved through existing executions, ideas, evidence, and
  evaluator executions.
- Future synthesis may combine verified components from multiple agents or approaches while
  retaining every original reference.

## Explicitly deferred

- automatic problem extraction
- automatic solution decomposition
- automatic component scoring
- automatic root-cause discovery
- automatic contradiction discovery
- automatic synthesis
- dynamic agent routing
- automatic capability scoring
- world-model reasoning
- automatic policy mutation
- autonomous self-improvement decisions
