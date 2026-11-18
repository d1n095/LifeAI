"""Read-only access to `SystemActivationAuthorization` evidence -- see that model's own
docstring (app/models/agent_coordination.py) for why this module deliberately has no
authorize_*/revoke_* function: no application code path may ever create or revoke a row in
`system_activation_authorizations` (enforced at the database privilege level, migration
0069 -- `mainai_app` holds only SELECT). This module's entire job is to answer "is there
currently valid authorization evidence," failing closed on every ambiguous case."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.agent_coordination import SystemActivationAuthorization


def current_system_activation(db: Session) -> SystemActivationAuthorization | None:
    """Returns the current, valid authorization row, or `None` when authorization is missing,
    revoked, or expired -- the caller treats all three identically (fail closed), never
    distinguishing "no row exists" from "a row exists but is stale." Deliberately does not
    cache: a caller re-invoking this after a founder revokes or an expiry lapses sees the
    change immediately, satisfying the "concurrent changes and stale authorization" scenario
    the authority audit flagged."""
    return db.execute(
        select(SystemActivationAuthorization)
        .where(
            SystemActivationAuthorization.status == "active",
            (SystemActivationAuthorization.expires_at.is_(None)) | (SystemActivationAuthorization.expires_at > datetime.utcnow()),  # noqa: DTZ003 -- naive UTC, matching this column's own storage convention (see the model)
        )
        .order_by(SystemActivationAuthorization.authorized_at.desc())
        .limit(1)
    ).scalar_one_or_none()
