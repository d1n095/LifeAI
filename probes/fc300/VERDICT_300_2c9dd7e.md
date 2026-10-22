# Independent examination — #300 (read-only GitHub intake into the durable ledger)

- **Candidate:** PR #300 (draft; PR body declares "Frozen for independent examination"), branch `cursor/github-intake-91c1`.
- **Examined SHA:** `2c9dd7e06dd3072f8f1f31531437f285ae978e5f`. Two commits on #299 `1105e33`; ancestry verified.
- **Exact-SHA CI:** **all 12 required checks green on BOTH suites** (independently re-read; 25 success / 12 skipped VPS-deploy / 0 failure).
- **Delta:** `github_intake.py` (559 lines, read-only GitHub discovery), `POST /api/mainai/founder-ops/github-intake/refresh` (Founder-only), durable.py +33, founder-ops snapshot renders `durable_coordination.github_intake`.
- **Examiner:** Claude, read-only. The only faked thing is the GitHub network boundary (`GitHubReadClient.get` monkeypatched to scripted JSON); the route, `require_founder`, the service, persistence and the Founder read are all production code. No paid calls, no real network, no GitHub writes.

## Verdict 1 — exact-SHA code: **RETURN_TO_BUILDER** (one real production-wiring defect)

#300 adds exactly the ingestion boundary I flagged as missing on #299, and the read-only intake, auth, discovery, BLOCKED handling, stale-CI and builder→examiner logic are all correct. But the endpoint's **persistence is incomplete**, which breaks the two durable guarantees the PR exists to provide.

### DEF-1 (P2, #300) — refresh persists only one obligation, and only when a backing row pre-exists

`refresh_github_intake` persists `obligation_id = next(iter(service._obligations))` onto the **first** `AgentWorkAssignment`, and only `if assignment is not None`. Reproduced on the real endpoint:
- **GH-4b:** a single PR refresh creates **2 obligations** (builder + examiner) but **only 1 survives restart** — the examiner obligation is lost (`created=2 survived_restart=1`). Contradicts item 8 (durable survival) and item 5 (builder→examiner).
- **GH-4:** with no pre-existing `AgentWorkAssignment`, the endpoint persists **nothing**, so a second refresh reloads an empty ledger and re-mints obligations — **not idempotent across HTTP calls / restarts** (different ids each refresh). Contradicts item 4 (no duplicate assignments).

The in-process logic is correct (`_ensure_obligation` dedups by canonical key, examiner is created); only the endpoint's persistence step is partial. **Fix:** persist every obligation the refresh touched (not just the first), and ensure/create a durable backing row when none exists, so reload is complete and idempotent.

### What is correct (production-path verified)

| Item | GH | Result |
|---|---|---|
| 6 Founder-only; input cannot forge authority | GH-1 | PASS — anon/member/role-only `founder` impostor all refused (403); **endpoint is absent on #299 → 404**, confirming #300 added it |
| 1,2,7 Real discovery, read-only | GH-2 | PASS — `outcome=applied`, `write_attempted=False`, `github_write=False`, `delivery_claimed=False` |
| 8 Persist + restart (single obligation) | GH-3 | PASS — a persisted obligation survives a fresh-session `founder_coordination_snapshot` (the one that *is* persisted) |
| 3 Stale failed workflow | GH-5 | PASS — a later failed-CI observation of a different SHA marks `freeze_blockers`, does not erase the good candidate |
| 5,10 Builder→examiner, no false delivery/certification | GH-6 | PASS — `delivery_claimed=False`; green CI alone never marks `independently_verified` (needs a non-builder examiner) |
| 7 API failure / rate limit → BLOCKED | GH-7 | PASS — `outcome=blocked`, `missing_capability=github_read_rate_limit`, `write_attempted=False` |

Read-only is structurally sound: `GitHubReadClient` is GET-only, public-unauthenticated when no token; `claim_agent_delivery` and `claim_github_publication` both return `{"outcome": "blocked", "write_attempted": False}`.

## Verdict 2 — operational handover: **BLOCKED (materially advanced)**

**What MainAI can truly automate today:** read-only GitHub discovery of real PRs (head SHA, ancestry, required-CI classification, reviews) **through a Founder-only production endpoint**, turning them into builder/examiner obligations in the durable ledger, with truthful BLOCKED on API failure and no false claims of delivery or certification. `live_handover_v1` **LRH-2 now PASSES** — the ingestion boundary exists (it was BLOCKED on #299). This is the exact gap #299 lacked.

**What remains simulated or disconnected:**
1. **DEF-1** — the ledger does not durably persist all obligations, so the discovered work is not reliably reconcilable across restarts/refreshes (Cursor).
2. **No live agent delivery/messaging** — `claim_agent_delivery` is correctly BLOCKED; MainAI discovers and records GitHub activity but does **not** send obligations to Cursor/Codex/Claude. So the Founder still forwards the actual *instructions to agents*; MainAI now tracks the GitHub side read-only.
3. **Codex-owned** STOP-resume integrity (FA-9/CO-10) and Security-Kernel DB enforcement (FA-PRIV) — preserved, still open.

## Minimum remaining work before the Founder can stop forwarding routine agent updates

1. **Cursor — fix DEF-1** (persist all obligations + ensure a durable backing row) so the GitHub-intake ledger is durable and idempotent. Gate: GH-4, GH-4b.
2. **Cursor/MainAI — agent delivery path** (even read-only "notify" receipts) so a builder→examiner obligation is actually surfaced to the examiner, not only recorded. Until then this is one-directional GitHub observation, not two-way coordination.
3. **Codex — FA-9/CO-10 and FA-PRIV** (safety).

Items 1–2 are the coordination gap; item 3 is safety. None needs GitHub writes, external API access beyond read-only, or manual Founder file transfers.

## Reproduce
```
probes/gh_intake_v1/run.sh ../wtcert-2c9dd7e        # GH-1..7 + GH-4b (DEF-1); GH-4/4b FAIL
probes/live_handover_v1/run.sh ../wtcert-2c9dd7e    # LRH-2 now PASS (ingestion surface discovered)
probes/gh_intake_v1/run.sh ../wtcert-1105e33 test_gh.py -k GH1   # control: endpoint absent on #299 (404)
```
