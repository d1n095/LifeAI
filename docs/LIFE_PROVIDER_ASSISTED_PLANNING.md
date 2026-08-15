# LIFE PROVIDER-ASSISTED PLANNING GATE

Providers are bounded planning specialists, never execution authorities. MainAI owns goals,
tasks, jobs, approvals and checkpoints; Safe Planner owns deterministic acceptance; Work
Driver controls bounded execution; Development Operator performs repository effects; and
verification decides whether work is correct. Provider output cannot collapse these layers.

## Planning boundary

Safe Planner first resolves source authority and ambiguity and checks its explicit local
recipe registry. A sufficient deterministic recipe avoids a provider call. Otherwise a
provider-neutral adapter receives a bounded request containing redacted founder instruction,
authority class, requested outcome, constraints/prohibitions, typed Active Context
references, hashed repository identity, allowed paths, base/branch expectations, capability
vocabulary, risk prohibitions, output schema and size/count limits. It receives no environment
dump, file contents, credentials or unrestricted filesystem view.

The provider returns a strict JSON envelope containing a Safe Planner `PlanCandidate`, an
optional clarification request, capability gaps, bounded useful components and optional
confidence metadata. Confidence is neither authority nor verification. Provider/model/version
attribution is imposed from the actual adapter response, not trusted from generated JSON.
Malformed or oversized output fails closed.

## Validation and dispositions

Every executable candidate passes the existing Safe Planner checks for canonical founder
source, authority, owner/task/job/repository identity, path scope, dependency order, bounds,
capability vocabulary, risk, approval, secrets and verification implications. Shell, merge,
deploy, force-push, production mutation, arbitrary executables and remote write remain
forbidden. Accepted output is the existing Work Driver `DevelopmentPlan`; no provider or
planner invokes repository tools.

Explicit dispositions include `ACCEPTED`, `REJECTED_UNSAFE`, `REJECTED_UNSUPPORTED`,
`NEEDS_CLARIFICATION`, `NEEDS_AUTHORIZATION`, `CAPABILITY_MISSING`, `PROVIDER_FAILED` and
`WAITING_PROVIDER`. Material intent is never silently repaired. Equivalent accepted request
replay uses checkpointed normalized candidate data; revisions retain Safe Planner predecessor
and reason.

Multiple explicitly supplied candidates can be evaluated independently without automatic
winner selection. Useful components from a rejected candidate remain Intelligence Ideas with
provider provenance and disposition. Combining components is not verification and must later
pass Safe Planner and the normal 0045 experiment/promotion governance.

## Interruption, evidence and learning

Provider outage, quota/rate limit, authentication failure or timeout checkpoints the exact
request hash, safe failure category, provider/model hint, context-set reference and unresolved
planning need as `WAITING_PROVIDER`. Deterministic progress remains canonical and retryable;
no provider owns task state.

Successful calls reuse `UsageLog` for tokens and known/unknown cost. Intelligence Execution,
Evidence and Ideas preserve provider/model/version, latency, candidate disposition,
confidence as metadata, capability gaps and useful components. The existing 0043 strategy
execution records the specialist contribution. Later verified task outcomes may evaluate
planning usefulness without creating permanent model winners.

The same provider-neutral adapter contract supports a future local model. LifeAI self-work
has no bypass: provider candidate → Safe Planner → Work Driver → Development Operator →
verification/approval.

## Deferred

Automatic provider routing, quota scheduling/economics, model leaderboards, permanent winner
selection, autonomous teams, global task prioritization, automatic component synthesis,
strategy activation/promotion, capability installation, unrestricted self-improvement,
autonomous PR creation, merge, deployment, production mutation, browser automation,
unrestricted shell and reinforcement-style policy mutation remain outside this foundation.
