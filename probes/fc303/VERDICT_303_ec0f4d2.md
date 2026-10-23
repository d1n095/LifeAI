# Independent examination — PR #303 (close DEF-1 / operational handover)

- **Candidate:** PR #303, **NOT yet frozen**. SHA under test: `ec0f4d2f76460a9d51de15f000c76297ab26a7c6`. Base: frozen #302 `ff28b51429a5bc61f78308b657b8d57b2e5bf152` (verified ancestor; 2 commits on top).
- **Status of this verdict:** interim. Per the assignment, the **final exact-SHA PASS/FREEZE verdict is issued only once #303 is frozen and required CI is green.** All independently executable examination is complete now and recorded below.
- **Examiner:** Claude, read-only. Real app/worker/DB/gateway/bridge; no paid calls, no GitHub writes, no production change; worktree verified unchanged after every suite.
- **Delta over #302:** new `persist.py` (owner-scoped backing-row creation), new `connect.py` (`run_authorized_receiver` + honest handover view + CLI inventory), `lifecycle.py` `_persist_ledger` now delegates to `persist_owner_ledger`, `durable.py` status-rank merge tie-break + `operational_handover_view`, Founder-ops `/agent-gateway/connect` surface, candidate suite (213).

## Interim code assessment: **PASS (pending freeze + CI)** — DEF-1 closed, no new defect found
The DEF-1 root cause (`_persist_ledger`'s `if not assignments: return`) is removed and replaced by `persist_owner_ledger`, which:
- creates **one owner-scoped** `AgentWorkAssignment` backing row when discovery starts cold (no prior assignment), via a real goal/plan/task + an **internal, read-only, `supports_write=False`** `mainai-ledger` agent — so the backing row carries no write or GitHub capability;
- writes every obligation's full ledger dump onto the backing row **and** sibling assignments (GH-4b multi-obligation survival);
- is idempotent — `reconcile_owner` skips the backing row itself (no feedback loop), and the `durable.py` load-merge now keeps the higher status-rank obligation so a stale sibling cannot regress a more-advanced one.

## 1–3. GH-4 / GH-4b and durability — **FIXED** (my own DEF-1 probe now passes)
| Check | #302 | #303 @ ec0f4d2 |
|---|---|---|
| GH-4 cold-start idempotency (no prior assignment) | **FAIL** `idempotent=False` | **PASS** `idempotent=True` (run1 ids == run2 ids) |
| GH-4b two obligations survive restart | PASS `2/2` | PASS `created=2 survived_restart=2 missing=set()` |
| GH-3 restart obligation count | 2 | 2 |
| gh_intake_v1 overall | 7 pass / 1 fail | **8 pass / 0 fail** |

`gh_intake_v1` is the suite I wrote to *detect* DEF-1; its GH-4 passing on #303 is independent confirmation the cold-start persistence defect is closed, not the builder's own assertion.

## 4. Backing-row creation — owner-scoped, write-incapable, no RLS-bypass path (`connect_v1` CONN-1)
- Two owners with no prior assignment each get their **own** backing row (`CONN-1`: distinct ids, `owner_id` matches); a second `ensure` for the same owner returns the **same** row (idempotent, no duplicate).
- The backing row is `read_only` / `supports_write=False`; `persist_status.created_without_manual_assignment=True`.
- Creation is driven only from `owner_id`s already present in the DB (worker reconciles `AgentWorkAssignment.owner_id.distinct()`; the HTTP refresh uses the authenticated Founder's id) — there is **no attacker-controlled `owner_id` path**. The HTTP founder-ops path creates it under founder RLS context (confirmed by GH-4 passing via the real endpoint); the worker path runs on the pre-existing superuser maintenance engine (RLS-bypassing **by existing design**, not introduced here). Observation, not a finding.

## 5. Regression suites on the candidate — all green, blockers preserved
`delivery_v1` 6/6, `lifecycle_v1` 7/7, `live_handover_v1` 9/9, `handover_v1` 12/12, `security_audit_v1` 4/4, candidate `test_coordination_operational_handover.py` 5/5. Preserved Codex blockers still open: `coord_accept_v1` **CO-10 FAIL**, `fa_core_v1` **FAC-1..5 FAIL** (FA-PRIV). `security_diag_v1` 10/10.

## 6–8. Authorized receiver + CLI gating + durability (`connect_v1` CONN-2..4)
- **CONN-3 (item 6):** `run_authorized_receiver` drives the **real** gateway over TestClient — pull→claim→deliver→acknowledge→complete — `delivered=1`, `completed`, `vendor_delivery_claimed=False`. The CLI runs only after a genuine authorized claim.
- **CONN-2 (items 7, 9):** the local CLI is **fail-closed**. Not enabled ⇒ `ran=False, missing_capability=founder_adapter_enablement`. Even with the executable present on PATH **and** `ENABLED=true` **and** `CREDENTIALS_CONFIRMED=true`, with **no Founder-supplied argv template** it still does **not** run. An available local executable is never, by itself, proof a vendor agent is connected. Message contents never flow into argv — they cannot elevate authority, clear STOP, or grant GitHub write.
- **CONN-4 (item 8):** completion survives a genuinely fresh `sessionmaker` session; a duplicate completion `event_id` ⇒ `replay_ignored` — idempotent execution, no double-exec across restart.

## 9. Capability classification (kept separate, as required)
| Layer | State on #303 | Evidence |
|---|---|---|
| **Internal coordination** (discover → obligation → durable ledger → examiner) | **WORKS**, now durable even cold-start | gh_intake GH-4/4b, lifecycle_v1, handover_v1 |
| **Local authorized CLI execution** (Founder-enabled, after a real claim) | **AVAILABLE but fail-closed** — off until the Founder sets ENABLED+CREDENTIALS_CONFIRMED+ARGS | CONN-2/3; `adapter_config.py` five-way distinction |
| **Real Cursor/Claude/Codex vendor comms** | **BLOCKED** — `agent_messaging_adapter` missing; no vendor API in this runtime | CONN-5; `connect.py` VENDOR_SETUP |

## 10. Founder-ops honesty (`connect_v1` CONN-5)
`operational_handover_view` reports `external_vendor_operations.usable=False` (cursor/claude/codex all False), `founder_must_forward_messages=True` with no live delivery, `vendor_delivery_claimed=False`, `first_cycle_go=False`, and per-recipient `remaining_permissions` with concrete Founder setup steps; `github_publication` stays Codex-owned. It does **not** pretend an adapter is active.

## Deliverable answers
- **Exact-SHA code verdict:** **deferred** until #303 is frozen and CI is green; the interim assessment on `ec0f4d2` is **PASS / no new defect, DEF-1 closed**. (Re-run `gh_intake_v1`+`connect_v1` on the frozen SHA to finalize.)
- **GH-4 / GH-4b:** GH-4 **CLOSED** (`idempotent=True`), GH-4b PASS.
- **Can the Founder stop manually forwarding agent messages TODAY?** **Not for the real vendor agents.** Real transport evidence: internal coordination + the Founder-issued local authorized bridge can receive updates, deliver one instruction, ingest a completion and prepare independent review (CONN-3, delivery_v1) — so for **any agent operating the authorized bridge/CLI**, forwarding is automated. But Cursor/Claude/Codex vendor messaging is **BLOCKED** (CONN-2/5), so until an agent-side runner is operated with a Founder-issued token, the Founder still forwards to the real vendors. Honest `founder_must_forward_messages=True` with no live bridge.
- **Minimal remaining engineering:** an **agent-side bridge/CLI runner** that each real agent executes against the gateway (Cursor/MainAI); the gateway side is done. **Founder-only dependencies:** issue + hand off a scoped bridge token per agent, and (optionally) set `LIFE_AGENT_ADAPTER_ENABLED/CREDENTIALS_CONFIRMED/ARGS` to enable local CLI execution. **Codex:** FA-9/CO-10 + FA-PRIV + GitHub publication authority.

## Limitations & queue
- #303 not frozen; CI not independently re-read here (no GitHub credentials). Final exact-SHA verdict pending freeze + green CI.
- No access to Codex's private bundle or the live VPS — not treated as evidence of security.
- Queue: all independently executable examination of #303 is complete at `ec0f4d2`. Only the freeze+CI confirmation remains before finalizing; that is a re-run, not new research.

## Reproduce
```
probes/gh_intake_v1/run.sh    ../wtcert-ec0f4d2   # 8 passed — GH-4 idempotent=True (DEF-1 closed), GH-4b 2/2
probes/connect_v1/run.sh      ../wtcert-ec0f4d2   # CONN-1..5 (5 passed) — backing-row isolation, CLI fail-closed, receiver, durability, honesty
probes/delivery_v1/run.sh     ../wtcert-ec0f4d2   # 6 passed
probes/lifecycle_v1/run.sh    ../wtcert-ec0f4d2   # 7 passed
probes/live_handover_v1/run.sh ../wtcert-ec0f4d2  # 9 passed
probes/handover_v1/run.sh     ../wtcert-ec0f4d2   # 12 passed
probes/security_audit_v1/run.sh ../wtcert-ec0f4d2 # 4 passed
probes/coord_accept_v1/run.sh ../wtcert-ec0f4d2   # CO-10 FAIL = preserved (Codex)
probes/fa_core_v1/run.sh      ../wtcert-ec0f4d2   # FAC-1..5 FAIL = preserved FA-PRIV (Codex)
```
