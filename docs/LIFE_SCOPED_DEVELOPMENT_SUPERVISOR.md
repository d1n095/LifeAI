# LIFE AUTONOMOUS SCOPED DEVELOPMENT SUPERVISOR

The Scoped Development Supervisor continuously coordinates bounded engineering work inside
one explicitly founder-authorized MainAI goal. It is not a scheduler, planner, job queue, or
repository operator. MainAI remains canonical for plans, tasks, jobs, approvals, leases,
events and checkpoints. Safe Planner and Provider-Assisted Planning produce validated plans;
Work Driver controls execution; Development Operator performs effects; verification decides
whether work completed.

## Authorization and scoped autonomy

A run binds owner, parent goal, exact founder-instruction hash and authority reference,
repository identity, allowed paths, capabilities, maximum risk and explicit completion
criteria. Missing authority returns `NEEDS_AUTHORIZATION`. A founder correction changes the
canonical instruction hash and invalidates stale work before dispatch. Repository/path and
capability scope are checked while work is still a candidate, then checked again against the
leased operator context. Self-work uses precisely the same gates.

The supervisor may select work only among existing child MainAI tasks for that goal. It does
not invent top-level missions, projects, goals or free-text executable work. Provider ideas
must first become authorized canonical work and then pass the normal planning gate.

## Assessment, discovery and selection

Current-state assessment queries bounded owner/goal-scoped MainAI tasks and their canonical
statuses, priority, risk, blockers, dependency-derived readiness, execution binding and
capability needs. A linked Life Intent uses the existing deterministic feasibility resolver.
Active Context is assembled by Safe Planner/Work Driver for the selected task rather than
copied into supervisor state.

Discovery is count-bounded and explainable. Completed, failed, cancelled, pending, blocked
and waiting work remains visible but non-actionable. Selection is deterministic: the highest
canonical priority among ready/running in-scope candidates wins. A meaningful equal-priority
tie returns `NEEDS_SELECTION`; there is no opaque score or provider-confidence shortcut.
Blocked, provider-waiting and capability-missing candidates do not freeze independent work.

## Planning, execution and continuation

An existing ready task is dispatched through MainAI's approval-enforcing dispatcher. Its
repository execution context must have a live owner/task/job lease and remain within the
authorized scope. An explicit candidate goes through Safe Planner. Work without a sufficient
deterministic candidate goes through Provider-Assisted Planning, where the provider remains a
specialist and Safe Planner remains the gatekeeper. Only an accepted `DevelopmentPlan` reaches
Work Driver and Development Operator.

After each child result the supervisor reassesses canonical task readiness. It continues only
when authority remains current, another task is deterministically actionable, approval is not
missing, repository identity is valid, and invocation job/time/count bounds remain. Failed or
missing verification never completes a child or parent; it returns a bounded repair signal.
Provider outage and capability gaps are checkpointed while independent work may continue.

## Checkpoint, recovery and learning

Append-only MainAI checkpoints store phase, candidates, selection reason, completed child IDs,
current job, planning/driver checkpoint and next transition. Repeated invocation reads the
latest canonical checkpoint, so verified child work is not replayed. Work Driver and Operator
retain their existing idempotency and lease fencing; stale workers cannot act, while existing
V0.2/V0.3 takeover can recover a dead child job. Provider sessions never own supervisor state.

Selection produces deterministic Intelligence Evidence on the existing strategy execution;
operator actions, verification, provider attribution and usage continue feeding the existing
0038 and 0042–0045 learning chain. The supervisor creates no parallel evidence or policy
system and never promotes a strategy automatically.

## Parent completion and safety

The parent goal completes only after every required child is terminal-successful and explicit
completion criteria exist. No-candidate is not proof of completion. Mandatory blockers,
failed work, missing review or unknown completion criteria keep the goal active and return an
explainable stop state.

Secrets and source content are not copied into supervisor checkpoints. Existing planner and
operator redaction, path denial, capability validation, approval, lease and verification
boundaries remain intact. No shell, merge, deploy, force-push, production mutation or direct
self-modification capability is introduced.

## Deferred autonomy

Global cross-goal prioritization, automatic project/goal creation, provider routing and quota
economics, model leaderboards or permanent winners, specialist swarms, capability installation,
strategy activation/promotion, PR creation, merge, deployment, production mutation, browser
automation, unrestricted shell, corpus-wide planning, world-model reasoning and reinforcement-
style policy mutation remain explicitly deferred.
