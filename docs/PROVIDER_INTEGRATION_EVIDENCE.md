# Provider integration, durable evidence, limitation transparency

Isolated composition of frozen `#306` product @ `2444bcc3da8ffa5ca10ef3295bac13aa045769c6`
and `#307` connectivity work. **Not certified.** `#306` freeze handoff is `#308`.
Claude owns final certification of `#306`. This PR does not amend `#303`–`#307`.

`first_cycle_go=false`. No live vendor connectivity is claimed.

## What is composed

- `#306` agent-side bridge, CF-1/CF-2, zod `4.6.7`, audit policy unchanged
- `#307` official-interface catalog, identity classes, fail-closed Cursor Cloud client
- New: authenticated provider-event ingestion on the **existing** ledger
- New: provider-neutral restriction records on the **same** ledger

No second inbox. No second memory system. No new Alembic revision.

## Ingestion contract

`authenticated provider event → assignment identity verification → durable ingestion → trusted lifecycle transition → independent review`

Implemented in `app.agent_coordination.provider_ingestion`:

1. Bind `provider + external_agent_id + assignment_id + owner_id`.
2. Verify official v0 HMAC before acceptance.
3. Reject forged, replayed, reordered, expired and cross-owner events.
4. Persist bindings/events via `persist_owner_ledger` (survives restart).
5. FINISHED → `completed` + examiner obligation. `independently_verified` stays false.
6. ERROR → `failed`.
7. UNKNOWN / BLOCKED → `blocked`, never verified completion.
8. Live paid mutate remains fail-closed.

Fixture HMAC is cryptography, not Cursor delivery (`fixture=True` ⇒
`provider_authenticated=false`).

## Limitation transparency

`app.mainai_restrictions` is provider-neutral.

On a material limitation MainAI:

1. Names the restriction, source, affected task and confidence.
2. Classifies technical / contractual / legal / authorization / financial / safety / unknown.
3. Splits alternatives into supported, prohibited, uncertain.
4. Continues only authorized troubleshooting.
5. Pauses when a path would bypass, expand authority, violate terms, exceed spend, or is uncertain.
6. Surfaces the record on Founder-ops / `founder_view()`.
7. Stores a versioned, searchable, secret-redacted ledger record.
8. Reuses lessons with `authorization_still_valid=false`.

Providers cannot suppress records, rewrite rules, or mint Founder authorization.

## Remaining Founder actions for the first real round trip

1. Accept Claude's final certification of frozen `#306` @ `2444bcc`.
2. Issue a scoped Cursor user or service-account API key (off-chat).
3. Authorize live mutate separately (`LIFE_CURSOR_CLOUD_AGENTS_LIVE_MUTATE` is still hard-blocked).
4. Provision `LIFE_CURSOR_CLOUD_WEBHOOK_SECRET` and HTTPS ingress, or authorize GET-only inspect.
5. Bind the resulting `agent.id` to one existing assignment.
6. Keep publication / STOP / FA-9 / CO-10 with Codex.
7. Do not set `first_cycle_go`.

Until those happen, the verified local proof is fixture + fault injection only.
