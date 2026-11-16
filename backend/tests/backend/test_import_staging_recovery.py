from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from app.config import get_settings
from app.library.import_staging import cleanup_import_stages, recover_expired_import_uploads
from app.models.import_job import ImportJob, ImportJobStatus
from app.storage import get_storage


def _chunks(payload: bytes):
    sent = False

    def read():
        nonlocal sent
        if sent:
            return b""
        sent = True
        return payload

    return read


def _receiving_job(db, owner_id, *, expired=True):
    job_id = uuid.uuid4()
    staging_key = f"staging/{owner_id}/{job_id}"
    job = ImportJob(
        id=job_id,
        owner_id=owner_id,
        status=ImportJobStatus.receiving,
        source_filename="recovery.txt",
        staging_storage_key=staging_key,
        upload_lease_token=uuid.uuid4(),
        upload_lease_expires_at=datetime.utcnow() - timedelta(minutes=5) if expired else datetime.utcnow() + timedelta(minutes=5),
    )
    db.add(job)
    db.commit()
    return job


def test_expired_upload_intent_promotes_verified_stage_and_cleanup_is_idempotent(
    superuser_db, make_verified_user
):
    user, _ = make_verified_user()
    job = _receiving_job(superuser_db, user.id)
    staging_key = job.staging_storage_key
    payload = b"durable original survived a process restart"
    storage = get_storage()
    staged = storage.write_staged_stream(staging_key, _chunks(payload), max_bytes=1024)

    assert recover_expired_import_uploads(superuser_db) == 1
    superuser_db.expire_all()
    recovered = superuser_db.get(ImportJob, job.id)
    assert recovered.status == ImportJobStatus.pending
    assert recovered.source_checksum == hashlib.sha256(payload).hexdigest() == staged.sha256
    assert recovered.source_size_bytes == len(payload)
    assert recovered.source_storage_key is not None
    assert storage.verify(
        recovered.source_storage_key,
        expected_sha256=staged.sha256,
        expected_size=len(payload),
    )
    assert storage.inspect_staged(staging_key) is not None

    assert cleanup_import_stages(superuser_db) == 1
    superuser_db.expire_all()
    assert superuser_db.get(ImportJob, job.id).staging_storage_key is None
    assert storage.inspect_staged(staging_key) is None
    assert cleanup_import_stages(superuser_db) == 0


def test_corrupt_expired_stage_fails_closed_and_retains_the_original_pointer(
    superuser_db, make_verified_user
):
    user, _ = make_verified_user()
    job = _receiving_job(superuser_db, user.id)
    storage = get_storage()
    storage.write_staged_stream(job.staging_storage_key, _chunks(b"original"), max_bytes=1024)
    content_path = Path(get_settings().storage_root) / job.staging_storage_key / "content"
    content_path.write_bytes(b"corrupted")

    assert recover_expired_import_uploads(superuser_db) == 0
    superuser_db.expire_all()
    failed = superuser_db.get(ImportJob, job.id)
    assert failed.status == ImportJobStatus.failed
    assert failed.staging_storage_key == job.staging_storage_key
    assert failed.source_storage_key is None


def test_recovery_rejects_a_stage_key_bound_to_another_owner(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    other_owner, _ = make_verified_user()
    job = _receiving_job(superuser_db, owner.id)
    foreign_stage_key = f"staging/{other_owner.id}/{job.id}"
    job.staging_storage_key = foreign_stage_key
    superuser_db.commit()
    storage = get_storage()
    staged = storage.write_staged_stream(foreign_stage_key, _chunks(b"foreign owner's bytes"), max_bytes=1024)

    assert recover_expired_import_uploads(superuser_db) == 0
    superuser_db.expire_all()
    failed = superuser_db.get(ImportJob, job.id)
    assert failed.status == ImportJobStatus.failed
    assert failed.source_storage_key is None
    assert failed.staging_storage_key == foreign_stage_key
    assert storage.inspect_staged(foreign_stage_key) == staged


def test_cleanup_never_deletes_a_stage_key_bound_to_another_owner(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    other_owner, _ = make_verified_user()
    job = _receiving_job(superuser_db, owner.id)
    payload = b"canonical original is valid"
    storage = get_storage()
    canonical = storage.write_stream(_chunks(payload), max_bytes=1024)
    foreign_stage_key = f"staging/{other_owner.id}/{uuid.uuid4()}"
    staged = storage.write_staged_stream(foreign_stage_key, _chunks(b"foreign staged bytes"), max_bytes=1024)

    job.status = ImportJobStatus.pending
    job.staging_storage_key = foreign_stage_key
    job.source_storage_key = canonical.storage_key
    job.source_checksum = canonical.sha256
    job.source_size_bytes = canonical.size_bytes
    job.upload_lease_token = None
    job.upload_lease_expires_at = None
    superuser_db.commit()

    assert cleanup_import_stages(superuser_db) == 0
    superuser_db.expire_all()
    assert superuser_db.get(ImportJob, job.id).staging_storage_key == foreign_stage_key
    assert storage.inspect_staged(foreign_stage_key) == staged


def test_process_death_after_durable_stage_recovers_the_same_import_job(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    script = """
import os
import sys
import uuid
import io
from app.db import SessionLocal
from app.library.import_uploads import receive_import_upload
from app.request_context import current_user_id
from app.storage import get_storage

current_user_id.set(sys.argv[1])
db = SessionLocal()
storage = get_storage()
write_staged_stream = storage.write_staged_stream
payload = io.BytesIO(b"original survives process termination")
def write_then_exit(*args, **kwargs):
    staged = write_staged_stream(*args, **kwargs)
    if not staged.sha256:
        raise RuntimeError("staged hash missing")
    os._exit(73)
storage.write_staged_stream = write_then_exit
receive_import_upload(
    db,
    owner_id=uuid.UUID(sys.argv[1]),
    source_filename="process-crash.txt",
    media_type="text/plain",
    read_chunk=payload.read,
    max_bytes=1024,
)
raise RuntimeError("expected process termination after durable staging")
"""
    backend_dir = Path(__file__).resolve().parents[2]
    child = subprocess.run(
        [sys.executable, "-c", script, str(owner.id)],
        cwd=backend_dir,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 73, child.stderr

    superuser_db.expire_all()
    crashed = (
        superuser_db.query(ImportJob)
        .filter_by(owner_id=owner.id, status=ImportJobStatus.receiving)
        .one()
    )
    staged_key = crashed.staging_storage_key
    assert staged_key is not None
    assert get_storage().inspect_staged(staged_key) is not None

    crashed.upload_lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    superuser_db.commit()
    assert recover_expired_import_uploads(superuser_db) == 1
    superuser_db.expire_all()
    recovered = superuser_db.get(ImportJob, crashed.id)
    assert recovered.status == ImportJobStatus.pending
    assert get_storage().verify(
        recovered.source_storage_key,
        expected_sha256=hashlib.sha256(b"original survives process termination").hexdigest(),
        expected_size=len(b"original survives process termination"),
    )
    assert cleanup_import_stages(superuser_db) == 1
    assert get_storage().inspect_staged(staged_key) is None
