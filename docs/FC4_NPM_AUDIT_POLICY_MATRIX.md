# FC-4 npm-audit policy matrix (isolated A6/A7/A10/A11 correction)

Parent / frozen #275 SHA: `eed3f4a380be71398a0481414518a29506a57ccb`
Independent exam of that SHA: `6d56ff029328c542a77bd6cccbaebdaf16124539`

## Before (frozen #275 policy)

| Probe | Result on `eed3f4a` |
|---|---|
| A0 exact dev-only braces advisory (major-only fix) | HOLDS |
| A1 another advisory on braces cannot inherit | HOLDS |
| A2 production occurrence fails | HOLDS |
| A3 different high dev advisory fails | HOLDS |
| A4 same source re-rated critical fails | HOLDS |
| A5 range rewritten after a patch fails | HOLDS |
| A6a `fixAvailable: true` | GAP (P1) |
| A6b object `{braces 3.0.4, isSemVerMajor:false}` | GAP (P1) |
| A6c object `{micromatch 4.0.9, isSemVerMajor:false}` | GAP (P1) |
| A7 expiry after 2027-01-15 | GAP (P1) |
| A8 unattributed high node | HOLDS |
| A9 via-cycle fail-closed | HOLDS |
| A10 independent moderate on high-via-braces wrapper | GAP (P2) |
| A11 object-shaped non-major wrapper fix vs major-only root | GAP (P2) |
| R10 real npm 10.9.7 lock | HOLDS |
| R11 real npm 11.19.1 lock | HOLDS |

## After (this correction, same lock)

Claude's original `adversarial_policy.test.js` against this `evaluateAudits`:

| Probe | Result |
|---|---|
| A0 | HOLDS |
| A1 | HOLDS |
| A2 | HOLDS |
| A3 | HOLDS |
| A4 | HOLDS |
| A5 | HOLDS |
| A6a | HOLDS |
| A6b | HOLDS |
| A6c | HOLDS |
| A7 | HOLDS |
| A8 | HOLDS |
| A9 | HOLDS |
| A10 | HOLDS |
| A11 | HOLDS |
| R10 | HOLDS |
| R11 | HOLDS |

Additional executable coverage in `frontend/scripts/check-npm-audit.test.js`: malformed/missing expiry, date-only UTC comparison before/on/after 2027-01-15, injectable clock, incomplete fix metadata, dependency cycles, npm 10 boolean-true vs npm 11 major object on `postcss-nested`, production introduction, dev-to-production movement, ID/title/range/source variation, unknown fix availability, duplicated advisories, invalid JSON, unexpected report shapes, exception masking.

## Audit evidence (lock unchanged from #275)

npm 10.9.7:

- `npm audit --omit=dev`: 0 vulnerabilities
- full tree: 9 findings (7 high, 2 moderate); every high roots in `braces` source 1240992 with major-only `tailwindcss@4.3.3` or `eslint-config-next@14.2.35`
- `postcss-nested.fixAvailable === true` (boolean); root `postcss-selector-parser` is major-only

npm 11.19.1:

- `npm audit --omit=dev`: 0 vulnerabilities
- full tree: same 9 findings
- `postcss-nested.fixAvailable = { name: "tailwindcss", version: "4.3.3", isSemVerMajor: true }`

Preserved remediations (no lock edits in this PR):

- `next` / `eslint-config-next` 16.3.8
- `postcss` 8.5.29
- `sharp` 0.35.5
- `source-map-js` 1.2.2
- `brace-expansion@^1` 1.1.21 / `brace-expansion@^5` 5.0.12
- Tailwind 3.4.4 remaining dev-only moderates: tracked, major-only, not excepted by GHSA-vfj7-8cjw-p6xm
