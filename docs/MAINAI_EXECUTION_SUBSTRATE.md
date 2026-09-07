# MainAI execution substrate

This isolated lane freezes Personal Recall at `024835547850035667c3d77383fd75699ceab178`
and supplies only lower-level execution primitives for a future Development Director. It does
not import Claude's branch, define Intent/Goal semantics, register routes, invoke real providers,
or modify Recall.

`app/mainai_execution/substrate.py` is a durable SQLite adapter for controller-level state:
jobs, process heartbeats, provider availability, and worktree claims. A job claim carries a
random attempt identity and monotonically increasing lease generation. Every renewal and
completion is fenced by worker, attempt, generation, cancellation and lease expiry. A process
restart therefore cannot inherit authority merely by reusing a PID or worker name.

Stale running jobs become `abandoned`; a later claim is a new attempt. Worktree paths are unique
claims, and completion requires fresh git evidence: the expected base SHA, actual HEAD, branch,
clean status, and a non-protected ref. Provider state records availability and authorization
separately; exhaustion never widens authority. `retry_or_reassign` only returns abandoned or
failed work to `queued`.

`DeterministicFakeProvider` implements the existing provider interface without network access and
has bounded usage/failure behavior for orchestration tests. It emits no credentials or raw
diagnostic content. Production callers should adapt these operations to the existing
`mainai_jobs`/lease services rather than create a second production queue; no adapter is wired
in this round.
