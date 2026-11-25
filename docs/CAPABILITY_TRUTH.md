# Capability and route truth (D1/D2)

`backend/app/mainai_executive/capability_truth.py` answers two founder questions with
evidence instead of impressions:

- **D1 — what can MainAI actually do?** A declared inventory of the code paths that exist,
  with each of four facts kept separate: `wired` (static checkout evidence, supplied by
  `SystemIntelligence`), `available` (only from a caller-supplied probe), `permitted` (only
  from a caller-supplied decision) and the gate that would authorize it.
- **D2 — which chat route would actually run?** `chat_route_truth()` calls the same
  `resolve_chat_chain()` the chat router uses, then adds what is known about each entry:
  configured, external, circuit state, exact-model verification, and `usable_now`.

## What it deliberately does not claim

- A vendor advertising `tools` is not a wired tool-calling transport. `model_tool_calling()`
  answers from `ModelSpec.supported_capabilities` (text chat and embeddings) and reports
  `wired: false` with the vendors' claims kept visible as claims.
- An unanswered probe stays `None`. Nothing is rounded up to "available", and nothing is
  reported as "permitted" because it happens to be wired.
- `configured` means a key or local runtime is present, not that the model answers.
- A closed circuit can also mean the breaker store is unreachable (the breaker fails open),
  so `circuit_open: false` is not health evidence.
- Verification is exact-model evidence, but the legacy chat path does not require it.

## Boundary

The view performs no probe, contacts no provider, authorizes no spend and grants no egress.
`execution_authorized` is always `false`; the caller still has to pass egress, billing and
circuit boundaries exactly as before. `SystemIntelligence.capabilities(db)` is the single
entry point and reuses the instance's existing static scan rather than a second one.
