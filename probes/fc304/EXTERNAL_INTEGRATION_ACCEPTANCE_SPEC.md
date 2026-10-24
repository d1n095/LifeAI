# Engineering Acceptance Spec — genuine MainAI ↔ external coding-agent integration

Independent technical analysis (examiner-owned; not Cursor's implementation). Defines what is still required for MainAI
to communicate with actual Cursor / Claude Code / Codex environments **without Founder message forwarding**, grounded in
the code as of #304 candidate `859f5ec`. It does **not** assume private vendor sessions expose undocumented APIs.

## 1. Supported integration surfaces (observed, with honest state)
| Surface | What it is | State @ 859f5ec |
|---|---|---|
| `agent_side_runner` (authorized gateway runner) | a process that pulls the owner's inbox over the gateway with a Founder-issued scoped token (`python -m app.agent_coordination.agent_side`) | **usable** (token-authenticated); the only live messaging path |
| `local_cli` | Founder-enabled local CLI execution of a provider binary | fail-closed until ENABLED + CREDENTIALS_CONFIRMED + ARGS template |
| `provider_model_api` | paid chat/completion model APIs | **BLOCKED / out of scope** (no paid calls) |
| `remote_background_agent` | a hosted vendor coding-agent API MainAI could call | **BLOCKED** — none authorized/available |
| `private_vendor_session_{cursor,claude,codex}` | the agent's own IDE/editor session | **BLOCKED** — no documented push API; must not be assumed |
| `github_publication` | write path | **BLOCKED** — Codex-owned |

## 2. Authenticated agent-side registration (the key missing piece)
Today the runner authenticates by **token possession** only: whoever holds the Founder-issued bridge token for recipient R is treated as R. That is sufficient to *operate* the gateway but does **not** prove the token holder is the real external agent. Requirement for genuine integration:
- A **registration handshake** binding a runner instance to a verified agent identity: the agent presents the Founder-issued token **plus** an agent-attested proof (e.g. a per-agent signing key the Founder registers out of band, or a mutually-authenticated channel), and the gateway records a `registered_runner` row with that identity, first-seen, and key fingerprint.
- Registration must be **revocable and scoped** (reuse the existing bridge revoke + TTL) and **owner-isolated** (reuse the existing RLS + recipient scoping — already PASS in RAI-07/CS-6).
- Acceptance check: a token **without** a matching registered agent proof cannot acknowledge/complete on behalf of that identity (today only cross-identity receipts are rejected; cross-identity *possession* of a valid token is not distinguished).

## 3. How MainAI verifies the actual agent received an assignment
Today: a message reaches `delivered` when *a* token holder claims it — not proof the real agent received it. Requirement:
- The registered runner returns a **receipt signed with its registered key** over `(message_id, obligation_id, candidate_sha)`; the gateway verifies the signature against the registered identity before marking `delivered`.
- `delivered` must remain distinct from `prepared/available` (already enforced) **and** from "claimed by an unverified token" (new).
- Acceptance check (flips RAI-01): a `delivered` state exists only with a verified-signature receipt from the registered agent; absent that, the message stays `available` (not silently delivered).

## 4. How authorized work is executed
Already sound and least-privilege (keep as regression lock): the `RestrictedAssignmentExecutor` `record` mode persists evidence only; `allowlisted` mode runs a **Founder-supplied argv template** with no message fields interpolated, restricted env (allowlisted names), bounded timeout, `write_scope=none` by default. Requirement for real execution:
- Any **write** scope must require an explicit, Founder-approved, per-assignment branch+worktree (the dispatch gate already enforces this — RAI-19) and must never be grantable from message content (already enforced — ADV-5).
- Real coding-agent execution happens **inside the agent's own environment**, not MainAI's; MainAI must treat the returned diff/PR as a proposal subject to independent examination, never auto-certified.

## 5. How completion evidence returns to MainAI
Today: a completion carries a self-asserted evidence dict (not independently verifiable). Requirement (flips RAI-03/RAI-27):
- Completion evidence must be **request-bound and independently checkable**: e.g. a commit SHA on the assignment's branch that MainAI confirms via the existing read-only GitHub intake (`classify_required_ci` / head-SHA ancestry), not merely an agent assertion.
- The gateway must bind the receipt to the exact requested `message_id`+`obligation_id` (already rejects foreign ids — ADV-7) **and** reject a completion whose evidence does not correspond to the requested work (new: semantic/commit correspondence, currently NOT_IMPLEMENTED).
- Independent certification remains a **non-builder examiner** verdict; green CI / self-receipt / consensus never suffice (already enforced — CR-11/16, RAI-24).

## 6. Distinguishing a real coding-agent session from an ordinary local runner
This is the crux and is **not** solvable by the runner alone. A local runner holding a token is indistinguishable from a real agent unless:
- the runner is **registered with a verified agent identity** (§2), and
- it demonstrates a capability only the real agent has (e.g. producing a commit in the agent's authenticated vendor workspace that MainAI verifies read-only via GitHub) — i.e. proof-of-work in the agent's own environment, observed through an independent channel.
Until both hold, MainAI must classify the surface as `agent_side_runner`, **never** as a vendor session (the code already does this — AT-2).

## 7. Which paths require Founder setup vs vendor support
| Requirement | Needs |
|---|---|
| Agent-side runner operation | **Founder setup** — issue + securely hand a scoped token to each agent's runner |
| Local CLI execution | **Founder setup** — ENABLED + CREDENTIALS_CONFIRMED + ARGS, binary on PATH |
| Registered-identity handshake + signed receipts | **Cursor/MainAI engineering** (new) + **Founder** out-of-band key registration |
| Request-bound verifiable completion | **Cursor/MainAI engineering** (reuse read-only GitHub intake) |
| Private vendor session / remote agent API | **Vendor support** — a documented push/callback API; **do not assume one exists** |
| GitHub publication | **Codex** — publication authority |
| STOP-clear / audit immutability | **Codex** — FA-9/CO-10, FA-PRIV |

## 8. Smallest genuinely usable integration path (recommended first target)
**The agent-side runner over the gateway, hardened with registration + verifiable completion — no vendor API required.**
1. Founder issues a scoped bridge token per agent and hands it to a runner the agent operates (Founder setup; works today, cold-start now persists after CF-1).
2. Add the **registered-identity handshake + signed receipts** (§2/§3) so "delivered" means the *verified* agent received it — the single most important gap, and it is buildable without any vendor API.
3. Bind completion to a **read-only-verifiable commit** on the assignment branch (§5), reusing the existing GitHub intake.
4. Keep execution least-privilege (§4) and independent examination mandatory (§5).
This path depends only on Founder setup + MainAI/Cursor engineering, **not** on undocumented vendor session APIs. It turns RAI-01/03/22/27 from BLOCKED/PARTIAL to PASS while preserving the current PASS regression lock. Private vendor sessions and remote agent APIs remain a separate, vendor-gated future surface.

## Acceptance gate for the next integration candidate
When an integration candidate is submitted, it must: (a) flip RAI-01 (verified external receipt), RAI-03/27 (request-bound verifiable completion), RAI-13 (reconnection), RAI-22 (real loop legs) to PASS; (b) keep the RAI PASS set + `agentbridge` AT/ADV/CS set green (including CS-3 once CF-2 is fixed); (c) leave Codex FA-9/CO-10 + FA-PRIV to Codex; (d) `first_cycle_go=false`; (e) never classify a local runner as a vendor session.
