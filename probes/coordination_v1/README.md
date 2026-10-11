# Coordination store security examination (examiner-owned, never committed)

Target: shared Founder Alpha base 2fbe20aacf1203fc0e16d216ef55b666b2181619 (examiner worktree `wtbase2`, detached).
Run: `./run.sh <target-worktree>`. It loads the target's own `tests.conftest` as a plugin, so `db_session` is the
real `mainai_app` role after startup privilege narrowing. It attacks with raw SQL and drives the service with
real two-session thread races, then checks that the target worktree is unchanged.
Output is a matrix: `MATRIX|id|DB/SVC|PASS/FAIL|evidence`. Tests never raise on FAIL, so the full matrix is
always printed. Unexpected SQL errors and 0-row matches are reported as PROBE_ERROR, never as PASS.

Baseline 2026-10-10 on 2fbe20a: DB 7 PASS / 20 FAIL; SVC 3 PASS / 2 FAIL (incl. sequential control S4c) (S2 alias, S4 race 5/5).
When the store is hardened, convert the FAIL rows into hard assertions.
