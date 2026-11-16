# Frontend npm-audit baseline consolidation (2026-10-31)

Consolidates the existing, scattered frontend dependency-security remediations into ONE
minimal, verified change on the current baseline (`claude/det-kommer-mer-879lcm`, tip
`64c07d5b0`), so the repo-wide `Frontend — npm audit` gate (`frontend/scripts/check-npm-audit.js`)
passes with **no major upgrade, no `--force`, no suppression, no blanket allowlist**.

This branch changes **only** `frontend/package.json` + `frontend/package-lock.json`. No code.
Author does **not** self-certify — independent examination required.

## 1. Existing remediation PRs examined (exact commits)
| PR | head @ SHA | base branch | what it does | reused here? |
|---|---|---|---|---|
| #248 | `cursor/frontend-npm-audit-browserslist-4-28-7` @ `2f47345` | **`claude/det-kommer-mer-879lcm`** (current baseline) | `next`→16.3.3, `eslint-config-next`→16.3.3, `browserslist` 4.28.7 (dep+override), overrides `sharp`→0.35.4, `js-yaml`→4.3.2 | **Yes** — its baseline-compatible dep set is the starting point. |
| #305 | `cursor/frontend-zod-ghsa-mrhx-91c1` @ `50ca651` | `cursor/agent-side-communication-bridge-91c1` (different base) | pin transitive `zod`→4.6.7 (GHSA-mrhx-c7xc-m3w5) | **Yes** — reused the `zod@4.6.7` pin as an override. |
| #275 | `codex/frontend-npm-audit-fc4-remediation` @ `eed3f4a` | `codex/founder-alpha-final-composed-candidate` (different base) | adds the `check-npm-audit.js` policy script + `ci.yml` gate + `SECURITY_BLOCKERS.md` + policy matrix | **No** — the gate script already exists on the current baseline; #275's policy files are on a different base/scope (would be scope creep). |
| #282 | `cursor/fc4-npm-audit-a6-a7-91c1` @ `1e3ee3f` | `codex/frontend-npm-audit-fc4-remediation` (stacked on #275) | security-policy correction to #275's matrix | **No** — same different-base lineage as #275. |

**Ancestry conflict:** only #248 descends from the current baseline. #275/#282/#305 are based on
other branches and **cannot be merged directly** onto the baseline. This consolidation therefore
reuses the *dependency decisions* (not the branches) from #248 and #305, applied onto the
baseline, plus the newer fixes required by today's advisories.

## 2. Today's high/critical advisories vs the reused fixes
Starting from the baseline gate (`node scripts/check-npm-audit.js`): **~21 unallowlisted
high/critical advisory sources** across 7 packages — `next` (critical), `sharp`, `brace-expansion`,
`browserslist`, `js-yaml`, `source-map-js`, `zod` (all high).

Resolved here (all **in-range / controlled-minor**, verified empirically):

| package | before | after | advisory | source |
|---|---|---|---|---|
| next | 16.2.11 | **16.4.3** (controlled MINOR within Next major 16) | next critical + transitive | needed by today's audit; #248 used 16.3.3 (predated the new next-critical) |
| eslint-config-next | 16.2.11 | 16.4.3 | — | match next |
| sharp (override) | 0.35.3 | **0.35.5** | GHSA-wq5f-xc86-pv6w (librsvg, CVE-2026-96889, <0.35.5) | newer than #248's 0.35.4 |
| browserslist (dep+override) | — | 4.28.7 | browserslist high | #248 |
| js-yaml (override) | — | 4.3.2 | js-yaml high | #248 |
| zod (override) | — | 4.6.7 | GHSA-mrhx-c7xc-m3w5 | #305 |
| source-map-js (override) | — | 1.2.2 | GHSA-68fv-2mgg-jv7q | new |
| brace-expansion | (vuln) | patched via `npm audit fix` (in-range, no `--force`) | brace-expansion high | new |

**next 16.4.3 is a MINOR upgrade (16.2→16.4, same major 16), not a major-version upgrade** — taken
deliberately and build/lint-verified (see §4), so it is a *controlled* change, not the forbidden
uncontrolled major.

## 3. Remaining advisories (tracked, NOT suppressed)
`npm audit` after this change: **0 high, 0 critical, 8 moderate.** The 8 moderate are the
eslint / typescript-eslint tooling chain plus `postcss` (GHSA-fxqj-rqcc-2cmp) and `minimatch`
(GHSA-g3hv-75wg-m249) — mostly dev-only. Per the **existing** policy (`check-npm-audit.js`:
moderate-and-below are tracked but non-blocking), these do not fail the gate. They are **not**
allowlisted or hidden — they simply sit below the high/critical blocking threshold, unchanged
from the baseline policy.

**Stale/expired exceptions:** the baseline allowlist in `check-npm-audit.js` still lists old
brace-expansion ids (1124334 / 1130588 / 1130591) for GHSA-mh99-v99m-4gvg. Those ids no longer
match the current audit, and brace-expansion is now patched by the bump, so **no allowlist entry
was added or relied on here**. Pruning the stale ids is a separate policy-hygiene task (out of
scope for this dependency consolidation).

## 4. Verification (this branch)
- **npm audit gate:** `node scripts/check-npm-audit.js` → *"no high/critical findings beyond the
  documented allowlist. OK."* (exit 0).
- **Dependency resolution + lockfile consistency:** `npm ci` succeeds from the regenerated
  `package-lock.json`.
- **Lint:** `npm run lint` → 0 errors (1 pre-existing warning, see note).
- **Build:** `npm run build` → success.
- `npm audit`: 0 high, 0 critical, 8 moderate.

**Note (separate, not fixed here):** the `next`/`eslint-config-next` 16.4.3 bump surfaces one new
ESLint **warning** (`@next/next/no-location-assign-relative-destination`) on pre-existing code in
`frontend/lib/api.ts:97` (`window.location.href = "/login"`). It is a warning (0 errors, does not
fail lint/build) on code this PR does not touch — a tiny code follow-up for its own branch, kept
out of this deps-only consolidation.

## 5. Scope / governance
Deps-only (package.json + lock). Does **not** modify or depend on #316–#318. Does **not** touch
#314/#315/RT3 or Codex/Cursor branches. Gate/policy preserved. No merge/deploy. Independent
review required before integration; the author does not self-certify.
