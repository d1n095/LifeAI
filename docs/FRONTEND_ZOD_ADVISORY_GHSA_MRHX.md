# Frontend zod advisory GHSA-mrhx-c7xc-m3w5

Isolated dependency-security child of unfrozen `#304` @
`859f5ec5bf0eea4662b43caa061ff67967bba3aa`. Do **not** amend frozen `#303`
(`74ebda974ff6952c7c7ca15f837430a73c594b43`) or the `#304` transport commits.

This is not a second messaging system and not a policy change.

## Finding

`Frontend — npm audit` fails on `#304` because the lockfile resolves
`zod@4.4.3` as a **dev** transitive of `eslint-config-next@16.4.2` →
`eslint-plugin-react-hooks@7.1.1`. Direct frontend dependencies remain
`next` / `react` / `react-dom` only.

Advisory [GHSA-mrhx-c7xc-m3w5](https://github.com/advisories/GHSA-mrhx-c7xc-m3w5)
(`1243066`, CVE-2023-54404): affected `< 4.6.6`, patched `4.6.6`. npm reports
`fixAvailable: true`. `eslint-plugin-react-hooks@7.1.1` and
`zod-validation-error@4.0.2` both accept `zod` `^3.25.0 || ^4.0.0`.

## Fix

Pin `overrides.zod` to `4.6.7` (latest 4.6.x that includes the patched floor).
The audit script is unchanged. No new exception. The existing braces exception
(`GHSA-vfj7-8cjw-p6xm` until 2027-01-15) is untouched.

Remaining `npm audit` moderates are `postcss-selector-parser` via Tailwind 3,
whose npm-suggested fix is Tailwind 4 (`isSemVerMajor: true`). Those stay
major-only and are not newly excepted.

## Integration

1. Frozen `#303` @ `74ebda9` — do not amend
2. Unfrozen `#304` @ `859f5ec` — transport + CF-1; do not rewrite
3. This branch on top of `#304` — certification candidate
4. Freeze only after both required workflows are green on this SHA and Claude
   re-exams it
