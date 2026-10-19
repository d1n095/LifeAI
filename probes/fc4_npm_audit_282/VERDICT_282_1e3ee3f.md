# Independent security examination — Candidate B: #282 FC-4 npm-audit policy correction

- **Candidate:** PR #282.
- **Frozen head:** `1e3ee3fd604fc540600f393e215a712b283686d4`.
- **Parent:** #275 `eed3f4a380be71398a0481414518a29506a57ccb`.
- **Diff vs parent:** `frontend/scripts/check-npm-audit.js`, its test file, `.github/workflows/ci.yml` (one comment), and four docs.
- **Dependencies:** `package.json` and `package-lock.json` are **unchanged** vs #275.
- **Baseline:** my #275 verdict `6d56ff0` (A6/A7 P1, A10/A11 P2).
- **Examiner:** Claude, read-only. No GitHub mutation, merge, deploy or Recall. This is an independent examination; I did not build #282.

## Verdict: PASS / FREEZE — no P0/P1; three P2 policy-hardening items

All four #275 findings are closed. The original matrix gives A0–A11 HOLD, with real npm 10 and npm 11 output HOLD.

Production audit is 0 under both npm majors, and exact-SHA CI is fully green.

My new bypass probes found the policy **fails open on internally inconsistent or out-of-vocabulary severity metadata, and on metadata/vulnerability count mismatch**. Each of these needs an audit report that genuine npm 10/11 does not produce:
- npm derives a node's `severity` as the max of its advisories;
- severity is a fixed five-word vocabulary;
- `metadata` counts are computed from the same map.

An attacker able to forge the report could equally just omit advisories, since the registry and npm are already trusted inputs. These items are therefore **P2 hardening, not a bypass reachable from real `npm audit` output**. They should be fixed in the next FC-4 touch, because the gate claims "malformed metadata fails closed".

## 1. Original findings (re-run of `probes/fc4_npm_audit_275/adversarial_policy.test.js`)

| Probe | #275 | #282 |
|---|---|---|
| A0 exact braces exception (major-only fix) passes | HOLDS | HOLDS |
| A1/A3/A4/A5 exception specificity (another advisory on braces, a different high, re-rated critical, range rewritten) | HOLDS | HOLDS |
| A2 production occurrence fails | HOLDS | HOLDS |
| **A6a/b/c** compatible fix (`true`, `{braces 3.0.4}`, `{micromatch 4.0.9}`) revokes the exception | **GAP P1** | **HOLDS**: "excepted advisory now has a compatible fix — remove the exception" |
| **A7** expiry 2027-01-15 enforced | **GAP P1** | **HOLDS**: "exception expired — re-review required" |
| A8/A9 unattributed node, via-cycle | HOLDS | HOLDS |
| **A10** independent moderate with compatible fix on a high-via-braces wrapper | **GAP P2** | **HOLDS**: "actionable dev vulnerability: fg (moderate)" |
| **A11** npm 10/11 wrapper/root semantics | **GAP P2** | **HOLDS** |
| R10 / R11: real npm 10.9.2 / 11.19.1 output for this lock | HOLDS | HOLDS |

Full output: `matrix_1e3ee3f.txt`.

## 2. New bypass probes (`bypass_policy.test.js`)

| Probe | Result |
|---|---|
| B1 a high advisory under a node whose own `severity` is "moderate" | **GAP**: passes. The high branch keys on node severity (`nodeIsHigh`); the high roots of a non-high node are ignored. |
| B2 severity "High" (non-canonical casing) | **GAP**: passes (`isHighSeverity` is an exact lowercase match) |
| B3 unknown severity word | **GAP**: passes |
| B4a/B4b deadline boundary (−1 ms passes; at the deadline expired) | HOLDS |
| B5 malformed expiry (2027-02-30) | HOLDS |
| B6 missing `vulnerabilities` | HOLDS |
| B7 production report: `vulnerabilities:{}` but `metadata` counts 2 highs | **GAP**: passes |
| B8 full report: the same with a critical | **GAP**: passes |
| B9 non-array `via` | HOLDS |
| B10 `fixAvailable` object lacking `isSemVerMajor` | HOLDS ("untrustworthy fix metadata") |
| B11 (info) excepted root also carrying a major-only moderate | HOLDS (policy) |
| B12 invalid clock | HOLDS |
| B13 the exact braces advisory attributed to a different package | HOLDS (no inheritance) |

**P2-1 (B1/B2/B3).** Normalize and validate severity:
- reject any `severity` (node or via) outside `{info, low, moderate, high, critical}`;
- compute the effective node severity as the max over the node's own severity and its object vias.

**P2-2 (B7/B8).** Cross-check `metadata.vulnerabilities` per severity, and `total`, against the counted `vulnerabilities` map in both reports. Fail on any mismatch.

**P2-3 (documentation).** `docs/FC4_NPM_AUDIT_POLICY_MATRIX.md` lists "unexpected report shapes" under malformed coverage. Severity vocabulary and count consistency are not covered, and the doc should say so until P2-1/P2-2 land.

Correction to my working notes: the matrix doc is **not** stale. A11 "GAP (P2)" appears only in its *Before (#275)* table, and the *After* table says HOLDS.

## 3. Dependency, production/dev separation and CI

| Check | Result |
|---|---|
| Lockfile vs #275 | unchanged (no package files in the diff) |
| `npm audit --omit=dev` (npm 10.9.2 and 11.19.1) | **0** (all severities 0) |
| Full tree | 9 (7 high, 2 moderate). All 9 nodes are dev-only. The highs sit on the braces → micromatch → fast-glob/chokidar → tailwindcss / eslint-config-next chain. The moderates are postcss-nested and postcss-selector-parser. |
| npm 10 vs npm 11 `postcss-nested` | npm 10: `fixAvailable: true`; npm 11: a major-only object. The policy accepts both correctly as a string-via-only propagation wrapper (A11/R10/R11). |
| Candidate's tests `node --test scripts/check-npm-audit.test.js` | **59 / 59 pass** |
| Exact-SHA CI `1e3ee3f` | **All green (2 runs each):** `Frontend — npm audit`, `All required checks passed`, `Backend — unit/integration tests`, Alembic, RLS, account lifecycle, E2E ×3, TypeScript & ESLint, builds (docker, vercel). |
| Frontend application source | unchanged since #275 `eed3f4a`. Its TS, ESLint and build were verified there, and CI is green here. |

## 4. Composition

- **#276** `09b87e9` and **#281** `abc88b4`: `git merge-tree` is clean for both. Neither branch touches `frontend/`.
- `ci.yml`: #282 changes one comment only. Job names and branch filters are unchanged, so #281's derived 12-check contract (including `Frontend — npm audit`) and `required_events` are unaffected.
- With #282 merged, the inherited npm-audit failure on the #276/#281 lineage goes away.
- **#280** `37bca08`: merge-tree clean.
- No migration or contract conflicts.

## Reproduce

```
git worktree add --detach ../wt282 1e3ee3fd604fc540600f393e215a712b283686d4
(cd ../wt282/frontend && npm ci && node --test scripts/check-npm-audit.test.js)
npm audit --json --omit=dev > prod.json; npm audit --json > full.json   # under npm 10 and npm 11
POLICY=../wt282/frontend/scripts/check-npm-audit.js node --test probes/fc4_npm_audit_282/bypass_policy.test.js
POLICY=../wt282/frontend/scripts/check-npm-audit.js REAL_DIR=<dir with full_npm1{0,1}.json, prod_npm1{0,1}.json> node --test probes/fc4_npm_audit_275/adversarial_policy.test.js
```
