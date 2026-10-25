"""HD-1 — Synthetic Ledger Origin Trust Boundary (examiner-owned negative tests).

Question: synthetic-ledger identity on #306 is decided SOLELY by the mutable provenance boolean
`created_without_manual_assignment` (via persist._is_backing and lifecycle.reconcile_owner's skip). These tests probe
whether that single field is a safe origin-of-truth under forged markers, import/recovery, identity/capability
inconsistency, conflicting rows, corrupted provenance, and DB/API paths — and whether classification should be
corroborated by canonical agent identity + write restriction + a trusted origin record.

Each test prints REACHABLE (exploitable via an application path today) or HYPOTHETICAL (needs direct DB write =
the Codex-owned FA-PRIV surface). No production change. Read-only vs target. first_cycle_go unaffected.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401

LEDGER_AGENT_KEY = "mainai-ledger"


def _rls(s, o):
    from app.request_context import current_user_id as current_user_id_var
    current_user_id_var.set(str(o)); s.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": str(o)})


def _seed(db, owner, key="cursor", role="builder", write=True):
    from app.agent_coordination.service import create_work_assignment, register_agent
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
    from app.models.mainai_execution import MainAITask
    _rls(db, owner)
    g = create_goal(db, owner_id=owner, title=key, original_instruction="x", created_by="founder", approval_policy="autonomous_development_work")
    p = create_plan(db, goal=g, rationale="r", created_by="founder", tasks=[PlannedTaskSpec(description="d", task_type="repo_edit", target_files=["docs/x.md"])])
    t = db.query(MainAITask).filter_by(plan_id=p.id).one()
    a = register_agent(db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", display_name=key, adapter_kind="cli", supports_write=write)
    wa = create_work_assignment(db, owner_id=owner, goal_id=g.id, task_id=t.id, agent_id=a.id, role=role,
                                read_write_mode="read_write" if role == "builder" else "read_only",
                                repository_identity="lifeai", allowed_paths=["docs/**"], requested_by="founder")
    db.commit()
    return wa


def _stamp(db, row, **kv):
    from sqlalchemy.orm.attributes import flag_modified
    prov = dict(row.provenance or {}); prov.update(kv); row.provenance = prov; flag_modified(row, "provenance"); db.flush()


def _agent_key(db, wa):
    from app.models.agent_coordination import CoordinationAgent
    a = db.get(CoordinationAgent, wa.agent_id)
    return a.agent_key if a else None


# ---- HD1-1: forged created_without_manual_assignment on a legitimate work assignment
def test_HD1_1_forged_synthetic_marker_sidelines_real_assignment(superuser_db, make_verified_user):
    from app.agent_coordination.persist import _is_backing
    u, _ = make_verified_user(); owner = u.id
    wa = _seed(superuser_db, owner)  # real builder, supports_write=True, agent_key cursor-*
    _stamp(superuser_db, wa, created_without_manual_assignment=True); superuser_db.commit(); superuser_db.refresh(wa)
    is_b = _is_backing(wa); akey = _agent_key(superuser_db, wa)
    print(f"HD1-1|HYPOTHETICAL(needs direct provenance write=FA-PRIV) is_backing={is_b} agent_key={akey} supports_write=True "
          f"-> a REAL builder would be treated as synthetic ledger storage and its events skipped by reconcile")
    assert is_b is True, "HD1-1 classification keys on the raw marker (origin-of-truth is a single mutable field)"
    # corroboration that WOULD catch it (canonical identity + write restriction) — currently NOT applied:
    assert akey != LEDGER_AGENT_KEY, "HD1-1 the forged row has a non-canonical agent identity the classifier ignores"


# ---- HD1-2: synthetic-origin marker tampering during import or recovery
def test_HD1_2_no_assignment_import_recovery_restores_provenance():
    """REACHABILITY CHECK: no import/recovery path restores AgentWorkAssignment.provenance from external/untrusted data.
    RAG zip/library import and storage operate on Document provenance; recovery_takeover does not write assignment
    provenance. So a tampered bundle cannot inject the synthetic marker onto a work assignment."""
    import inspect
    import app.rag.zip_import as zi, app.rag.library_import as li
    srcs = inspect.getsource(zi) + inspect.getsource(li)
    writes_assignment_prov = ("AgentWorkAssignment" in srcs and ".provenance" in srcs)
    try:
        import app.mainai_execution.recovery_takeover as rt
        writes_assignment_prov = writes_assignment_prov or ("AgentWorkAssignment" in inspect.getsource(rt) and "created_without_manual" in inspect.getsource(rt))
    except Exception:
        pass
    print(f"HD1-2|REACHABILITY import/recovery_writes_assignment_provenance={writes_assignment_prov}")
    assert writes_assignment_prov is False, "HD1-2 an import/recovery path restores assignment provenance — tampering vector"


# ---- HD1-3: inconsistent agent identity vs synthetic-origin metadata
def test_HD1_3_identity_inconsistency_not_corroborated(superuser_db, make_verified_user):
    from app.agent_coordination.persist import _is_backing
    u, _ = make_verified_user(); owner = u.id
    wa = _seed(superuser_db, owner, key="codex")  # a real codex agent, NOT the canonical ledger identity
    _stamp(superuser_db, wa, created_without_manual_assignment=True); superuser_db.commit(); superuser_db.refresh(wa)
    akey = _agent_key(superuser_db, wa)
    print(f"HD1-3|HYPOTHETICAL is_backing={_is_backing(wa)} agent_key={akey} canonical={LEDGER_AGENT_KEY} "
          f"inconsistent={akey != LEDGER_AGENT_KEY} -> classification does NOT cross-check agent identity")
    assert _is_backing(wa) is True and akey != LEDGER_AGENT_KEY, "HD1-3 synthetic classification ignores agent-identity inconsistency"


# ---- HD1-4: synthetic backing with unexpected WRITE capability
def test_HD1_4_synthetic_with_write_capability_not_corroborated(superuser_db, make_verified_user):
    from app.agent_coordination.persist import _is_backing
    from app.models.agent_coordination import CoordinationAgent
    u, _ = make_verified_user(); owner = u.id
    wa = _seed(superuser_db, owner, role="builder", write=True)  # read_write + supports_write=True
    _stamp(superuser_db, wa, created_without_manual_assignment=True); superuser_db.commit(); superuser_db.refresh(wa)
    agent = superuser_db.get(CoordinationAgent, wa.agent_id)
    rw = wa.read_write_mode.value if hasattr(wa.read_write_mode, "value") else wa.read_write_mode
    print(f"HD1-4|HYPOTHETICAL is_backing={_is_backing(wa)} supports_write={agent.supports_write} read_write_mode={rw} "
          f"(genuine synthetic is supports_write=False/read_only) -> classification ignores write capability")
    assert _is_backing(wa) is True and agent.supports_write is True, "HD1-4 a write-capable row is classified synthetic backing"


# ---- HD1-5: multiple / conflicting synthetic backing rows
def test_HD1_5_multiple_synthetic_rows_handled_deterministically(superuser_db, make_verified_user):
    from app.agent_coordination.persist import _is_backing, ensure_owner_ledger_backing, find_owner_assignments, persist_status
    u, _ = make_verified_user(); owner = u.id
    a1 = _seed(superuser_db, owner, key="r1", role="researcher", write=False)
    a2 = _seed(superuser_db, owner, key="r2", role="researcher", write=False)
    _stamp(superuser_db, a1, created_without_manual_assignment=True)
    _stamp(superuser_db, a2, created_without_manual_assignment=True); superuser_db.commit()
    picked = ensure_owner_ledger_backing(superuser_db, owner_id=owner); superuser_db.commit()
    rows = find_owner_assignments(superuser_db, owner_id=owner)
    n_backing = sum(1 for r in rows if _is_backing(r))
    st = persist_status(superuser_db, owner_id=owner)
    # determinism: a second call returns the SAME pick, no crash, no new row minted
    picked2 = ensure_owner_ledger_backing(superuser_db, owner_id=owner)
    print(f"HD1-5|n_synthetic_rows={n_backing} picked_stable={picked.id == picked2.id} backing_present={st['backing_present']} total={len(rows)}")
    assert picked.id == picked2.id, "HD1-5 conflicting synthetic rows produce a non-deterministic/unstable backing pick"
    assert n_backing == 2, "HD1-5 precondition: two synthetic rows exist (conflict surface present)"


# ---- HD1-6: corrupted / incomplete provenance after restart
def test_HD1_6_corrupted_provenance_safe_after_restart(superuser_db, make_verified_user):
    from app.agent_coordination.durable import load_owner_ledger
    from app.agent_coordination.persist import _is_backing, repair_misclassified_backing
    from app.db import migration_engine
    u, _ = make_verified_user(); owner = u.id
    wa = _seed(superuser_db, owner)
    _stamp(superuser_db, wa, durable_coordination={"partial": True})  # incomplete dump, no obligations/delivery
    superuser_db.commit()
    with sessionmaker(bind=migration_engine)() as fr:
        _rls(fr, owner)
        repair_misclassified_backing(fr, owner_id=owner); fr.commit()  # must not raise
        svc = load_owner_ledger(fr, owner_id=owner)  # must not raise on partial dump
        rows = find = __import__("app.agent_coordination.persist", fromlist=["find_owner_assignments"]).find_owner_assignments(fr, owner_id=owner)
        backing = sum(1 for r in rows if _is_backing(r))
    print(f"HD1-6|REACHABLE(benign) reload_ok=True backing_rows={backing} obligations={len(svc._obligations)}")
    assert backing == 0, "HD1-6 a corrupted/partial dump on a real row must not make it classify synthetic"


# ---- HD1-7: unauthorized modification through accessible DB or API paths (the key reachability result)
def test_HD1_7_no_application_path_sets_synthetic_marker(superuser_db, make_verified_user, client, monkeypatch):
    """Drive the real receipt/gateway application flow and confirm NO work assignment gains
    created_without_manual_assignment through it. The only writer is _create_backing_assignment."""
    from app.agent_coordination.delivery import (attempt_transmit, claim_message, issue_bridge_credential,
                                                 prepare_message, submit_receipt)
    from app.agent_coordination.durable import load_owner_ledger
    from app.agent_coordination.persist import find_owner_assignments
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    u, _ = make_verified_user(); owner = u.id; wa = _seed(superuser_db, owner)
    issue_bridge_credential(superuser_db, owner_id=owner, recipient="cursor", actor="founder"); superuser_db.commit()
    svc = load_owner_ledger(superuser_db, owner_id=owner)
    prepare_message(svc, obligation_id=str(wa.id), recipient="cursor", instructions="do", idempotency_key=f"{wa.id}:inbox")
    mid = next(r["message_id"] for r in svc._delivery["messages"].values()); attempt_transmit(svc, message_id=mid); claim_message(svc, recipient="cursor", message_id=mid)
    # an agent receipt attempting to inject the synthetic marker as a field
    submit_receipt(superuser_db, owner_id=owner, recipient="cursor", service=svc,
                   payload={"kind": "completion", "event_id": "hd17", "message_id": mid, "created_without_manual_assignment": True, "evidence": {"c": 1}})
    superuser_db.commit()
    leaked = [str(r.id) for r in find_owner_assignments(superuser_db, owner_id=owner) if (r.provenance or {}).get("created_without_manual_assignment")]
    print(f"HD1-7|REACHABILITY app_path_set_synthetic_marker={bool(leaked)} rows_with_marker={len(leaked)}")
    assert leaked == [], "HD1-7 an application (receipt/gateway) path set created_without_manual_assignment — REACHABLE vector"


# ---- HD1-8: correct behavior when historical provenance requires repair (synthetic vs real)
def test_HD1_8_repair_demotes_real_preserves_synthetic(superuser_db, make_verified_user):
    from app.agent_coordination.persist import (LEDGER_BACKING_KEY, _create_backing_assignment, _is_backing,
                                               repair_misclassified_backing)
    from sqlalchemy.orm.attributes import flag_modified
    u, _ = make_verified_user(); owner = u.id; _rls(superuser_db, owner)
    synth = _create_backing_assignment(superuser_db, owner_id=owner)
    real = _seed(superuser_db, owner)
    # contaminate the REAL row with a stray ledger_backing marker (CF-2 style)
    prov = dict(real.provenance or {}); prov[LEDGER_BACKING_KEY] = True; real.provenance = prov; flag_modified(real, "provenance")
    superuser_db.commit()
    n = repair_misclassified_backing(superuser_db, owner_id=owner); superuser_db.commit()
    superuser_db.refresh(real); superuser_db.refresh(synth)
    print(f"HD1-8|repaired={n} real_is_backing={_is_backing(real)} synthetic_is_backing={_is_backing(synth)}")
    assert _is_backing(real) is False and _is_backing(synth) is True, "HD1-8 repair must demote the real row and preserve the synthetic one"
