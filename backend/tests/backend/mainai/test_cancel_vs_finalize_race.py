"""Real concurrent cancel vs success-finalize race on the same task/job rows.

When founder cancel is durable before success-finalize observes the job under lock,
task must become cancelled — never completed. If success-finalize commits first,
completed is a historical fact and later cancel_requested on the job does not rewrite it.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy.orm import sessionmaker

from app.mainai_execution.execution_job import _finalize_task_outcome
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from tests.backend.mainai.test_development_operator import _foundation


def _reset_running(db, task, job):
    task.status = MainAITaskStatus.running
    task.completed_at = None
    job.cancel_requested = False
    job.cancel_acknowledged = False
    job.status = MainAIJobStatus.running
    db.commit()


def test_cancel_committed_before_finalize_wins_under_lock(superuser_db, tmp_path):
    """Cancel commits first; finalize then runs — must cooperative-cancel, not complete."""
    owner, goal, task, job, _, _ = _foundation(superuser_db, tmp_path)
    task.mainai_job_id = job.id
    _reset_running(superuser_db, task, job)

    bind = superuser_db.get_bind()
    Session = sessionmaker(bind=bind)
    task_id, job_id = task.id, job.id
    start = threading.Barrier(2)
    cancel_done = threading.Barrier(2)
    errors: list[str] = []

    def _cancel():
        session = Session()
        try:
            start.wait(timeout=10)
            row = session.get(MainAIJob, job_id)
            row.cancel_requested = True
            session.commit()
            cancel_done.wait(timeout=10)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            errors.append(f"cancel:{exc}")
        finally:
            session.close()

    def _finalize():
        session = Session()
        try:
            start.wait(timeout=10)
            cancel_done.wait(timeout=10)
            row = session.get(MainAITask, task_id)
            _finalize_task_outcome(
                session,
                row,
                passed=True,
                evidence={"race": "cancel_first"},
                job_id=job_id,
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            errors.append(f"finalize:{exc}")
        finally:
            session.close()

    t1 = threading.Thread(target=_cancel)
    t2 = threading.Thread(target=_finalize)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    assert errors == [], errors
    superuser_db.expire_all()
    assert superuser_db.get(MainAITask, task_id).status == MainAITaskStatus.cancelled


def test_finalize_completed_before_cancel_remains_historical_completed(
    superuser_db, tmp_path
):
    """Success finalize commits first — completed stays; later cancel_requested is not rewrite."""
    _, _, task, job, _, _ = _foundation(superuser_db, tmp_path)
    task.mainai_job_id = job.id
    _reset_running(superuser_db, task, job)

    bind = superuser_db.get_bind()
    Session = sessionmaker(bind=bind)
    task_id, job_id = task.id, job.id
    start = threading.Barrier(2)
    finalize_done = threading.Barrier(2)
    errors: list[str] = []

    def _finalize():
        session = Session()
        try:
            start.wait(timeout=10)
            row = session.get(MainAITask, task_id)
            _finalize_task_outcome(
                session,
                row,
                passed=True,
                evidence={"race": "finalize_first"},
                job_id=job_id,
            )
            session.commit()
            finalize_done.wait(timeout=10)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            errors.append(f"finalize:{exc}")
        finally:
            session.close()

    def _cancel():
        session = Session()
        try:
            start.wait(timeout=10)
            finalize_done.wait(timeout=10)
            row = session.get(MainAIJob, job_id)
            # Job may still be running (finalize gate does not mark_completed the job).
            row.cancel_requested = True
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            errors.append(f"cancel:{exc}")
        finally:
            session.close()

    t1 = threading.Thread(target=_finalize)
    t2 = threading.Thread(target=_cancel)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    assert errors == [], errors
    superuser_db.expire_all()
    task_row = superuser_db.get(MainAITask, task_id)
    job_row = superuser_db.get(MainAIJob, job_id)
    assert task_row.status == MainAITaskStatus.completed
    assert job_row.cancel_requested is True
