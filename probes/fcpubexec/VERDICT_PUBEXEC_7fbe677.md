# Priority 1 — full publication authorization+execution sequence (executable) — `7fbe6771`

- **Commit:** `7fbe677166864f398ef2b32cc374487cc7034503` (Codex verified bundle; not on GitHub).
- **Suite:** `probes/fc_pubexec_v1` — **9 passed**, driving the REAL `claim_review_publication_grant`
  + `execute_review_publication` with a **local git-bundle artifact** and **fully mocked external
  effects** (fake observer / token provider / push / PR transport). No real GitHub, no credentials, no
  network (egress-guarded). `first_cycle_go=false`.

Converts the previously code-reviewed claim/execute controls (see `fcpub/VERDICT_PUB_7fbe677.md`) into
executed adversarial tests.

| # | Priority-1 requirement | Test | Result |
|---|---|---|---|
| 1 | Exact SHA/tree/base binding at consumption | PE6 (tree/base via real bundle inspect) + PE4 (base moved) | PASS — rejected before mint |
| 2 | Bundle/manifest substitution or corruption | PE6 (bundle bytes replaced after authorization) | PASS — `ReviewPublicationError`, no mint |
| 3 | Changed repository / destination branch after authorization | PE4 (base moved) + PE5 (branch already exists → no overwrite/force-push) | PASS — no mint |
| 4 | Concurrent claims against a one-use grant | PE7 (two claims; atomic consume) | PASS — one `consumed`, second rejected |
| 5 | STOP/revocation between issuance and consumption | PE8 (STOP before execute) | PASS — blocked before mint |
| 6 | Crash/restart after consume, before remote effect | PE3 (consumed grant replay) | PASS — consumed grant cannot re-effect; no second mint/push |
| 7 | Token minted only after the authorization txn commits | PE1 (`consumed_at_mint is True`) | PASS — grant `status=consumed` before `provider.mint` |
| 8 | No token/publication/second effect on rejected/replayed claims | PE3 (replay), PE4/5/6/8 (rejected) | PASS — `mint_calls==0`, push log empty |
| 9 | Accurate append-only audit for success and rejection | PE1 (success attempt+receipts) + PE10 (runtime cannot DELETE `founder_authority_audit_receipts`) | PASS |

### Key proofs
- **PE1 happy PUSH:** with mocked effects the full sequence yields `observed_succeeded` only after an
  independent post-push ref observation equals the authorized candidate SHA; **exactly one** push
  effect; `provider.mint` ran **once and only after the grant was durably `consumed`** (token-after-commit).
- **PE3 replay / PE7 concurrency:** a one-use grant consumes atomically; a second execute/claim is
  rejected and mints **no** token and performs **no** second effect — so a crash between consume and
  effect cannot be retried into a double publication with fresh authority.
- **PE9 bad lease:** a lease carrying an unrelated `administration:write` scope is rejected **after**
  mint → the token is **revoked**, **no push** occurs, and the grant stays consumed (never retried with
  new authority) — matching the design's "any error after mint is UNKNOWN, never auto-retry."
- **PE10 audit:** `founder_authority_audit_receipts` is runtime SELECT-only; the runtime cannot delete
  or forge audit evidence.

## Verdict
**Priority 1 COMPLETE — the full publication authorization+execution sequence is now INDEPENDENTLY
EXECUTED and fail-closed across all nine requirements (fc_pubexec_v1, 9 passed), using local artifacts
and mocked external effects.** No new defect; behaviour matches the code-reviewed design. Still not a
release certification: the real GitHub App token mint + live push/PR are mocked (by design — no real
publication/credentials). Read-only; no merge/deploy/publication/GO. `first_cycle_go=false`.

## Reproduce
```
probes/fc_pubexec_v1/run.sh ../../wtcert-7fbe677     # 9 passed (PE1..PE10)
```
