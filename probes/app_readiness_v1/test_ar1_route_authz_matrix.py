"""AR-1 — Authorization matrix over EVERY mounted /api route (not a sample).

Each route x method is called as:
  anon      no session;
  member    a verified ordinary account (with its valid CSRF token);
  impostor  a verified account whose role column says `founder` but whose id is not FOUNDER_USER_ID.
Path parameters are random UUIDs; bodies are `{}`. Providers and egress are blocked (_guards.egress),
the rate limiter is disabled for this module so 429 cannot mask an authorization answer.

Pass criteria
  AR-1a  anon never gets 2xx/5xx anywhere except the public allowlist (health, auth entry points).
  AR-1b  every MainAI / agents / admin / workforce / founder-ops / execution / spend route denies
         member AND impostor with 401/403 before validation or handler logic (no 2xx/404/409/422/5xx).
  AR-1c  no provider call and no egress happen while probing.
The full matrix is written to ar1_matrix.tsv for the evidence package.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from _guards import egress  # noqa: F401

FOUNDER_ONLY_PREFIXES = (
    "/api/mainai", "/api/agents", "/api/admin", "/api/workforce", "/api/execution", "/api/provider-spend",
    "/api/project-entities", "/api/inspectable-memory", "/api/memory", "/api/workbench",
)
# Self-service / entry points that legitimately answer an anonymous caller.
ANON_ALLOWED = re.compile(r"^/api/(health|auth/(login|register|refresh|verify-email|resend-verification|forgot-password|reset-password|csrf|logout)[^/]*)")
SKIP = re.compile(r"^/api/(auth|account)(/|$)")  # session-mutating self-service; covered by CI account suites
OUT = Path(__file__).with_name("ar1_matrix.tsv")


def _routes():
    from app.main import app

    for r in app.routes:
        if isinstance(r, APIRoute) and r.path.startswith("/api") and not SKIP.match(r.path):
            for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
                yield r.path, m


def _fill(path: str) -> str:
    return re.sub(r"\{[^}/]+(:path)?\}", lambda m: "x" if m.group(1) else str(uuid.uuid4()), path)


def _session(app, email=None, password=None):
    c = TestClient(app, base_url="https://testserver")
    c.__enter__()
    csrf = None
    if email:
        res = c.post("/api/auth/login", json={"email": email, "password": password})
        assert res.status_code == 200, res.text
        csrf = res.json()["csrf_token"]
    return c, csrf


def test_AR1_every_api_route(egress, make_verified_user, db_session, monkeypatch):
    from app.limiter import limiter
    from app.main import app

    monkeypatch.setattr(limiter, "enabled", False)
    member, mpw = make_verified_user()
    impostor, ipw = make_verified_user(role="founder")
    db_session.commit()
    roles = {"anon": _session(app), "member": _session(app, member.email, mpw), "impostor": _session(app, impostor.email, ipw)}
    rows, bad = [], []
    try:
        for path, method in _routes():
            url = _fill(path)
            codes = {}
            for role, (c, csrf) in roles.items():
                headers = {"X-CSRF-Token": csrf} if csrf else {}
                try:
                    res = c.request(method, url, json={} if method in ("POST", "PUT", "PATCH") else None, headers=headers)
                    codes[role] = res.status_code
                except Exception as exc:  # noqa: BLE001 — an unhandled server exception is a 5xx
                    codes[role] = f"EXC:{type(exc).__name__}"
            rows.append((method, path, codes["anon"], codes["member"], codes["impostor"]))
            anon = codes["anon"]
            if not ANON_ALLOWED.match(path) and (isinstance(anon, str) or anon < 300 or anon >= 500):
                bad.append(f"AR-1a anon {method} {path} -> {anon}")
            if path.startswith(FOUNDER_ONLY_PREFIXES):
                for role in ("member", "impostor"):
                    if codes[role] not in (401, 403):
                        bad.append(f"AR-1b {role} {method} {path} -> {codes[role]}")
    finally:
        for c, _ in roles.values():
            c.__exit__(None, None, None)
        OUT.write_text("method\tpath\tanon\tmember\timpostor\n" + "\n".join("\t".join(map(str, r)) for r in rows) + "\n")
    print(f"AR1|routes={len(rows)} violations={len(bad)} provider_calls={len(egress.provider_calls)} egress={egress.sockets[:5]}")
    assert not egress.provider_calls and not egress.sockets, f"AR-1c provider={egress.provider_calls[:5]} egress={egress.sockets[:5]}"
    assert not bad, "\n".join(bad[:60])
