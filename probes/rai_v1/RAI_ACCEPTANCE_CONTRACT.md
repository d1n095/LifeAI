# Real Agent Integration — Security & Reliability Acceptance Contract (RAI-01..RAI-27)

- **Purpose:** a production-grade, adversarial standard that decides whether MainAI is GENUINELY communicating two-way with an authorized **external** coding agent (Cursor / Claude Code / Codex), rather than running a local CLI, writing gateway records, or simulating delivery. Established **now**, against the certified reference, so the bar is fixed before the first real integration (#304) is submitted.
- **Certified reference SHA:** frozen #303 `74ebda974ff6952c7c7ca15f837430a73c594b43`.
- **Result on the reference:** `rai_v1` **24 passed, 3 skipped (BLOCKED)**. Examiner-owned; no production code; read-only vs target; credential-free; no paid calls; no external-agent activation; `first_cycle_go=false`.
- **Governing rule:** an unavailable vendor integration is **never** PASS because its local substrate passes. Each BLOCKED area still carries an executable NEGATIVE control proving the system does not FAKE the missing capability today.

## Classification key
**PASS** authentic evidence on the current substrate (+ negative control) · **PARTIAL** substrate guard PASSES, external/model/Codex half BLOCKED · **BLOCKED** integration unavailable on #303 (needs #304 / real agent-side env) · **NOT_IMPLEMENTED** no production code path.

## Coverage matrix
| RAI | Requirement | Class @ 74ebda9 | Evidence / why |
|----|----|----|----|
| **Area 1 — authentic external delivery** |
| RAI-01 | Prove the intended external recipient actually received the assignment | **BLOCKED** | no authenticated external-receive path; only the Founder-issued local bridge pulls the owner's OWN inbox. Needs an agent-side runner authenticated as that agent (#304). |
| RAI-02 | Distinguish local gateway delivery from authenticated external execution | **PASS** | `vendor_delivery_claimed=False` always; vendor messaging `usable=False`; a local claim is never reported as vendor delivery |
| RAI-03 | Require verifiable completion evidence | **PARTIAL** | empty-evidence execution `rejected` (PASS); evidence is self-asserted by the token holder — independent verifiability of external execution BLOCKED |
| RAI-04 | Reject fabricated acknowledgements / unsupported capability claims | **PASS** | spoofed-identity ack `forged_rejected`; `delivery_claim` → `delivery_claimed=False` |
| **Area 2 — identity & authorization** |
| RAI-05 | Recipient identity binding | **PASS** | receipt with mismatched `agent_identity` → `forged_rejected` |
| RAI-06 | Scoped & revocable credentials | **PASS** | HMAC token resolves before, not after revoke; non-founder issue `forged_rejected` |
| RAI-07 | Cross-agent & cross-owner isolation | **PASS** | cross-agent claim `forged_rejected`; owner-2 token sees 0 of owner-1's messages |
| RAI-08 | No privilege escalation through messages | **PASS** | `independently_verified`/`github_write`/`override_stop`/`stop` all `forged_rejected` |
| RAI-09 | Founder approval for sensitive operations | **PASS** | unauth bridge-issue HTTP → 401; dispatch gate → APPROVAL_REQUIRED |
| **Area 3 — durability & correctness** |
| RAI-10 | Restart recovery | **PASS** | completed receiver round trip survives a fresh sessionmaker session |
| RAI-11 | Idempotent delivery & execution | **PASS** | duplicate completion across restart → `replay_ignored` |
| RAI-12 | Duplicate & delayed receipts | **PASS** | delayed unknown → `stale_rejected`; duplicate event_id → `replay_ignored` |
| RAI-13 | Network interruption & reconnection | **BLOCKED** | no real external transport; disconnection modeled only as "no live bridge → blocked". Needs #304/real agent |
| RAI-14 | Cancellation & timeout | **PARTIAL** | message expiry → `failed` (PASS); real external cancel/timeout to a vendor agent BLOCKED |
| RAI-15 | Race / out-of-order events | **PASS** | status-rank merge keeps the advanced status; no regression on reload |
| **Area 4 — security** |
| RAI-16 | Prompt-injection & malicious payload | **PARTIAL** | structured policy-override `forged_rejected` (PASS); free-text LLM injection resistance BLOCKED (model-side) |
| RAI-17 | Message-content isolation from shell commands | **PASS** | CLI argv comes only from Founder `adapter_config`; message content never enters argv; fail-closed without a template |
| RAI-18 | Credential & env-var leakage | **PASS** | adapter env forwards only a Founder-allowlisted set of NAMES; a canary secret does not leak |
| RAI-19 | Unauthorized filesystem / repo access | **PARTIAL** | read_write dispatch needs explicit branch+worktree + approval (PASS); a real external agent's own FS access is outside MainAI (BLOCKED) |
| RAI-20 | Unauthorized spending / publishing / STOP-clear | **PARTIAL** | publishing BLOCKED + coordination can't clear STOP (PASS); authenticated-reauth STOP-clear = Codex CO-10 (BLOCKED) |
| RAI-21 | Audit integrity & least-privilege | **BLOCKED (Codex FA-PRIV)** | founder-comms ledger append-only, but `audit_log` runtime-writable by `mainai_app` (FA-PRIV open; `fa_core_v1` FAC-1..5 FAIL) |
| **Area 5 — multi-agent coordination** |
| RAI-22 | MainAI → builder → examiner → MainAI | **PARTIAL** | the internal obligation loop (≤1 examiner) is enforced (PASS); the external receive/execute legs BLOCKED |
| RAI-23 | Prevent duplicate assignments | **PASS** | second canonical-work assignment raises `DuplicateWorkError` |
| RAI-24 | Builder cannot certify its own work | **PASS** | builder completion → `independently_verified` not set |
| RAI-25 | Preserve ownership / chronology / evidence | **PASS** | cross-agent completion on another's assignment → `forged_rejected` |
| RAI-26 | Handle unavailable agents without inventing results | **PASS** | no live bridge → `blocked`, `vendor_delivery_claimed=False`, never a fabricated completion |
| RAI-27 | External response corresponds to the requested assignment | **PARTIAL** | receipt for a foreign `message_id` → `forged_rejected` (binding by id, PASS); semantic request/response correspondence NOT_IMPLEMENTED |

**Tally:** 15 PASS · 6 PARTIAL · 3 BLOCKED (RAI-01, RAI-13, RAI-21) · correspondence/verifiability gaps noted NOT_IMPLEMENTED within PARTIALs.

## Capability & evidence matrix — Cursor / Claude Code / Codex (today, on #303)
| Capability | Cursor | Claude Code | Codex | Evidence |
|----|----|----|----|----|
| Local authorized bridge inbox (owner-scoped) | available (Founder token) | available | available | `transport_inventory.local_authorized_bridge usable=True` once a credential exists |
| Local CLI execution (Founder-enabled) | fail-closed until ENABLED+CREDS+ARGS | same | same | `adapter_config` five-way distinction; RAI-17 |
| **Authenticated external vendor messaging** | **BLOCKED** | **BLOCKED** | **BLOCKED** | `*_vendor_messaging usable=False`, `missing_capability=agent_messaging_adapter` |
| Proof the real agent received/executed | **BLOCKED** | **BLOCKED** | **BLOCKED** | RAI-01 / RAI-03 — no authenticated external receipt |
| GitHub publication authority | n/a | n/a | **BLOCKED (Codex-owned)** | `github_publication usable=False`, `codex_github_publication_authority` |

No vendor API is assumed to exist for any of the three. A working local bridge/CLI is explicitly NOT evidence of a connected vendor agent.

## Criteria that would prove genuine reduction of Founder manual forwarding
MainAI replaces manual forwarding to a given agent **only when all hold**, each independently verifiable:
1. The agent runs an **agent-side runner** authenticated as that identity, and MainAI records an **authenticated external receipt** distinct from a local pull (flips RAI-01 → PASS).
2. A completion carries **independently verifiable** evidence (not a self-asserted dict) tied to the requested assignment (flips RAI-03 / RAI-27 → PASS).
3. The full loop MainAI → builder → **independent examiner** → MainAI completes with the external legs real (flips RAI-22 → PASS), builder never self-certifies (RAI-24 holds).
4. Unavailable agents degrade to BLOCKED with no invented result (RAI-26 holds), and restart/idempotency hold across the real transport (RAI-10/11/13).
Until #1–#4, the honest state is: MainAI automates coordination for any agent operating the authorized bridge/CLI, but **cannot yet prove two-way communication with the real vendor agents** — the Founder still forwards to them.

## Security release conditions (for the future composed release WITH real integration)
- **MUST (flip from BLOCKED/PARTIAL to PASS before external activation):** RAI-01 authenticated external receipt; RAI-03/RAI-27 verifiable, request-bound completion evidence; RAI-13 real reconnection; RAI-22 real loop legs.
- **MUST remain enforced (regression-lock):** RAI-02/04/05/06/07/08/09/10/11/12/15/17/18/23/24/25/26 (the PASS set).
- **Codex-owned prerequisites (separate, still BLOCKED):** FA-9/CO-10 (authenticated STOP-clear) and FA-PRIV / RAI-21 (audit immutability + least privilege) — gate unattended autonomy and audit integrity; not bypassable by this gate.
- **Boundaries:** no unauthorized spend/publish/STOP-clear; `first_cycle_go=false`; no self-certification.

## Remaining integration blockers (summary)
| Blocker | Owner | Flips |
|----|----|----|
| Agent-side runner + authenticated external receipt | Cursor/MainAI (#304) | RAI-01, RAI-22 |
| Independently verifiable, request-bound completion evidence | Cursor/MainAI (#304) | RAI-03, RAI-27 |
| Real transport reconnection semantics | Cursor/MainAI (#304) | RAI-13 |
| Vendor messaging adapter (or accept bridge-runner as the transport) | Founder decision + Cursor | vendor rows in the capability matrix |
| FA-9/CO-10 authenticated STOP-clear; FA-PRIV audit least-privilege | Codex | RAI-20 (reauth half), RAI-21 |

## Reproduce
```
probes/rai_v1/run.sh ../wtcert-74ebda9   # 24 passed, 3 skipped (BLOCKED: RAI-01, RAI-13, RAI-21)
```
When #304 (or an authorized real agent-side environment) is submitted, re-run this suite on its frozen SHA: the BLOCKED/PARTIAL rows become the acceptance checklist, and the PASS set is the regression lock. This contract is the examiner standard; it does not certify any real integration until that evidence exists.
