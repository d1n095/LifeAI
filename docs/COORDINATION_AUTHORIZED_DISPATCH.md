# Authorized dispatch step — real dispatch or durable WAITING_FOR_PROVIDER

Follows task selection in Life's development-management loop (GitHub evidence → select →
**dispatch** → …). `agent_coordination/authorized_dispatch_step.py::dispatch_or_wait` composes
EXISTING public primitives only — no new queue/registry/approval/STOP/transport:

    transition_status (planned→ready) → evaluate_dispatch_readiness(require_adapter_enabled=True)
    → get_real_adapter → dispatch_assignment    (real start on a confirmed adapter)
    else → durable WAITING_FOR_PROVIDER (ready→blocked, named missing capability) + resume

## Honest availability (this environment)
The three supported agents (claude-code / cursor-agent / codex) require founder opt-in
(`LIFE_AGENT_ADAPTER_ENABLED__<KEY>=true`), the executable on PATH, AND a founder-supplied argv
template before a real adapter is constructed; otherwise `get_real_adapter` returns the
fail-closed `NotConfiguredAdapter`. **No supported agent is enabled or installed here**, so a
genuine external round-trip is NOT available — and is never simulated. The verified outcome is
WAITING_FOR_PROVIDER; the assignment is preserved durably and resumes when a provider appears.

## Executed evidence (real Postgres, no injected adapter)
`tests/backend/test_authorized_dispatch_step.py` — 4 passed:
1. Unavailable provider → durable WAITING_FOR_PROVIDER (blocked, `missing_capability=ADAPTER_DISABLED`), then resume → ready.
2. Unrelated authorized work (a different agent) is not blocked by a waiting assignment.
3. Duplicate/conflicting assignment prevented (`DuplicateWorkError`).
4. Enabled-but-incomplete config (REAL env flags + a REAL on-PATH binary, no argv template) →
   fail-closed `REAL_PROVIDER_NOT_CONFIGURED` via `NotConfiguredAdapter`; a successful delivery
   is NEVER fabricated even when a provider is "enabled".

## Honest blocker
`dispatch → receives evidence → verify → store` to a real external agent requires a
separately-reviewed real Agent Runtime + a founder-enabled, installed agent CLI, neither present
here. This step dispatches to it when available and durably waits otherwise.

Depends on #314/#338. **Independent review required — author does not self-certify.** No merge,
no paid providers, no STRATO; `first_cycle_go=false`.
