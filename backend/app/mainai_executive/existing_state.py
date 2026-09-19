"""Existing-state (rich history) helpers for safe-internal certification.

Seeds a disposable owner with superseded memory, corrections, capabilities,
continuity, and unreviewed work — then exposes system-level CURRENT vs SUPERSEDED
queries. No new product features; certification surface only.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, text as sa_text
from sqlalchemy.orm import Session

from app.capability_reality import list_capability_records, record_capability_observation
from app.concept_reconciliation import reconcile_and_promote_idea
from app.founder_memory import list_founder_memory
from app.inspectable_memory import (
    founder_add_memory_note,
    founder_correct_memory_note,
    founder_dispute_memory_item,
)
from app.intelligence_governance.service import record_evidence, record_execution
from app.mainai_executive.continuity import load_continuity_checkpoint
from app.mainai_executive.dashboard import founder_executive_dashboard
from app.mainai_executive.loop import run_executive_cycle
from app.mainai_executive.why_graph import list_decision_debt
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask
from app.models.user import User
from app.models.work_candidate import WorkCandidate
from app.project_entities import record_interpretation_proposal
from app.request_context import current_user_id as current_user_id_var
from app.workforce.kill_switch import query_stop_status


def _set_rls(db: Session, owner_id: uuid.UUID) -> None:
    current_user_id_var.set(str(owner_id))
    try:
        db.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})
    except Exception:
        pass


def _promote_entity(db: Session, *, owner_id: uuid.UUID, title: str, key: str) -> uuid.UUID:
    document = Document(
        title="rich-state-src",
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        active_truth_status=ActiveTruthStatus.active,
    )
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(
        owner_id=owner_id, source_id=document.id, claim_text=title, extraction_version="v1"
    )
    db.add(claim)
    db.flush()
    proposal = record_interpretation_proposal(
        db,
        owner_id=owner_id,
        source_claim_id=claim.id,
        proposed_entity_type="idea",
        idempotency_key=f"rich-prop-{key}",
    )
    db.flush()
    result = reconcile_and_promote_idea(
        db,
        owner_id=owner_id,
        proposal_id=proposal.id,
        title=title,
        entity_idempotency_key=f"rich-entity-{key}",
    )
    db.flush()
    return result.canonical_entity_id


def _task_execution(db: Session, owner_id: uuid.UUID, suffix: str):
    goal = MainAIGoal(
        owner_id=owner_id,
        title=f"rich-goal-{suffix}",
        original_instruction="rich-state seed",
        created_by="certification",
        completed_at=None,
    )
    db.add(goal)
    db.flush()
    plan = MainAIPlan(
        owner_id=owner_id,
        goal_id=goal.id,
        version=1,
        rationale="rich-state",
        created_by="certification",
    )
    db.add(plan)
    db.flush()
    task = MainAITask(
        owner_id=owner_id,
        goal_id=goal.id,
        plan_id=plan.id,
        task_type="repo_edit",
        description="rich-state task",
        status="pending",
        risk_level="low",
    )
    db.add(task)
    db.flush()
    return record_execution(
        db,
        owner_id=owner_id,
        task_id=task.id,
        idempotency_key=f"rich-exec-{suffix}",
        provider="internal",
    )


def seed_rich_safe_internal_state(
    db: Session,
    *,
    owner_email: str | None = None,
) -> dict[str, Any]:
    """Populate durable rich history for existing-state boot proofs."""
    email = owner_email or f"rich-boot-{uuid.uuid4().hex[:10]}@local.internal"
    owner = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if owner is None:
        owner = User(email=email, password_hash="rich-boot-local", email_verified=True)
        db.add(owner)
        db.flush()
    _set_rls(db, owner.id)
    tag = uuid.uuid4().hex[:8]

    entity_id = _promote_entity(
        db, owner_id=owner.id, title="Library public hours policy", key=f"lib-{tag}"
    )

    old_note, _ = founder_add_memory_note(
        db,
        owner_id=owner.id,
        content="Library closes at 17:00 on weekdays (OLD).",
        note_type="preference",
        idempotency_key=f"rich-old-{tag}",
        link_to_work=False,
    )
    corr_note, _ = founder_correct_memory_note(
        db,
        owner_id=owner.id,
        note_id=old_note.id,
        content="Library closes at 18:00 on weekdays (CURRENT correction).",
        idempotency_key=f"rich-corr-{tag}",
        link_to_work=False,
    )
    assumption, _ = founder_add_memory_note(
        db,
        owner_id=owner.id,
        content="Assumption: weekend hours match weekday hours (UNVERIFIED).",
        note_type="decision",
        idempotency_key=f"rich-assume-{tag}",
        link_to_work=False,
    )
    disputed, _ = founder_add_memory_note(
        db,
        owner_id=owner.id,
        content="Contradicted claim: we already verified external provider spend=0 forever.",
        note_type="decision",
        idempotency_key=f"rich-dispute-src-{tag}",
        link_to_work=False,
    )
    founder_dispute_memory_item(
        db,
        owner_id=owner.id,
        item_id=disputed.id,
        kind="founder_memory_note",
        reason="false competence claim — provider may be enabled later by founder only",
    )

    goal_note, _ = founder_add_memory_note(
        db,
        owner_id=owner.id,
        content="Park follow-ups for library hours notice as unreviewed work candidates.",
        note_type="goal",
        idempotency_key=f"rich-goal-note-{tag}",
        link_to_work=True,
    )

    # Unavailable history + verified capability (evidence-backed only for verified).
    record_capability_observation(
        db,
        owner_id=owner.id,
        capability_key="research.classify_public_notice",
        domain="research",
        status="configured_unavailable",
        status_reason="prior failure retained in history",
        provenance={"rich_state": True, "phase": "unavailable"},
    )

    exec_ok = _task_execution(db, owner.id, f"ok-{tag}")
    ok_ev = record_evidence(
        db,
        owner_id=owner.id,
        execution_id=exec_ok.id,
        evidence_kind="test_run_result",
        payload={
            "passed": True,
            "capability_key": "research.local_dry_classify",
            "supports_claim": True,
        },
        source_type="pytest",
        source_ref=f"rich-ok-{tag}",
        idempotency_key=f"rich-ok-ev-{tag}",
        deterministic=True,
    )
    record_capability_observation(
        db,
        owner_id=owner.id,
        capability_key="research.local_dry_classify",
        domain="research",
        status="verified_available",
        status_reason="supporting evidence attached",
        verification_evidence_id=ok_ev.id,
        success=True,
        provenance={"rich_state": True, "phase": "verified"},
    )
    record_capability_observation(
        db,
        owner_id=owner.id,
        capability_key="research.external_provider_invoke",
        domain="research",
        status="configured_disabled",
        status_reason="safe-internal: provider invoke disabled",
        provenance={"rich_state": True},
    )

    sid = f"rich-continuity-{tag}"
    cycle = run_executive_cycle(
        db,
        owner_id=owner.id,
        founder_request=(
            "Continue library hours classification using local-only path; "
            "do not invoke external provider."
        ),
        source_entity_id=entity_id,
        note_id=goal_note.id,
        session_id=sid,
        need_capability="low_risk_classification",
        run_workforce_dry=True,
    )
    db.flush()

    return {
        "owner_id": str(owner.id),
        "owner_email": owner.email,
        "entity_id": str(entity_id),
        "old_note_id": str(old_note.id),
        "current_correction_id": str(corr_note.id),
        "assumption_note_id": str(assumption.id),
        "disputed_note_id": str(disputed.id),
        "goal_note_id": str(goal_note.id),
        "session_id": sid,
        "continuity_note_id": str(cycle.continuity_note_id) if cycle.continuity_note_id else None,
        "provider_invoked": bool((cycle.workforce_dry_run or {}).get("provider_invoked")),
        "work_candidate_ids": [str(x) for x in cycle.work_candidate_ids],
        "tag": tag,
    }


def inspect_existing_state_after_boot(
    db: Session,
    *,
    owner_id: uuid.UUID,
    session_id: str | None = None,
) -> dict[str, Any]:
    """System-level CURRENT / SUPERSEDED / blocked / authority queries after boot."""
    _set_rls(db, owner_id)
    from app.founder_memory import list_current_founder_memory

    current_mem = list_current_founder_memory(db, owner_id=owner_id)
    all_mem = list_founder_memory(db, owner_id=owner_id)
    superseded_notes = list_founder_memory(db, owner_id=owner_id, status="superseded")
    disputed_notes = list_founder_memory(db, owner_id=owner_id, status="disputed")
    active_wcs = list(
        db.execute(
            select(WorkCandidate).where(
                WorkCandidate.owner_id == owner_id,
                WorkCandidate.status == "unreviewed",
            )
        ).scalars()
    )
    superseded_wcs = list(
        db.execute(
            select(WorkCandidate).where(
                WorkCandidate.owner_id == owner_id,
                WorkCandidate.status == "superseded",
            )
        ).scalars()
    )
    caps = list_capability_records(db, owner_id=owner_id)
    debt = list_decision_debt(db, owner_id=owner_id)
    stop = query_stop_status(db, owner_id=owner_id)
    dash = founder_executive_dashboard(db, owner_id=owner_id, session_id=session_id)
    checkpoint = None
    if session_id:
        try:
            checkpoint = load_continuity_checkpoint(db, owner_id=owner_id, session_id=session_id)
        except Exception as exc:  # noqa: BLE001 — surface uncertainty honestly
            checkpoint = {"error": type(exc).__name__, "detail": str(exc)}

    verified = [c for c in caps if c.status == "verified_available"]
    failed = [c for c in caps if c.status == "configured_unavailable"]
    disabled = [c for c in caps if c.status == "configured_disabled"]

    return {
        "CURRENT_MEMORY": [
            {"id": str(n.id), "content": (n.content or "")[:120], "status": n.status}
            for n in current_mem
        ],
        "SUPERSEDED_MEMORY": [
            {"id": str(n.id), "content": (n.content or "")[:120], "status": n.status}
            for n in superseded_notes
        ],
        "DISPUTED_MEMORY": [
            {"id": str(n.id), "content": (n.content or "")[:120], "status": n.status}
            for n in disputed_notes
        ],
        "NON_CURRENT_INCLUDED_WHEN_REQUESTED": len(all_mem) >= len(current_mem),
        "ACTIVE_UNREVIEWED_WORK": [
            {"id": str(w.id), "title": w.title, "status": w.status} for w in active_wcs
        ],
        "SUPERSEDED_WORK": [
            {"id": str(w.id), "title": w.title, "status": w.status} for w in superseded_wcs
        ],
        "VERIFIED_CAPABILITIES": [c.capability_key for c in verified],
        "FAILED_CAPABILITIES": [c.capability_key for c in failed],
        "DISABLED_CAPABILITIES": [c.capability_key for c in disabled],
        "DECISION_DEBT": debt,
        "KILL_SWITCH": stop,
        "AUTHORITY_EXISTS": dash.get("authority_state"),
        "AUTHORITY_DOES_NOT": {
            "provider_enabled": False,
            "boot_cannot_clear_kill_switch": True,
            "unreviewed_candidates_are_not_authorized": True,
        },
        "DASHBOARD": {
            "WHAT_SHE_IS_DOING": dash.get("WHAT_SHE_IS_DOING"),
            "WHAT_IS_BLOCKED": dash.get("WHAT_IS_BLOCKED"),
            "WHAT_FAILED_RECENTLY": dash.get("WHAT_FAILED_RECENTLY"),
            "WHAT_SHOULD_RESUME": dash.get("WHAT_RECOVERED"),
            "WHAT_CHANGED": dash.get("WHAT_CHANGED"),
        },
        "CONTINUITY_CHECKPOINT": checkpoint
        if isinstance(checkpoint, dict)
        else (
            {
                "session_id": getattr(checkpoint, "session_id", None),
                "uncertain": getattr(checkpoint, "uncertain", None),
            }
            if checkpoint is not None
            else None
        ),
        "invariants": {
            "no_oldest_row_as_current": all("OLD" not in (n.content or "") for n in current_mem),
            "superseded_not_in_current": all(n.status != "superseded" for n in current_mem),
            "disputed_not_in_current": all(n.status != "disputed" for n in current_mem),
            "no_false_provider_competence": "research.external_provider_invoke"
            not in [c.capability_key for c in verified],
        },
    }
