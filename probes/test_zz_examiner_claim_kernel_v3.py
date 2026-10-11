"""Examiner-only kernel probes for 6bd8bf7 (disposable, never committed). Prints HOLDS/GAP per property."""
import uuid
from datetime import datetime, timedelta, timezone

from app.claim_action_integrity import ClaimActionIntegrityError, assess_claim, record_evidence, record_receipt
from app.models.user import User

SHA, OTHER = "1" * 40, "2" * 40
NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=30)
ISSUER = "founder-capability-authority"


def _owner(db):
    o = User(email=f"p6-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(o)
    db.flush()
    return o


def ev(db, o, *, action, source, payload, execution="exec-1", sha=SHA, observed_at=None, subject="release:alpha", expires_at=None):
    return record_evidence(db, owner_id=o.id, execution_id=execution, subject_key=subject, action_key=action, source_type=source,
                           source_ref=f"opaque:{uuid.uuid4()}", payload=payload, artifact_sha=sha, observed_at=observed_at,
                           expires_at=expires_at, recorded_by="probe")


def assess(db, o, action, state, e=None, *, execution="exec-1", sha=SHA, subject="release:alpha"):
    return assess_claim(db, owner_id=o.id, execution_id=execution, subject_key=subject, action_key=action,
                        requested_state=state, artifact_sha=sha, evidence_id=e.id if e else None)


def report(name, holds, r=None, extra=""):
    d = f":: claimable={r.claimable} effective={r.effective_state} reasons={r.reasons}" if r is not None else ""
    print(f"\n{'HOLDS' if holds else 'GAP  '} {name} {d} {extra}")


# ---------------------------------------------------------------- REQUESTED invariant
def test_requested_invariant(superuser_db):
    o = _owner(superuser_db)
    for label, src, payload in (("self-report failure", "agent_self_report", {"status": "failed"}),
                                ("non-failure CI pass", "ci", {"status": "passed", "passed": 1, "failed": 0, "command": "p", "environment": {}, "execution_id": "exec-1", "artifact_sha": SHA}),
                                ("provider error payload", "external_service", {"status": "error", "message": "provider 500"})):
        e = ev(superuser_db, o, action="test_run", source=src, payload=payload, execution=f"exec-r-{label[:4]}")
        for state in ("failed", "completed", "unknown", "externally_observed"):
            r = assess(superuser_db, o, "test_run", state, e, execution=f"exec-r-{label[:4]}")
            report(f"[{label}] claim {state!r} never yields REQUESTED", r.effective_state != "requested", r)
    r = assess(superuser_db, o, "test_run", "requested")
    report("REQUESTED without receipt not claimable", not r.claimable and r.effective_state != "requested", r)


# ---------------------------------------------------------------- freshness model
def test_freshness(superuser_db):
    o = _owner(superuser_db)
    hist = {"status": "succeeded", "deployment_id": "d1", "artifact_sha": SHA, "evidence_semantics": "historical_event", "completed_at": OLD.isoformat()}
    cur = {"status": "deployed", "deployment_id": "d1", "artifact_sha": SHA, "evidence_semantics": "current_state", "provider_observation_id": "po1", "observed_at": OLD.isoformat()}
    e = ev(superuser_db, o, action="deployment", source="deployment_provider", payload=hist, observed_at=OLD, execution="x-dh")
    r = assess(superuser_db, o, "deployment", "completed", e, execution="x-dh"); report("historical deployment event (30d) stays valid as completed", r.claimable, r)
    r = assess(superuser_db, o, "deployment", "deployed", e, execution="x-dh"); report("historical deployment event cannot prove CURRENTLY deployed", not r.claimable, r)
    e = ev(superuser_db, o, action="deployment", source="deployment_provider", payload=cur, observed_at=OLD, execution="x-dc")
    r = assess(superuser_db, o, "deployment", "deployed", e, execution="x-dc"); report("30d current-state deployment snapshot stale", not r.claimable, r, f"mutability={e.payload['fact_mutability']}")
    e = ev(superuser_db, o, action="deployment", source="deployment_provider", payload={k: v for k, v in cur.items() if k != "evidence_semantics"}, observed_at=OLD, execution="x-dn")
    r = assess(superuser_db, o, "deployment", "deployed", e, execution="x-dn"); report("deployment without semantics label cannot prove deployed", not r.claimable, r)
    # external-service state: same observation labelled historical vs current
    base = {"owner_id": str(o.id), "subject_key": "release:alpha", "action_key": "provider_health", "observed_state": "completed",
            "provider_id": "p", "provider_observation_id": "o", "observed_at": OLD.isoformat()}
    e = ev(superuser_db, o, action="provider_health", source="external_service", sha=None, observed_at=OLD, execution="x-ec",
           payload={**base, "execution_id": "x-ec", "evidence_semantics": "current_state"})
    r = assess(superuser_db, o, "provider_health", "completed", e, execution="x-ec", sha=None); report("30d external-service current_state stale", not r.claimable, r)
    e = ev(superuser_db, o, action="provider_health", source="external_service", sha=None, observed_at=OLD, execution="x-eh",
           payload={**base, "execution_id": "x-eh", "evidence_semantics": "historical_event", "completed_at": OLD.isoformat()})
    r = assess(superuser_db, o, "provider_health", "completed", e, execution="x-eh", sha=None)
    report("[label-trust] same mutable provider state relabelled historical_event by recorder proves completion forever", not r.claimable, r)
    # database observation and task execution state
    dbp = {"owner_id": str(o.id), "execution_id": "x-db", "subject_key": "release:alpha", "action_key": "database_observation", "observed_state": "completed",
           "observation_id": "ob", "observed_at": OLD.isoformat()}
    e = ev(superuser_db, o, action="database_observation", source="database", sha=None, observed_at=OLD, execution="x-db", payload=dbp)
    r = assess(superuser_db, o, "database_observation", "completed", e, execution="x-db", sha=None); report("30d database observation stale", not r.claimable, r)


# ---------------------------------------------------------------- generic payload validation
def test_generic_payloads(superuser_db):
    o = _owner(superuser_db)
    full = {"owner_id": str(o.id), "execution_id": "x-g", "subject_key": "release:alpha", "action_key": "send_mail", "observed_state": "completed",
            "observation_id": "ob", "observed_at": NOW.isoformat(), "provider_id": "p", "provider_observation_id": "po", "evidence_semantics": "historical_event",
            "completed_at": NOW.isoformat(), "job_id": "j", "result_artifact_id": "ra"}
    for src in ("database", "external_service", "task_execution_ledger"):
        e = ev(superuser_db, o, action="send_mail", source=src, payload={}, sha=None, execution="x-g")
        r = assess(superuser_db, o, "send_mail", "completed", e, execution="x-g", sha=None); report(f"empty {src} payload cannot prove completion", not r.claimable, r)
        e = ev(superuser_db, o, action="send_mail", source=src, payload=dict(full), sha=None, execution="x-g")
        r = assess(superuser_db, o, "send_mail", "completed", e, execution="x-g", sha=None); report(f"[control] fully bound {src} payload proves completion", r.claimable, r)
        for missing in ("owner_id", "execution_id", "subject_key", "action_key", "observed_state", "observed_at"):
            p = {k: v for k, v in full.items() if k != missing}
            e = ev(superuser_db, o, action="send_mail", source=src, payload=p, sha=None, execution="x-g")
            r = assess(superuser_db, o, "send_mail", "completed", e, execution="x-g", sha=None)
            report(f"{src} missing {missing} rejected", not r.claimable, r)
        p = dict(full, owner_id=str(uuid.uuid4()))
        e = ev(superuser_db, o, action="send_mail", source=src, payload=p, sha=None, execution="x-g")
        r = assess(superuser_db, o, "send_mail", "completed", e, execution="x-g", sha=None); report(f"{src} payload naming another owner rejected", not r.claimable, r)


# ---------------------------------------------------------------- permission authority
def _grant(db, o, *, execution="x-p", source="permission_authority", observed_at=None, **over):
    now = observed_at or NOW
    p = {"owner_id": str(o.id), "principal": "codex", "execution_id": execution, "resource": "release:alpha", "action": "deployment",
         "issuer_authority": ISSUER, "granted": True, "revoked": False, "grant_id": "g-1", "limits": {"max_runs": 1},
         "scopes": ["deployment"], "issued_at": (now - timedelta(seconds=10)).isoformat(), "expires_at": (now + timedelta(hours=1)).isoformat()}
    p.update(over)
    return ev(db, o, action="permission_grant", source=source, payload=p, sha=None, execution=execution, observed_at=now)


def _use(db, o, g, *, execution, principal="codex", authority_ref=ISSUER, limits=None, action="deployment", subject="release:alpha"):
    try:
        record_receipt(db, owner_id=o.id, execution_id=execution, subject_key=subject, action_key=action, declared_state="requested",
                       action_state="dispatched", declared_action={"d": 1},
                       permitted_action={"granted": True, "authority_ref": authority_ref, "principal": principal, "scopes": [action],
                                         "grant_evidence_id": str(g.id), "limits": limits or {"max_runs": 1}},
                       executed_action={"run": 1}, observed_result={}, authority_snapshot={}, created_by="control-plane")
        return True, "accepted"
    except ClaimActionIntegrityError as exc:
        return False, str(exc)


def _case(db, o, name, *, use_kw=None, grant_kw=None, expect_accept=False, use_owner=None):
    ex = f"x-{uuid.uuid4().hex[:8]}"
    g = _grant(db, o, execution=ex, **(grant_kw or {}))
    kw = dict(use_kw or {})
    kw.setdefault("execution", ex)
    ok, msg = _use(db, use_owner or o, g, **kw)
    perm_reason = ("permission" in msg) or ok
    holds = (ok == expect_accept) and (expect_accept or perm_reason)
    report(name, holds, extra=f"-> {msg[:90]}")


def test_permission_authority(superuser_db):
    o = _owner(superuser_db)
    _case(superuser_db, o, "[control] valid permission_authority grant accepted", expect_accept=True)
    for src in ("github", "filesystem", "database", "ci"):
        _case(superuser_db, o, f"grant from {src} rejected", grant_kw={"source": src})
    _case(superuser_db, o, "wrong principal rejected", use_kw={"principal": "cursor"})
    _case(superuser_db, o, "grant WITHOUT principal + request WITHOUT principal rejected", grant_kw={"principal": None}, use_kw={"principal": None})
    _case(superuser_db, o, "wrong issuer (authority_ref) rejected", use_kw={"authority_ref": "someone-else"})
    _case(superuser_db, o, "non-allowlisted issuer rejected", grant_kw={"issuer_authority": "rogue-authority"}, use_kw={"authority_ref": "rogue-authority"})
    _case(superuser_db, o, "wrong execution rejected", use_kw={"execution": "x-other-" + uuid.uuid4().hex[:6]})
    _case(superuser_db, o, "wrong action rejected", use_kw={"action": "merge"})
    _case(superuser_db, o, "wrong resource rejected", use_kw={"subject": "release:beta"})
    _case(superuser_db, o, "WIDER limits than granted rejected (granted max_runs=1, requested 1000)", use_kw={"limits": {"max_runs": 1000}})
    _case(superuser_db, o, "expired grant (payload expires_at) rejected", grant_kw={"expires_at": (NOW - timedelta(minutes=1)).isoformat()})
    _case(superuser_db, o, "stale grant observation (>5 min) rejected", grant_kw={"observed_at": NOW - timedelta(minutes=10), "issued_at": (NOW - timedelta(minutes=11)).isoformat()})
    _case(superuser_db, o, "grant from another owner rejected", use_owner=_owner(superuser_db))


def test_permission_revocation_ordering(superuser_db):
    o = _owner(superuser_db)
    ex = "x-rev1"
    g = _grant(superuser_db, o, execution=ex, grant_id="g-rev", observed_at=NOW - timedelta(minutes=2))
    _grant(superuser_db, o, execution=ex, grant_id="g-rev", revoked=True, observed_at=NOW - timedelta(minutes=1))
    ok, msg = _use(superuser_db, o, g, execution=ex)
    report("newer revocation overrides earlier active grant", not ok and "revoked" in msg, extra=f"-> {msg[:80]}")
    ex2 = "x-rev2"
    _grant(superuser_db, o, execution=ex2, grant_id="g-rev2", observed_at=NOW - timedelta(minutes=2))
    _grant(superuser_db, o, execution=ex2, grant_id="g-rev2", revoked=True, observed_at=NOW - timedelta(minutes=1))
    copy = _grant(superuser_db, o, execution=ex2, grant_id="g-rev2", observed_at=NOW - timedelta(seconds=5))
    ok, msg = _use(superuser_db, o, copy, execution=ex2)
    report("re-recorded (copied) old grant payload cannot resurrect a revoked grant", not ok, extra=f"(grant t-2m, revoke t-1m, copy revoked=False t-5s) -> {msg[:60]}")
    ex3 = "x-rev3"
    g3 = _grant(superuser_db, o, execution=ex3, grant_id="g-rev3", observed_at=NOW - timedelta(seconds=30))
    _grant(superuser_db, o, execution=ex3, grant_id="g-rev3", revoked=True, observed_at=NOW - timedelta(minutes=3))
    ok, msg = _use(superuser_db, o, g3, execution=ex3)
    report("revocation recorded with an EARLIER observation time still revokes", not ok and "revoked" in msg, extra=f"(revoke t-3m, grant t-30s) -> {msg[:60]}")


# ---------------------------------------------------------------- previously-fixed properties stay green
def test_prior_properties(superuser_db):
    o = _owner(superuser_db)
    cert = {"review_result": "PASS", "candidate_sha": SHA, "builder_actor_id": "codex", "examiner_actor_id": "claude"}
    e = ev(superuser_db, o, action="certification", source="verification_registry", payload=cert, execution="x-c")
    r = assess(superuser_db, o, "certification", "certified", e, execution="x-c", sha=None); report("cert requires caller SHA", not r.claimable, r)
    r = assess(superuser_db, o, "certification", "certified", e, execution="x-c", sha=OTHER); report("cert wrong SHA rejected", not r.claimable, r)
    for i, ex in enumerate(["Codex", " CODEX ", "ｃｏｄｅｘ", "сodex"]):
        e2 = ev(superuser_db, o, action="certification", source="verification_registry", payload=dict(cert, examiner_actor_id=ex), execution=f"x-a{i}")
        r = assess(superuser_db, o, "certification", "certified", e2, execution=f"x-a{i}"); report(f"actor independence: examiner {ex!r} vs codex", not r.claimable, r)
    tp = lambda st, f: {"status": st, "passed": 5, "failed": f, "command": "p", "environment": {}, "execution_id": "x-t", "artifact_sha": SHA}
    bad = ev(superuser_db, o, action="test_run", source="ci", payload=tp("failed", 1), observed_at=NOW - timedelta(hours=2), execution="x-t")
    good = ev(superuser_db, o, action="test_run", source="ci", payload=tp("passed", 0), observed_at=NOW - timedelta(hours=1), execution="x-t")
    ev(superuser_db, o, action="test_run", source="agent_self_report", payload={"status": "failed"}, observed_at=NOW - timedelta(minutes=5), execution="x-t")
    r = assess(superuser_db, o, "test_run", "failed", bad, execution="x-t"); report("older failure superseded by newer pass", not r.claimable, r)
    r = assess(superuser_db, o, "test_run", "externally_observed", good, execution="x-t"); report("later self-report cannot veto authoritative pass", r.claimable, r)
    o2 = _owner(superuser_db)
    r = assess(superuser_db, o2, "test_run", "externally_observed", good, execution="x-t"); report("wrong owner rejected", not r.claimable, r)
    r = assess(superuser_db, o, "test_run", "externally_observed", good, execution="x-zz"); report("wrong execution rejected", not r.claimable, r)
