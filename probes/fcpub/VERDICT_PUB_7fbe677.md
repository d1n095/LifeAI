# Independent examination — security base `7fbe6771` (Founder-authorized GitHub App review publication)

- **Commit:** `7fbe677166864f398ef2b32cc374487cc7034503` ("Add Founder-authorized GitHub App review
  publication", 2026-10-25). Present from Codex's verified bundles; **not fetchable from GitHub**. It
  is the parent base of the STOP-clear (`20d5a143`) + FA-PRIV (`078312d7`) successors.
- **Delta vs parent `c3a9045f` (backend, 1081 ins):** `founder_authority/review_publication.py` (+452),
  `review_transport.py` (+294), `schemas.py`, `models/founder_authority.py` (+66),
  `routers/founder_authority.py` (+127), `mainai_execution/remote_effect.py` (+106), `rls.py`.
- **Lineage note:** the founder-authority capability core is inherited from `e68e7f4a` (2026-10-21);
  none of my previously certified SHAs (#303/#306/#309) are ancestors — this is a distinct line.
- **Suite:** `probes/fc_pub_v1` — **9 passed**, driving the real module. Read-only; no token minted, no
  network (egress-guarded). `first_cycle_go=false`.

## Exercised controls (executable adversarial tests, PASS)
| ID | Control | Evidence |
|---|---|---|
| PUB0 | **Privilege boundary:** ordinary `mainai_app` runtime cannot write `founder_step_up_receipts` | `permission denied` |
| PUB1 | **Step-up = identity + password + live session:** non-pinned Founder rejected; missing access session rejected; wrong password rejected | `FounderAuthorityError` / raises |
| PUB2 | **No self-authorization:** a non-Founder cannot issue a publication grant | `FounderAuthorityError` |
| PUB2b | **Binding:** grant TTL capped at 10 min; **cross-repository forbidden** (artifact repo ≠ destination); protected/base branch forbidden as destination; destination must be a new branch under the review prefix | `ReviewPublicationError` ×4 |
| PUB3 | **Effect separation:** a PUSH step-up cannot authorize a DRAFT_PR grant (purpose-bound) | `ReviewPublicationError` |
| PUB4 | **Single-use step-up:** a consumed step-up cannot issue a second grant | `ReviewPublicationError` |
| PUB5 | **Revocation:** a cancelled grant cannot be claimed | `ReviewPublicationError` |
| PUB6 | **STOP interaction:** an active Founder STOP blocks the claim *before* any token mint/network (`assert_grant_allowed`) | `ReviewPublicationError` "blocked by durable STOP" |
| PUB7 | **Exact-bytes binding:** bundle bytes must match the signed manifest digest; path-escape read refused | `ReviewPublicationError` |

## Code-reviewed controls (not fully exercised — no live GitHub App / network)
- **Exact-SHA / tree / base binding at claim:** `claim_review_publication_grant` re-inspects the
  artifact and rejects if `candidate_tree_sha`/`base_sha` changed after authorization, if the observed
  base moved, if (PUSH) the branch already exists (no overwrite/force-push), or if (DRAFT_PR) the
  observed head ≠ the authorized candidate SHA. (Guards read; the positive push/PR path needs a live
  installation token.)
- **Single-use grant race-safety:** atomic `UPDATE … WHERE status='active' → consumed`; token minted
  only **after** the consumed grant is committed ("irreversible claim is durable before credential mint
  or network I/O"). Ordering verified by reading; the committed-then-mint path is not executed.
- **Transport least privilege (`review_transport._assert_lease`):** the installation token's effect,
  repository, installation-id and app-id must equal the consumed grant; permissions must match
  `PERMISSIONS_BY_EFFECT` exactly (PUSH=`contents:write`; DRAFT_PR=`contents:read`+`pull_requests:write`);
  **any unrelated write scope is rejected** (`write_scopes != expected_writes`). Fail-closed; read-only
  `AnonymousGitHubObserver` raises on any non-200. Verified by reading (constructing a real signed
  installation lease is out of scope without the GitHub App).
- **Audit integrity:** step-up receipts and grants carry a `*_digest` + `issuer_signature`
  (`_grant_is_authentic` / step-up signature checked at issue and claim); attempts are recorded.
  Tamper-evidence verified by reading.

## Compatibility with the examined successors
Consistent and additive: the same identity pin (`_founder` → `user.id == FOUNDER_USER_ID`) and the same
durable STOP fence (`assert_grant_allowed`) are used here and in STOP-clear/FA-PRIV; `fc_kernel_v1`
(7 passed) already runs green on `078312d7`, which descends from this base. The FA-PRIV REVOKEs tighten
(not weaken) the privilege posture observed here (PUB0 shows runtime already cannot write step-up rows
at this base). No regression between base and successors was observed.

## Findings
- **No new defect** found in the review-publication boundary at `7fbe6771`. All inspected controls are
  present and fail-closed; nothing here is attributable as a defect to the newer Codex commits.
- **Pre-existing / inherited observation (not a defect):** the founder-authority core (`e68e7f4a`→) and
  this base sit on a line that has **not** been through full examiner certification as a whole; this
  examination certifies the **review-publication boundary** specifically, not the entire base.

## Verdict
**`7fbe6771` review-publication boundary: INDEPENDENTLY EXAMINED — step-up identity/password/session,
no self-authorization, exact-repo/branch/TTL binding, effect separation, single-use step-up and grant,
revocation, STOP-before-mint, and exact-bytes artifact binding are all present and fail-closed
(fc_pub_v1, 9 passed); transport least-privilege, exact-SHA claim re-checks, mint-after-commit ordering
and audit signatures are code-verified.** No new defect. This is examination, not release certification:
the live push/draft-PR token path is code-reviewed (no live GitHub App), and the broader base line is
not yet examiner-certified end to end. No merge/deploy/publication/GO; read-only; Codex/Cursor branches
unmodified. `first_cycle_go=false`.

## Reproduce
```
probes/fc_pub_v1/run.sh ../../wtcert-7fbe677     # 9 passed (PUB0..PUB7)
```
