"""PI-STOP security boundary — executable acceptance + race tests (examiner-owned).

Subject: #309 aff5de1a composed candidate. Proves that a provider completion webhook
MUST NOT bypass a Founder STOP / revoked authority / expired grant.

Honesty rules enforced here:
- Tests that assert a property the product REALLY provides today use plain asserts (they
  PASS, anchoring that the STOP mechanism is genuine and that this suite drives it for real).
- Tests that encode the REQUIRED boundary that #309's ingestion path does NOT yet meet are
  marked `xfail(strict=True)`: they fail today (correct — the boundary is unmet) and, if the
  product is later fixed, the strict-xfail turns the suite RED (XPASS) so the finding must be
  promoted. No test simulates a STOP gate inside ingestion and then reports PASS.
- Tests that cannot run until Codex ships the authenticated STOP-clear / authority-state
  interface are `skip`-marked with an exact BLOCKED reason.

The real STOP mechanism exercised: app.workforce.kill_switch (DB-backed
workforce_authority_epoch; activate_kill_switch / assert_not_killed / query_stop_status).
The gap: app.agent_coordination.provider_ingestion.ingest_authenticated_provider_event does
not take a db session and never consults the kill switch, so an ingested terminal event
drives completion + examiner promotion with no authority check.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest

from _guards import egress  # noqa: F401  (network/paid-call guard)
from _harness import _privileges, owner_id  # noqa: F401  (real verified-user owner_id)

SECRET = "whsec_examiner_pistop"
UA = "Cursor-Agent-Webhook/1.0"


def _completed(r: dict) -> bool:
    """A terminal completion landed in the durable ledger (composed_v1 uses transition)."""
    return r.get("transition") == "completed"


def _sig(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _body(status: str, agent_id: str = "ext_stop", ts: str | None = None) -> bytes:
    # verify_v0_webhook reads the TOP-LEVEL "id" as the agent id and top-level "status".
    payload = {"event": "statusChange", "status": status, "id": agent_id}
    if ts is not None:
        payload["timestamp"] = ts
    return json.dumps(payload).encode()


def _svc_bound(owner: str, recipient: str = "cursor", agent_id: str = "ext_stop"):
    """A DurableCoordinationService with one open obligation under `owner`, with a provider
    binding — the exact state in which an inbound FINISHED webhook would complete it."""
    from app.agent_coordination.durable import DurableCoordinationService
    from app.agent_coordination.provider_ingestion import bind_provider_assignment
    svc = DurableCoordinationService()
    aid = "asg_" + _uuid.uuid4().hex[:8]
    svc.assign(agent_identity=recipient, workstream="build", owner_id=owner,
               instruction_id="i1", assignment_id=aid, actor="founder")
    bind_provider_assignment(svc, owner_id=owner, assignment_id=aid, provider="cursor",
                             external_agent_id=agent_id, recipient=recipient)
    return svc, aid


def _ingest(svc, owner: str, status: str = "FINISHED", agent_id: str = "ext_stop",
            ts: str | None = None, now: datetime | None = None):
    from app.agent_coordination.provider_ingestion import ingest_authenticated_provider_event
    if ts is None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    body = _body(status, agent_id=agent_id, ts=ts)
    return ingest_authenticated_provider_event(
        svc, owner_id=owner, secret=SECRET, raw_body=body, signature=_sig(body),
        user_agent=UA, fixture=True, now=now)


def _examiner_obligations(svc) -> int:
    """Count examiner obligations the ingestion promoted (composed_v1 CMP-6 method)."""
    return len([o for o in svc._obligations.values() if o.get("agent_identity") == "examiner"])


# ---------------------------------------------------------------------------
# A. POSITIVE CONTROLS — the real STOP mechanism exists and this suite drives it (PASS now)
# ---------------------------------------------------------------------------

def test_PIS_A1_kill_switch_is_real_and_db_backed(db_session, owner_id):
    """activate_kill_switch sets a DB-backed owner STOP that assert_not_killed enforces."""
    from app.workforce.kill_switch import activate_kill_switch, assert_not_killed, query_stop_status, KillSwitchError
    activate_kill_switch(db_session, owner_id=owner_id, reason="founder emergency stop")
    status = query_stop_status(db_session, owner_id=owner_id)
    assert status["blocked"] is True and status["owner"]["active"] is True, status
    with pytest.raises(KillSwitchError):
        assert_not_killed(db_session, owner_id)


def test_PIS_A2_kill_switch_is_owner_scoped(db_session, owner_id, make_verified_user):
    """A STOP on owner A must NOT stop a different owner B (no cross-owner DoS)."""
    from app.workforce.kill_switch import activate_kill_switch, assert_not_killed, KillSwitchError
    other, _ = make_verified_user()
    activate_kill_switch(db_session, owner_id=owner_id, reason="stop A only")
    with pytest.raises(KillSwitchError):
        assert_not_killed(db_session, owner_id)
    # B must be unaffected — this is a genuine PASS of the cross-owner isolation property.
    assert_not_killed(db_session, other.id)


def test_PIS_A3_clear_requires_founder_ack_format(db_session, owner_id):
    """STOP-clear at minimum rejects empty/boot-fabricated acks (the weak check CO-10 flags)."""
    from app.workforce.kill_switch import activate_kill_switch, clear_owner_stop, KillSwitchError
    activate_kill_switch(db_session, owner_id=owner_id, reason="stop")
    with pytest.raises(KillSwitchError):
        clear_owner_stop(db_session, owner_id=owner_id, founder_ack="")
    with pytest.raises(KillSwitchError):
        clear_owner_stop(db_session, owner_id=owner_id, founder_ack="boot_clear")


# ---------------------------------------------------------------------------
# B. EXECUTABLE-NOW findings — ingestion is NOT STOP-gated (required boundary unmet)
#    Each is xfail(strict): fails today (gap present); XPASS if fixed -> promote.
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="PI-STOP-1: ingestion not gated on Founder STOP before completion")
def test_PIS_B1_stop_before_ingestion_blocks_completion(db_session, owner_id):
    from app.workforce.kill_switch import activate_kill_switch, assert_not_killed, KillSwitchError
    owner = str(owner_id)
    svc, aid = _svc_bound(owner)
    activate_kill_switch(db_session, owner_id=owner_id, reason="founder stop before event")
    # positive control: STOP is genuinely active in THIS transaction
    with pytest.raises(KillSwitchError):
        assert_not_killed(db_session, owner_id)
    r = _ingest(svc, owner, status="FINISHED")
    # REQUIRED: a FINISHED webhook under an active STOP must NOT complete the obligation.
    assert not _completed(r), f"completed despite active STOP: {r}"


@pytest.mark.xfail(strict=True, reason="PI-STOP-1: no examiner promotion may occur under STOP")
def test_PIS_B2_stop_prevents_examiner_promotion(db_session, owner_id):
    from app.workforce.kill_switch import activate_kill_switch
    owner = str(owner_id)
    svc, aid = _svc_bound(owner)
    activate_kill_switch(db_session, owner_id=owner_id, reason="stop")
    _ingest(svc, owner, status="FINISHED")
    # REQUIRED: a stopped assignment must not spawn an independent-examiner obligation.
    assert _examiner_obligations(svc) == 0, "examiner promoted under STOP"


@pytest.mark.xfail(strict=True, reason="PI-STOP-2: STOP arriving between receipt and durable transition is not observed")
def test_PIS_B3_stop_after_receipt_before_transition(db_session, owner_id):
    """Verify-then-STOP-then-apply ordering: ingestion makes no authority re-check between
    authenticating the event and writing the terminal transition."""
    from app.agent_coordination.provider_ingestion import ingest_authenticated_provider_event
    from app.workforce.kill_switch import activate_kill_switch
    owner = str(owner_id)
    svc, aid = _svc_bound(owner)
    body = _body("FINISHED", ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    # STOP commits after the event is in flight but before the transition would be durable.
    activate_kill_switch(db_session, owner_id=owner_id, reason="stop mid-flight")
    r = ingest_authenticated_provider_event(svc, owner_id=owner, secret=SECRET, raw_body=body,
                                            signature=_sig(body), user_agent=UA, fixture=True)
    assert not _completed(r), f"completed despite STOP racing the transition: {r}"


@pytest.mark.xfail(strict=True, reason="PI-STOP-5: replayed completion after STOP must stay rejected on authority, not only dedup")
def test_PIS_B4_replay_after_stop(db_session, owner_id):
    """Dedup (replay_ignored) is real, but it is NOT an authority decision: the FIRST
    completion already landed before STOP. Required: the completion must not stand once STOP
    is active; a replay must be refused on authority grounds, not merely deduped."""
    from app.workforce.kill_switch import activate_kill_switch
    owner = str(owner_id)
    svc, aid = _svc_bound(owner)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    first = _ingest(svc, owner, status="FINISHED", ts=ts)
    assert _completed(first), f"expected first completion: {first}"
    activate_kill_switch(db_session, owner_id=owner_id, reason="stop after completion")
    replay = _ingest(svc, owner, status="FINISHED", ts=ts)
    # REQUIRED: after STOP, the standing completion is revoked AND the replay is authority-rejected.
    assert replay.get("outcome") == "authority_rejected", f"replay not authority-rejected under STOP: {replay}"


@pytest.mark.xfail(strict=True, reason="PI-STOP-6: STOP is durable (DB) but ingestion ignores it across a fresh service")
def test_PIS_B5_stop_survives_restart_but_ingestion_ignores_it(db_session, owner_id):
    """The kill switch is DB-durable, so a restart must not resurrect authority. A fresh
    service loading persisted ledger state and then ingesting FINISHED must still honor STOP."""
    from app.workforce.kill_switch import activate_kill_switch, assert_not_killed, KillSwitchError
    owner = str(owner_id)
    svc, aid = _svc_bound(owner)
    activate_kill_switch(db_session, owner_id=owner_id, reason="stop persists")
    # positive control: STOP is durable across the restart boundary (DB-backed)
    with pytest.raises(KillSwitchError):
        assert_not_killed(db_session, owner_id)
    fresh, _ = _svc_bound(owner)  # a fresh service instance (post-restart analogue)
    r = _ingest(fresh, owner, status="FINISHED")
    assert not _completed(r), f"fresh service completed despite durable STOP: {r}"


def test_PIS_B6_cross_owner_stop_does_not_leak_to_other_owner(db_session, owner_id, make_verified_user):
    """CROSS-OWNER (executable PASS): a STOP on owner A must not block owner B's legitimate
    ingestion. B has no STOP, so B completing is correct — this verifies the binding resolves
    the right owner and that A's stop does not become a cross-owner denial of service."""
    owner_b_user, _ = make_verified_user()
    owner_b = str(owner_b_user.id)
    from app.workforce.kill_switch import activate_kill_switch, assert_not_killed
    activate_kill_switch(db_session, owner_id=owner_id, reason="stop A only")  # A stopped
    assert_not_killed(db_session, owner_b_user.id)  # B genuinely not stopped
    svc_b, _ = _svc_bound(owner_b)
    r = _ingest(svc_b, owner_b, status="FINISHED")
    assert _completed(r), f"owner B wrongly blocked: {r}"


# ---------------------------------------------------------------------------
# C. BLOCKED on Codex authenticated STOP-clear / authority-state interface
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="BLOCKED (Codex CO-10): STOP-clear is gated only by a founder_ack STRING "
                         "(_validate_founder_ack format + small denylist), not an authenticated "
                         "authority interface. Cannot test that a clear was genuinely Founder-authorized "
                         "until Codex ships the authenticated STOP-clear interface.")
def test_PIS_C1_authenticated_stop_clear():  # pragma: no cover
    ...


@pytest.mark.skip(reason="BLOCKED (Codex): no queryable AUTHORITY-STATE interface with an UNKNOWN "
                         "value. assert_not_killed is binary (stopped/not) and has no 'authority "
                         "unknown / kernel unavailable -> fail closed' semantic for the ingestion "
                         "path to consult. Required: authority_state() in {GRANTED,STOPPED,UNKNOWN} "
                         "that ingestion treats UNKNOWN as refuse.")
def test_PIS_C2_authority_unknown_fails_closed():  # pragma: no cover
    ...


@pytest.mark.skip(reason="BLOCKED (Cursor+Codex): provider_ingestion does not take a db session, so a "
                         "STOP-rejected event cannot yet be persisted as durable audit evidence. "
                         "Required: a rejected-on-authority event is recorded (owner, agent, event_id, "
                         "reason=authority_rejected) for Founder audit. Testable once ingestion is wired "
                         "to the authority oracle AND persists rejections.")
def test_PIS_C3_stop_rejected_event_preserved_for_audit():  # pragma: no cover
    ...


# ---------------------------------------------------------------------------
# D. Focused regression for PI-DEDUP and RST-4 (NOT a re-run of the full #309 exam)
# ---------------------------------------------------------------------------

def test_PIS_D1_regression_PI_DEDUP_timestampless_duplicate(db_session, owner_id):
    """PI-DEDUP (P3): when the provider omits `timestamp`, event_id falls back to receive-time,
    so a duplicate terminal event can re-apply instead of being deduped. Confirmed, focused."""
    owner = str(owner_id)
    svc, aid = _svc_bound(owner)
    # no ts -> receive-time fallback in event_id
    r1 = _ingest(svc, owner, status="RUNNING", ts=None)
    r2 = _ingest(svc, owner, status="RUNNING", ts=None)
    # The defect: the second is NOT recognized as a replay (distinct receive-time event_id).
    assert r2.get("outcome") != "replay_ignored", (
        "unexpected: timestampless duplicate was deduped — PI-DEDUP may be fixed, re-verify")


def test_PIS_D2_regression_RST4_bare_secret_not_redacted(db_session, owner_id):
    """RST-4 (P3): redact_secrets is keyword-driven; a bare high-entropy secret value in free
    text with no nearby keyword is NOT redacted. Confirmed, focused."""
    from app.mainai_restrictions.service import redact_secrets
    bare = "sk-live-" + "A1b2C3d4E5" * 4  # looks like a live key, no 'token=/secret=' keyword
    out = redact_secrets({"note": f"the value is {bare} please use it"})
    assert bare in json.dumps(out), "unexpected: bare value redacted — RST-4 may be fixed, re-verify"
