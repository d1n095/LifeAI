# Priority 2 — full authority-chain composition (publication + STOP-clear + FA-PRIV)

- **Subject:** FA-PRIV tip `078312d7abce3f5c0f87c9e7b30161973e55d532` — contains all three subsystems
  (the 7fbe6771 review-publication boundary + STOP-clear `20d5a143` + FA-PRIV).
- **Suite:** `probes/fc_chain_v1` — **5 passed**. Targets composition/concurrency risks NOT covered by
  `fc_kernel_v1` (7) or the publication suites (`fc_pub` 11 / `fc_pubexec` 9). Read-only; mocked
  effects; no network/credentials. `first_cycle_go=false`.

| ID | Composition risk | Result |
|---|---|---|
| CH1 | **Publication ↔ STOP fence on the composed tip:** an active STOP blocks the publication claim before any mint, on the FA-PRIV-reworked tip (not just the 7fbe6771 base) | PASS — "blocked by durable STOP" |
| CH2 | **Credential isolation:** a review-publication step-up receipt cannot authorize a STOP-clear (different authorization object; no cross-subsystem proof reuse) | PASS — `StopClearError` |
| CH3 | **Uniform FA-PRIV denial:** the ordinary runtime cannot write **any** of the three authority tables — `workforce_authority_epoch`, `founder_stop_clear_authorizations`, `founder_review_publication_grants` | PASS — permission denied on all three |
| CH4 | **STOP-clear epoch binding:** a stale `expected_epoch` cannot authorize a clear (epoch replay resistance) — not covered by fc_kernel | PASS — `StopClearError` |
| CH5 | **Epoch monotonicity:** the authority epoch strictly increases across STOP cycles (generation number; a stale epoch can never be replayed) | PASS — `e2 > e1` |

## Findings
- **No new defect.** The three authority subsystems compose consistently: they share one durable STOP
  fence (`assert_grant_allowed` / `workforce_authority_epoch`) that gates publication, a single pinned
  Founder identity, a uniform runtime write-denial surface, and a monotonic epoch that STOP-clear binds
  to. Credentials are not reusable across subsystems (CH2). No composition or concurrency weakness was
  found at the examined boundaries.
- Concurrency note (code-reviewed): `assert_grant_allowed` (publication claim) takes a `FOR SHARE`
  lock and `activate_kill_switch` a `FOR UPDATE` on the same epoch row, so a STOP racing a publication
  claim is ordered by the database lock manager, not application timing (consistent with the module
  docstrings). A true multi-process race was not executed; the lock-ordering invariant is read-verified
  and the single-threaded epoch/stale-epoch behaviour (CH4/CH5) is executed.

## Verdict
**Priority 2 COMPLETE — the publication/STOP-clear/FA-PRIV authority chain composes correctly on
`078312d7` with no new defect (fc_chain_v1, 5 passed):** one shared STOP fence gates publication,
runtime write-denial is uniform across all three authority tables, STOP-clear is epoch-bound and
stale-epoch-resistant, the epoch is monotonic, and credentials do not cross subsystems. Not a release
certification (the broader base line is not examiner-certified end to end; live publication paths
remain mocked). Read-only; no merge/deploy/publication/GO. `first_cycle_go=false`.

## Reproduce
```
probes/fc_chain_v1/run.sh ../../wtcert-078312d7     # 5 passed (CH1..CH5)
```
