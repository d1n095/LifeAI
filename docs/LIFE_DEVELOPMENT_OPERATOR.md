# LIFE DEVELOPMENT OPERATOR

The Life Development Operator is a governed capability layer beneath the existing MainAI
orchestrator. MainAI goals, plans, tasks, jobs, leases, checkpoints, verification and
approvals remain execution truth; the operator does not schedule work or create another
coding-agent loop.

## Safety model

Every action is bound to an owner, current MainAI task/job, live fenced job lease, canonical
Intelligence Execution and 0043 Work Strategy Execution. Repository identity includes the
canonical root, exact base SHA and branch. Writes additionally require the existing V0.2
task worktree row and its on-disk ownership marker. A stale worker, another job, a changed
base, a different branch or an unowned workspace fails closed.

Capabilities have explicit risk classes: read-only repository inspection, local worktree
writes, allowlisted local validation, and separately authorized remote writes. There is no
raw-shell, merge, deploy or production capability. Plans must resolve to structured
capabilities with validated arguments; an unsupported action returns an auditable
`CAPABILITY_MISSING` result and may state that unrelated feasible work can continue.

Paths are repository-relative, containment checked and optionally narrowed to an approved
path scope. Direct `.git`, environment/credential files, common key stores and symlink reads
are denied. Reads and output are bounded. Secret-like values are recursively redacted from
results and audit payloads, and command environments use an allowlist rather than inheriting
arbitrary credentials.

Edits require an expected-before SHA-256 and record the resulting hash. Scoped staging is
explicit. Commit requires the existing verification checkpoint, a valid scoped staged diff,
`git diff --check`, and a secret-like-content check. Commit records the exact SHA. Push is a
separate `REMOTE_WRITE` gate, requires explicit authorization and the expected remote state,
and always uses the existing non-force worktree push. The operator cannot create a PR,
merge, force-push or deploy.

## Commands, audit and recovery

Validation uses fixed command profiles (focused/full pytest, Ruff, diff checking and bounded
migration checks), with fixed executable prefixes, validated arguments, environment
allowlisting, timeout and output limits. This is deliberately not a generic terminal.

Meaningful actions append to the canonical 0043 `work_trace_events` stream. They retain
task/job provenance, deterministic ordering, redacted arguments/results and idempotency.
Conflicting reuse of an idempotency key fails closed. Repository progress is captured in the
existing MainAI checkpoint system: workspace, base, branch, completed action IDs, next phase,
failures, dirty state and HEAD. The live job lease and worktree generation fence every new
action, so a restarted/takeover worker cannot act through stale authority. Provider failure
does not prevent deterministic repository work; provider-dependent steps can be recorded as
capability gaps or blockers without discarding local progress.

Action completion is not verification. A successful patch or zero command exit does not
mark a task verified. Existing verification and approval policy remains authoritative.

## Learning and self-improvement boundary

Operator traces are real execution evidence for the existing foundations: 0042
problem/approach/decision/outcome learning, 0043 strategies/navigation/efficiency/waste and
verification, 0044 comparisons/experiments/promotion proposals, and 0045 synthesis lineage.
The operator copies none of that truth and invokes no AI provider. LifeAI itself receives no
backdoor: changes to Life use the same isolated, bounded, verified and governed pipeline.

Deferred layers include autonomous coding-loop scheduling, task prioritization, quota-aware
or provider routing, automatic pushing, PR creation, merge/deploy, production operations,
unrestricted shell access, browser automation, semantic code search, image understanding,
universal parsing, autonomous capability installation and autonomous Life Core mutation.
