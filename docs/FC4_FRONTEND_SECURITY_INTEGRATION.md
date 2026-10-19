# FC-4 frontend security integration (isolated correction)

Frozen original candidate (do not amend):

- PR #275 `codex/frontend-npm-audit-fc4-remediation`
- SHA `eed3f4a380be71398a0481414518a29506a57ccb`
- Parent of that SHA: `88b90742c536938136d8c07064d9db283c7fe985`

This correction branch:

- `cursor/fc4-npm-audit-a6-a7-91c1`
- Parent SHA (exact): `eed3f4a380be71398a0481414518a29506a57ccb`
- Independent exam of #275: `6d56ff029328c542a77bd6cccbaebdaf16124539` (RETURN_TO_BUILDER)

Do not merge this document's lanes here. Do not amend #275, #276, #277, #279, #280, or #281.

## Authoritative policy rule

The final activation composition must contain **exactly one** `frontend/scripts/check-npm-audit.js`.

Take **this branch's** implementation. Discard #275's pre-A6/A7 policy. Do not keep both.

#248 (`cursor/frontend-npm-audit-browserslist-4-28-7`) also touches `frontend/package.json` / lock and is a competing older lock, not a second policy. Do not compose it as an alternative FC-4 candidate.

## How this composes

| Lane | Frozen/head SHA at writing | Overlap with this PR | Compose rule |
|---|---|---|---|
| Frozen #276 Path A | `09b87e9b70dc02889d39c2bae3d56458509d49cb` | none (`check-npm-audit.js` absent) | Keep frozen. Cherry-pick or merge this frontend policy later; no file conflict expected. |
| Claude #277 (being replaced) | `f0c1d70e0fae44951d82fb24a2fc4bc3188093dc` | `.github/workflows/ci.yml` only | #277/#281 change CI wait/triggers. This PR only comments the npm-audit job. When composing, keep their wait/trigger logic and this job's policy command + comment. One `check-npm-audit.js`. |
| Claude #281 (active #277 replacement) | `abc88b4f479489c8c85111d754756058ab77eb22` | `.github/workflows/ci.yml` only | Same as #277. Do not take their branch's copy of any older audit script. |
| Codex #280 activation-security | `37bca08a287f00b6282222047340bffa949ff788` | none | Orthogonal. Do not import this policy into that substrate, or that substrate into this PR. |
| Harness #279 | `ceb416e526b671125df1464525501bfb736008ef` (exercised `fe4001e96e297b245eb3b2872902aa5ac10107d3`) | none | Preserve as test-only evidence. Its NO-GO against #276 stays. |

Conflicts are limited to `docs/BRANCH_REGISTRY.md` (every lane edits it) and a comment in `.github/workflows/ci.yml` versus #277/#281. Resolve registry by union of rows; resolve CI by combining trigger/wait from the activation-readiness lane with this npm-audit job text.

## Out of scope

No real activation, GitHub settings, merge, deployment, paid providers, or Recall.
