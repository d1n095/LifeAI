"""Founder Authority & Capability Core — independent acceptance (FAC-*). For Codex candidate
`e68e7f4` once its bundle is imported. Candidate-independent: each check discovers the relevant tables/modules and
reports BLOCKED (never PASS) when the capability is genuinely absent, so it is meaningful to run against ANY tree
(it will mostly BLOCK on a tree that lacks the core, and must turn green only on the real candidate).

Threat model (unchanged from security_kernel_v1): enforcement must hold independently of the model, i.e. of the
runtime DB role `mainai_app` that every web/worker/code path uses. Reuses FA-1..FA-16 from security_kernel_v1
(run that suite too); this module adds the NEW-core requirements and the legacy-writability regression (item 6).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from _harness import _privileges  # noqa: F401


def _tables(db):
    return {r[0] for r in db.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))}


def _priv(db, table, p):
    return bool(db.execute(text("SELECT has_table_privilege(current_user, :t, :p)"), {"t": table, "p": p}).scalar())


def _rls(db, table):
    row = db.execute(text("SELECT coalesce(relrowsecurity,false), coalesce(relforcerowsecurity,false) FROM pg_class WHERE relname=:t"), {"t": table}).first()
    return tuple(row) if row else (None, None)


def _find(db, *needles):
    t = _tables(db)
    return sorted(x for x in t if any(n in x for n in needles))


# ---- item 5 / FA-PRIV-NEW: signing keys, policy records, audit receipts protected independently of mainai_app
def test_FAC1_signing_keys_not_readable_or_writable_by_runtime(db_session):
    """Req 5: effective-policy signing keys must not be SELECT/UPDATE/DELETE-able by the runtime role."""
    keys = _find(db_session, "signing_key", "policy_key", "authority_key", "kek", "signing")
    keys = [t for t in keys if "key" in t]
    if not keys:
        pytest.fail("BLOCKED: no signing-key table found (capability absent or named unexpectedly; verify against the core)")
    bad = {t: [p for p in ("SELECT", "UPDATE", "DELETE", "INSERT") if _priv(db_session, t, p)] for t in keys}
    print(f"FAC1|signing_key_tables={bad}")
    assert all("SELECT" not in v and "UPDATE" not in v and "DELETE" not in v for v in bad.values()), (
        f"FAC-1 runtime role can read/modify signing keys: {bad} — effective policies are forgeable by the model")


def test_FAC2_new_authority_tables_are_not_runtime_writable(db_session):
    """Req 2/4/5: capability policies, autonomy ceilings, effective policies, grants must not be UPDATE/DELETE-able by runtime."""
    core = _find(db_session, "capability_polic", "autonomy_ceiling", "effective_polic", "authority_grant",
                 "founder_authority", "capability_grant", "authorization_grant")
    if not core:
        pytest.fail("BLOCKED: no new authority-core tables present (expected on commit e68e7f4; absent here)")
    report, viol = {}, {}
    for t in core:
        held = [p for p in ("SELECT", "INSERT", "UPDATE", "DELETE") if _priv(db_session, t, p)]
        report[t] = {"held": held, "rls": _rls(db_session, t)}
        bad = [p for p in ("UPDATE", "DELETE") if p in held]
        if bad:
            viol[t] = bad
    for t, i in report.items():
        print(f"FAC2|{t}: held={i['held']} rls={i['rls']}")
    assert not viol, f"FAC-2 runtime role can write new authority tables: {viol}"


def test_FAC3_immutable_autonomy_ceiling_cannot_be_raised(db_session):
    """Req 2: an autonomy ceiling must be immutable from the runtime — no UPDATE that raises it, enforced in the DB."""
    ceil = _find(db_session, "autonomy_ceiling", "autonomy_limit")
    if not ceil:
        pytest.fail("BLOCKED: no autonomy-ceiling table present")
    t = ceil[0]
    held = [p for p in ("UPDATE", "DELETE") if _priv(db_session, t, p)]
    guarded = db_session.execute(text("SELECT count(*) FROM information_schema.triggers WHERE event_object_table=:t AND event_manipulation IN ('UPDATE','DELETE')"), {"t": t}).scalar_one()
    print(f"FAC3|{t} runtime_write={held} guard_triggers={guarded}")
    assert not held or guarded, f"FAC-3 autonomy ceiling {t} is runtime-writable with no guard trigger: {held}"


def test_FAC4_audit_receipts_append_only_for_runtime(db_session):
    """Req 7: new audit/receipt tables must be insert-only for the runtime role (no UPDATE/DELETE)."""
    audits = _find(db_session, "audit", "receipt", "provenance", "authority_event", "policy_event")
    if not audits:
        pytest.fail("BLOCKED: no audit/receipt tables discovered")
    viol = {}
    for t in audits:
        bad = [p for p in ("UPDATE", "DELETE") if _priv(db_session, t, p)]
        guarded = db_session.execute(text("SELECT count(*) FROM information_schema.triggers WHERE event_object_table=:t AND event_manipulation IN ('UPDATE','DELETE')"), {"t": t}).scalar_one()
        if bad and not guarded:
            viol[t] = bad
        print(f"FAC4|{t}: forbidden_held={bad} guard_triggers={guarded}")
    assert not viol, f"FAC-4 audit/receipt tables rewritable/deletable by runtime with no guard: {viol}"


# ---- item 6: legacy tables must ALSO be protected, even if the new core is correct
LEGACY_FORBIDDEN = {
    "users": ["UPDATE", "DELETE"],
    "audit_log": ["UPDATE", "DELETE"],
    "workforce_authority_epoch": ["UPDATE", "DELETE"],
    "execution_authorization_envelopes": ["UPDATE", "DELETE"],
    "provider_spend_authorizations": ["UPDATE", "DELETE"],
    "mainai_goals": ["DELETE"],
}


def test_FAC5_legacy_authority_tables_also_hardened(db_session):
    """Item 6 / FA-PRIV regression: the security_kernel_v1 legacy finding (runtime can write users/audit_log/STOP/
    envelope/spend) must be closed too. A correct new core with legacy tables still writable is NOT a pass."""
    present = _tables(db_session)
    report, viol = {}, {}
    for t, forbidden in LEGACY_FORBIDDEN.items():
        if t not in present:
            continue
        held = [p for p in ("SELECT", "INSERT", "UPDATE", "DELETE") if _priv(db_session, t, p)]
        bad = sorted(set(held) & set(forbidden))
        report[t] = {"held": held, "rls": _rls(db_session, t), "forbidden_held": bad}
        if bad:
            viol[t] = bad
    for t, i in report.items():
        print(f"FAC5|{t}: held={i['held']} rls={i['rls']} FORBIDDEN_HELD={i['forbidden_held']}")
    assert not viol, (f"FAC-5 legacy authority/identity/audit tables remain runtime-writable: {viol} "
                      "(Security Kernel FA-PRIV finding not closed — the model can still tamper via the legacy surface)")


def test_FAC6_single_migration_head_0094(db_session):
    """Req 1/9: exactly one Alembic head, and it is 0094_founder_authority_core."""
    import os
    import sys

    backend = os.environ.get("TARGET_BACKEND")
    if not backend:
        pytest.skip("set TARGET_BACKEND=<candidate>/backend to check the migration head")
    sys.path.insert(0, backend)
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(os.path.join(backend, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(backend, "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    print(f"FAC6|heads={heads}")
    assert heads == ("0094_founder_authority_core",), f"FAC-6 expected single head 0094_founder_authority_core, got {heads}"


def test_FAC7_recall_identity_unchanged_and_inactive(db_session):
    """Req 9: the inactive Recall production identity must not have been activated or re-keyed by this core."""
    from app.config import get_settings
    from app.main import app

    recall_routes = [getattr(r, "path", "") for r in app.routes if "recall" in getattr(r, "path", "").lower()]
    settings = get_settings()
    activation = getattr(settings, "mainai_activation_mode", None)
    print(f"FAC7|recall_routes={recall_routes} activation_mode={activation}")
    assert recall_routes == [], f"FAC-7 Recall HTTP became mounted: {recall_routes}"
    assert activation is False, f"FAC-7 activation mode is on: {activation}"
