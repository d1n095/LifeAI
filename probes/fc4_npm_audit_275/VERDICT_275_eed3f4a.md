# Independent examination — FC-4 npm-audit remediation PR #275

- Exact SHA: `eed3f4a380be71398a0481414518a29506a57ccb` (`codex/frontend-npm-audit-fc4-remediation`). Builder: Codex.
- Base: `2fbe20aacf1203fc0e16d216ef55b666b2181619`; the diff vs base is 7 files (frontend `package.json`/lock, `check-npm-audit.js` plus a new test, `ci.yml`, `SECURITY_BLOCKERS.md`, registry).
- Examiner: Claude, read-only. The candidate was not modified.
- Clean `git archive` export to a scratch directory, `npm ci` from the npm registry. No dependency changes, no GitHub writes.
- Disclosure: Claude authored the FC-4 *proposal* this implements (evidence `2eeab79`), not the implementation.

## Verdict: RETURN_TO_BUILDER — dependency remediation is correct; the exception policy lacks two required bindings (P1 ×2, ~15 lines in `check-npm-audit.js`)

### 1. Baseline (clean checkout, npm 11.19.1 lock-only)
- Base `2fbe20a`: full tree 14 findings (1 critical, 10 high, 3 moderate); production (`--omit=dev`) 4 (1 critical, 2 high, 1 moderate). This matches FC-4 evidence `2eeab79` and the #267 CI log.

### 2. Production
- `eed3f4a`, after `npm ci`: **`npm audit --omit=dev` = 0** under both npm 11.19.1 and npm 10.9.2 (CI's Node 22 line).
- Full tree: 9 findings (7 high, 2 moderate, 0 critical). Every high finding roots in `braces` source 1240992.

### 3. Dependency changes: necessary and scoped
The lock diff is 44 entries, all registry.npmjs.org, all with integrity hashes. They are exactly the packages below plus their platform binaries.

| Change | Fixes | Necessary? |
|---|---|---|
| `next` 16.3.5 → **16.3.8** (prod; plus `@next/env`, `@next/swc-*`) | critical GHSA-vcvr-r3jv-pc5j (≥16.3.6), high SSRF GHSA-cjq9-62q9-8jv4 (≥16.3.8) | yes; the smallest patched version (smaller than my 16.4.0 proposal) |
| `eslint-config-next` 16.3.5 → 16.3.8 (dev; plus `@next/eslint-plugin-next`) | keeps lint rules on the same Next line | alignment only (it stays high via braces either way); appropriate |
| `postcss` 8.5.19 → 8.5.29 (direct + override; pulls `nanoid` 3.3.20) | moderate GHSA-fxqj (≥8.5.23); the old override pinned the vulnerable version | yes |
| `overrides.sharp` 0.35.4 → 0.35.5 (+ `@img/*` binaries) | high GHSA-wq5f-xc86-pv6w; the old override pinned the vulnerable version | yes |
| `overrides.source-map-js` = 1.2.2 | high GHSA-68fv-2mgg-jv7q (prod) | yes |
| `overrides["brace-expansion@^1"]` = 1.1.21, `["brace-expansion@^5"]` = 5.0.12 | the brace-expansion highs; range-keyed, so no cross-major forcing | yes; old allowlist IDs 1124334/1130588/1130591 removed |

### 4. `braces` exception (GHSA-vfj7-8cjw-p6xm)
Bound in code to package, source 1240992, severity `high`, URL, title, and range `<=3.0.3`. Dev-only status is enforced structurally by a separate production audit that accepts no exception. The deadline 2027-01-15 is in a comment and in `SECURITY_BLOCKERS.md` §3a only.

Adversarial probes (`adversarial_policy.test.js`, run against the candidate's own `evaluateAudits`; matrix in `adversarial_matrix_eed3f4a.txt`):

| Probe | Result |
|---|---|
| A0 exact dev-only braces advisory (major-only fix) passes | HOLDS |
| A1 another advisory on `braces` cannot inherit | HOLDS (`unapproved high dev advisory: braces 1299999`) |
| A2 production occurrence fails | HOLDS (`production vulnerability: braces (high)`) |
| A3 a different high dev advisory fails | HOLDS |
| A4 same source re-rated critical fails | HOLDS |
| A5 range rewritten after a patch (`<3.0.4`) fails | HOLDS |
| **A6a/b/c the excepted advisory with a COMPATIBLE fix available** (`fixAvailable: true`, `{braces 3.0.4, isSemVerMajor:false}`, `{micromatch 4.0.9, isSemVerMajor:false}`) fails | **GAP — passes silently**. The high branch never consults `fixAvailable`; only the moderate branch does. If braces ships 3.0.4 and the GHSA keeps `<=3.0.3`, the stale lock passes CI indefinitely. |
| **A7 exception expires after its review deadline** (clock 2027-02-01) | **GAP — passes**. No expiry in code. |
| A8 unattributed high node fails | HOLDS |
| A9 a `via` cycle terminates and fails closed | HOLDS |
| A10 a high-via-braces package that also carries its own compatible-fix moderate advisory | GAP (P2): the high branch filters roots to high/critical, so that moderate is never evaluated as actionable |
| A11 moderate wrapper with its own compatible non-major fix while its root is major-only | GAP (P2, informational): by design (`eed3f4a` "normalize root fixability"), wrapper-level fixability is ignored whenever roots exist |
| R10 / R11 real npm 10.9.2 / 11.19.1 output at eed3f4a passes the policy | HOLDS (`node scripts/check-npm-audit.js` exits 0 under both; the candidate's own `npm run audit:policy:test` passes) |

### 5. Remaining moderates (Tailwind chain)
- `postcss-selector-parser` (source 1241232, `<7.1.6`) and its wrapper `postcss-nested`, both via `tailwindcss@3.4.4`.
- **Dev-only:** yes; absent from `--omit=dev`.
- **Actionable:** not compatibly. The only fix is `tailwindcss@4.3.3` (`isSemVerMajor: true`), because postcss-nested 6.x requires selector-parser ^6.
- npm 10.9.2 reports `postcss-nested.fixAvailable: true` while npm 11 reports the major. The policy's root normalization handles this divergence correctly here.
- **Policy:** intentionally allowed (moderate + major-only), documented in `SECURITY_BLOCKERS.md` §3a. Not an unhandled gap.

### 6–7. Builds and checks (local, clean export; Node 26.9 / npm 11.19 — npm 11 blocks install scripts by default, so CI on Node 22 / npm 10 is the authoritative run)
- `npx tsc --noEmit`: rc 0.
- `npx eslint .`: 0 errors, 1 warning (`@next/next/no-location-assign-relative-destination` in `lib/api.ts:97`; informational, non-blocking).
- `next build` docker mode: OK, `.next/standalone` produced. Vercel mode (`VERCEL=1`): OK, no standalone. Next.js 16.3.8.
- Playwright E2E: not run locally (no Docker / full stack here). Covered by exact-SHA CI.

### 8. Exact-SHA GitHub CI at `eed3f4a` (two runs: 38699760621, 38699766049)
- Every product check passes in both runs: npm audit, TS & ESLint, build docker/vercel, backend unit/integration (completed 08:28Z and 09:02Z, the job that was still running at Codex's report), RLS, account lifecycle, Alembic, Playwright full stack, Library/Founder Studio, same-origin proxy, Vercel.
- **`All required checks passed` = success.**
- Combined-container and VPS-compose jobs are skipped (path/conditional rules, unchanged).

## Findings
- **P1 — A6:** the exception does not fail when a compatible fix exists. **Fix:** in the high branch, when a root matches the exception, also require `!compatibleFixAvailable(full.vulnerabilities[root.packageName])` and `!compatibleFixAvailable(<the propagating node>)`; otherwise error `excepted advisory now has a compatible fix — remove the exception`.
- **P1 — A7:** the review deadline is not machine-enforced. **Fix:** add `expires: "2027-01-15"` to the exception and fail when `Date.now()` passes it (`exception expired — re-review required`). Add a unit test with an injectable clock.
- **P2 — A10:** evaluate actionable moderate/low roots for every node, including nodes that are high only via braces.
- **P2 — A11:** document the wrapper-fixability normalization (npm 10/11 divergence) in `SECURITY_BLOCKERS.md`, or limit it to `fixAvailable === true` on propagation-only wrappers.

Everything else, including the dependency set, the lock, production = 0 and CI, is ready. After the A6/A7 fix plus tests, the expected verdict is PASS / FREEZE with no dependency changes.

## Reproduce
```
git archive eed3f4a380be71398a0481414518a29506a57ccb frontend | tar -x -C <dir> && cd <dir>/frontend && npm ci
npm audit --json; npm audit --omit=dev --json; npx -y npm@10.9.2 audit --json   # (and --omit=dev)
node scripts/check-npm-audit.js && npm run audit:policy:test
POLICY=<dir>/frontend/scripts/check-npm-audit.js REAL_DIR=<dir with full_npm1{0,1}.json, prod_npm1{0,1}.json> node --test probes/fc4_npm_audit_275/adversarial_policy.test.js
npx tsc --noEmit && npx eslint . && NEXT_PUBLIC_API_URL=http://localhost:8000 npx next build   # then VERCEL=1 for vercel mode
```
