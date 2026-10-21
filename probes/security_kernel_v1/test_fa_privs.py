"""FA-PRIV: effective privilege inventory of the runtime role on authority/identity/audit tables.
The Security Kernel must not be enforceable only in the application layer (require_founder); the runtime DB role
(mainai_app — the identity of every model-driven code path) must not hold the privileges that let it tamper.
Reported as data; the assertions below are the enforced requirements."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from _harness import _privileges  # noqa: F401

# table -> privileges the runtime role must NOT hold (write access to someone else's authority = tamper surface)
FORBIDDEN = {
    "execution_authorization_envelopes": ["UPDATE", "DELETE"],
    "provider_spend_authorizations": ["UPDATE", "DELETE"],  # revoke goes through a function, not a blanket UPDATE
    "users": ["UPDATE", "DELETE"],
    "audit_log": ["UPDATE", "DELETE"],
    "workforce_authority_epoch": ["UPDATE", "DELETE"],
    "mainai_goals": ["DELETE"],
}


def test_FA_PRIV_runtime_role_cannot_write_authority_identity_or_audit(db_session):
    role = db_session.execute(text("SELECT current_user")).scalar_one()
    report, violations = {}, {}
    for table, forbidden in FORBIDDEN.items():
        held = [p for p in ("SELECT", "INSERT", "UPDATE", "DELETE")
                if db_session.execute(text("SELECT has_table_privilege(:r, :t, :p)"), {"r": role, "t": table, "p": p}).scalar()]
        rls = db_session.execute(text("SELECT coalesce(relrowsecurity,false), coalesce(relforcerowsecurity,false) "
                                      "FROM pg_class WHERE relname=:t"), {"t": table}).first()
        bad = sorted(set(held) & set(forbidden))
        report[table] = {"held": held, "rls": tuple(rls) if rls else None, "forbidden_held": bad}
        if bad:
            violations[table] = bad
    print(f"FA_PRIV|role={role}")
    for t, info in report.items():
        print(f"FA_PRIV|{t}: held={info['held']} rls={info['rls']} FORBIDDEN_HELD={info['forbidden_held']}")
    assert not violations, (f"FA-PRIV runtime role '{role}' holds tamper privileges on authority/identity/audit tables: "
                            f"{violations}. Enforcement is application-layer only (require_founder); a prompt-injected or "
                            f"buggy code path running as this role can widen envelopes, raise spend ceilings, clear STOP, "
                            f"lock out or re-key the Founder, and rewrite audit receipts.")
