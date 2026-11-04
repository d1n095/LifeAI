"""Helper script for test_scenario_7_restart_between_operations_no_process_local_state
(test_kill_switch_authority_epoch_race.py). Run as a FRESH, SEPARATE Python process -- not
imported by the test suite -- so it shares zero in-memory Python state (no module globals,
no cached objects, nothing) with the parent test process that committed the kill-switch
stop. This is the most faithful available in-process-test approximation of "a stop
committed by one worker process must be enforced by a brand new worker process that starts
up afterward with no memory of it": if the fix genuinely depends only on Postgres state and
not on anything process-local, this subprocess -- which imports the app package for the
first time in its own interpreter -- must still see the stop and refuse the grant.

Usage: python _kill_switch_restart_subprocess_helper.py <owner_id> <verifier_profile_id>
Prints exactly one line: "REFUSED" | "GRANTED:<assignment_id>" | "ERROR:<repr>", then
"PROVEN:<True|False>" for prove_no_reusable_live_authority on a second line.
Inherits DATABASE_URL/APP_DATABASE_URL/etc. from the parent process's environment (already
resolved by the parent test process's conftest.py) -- connects to the SAME test database,
just via a brand new connection/session/interpreter.
"""

from __future__ import annotations

import sys
import uuid

from app.db import migration_engine
from app.workforce import (
    KillSwitchError,
    prove_no_reusable_live_authority,
    resolve_delegation,
    submit_delegation_request,
)


def main() -> None:
    owner_id = uuid.UUID(sys.argv[1])
    verifier_profile_id = uuid.UUID(sys.argv[2])

    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=migration_engine)()
    try:
        try:
            req = submit_delegation_request(
                session,
                owner_id=owner_id,
                goal_text="post-restart grant attempt",
                required_capability="low_risk_classification",
                verification_requirement="independent_verifier",
            )
            asg = resolve_delegation(session, owner_id=owner_id, request=req, verifier_profile_id=verifier_profile_id)
            session.commit()
            print(f"GRANTED:{asg.id}")
        except KillSwitchError:
            session.rollback()
            print("REFUSED")
        except Exception as exc:  # noqa: BLE001 - surfaced to the parent test, not swallowed
            session.rollback()
            print(f"ERROR:{exc!r}")

        proven = prove_no_reusable_live_authority(session, owner_id=owner_id)
        print(f"PROVEN:{proven}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
