"""#306 historical ledger repair — examiner-owned tests for repair_misclassified_backing (RP-*).

Contaminates records exactly as CF-2 did (stamps ledger_backing on a REAL assignment, top-level + nested dump), then
verifies the repair: strips both markers, preserves genuine synthetic rows, erases nothing else, is idempotent and
restart-safe, fails safely on malformed provenance, keeps history reconstructable, and that an untrusted
created_without_manual_assignment marker cannot silently turn a real assignment synthetic. Read-only vs target.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401


def _rls(s, o):
    from app.request_context import current_user_id as current_user_id_var
    current_user_id_var.set(str(o)); s.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": str(o)})


def _seed(db, owner, key="cursor", role="builder"):
    from app.agent_coordination.service import create_work_assignment, register_agent
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
    from app.models.mainai_execution import MainAITask
    _rls(db, owner)
    g = create_goal(db, owner_id=owner, title=key, original_instruction="x", created_by="founder", approval_policy="autonomous_development_work")
    p = create_plan(db, goal=g, rationale="r", created_by="founder", tasks=[PlannedTaskSpec(description="d", task_type="repo_edit", target_files=["docs/x.md"])])
    t = db.query(MainAITask).filter_by(plan_id=p.id).one()
    a = register_agent(db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", display_name=key, adapter_kind="cli", supports_write=(role == "builder"))
    wa = create_work_assignment(db, owner_id=owner, goal_id=g.id, task_id=t.id, agent_id=a.id, role=role,
                                read_write_mode="read_write" if role == "builder" else "read_only",
                                repository_identity="lifeai", allowed_paths=["docs/**"], requested_by="founder")
    db.commit()
    return wa


def _contaminate(db, row, *, real_history=True):
    """Reproduce CF-2 contamination on a REAL assignment: ledger_backing at top level AND inside the durable dump,
    alongside genuine historical evidence that must survive."""
    from app.agent_coordination.persist import LEDGER_BACKING_KEY
    from sqlalchemy.orm.attributes import flag_modified
    prov = dict(row.provenance or {})
    prov[LEDGER_BACKING_KEY] = True
    prov["key"] = "real-work"  # unrelated provenance that must be preserved
    dump = {LEDGER_BACKING_KEY: True, "obligations": [{"assignment_id": str(row.id), "status": "completed"}],
            "delivery": {"messages": {"m1": {"message_id": "m1", "status": "completed"}}}} if real_history else {LEDGER_BACKING_KEY: True}
    prov["durable_coordination"] = dump
    row.provenance = prov
    flag_modified(row, "provenance")
    db.flush()


# ---- RP-1: repair strips BOTH top-level and nested ledger_backing from a real assignment
def test_RP1_strips_both_markers(superuser_db, make_verified_user):
    from app.agent_coordination.persist import LEDGER_BACKING_KEY, _is_backing, repair_misclassified_backing
    u, _ = make_verified_user(); owner = u.id
    wa = _seed(superuser_db, owner); _contaminate(superuser_db, wa)
    before = _is_backing(wa)
    n = repair_misclassified_backing(superuser_db, owner_id=owner); superuser_db.commit(); superuser_db.refresh(wa)
    prov = wa.provenance or {}
    top = LEDGER_BACKING_KEY in prov
    nested = LEDGER_BACKING_KEY in (prov.get("durable_coordination") or {})
    print(f"RP1|repaired={n} before_is_backing={before} after_top={top} after_nested={nested} after_is_backing={_is_backing(wa)}")
    assert before is False  # _is_backing already keys on created_without_manual (CF-2 fix) — the markers are stale junk
    assert n == 1 and top is False and nested is False, "RP-1 ledger_backing markers not fully stripped"


# ---- RP-2: genuine synthetic backing records are PRESERVED
def test_RP2_preserves_synthetic_backing(superuser_db, make_verified_user):
    from app.agent_coordination.persist import _create_backing_assignment, _is_backing, repair_misclassified_backing
    u, _ = make_verified_user(); owner = u.id; _rls(superuser_db, owner)
    synth = _create_backing_assignment(superuser_db, owner_id=owner); superuser_db.commit()
    assert _is_backing(synth) is True
    n = repair_misclassified_backing(superuser_db, owner_id=owner); superuser_db.commit(); superuser_db.refresh(synth)
    print(f"RP2|repaired={n} synthetic_still_backing={_is_backing(synth)} created_wo_manual={(synth.provenance or {}).get('created_without_manual_assignment')}")
    assert _is_backing(synth) is True, "RP-2 repair wrongly demoted a genuine synthetic backing row"
    assert n == 0, "RP-2 repair should not 'repair' a legitimate synthetic row"


# ---- RP-3: no unrelated provenance or historical evidence is erased
def test_RP3_preserves_unrelated_provenance_and_history(superuser_db, make_verified_user):
    from app.agent_coordination.persist import repair_misclassified_backing
    u, _ = make_verified_user(); owner = u.id
    wa = _seed(superuser_db, owner); _contaminate(superuser_db, wa)
    repair_misclassified_backing(superuser_db, owner_id=owner); superuser_db.commit(); superuser_db.refresh(wa)
    prov = wa.provenance or {}
    dump = prov.get("durable_coordination") or {}
    print(f"RP3|unrelated_key={prov.get('key')} dump_obligations={len(dump.get('obligations') or [])} dump_delivery={'delivery' in dump}")
    assert prov.get("key") == "real-work", "RP-3 erased unrelated provenance"
    assert dump.get("obligations") and dump.get("delivery"), "RP-3 erased historical obligations/delivery evidence"


# ---- RP-4: idempotent
def test_RP4_idempotent(superuser_db, make_verified_user):
    from app.agent_coordination.persist import repair_misclassified_backing
    u, _ = make_verified_user(); owner = u.id
    wa = _seed(superuser_db, owner); _contaminate(superuser_db, wa)
    n1 = repair_misclassified_backing(superuser_db, owner_id=owner); superuser_db.commit()
    n2 = repair_misclassified_backing(superuser_db, owner_id=owner); superuser_db.commit()
    n3 = repair_misclassified_backing(superuser_db, owner_id=owner); superuser_db.commit()
    print(f"RP4|first={n1} second={n2} third={n3}")
    assert n1 == 1 and n2 == 0 and n3 == 0, "RP-4 repair is not idempotent"


# ---- RP-5: repeated repairs (including after a fresh-session reload) cannot corrupt the assignment
def test_RP5_restart_safe(superuser_db, make_verified_user):
    from app.agent_coordination.persist import _is_backing, find_owner_assignments, repair_misclassified_backing
    from app.db import migration_engine
    u, _ = make_verified_user(); owner = u.id
    wa = _seed(superuser_db, owner); _contaminate(superuser_db, wa); wa_id = str(wa.id); superuser_db.commit()
    # first repair in the same (committed) session, then a repeat here, then one more after a fresh-session reload
    n1 = repair_misclassified_backing(superuser_db, owner_id=owner); superuser_db.commit()
    n2 = repair_misclassified_backing(superuser_db, owner_id=owner); superuser_db.commit()
    with sessionmaker(bind=migration_engine)() as fr:
        _rls(fr, owner)
        n3 = repair_misclassified_backing(fr, owner_id=owner); fr.commit()
        row = next(r for r in find_owner_assignments(fr, owner_id=owner) if str(r.id) == wa_id)
        intact = row.role is not None and row.status is not None and _is_backing(row) is False
    print(f"RP5|first={n1} repeat={n2} after_fresh_session={n3} assignment_intact={intact}")
    assert n1 == 1 and n2 == 0 and n3 == 0 and intact, "RP-5 repeated/cross-session repair corrupted or re-repaired the assignment"


# ---- RP-6: missing/malformed provenance fails safely
def test_RP6_malformed_provenance_safe(superuser_db, make_verified_user):
    """The DB CHECK constraint ck_agent_work_assignments_provenance_object forbids a non-object provenance (NULL), so the
    worst storable 'malformed' states are an empty {} and a non-dict durable_coordination. repair must handle both
    without raising and without mangling them. (A NULL provenance is rejected at the DB layer — a safety guarantee.)"""
    from app.agent_coordination.persist import _is_backing, repair_misclassified_backing
    from sqlalchemy.orm.attributes import flag_modified
    u, _ = make_verified_user(); owner = u.id
    wa_empty = _seed(superuser_db, owner, key="cursor")
    wa_empty.provenance = {}; flag_modified(wa_empty, "provenance")
    wa_bad = _seed(superuser_db, owner, key="claude", role="reviewer")
    bad_stored = True
    try:
        wa_bad.provenance = {"durable_coordination": "not-a-dict", "key": "x"}; flag_modified(wa_bad, "provenance")
        superuser_db.commit()
    except Exception:
        superuser_db.rollback(); bad_stored = False  # DB rejects a non-dict dump -> malformed can't even be stored (safe)
    # repair must not raise regardless
    n = repair_misclassified_backing(superuser_db, owner_id=owner); superuser_db.commit()
    superuser_db.refresh(wa_empty)
    print(f"RP6|repaired={n} empty_is_backing={_is_backing(wa_empty)} nondict_dump_storable={bad_stored}")
    assert _is_backing(wa_empty) is False, "RP-6 an empty-provenance real assignment must not read as synthetic"
    if bad_stored:
        superuser_db.refresh(wa_bad)
        assert (wa_bad.provenance or {}).get("durable_coordination") == "not-a-dict", "RP-6 mangled a non-dict dump"


# ---- RP-7: historical obligations/delivery/completion remain reconstructable after repair
def test_RP7_history_reconstructable(superuser_db, make_verified_user):
    from app.agent_coordination.durable import load_owner_ledger
    from app.agent_coordination.persist import repair_misclassified_backing
    from app.agent_coordination.service import transition_status
    from app.db import migration_engine
    u, _ = make_verified_user(); owner = u.id
    wa = _seed(superuser_db, owner); _contaminate(superuser_db, wa)
    for s in ("ready", "running", "completed"):
        transition_status(superuser_db, assignment=wa, new_status=s)
    superuser_db.commit()
    repair_misclassified_backing(superuser_db, owner_id=owner); superuser_db.commit()
    with sessionmaker(bind=migration_engine)() as fr:
        _rls(fr, owner); svc = load_owner_ledger(fr, owner_id=owner)
        obl = svc._obligations.get(str(wa.id))
    print(f"RP7|obligation_present={obl is not None} status={obl.get('status') if obl else None} total={len(svc._obligations)}")
    assert obl is not None, "RP-7 repair made the historical obligation unreconstructable"


# ---- RP-8 (security): an untrusted created_without_manual_assignment marker on a REAL assignment
def test_RP8_untrusted_synthetic_marker_trust_boundary(superuser_db, make_verified_user):
    """_is_backing (and reconcile_owner's skip) now key SOLELY on created_without_manual_assignment. If untrusted input
    could set that on a real assignment, the assignment would be silently treated as synthetic (events skipped) AND
    repair would NOT fix it (repair early-returns for created_without_manual rows). This test documents the trust
    boundary: the marker is honored as authoritative. It is examiner-asserted-safe ONLY because no agent/message/receipt
    production path writes this key — the sole writer is _create_backing_assignment (verified by grep). Hardening note:
    consider corroborating the synthetic classification with agent_key==LEDGER_AGENT_KEY + supports_write=False."""
    from app.agent_coordination.persist import _is_backing, repair_misclassified_backing
    from sqlalchemy.orm.attributes import flag_modified
    u, _ = make_verified_user(); owner = u.id
    wa = _seed(superuser_db, owner)  # a REAL builder
    prov = dict(wa.provenance or {}); prov["created_without_manual_assignment"] = True  # simulate an untrusted/incorrect marker
    wa.provenance = prov; flag_modified(wa, "provenance"); superuser_db.commit(); superuser_db.refresh(wa)
    treated_synthetic = _is_backing(wa)
    repaired = repair_misclassified_backing(superuser_db, owner_id=owner)  # repair does NOT demote it
    superuser_db.refresh(wa)
    print(f"RP8|HARDENING real_with_untrusted_marker_is_backing={treated_synthetic} repair_fixed_it={_is_backing(wa) is False} repaired_count={repaired}")
    # This is the DOCUMENTED trust boundary, not a reachable exploit on #306: assert the classification trusts the marker
    assert treated_synthetic is True, "RP-8 (if False, classification no longer trusts the raw marker — note the hardening)"
    assert _is_backing(wa) is True and repaired == 0, "RP-8 repair intentionally does not demote a created_without_manual row"
