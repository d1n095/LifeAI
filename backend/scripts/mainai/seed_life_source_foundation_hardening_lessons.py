"""Persists the durable engineering lessons found during the Life Source Foundation Bootstrap
hardening pass (PR #61, docs/LIFE_SOURCE_FOUNDATION_BOOTSTRAP.md's hardening mandate, Section
23) into `engineering_lessons` (app/mainai_execution/lessons.py, migration 0032) — the
machine-readable safety memory app/mainai_execution/planner.py's create_plan() already reads
from via apply_lessons_to_verification_plan(), so a future task whose `task_type` matches one
of these lessons' `applies_to` tags picks up the named regression test automatically instead of
this knowledge staying only as prose in a PR description or docs/BRANCH_REGISTRY.md.

Idempotent by `source_ref`: safe to re-run (e.g. after a later hardening pass adds more lessons
to this same file) without creating duplicates — each lesson is looked up by its exact
source_ref before inserting, and only missing ones are added.

Deliberately NOT run against production by this hardening pass (CLAUDE.md's standing "Ingen
production, Ingen prod migration/backfill" rule) — verified locally against the test database
only. Run manually, with DATABASE_URL pointed at the target database, when the founder is ready:

    DATABASE_URL=... python3 scripts/mainai/seed_life_source_foundation_hardening_lessons.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.mainai_execution import lessons  # noqa: E402
from app.models.mainai_execution import EngineeringLesson, EngineeringLessonConfidence, EngineeringLessonSeverity  # noqa: E402

_SOURCE_TYPE = "hardening_pass"
_SOURCE_REF_PREFIX = "PR #61 (life-source-foundation-bootstrap) hardening pass"

_LESSONS = [
    dict(
        problem=(
            "A column-level privilege REVOKE meant to enforce write-once immutability on "
            "documents.storage_key/file_path broke the legitimate multi-phase write lifecycle: "
            "app/rag/library_import.py creates a Document row with storage_key=NULL first (the "
            "content-addressed key can't be computed before the bytes are read), then UPDATEs "
            "it once the real key is known -- a genuine second write to a column privilege "
            "narrowing had revoked UPDATE on entirely."
        ),
        root_cause=(
            "Privilege design (GRANT/REVOKE) is binary -- can or cannot UPDATE a column -- it "
            "cannot express 'only if the column is currently NULL'. The narrowing was written "
            "against the INTENDED invariant (a storage_key must never change once set) without "
            "first tracing the actual write lifecycle the same column goes through in normal, "
            "correct operation."
        ),
        affected_component="documents.storage_key / documents.file_path immutability",
        severity=EngineeringLessonSeverity.critical,
        evidence=(
            "Caught by re-running the existing library import test suite after the privilege "
            "narrowing, not by a new test written for the narrowing itself -- the narrowing's "
            "own test coverage only exercised the narrowing in isolation (INSERT with a value "
            "already present, then attempt UPDATE) and never exercised it against the real "
            "two-phase INSERT-then-UPDATE call sequence library_import.py actually performs. "
            "Fixed by replacing column-privilege narrowing with a BEFORE UPDATE trigger "
            "(trg_documents_guard_storage_immutable) that allows exactly one legal transition "
            "(NULL -> value) and rejects any change to an already-set value -- expressing the "
            "real invariant directly instead of approximating it with a privilege that can't "
            "represent it."
        ),
        fix=(
            "Replaced the REVOKE UPDATE column-privilege approach with a BEFORE UPDATE trigger "
            "that inspects OLD/NEW per-column and raises only when a non-NULL value would "
            "change -- allows NULL to value once, rejects value to any-other-value always."
        ),
        general_rule=(
            "Before narrowing a privilege (GRANT/REVOKE) to enforce an invariant, trace the "
            "REAL write lifecycle every legitimate caller goes through for that column/table -- "
            "not just the attack you're defending against. A privilege is binary and cannot "
            "express conditional invariants ('only if NULL', 'only once'); those require a "
            "trigger that can inspect OLD/NEW, not a GRANT/REVOKE statement.",
        ),
        applies_to=["privilege_policy", "immutability", "storage", "source_foundation"],
        regression_test="tests/backend/rag/test_bootstrap_hardening.py::test_section2_g_both_storage_key_and_file_path_changed_in_one_statement_rejected",
        confidence=EngineeringLessonConfidence.certain,
    ),
    dict(
        problem=(
            "S1C added `message_source_units` as a second arm of `memory_source_units`' "
            "exclusive-arc pattern (document OR message), but the pre-existing "
            "`trg_msu_check_subtype_exists` deferred constraint trigger from migration 0019 "
            "only ever checked for a matching `document_source_units` row -- inserting a "
            "message-only memory_source_units row (no document_source_units row, correctly, "
            "since it's a message) was rejected by a trigger that had never been told about "
            "the new subtype."
        ),
        root_cause=(
            "Adding a new subtype to an existing exclusive-arc abstraction requires auditing "
            "every piece of logic written when the abstraction only had ONE arm for hardcoded "
            "single-subtype assumptions -- the trigger's own SQL literally named "
            "`document_source_units` and had no branch for anything else, because nothing else "
            "existed when it was written."
        ),
        affected_component="memory_source_units exclusive-arc subtype-existence trigger",
        severity=EngineeringLessonSeverity.high,
        evidence=(
            "Existing test coverage from the S1A phase never caught this because it only ever "
            "inserted document_source_units rows -- there was no test exercising the "
            "message-only path until S1C's own new tests were written, so the gap was invisible "
            "until the new subtype's happy path was tested at all. A dedicated mutation test "
            "(restore the OLD document-only trigger body, confirm the S1C regression test goes "
            "red, then restore the correct multi-arc body and confirm it goes green again) now "
            "proves this coverage is not vacuous."
        ),
        fix=(
            "Rewrote trg_msu_check_subtype_exists to check for EITHER a matching "
            "document_source_units OR message_source_units row, not just the former."
        ),
        general_rule=(
            "When adding a new subtype/arm to an existing exclusive-arc (or any 'exactly one "
            "of N related rows must exist') pattern, grep for every trigger/function/query that "
            "referenced the OLD single-subtype table by name -- a correct-looking addition of "
            "the new table alone is not sufficient; the enforcement logic itself usually needs "
            "editing too, and won't fail loudly at migration time since the old logic is still "
            "syntactically valid SQL, just semantically incomplete.",
        ),
        applies_to=["exclusive_arc", "trigger", "migration", "source_foundation"],
        regression_test="tests/backend/rag/test_bootstrap_hardening.py::test_section8_mutation_restoring_old_document_only_trigger_makes_s1c_regress",
        confidence=EngineeringLessonConfidence.certain,
    ),
    dict(
        problem=(
            "source_import_batch_failures.batch_id was a plain single-column FK REFERENCES "
            "source_import_batches(id), with no binding between the failure row's OWN owner_id "
            "and the owner_id of the batch it claims to belong to. RLS on "
            "source_import_batch_failures only ever checks the row's own owner_id column (its "
            "WITH CHECK), never anything about the row it references -- so an owner could "
            "insert a failure row naming their own owner_id (satisfying RLS) while pointing "
            "batch_id at a batch genuinely owned by someone else, entirely undetected by either "
            "table's own RLS policy in isolation."
        ),
        root_cause=(
            "A plain FK only proves the referenced row EXISTS, never that it belongs to the "
            "same owner as the referencing row. This project had already established the fix "
            "for exactly this shape of gap once before (migration 0027, mainai_jobs' child "
            "tables: UNIQUE(id, owner_id) on the parent + composite FOREIGN KEY (child_id, "
            "owner_id) REFERENCES parent(id, owner_id) on the child) -- but migration 0037 "
            "introduced a new parent/child pair (source_import_batches / "
            "source_import_batch_failures) without applying that same established pattern."
        ),
        affected_component="source_import_batch_failures.batch_id",
        severity=EngineeringLessonSeverity.high,
        evidence=(
            "Found by an explicit Section 13 (RLS owner isolation) hardening test that "
            "deliberately inserted a failure row with owner_id=owner_b but batch_id pointing at "
            "a batch genuinely owned by owner_a, expecting rejection -- the insert instead "
            "succeeded (pytest.raises DID NOT RAISE), the first sign the composite FK pattern "
            "was missing. No pre-existing test exercised this specific cross-owner batch_id "
            "case; the table's own RLS-isolation tests only ever verified a wrong owner's "
            "SELECT visibility, never an INSERT naming a foreign parent id."
        ),
        fix=(
            "Migration 0037 (edited in place, pre-merge, not a stacked follow-up migration): "
            "added UNIQUE(id, owner_id) to source_import_batches, changed "
            "source_import_batch_failures.batch_id to a composite "
            "FOREIGN KEY (batch_id, owner_id) REFERENCES source_import_batches(id, owner_id)."
        ),
        general_rule=(
            "Any child table with its own owner_id column AND a foreign key to a parent table "
            "that also has an owner_id column MUST use the composite-FK pattern "
            "(UNIQUE(id, owner_id) on the parent + FOREIGN KEY (child_id, owner_id) REFERENCES "
            "parent(id, owner_id) on the child), never a plain single-column FK to the parent's "
            "id alone -- RLS on the child table cannot see or validate anything about the "
            "parent row it references, only the child row's own columns. This is a checklist "
            "item for every new owner-scoped parent/child table pair, not a one-off fix.",
        ),
        applies_to=["rls", "foreign_key", "cross_owner", "migration", "source_foundation"],
        regression_test="tests/backend/rag/test_bootstrap_hardening.py::test_section13_insert_into_source_import_batch_failures_referencing_a_foreign_batch_id_is_rejected",
        confidence=EngineeringLessonConfidence.certain,
    ),
]


def seed(db) -> list[str]:
    """Inserts any of `_LESSONS` not already present (matched by source_ref). Returns the
    source_refs actually inserted this call (empty on a fully-idempotent re-run)."""
    inserted = []
    for i, lesson_kwargs in enumerate(_LESSONS, start=1):
        source_ref = f"{_SOURCE_REF_PREFIX}, lesson {i}"
        existing = db.query(EngineeringLesson).filter_by(source_ref=source_ref).first()
        if existing is not None:
            continue
        lessons.record_lesson(
            db,
            source_type=_SOURCE_TYPE,
            source_ref=source_ref,
            created_by="claude-hardening-pass",
            first_seen_at=datetime.now(timezone.utc).replace(tzinfo=None),
            **lesson_kwargs,
        )
        inserted.append(source_ref)
    db.commit()
    return inserted


def main() -> None:
    db = SessionLocal()
    try:
        inserted = seed(db)
    finally:
        db.close()
    if inserted:
        print(f"seed_life_source_foundation_hardening_lessons: inserted {len(inserted)} new lesson(s):")
        for ref in inserted:
            print(f"  - {ref}")
    else:
        print("seed_life_source_foundation_hardening_lessons: all lessons already present, nothing inserted.")


if __name__ == "__main__":
    main()
