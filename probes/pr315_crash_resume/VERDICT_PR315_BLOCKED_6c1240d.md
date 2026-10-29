# Examination B — #315 Crash / Resume File Integrity — **BLOCKED (artifact inaccessible)**

- **Builder:** Codex
- **Branch (declared):** `codex/pr315-crash-resume-integrity`
- **Exact commit to examine (declared):** `6c1240d8dc4f5b707553c1894b0a57e1dd9d915e`
- **Parent (declared):** `0741a3d5d7bda27f895fc382e1b4c8b1971aaa44`
- **Archive (declared):** `pr315-crash-resume-integrity-6c1240d.tar.gz`
- **Expected archive SHA-256 (declared):** `38d47311db0d7d75fa351059249a083d466680acc2999f52e3dc925441b7731a`

## Verdict: **BLOCKED** — the authoritative artifact cannot be located or verified.

Same disposition as Examination A: the commit to be examined is unobtainable and its integrity
cannot be established, so none of the ten required durability/integrity properties can be
independently verified. Marked **BLOCKED with precise missing evidence**; no older PR head or the
parent commit is substituted.

## Precise missing evidence (checked 2026-10-29)
1. **Archive not found.** `pr315-crash-resume-integrity-6c1240d.tar.gz` is absent from every
   checked handoff location and from a full recursive scan of `/Users/dennistorildson` (0 hits).
   → **SHA-256 `38d47311…` cannot be computed or verified.**
2. **Exact commit missing from git.** `git cat-file -t 6c1240d8dc4f5b707553c1894b0a57e1dd9d915e`
   → not present locally; `git rev-parse --verify 6c1240d8…^{commit}` → remote
   `upload-pack: not our ref` (**not on origin either**).
3. **Branch absent.** `codex/pr315-crash-resume-integrity` is in neither local nor remote-tracking
   refs.
4. **Parent present but NOT substituted.** Only `0741a3d5d…` ("docs: name large-file resumable
   HTTP successor as #315") exists locally — a naming/docs commit, not the implementation.
   Recorded for traceability only.

> Note: untracked patch files from PRIOR sessions exist in this examiner worktree
> (`0001-examiner-315-ACTUAL-HTTP-integration-acceptance-*.patch`,
> `0001-examiner-315-crash-consistency-O-1-escalated-to-CONF.patch`). Those concern an EARLIER
> #315-related candidate, NOT commit `6c1240d8…`, and are explicitly **not** used as a substitute
> for the artifact under examination.

## Required-report fields
1. **Exact examined SHA / verified identity:** none — `6c1240d8…` unobtainable; identity UNVERIFIED.
2. **Verdict:** **BLOCKED.**
3. **Executable adversarial results:** none runnable (no tree). In particular the "previously
   demonstrated silent corruption case" cannot be re-exercised against this commit.
4. **Reproducible failure evidence:** n/a.
5. **Regression coverage / remaining risks:** cannot assess. All ten required properties remain
   **UNVERIFIED** — spool durably synced before manifest advancement; uncommitted crash tails
   reconciled; manifest-ahead-of-spool fails closed; exact Upload-Offset enforcement; concurrent
   same-offset writes cannot corrupt; duplicate/replayed parts rejected; manifest/session/owner
   identity binding; physical file-size validated before finalize; crash injection at durability
   boundaries; no regressions in account erasure/quotas/RLS/Founder authorization.
6. **Examiner-only record:** this file + REPORTS.md row (examiner orphan branch).
7. **Integration eligibility:** **NOT eligible** — artifact unverifiable; remains gated.

## To unblock
Provide `pr315-crash-resume-integrity-6c1240d.tar.gz` at a stated path (to match SHA-256
`38d47311…`), or make commit `6c1240d8…` fetchable so its tree + parent (`0741a3d5d…`) verify.

Read-only examiner; no product branch modified; no publication/deploy/autonomy GO; no paid calls.
`first_cycle_go=false`. Artifact-integrity block, distinct from the first-cycle matrix's existing
9 FAIL / 1 BLOCKED and from the builder's 9 declared test xfails.
