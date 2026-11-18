"""Blueprint READINESS read-model — the cross-reference view the living blueprint needs.

Founder Alpha audit gap (Area 3): the versioned blueprint engine itself is upstream-only
(`blueprint_drafts`, migrations 0070-0072 on the founder-alpha composition lane — see
backend/docs/MIGRATION_RECONCILIATION.md). What is missing EVERYWHERE, including
upstream, is the join between knowledge artifacts and implementation reality: which
GitHub branches/PRs/commits, which tests, which verification status, and which next
actions actually connect to a given piece of knowledge. Agent C of the 2026-11-18 audit:
"MISSING: a durable blueprint/PR/commit cross-reference store … today links live
per-feature (project_memory PR snapshots, agent-task events, wait SHAs) with no unified
join to knowledge claims/documents."

This service is deliberately NOT a second blueprint engine and creates NO tables. It is
a read-only aggregation over the EXISTING evidence stores:
- documents (title/classification/active_truth_status) — the knowledge side,
- project_sources (git_commit / github_branch / github_pr snapshots) — the code side,
- project_branch_pr_statuses (is_current supersession) — live PR/branch state,
- engineering_lessons (affected_component, resolved_in, regression_test,
  verification_status) — implementation locations, tests, verification,
- agent_tasks (target_files, required_tests, status) — next actions.

When the upstream blueprint stack composes, `blueprint_drafts.source_document_ids`
joins directly to the document entries returned here — the read-model is the attachment
point, never a rival. Reads are founder-wide (same rationale as project_memory: a
founder-only system; the router endpoint is `require_founder`-gated). Everything
returned is EVIDENCE with its verification status attached — the service computes
nothing authoritative and promotes nothing (the non-promotion rule applies to this
surface like everywhere else: an unverified lesson is reported as unverified).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.agent_task import AgentTask
from app.models.document import Document
from app.models.mainai_execution import EngineeringLesson, EngineeringLessonStatus
from app.models.project_memory import ProjectBranchPRStatus, ProjectSource

_CODE_SOURCE_TYPES = ("git_commit", "github_branch", "github_pr")

# Closed vocabulary for the readiness verdict per component — derived ONLY from stored
# verification statuses, never guessed. `verified_by_regression_test` is the value migration
# 0063's ck_engineering_lessons_verification_status CHECK actually permits for verified work —
# omitting it here (the original set) made a DB-legal, regression-proven lesson present as
# "other", i.e. planned work could never be distinguished from verified code. Found by the
# Founder Acceptance Suite (scenario 6).
_VERIFIED = {"verified", "verified_by_ci", "checked", "verified_by_regression_test"}
_UNVERIFIED = {"unverified", "pending", "unknown", None}


def _lesson_verdict(verification_status: str | None) -> str:
    if verification_status in _VERIFIED:
        return "verified"
    if verification_status in _UNVERIFIED:
        return "unverified"
    return "other"


def collect_blueprint_readiness(db: Session, *, document_limit: int = 200) -> dict:
    """Aggregate existing evidence into the blueprint cross-reference view.

    Returns a plain dict (JSON-ready): documents, code_refs, pr_status, lessons,
    next_actions and a per-component index with a readiness verdict. Fail-closed
    semantics: missing rows yield empty sections, never fabricated links.
    """
    now = datetime.utcnow()

    documents = db.execute(
        select(
            Document.id,
            Document.title,
            Document.classification,
            Document.active_truth_status,
            Document.chunk_count,
            Document.created_at,
        )
        .where(Document.deleted_at.is_(None))
        .order_by(Document.created_at.desc())
        .limit(max(int(document_limit), 1))
    ).all()

    code_refs = db.execute(
        select(
            ProjectSource.source_type,
            ProjectSource.source_ref,
            ProjectSource.commit_sha,
            ProjectSource.ingested_by,
            ProjectSource.ingested_at,
        )
        .where(ProjectSource.source_type.in_(_CODE_SOURCE_TYPES))
        .order_by(ProjectSource.ingested_at.desc())
        .limit(100)
    ).all()

    pr_status = db.execute(
        select(
            ProjectBranchPRStatus.kind,
            ProjectBranchPRStatus.ref,
            ProjectBranchPRStatus.title,
            ProjectBranchPRStatus.status,
            ProjectBranchPRStatus.base_ref,
            ProjectBranchPRStatus.head_ref,
            ProjectBranchPRStatus.ci_status,
            ProjectBranchPRStatus.is_current,
            ProjectBranchPRStatus.recorded_at,
        )
        .where(ProjectBranchPRStatus.is_current.is_(True))
        .order_by(ProjectBranchPRStatus.recorded_at.desc())
        .limit(100)
    ).all()

    lessons = db.execute(
        select(
            EngineeringLesson.id,
            EngineeringLesson.affected_component,
            EngineeringLesson.verification_status,
            EngineeringLesson.resolved_in,
            EngineeringLesson.regression_test,
            EngineeringLesson.source_ref,
            EngineeringLesson.status,
        )
        .where(EngineeringLesson.status == EngineeringLessonStatus.active)
        .limit(200)
    ).all()

    tasks = db.execute(
        select(
            AgentTask.id,
            AgentTask.title,
            AgentTask.status,
            AgentTask.target_files,
            AgentTask.required_tests,
        )
        .limit(100)  # every AgentTaskStatus is an open work stage (created..ready_for_human)
    ).all()

    # Component index: implementation locations ↔ tests ↔ verification. This is the
    # connective tissue the blueprint needs; built only from stored rows.
    component_index: dict[str, dict] = {}
    for (_lid, component, verification_status, resolved_in, regression_test, source_ref, _status) in lessons:
        entry = component_index.setdefault(
            component,
            {"lessons": [], "regression_tests": [], "resolved_in": [], "verdict_counts": {"verified": 0, "unverified": 0, "other": 0}},
        )
        entry["lessons"].append({"source_ref": source_ref, "verification_status": verification_status})
        if regression_test:
            entry["regression_tests"].append(regression_test)
        if resolved_in:
            entry["resolved_in"].append(resolved_in)
        entry["verdict_counts"][_lesson_verdict(verification_status)] += 1

    doc_entries = [
        {
            "document_id": str(did),
            "title": title,
            "classification": classification,
            "truth_status": truth_status.value if hasattr(truth_status, "value") else truth_status,
            "chunk_count": chunk_count,
            "created_at": created_at.isoformat() if created_at else None,
        }
        for (did, title, classification, truth_status, chunk_count, created_at) in documents
    ]

    summary = {
        "generated_at": now.isoformat(),
        "documents": len(doc_entries),
        "documents_superseded_or_disputed": sum(
            1 for d in doc_entries if d["truth_status"] in ("superseded", "disputed")
        ),
        "code_refs": len(code_refs),
        "open_pr_or_branch_snapshots": len(pr_status),
        "active_lessons": len(lessons),
        "unverified_lessons": sum(1 for _lesson in lessons if _lesson_verdict(_lesson[2]) == "unverified"),
        "open_next_actions": len(tasks),
        "components_tracked": len(component_index),
    }

    return {
        "summary": summary,
        "documents": doc_entries,
        "code_refs": [
            {
                "source_type": st,
                "source_ref": ref,
                "commit_sha": commit_sha,
                "ingested_by": ingested_by,
                "ingested_at": ingested_at.isoformat() if ingested_at else None,
            }
            for (st, ref, commit_sha, ingested_by, ingested_at) in code_refs
        ],
        "pr_status": [
            {
                "kind": kind,
                "ref": ref,
                "title": title,
                "status": status,
                "base_ref": base_ref,
                "head_ref": head_ref,
                "ci_status": ci_status,
                "created_at": observed_at.isoformat() if observed_at else None,
            }
            for (kind, ref, title, status, base_ref, head_ref, ci_status, _cur, observed_at) in pr_status
        ],
        "lessons": [
            {
                "affected_component": component,
                "verification_status": verification_status,
                "resolved_in": resolved_in,
                "regression_test": regression_test,
                "source_ref": source_ref,
            }
            for (_lid, component, verification_status, resolved_in, regression_test, source_ref, _status) in lessons
        ],
        "next_actions": [
            {
                "task_id": str(tid),
                "title": title,
                "status": status.value if hasattr(status, "value") else status,
                "target_files": target_files,
                "required_tests": required_tests,
            }
            for (tid, title, status, target_files, required_tests) in tasks
        ],
        "component_index": component_index,
    }
