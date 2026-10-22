"""Security Diagnostics & Threat Monitoring (#296) — independent adversarial acceptance (SD-*).
Runs the real HTTP app and the real service functions. Providers/egress are blocked (no paid calls)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from _guards import egress  # noqa: F401  (blocks provider calls + non-loopback sockets)
from _harness import _privileges, owner_id  # noqa: F401

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
SEC = "/api/mainai/security"
ENDPOINTS = [
    ("GET", f"{SEC}/dashboard"), ("GET", f"{SEC}/restrictions"),
    ("POST", f"{SEC}/remediation/propose"), ("POST", f"{SEC}/remediation/{{id}}/approve"),
    ("POST", f"{SEC}/remediation/{{id}}/apply"), ("POST", f"{SEC}/incidents/preserve"),
    ("POST", f"{SEC}/incidents/{{id}}/acknowledge"),
]


def _csrf(client, email=FOUNDER_EMAIL, password=FOUNDER_PASSWORD):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


# ---- SD-1: every security endpoint is Founder-only (role AND id)
def test_SD1_security_endpoints_are_founder_only(client, make_verified_user, db_session, monkeypatch):
    from app.limiter import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    member, mpw = make_verified_user()
    impostor, ipw = make_verified_user(role="founder")
    db_session.commit()
    bad = []
    for method, path in ENDPOINTS:
        url = path.replace("{id}", str(uuid.uuid4()))
        assert client.request(method, url, json={}).status_code in (401, 403), f"anon reached {path}"
        for email, pw in ((member.email, mpw), (impostor.email, ipw)):
            csrf = _csrf(client, email, pw)
            code = client.request(method, url, json={}, headers={"X-CSRF-Token": csrf}).status_code
            if code not in (401, 403):
                bad.append(f"{method} {path} as {'impostor' if email==impostor.email else 'member'} -> {code}")
            client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
            client.cookies.clear()
    print(f"SD1|violations={bad}")
    assert not bad, bad


# ---- SD-2: remediation can never be applied autonomously, incl. direct service calls
def test_SD2_remediation_apply_is_always_refused(client, db_session, monkeypatch):
    from app.config import get_settings
    from app.limiter import limiter
    from app.mainai_security.remediation import SensitiveChangeRefused, approve_remediation, propose_remediation, refuse_apply
    from app.bootstrap import bootstrap_founder_user
    from app.founder import FOUNDER_USER_ID

    monkeypatch.setattr(limiter, "enabled", False)
    bootstrap_founder_user(db_session)
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": str(FOUNDER_USER_ID)})
    settings = get_settings()
    before = (settings.github_write_enabled, settings.mainai_activation_mode, settings.secret_key)
    prop = propose_remediation(db_session, owner_id=FOUNDER_USER_ID, finding_id="f1", change_class="policy",
                               title="t", summary="s", isolated_test_plan="p")
    approve_remediation(db_session, owner_id=FOUNDER_USER_ID, proposal_id=prop["proposal_id"])
    # Even a direct service call to apply a credential/policy mutation must refuse.
    with pytest.raises(SensitiveChangeRefused):
        refuse_apply(db_session, owner_id=FOUNDER_USER_ID, proposal_id=prop["proposal_id"],
                     reason="examiner attempt to apply github_write_enabled=true")
    after = (settings.github_write_enabled, settings.mainai_activation_mode, settings.secret_key)
    print(f"SD2|applied={prop['applied']} settings_before==after={before==after}")
    assert before == after, "SD-2 a security remediation mutated settings"
    assert prop["applied"] is False and prop.get("live_checkout_written") is False


def test_SD2b_apply_endpoint_refuses_sensitive_payload(client, db_session, monkeypatch):
    from app.limiter import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    csrf = _csrf(client)
    pr = client.post(f"{SEC}/remediation/propose", json={"finding_id": "f", "change_class": "dependency_upgrade",
                     "title": "t", "summary": "s", "isolated_test_plan": "p"}, headers={"X-CSRF-Token": csrf})
    assert pr.status_code == 200, pr.text
    pid = pr.json()["proposal_id"]
    client.post(f"{SEC}/remediation/{pid}/approve", headers={"X-CSRF-Token": csrf})
    resp = client.post(f"{SEC}/remediation/{pid}/apply", json={"github_write_enabled": True, "secret_key": "x"},
                       headers={"X-CSRF-Token": csrf})
    print(f"SD2b|apply_status={resp.status_code}")
    assert resp.status_code == 409, f"SD-2b apply with a sensitive payload was not refused: {resp.status_code} {resp.text}"


# ---- SD-3: forged / malicious audit events do not fabricate confirmed incidents or trigger action
def test_SD3_forged_unknown_audit_actions_are_not_incidents(db_session, owner_id):
    from app.mainai_security.monitoring import classify_audit_rows
    from types import SimpleNamespace

    forged = [SimpleNamespace(action="attacker_declares_breach", user_id=owner_id, ip_address="9.9.9.9",
                              detail="evil", entity_type="security_incident", created_at=None) for _ in range(50)]
    incidents = classify_audit_rows(forged, owner_id=owner_id)
    print(f"SD3|incidents_from_50_forged_unknown={len(incidents)}")
    assert incidents == [], "SD-3 forged unknown-action rows were turned into incidents"


def test_SD3b_fabricated_known_rows_stay_uncertain_or_evidence_only(db_session, owner_id):
    """A model-driven path could INSERT known-action rows (audit_log is runtime-writable, LEGACY). Those must not be
    presented as confirmed truth beyond what the row count supports, and must never trigger an autonomous action."""
    from app.mainai_security.monitoring import classify_audit_rows
    from types import SimpleNamespace

    rows = [SimpleNamespace(action="login_failed", user_id=owner_id, ip_address=f"1.2.3.{i}", detail="a@b.c",
                            entity_type=None, created_at=None) for i in range(3)]
    inc = classify_audit_rows(rows, owner_id=owner_id)
    print(f"SD3b|incidents={[(i['kind'], i['confidence']) for i in inc]}")
    # None of these 3 forgeable rows may be presented as 'confirmed'; and no recommendation may INSTRUCT an autonomous
    # action (disabling an account, applying a change). A sentence that says "do NOT disable ... automatically" is correct.
    assert all(i["confidence"] != "confirmed" for i in inc), "SD-3b 3 forgeable rows produced a 'confirmed' incident"
    for i in inc:
        act = str(i.get("recommended_action", "")).lower()
        instructs_autodisable = ("disable" in act or "apply" in act) and "do not" not in act and "never" not in act
        assert not instructs_autodisable, f"SD-3b recommends an autonomous action: {act!r}"


# ---- SD-4: secret redaction never echoes raw secrets
SECRETS = {
    "openai_sk": "sk-" + "A" * 40, "aws": "AKIA" + "B" * 16, "ghp": "ghp_" + "c" * 36,
    "pat": "github_pat_" + "d" * 30, "slack": "xoxb-" + "1" * 20,
    "jwt": "eyJhbGciOiJ." + "e" * 20 + "." + "f" * 20, "pem": "-----BEGIN RSA PRIVATE KEY-----MIIExx",
    "pg": "postgresql://user:SuperSecret123@host/db", "bearer": "Bearer " + "g" * 30,
    "assignment": "secret_key='TopSecretValue123'",
}


def test_SD4_redaction_never_echoes_raw_secret():
    from app.mainai_security.leak import redact_text, scan_text_for_leaks

    leaked = []
    for name, raw in SECRETS.items():
        red, cats = redact_text(raw)
        secret_core = raw.split("://")[-1] if "pg" == name else raw.split("'")[1] if name == "assignment" else raw[-20:]
        if name == "pg":
            secret_core = "SuperSecret123"
        if secret_core and secret_core in red:
            leaked.append((name, red))
        if not cats:
            leaked.append((name, f"NOT DETECTED: {red}"))
        findings = scan_text_for_leaks(f"prefix {raw} suffix", source="test")
        for f in findings:
            if secret_core and secret_core in str(f):
                leaked.append((name, f"in finding: {f}"))
    print(f"SD4|leaked={leaked}")
    assert not leaked, f"SD-4 raw secret present in redacted output / finding: {leaked}"


def test_SD4b_redaction_positive_control_preserves_nonsecret():
    """Control: redaction must not destroy ordinary text (would make the engine useless / lie about content)."""
    from app.mainai_security.leak import redact_text

    red, cats = redact_text("the deployment finished at 12:00 and the build id is 12345")
    print(f"SD4b|red={red!r} cats={cats}")
    assert "deployment finished" in red and not cats, "SD-4b redaction corrupted non-secret text or false-positived"


# ---- SD-5: dashboard status truthful; never sets GO
def test_SD5_dashboard_truthful_and_never_go(client, db_session, monkeypatch):
    from app.limiter import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    _csrf(client)
    body = client.get(f"{SEC}/dashboard").json()
    print(f"SD5|keys={sorted(body)[:12]} go={body.get('first_cycle_go')}")
    assert body.get("first_cycle_go") is False, "SD-5 security dashboard sets GO true"
    # Founder-ops snapshot must still be false and must not inherit a fabricated 'operational' from this module.
    snap = client.get("/api/mainai/founder-ops/snapshot").json()
    assert snap["first_cycle_go"] is False


def test_SD5b_findings_report_confidence_and_evidence(client, monkeypatch):
    """Req 3: findings must carry confidence and evidence, not bare assertions."""
    from app.limiter import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    _csrf(client)
    body = client.get(f"{SEC}/dashboard").json()
    import json as _j

    blob = _j.dumps(body)
    assert "confidence" in blob and "evidence" in blob, "SD-5b findings lack confidence/evidence fields"


# ---- SD-6: incident preservation redacts and cannot escalate
def test_SD6_incident_preserve_redacts_and_does_not_escalate(client, db_session, monkeypatch):
    from app.config import get_settings
    from app.limiter import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    csrf = _csrf(client)
    settings = get_settings()
    before = settings.github_write_enabled
    raw = "sk-" + "Z" * 40
    r = client.post(f"{SEC}/incidents/preserve", json={"kind": "test", "confidence": "uncertain", "severity": "low",
                    "evidence": {"blob": raw, "grant_founder": True, "role": "founder"}}, headers={"X-CSRF-Token": csrf})
    print(f"SD6|status={r.status_code} body_has_raw={raw in r.text}")
    assert r.status_code == 200, r.text
    assert raw not in r.text, "SD-6 incident preservation echoed a raw secret"
    assert settings.github_write_enabled == before, "SD-6 incident evidence mutated settings"
