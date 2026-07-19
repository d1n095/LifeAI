"""Founder-only launch: adds 'founder' to the userrole enum (backend/app/models/user.py).

MainAI is a Founder AI, not a shared or per-user assistant (see
docs/FOUNDER_KNOWLEDGE_BOOTSTRAP.md) — the single founder account is provisioned with a
fixed primary key (app/founder.py's FOUNDER_USER_ID) and this role by app/bootstrap.py's
bootstrap_founder_user(), and every protected route checks both via app/deps.py's
require_founder(). admin/member are kept in the enum (unused today — public registration is
disabled, see app/routers/auth.py's register()) for the future UserAI phase, not removed.

ALTER TYPE ... ADD VALUE is safe to run inside a transaction on Postgres 12+ (this project
targets pg16, see docs/RENDER_DEPLOY.md) as long as the new value isn't used in the same
transaction, which this migration doesn't do.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-19
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE userrole ADD VALUE 'founder';")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE — the standard workaround is to recreate the
    # enum type without the value and repoint the column at it. Only safe if no row is
    # actually using role='founder' at downgrade time (the founder account always exists in
    # any deployed environment, so this downgrade is expected to be run against an empty/
    # test database, not a live one — same caveat as any narrowing enum downgrade).
    op.execute("""
        ALTER TYPE userrole RENAME TO userrole_old;
        CREATE TYPE userrole AS ENUM ('admin', 'member');
        ALTER TABLE users ALTER COLUMN role TYPE userrole USING role::text::userrole;
        DROP TYPE userrole_old;
    """)
