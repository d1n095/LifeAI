# LIFE INTELLIGENCE GOVERNANCE & META-LEARNING

## Foundation scope

This document defines the observation-first foundation introduced by migration 0038. Life
Core records execution identity, context, evidence, interpretations, and individual ideas
without requiring Claude, Codex, OpenAI, another provider, or any external API. It extends
the existing MainAI goal/plan/task/job runtime; it does not introduce another queue,
executor, lesson store, provenance system, or generic knowledge graph.

The foundation is deliberately not a model leaderboard. A model/provider is never encoded as
a permanent winner, loser, specialist, or excluded option.

## Principles

- Learn from every agent, not only winners.
- Preserve useful sub-ideas from otherwise inferior or failed solutions.
- Separate raw observations from interpretations and scores.
- Preserve assumptions and evidence.
- Deterministic verification outranks model confidence.
- Track model/version/tool/environment changes over time.
- No permanent agent exclusion.
- Maintain exploration so improvements can be detected.
- Compare methods, not only final answers.
- Learn reusable process improvements from failures and reviews.
- Life ultimately orchestrates specialists rather than becoming dependent on any single provider.

## Existing systems reused

`mainai_goals`, `mainai_plans`, and `mainai_tasks` define the problem and task. `mainai_jobs`
remains the durable leased execution runtime. Task/job events, checkpoints, recovery records,
verification, and approval histories remain their respective sources of truth. Governance
evidence can reference those records and never replaces them.

`engineering_lessons` remains the only reusable engineering lesson store. An idea may link to
an engineering lesson as a failure, root cause, reusable lesson, or process improvement;
case-specific observations are not automatically promoted into lessons.

## Durable records

An `intelligence_execution` is an immutable identity and context snapshot for one candidate
execution of an existing MainAI task. Provider, model, version, agent identity, environment,
tools, capabilities, strategy, role, participation mode, domain, and task type are recorded
independently. Unknown values are valid. Multiple primary, shadow, parallel, challenger, or
review executions may reference the same task.

`intelligence_evidence` stores immutable raw observations with explicit source provenance.
Self-review, independent-model review, deterministic-tool verification, and founder review
are structurally distinguishable. Derived judgments are separate immutable
`intelligence_interpretations`; recalculation creates another interpretation and cannot
rewrite its source evidence.

`intelligence_ideas` preserves an individual idea, proposal, approach, risk, test strategy,
architectural pattern, optimization, or assumption with its originating execution and
evidence. Accepted, rejected, deferred, and unknown dispositions are all durable. A failed
execution can therefore contribute an accepted idea, while rejected ideas remain available
for later contradiction or reconsideration.

The narrow idea-link vocabulary supports reuse, contradiction, dependency on an assumption,
and combination. It is intentionally not a full assumption/contradiction or knowledge graph.

Identity snapshots plus task type, role, tools, environment, strategy, and timestamps retain
the raw dimensions needed to measure capability changes over later time windows. Any future
score must remain context/version specific, retain uncertainty and sample size, and be
recalculable from observations. No global score is source truth.

## Explicitly deferred layers

- adaptive agent routing
- capability matrix and derived scoring
- exploration scheduling
- automatic team composition
- cognitive diversity engine
- automatic solution decomposition
- automatic cross-agent synthesis
- full assumption/contradiction graph
- world model
- automatic question generation
- automatic process-policy mutation

Those layers may interpret this foundation later. They must not overwrite its evidence or
make permanent exclusion the natural outcome of historical performance.

## Engineering lesson from this foundation

Privilege verifiers must model each function's intended security mode explicitly. Trigger
functions that need no elevated privileges should remain invoker-security; a verifier that
blindly requires `SECURITY DEFINER` can encourage needless privilege or prevent startup. The
regression coverage checks both the intended security mode and the exact runtime grants.
