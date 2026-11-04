"""Independent examiner probes for PR #340 at db83ab9; not production implementation."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.blueprint.service import generate_blueprint_draft


def test_non_unique_integrity_error_is_not_relabelled_as_version_conflict(superuser_db):
    """A missing owner is an FK error, never a retryable version uniqueness race."""
    missing_owner = uuid.uuid4()
    with pytest.raises(IntegrityError):
        generate_blueprint_draft(
            superuser_db,
            owner_id=missing_owner,
            title="examiner-non-unique-integrity",
            source_hits=[
                {
                    "document_id": str(uuid.uuid4()),
                    "text": "examiner source",
                    "similarity": 1.0,
                }
            ],
            synthesizer=lambda _title, _query, _hits: "examiner body",
        )
