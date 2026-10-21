# Founder Authority / Security Kernel — independent audit

- **Audited SHA:** `27e945db8ee414c813248bbeb9e7462b93903cee` (#294, frozen; latest accessible).
- **Method:** executable adversarial suite `security_kernel_v1` (FA-1..FA-16 + FA-PRIV). Every tamper attempt runs as the **runtime database role `mainai_app`** — the identity of every web and worker code path, i.e. anything a prompt injection, a model-chosen tool call, or a code bug could drive. Authority rows are created through the legitimate Founder path (superuser service session). No production changes; no paid calls.
- **Outcome vocabulary:** PASS = enforced; FAIL = runtime can do it (unsafe); BLOCKED = capability genuinely missing (never counted as PASS).

## Headline

**The Security Kernel is enforced in the application layer only (`require_founder` at the HTTP boundary), not at the database privilege boundary.** The runtime role holds the privileges that let a model-driven path tamper with authority, identity and audit state. This directly contradicts Founder requirement 5 ("Security Kernel enforcement must remain independent of the model") and 4/6.

`FA-PRIV` privilege inventory at this SHA (role `mainai_app`):

| Table | RLS | Runtime holds | Must not hold |
|---|---|---|---|
| `users` | **none** | SELECT, INSERT, **UPDATE, DELETE** | UPDATE, DELETE |
| `audit_log` | **none** | SELECT, INSERT, **UPDATE, DELETE** | UPDATE, DELETE |
| `workforce_authority_epoch` (STOP) | **none** | SELECT, INSERT, **UPDATE, DELETE** | UPDATE, DELETE |
| `execution_authorization_envelopes` | forced | SELECT, INSERT, **UPDATE** | UPDATE, DELETE |
| `provider_spend_authorizations` | forced | SELECT, INSERT, **UPDATE** | UPDATE, DELETE |
| `mainai_goals` | forced | SELECT, INSERT, UPDATE, **DELETE** | DELETE |

RLS scopes by owner, but in a single-Founder deployment the owner **is** the Founder, so RLS does not stop the runtime from rewriting the Founder's own authority rows.

## Requirement-by-requirement

| Req | Capability | State | Evidence |
|---|---|---|---|
| 1 | Explain *why* an operation is restricted, naming the real enforceable rule | **PARTIAL** | FA-1b PASS: an active STOP is surfaced as a restriction. FA-1 FAIL: `_founder_authority()` returns `policy_transparent / sensitive_changes_require_founder_approval / independent_security_enforcement = True` **unconditionally** — asserted, not derived (and FA-4..FA-12 show the last one is false). Per-restriction `code`+`explain` for the safe-launch flags are correctly derived. |
| 2 | Founder can inspect **and modify** permissions, restrictions, AI behaviour, tool capabilities at runtime | **MISSING** | FA-2 BLOCKED: the only policy-write route is `PUT /api/admin/providers/config`. Permissions/restrictions/AI-behaviour/tool-capability are environment settings (`config.py`), not Founder-modifiable at runtime. |
| 2 | Step-up authentication for sensitive changes | **MISSING (except erasure)** | FA-3 BLOCKED: only `DELETE /api/account` uses a reauth receipt (`account/reauth.py`). Spend authorization, execution-scope widening, approvals and STOP-clear accept an ordinary live session. |
| 3 | Sensitive changes require explicit, scoped Founder approval | **PARTIAL** | Task approval works and is not replayable (FA-13 PASS) and not forgeable **through the service API**. But FA-5 FAIL: inserting an `approval_granted` event as the runtime role satisfies `require_task_approval`; FA-6 FAIL: the runtime role can set `approval_required=false` / change `mainai_goals.approval_policy` directly. |
| 4 | Never self-grant, forge approval, silently change policy, or lock out the Founder | **FAIL** | FA-5, FA-6, FA-7, FA-8, FA-9, FA-10, FA-12 all FAIL at the privilege level (see table). FA-4 FAIL for envelope/spend (INSERT held, no guard trigger); **mainai_verification_records is guarded** by a BEFORE INSERT trigger (good — this is why D1 receipts cannot be forged by the runtime). FA-11 PASS: a role-only `founder` impostor is still refused by id-pinning. |
| 5 | Kernel enforcement independent of the model; isolation/auth/external boundaries non-overridable | **FAIL** | The whole FA-PRIV table. Enforcement is `require_founder` in FastAPI, above the role that the model drives. |
| 6 | Provenance, exact scope, audit receipt, secure rollback for every privileged change | **PARTIAL** | Spend has `revoke` (FA-14 control PASS). FA-14 BLOCKED: an **authorized execution envelope has no revoke/rescind** path (only proposals can be rejected). FA-15 FAIL: `POST /founder-ops/stop` leaves **no `audit_log` receipt** (`record_audit` is not called). FA-12 FAIL: audit rows are rewritable/deletable by the runtime role. |
| 7 | Distinguish policy denial, technical failure, unavailable capability, external restriction, security incident | **PARTIAL** | FA-16: `ApprovalRequiredError`, `PathAEnvelopeError`, `KillSwitchError`, `ProviderError`, `ExternalBoundaryError` are distinct types. BLOCKED: there is **no distinct "genuine security incident" class** — tamper, a forged approval, or credential exposure are not type-distinguishable from an ordinary failure. |
| 8 | Founder identity/capabilities isolated from customer AI and data | **N/A for first cycle (single-tenant)** | No customer-AI subsystem exists in this lineage; `require_founder` pins the single `FOUNDER_USER_ID`. Re-audit when a second tenant is introduced; not a first-cycle blocker. |

## What is already sound (keep)

- Application-layer `require_founder` pins role **and** the fixed `FOUNDER_USER_ID` (FA-11).
- Task approval is not replayable across tasks (FA-13).
- STOP is surfaced as the enforcing rule when active (FA-1b).
- `mainai_verification_records` has a DB insert-guard trigger — independent-review receipts cannot be forged by the runtime (this is the D1 fix's real backstop).
- `founder_ack` for STOP-clear rejects fabricated "BOOT"/empty acks and enforces a format — a genuine control, though not a proof of authentication (FA-9).
- Distinct typed denials for the common classes (FA-16 partial).

## Prioritized remediation contract

**P1 — kernel independence (Req 4, 5). Security-critical; reported separately from first-cycle feature scope, but a hard prerequisite for "enforcement independent of the model".**
1. Enable RLS on `users`, `audit_log` and `workforce_authority_epoch`, and `REVOKE UPDATE, DELETE` on them from `mainai_app`. (FA-10, FA-12, FA-9 direct.)
2. `REVOKE UPDATE` on `execution_authorization_envelopes` and `provider_spend_authorizations` from `mainai_app`; route the legitimate state changes (consume, revoke, settle) through `SECURITY DEFINER` functions, as `mainai_verification_records` and the erasure children already do. (FA-7, FA-8.)
3. Make `audit_log` append-only for the runtime role (insert-only; a guard trigger rejecting UPDATE/DELETE), and have `POST /founder-ops/stop` and spend-revoke call `record_audit`. (FA-12, FA-15.)
4. Gate task approval on a durable, non-runtime-writable fact (e.g. a `SECURITY DEFINER` grant function or an approval row the runtime cannot INSERT), not on an `approval_granted` event the runtime can write. Protect `approval_required` / `approval_policy` the same way. (FA-5, FA-6.)

**P2 — provenance & taxonomy (Req 6, 7).**
5. Add a Founder revoke/rescind for an authorized execution envelope. (FA-14.)
6. Add a distinct `SecurityIncident` class/code, raised on tamper detection (guard-trigger violation, forged-approval attempt, credential exposure), separate from policy denial and technical failure. (FA-16.)

**P3 — transparency & configurability (Req 1, 2).**
7. Derive `policy_transparent` / `independent_security_enforcement` / `sensitive_changes_require_founder_approval` from actual probes (or remove them) rather than hard-coding `True`. (FA-1.)
8. Founder runtime APIs to read and change configurable permissions / AI-behaviour / tool capabilities, with step-up (reauth receipt) for sensitive changes, reusing the erasure reauth mechanism. (FA-2, FA-3.)

**Not a first-cycle blocker:** Req 8 (customer isolation) — no customer subsystem exists yet.

## Gate

The suite is the acceptance contract. A corrected candidate must turn every FA-* and FA-PRIV to PASS; `pytest.fail("BLOCKED: …")` marks a genuinely missing capability and is never a pass. Run: `probes/security_kernel_v1/run.sh <candidate-worktree>`.
