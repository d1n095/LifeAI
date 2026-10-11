# Founder Sovereignty P1-fix-2 adversarial suite (examiner-owned, never committed)

Target examined 2026-10-11: 4ccf1ed0aeb0ea35301a38437b57e097c16f435d (worktree wtfs3, detached; unchanged after every run).
Run: `./run.sh <target-worktree>`. It loads the target conftest as a plugin; `db_session` = mainai_app after startup narrowing.
Grant parity with a production boot-order DB (ensure_app_role -> alembic -> apply_runtime_privileges -> startup) is checked (ENV row).
Output: `MATRIX|id|HOLDS/GAP|evidence`. Non-security errors = PROBE_ERROR. Races use separate sessions/threads.

Result on 4ccf1ed: every P1-1..P1-5 attack row HOLDS except the R:* root-of-trust chain (GAP).
Runtime can UPDATE users.password_hash and INSERT refresh_tokens, which lets it forge reauth -> step-up -> unlock.
diag/: read-only plugin proving the builder's order-dependent CI failure is a leaked current_user_id contextvar
(after_begin re-applies it inside the savepoint) -> UPDATE matched 0 rows; the head stayed locked.
