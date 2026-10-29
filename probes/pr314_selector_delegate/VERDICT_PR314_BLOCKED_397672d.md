# Examination A — #314 Selector / Delegation Authority — **BLOCKED (artifact inaccessible)**

- **Builder:** Codex
- **Branch (declared):** `codex/pr314-selector-delegate-authority-hardening`
- **Exact commit to examine (declared):** `397672d84eb8fe48782018b62f682bc830de97ff`
- **Parent (declared):** `3aa2bec84665db3225c4fc0f99598d56747a0f7e`
- **Archive (declared):** `pr314-selector-delegate-hardening-397672d.tar.gz`
- **Expected archive SHA-256 (declared):** `db0bd2d9af2bc7c319d385f64cdcecdccf27a4036ff9b6aba931bb4e1108e492`

## Verdict: **BLOCKED** — the authoritative artifact cannot be located or verified.

Independent examination cannot begin because the commit to be examined is not obtainable and
its integrity cannot be established. No test, adversarial probe, or verdict on the eight
required properties is possible without the actual tree. Per the handoff governance, I mark this
**BLOCKED with precise missing evidence** and do **not** substitute any older public PR head or
the parent commit.

## Precise missing evidence (what was checked, 2026-10-29)
1. **Archive not found.** `pr314-selector-delegate-hardening-397672d.tar.gz` is absent from every
   checked handoff location — the session workspace root, `examiner/`, `examiner/patches/`,
   `examiner/docs/`, `examiner/handoff/`, `handoff/`, `~/Downloads`, `~/Desktop`, `~/Documents`,
   `/tmp`, `/var/tmp`, `~/codex`, and a recursive scan of the home tree. No `*.tar.gz` matching
   `pr314*` exists anywhere searched. → **SHA-256 `db0bd2d9…` cannot be computed or verified.**
2. **Exact commit missing from git.** `git cat-file -t 397672d84eb8fe48782018b62f682bc830de97ff`
   → object not present locally. `git rev-parse --verify 397672d84…^{commit}` consulted the
   remote and returned `upload-pack: not our ref` — the commit is **not on origin either**.
3. **Branch absent.** `codex/pr314-selector-delegate-authority-hardening` exists in neither the
   local branches nor the remote-tracking refs.
4. **Parent present but NOT substituted.** Only the declared parent
   `3aa2bec84…` ("docs: name authorized-cycle successor as #314") exists locally — a naming/docs
   commit, not the implementation. It is recorded here for traceability and is **not** used as a
   stand-in for the examined SHA.

## Required-report fields
1. **Exact examined SHA / verified identity:** none — `397672d84…` unobtainable; identity
   UNVERIFIED (archive absent, commit absent locally and on origin, branch absent).
2. **Verdict:** **BLOCKED.**
3. **Executable adversarial results:** none runnable (no tree).
4. **Reproducible failure evidence:** n/a.
5. **Regression coverage / remaining risks:** cannot assess. The eight required properties
   (capability-based selection accepted by the authoritative assignment path; no provider
   namespace confusion / fallback authority bypass; delegation always through the authorized
   broker; direct service assignment cannot circumvent grants/delegation; retry reuses only the
   exact authorized assignment; revoked/expired/superseded/missing authority fails closed;
   concurrent assignment/event paths cannot bypass authority; STOP + spending controls intact)
   remain **UNVERIFIED**. The previously documented #314 selector-namespace / broker-bypass
   defects (from the 2308b697 lineage) remain open and are **not** cleared by this blocked exam.
6. **Examiner-only record:** this file + REPORTS.md row (examiner orphan branch; no product code).
7. **Integration eligibility:** **NOT eligible.** A candidate whose artifact cannot be located or
   its integrity verified cannot be certified for integration. Remains gated.

## To unblock
Provide the archive `pr314-selector-delegate-hardening-397672d.tar.gz` at a stated path (so its
SHA-256 can be matched to `db0bd2d9…`), or make commit `397672d84…` fetchable so its git tree +
parent (`3aa2bec84…`) can be verified. Then this examination proceeds with adversarial + regression
probes.

Read-only examiner; no product branch modified; no publication/deploy/autonomy GO; no paid calls.
`first_cycle_go=false`. This BLOCK is an artifact-integrity block, distinct from the first-cycle
matrix's existing 9 FAIL / 1 BLOCKED and from the builder's 9 declared test xfails.
