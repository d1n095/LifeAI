"""Examiner-only: inherited Recall identities cannot certify this tree; Recall stays disabled."""
from app.mainai_founder_boot.readiness import build_readiness_matrix, component_manifest, personal_recall_production_identity
from app.mainai_verification_registry.service import record_verification_attestation
from tests.backend.personal_recall.test_production_activation import PERSONAL_RECALL_PRODUCTION_COMPONENT_ID

OLD = ("fb54de7b69e9a1e224ef615246a2422b60ef860e", "e03dc0b88d8d3c32291a801bdf2eaecc7193fb8a", "abfa8be482bd2a27c6d3feec9209e2df4309c840",
       "eda17056ca122e3d0142fd75cab16a8f94b45a5c", "0a6b0368d730efb603fa8f26a3d532b26bb78d53")


def _rec(db, sha):
    record_verification_attestation(db, component_id=PERSONAL_RECALL_PRODUCTION_COMPONENT_ID, candidate_sha=sha, builder_identity="codex",
                                    examiner_identity="claude", review_result="PASS", evidence_summary="examiner probe")
    db.commit()


def test_identity_gate(superuser_db):
    cur = personal_recall_production_identity()
    print("CURRENT_IDENTITY", cur)
    for o in OLD:
        if o != cur:
            _rec(superuser_db, o)
    m = build_readiness_matrix(covenant_ready=True, founder_ready=True, db_ready=True, db=superuser_db)["PERSONAL_RECALL"]
    print("WITH_OTHER_IDENTITY_PASSES", m["INDEPENDENTLY_VERIFIED"], m["ACTIVATED"], m["SAFE_FOR_FOUNDER_BOOT"])
    _rec(superuser_db, cur)
    m = build_readiness_matrix(covenant_ready=True, founder_ready=True, db_ready=True, db=superuser_db)["PERSONAL_RECALL"]
    print("WITH_CURRENT_PASS", m["INDEPENDENTLY_VERIFIED"], m["ACTIVATED"], m["SAFE_FOR_FOUNDER_BOOT"], "manifest_activated", component_manifest(superuser_db)["personal_recall"]["activated"])
