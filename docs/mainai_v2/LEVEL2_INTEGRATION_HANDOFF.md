# MainAI Level-2 integration handoff

This lane composes the verified lower runtime through an adapter rather than copying its
authority model.  `mainai_jobs`, `mainai_goals`, and `mainai_tasks` remain canonical; the
Level-2 journal is evidence and recovery input only and never grants a claim, lease, review,
merge, or deploy authority.

Resource Intelligence Round 2 was independently rerun at `063c2569a170ccc3eb7887eadd2ed1b7caed73ff`:
141 scoped PostgreSQL tests passed.  The candidate is advisory-only and is not imported into
this lane; future wiring should use an adapter carrying recommendations with `authorized=False`.

The integration harness currently provides:

- durable-shaped program contracts and append-only recovery journal,
- owner/program-scoped readiness and dependency selection,
- bounded continuation for partial or premature returns,
- exact-SHA artifact freeze and independent examiner routing,
- stale/wrong-review rejection and fix/new-SHA flow,
- provider exhaustion observation without authority widening,
- canonical MainAI goal/task adapter (`CanonicalProgramStore`), and
- deterministic 1,000-job unattended composition tests.

Real provider invocation, merge, deploy, and live chat/UI wiring remain disabled.  A production
deployment must connect the `RuntimePort` to the independently verified runtime adapter and
re-read canonical PostgreSQL state before every consequential action.
