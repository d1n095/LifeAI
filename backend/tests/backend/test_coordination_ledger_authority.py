"""CC3/CC4 adversarial tests against separate PostgreSQL sessions."""

from __future__ import annotations

import hashlib
import threading
import uuid

from sqlalchemy import select, text

from app.agent_coordination.durable import DurableCoordinationService, load_owner_ledger
from app.agent_coordination.persist import persist_owner_ledger
from app.agent_coordination.provider_ingestion import _claim_event_receipt
from app.db import SessionLocal
from app.models.agent_coordination import CoordinationEventReceipt, CoordinationOwnerLedger
from app.request_context import current_user_id as current_user_id_var


def _runtime_session(owner_id: uuid.UUID):
    current_user_id_var.set(str(owner_id))
    session = SessionLocal()
    session.execute(text("SET LOCAL app.current_user_id = :owner"), {"owner": str(owner_id)})
    return session


def test_concurrent_whole_ledger_writers_preserve_both_updates(db_session, make_verified_user):
    owner, _ = make_verified_user()
    current_user_id_var.set(str(owner.id))
    db_session.execute(text("SET LOCAL app.current_user_id = :owner"), {"owner": str(owner.id)})
    persist_owner_ledger(db_session, owner_id=owner.id, service=DurableCoordinationService(), actor="test_setup")
    db_session.commit()

    barrier = threading.Barrier(2)
    failures: list[BaseException] = []
    assignment_ids: list[str] = []

    def writer(agent: str, instruction: str) -> None:
        session = _runtime_session(owner.id)
        try:
            service = load_owner_ledger(session, owner_id=owner.id)
            created = service.assign(
                agent_identity=agent,
                workstream="cc3-concurrent",
                owner_id=str(owner.id),
                instruction_id=instruction,
                actor="founder",
            )
            assignment_ids.append(str(created.assignment_id))
            barrier.wait(timeout=10)
            persist_owner_ledger(session, owner_id=owner.id, service=service, actor=f"writer:{agent}")
            session.commit()
        except BaseException as exc:  # noqa: BLE001 - surfaced in the parent test thread
            session.rollback()
            failures.append(exc)
        finally:
            session.close()

    threads = [
        threading.Thread(target=writer, args=("cursor", "writer-a")),
        threading.Thread(target=writer, args=("codex", "writer-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    check = _runtime_session(owner.id)
    try:
        restored = load_owner_ledger(check, owner_id=owner.id)
        assert set(assignment_ids).issubset(restored._obligations)
        authority = check.get(CoordinationOwnerLedger, owner.id)
        assert authority is not None
        assert authority.revision == 3
    finally:
        check.close()


def test_cross_process_event_claim_has_one_winner_and_survives_restart(db_session, make_verified_user):
    owner, _ = make_verified_user()
    db_session.commit()
    event_id = "cursor:agent-7:FINISHED:2026-10-31T00:00:00Z"
    digest = hashlib.sha256(b"same signed body").hexdigest()
    barrier = threading.Barrier(2)
    outcomes: list[bool] = []
    failures: list[BaseException] = []

    def claimant() -> None:
        session = _runtime_session(owner.id)
        try:
            barrier.wait(timeout=10)
            outcomes.append(
                _claim_event_receipt(
                    session,
                    owner_id=str(owner.id),
                    namespace="legacy_provider_webhook",
                    event_id=event_id,
                    payload_sha256=digest,
                )
            )
            session.commit()
        except BaseException as exc:  # noqa: BLE001
            session.rollback()
            failures.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=claimant), threading.Thread(target=claimant)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not failures
    assert sorted(outcomes) == [False, True]

    restarted = _runtime_session(owner.id)
    try:
        assert not _claim_event_receipt(
            restarted,
            owner_id=str(owner.id),
            namespace="legacy_provider_webhook",
            event_id=event_id,
            payload_sha256=digest,
        )
        restarted.commit()
    finally:
        restarted.close()


def test_event_receipts_are_owner_isolated_and_runtime_append_only(db_session, make_verified_user):
    owner, _ = make_verified_user()
    other, _ = make_verified_user()
    db_session.commit()
    event_id = "event-owner-isolation"
    digest = "a" * 64

    owner_session = _runtime_session(owner.id)
    assert _claim_event_receipt(
        owner_session,
        owner_id=str(owner.id),
        namespace="legacy_provider_webhook",
        event_id=event_id,
        payload_sha256=digest,
    )
    owner_session.commit()
    owner_session.close()

    other_session = _runtime_session(other.id)
    try:
        assert other_session.execute(select(CoordinationEventReceipt)).scalars().all() == []
    finally:
        other_session.close()

    owner_session = _runtime_session(owner.id)
    try:
        receipt = owner_session.get(
            CoordinationEventReceipt,
            {"owner_id": owner.id, "namespace": "legacy_provider_webhook", "event_id": event_id},
        )
        assert receipt is not None
        receipt.payload_sha256 = "b" * 64
        try:
            owner_session.flush()
        except Exception as exc:  # noqa: BLE001
            assert "append-only" in str(exc) or "permission denied" in str(exc)
            owner_session.rollback()
        else:  # pragma: no cover - a security regression must fail loudly
            raise AssertionError("runtime role mutated an append-only event receipt")
    finally:
        owner_session.close()
