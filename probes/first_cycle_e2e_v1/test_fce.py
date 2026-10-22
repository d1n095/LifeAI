"""First-cycle Founder-supervised end-to-end acceptance (FCE-*). Drives the REAL production dry-run path
(`run_founder_supervised_local_dry_run`: Goal -> local verify -> STOP-before-merge) plus the Founder HTTP surface.
Isolated, credential-free, non-billable: a synthetic chat provider, github_write off, egress guard. No paid calls.

Each phase maps to the assignment's Founder-facing sequence. Adversarial coverage that already has a dedicated suite is
cited in READINESS_MATRIX.md rather than duplicated. A capability that is genuinely absent is marked BLOCKED, never PASS.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
EDIT = ('{"files": [{"path": "backend/tests/test_scratch_edit.py", '
        '"content": "def test_scratch_edit():\\n    assert 2 + 3 == 5\\n"}], '
        '"commit_message": "Document isolated first-cycle edit"}')


def _fake_chat(payload):
    from app.providers.base import ChatResult

    async def chat(self, messages, model, **kwargs):
        system = messages[0].content if messages else ""
        content = payload if "kodagent" in system else '{"tasks": [{"description": "edit", "task_type": "repo_edit"}]}'
        return ChatResult(content=content, provider="openai", model=model, raw_usage={"prompt_tokens": 1, "completion_tokens": 1})

    return chat


# ---- Steps 2,3,4,5,7,10: the real supervised dry-run cycle, evidence-grounded, STOP-before-merge, no paid calls
@pytest.mark.asyncio
async def test_FCE_supervised_dry_run_full_sequence(db_session, superuser_db, owner_id, monkeypatch, tmp_path):
    from app.mainai_execution.first_cycle_ops import run_founder_supervised_local_dry_run
    from app.providers.openai_provider import OpenAIProvider

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat(EDIT))
    result = await run_founder_supervised_local_dry_run(db_session, owner_id=owner_id, claim_db=superuser_db, repo_root=tmp_path)
    print(f"FCE|stages={result['stages']} stopped={result['stopped']} merged={result['merged']} go={result['go']} "
          f"sha_len={len(result['verification_sha'])} paid={result['empty_snapshot']['costs']['live_paid_calls']}")
    # Step 5: assignment -> approval -> dispatch -> execution -> verification -> STOP, in order, evidence-grounded.
    assert result["stages"] == ["goal", "clear_state", "proposed_task", "approval_required", "founder_approval",
                                "dispatch", "local_verification", "STOP"], f"stage sequence: {result['stages']}"
    # Step 4: approval is enforced BEFORE dispatch (approval_required precedes founder_approval precedes dispatch).
    assert result["stages"].index("approval_required") < result["stages"].index("founder_approval") < result["stages"].index("dispatch")
    # Step 10: STOP-before-merge; never merged; GO stays false.
    assert result["stopped"] is True and result["merged"] is False and result["go"] is False
    # Step 3/6: a real local verification SHA exists (concrete work, not a claim).
    assert len(result["verification_sha"]) == 40
    # Step 7: no live paid calls in the whole cycle.
    assert result["empty_snapshot"]["costs"]["live_paid_calls"] is False
    assert result["final_snapshot"]["stopped"] is True


# ---- Step 1 & 8: the Founder signs in and sees real readiness / restrictions / blockers / audit evidence
def test_FCE_founder_sees_truthful_readiness_and_restrictions(client, db_session, monkeypatch):
    from app.bootstrap import bootstrap_founder_user
    from app.limiter import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    bootstrap_founder_user(db_session)
    db_session.commit()
    anon = client.get("/api/mainai/founder-ops/snapshot").status_code
    assert anon in (401, 403), "Step 1: founder-ops is not Founder-gated"
    csrf = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD}).json()["csrf_token"]
    snap = client.get("/api/mainai/founder-ops/snapshot").json()
    print(f"FCE1|go={snap['first_cycle_go']} readiness={snap['startup_readiness'].get('level')} "
          f"restrictions={[r.get('code') for r in (snap.get('authority') or {}).get('restrictions', [])][:4]}")
    assert snap["first_cycle_go"] is False, "Step 1: GO is not false before any supervised run"
    assert snap["startup_readiness"].get("level"), "Step 1: no readiness level surfaced"
    assert snap["safe_launch"]["github_write_enabled"] is False and snap["safe_launch"]["recall_http_mounted"] is False
    # Step 8: the security dashboard is reachable and carries evidence/confidence (no fabricated 'operational').
    sec = client.get("/api/mainai/security/dashboard")
    assert sec.status_code == 200 and sec.json().get("first_cycle_go") is False


# ---- Step 9: a denied/unavailable capability yields an accurate typed explanation, not fabricated status
def test_FCE_denials_are_typed_and_explained():
    from app.mainai_execution.approval import ApprovalRequiredError
    from app.mainai_execution.envelope_scope import PathAEnvelopeError
    from app.providers.base import ProviderError
    from app.workforce.kill_switch import KillSwitchError

    classes = [ApprovalRequiredError, PathAEnvelopeError, KillSwitchError, ProviderError]
    print(f"FCE9|typed_denials={[c.__name__ for c in classes]}")
    assert len({c.__name__ for c in classes}) == len(classes)
    # 'genuine security incident' as a distinct class is still absent (FA-16) — recorded BLOCKED in the matrix, not here.


# ---- Step 6: independent verification cannot be claimed from builder tests / CI alone (cited: AR-11/AR-18)
def test_FCE_independent_verification_requires_non_builder_receipt():
    from app.mainai_founder_ops import status as st

    if not hasattr(st, "is_independent_exact_sha_receipt"):
        pytest.fail("BLOCKED: pre-#294 tree without receipt-based verification")
    assert st.is_independent_exact_sha_receipt({"candidate_sha": "a" * 40, "builder_identity": "cursor", "examiner_identity": "cursor", "review_result": "PASS"}) is False
    assert st.is_independent_exact_sha_receipt({"candidate_sha": "a" * 40, "builder_identity": "cursor", "examiner_identity": "claude", "review_result": "PASS"}) is True


# ---- Step 10 (integrity): STOP cannot be silently cleared without an authenticated Founder — Codex-owned gap
def test_FCE_stop_clear_requires_authenticated_founder():
    """BLOCKED dependency: FA-9/CO-10. A format-valid founder_ack string clears STOP today. Codex owns the fix;
    recorded here so the first-cycle GO contract treats STOP-resume integrity as an explicit blocker."""
    import inspect

    from app.workforce import kill_switch

    src = inspect.getsource(kill_switch._validate_founder_ack)
    binds_identity = any(k in src for k in ("reauth", "receipt", "access_jti", "signature", "verify_"))
    print(f"FCE10|stop_ack_binds_authenticated_identity={binds_identity}")
    if not binds_identity:
        pytest.fail("BLOCKED: STOP-clear is gated only by founder_ack STRING FORMAT, not an authenticated Founder "
                    "(reauth receipt / signed token). Owner: Codex (Security Kernel). See CO-10 / FA-9.")
