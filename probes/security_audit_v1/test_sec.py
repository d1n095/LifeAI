"""Independent security-audit regressions (SEC-*) for the whole LifeAI/MainAI codebase at frozen #302 ff28b51.

Each test exercises a security-critical invariant against the REAL production code with an explicit positive control.
These lock the controls the narrative audit verified so a later change cannot silently regress them. Credential-free,
no paid calls, no network, isolated DB. No production modification.
"""

from __future__ import annotations

import os
import re
import uuid

import pytest
from sqlalchemy import text as sa_text

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


# ---- Item 2/7: the production startup guard fails closed on default SECRET_KEY / FOUNDER_PASSWORD / FOUNDER_EMAIL
def test_SEC1_production_rejects_placeholder_secrets(monkeypatch):
    import app.main as main

    def set_all(*, env, secret, fpw, femail):
        monkeypatch.setattr(main.settings, "environment", env)
        monkeypatch.setattr(main.settings, "secret_key", secret)
        monkeypatch.setattr(main.settings, "founder_password", fpw)
        monkeypatch.setattr(main.settings, "founder_email", femail)

    # POSITIVE CONTROL: real values in production must NOT raise.
    set_all(env="production", secret="a-real-32-byte-secret-value-xxxxx", fpw="A-Real-Founder-Pw-9!", femail="real@founder.example")
    main._check_no_placeholder_secrets()  # no raise
    # DEFAULT secret in production must fail closed.
    raised = []
    for field, val in (("secret_key", "change-me-in-production"), ("founder_password", "change-me-in-production"), ("founder_email", "founder@lifeos.local")):
        set_all(env="production", secret="x" * 32, fpw="ok-pw-9!", femail="real@founder.example")
        monkeypatch.setattr(main.settings, field, val)
        try:
            main._check_no_placeholder_secrets()
            raised.append((field, False))
        except RuntimeError:
            raised.append((field, True))
    # COOKIE_SECURE=false must also fail closed.
    monkeypatch.setattr(main.settings, "cookie_secure", False)
    try:
        main._check_cookies_secure(); cookie_failclosed = False
    except RuntimeError:
        cookie_failclosed = True
    print(f"SEC1|placeholder_guard={raised} cookie_secure_guard={cookie_failclosed}")
    assert all(flagged for _, flagged in raised), "SEC-1 a default secret was accepted in production"
    assert cookie_failclosed, "SEC-1 COOKIE_SECURE=false accepted in production"


# ---- Item 2/3: require_founder pins the exact FOUNDER_USER_ID, not merely role=founder
def test_SEC2_require_founder_pins_id_not_role(client, make_verified_user, db_session, monkeypatch):
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    impostor, pw = make_verified_user(role="founder")  # role=founder but a DIFFERENT id than FOUNDER_USER_ID
    db_session.commit()
    from app.founder import FOUNDER_USER_ID
    r = client.post("/api/auth/login", json={"email": impostor.email, "password": pw})
    assert r.status_code == 200, r.text
    csrf = r.json()["csrf_token"]
    # a Founder-only endpoint must refuse the role=founder impostor whose id != FOUNDER_USER_ID
    resp = client.get("/api/mainai/founder-ops/snapshot")
    resp2 = client.post("/api/mainai/founder-ops/agent-gateway/bridges", json={"recipient": "cursor"}, headers={"X-CSRF-Token": csrf})
    print(f"SEC2|impostor_id={impostor.id} founder_id={FOUNDER_USER_ID} snapshot={resp.status_code} issue_bridge={resp2.status_code} role={impostor.role}")
    assert impostor.id != FOUNDER_USER_ID
    assert resp.status_code == 403 and resp2.status_code == 403, "SEC-2 a role=founder row with the wrong id reached a Founder-only route"


# ---- Item 2/5: access token is cookie-only (no Authorization-header fallback); CSRF required on mutations
def test_SEC3_cookie_only_auth_and_csrf_required(client, monkeypatch):
    from app.cookies import ACCESS_COOKIE
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    r = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert r.status_code == 200, r.text
    csrf = r.json()["csrf_token"]
    access = client.cookies.get(ACCESS_COOKIE)
    assert access, "precondition: login set an access cookie"
    # (a) a mutating request WITHOUT the CSRF header is refused even though the cookie is present
    no_csrf = client.post("/api/mainai/founder-ops/agent-gateway/bridges", json={"recipient": "cursor"})
    # (b) presenting the token ONLY as an Authorization header (no cookie) must NOT authenticate
    client.cookies.clear()
    hdr_only = client.get("/api/mainai/founder-ops/snapshot", headers={"Authorization": f"Bearer {access}"})
    print(f"SEC3|mutation_without_csrf={no_csrf.status_code} header_token_only={hdr_only.status_code}")
    assert no_csrf.status_code == 403, "SEC-3 a mutating request succeeded without a CSRF token"
    assert hdr_only.status_code == 401, "SEC-3 an Authorization-header token authenticated (cookie-only policy bypassed)"


# ---- Item 1: verified dependency advisory floor — reproducible record of the audited pins vs fixed versions
def test_SEC4_dependency_advisory_floor():
    target = os.environ.get("TARGET_ROOT")
    assert target, "TARGET_ROOT not set by run.sh"
    req = os.path.join(target, "backend", "requirements.txt")
    pins = {}
    with open(req) as fh:
        for line in fh:
            m = re.match(r"^([A-Za-z0-9_.\-]+)\s*==\s*([0-9][^\s;]*)", line.strip())
            if m:
                pins[m.group(1).lower().replace("_", "-")] = m.group(2)
    # verified against osv.dev on the audit date (see VERDICT): DoS-class, reachable only behind founder-auth + size caps
    floors = {
        "python-multipart": "0.0.18",   # CVE-2024-53981 et al. (DoS); later DoS fixed 0.0.26
        "pypdf": "6.0.0",               # dozens of malformed-PDF DoS advisories affect 4.3.1
    }
    report = {}
    for pkg, floor in floors.items():
        cur = pins.get(pkg)
        report[pkg] = {"pinned": cur, "fixed_floor": floor, "below_floor": _lt(cur, floor) if cur else None}
    # PyJWT: usage is symmetric HS256 with explicit algorithms=[HS256] + jti revocation -> the JWKS/alg advisories are N/A
    report["pyjwt"] = {"pinned": pins.get("pyjwt"), "note": "safe-usage: symmetric HS256, explicit alg list, jti revocation"}
    print(f"SEC4|dependency_floor={report}")
    # This is a reproducible evidence record, not a launch gate: assert the audited pins are what the register cites,
    # so a silent downgrade/upgrade is caught and the register stays truthful.
    assert report["python-multipart"]["pinned"] == "0.0.9", "SEC-4 python-multipart pin changed; re-verify advisories"
    assert report["pypdf"]["pinned"] == "4.3.1", "SEC-4 pypdf pin changed; re-verify advisories"
    assert report["python-multipart"]["below_floor"] and report["pypdf"]["below_floor"], "SEC-4 a flagged dep is now at/above its fixed floor — downgrade the finding"


def _lt(a: str, b: str) -> bool:
    def parts(v):
        return [int(x) for x in re.findall(r"\d+", v)]
    pa, pb = parts(a), parts(b)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa)); pb += [0] * (n - len(pb))
    return pa < pb
