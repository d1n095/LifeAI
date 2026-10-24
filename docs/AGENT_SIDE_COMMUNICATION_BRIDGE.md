# MainAI Agent-Side Communication Bridge

Isolated successor of frozen `#303` @ `74ebda974ff6952c7c7ca15f837430a73c594b43`.
This is not a second messaging system. It sits on the `#302`/`#303` durable
gateway, owner-scoped ledger, Founder-issued tokens and delivery receipts.

`first_cycle_go` stays false. Cursor/Claude/Codex vendor sessions are not claimed.

## Capability matrix (inspected, not assumed)

| Surface | Cursor | Claude Code | Codex | Honest status |
|---|---|---|---|---|
| Local installed CLI | `cursor-agent` registered, not on this PATH | `claude` registered, not on this PATH | `codex` registered, not on this PATH | Execution only after Founder enablement. **Not** messaging. **Not** a private session. |
| Authenticated agent-side runner | Founder token `recipient=cursor` | Founder token `recipient=claude` | Founder token `recipient=codex` | **First verified path.** `python -m app.agent_coordination.agent_side` |
| Provider model API | not invoked | not invoked | not invoked | BLOCKED `provider_model_api` (no paid calls) |
| Remote/background coding agent | no authorized API | no authorized API | no authorized API | BLOCKED `remote_background_agent` |
| Existing private session | `CURSOR_AGENT_SOCKET` is this runtime, not a delivery API | none | none | BLOCKED `vendor_private_session` |

A local CLI binary, if later Founder-enabled, is still not a vendor session.

## Architecture

`AgentTransport` describes deliver / acknowledge / execute / cancel / ingest.
Generic receive logic keys adapters by `transport_id`. Vendor names appear only
inside isolated adapters and the inventory.

- `authorized_gateway_runner` — first usable path
- `local_cli_execution` — Founder-gated subprocess, no message interpolation
- `vendor_session_{identity}` / `provider_model_api` / `remote_background_agent` — fail closed

## Founder activation

1. `POST /api/mainai/founder-ops/agent-gateway/bridges` with a recipient
2. `LIFE_AGENT_BRIDGE_TOKEN=... python -m app.agent_coordination.agent_side`
3. Optional allowlisted executor: `LIFE_AGENT_SIDE_EXECUTOR=allowlisted` plus the existing `LIFE_AGENT_ADAPTER_*` flags
4. Default executor is `record`: structured evidence, no subprocess

## Remaining Founder / vendor requirements

- Cursor / Claude / Codex **vendor messaging and private sessions**: issue vendor webhooks or an authorized session API. Do not use personal OAuth.
- Paid model APIs: out of scope
- GitHub publication: Codex-owned
- First-cycle GO: false
