# GO/NO-GO — `ee132bb8e87fcc99f2f3607cfc2b46a5364d23c7`

| Area | Suites | Result | Detail |
|---|---|---|---|
| Founder authority, one-use grants, replay prevention, typed denial | authorization_grants, grant_contract | **NO-GO** | 12 blocker/regression |
| Credential isolation / credential-free transport | credential_canary, transport_contract, candidate_harness | **NO-GO** | 7 blocker/regression |
| External failures terminal, no paid re-run (incl. FO-6), STOP/recovery | stop_classification, review_stop | **NO-GO** | 4 blocker/regression |
| Publication content and history (TA-12/13, PUB-2), task authority | task_authority | **NO-GO** | 2 blocker/regression |
| Spend controls | spend | GO | clean |
| Migration integrity (single head, no dual 0089) | migration_integrity | GO | clean |
| Application readiness (authz matrix, truthfulness D1-D5, races, safe launch) | app_readiness | **NO-GO** | 11 blocker/regression |
| Same-origin proxy (traversal, XFF trust, rate limits) | proxy | **NO-GO** | 3 blocker/regression |
| UI truthfulness in a real browser | ui | **NO-GO** | 2 blocker/regression |
| Candidate regression + FC-4 policy | backend_regression, fc4_policy, fc4_bypass | GO | clean |
| Exact-SHA CI (12 checks x push+PR) | ci_exact_sha | GO | clean |
| Integration branch at approved base SHA | class F | **NO-GO (not attested)** | Founder/GitHub evidence required |
| Ruleset: PR required, 12 checks bound to app 15368, no bypass, Founder-only merge | class F | **NO-GO (not attested)** | Founder/GitHub evidence required |
| Runtime GitHub App identity + permission manifest, no classic PAT | class F | **NO-GO (not attested)** | Founder/GitHub evidence required |
| Signed attestation material + pinned verification key | class F | **NO-GO (not attested)** | Founder/GitHub evidence required |
| First-cycle spend grant + envelope profile | class F | **NO-GO (not attested)** | Founder/GitHub evidence required |
| Reviewer identity + receipt runbook | class F | **NO-GO (not attested)** | Founder/GitHub evidence required |
| Incident token revocation + evidence publication | class F | **NO-GO (not attested)** | Founder/GitHub evidence required |

**Overall: NO-GO** (executable rows not all GO; class-F rows require Founder attestation).
