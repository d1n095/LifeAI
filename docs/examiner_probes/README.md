# Examiner probes — coordination certification (#300/#301/#303/#314)

Independent, examiner-owned adversarial tests used to certify the coordination PRs. They are
reference evidence, NOT part of any candidate and NOT run by CI. To reproduce a verdict, copy the
matching probe into a detached worktree checked out at that PR's exact frozen SHA and run it with
the Python 3.12 test venv against local Postgres, e.g.:

    git worktree add --detach /tmp/exam300 <FROZEN_SHA_300>
    cp docs/examiner_probes/examiner_probe_300.py /tmp/exam300/backend/tests/backend/
    cd /tmp/exam300/backend && LIFEAI_TEST_DATABASE_NAME=exam300 python -m pytest tests/backend/examiner_probe_300.py -v -s

Frozen SHAs: #300 2c9dd7e0 · #301 a786dac9 · #303 74ebda97 (body; head drifted to 296de97f) · #314 3aa2bec8.
See ../COORDINATION_CERTIFICATION_300_301_303_314.md for the full verdicts and evidence.
