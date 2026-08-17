# LIFE STRATEGY EVALUATION, EXPERIMENTATION & PROMOTION

## Purpose

Life learns how to work better from verified evidence. This foundation compares bounded executions, records experiments, and prepares governed promotion proposals. It does not route work, activate a strategy, call a provider, or modify Life Core policy.

Quality comes first; efficiency is considered only inside a quality-safe envelope. Faster, cheaper, or shorter work is not better when required verification is incomplete, a regression remains unresolved, or scope was violated. Model confidence never counts as verification.

## Reused truth

0044 references the existing canonical records rather than copying them:

- Intelligence Governance executions, evidence, ideas, provider/model/version, tools, environment, role, and task context;
- 0043 versioned work strategies, execution bindings, traces, efficiency observations, findings, verification obligations/results, stopping decisions, specialist contributions, and Engineering Lesson links;
- Problem/Solution/Decision problems, components, and explicit assumptions;
- MainAI tasks and its existing execution, verification, approval, checkpoint, and recovery systems.

Raw observations remain separate from derived assessments. Reassessment appends a new comparability or quality record; it does not rewrite the raw trace, evidence, verification, or metric.

## Comparison and comparability

A strategy comparison has one baseline binding and one challenger binding plus bounded task/problem/domain/risk context. Each binding preserves strategy version independently from provider, model/version, agent, tools, and environment. There is no global winner field.

Comparability is explicit: `comparable`, `partially_comparable`, `not_comparable`, or `unknown`. Dimensions and reasons state what was actually matched. Invalid and unknown comparisons remain historical evidence but do not count as valid promotion support.

## Quality gate and efficiency

Quality assessments are deterministically derived from 0043 required verification obligations and their observations. No obligations, missing observations, failed verification, unresolved regressions, and scope violations fail closed. Promotion requires quality-passing valid comparisons.

Efficiency deltas reference the two canonical 0043 metric observations. The metric, unit, baseline value, challenger value, and arithmetic delta remain queryable independently; there is no magic aggregate score. A negative duration delta is evidence of less elapsed time, not proof of a better strategy.

## Experiments and promotion

Experiments preserve a hypothesis, exact intended change, expected benefit, quality invariants, scope, applicability, optional sample expectation, and an auditable bounded lifecycle: `draft → ready → running → completed/failed/cancelled/invalidated`.

Promotion candidates are separate proposals with applicability, tradeoffs, minimum evidence expectations, and an auditable lifecycle. Approval is only evidence that the proposal passed this review state; it has no code path that activates a strategy or rewrites production policy. Rejected, invalidated, and superseded candidates remain queryable.

Promotion summaries are recalculated from linked comparisons. They preserve total, valid, invalid, quality-pass/fail, and unresolved-conflict counts. Repeated verified evidence matters more than one lucky run; the deterministic default requires at least two valid comparisons unless a narrower manual threshold is explicitly recorded.

## Learning below the winner/loser level

A losing strategy may still contribute a useful idea or component. Typed learning links connect comparisons or experiments to existing Intelligence Ideas, solution components, assumptions, and evidence with useful, harmful, unproven, context-specific, accepted, rejected, or deferred dispositions. Rejected ideas remain historical evidence.

Assumptions and contradictions reuse the existing problem/intelligence records. They are linked as supporting, contradicting, invalidating, or unresolved evidence rather than copied into another graph.

0043 Engineering Lesson links remain the reusable lesson mechanism. Migration 0044 only adds `rejected_pattern` to the forward vocabulary, alongside candidate, verified, counterexample, and superseded patterns.

## Search, stopping, and cross-agent learning

Learning observations can reference canonical trace events and stopping decisions to record deterministic patterns such as repeated queries, successful narrowing, dependency-first navigation, tests-to-source tracing, search latency, continuation value, or premature stopping. No semantic relevance is guessed.

The same strategy may be used by different models, and the same model may use different strategies. Provider, model/version, tool access, environment, strategy version, task context, and time remain independent dimensions. Claude, Codex, local models, deterministic tools, humans, and future specialists are contextual participants and teachers—not permanent winners or dependencies.

## Security and determinism

All 0044 records are owner scoped with composite owner-aware foreign keys, forced RLS, indexed owner/reference paths, and least runtime privileges. Raw comparison evidence and links are append-only. Lifecycle transitions use row locks and append-only events. Idempotency replay returns the original semantic record; conflicting key reuse fails closed.

The entire layer works with no Claude, Codex, OpenAI, local model, external LLM, or external API.

## Protected-vs-adaptive boundary (cross-system, verified)

This document's own prose already claimed "no code path that activates a strategy or rewrites
production policy" -- `tests/backend/mainai/test_adaptive_cognition_protected_boundary.py`
(added for the mission "LIFE SELF-MODEL, ADAPTIVE COGNITION & CORPUS READINESS") is the first
test to prove that claim against a REAL, separate governed subsystem rather than only within
this module's own tests. Two proofs: (1) a structural, AST-level check that
`app.strategy_evaluation`/`app.work_intelligence`/`app.strategy_synthesis` import nothing from
`app.agent_coordination` or `app.mainai_execution.approval` at all -- there is no code path
capable of reaching the dispatch/approval gate in the first place, the same pattern
`docs/LIFE_AI_INDEPENDENCE_CONSTITUTION.md` §3 already established for the AI-independence
boundary; (2) a behavioral proof that a strategy taken all the way through this module's own
real evaluate → verify → compare → promote → approve pipeline still leaves a real
`AgentWorkAssignment`'s dispatch gate reporting `APPROVAL_REQUIRED` until the founder's own,
completely separate `grant_task_approval()` is called -- the strongest evidence this module's
own adaptive-cognition layer can produce has zero effect on either governed gate.

## Explicitly deferred

- automatic production strategy activation or production policy mutation;
- autonomous routing, agent-team composition, or external-model invocation;
- global leaderboards, permanent winner selection, or exploration scheduling;
- reinforcement-style policy mutation or autonomous self-editing of Life Core;
- semantic/generated code search or automatic relevance judgments;
- automatic idea, assumption, contradiction, or strategy extraction;
- automatic strategy synthesis or autonomous experiment scheduling;
- full contradiction graph or world-model planning.
