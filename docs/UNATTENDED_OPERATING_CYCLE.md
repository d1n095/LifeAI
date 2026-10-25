# Unattended operating cycle — verdict, first slice, 72-hour trial

Isolated on `#307` / `cursor/external-agent-connectivity-91c1`, child of frozen
`#303` @ `74ebda974ff6952c7c7ca15f837430a73c594b43`. Does not amend `#303`–`#306`.

**Capability verdict: NOT READY.**

Local gateway function, green CI and this disconnected API prototype are not
operational independence. Success is completed, independently verifiable work
without Founder babysitting — not the number of components implemented.

`first_cycle_go=false`. Unattended autonomy is not marked ready.

## Honest verdict

| Claim someone might make | Honest status |
|---|---|
| `#302`/`#303` local gateway works | True for Founder-issued tokens. **Not** a connected external agent. |
| Required CI is green | Insufficient even if true. This `#303`-based branch still inherits the `#305` zod audit blocker. Do not suppress it here. |
| Cursor Cloud Agents client exists | True as a fail-closed prototype. **No live create, receipt or completion.** |
| MainAI can run unattended | **False.** |
| 72-hour trial may start | **False.** |

Machine-readable copy: `capability_verdict()` in
`app.agent_coordination.external_connectivity.verdict`.

Inspect: `python -m app.agent_coordination.external_connectivity`

## Milestone requirements (1–10)

None are satisfied for a live external cycle. Local building blocks may exist;
they do not close the milestone.

1. **Discover and prioritize authorized work** — durable ledger + GitHub read-only intake. No connected dispatch target.
2. **Dispatch to genuinely connected external agents** — selected adapter is Cursor Cloud Agents API v1; live mutate is hard-blocked.
3. **Verify provider-side receipt and execution identity** — identity classes exist; no provider-authenticated receipt in this runtime.
4. **Collect independently verifiable completion evidence** — HMAC/GET contracts exist; no live FINISHED/ERROR evidence.
5. **Route completed implementations to independent examination** — examiner obligations can be prepared locally; nothing external completed to route.
6. **Process findings and dispatch authorized corrections** — would reuse the same fail-closed adapter. Not live.
7. **Observe exact-SHA CI and coordinate certification** — public GitHub intake can observe checks; certification stays examiner-owned.
8. **Recover from restarts, duplicates, expired credentials, temporary failures** — local ledger reconcile exists; provider-side retry/revocation is not authorized.
9. **Detect stuck agents and escalate only when needed** — no live provider run to watch. Escalation must not invent progress.
10. **Report progress without routine supervision requests** — Founder-ops can show local state; the external cycle is not running.

Architecture constraints that remain in force:

- No mandatory single-vendor dependency. Orchestration stays provider-neutral; adapters are replaceable.
- External agents cannot redefine MainAI policies or authority.
- No fabricated vendor receipt, execution or success.
- Durable, idempotent, auditable operations; bounded retries.
- Founder-defined budgets, permissions, expiry and revocation.
- Codex-owned STOP, privilege, FA-9, CO-10 and publication gates stay authoritative.

## Smallest end-to-end implementation (one provider)

Do **not** add Claude Code or Codex integrations before this slice works.

Provider: **Cursor Cloud Agents API v1** only.

1. Founder setup (off-chat): scoped user or service-account API key, spend bound, expiry, revocation. Not `LIFE_AGENT_BRIDGE_TOKEN`.
2. Discover one authorized `AgentWorkAssignment` on the existing ledger.
3. `POST /v1/agents` only after explicit live-mutate authorization.
4. `GET /v1/agents/{id}` with the same provider key → receipt + provider identity.
5. Completion: `GET` run terminal state and/or verified v0 HMAC `statusChange`.
6. Bind `agent.id` / `run.id` onto the existing delivery + durable ledger. Do not self-assert `independently_verified`.
7. Create one examiner obligation for a distinct examiner identity.
8. Observe exact-SHA CI through existing GitHub read-only intake.
9. High-risk actions (publication, merge, deploy, Recall, GO) stay blocked.

A fixture HMAC or a credential-free 401 is **not** this slice.

After `#306` disposition, compose the fail-closed client into the certified
AgentTransport registry. Do not copy uncertified `#306` code onto this branch.

## 72-hour unattended trial — acceptance plan

The trial **cannot start** while any of these remain:

- no live provider-authenticated external agent
- vertical slice incomplete
- FA-9 / CO-10 privilege and publication gates unsatisfied
- independent certification blocked
- Founder live-mutate unauthorized
- `first_cycle_go=false`

When — and only when — those gates are independently satisfied and separately
authorized, a 72-hour trial must prove:

| Scenario | Pass condition |
|---|---|
| Multiple assignments | ≥2 distinct authorized assignments on the same Cursor Cloud Agents path |
| Independent review | Each completion routed to an examiner ≠ builder |
| Authorized corrections | ≥1 examiner finding becomes a follow-up run under the same key and ledger binding |
| Process restart | Mid-cycle worker restart resumes durable state; no duplicate vendor create |
| Duplicate events | Replayed webhook/GET is idempotent |
| Expired credentials | Revoked/expired key fails closed and escalates once; no fabricated receipt |
| Temporary failure | Bounded retry on 5xx/timeout; no paid replan storm |
| Stuck agent | Escalate only after automated recovery and existing authority are insufficient |
| Founder reporting | Progress and exceptions on Founder-ops with **zero** routine message forwarding |

Restricted or high-risk actions stay blocked pending appropriate authorization.
Zero required routine Founder message forwarding during the trial.

Do not mark unattended autonomy ready while FA-9/CO-10, publication controls or
independent certification remain blocked.

## Next engineering steps

1. Close `#307` as a **feasibility + honest NOT READY verdict**, not as independence.
2. Leave `#306` unfrozen for Claude. Do not amend `#303`–`#306`.
3. After Founder live-mutate authorization, implement only the Cursor Cloud Agents vertical slice.
4. Keep Claude Code and Codex BLOCKED until that slice produces independently verifiable work.
5. Do not start the 72-hour trial, merge, deploy, or set `first_cycle_go=true` from this PR.
