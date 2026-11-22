# Direct OpenAI consultation (Responses API) — configuration & security contract

**Module:** `backend/app/openai_consult/` · **Status:** implemented, disabled by default,
awaiting founder authorization. This document is the operator-facing contract: what the
integration does, every gate a call passes, how to enable it deliberately, and what it
structurally cannot do.

## What it is

`POST /api/openai-consult` lets MainAI (the founder, authenticated like every other API
surface) consult an OpenAI model directly through the **official Responses API**
(`https://api.openai.com/v1/responses`) — no ChatGPT-website automation, no reused browser
sessions, no personal-account credentials. The only credential involved is a server-side
`OPENAI_API_KEY` environment secret, the same optional-until-configured key the existing
chat provider path already uses.

It is deliberately a **standalone adapter**, not a new entry in `app/providers/`' chat
fallback chain: the chat pipeline answers users inside conversations, while this surface is
a tightly budgeted consultation channel with its own stricter contract. Reusing the shared
gates (egress, credential hygiene, safe error classification) is intentional; sharing the
chat call path is not.

## Every gate a call passes, in order

| # | Gate | Where | Fail-closed behavior |
|---|------|-------|----------------------|
| 1 | Config | `service.resolve_gate()` | `OPENAI_CONSULT_ENABLED=false` (default), missing/placeholder key, or empty allowlist → `503` before any Redis/DB/network touch |
| 2 | Model | `service.resolve_model()` | exact allowlist match required; a model without a pricing entry in `app/providers/pricing.py` is refused (an unpriceable model is unbudgetable) |
| 3 | Egress | `app/egress_policy/service.enforce_egress_policy()` | default-deny; `NEVER_EGRESS` content denies the whole call; secret-pattern redaction runs before the request is built; one `ProviderDisclosureEvent` audit row per call |
| 4 | Budget | `budgets.DailyBudgetStore.reserve()` | worst-case reservation (estimated prompt + full `max_output_tokens`) per user per UTC day, before the network call; exhausted → `429` |
| 5 | HTTP | `service.consult_openai()` | hard httpx timeout; `store=False` default; at most the hosted read-only `web_search` tool; status mapped to safe categories; raw provider bodies never surfaced |
| 6 | Settle | `budgets.DailyBudgetStore.settle()` | real usage from the Responses API reconciles the reservation; a missing/invalid `usage` object fails the call and keeps the reservation (conservative) |

## Rate limits & budgets (requirement 4)

* **HTTP rate limit:** `OPENAI_CONSULT_REQUESTS_PER_MINUTE` (default 10/min/user) via the
  same slowapi limiter every other endpoint uses (Redis-backed when `REDIS_URL` is set).
* **Daily token budget:** `OPENAI_CONSULT_DAILY_TOKEN_BUDGET` (default 200 000/user/UTC-day),
  reserved pre-call, settled post-call.
* **Daily cost budget:** `OPENAI_CONSULT_DAILY_COST_BUDGET_USD` (default 5.00/user/UTC-day),
  computed from `app/providers/pricing.py`'s Decimal table — no invented numbers; an
  unknown model is denied at gate 2 instead of guessed at.
* Counters live in Redis when `REDIS_URL` is set (shared across replicas, survives
  restarts); without it they are process-local, the same documented single-process stance
  as `app/limiter.py`.

## Tools & structured responses (requirement 5)

* The **only** tool this module can ever attach is OpenAI's hosted, read-only
  `web_search`, and only when `OPENAI_CONSULT_WEB_SEARCH_ENABLED=true` **and** the request
  sets `allow_web_search=true`.
* Client-supplied tools are structurally impossible: the request model has no field that
  could carry them. There is no code path to shell execution, filesystem access, GitHub
  writes or code-interpreter/computer-use — not disabled, *absent*.
* Structured responses: pass `response_schema` (a JSON schema of `type: "object"`);
  the request sends Responses-API `text.format.json_schema` (strict), and the answer is
  parsed server-side. An answer that is not valid JSON fails the call (fail-closed) rather
  than passing unvalidated text to a caller expecting a schema.

## Credentials (requirement 3)

`OPENAI_API_KEY` is read from server-side environment only, passed only as the HTTP
Authorization header to `api.openai.com`. It never appears in: logs (call failures log the
exception *type* only), HTTP responses (`ConsultOut`/`ConsultStatusOut`/error details carry
no key, prefix, URL or raw provider body), the frontend, or Git. The placeholder-detection
from `app/providers/base.py` (`looks_like_placeholder_secret`) also applies — a template
leftover counts as "not configured".

## Enabling (requirement 6) — deliberate, operator-driven

```bash
# 1. Real key, server-side only (Render dashboard / VPS .env — never committed):
OPENAI_API_KEY=sk-...
# 2. Explicit allowlist (exact model ids; every model MUST have a pricing entry):
OPENAI_CONSULT_ALLOWED_MODELS=gpt-4o-mini,gpt-4o
# 3. Budgets/tuning (defaults are conservative; see .env.example for all knobs):
OPENAI_CONSULT_MAX_OUTPUT_TOKENS=1024
OPENAI_CONSULT_DAILY_TOKEN_BUDGET=200000
OPENAI_CONSULT_DAILY_COST_BUDGET_USD=5.00
# 4. Master switch — flips the integration on:
OPENAI_CONSULT_ENABLED=true
```

`GET /api/openai-consult/status` shows the effective configuration and the calling user's
remaining daily budget — it can never return any part of the key. **Do not flip
`OPENAI_CONSULT_ENABLED` on a shared deployment without the founder's explicit approval**
(this repository's standing constraint: the integration ships inactive,
`first_cycle_go=false`).

## Tests

`backend/tests/backend/test_openai_consult.py` — unit + security-regression tests with a
mocked Responses API (httpx.MockTransport, no network): every gate, redaction-before-send,
tool-structural refusals, error mapping, budget reserve/settle math and day rollover.
A live-API integration test exists but is skipped unless `OPENAI_CONSULT_LIVE_TEST=1` and
a real key are present (CI/sandbox has no outbound network by default).

## Deliberate non-goals / boundaries

* No database tables, no Alembic migration — budget state is ephemeral counters, so this
  module cannot conflict with parallel migration work on other branches (e.g. migration
  0073 / EGRESS-0 ownership).
* No integration with the MainAI agent spend kernel (`app/provider_spend/`) — that kernel
  grants spend against execution envelopes (agent goals); a founder consultation has no
  goal/envelope, and fabricating one would blur execution authority. If consultations are
  ever agent-initiated, they must acquire a real envelope first and THEN route through
  this module's gates unchanged.
* No conversation persistence — the module is request-scoped; what the founder does with
  the answer is the existing app's job.
