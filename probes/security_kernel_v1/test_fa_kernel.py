"""Founder Authority / Security Kernel acceptance (FA-*). Candidate-independent where possible.

Threat model: enforcement must hold *independently of the model*. Every tamper attempt below runs as the runtime database
role (`mainai_app`, the identity of every web/worker code path -- i.e. anything a prompt injection, a model-chosen tool
call or a code bug could drive), against authority rows created through the legitimate Founder path. A control that only
holds because "no code path calls it today" is reported as PRESENCE-ONLY, not as enforced.

Outcome vocabulary: PASS (enforced), FAIL (unsafe: runtime can do it), BLOCKED (capability genuinely missing; never PASS).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from _harness import _privileges, owner_id  # noqa: F401  (runtime privileges applied exactly as production boot does)

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _try(db, sql, params=None):
    """Run one statement as the runtime role inside a savepoint; report allowed/denied without poisoning the session."""
    sp = db.begin_nested()
    try:
        res = db.execute(text(sql), params or {})
        n = res.rowcount
        sp.rollback()  # never keep a tamper
        return f"allowed(rows={n})"
    except Exception as exc:  # noqa: BLE001
        sp.rollback()
        msg = str(getattr(exc, "orig", exc)).splitlines()[0][:120]
        return f"denied({msg})"


def _rls(db, uid):
    db.execute(text("SET LOCAL app.current_user_id = :u"), {"u": str(uid)})


def _goal_with_authority(owner):
    """Founder path (superuser session = the privileged service path behind require_founder routes)."""
    from app.db import migration_engine
    from app.execution_envelopes.service import authorize_execution_scope, propose_execution_scope
    from app.mainai_execution import planner
    from app.mainai_execution.planner import PlannedTaskSpec
    from app.models.mainai_execution import MainAITask
    from app.provider_spend.service import authorize_provider_spend

    with sessionmaker(bind=migration_engine)() as s:
        goal = planner.create_goal(s, owner_id=owner, title="fa", original_instruction="fa", created_by="founder")
        prop = propose_execution_scope(s, owner_id=owner, goal_id=goal.id, idempotency_key=f"p-{uuid.uuid4()}")
        _, env = authorize_execution_scope(s, owner_id=owner, proposal_id=prop.id, authorized_by="founder",
                                           authorized_paths=["backend/app/greeting.py"], authorized_capabilities=["path_a", "repo_edit"],
                                           authorized_risk="low", envelope_idempotency_key=f"e-{uuid.uuid4()}")
        spend = authorize_provider_spend(s, owner_id=owner, goal_id=goal.id, execution_envelope_id=env.id, authorized_by="founder",
                                         max_cost_usd=Decimal("0.10"), max_requests=2, max_cost_per_request_usd=Decimal("0.05"),
                                         idempotency_key=f"s-{uuid.uuid4()}", allowed_providers=["openai"], allowed_models=[])
        planner.create_plan(s, goal=goal, rationale="r", created_by="founder",
                            tasks=[PlannedTaskSpec(description="needs approval", task_type="read_only_audit"),
                                   PlannedTaskSpec(description="sibling", task_type="read_only_audit")])
        tasks = s.query(MainAITask).filter(MainAITask.goal_id == goal.id).order_by(MainAITask.description).all()
        s.commit()
        return goal.id, env.id, spend.id, [t.id for t in tasks]


# --------------------------------------------------------------------------- R4/R5: kernel independent of the model
def test_FA4_runtime_cannot_self_grant_authority_rows(db_session, owner_id):
    """Req 4: the runtime role must not be able to create its own envelope / spend authorization / verification receipt.
    Evidence is the effective privilege set of the runtime role (has_table_privilege) plus any BEFORE INSERT guard trigger."""
    tables = ("execution_authorization_envelopes", "provider_spend_authorizations", "mainai_verification_records")
    out = {}
    for t in tables:
        can_insert = db_session.execute(text("SELECT has_table_privilege(current_user, :t, 'INSERT')"), {"t": t}).scalar_one()
        guarded = db_session.execute(text(
            "SELECT count(*) FROM information_schema.triggers WHERE event_object_table=:t AND event_manipulation='INSERT'"), {"t": t}).scalar_one()
        out[t] = "denied(no INSERT privilege)" if not can_insert else ("guarded(insert trigger present)" if guarded else "allowed(INSERT privilege, no guard trigger)")
    print(f"FA4|{out}")
    assert all(not v.startswith("allowed") for v in out.values()), f"FA-4 runtime role can mint authority rows: {out}"


def test_FA7_runtime_cannot_widen_an_authorized_envelope(db_session, owner_id):
    goal, env, _, _ = _goal_with_authority(owner_id)
    _rls(db_session, owner_id)
    cols = {c for (c,) in db_session.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='execution_authorization_envelopes'"))}
    sets = []
    if "authorized_paths" in cols:
        sets.append(("authorized_paths", "UPDATE execution_authorization_envelopes SET authorized_paths='[\"/\"]'::jsonb WHERE id=:i"))
    if "authorized_capabilities" in cols:
        sets.append(("authorized_capabilities", "UPDATE execution_authorization_envelopes SET authorized_capabilities='[\"path_a\",\"repo_edit\",\"push\",\"merge\"]'::jsonb WHERE id=:i"))
    if "authorized_risk" in cols:
        sets.append(("authorized_risk", "UPDATE execution_authorization_envelopes SET authorized_risk='high' WHERE id=:i"))
    r = {k: _try(db_session, q, {"i": str(env)}) for k, q in sets}
    print(f"FA7|{r}")
    assert r and all(v.startswith("denied") or "rows=0" in v for v in r.values()), f"FA-7 runtime role can widen the Founder's envelope: {r}"


def test_FA8_runtime_cannot_raise_a_spend_ceiling(db_session, owner_id):
    _, _, spend, _ = _goal_with_authority(owner_id)
    _rls(db_session, owner_id)
    r = {
        "max_cost_usd": _try(db_session, "UPDATE provider_spend_authorizations SET max_cost_usd=1000 WHERE id=:i", {"i": str(spend)}),
        "max_requests": _try(db_session, "UPDATE provider_spend_authorizations SET max_requests=100000 WHERE id=:i", {"i": str(spend)}),
        "allowed_providers": _try(db_session, "UPDATE provider_spend_authorizations SET allowed_providers='[\"openai\",\"anthropic\"]'::jsonb WHERE id=:i", {"i": str(spend)}),
    }
    print(f"FA8|{r}")
    assert all(v.startswith("denied") or "rows=0" in v for v in r.values()), f"FA-8 runtime role can raise the Founder's spend ceiling: {r}"


def test_FA5_runtime_cannot_forge_a_founder_approval(db_session, owner_id):
    """Req 4: approval must not be forgeable by writing an ordinary event row."""
    from app.mainai_execution.approval import ApprovalRequiredError, require_task_approval
    from app.models.mainai_execution import MainAITask

    _, _, _, (t_approve, _) = _goal_with_authority(owner_id)
    _rls(db_session, owner_id)
    db_session.execute(text("UPDATE mainai_tasks SET approval_required=true WHERE id=:t"), {"t": str(t_approve)})
    task = db_session.get(MainAITask, t_approve)
    with pytest.raises(ApprovalRequiredError):
        require_task_approval(db_session, task=task, goal_approval_policy="standard_repo_work")  # control
    forged = _try(db_session, "SELECT 1")  # placeholder to keep savepoint API symmetric
    sp = db_session.begin_nested()
    try:
        db_session.execute(text("INSERT INTO mainai_task_events (id, task_id, owner_id, event_type, detail, created_at) "
                                "VALUES (gen_random_uuid(), :t, :o, 'approval_granted', '{\"approved_by\":\"founder\"}'::jsonb, now())"),
                           {"t": str(t_approve), "o": str(owner_id)})
        try:
            require_task_approval(db_session, task=task, goal_approval_policy="standard_repo_work")
            forged = "allowed: forged approval_granted event satisfies the approval gate"
        except ApprovalRequiredError:
            forged = "denied: gate not satisfied by a forged event"
    except Exception as exc:  # noqa: BLE001
        forged = f"denied({str(getattr(exc, 'orig', exc)).splitlines()[0][:100]})"
    finally:
        sp.rollback()
    print(f"FA5|{forged}")
    assert forged.startswith("denied"), f"FA-5 {forged}"


def test_FA6_runtime_cannot_silently_drop_an_approval_requirement(db_session, owner_id):
    """Req 4: approval policy must not be silently changeable by the runtime role."""
    _, _, _, (t_approve, _) = _goal_with_authority(owner_id)
    from app.db import migration_engine

    with migration_engine.begin() as c:
        c.execute(text("UPDATE mainai_tasks SET approval_required=true WHERE id=:t"), {"t": str(t_approve)})
    _rls(db_session, owner_id)
    r = {
        "approval_required_false": _try(db_session, "UPDATE mainai_tasks SET approval_required=false WHERE id=:t", {"t": str(t_approve)}),
        "goal_approval_policy_change": _try(db_session, "UPDATE mainai_goals SET approval_policy='read_only_analysis' WHERE id=(SELECT goal_id FROM mainai_tasks WHERE id=:t)", {"t": str(t_approve)}),
    }
    print(f"FA6|{r}")
    assert all(v.startswith("denied") or "rows=0" in v for v in r.values()), f"FA-6 runtime role can silently drop the approval requirement: {r}"


def test_FA9_stop_cannot_be_cleared_without_an_authenticated_founder(db_session, owner_id):
    """Req 4/5: clearing STOP needs proof of an authenticated Founder, not a well-formed string."""
    from app.workforce.kill_switch import KillSwitchError, activate_kill_switch, clear_owner_stop, get_kill_switch

    _rls(db_session, owner_id)
    activate_kill_switch(db_session, owner_id=owner_id, reason="founder stop")
    db_session.commit()
    _rls(db_session, owner_id)
    direct = _try(db_session, "UPDATE workforce_authority_epoch SET stopped=false WHERE stopped=true")
    try:
        clear_owner_stop(db_session, owner_id=owner_id, founder_ack="founder_ack:written by the runtime, not the Founder")
        via_api = "allowed: fabricated founder_ack string cleared STOP"
        db_session.rollback()
    except KillSwitchError as exc:
        via_api = f"denied({exc})"
        db_session.rollback()
    _rls(db_session, owner_id)
    still = get_kill_switch(db_session, owner_id).active
    print(f"FA9|direct_update={direct} clear_api={via_api} stop_active_after_rollback={still}")
    assert direct.startswith("denied") and via_api.startswith("denied"), f"FA-9 STOP clearable without an authenticated Founder: direct={direct} api={via_api}"


def test_FA10_runtime_cannot_lock_out_or_impersonate_the_founder(db_session, owner_id):
    """Req 4: no non-Founder code path may disable, re-key or invalidate the Founder account."""
    from app.bootstrap import bootstrap_founder_user
    from app.founder import FOUNDER_USER_ID

    bootstrap_founder_user(db_session)
    db_session.commit()
    _rls(db_session, owner_id)  # a non-Founder request context
    future = datetime.utcnow() + timedelta(days=3650)
    r = {
        "deactivate": _try(db_session, "UPDATE users SET is_active=false WHERE id=:f", {"f": str(FOUNDER_USER_ID)}),
        "invalidate_sessions": _try(db_session, "UPDATE users SET sessions_valid_after=:t WHERE id=:f", {"t": future, "f": str(FOUNDER_USER_ID)}),
        "replace_password": _try(db_session, "UPDATE users SET password_hash='x' WHERE id=:f", {"f": str(FOUNDER_USER_ID)}),
        "self_promote_role": _try(db_session, "UPDATE users SET role='founder' WHERE id=:o", {"o": str(owner_id)}),
    }
    print(f"FA10|{r}")
    lock = {k: v for k, v in r.items() if k != "self_promote_role"}
    assert all(v.startswith("denied") or "rows=0" in v for v in lock.values()), f"FA-10 a non-Founder context can lock out the Founder: {lock}"


def test_FA11_role_self_promotion_grants_nothing(client, make_verified_user, db_session):
    """Req 4: even if a row claims role=founder, Founder APIs stay closed (id pinning)."""
    u, pw = make_verified_user(role="founder")
    db_session.commit()
    csrf = client.post("/api/auth/login", json={"email": u.email, "password": pw}).json()["csrf_token"]
    codes = [client.get("/api/mainai/founder-ops/snapshot").status_code,
             client.post("/api/provider-spend/authorize", json={}, headers={"X-CSRF-Token": csrf}).status_code]
    print(f"FA11|codes={codes}")
    assert all(c == 403 for c in codes)


def test_FA12_audit_trail_is_append_only_for_the_runtime(db_session, owner_id):
    """Req 6: receipts must not be rewritable or deletable by the runtime role."""
    from app.db import migration_engine

    with migration_engine.begin() as c:
        aid = c.execute(text("INSERT INTO audit_log (id, user_id, action, created_at) VALUES (gen_random_uuid(), :u, 'examiner_probe', now()) RETURNING id"),
                        {"u": str(owner_id)}).scalar_one()
    _rls(db_session, owner_id)
    r = {"update": _try(db_session, "UPDATE audit_log SET action='rewritten' WHERE id=:i", {"i": str(aid)}),
         "delete": _try(db_session, "DELETE FROM audit_log WHERE id=:i", {"i": str(aid)})}
    print(f"FA12|{r}")
    assert all(v.startswith("denied") or "rows=0" in v for v in r.values()), f"FA-12 runtime role can rewrite/delete audit receipts: {r}"


def test_FA13_approval_is_not_replayable_across_tasks(db_session, owner_id):
    from app.mainai_execution.approval import ApprovalRequiredError, grant_task_approval, require_task_approval
    from app.db import migration_engine
    from app.models.mainai_execution import MainAITask

    _, _, _, (a, b) = _goal_with_authority(owner_id)
    with migration_engine.begin() as c:
        c.execute(text("UPDATE mainai_tasks SET approval_required=true WHERE id IN (:a,:b)"), {"a": str(a), "b": str(b)})
    _rls(db_session, owner_id)
    grant_task_approval(db_session, task=db_session.get(MainAITask, a), approved_by="founder")
    replay = "denied"
    try:
        require_task_approval(db_session, task=db_session.get(MainAITask, b), goal_approval_policy="standard_repo_work")
        replay = "allowed: approval of task A satisfied task B"
    except ApprovalRequiredError:
        pass
    db_session.rollback()
    print(f"FA13|{replay}")
    assert replay == "denied"


# --------------------------------------------------------------------------- R2/R3: step-up, configurability
def test_FA3_sensitive_founder_changes_require_step_up(client, db_session, owner_id):
    """Req 2/3: authorizing spend, widening execution scope and approving privileged steps are sensitive changes; they
    need step-up (recent reauthentication), not just a live session. Only account erasure has step-up today."""
    from app.bootstrap import bootstrap_founder_user

    bootstrap_founder_user(db_session)
    db_session.commit()
    csrf = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD}).json()["csrf_token"]
    paths = [r.path for r in client.app.routes if getattr(r, "path", "").startswith("/api/") and "reauth" in getattr(r, "path", "")]
    import inspect

    from app.routers import execution_envelopes, provider_spend

    step_up_aware = [m.__name__ for m in (execution_envelopes, provider_spend) if "reauth" in inspect.getsource(m)]
    print(f"FA3|reauth_routes={paths} step_up_aware_modules={step_up_aware}")
    if not step_up_aware:
        pytest.fail("BLOCKED: no step-up authentication for sensitive Founder changes (spend authorization, execution "
                    "scope, approvals, STOP clear); only DELETE /api/account uses a reauth receipt")


def test_FA2_founder_can_inspect_and_modify_permissions_and_ai_behavior(client):
    """Req 2: an authenticated Founder API to read and change configurable permissions, restrictions, AI behaviour and
    tool capabilities. Settings-file/env flags are not Founder-modifiable at runtime."""
    routes = {(m, r.path) for r in client.app.routes for m in (getattr(r, "methods", None) or set())}
    writable = sorted(p for m, p in routes if m in {"PUT", "PATCH", "POST"} and any(k in p for k in ("permission", "policy", "restriction", "capabilit", "settings", "behavio")))
    print(f"FA2|policy_write_routes={writable}")
    if not writable:
        pytest.fail("BLOCKED: no Founder API to modify permissions/restrictions/AI behaviour/tool capabilities "
                    "(only PUT /api/admin/providers/config); safe-launch flags are environment settings")


# --------------------------------------------------------------------------- R1/R7: explanations and denial taxonomy
def test_FA1_restriction_explanations_are_derived_not_asserted(monkeypatch):
    """Req 1: every Founder-facing claim about enforcement must be derived from an actual control. Flipping the control
    must flip the claim; an unconditional `True` is an invented reason."""
    snap = pytest.importorskip("app.mainai_founder_ops.snapshot")
    if not hasattr(snap, "_founder_authority"):
        pytest.fail("BLOCKED: no Founder-facing restriction explanation surface")
    from app.config import get_settings

    st = get_settings()
    base = snap._founder_authority(recall_http_mounted=False)
    for flag in ("github_write_enabled", "github_auto_merge_enabled", "mainai_activation_mode", "mainai_legacy_prepare_pr_writes_enabled"):
        monkeypatch.setattr(st, flag, True)
    if hasattr(st, "mainai_allow_live_checkout_writes"):
        monkeypatch.setattr(st, "mainai_allow_live_checkout_writes", True)
    loose = snap._founder_authority(recall_http_mounted=True)
    unconditional = [k for k, v in base.items() if isinstance(v, bool) and v is True and loose.get(k) is True
                     and k in ("policy_transparent", "sensitive_changes_require_founder_approval", "independent_security_enforcement")]
    print(f"FA1|base_codes={[r['code'] for r in base['restrictions']]} loose_codes={[r['code'] for r in loose['restrictions']]} unconditional_claims={unconditional}")
    assert not unconditional, (f"FA-1 enforcement claims are hard-coded True regardless of the controls: {unconditional} "
                               "(see FA-4..FA-10 for what the runtime role can actually do)")


def test_FA1b_active_stop_is_explained_as_the_enforcing_rule(client, db_session):
    """Req 1/7: when STOP blocks work, the Founder-facing explanation must name STOP (the enforceable rule)."""
    from app.bootstrap import bootstrap_founder_user

    bootstrap_founder_user(db_session)
    db_session.commit()
    csrf = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD}).json()["csrf_token"]
    client.post("/api/mainai/founder-ops/stop", json={"reason": "examiner"}, headers={"X-CSRF-Token": csrf})
    body = client.get("/api/mainai/founder-ops/snapshot").json()
    codes = [r.get("code") for r in (body.get("authority") or {}).get("restrictions", [])] + [b.get("kind") for b in body.get("blockers", [])]
    print(f"FA1b|restriction_and_blocker_codes={codes}")
    assert any("stop" in str(c).lower() for c in codes), f"FA-1b active STOP is not named among restrictions/blockers: {codes}"


def test_FA16_denial_classes_are_distinct_and_machine_readable():
    """Req 7: policy denial, technical failure, unavailable capability, external restriction and security incident are
    distinguishable by type/code, not by message text."""
    from app.mainai_execution.approval import ApprovalRequiredError
    from app.mainai_execution.envelope_scope import PathAEnvelopeError
    from app.providers.base import ProviderError
    from app.workforce.kill_switch import KillSwitchError

    try:
        from app.mainai_execution.publication_errors import ExternalBoundaryError
    except ImportError:
        ExternalBoundaryError = None
    classes = {"policy_denial:approval": ApprovalRequiredError, "policy_denial:envelope": PathAEnvelopeError,
               "stop": KillSwitchError, "unavailable/technical:provider": ProviderError, "external": ExternalBoundaryError}
    incident = None
    for mod, name in (("app.mainai_execution.security_incident", "SecurityIncident"), ("app.security", "SecurityIncidentError")):
        try:
            incident = getattr(__import__(mod, fromlist=[name]), name)
        except (ImportError, AttributeError):
            pass
    print(f"FA16|{ {k: (v.__name__ if v else None) for k, v in classes.items()} } security_incident={incident}")
    distinct = {v for v in classes.values() if v}
    assert len(distinct) == len([v for v in classes.values() if v]), "FA-16 denial classes collapse"
    if incident is None:
        pytest.fail("BLOCKED: no distinct 'genuine security incident' class/code (tamper, forged approval, credential "
                    "exposure are not distinguishable from ordinary failures)")


# --------------------------------------------------------------------------- R6: rollback / revocation
def test_FA14_founder_can_revoke_granted_authority(client, db_session, owner_id):
    """Req 6: every privileged grant needs a rollback path. Spend has revoke; execution envelopes must too."""
    routes = {r.path for r in client.app.routes}
    spend_revoke = any("/provider-spend/" in p and p.endswith("/revoke") for p in routes)
    env_revoke = any("/execution-envelopes/" in p and ("revoke" in p or "rescind" in p) for p in routes)
    print(f"FA14|spend_revoke_route={spend_revoke} envelope_revoke_route={env_revoke}")
    assert spend_revoke
    if not env_revoke:
        pytest.fail("BLOCKED: an authorized execution envelope cannot be revoked by the Founder (only proposals can be rejected)")


def test_FA15_privileged_changes_leave_an_audit_receipt(client, db_session):
    """Req 6: Founder STOP and spend revocation must leave durable, attributable receipts."""
    from app.bootstrap import bootstrap_founder_user
    from app.founder import FOUNDER_USER_ID

    bootstrap_founder_user(db_session)
    db_session.commit()
    csrf = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD}).json()["csrf_token"]
    from app.db import migration_engine

    with migration_engine.connect() as c:
        before = c.execute(text("SELECT count(*) FROM audit_log WHERE user_id=:u"), {"u": str(FOUNDER_USER_ID)}).scalar_one()
    client.post("/api/mainai/founder-ops/stop", json={"reason": "examiner audit"}, headers={"X-CSRF-Token": csrf})
    with migration_engine.connect() as c:
        rows = c.execute(text("SELECT action FROM audit_log WHERE user_id=:u ORDER BY created_at DESC LIMIT 5"), {"u": str(FOUNDER_USER_ID)}).all()
        after = c.execute(text("SELECT count(*) FROM audit_log WHERE user_id=:u"), {"u": str(FOUNDER_USER_ID)}).scalar_one()
    stop_audit = any("stop" in (a or "").lower() or "kill" in (a or "").lower() for (a,) in rows)
    print(f"FA15|audit_rows_added={after - before} recent={[a for (a,) in rows]}")
    assert stop_audit, f"FA-15 Founder STOP left no audit receipt (recent actions={[a for (a,) in rows]})"
