# FINAL exact-SHA verdict — PR #303 (close DEF-1 / operational handover)

- **Frozen product SHA:** `74ebda974ff6952c7c7ca15f837430a73c594b43` (Cursor-confirmed frozen). Base: frozen #302 `ff28b51`.
- **CI (user-reported, independently unre-readable here — no GitHub credentials):** push `39936417788` 12/12, pull request `39936424245` 12/12. A later docs-only commit `296de97` (branch-registry) does not change the frozen product code; this verdict is pinned to `74ebda9` and is unaffected. (I could not fetch `296de97` in this environment; immaterial to a product-SHA-pinned verdict.)
- **Examiner:** Claude, read-only; real app/worker/DB/gateway/bridge; no paid calls, no GitHub writes, no production change. Worktree verified unchanged after every suite.

## A. Exact-SHA code verdict: **PASS / FREEZE**
DEF-1 is closed and the superseding `ec0f4d2→74ebda9` regression (existing real assignments wrongly retagged as synthetic ledger backing) is fixed. No new defect found. Evidence on the frozen SHA:

| Check | Result @ 74ebda9 | Meaning |
|---|---|---|
| **GH-4** cold-start idempotency (no prior assignment) | **PASS** `idempotent=True` (run1 ids == run2 ids) | DEF-1 closed — the exact probe that failed on #300/#301/#302 now passes |
| **GH-4b** two obligations survive restart | **PASS** `created=2 survived_restart=2 missing=set()` | builder+examiner both durable |
| gh_intake_v1 overall | **8 passed / 0 failed** | read-only, Founder-only, stale-CI handled, BLOCKED on rate-limit |
| **REG** existing real assignment never retagged as ledger backing | **PASS** `existing_is_backing=False backing_is_existing=True` | the frozen-SHA regression fix confirmed; `reconcile_owner` no longer skips a real assignment's events |
| **CONN** authorized receiver (`connect_v1`) | **5 passed** | real gateway round trip; local CLI fail-closed; durable; no double-exec |
| lifecycle / delivery / handover / live_handover | 7 / 6 / 12 / 9 passed | coordination + delivery regressions clean |
| substrate (SUB-01..20) / security_audit (SEC-1..4) | 20 / 4 passed | substrate invariants + security controls intact |

## B. Internal coordination & durable delivery: **PASS**
Durable, idempotent, recipient-scoped, restart-safe — including cold-start cognition with no prior assignment (the owner-scoped, read-only, write-incapable backing row). The authorized receiver completes a genuine issue→transmit→pull→ack→execute→complete round trip and survives a fresh session with duplicate completion `replay_ignored`.

## C. Corrected Founder CR acceptance contract: **PASS with documented gaps** (`cr_contract_v1`: 19 passed, 2 skipped)
16 CR PASS + REG; 3 PARTIAL (CR-06, CR-15, CR-17 — deterministic guard enforced, conversational/Codex half BLOCKED); 2 NOT_IMPLEMENTED (CR-14 answer-the-question, CR-19 real-artifact vs false local-access claim — skipped, never faked). Full matrix: `probes/cr_contract_v1/CR_ACCEPTANCE_CONTRACT.md`. Substrate (SUB-01..20) cited as supporting evidence only.

## Separate BLOCKED classifications (preserved, NOT part of the code PASS)
- **Real Cursor/Claude/Codex vendor communication: BLOCKED.** `agent_messaging_adapter` missing; vendor messaging `usable=False`. A working local bridge/CLI is not a connected vendor agent. Reaching the three real agents needs an agent-side bridge/CLI runner + a Founder-issued scoped token per agent (Founder-only). No vendor API exists; not invented.
- **Codex Security Kernel: BLOCKED (unchanged).** FA-9/CO-10 (STOP-clear without authenticated Founder reauth) — `coord_accept_v1` CO-10 FAIL (1 failed/10 passed). FA-PRIV (`mainai_app` over security-critical tables) — `fa_core_v1` 5 failed. #303 correctly does not touch these; they gate unattended autonomy, not this code verdict.

## Scope summary (combined release gate, `audit_302/gate.sh` on 74ebda9)
Scope A (first Founder-supervised safe cycle) **PASS** — 10 must-pass suites. Scope B (unattended autonomy) **BLOCKED** — Codex FA-9/CO-10 + FA-PRIV. Scope C (vendor activation) **BLOCKED** — no vendor adapter. `first_cycle_go=false` unchanged.

## Verdict
**#303 @ `74ebda9`: PASS / FREEZE.** The PR delivers exactly what it claims — DEF-1 closed, cold-start coordination durable and idempotent, the retag regression fixed — with no new defect and all regressions green. Vendor communication and Codex security work remain separately BLOCKED and are not prerequisites for freezing this PR; they gate later autonomy/activation scopes.

## Reproduce (all on the frozen SHA)
```
probes/gh_intake_v1/run.sh    ../wtcert-74ebda9   # 8 passed — GH-4 idempotent=True, GH-4b 2/2 (DEF-1 closed)
probes/connect_v1/run.sh      ../wtcert-74ebda9   # 5 passed — CONN authorized receiver
probes/cr_contract_v1/run.sh  ../wtcert-74ebda9   # 19 passed, 2 skipped — CR-01..20 + REG
probes/substrate_v1/run.sh    ../wtcert-74ebda9   # 20 passed — SUB-01..20
probes/lifecycle_v1/run.sh    ../wtcert-74ebda9   # 7 passed
probes/delivery_v1/run.sh     ../wtcert-74ebda9   # 6 passed
probes/handover_v1/run.sh     ../wtcert-74ebda9   # 12 passed
probes/live_handover_v1/run.sh ../wtcert-74ebda9  # 9 passed
probes/security_audit_v1/run.sh ../wtcert-74ebda9 # 4 passed
probes/coord_accept_v1/run.sh ../wtcert-74ebda9   # CO-10 FAIL = preserved (Codex)
probes/fa_core_v1/run.sh      ../wtcert-74ebda9   # FAC-1..5 FAIL = preserved FA-PRIV (Codex)
probes/audit_302/gate.sh      ../wtcert-74ebda9   # Scope A PASS / B BLOCKED / C BLOCKED
```
