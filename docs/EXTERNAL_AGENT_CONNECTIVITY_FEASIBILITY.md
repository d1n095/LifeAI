# Real External Agent Connectivity — feasibility and first path

Isolated child of frozen `#303` @ `74ebda974ff6952c7c7ca15f837430a73c594b43`.
This is **not** a successor of uncertified `#306` and does not amend `#303`–`#306`.

`first_cycle_go` stays false. No vendor delivery is claimed.

## Reused acceptance requirements

Claude's RAI / external-integration rules already govern this surface. This PR
does not mint a parallel RAI-01..27 document. It applies the same contract:

- Unavailable vendor capabilities stay **BLOCKED** with a named missing capability.
- A local CLI, a Founder HMAC bridge token, or `CURSOR_AGENT_SOCKET` is never a
  vendor session or provider-authenticated identity.
- Execution is never inferred from an instruction or a queue-empty claim
  (`docs/DURABLE_AGENT_COORDINATION.md`).
- `independently_verified` is examiner-owned. The builder must not self-assert it.
- `#302`/`#303` gateway + durable ledger remain the only inbox. This is not a
  second messaging subsystem.
- `#304` AgentTransport (on its own unfrozen branch) is the intended later
  adapter slot. This PR does not copy uncertified `#304`–`#306` product code.
- No unauthorized credentials, paid mutate, publication, merge, deploy, Recall,
  or autonomy activation.

## Feasibility matrix (official interfaces, this runtime)

| Provider | Official surface | Identity | Assignment → result | Independently checkable evidence | This runtime | Honest status |
|---|---|---|---|---|---|---|
| Cursor | Cloud Agents API v1 `https://api.cursor.com/v1/agents` (+ runs, artifacts, archive) | User or service-account API key (Basic/Bearer). **Not** a local bridge token. | `POST /v1/agents` creates agent+run; `POST /v1/agents/{id}/runs` follow-up; `GET` inspect | Provider GET of agent/run; optional artifacts/PR metadata | No Founder API key; live mutate hard-blocked | **Selected first path.** Architecture ready. **Not live.** |
| Cursor | Cloud Agents API v0 webhooks | `X-Webhook-Signature` HMAC-SHA256 over raw body | `statusChange` `FINISHED` / `ERROR` | HMAC + payload `id`/`status`/`target` | No Founder webhook secret or public ingress | Architecture ready. **Not live.** v1 webhooks "coming soon". |
| Cursor | Local `cursor-agent` CLI | Founder adapter flags + binary | Local subprocess only | Process exit / heartbeat | Binary not on PATH | Local execution ≠ vendor session. BLOCKED until Founder enablement. |
| Cursor | This Cloud Agent (`CURSOR_AGENT_SOCKET`) | This runtime | None — not a delivery API | n/a | Present | BLOCKED `not_a_delivery_api` |
| Cursor | Private vendor session | n/a | No official session-injection API authorized here | n/a | Absent | BLOCKED `agent_messaging_adapter` |
| Claude | Headless `claude -p` + Agent SDK | `ANTHROPIC_API_KEY` / OAuth / cloud providers (paid) | Host-owned local process. No official remote coding-agent create+webhook API | Process IO | `claude` not on PATH; no paid auth | BLOCKED |
| Claude | Private vendor session | n/a | None | n/a | Absent | BLOCKED |
| Codex | `codex exec` CLI | `CODEX_API_KEY` / ChatGPT login (paid) | Local process | Process events | `codex` not on PATH | BLOCKED |
| Codex | GitHub / Copilot coding agent | GitHub App — **Codex-owned writes** | Remote, but publication authority is not Cursor's | GitHub PR/checks | No grants | BLOCKED `codex_github_publication_authority` |
| Any | Provider model/chat API | Paid provider key | Not an assignment-to-result coding-agent path | n/a | Not invoked | BLOCKED `provider_model_api` |

Evidence for the official Cursor surface: [API overview](https://cursor.com/docs/api),
[Cloud Agents overview](https://cursor.com/docs/cloud-agent/api/overview),
[endpoints](https://cursor.com/docs/cloud-agent/api/endpoints),
[v0 webhooks](https://cursor.com/docs/cloud-agent/api/webhooks),
[service accounts](https://cursor.com/docs/account/enterprise/service-accounts).

Claude: [authentication](https://code.claude.com/docs/en/authentication),
[headless](https://code.claude.com/docs/en/headless),
[Agent SDK](https://code.claude.com/docs/en/agent-sdk/quickstart).

Codex: [CLI](https://learn.chatgpt.com/docs/codex/cli.md),
[auth](https://learn.chatgpt.com/docs/auth.md).

## Selected first genuine path

**Cursor Cloud Agents API v1**, with legacy v0 HMAC webhooks as the completion
channel until v1 webhooks ship.

Why this one, and not Claude or Codex:

1. It is the only official API that lets MainAI *create* a remote coding agent,
   *steer* it, and *read* run status under a provider-issued key.
2. Provider identity is a Cursor Dashboard / service-account key — a different
   class from `LIFE_AGENT_BRIDGE_TOKEN`.
3. Completion can be checked without trusting the builder: GET the run, and/or
   verify `sha256=<hex>` on the raw webhook body.
4. Claude Code's official automation path is a local/host process plus paid
   Anthropic auth. Codex remote GitHub writes stay Codex-owned.

A live assignment-to-result round trip is **not** possible in this runtime.
That is reported as BLOCKED, not simulated.

## Identity

| Class | What it proves | What it does **not** prove |
|---|---|---|
| `local_bridge_token` | Founder issued a `#302`/`#303` HMAC token | Cursor/Claude/Codex account, remote delivery |
| `this_cloud_agent_runtime` | This Cloud Agent is running | Ability to deliver work to another agent |
| `cursor_cloud_api_key_candidate` | A secret *classified* as a Cursor API key was supplied | Founder authorization, spend approval, or a real create |
| `cursor_cloud_webhook_hmac` | Raw body matched the official HMAC | Examiner certification; fixture HMAC is not Cursor origin |
| `claude_code_provider_auth` / `codex_provider_auth` | Provider env *name* may exist | Usable paid path in this candidate |

`classify_credential()` rejects a `#303` bridge token and `CURSOR_AGENT_SOCKET`
when they are offered as a Cursor API key.

## Implementation architecture

```
Founder API key / webhook secret
        │
        ▼
CursorCloudAgentClient (fail-closed)
        │  never sends paid POST from this candidate
        ▼
verify_v0_webhook (official HMAC)
        │
        ▼
propose_ledger_event  ──►  existing delivery + DurableCoordinationService
                           (proposal only; persist is not invoked)
```

Reuse:

- Gateway / receipts: `app.agent_coordination.delivery`
- Local runner: `python -m app.agent_coordination.local_bridge` (Founder token)
- Ledger / persist: `DurableCoordinationService`, `persist_owner_ledger`
- Handover view: `operational_handover_view` now includes the inspect matrix
- Later (after `#306` disposition): register this client as an isolated
  `AgentTransport` on the certified `#304` lineage — do not copy `#306` here

## Prototype results (what this PR actually does)

Implemented and tested:

- Official catalog + selected-path justification
- Identity classification (bridge token ≠ provider key ≠ this runtime)
- Fail-closed `create_agent` / `follow_up` (no paid HTTP)
- Official v0 webhook HMAC accept/reject
- Ledger *proposal* from verified evidence (`persisted=False`)
- `inspect_external_connectivity()` / `python -m app.agent_coordination.external_connectivity`
- Optional credential-free `GET https://api.cursor.com/v1/me` (401/403 = surface exists)

Not done, and not claimed:

- Creating a Cloud Agent
- Claude Code or Codex remote delivery
- `independently_verified=true`
- `vendor_delivery_claimed=true`
- Composition onto uncertified `#306`

## Remaining Founder authorization

1. Scoped Cursor user or Enterprise service-account API key (off-chat).
2. `LIFE_CURSOR_CLOUD_AGENTS_ENABLED=true` after spend review.
3. Separate live-mutate authorization — currently a hard block even if the enable flag is set.
4. HTTPS webhook ingress + `LIFE_CURSOR_CLOUD_WEBHOOK_SECRET`.
5. Bind `agent.id` / `run.id` to an existing `AgentWorkAssignment`.
6. Independent examiner for certification.
7. Claude/Codex remain BLOCKED until their own Founder-owned remote contract exists.

## Next engineering steps

1. Do not freeze or amend `#306` while Claude examines `2444bcc`.
2. After `#306` disposition, compose this client into the certified transport registry.
3. Founder-supervised one-shot create + GET (throwaway repo) — only with authorized spend.
4. Record provider ids on the existing ledger; keep vendor flags false until that evidence exists.
5. Keep Claude Code and Codex BLOCKED.

Inspect: `python -m app.agent_coordination.external_connectivity`
Optional surface probe: `python -m app.agent_coordination.external_connectivity --probe`
