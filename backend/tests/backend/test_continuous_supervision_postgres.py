from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.mainai_execution.supervision_runtime import PostgresSupervisionStore
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.mainai_supervision import MainAISupervisionMessage


def test_postgres_supervision_is_idempotent_and_revalidates_canonical_job(superuser_db, make_verified_user):
    owner, _ = make_verified_user("supervision-owner@example.com")
    other, _ = make_verified_user("supervision-other@example.com")
    job = MainAIJob(owner_id=owner.id, job_type="supervision", status=MainAIJobStatus.queued, input_refs=[], output_refs=[], created_by="test")
    superuser_db.add(job)
    superuser_db.commit()

    store = PostgresSupervisionStore(superuser_db)
    first = store.continuation(owner_id=owner.id, job_id=job.id, kind="CONTINUE_SAME_JOB", reason="P0 remains", sequence=1)
    second = store.continuation(owner_id=owner.id, job_id=job.id, kind="CONTINUE_SAME_JOB", reason="different prose", sequence=1)
    assert first.id == second.id
    assert superuser_db.query(MainAISupervisionMessage).filter_by(owner_id=owner.id).count() == 1
    with pytest.raises(LookupError):
        store.continuation(owner_id=other.id, job_id=job.id, kind="CONTINUE_SAME_JOB", reason="cross owner", sequence=1)


def test_postgres_supervision_owner_rls_hides_other_owner_rows(superuser_db, db_session, make_verified_user):
    owner, _ = make_verified_user("rls-supervision-owner@example.com")
    other, _ = make_verified_user("rls-supervision-other@example.com")
    job = MainAIJob(owner_id=owner.id, job_type="supervision", status=MainAIJobStatus.queued, input_refs=[], output_refs=[], created_by="test")
    superuser_db.add(job)
    superuser_db.commit()
    PostgresSupervisionStore(superuser_db).continuation(owner_id=owner.id, job_id=job.id, kind="START_NEXT_READY_JOB", reason="ready", sequence=1)
    superuser_db.commit()

    db_session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(other.id)})
    assert db_session.query(MainAISupervisionMessage).filter_by(owner_id=owner.id).all() == []


def test_postgres_budget_expiry_is_bounded(superuser_db, make_verified_user):
    from app.models.mainai_supervision import MainAIBudgetReservation

    owner, _ = make_verified_user("budget-supervision@example.com")
    job = MainAIJob(owner_id=owner.id, job_type="supervision", status=MainAIJobStatus.queued, input_refs=[], output_refs=[], created_by="test")
    superuser_db.add(job)
    superuser_db.flush()
    now = datetime.now(timezone.utc)
    superuser_db.add(MainAIBudgetReservation(owner_id=owner.id, job_id=job.id, amount=2, state="reserved", created_at=now, expires_at=now - timedelta(seconds=1)))
    superuser_db.commit()
    assert PostgresSupervisionStore(superuser_db).expire_reservations(now=now) == 1


def test_postgres_delivery_is_retry_safe_and_dead_letters(superuser_db, make_verified_user):
    owner, _ = make_verified_user("delivery-supervision@example.com")
    job = MainAIJob(owner_id=owner.id, job_type="supervision", status=MainAIJobStatus.queued, input_refs=[], output_refs=[], created_by="test")
    superuser_db.add(job)
    superuser_db.commit()
    store = PostgresSupervisionStore(superuser_db)
    store.continuation(owner_id=owner.id, job_id=job.id, kind="CONTINUE_SAME_JOB", reason="retry", sequence=1)
    assert store.deliver(lambda _: (_ for _ in ()).throw(RuntimeError("poison")), owner_id=owner.id, max_attempts=1) == (0, 1, 0)


def test_postgres_idle_supervision_uses_canonical_job_state(superuser_db, make_verified_user):
    owner, _ = make_verified_user("idle-supervision@example.com")
    job = MainAIJob(owner_id=owner.id, job_type="supervision", status=MainAIJobStatus.queued, input_refs=[], output_refs=[], created_by="test")
    superuser_db.add(job)
    superuser_db.commit()
    store = PostgresSupervisionStore(superuser_db)
    store.observe(agent_id="agent-canonical", owner_id=owner.id, state="IDLE", process_nonce="n1", heartbeat_at=datetime.now(timezone.utc))
    messages = store.supervise_idle(owner_id=owner.id, agent_id="agent-canonical")
    assert len(messages) == 1 and messages[0].job_id == job.id
