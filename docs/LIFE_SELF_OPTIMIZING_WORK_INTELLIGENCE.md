# LIFE SELF-OPTIMIZING WORK INTELLIGENCE

## Work Strategy Learning

Life Core records not only what work produced, but how the work was performed. This foundation
is deterministic and works with every model and provider disabled. It records versioned
strategies, meaningful work traces, measurable efficiency observations, verification
obligations, bottleneck findings, stopping decisions, and specialist contributions. It does
not select, mutate, route, or execute strategies automatically.

## Existing truth reused

- MainAI goals, plans, tasks, jobs, checkpoints, recovery, approvals, and task/job events remain
  the execution system.
- Intelligence Governance remains the source for immutable execution identity, provider,
  model/version, tools, environment, task context, raw quality/review evidence, and derived
  interpretations.
- `UsageLog` remains the source for provider tokens and monetary cost. Trace events may
  reference it; they do not copy its accounting.
- Problem/Solution/Decision Learning remains the source for problems, approaches, components,
  assumptions, decisions, and observed solution outcomes.
- `engineering_lessons` remains the sole generalized lesson store.

The new layer is an index and observation layer over these systems. Existing records are never
rewritten to make a strategy appear successful.

## Strategy identity and evolution

A work strategy has a stable owner-scoped key and immutable positive version. It records only
caller-supplied phase, tool-sequence, method, context-acquisition, search, inspection, testing,
verification, review, stopping, escalation, concurrency, and environment-assumption metadata.
Unknown and empty metadata are valid. A newer strategy may reference an earlier version, but
old versions remain queryable and no row identifies a permanent winner or best strategy.

`intelligence_executions.work_strategy_id` remains its original immutable textual execution
snapshot. The typed `work_strategy_executions` binding adds a durable reference to a versioned
strategy without rewriting migration 0038 history. It also connects the same execution to an
optional existing Life Problem and approach. Model/version, tools, environment, role, task type,
and strategy therefore remain independent comparison dimensions.

## Meaningful work traces and navigation evidence

Work trace events are append-only and receive a transactionally serialized sequence number per
strategy execution. Events describe meaningful actions such as repository mapping, symbol
search, file inspection, dependency traversal, git-history inspection, test selection,
focused/full testing, migrations, static analysis, reproduction, changes/reverts,
verification, stopping, and escalation.

A navigation event can preserve the literal query, scope, tool, target, match/item counts,
files or symbols followed, result, duration, provenance, and exact linked evidence when those
facts are available. Missing telemetry remains null or unknown; Life does not fabricate it.
This is not semantic search, a vector store, or a repository crawler.

## Quality first; efficiency second

Efficiency observations are append-only raw measurements: durations, searches, files read,
repeated reads, failed attempts, edits/reverts, test runs, provider/tool calls, tokens, cost,
retries, reviewers, rework, and time to useful or verified results. A measurement contains no
quality judgment.

Quality is represented separately through existing immutable Intelligence Evidence plus
explicit verification obligations and append-only performed-passed, performed-failed, missing,
waived, or unknown observations. A performed pass/failure requires an evidence reference.
The deterministic query reports raw quality and efficiency dimensions; it never computes a
leaderboard or treats lower duration as superiority. Required obligations are satisfied only
when all have explicit passing evidence. Skipping required verification is therefore visible
as missing, never an efficiency improvement.

## Waste, stopping, escalation, and lessons

Waste/bottleneck findings remain case-specific observations. They distinguish a suspected
waste pattern from an expensive but justified step and retain trace/evidence provenance.
Stopping decisions preserve whether work continued, stopped, escalated, ran further tests, had
sufficient/insufficient evidence, blocked, or deferred—and what happened afterward when known.

Specialist contributions reference a distinct existing Intelligence Execution and existing
review evidence. Unique, duplicate, confirming, false-positive, rework-saving, rework-causing,
and no-contribution outcomes remain distinguishable. External agents are specialists and
teachers, not permanent dependencies.

Candidate recurring patterns may link a strategy to an existing Engineering Lesson. This link
does not create or promote a lesson. A case-specific success never becomes a universal
playbook automatically.

## Governance principles

- Quality first; efficiency second.
- Faster is better only when verified quality and safety obligations remain satisfied.
- Life learns methods, not merely which model won.
- Strategies are versioned and contextual.
- Raw execution evidence is separate from derived conclusions.
- Failed strategies remain useful evidence.
- Search and navigation methods themselves are learnable.
- Avoid repeated unnecessary work while preserving justified investigation.
- Learn stopping and escalation patterns from observed outcomes.
- No permanent best-model or best-strategy assumption exists.
- Self-improvement remains versioned, testable, auditable, and governed.
- No silent core self-modification is permitted.

The future governed flow is: observe work, propose a versioned strategy/playbook change, test it
on bounded work, compare verified evidence, request external/founder review when risk warrants,
and only then adopt it through an explicit later policy layer.

## Explicitly deferred

- automatic strategy selection
- automatic strategy mutation
- autonomous routing
- automatic model or team composition
- automatic playbook promotion
- reinforcement-learning style policy updates
- automatic code-search query generation
- semantic code-search ranking
- world-model planning
- autonomous self-modification
- production policy mutation
