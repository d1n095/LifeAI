# FC-4 — inherited `Frontend — npm audit` failure: findings and remediation proposal

Examined 2026-10-12, read-only, on the integration base `2fbe20a` (`frontend/package.json` + `package-lock.json`).
Audits ran on a scratch copy with `npm audit --package-lock-only` (no install, no scripts). The same advisory IDs appear
in the real failing CI log of #267 (`Frontend — npm audit`, run 38207357469). No repo file, CI rule, lockfile or PR was changed.

## Why CI fails
The `npm-audit` job runs `frontend/scripts/check-npm-audit.js`, which fails on any high/critical advisory not in a dated, ID-scoped
allowlist (today only the old brace-expansion IDs 1124334/1130588/1130591). The aggregate `All required checks passed`
job `needs: npm-audit`, so it fails too. New advisories published since the last baseline turned every branch red.

## Findings (before)
Full tree: 14 (1 critical, 10 high, 3 moderate). **Production only (`--omit=dev`): 4 (1 critical, 2 high, 1 moderate).**

| Package | Scope | Advisory | Severity | Patched in | Exposure today |
|---|---|---|---|---|---|
| next 16.3.5 | **prod** | GHSA-vcvr-r3jv-pc5j RCE in `next/og` ImageResponse | critical | 16.3.6 | `next/og` not imported |
| next 16.3.5 | **prod** | GHSA-cjq9-62q9-8jv4 SSRF in Image Optimization (+5 moderate/low) | high | 16.3.8 | no `next/image`, no remote patterns; `/_next/image` endpoint still exists in the standalone server |
| sharp 0.35.4 | **prod** (optional, via next) | GHSA-wq5f-xc86-pv6w librsvg | high | 0.35.5 | image pipeline; **pinned vulnerable by `overrides.sharp`** |
| source-map-js 1.2.1 | **prod** (via next/postcss) | GHSA-68fv-2mgg-jv7q event-loop DoS | high | 1.2.2 | build/runtime tooling |
| postcss 8.5.19 | prod/dev | GHSA-fxqj-rqcc-2cmp | moderate | 8.5.23+ | **pinned vulnerable by `overrides.postcss` + direct pin** |
| brace-expansion 1.1.18 / 5.0.9 | dev | GHSA-qhr7-859c-m2p7, GHSA-6j4f-fj2g-mc7p (high), GHSA-q2hr-2g5m-vwhr (mod) | high | 1.1.21 / 5.0.12 | lint tooling |
| braces 3.0.3 (→ micromatch, fast-glob, chokidar, tailwindcss 3, @next/eslint-plugin-next, eslint-config-next) | dev | GHSA-vfj7-8cjw-p6xm stack-exhaustion DoS on deeply nested patterns | high | **none** (3.0.3 is the latest release) | build/lint globbing of repo files, never shipped in the bundle |
| postcss-selector-parser / postcss-nested (tailwind 3) | dev | GHSA-rj75-hqrm-r3gf | moderate | tailwind 4 | non-blocking (moderate) |

## Minimal remediation (fix, not suppress)
Package changes (see `proposed_package_json.diff`); all patch/minor, no major upgrade:
- `next` 16.3.5 → **16.4.0**; `eslint-config-next` 16.3.5 → **16.4.0**.
- Raise the stale pins that currently force vulnerable versions: `postcss` 8.5.19 → **8.5.29** (direct + override), `overrides.sharp` 0.35.4 → **0.35.5**.
- Add overrides: `source-map-js` **1.2.2**, `brace-expansion@^1` **1.1.21**, `brace-expansion@^5` **5.0.12**.

Result (lockfile resolution + audit on the scratch copy): **production 0 vulnerabilities**; full tree 9 (0 critical, 7 high, 2 moderate).
**Exactly one advisory source still fails the gate: 1240992 = braces GHSA-vfj7-8cjw-p6xm**, which has no patched release at all.
It is dev-only: Tailwind 3 (chokidar/fast-glob/micromatch) and `@next/eslint-plugin-next@16.4.0` (pins `fast-glob@3.3.1`).
A Tailwind 4 migration would not remove it (the ESLint chain remains), and a forked `braces` is not acceptable (the
commit-pinned fork in `codex/frontend-npm-audit-remediation` @ 1a3443f was rejected earlier).

For that single advisory, the existing policy mechanism applies: one dated, ID-scoped allowlist entry (`1240992`) in
`scripts/check-npm-audit.js` with a `docs/SECURITY_BLOCKERS.md` entry. Rationale: dev/build-time only; the input is the
repo's own glob patterns, not attacker-controlled; no fix exists. Removal trigger: a patched `braces` release, or
next/tailwind dropping the chain. Re-check weekly. All other findings are FIXED, not allowlisted.

Verification the builder must run (not done here: no install): `npm ci`, `npx tsc --noEmit`, `npx eslint .`,
`npm run build` (docker and vercel modes), Playwright E2E, and `node scripts/check-npm-audit.js`.
next 16.3.5 → 16.4.0 is a minor release, so check its release notes for config changes.
Supersedes stale PR #248 (`cursor/frontend-npm-audit-browserslist-4-28-7`, targets the old default branch).

## Which checks should gate MainAI's draft-PR workflow (FC-4 decision)
Keep every security check REQUIRED for merge. MainAI's CI wait should judge whether its OWN change broke something, not the base's health:
1. Evaluate component checks, not the derived aggregate `All required checks passed` (it only mirrors the components).
2. Attribute failures: a check failing on the PR head that also fails (or is absent) on the PR's base SHA is **inherited**.
   It does not fail the MainAI task. Only checks that pass on the base and fail on the head count as the change's failure.
3. Inherited or unrelated failures put the task into a non-retrying "blocked: inherited CI failure" state for Founder
   attention, never into retry/replan. Never trigger an automatic paid replan from CI failure. At most one bounded repair
   attempt, using the failing check's log, counted against the spend grant.
4. Relevant gating set for a backend-only change: backend unit/integration, RLS & session-security, Alembic migration
   check, account lifecycle. Add frontend TS/ESLint/build and npm audit when `frontend/` changes; E2E for user-facing paths.

This keeps security gates intact for merges while preventing expensive AI loops on failures MainAI did not cause.

## Recommended order
1. Founder: approve the dependency PR scope above plus the single braces allowlist entry (policy decision).
2. Dependency PR (any builder; small, frontend-only) on the integration base: changes above, full frontend verification. CI green except nothing.
3. Path A batch (Cursor, B1): CI-wait attribution against the base SHA plus the no-replan-on-CI rule, with regressions E2E-C (inherited failure) and a
   head-only failure case.
