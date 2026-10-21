# Certifying an integration candidate (exact SHA)

`606b592bdbfba77cf0858163910ef9c5748ce8ea` (Codex unified integration) is **not accessible** as of 2026-10-21:
- The public API returns `422` for the commit.
- An anonymous fetch says `not our ref`.
- No branch or PR carries it.

The examiner does not read other agents' private application data or credential stores.

Its reported "3,305 passed, 1 skipped" is **not** certification evidence.

## Safe, authorized ways to make it examinable (either is enough)

1. **Publish it.** The owner pushes a branch or draft PR with their own authorized access. The examiner then runs:
   ```
   probes/certify_v1/intake.sh 606b592bdbfba77cf0858163910ef9c5748ce8ea
   ```
2. **Hand over a git bundle.** No network or credentials are involved, and the SHA is verified on intake. In the candidate repo:
   ```
   git bundle create codex-606b592.bundle <branch>
   ```
   Place the file in a folder this session can read, then:
   ```
   probes/certify_v1/intake.sh 606b592bdbfba77cf0858163910ef9c5748ce8ea /path/codex-606b592.bundle
   ```

## What intake does

1. **Intake and exact SHA.** It runs `git bundle verify` or does an anonymous fetch with an isolated config, no credential helper and no lazy fetch. It then requires the exact 40-char SHA to be present.
2. **Ancestry report.** It reports whether the candidate contains #293, #292, #291 and #280. A combined candidate missing the Cursor fixes fails the Cursor-owned gates on its own.
3. **Frozen worktree.** It creates a detached worktree, and the frontend is built in a scratch copy.
4. **Combined profile.** It runs `run_acceptance.sh combined`, where every suite is required:
   - stop_classification (FO-1..FO-7, **FO-6** P1, recovery bound, concurrent STOP vs draft PR, erasure after a full cycle);
   - credential canary + transport contract (no host helpers or askpass);
   - review/STOP-before-merge;
   - task authority / publication history (TA-12, TA-13, PUB-2);
   - spend;
   - authorization_grants (#280 matrix) + grant_contract GC-1..GC-8 (one-use, replay, concurrency, expiry, cancel, exact binding, distinct draft-PR grant, no re-issue without fresh preflight, typed denial);
   - migration integrity (single head, no dual 0089);
   - app_readiness AR-1..AR-17 (D1-D5, N1-N4, P-3);
   - proxy PX-1..PX-9 (P-1, P-2);
   - UI-1..UI-3 Playwright;
   - candidate regression;
   - FC-4;
   - exact-SHA CI (12 checks × push + pull_request).

   A candidate that is never published cannot pass the exact-SHA CI row.
5. **GO/NO-GO matrix.** `gonogo.py` produces it. Class-F Founder/GitHub rows stay **NO-GO (not attested)** until attested separately. Overall GO is never implied by a green executable gate.
