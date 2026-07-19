# Current Status — verified 2026-07-19
**Source:** Direct repository/CI inspection, this FKP v1.1 pass. Replaces FKP v1.0's `CURRENT_STATUS.md`, which described `savings-story-scanner` status.
**Status:** CONFIRMED, not reported-only.

---

## Where things stand right now

**Deploy branch (`claude/det-kommer-mer-879lcm`, tip `f0a1975`):** a working Life OS product — auth, RAG chat, documents, projects, admin — deployable as a single free Render container, with public registration still enabled on this branch. CI-green history. Not deployed to production yet; `autoDeploy: false`, deploy is CI-gated and founder-approved.

**Founder-only launch (`claude/founder-only-launch`, tip `e9b4b76`, based on `bf31fad`):** a complete, separately built and independently re-reviewed implementation of Step 1 — MainAI locked to a single founder identity, public registration blocked server-side, all MainAI-surface routes founder-gated. **CI run for the review-fix commit `e9b4b76` completed successfully.** This branch is ready to merge in the sense that every automated check is green and an independent second-pass review has been completed and its findings fixed on the branch. It has **not** been merged into the deploy branch, and nothing has been deployed. Both of those remain founder-approval actions per the standing instruction.

## What the independent review (this FKP v1.1 cycle) found and fixed on `claude/founder-only-launch`

1. **Real bug — migration rollback would abort against any live database with a founder row present.** `backend/alembic/versions/0005_founder_role.py`'s `downgrade()` recreated the `userrole` enum without the `'founder'` value and cast the column with `USING role::text::userrole` — Postgres aborts that cast outright if any row still holds `'founder'`, which it always will once `app/bootstrap.py` has run. Fixed by reassigning `role='founder'` rows to `'admin'` before the enum swap. Verified empirically: inserted a `role='founder'` row into a real disposable Postgres container, confirmed the downgrade failed before the fix and succeeded after.
2. **Stale documentation** — `README.md`, `docs/RENDER_DEPLOY.md`, `docs/OPERATIONS.md`, `docs/MAINAI_0.1_PLAN.md` still described the pre-founder-only `ADMIN_EMAIL`/`ADMIN_PASSWORD`/self-registration model. Fixed: all four now describe the founder-only model, correct environment variable names, and correct production verification steps.
3. **Checked and found no issues:** no remaining `ADMIN_EMAIL`/`require_admin`/`bootstrap_admin_user` references anywhere in the codebase; no hardcoded secrets in the review diff; `app/rls.py` untouched; `render.yaml` consistently renamed to `FOUNDER_EMAIL`/`FOUNDER_PASSWORD`; combined-container CI job unaffected.
4. **Local build limitation, not a defect:** the real `Dockerfile.combined` build cannot run in this sandbox (`apt-get`/`deb.debian.org` returns 403 here) — combined-container verification relies on the dedicated GitHub Actions job, which has full network access and already exercises this path in CI.

## What this FKP v1.1 pass produced (docs-only, separate branch `claude/fkp-v1.1`, based on the deploy branch)

- A corrected knowledge package replacing FKP v1.0's conflated `savings-story-scanner`/LifeAI material with a verified LifeAI inventory (`03_ARCHITECTURE/`, `04_PRODUCT_AND_MODULES/`, this folder).
- The review-overlay material (audit, corrected bootstrap, candidate requirements) and the external "conversation register" (domain/requirement map, phase-gate structure) integrated into the package structure, not left as separate loose files.
- An alternatives/risk/cost/dependency analysis for OD-02, OD-03, OD-04, OD-17 (`02_DECISIONS/OD_02_03_04_17_ANALYSIS.md`) — analysis only, no implementation, no decision made on the founder's behalf.
- A prioritized dependency staircase from founder-login to knowledge import to project memory (`09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md`).
- A corrected, checksummed manifest listing only files actually present in this package (fixes FKP v1's self-inconsistent manifest, audit finding K-04).

## What has NOT happened (explicitly, per standing instructions)

- Nothing has been merged. `claude/founder-only-launch` remains unmerged into the deploy branch.
- Nothing has been deployed. Render has not been touched, configured, or triggered.
- None of OD-02/03/04/17 has been implemented — analysis only.
- No secrets have been requested, displayed, or handled in this session.
