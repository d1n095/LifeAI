"""Founder Alpha: the project memory the founder's Life actually reads from.

This module is the composition; it never writes. Read-only by construction: every function here
issues SELECTs (through the same owner-scoped sessions and RLS identity as the routers) and
returns plain dicts. No goal, task, proposal, candidate or decision is ever created here —
"propose the next step" is a list of *suggestions with their evidence*, and the founder (or an
existing authorized endpoint) decides.

R13 built the picture (blueprint, four explicit buckets, goals, capabilities, GitHub evidence,
durable memory). R14 made GitHub observable and the locked priority durable. R15 adds `work`:
the LIVE chain — goals, tasks awaiting the exact approval the dispatch gate enforces, jobs
including abandoned ones, agent assignments and work candidates (app/project_status/work_chain.py).
"""

from __future__ import annotations

import uuid
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.capability_reality.service import list_capability_records
from app.founder_memory.service import list_current_founder_memory
from app.github_evidence.service import list_github_evidence_candidates
from app.github_status.service import dev_status, summary_for_prompt
from app.project_status.work_chain import build_work_chain
from app.workforce.org_view import organization_snapshot
from app.mainai_execution.improvement_proposals import list_proposals
from app.models.blueprint_draft import BlueprintDraft
from app.models.improvement_proposal import ImprovementStatus
from app.models.mainai_execution import MainAIGoal, MainAIGoalStatus
from app.models.problem_learning import LifeProblemDecision

SCHEMA_VERSION = "1.1"

# Which goal/capability states the founder's four buckets are built from. Deliberately explicit
# instead of inferred: a bucket that silently absorbed an unknown state would be a lie.
_IMPLEMENTED_GOAL_STATES = {MainAIGoalStatus.completed}
_PLANNED_GOAL_STATES = {MainAIGoalStatus.pending, MainAIGoalStatus.planning, MainAIGoalStatus.running}
_BLOCKED_GOAL_STATES = {MainAIGoalStatus.blocked, MainAIGoalStatus.failed}

_MAX_EXAMPLES = 5
_MAX_NEXT_STEPS = 5


def _iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def _blueprint(db: Session, owner_id: uuid.UUID) -> dict[str, Any]:
    approved = (
        db.execute(
            select(BlueprintDraft)
            .where(BlueprintDraft.owner_id == owner_id, BlueprintDraft.status == "approved")
            .order_by(BlueprintDraft.version.desc())
        )
        .scalars()
        .first()
    )
    latest = (
        db.execute(
            select(BlueprintDraft)
            .where(BlueprintDraft.owner_id == owner_id)
            .order_by(BlueprintDraft.created_at.desc())
        )
        .scalars()
        .first()
    )
    return {
        "approved": (
            {
                "id": str(approved.id),
                "title": approved.title,
                "version": approved.version,
                "source_count": len(approved.source_document_ids or []),
            }
            if approved is not None
            else None
        ),
        "latest_draft": (
            {
                "id": str(latest.id),
                "title": latest.title,
                "version": latest.version,
                "status": latest.status,
            }
            if latest is not None and (approved is None or latest.id != approved.id)
            else None
        ),
    }


def _goals(db: Session, owner_id: uuid.UUID) -> tuple[dict[str, int], list[MainAIGoal]]:
    rows = db.execute(
        select(MainAIGoal.status, func.count())
        .where(MainAIGoal.owner_id == owner_id)
        .group_by(MainAIGoal.status)
    ).all()
    counts = {str(status.value if hasattr(status, "value") else status): int(n) for status, n in rows}
    recent = (
        db.execute(
            select(MainAIGoal)
            .where(MainAIGoal.owner_id == owner_id)
            .order_by(MainAIGoal.created_at.desc())
            .limit(_MAX_EXAMPLES)
        )
        .scalars()
        .all()
    )
    return counts, list(recent)


def _status_classes(
    goal_counts: dict[str, int],
    recent_goals: list[MainAIGoal],
    capability_records: list[Any],
) -> dict[str, dict[str, Any]]:
    """The founder's four buckets, each backed by explicit states rather than a guess."""
    verified = [c for c in capability_records if c.status == "verified_available"]
    planned_caps = [c for c in capability_records if c.status == "planned"]
    gap_caps = [c for c in capability_records if c.status in ("configured_unavailable", "unknown")]

    implemented = sum(goal_counts.get(s.value, 0) for s in _IMPLEMENTED_GOAL_STATES)
    planned = sum(goal_counts.get(s.value, 0) for s in _PLANNED_GOAL_STATES) + len(planned_caps)
    blocked = sum(goal_counts.get(s.value, 0) for s in _BLOCKED_GOAL_STATES) + len(gap_caps)

    return {
        "implemented": {
            "count": implemented,
            "evidence": "avslutade MainAI-mål (mainai_goals.status=completed)",
            "examples": [g.title for g in recent_goals if g.status in _IMPLEMENTED_GOAL_STATES][:_MAX_EXAMPLES],
        },
        "verified": {
            "count": len(verified),
            "evidence": "capability_records.status=verified_available (explicit verifiering, aldrig antagen)",
            "examples": [c.capability_key for c in verified][:_MAX_EXAMPLES],
        },
        "planned": {
            "count": planned,
            "evidence": "mål som väntar/planeras/kör + capability_records.status=planned",
            "examples": [g.title for g in recent_goals if g.status in _PLANNED_GOAL_STATES][:_MAX_EXAMPLES],
        },
        "blocked": {
            "count": blocked,
            "evidence": "mål i blocked/failed + kapaciteter som är configured_unavailable/unknown",
            "examples": [g.title for g in recent_goals if g.status in _BLOCKED_GOAL_STATES][:_MAX_EXAMPLES]
            + [c.capability_key for c in gap_caps][:_MAX_EXAMPLES],
        },
    }


def _github(db: Session, owner_id: uuid.UUID) -> dict[str, Any]:
    candidates = list_github_evidence_candidates(db, owner_id=owner_id)
    unreviewed = [c for c in candidates if c.status == "unreviewed"]
    dev = dev_status(db, owner_id=owner_id)
    remote = dev["remote"] or {}
    local = dev["local"]
    return {
        # One source of truth for "is GitHub configured": dev_status's own reading, so the
        # project status and the github-status surface can never disagree about it.
        "configured": dev["configured"],
        "repo": dev["repo"],
        # What was actually VERIFIED against GitHub, and how old that is. `remote` stays None
        # until a refresh has succeeded — never a guessed head/PR count.
        "development": {
            "last_verified_at": dev["last_verified_at"],
            "age_minutes": dev["age_minutes"],
            "stale": dev["stale"],
            "head_sha": remote.get("head_sha"),
            "head_subject": (remote.get("head_commit") or {}).get("subject"),
            "open_prs": len(remote.get("open_prs") or []) if remote else None,
            "open_issues": remote.get("open_issue_count"),
            "checks": remote.get("checks"),
            "local_unpushed_count": local.get("unpushed_count") if local.get("available") else None,
            "local_unavailable_reason": None if local.get("available") else local.get("reason"),
            "local_matches_remote_head": dev["local_matches_remote_head"],
            "error": dev["error"],
        },
        "accepted_evidence": sum(1 for c in candidates if c.status == "authorized"),
        "unreviewed": [
            {
                "id": str(c.id),
                "kind": c.source_kind,
                "number": c.source_number,
                "title": c.title,
                "url": c.source_url,
                "signals": list(c.signals or []),
            }
            for c in unreviewed[:_MAX_EXAMPLES]
        ],
        "unreviewed_count": len(unreviewed),
    }


def _memory(db: Session, owner_id: uuid.UUID) -> list[dict[str, Any]]:
    """Durable project memory (the founder's own notes) — the SAME store the memory API uses."""
    notes = list_current_founder_memory(db, owner_id=owner_id)
    return [
        {
            "type": n.note_type,
            "content": (n.content or "")[:280],
            "authority": n.authority,
            "created_at": _iso(n.created_at),
        }
        for n in notes[: _MAX_EXAMPLES * 2]
    ]


def _priorities(memory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The locked decisions that must govern planning, straight from the founder's own record."""
    return [n for n in memory if n["type"] in ("priority", "decision") and n["authority"] == "founder"]


def _agents(db: Session, owner_id: uuid.UUID) -> dict[str, Any]:
    """Who is working — from the workforce org view that already owns this fact."""
    try:
        snap = organization_snapshot(db, owner_id=owner_id)
    except Exception:
        return {"available": False, "reason": "workforce-vyn kunde inte läsas", "agents": 0, "open_assignments": 0}
    return {
        "available": True,
        "agents": len(snap.get("agents") or []),
        "teams": len(snap.get("teams") or []),
        "open_assignments": len(snap.get("current_assignments") or []),
        "names": [a.get("name") or a.get("agent_key") for a in (snap.get("agents") or [])][:_MAX_EXAMPLES],
    }


def _warnings(status: dict[str, Any]) -> list[str]:
    """Exactly where the picture is missing or old — so "unknown" is visible, never implied."""
    warnings: list[str] = []
    if status["blueprint"]["approved"] is None:
        warnings.append("Ingen godkänd blueprint finns ännu — projektets målbild är inte fastställd.")
    gh = status["github"]
    if not gh["configured"]:
        warnings.append("GitHub är inte konfigurerad (GITHUB_TOKEN/GITHUB_REPO) — utvecklingsstatus kan inte verifieras.")
    elif gh["development"]["last_verified_at"] is None:
        warnings.append("GitHub är konfigurerad men aldrig verifierad — kör en uppdatering.")
    elif gh["development"]["stale"]:
        warnings.append(
            f"GitHub-statusen är {gh['development']['age_minutes']} minuter gammal och kan vara inaktuell."
        )
    if gh["configured"] and gh["development"]["local_unavailable_reason"]:
        warnings.append(
            "Lokala commits kan inte jämföras med GitHub: "
            + str(gh["development"]["local_unavailable_reason"])
        )
    if not status["goals"]["counts"]:
        warnings.append("Inga MainAI-mål är registrerade — ingen utvecklingshistorik finns ännu.")
    if not status["memory"]:
        warnings.append("Projektminnet är tomt — inga grundarbeslut är registrerade.")
    # R15: the live work chain's own honesty lines (waiting approvals, blocked tasks,
    # abandoned jobs, stalled agent assignments) — composed by the module that reads them.
    warnings.extend(status.get("work", {}).get("warnings") or [])
    return warnings


def _decisions(db: Session, owner_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        db.execute(
            select(LifeProblemDecision)
            .where(LifeProblemDecision.owner_id == owner_id, LifeProblemDecision.status == "active")
            .order_by(LifeProblemDecision.decided_at.desc())
            .limit(_MAX_EXAMPLES)
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(d.id),
            "decision": d.decision,
            "authority": d.authority,
            "decided_at": _iso(d.decided_at),
        }
        for d in rows
    ]


def _next_steps(
    *,
    github: dict[str, Any],
    proposals: list[Any],
    goal_counts: dict[str, int],
    recent_goals: list[MainAIGoal],
    capability_records: list[Any],
    work: dict[str, Any],
) -> list[dict[str, Any]]:
    """Suggested next steps with their evidence. NEVER authorized, NEVER created.

    Each entry carries `authorized: False` structurally so no caller can mistake this list for
    a queue of dispatched work — the existing founder-only decision endpoints remain the only
    way anything becomes a task.
    """
    steps: list[dict[str, Any]] = []

    for goal in recent_goals:
        if goal.status in _BLOCKED_GOAL_STATES:
            steps.append(
                {
                    "kind": "unblock_goal",
                    "title": f"Avblockera målet: {goal.title}",
                    "why": f"målstatus={goal.status.value} kräver grundarens beslut",
                    "source": f"mainai_goals:{goal.id}",
                }
            )

    # R15: work that is physically one decision away from running. Each step points at the
    # row it came from and stays `authorized: False` like every other suggestion here.
    for task in work["tasks"]["awaiting_approval"]:
        steps.append(
            {
                "kind": "approve_task",
                "title": f"Godkänn uppgift: {task['description'][:100]}",
                "why": f"godkännandepolicyn kräver grundarens beslut innan jobbet får starta (mål: {task['goal_title']})",
                "source": task["source"],
            }
        )

    for job in work["jobs"]["stalled"]:
        steps.append(
            {
                "kind": "inspect_stalled_job",
                "title": f"Undersök stillastående jobb ({job['job_type']})",
                "why": f"{job['reason']}; ålder {job['age_minutes']} min",
                "source": job["source"],
            }
        )

    for assignment in work["agents"]["stale"]:
        steps.append(
            {
                "kind": "inspect_stale_assignment",
                "title": f"Följ upp agentuppdrag ({assignment['agent'] or 'okänd agent'}, {assignment['role']})",
                "why": f"uppdraget står stilla i status {assignment['status']} sedan {assignment['idle_minutes']} min",
                "source": assignment["source"],
            }
        )

    for duplicate in work["suspected_duplicates"]:
        steps.append(
            {
                "kind": "review_possible_duplicate",
                "title": f"Möjlig dubblett: {duplicate['title']}",
                "why": (
                    f"liknar redan avslutat mål '{duplicate['matches_completed_goal']['title']}' "
                    f"(likhet {duplicate['similarity']})"
                ),
                "source": f"mainai_goals:{duplicate['goal_id']}",
            }
        )

    for candidate in github["unreviewed"]:
        steps.append(
            {
                "kind": "review_github_evidence",
                "title": f"Granska GitHub-evidens #{candidate['number']}: {candidate['title']}",
                "why": "staged evidens är aldrig auktoriserad i sig"
                + (f" (signaler: {', '.join(candidate['signals'])})" if candidate["signals"] else ""),
                "source": f"github_evidence_candidates:{candidate['id']}",
            }
        )

    for proposal in proposals:
        steps.append(
            {
                "kind": "decide_proposal",
                "title": f"Ta ställning till förslag: {proposal.title}",
                "why": "förslag exekverar aldrig sig själva",
                "source": f"mainai_improvement_proposals:{proposal.id}",
            }
        )

    for record in capability_records:
        if record.status in ("configured_unavailable", "unknown"):
            steps.append(
                {
                    "kind": "verify_capability",
                    "title": f"Verifiera kapacitet: {record.capability_key}",
                    "why": record.status_reason or f"status={record.status}",
                    "source": f"capability_records:{record.capability_key}",
                }
            )

    if not steps and goal_counts:
        steps.append(
            {
                "kind": "no_action",
                "title": "Inget väntar på grundaren",
                "why": "inga blockerade mål, granskningar eller förslag ligger öppna",
                "source": "project_status",
            }
        )

    for step in steps[:_MAX_NEXT_STEPS]:
        step["authorized"] = False
    return steps[:_MAX_NEXT_STEPS]


def build_project_status(db: Session, *, owner_id: uuid.UUID) -> dict[str, Any]:
    """The founder's project memory as it actually stands right now (read-only)."""
    blueprint = _blueprint(db, owner_id)
    goal_counts, recent_goals = _goals(db, owner_id)
    capabilities = list_capability_records(db, owner_id=owner_id)
    github = _github(db, owner_id)
    proposals = list_proposals(db, owner_id=owner_id, status=ImprovementStatus.proposed)
    memory = _memory(db, owner_id)
    work = build_work_chain(db, owner_id=owner_id)

    status = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "blueprint": blueprint,
        "status_classes": _status_classes(goal_counts, recent_goals, capabilities),
        "goals": {
            "counts": goal_counts,
            "recent": [
                {"id": str(g.id), "title": g.title, "status": g.status.value, "created_at": _iso(g.created_at)}
                for g in recent_goals
            ],
        },
        "capabilities": [
            {
                "key": c.capability_key,
                "domain": c.domain,
                "status": c.status,
                "verified_at": _iso(c.last_verified_at),
            }
            for c in capabilities[:_MAX_EXAMPLES * 2]
        ],
        "github": github,
        "memory": memory,
        "priorities": _priorities(memory),
        "agents": _agents(db, owner_id),
        "ideas": [
            {"id": str(p.id), "title": p.title, "status": p.status.value, "created_at": _iso(p.created_at)}
            for p in proposals[:_MAX_EXAMPLES]
        ],
        "decisions": _decisions(db, owner_id),
        "work": work,
    }
    status["next_steps"] = _next_steps(
        github=github,
        proposals=proposals,
        goal_counts=goal_counts,
        recent_goals=recent_goals,
        capability_records=capabilities,
        work=work,
    )
    status["warnings"] = _warnings(status)
    status["authorization"] = {
        "created_goals": 0,
        "created_tasks": 0,
        "note": "next_steps är förslag med evidens; inget skapas eller skickas härifrån",
    }
    return status


def project_status_relevant(question: str) -> bool:
    """Select read-only context, never execution authority. Callers include recent turns
    so short follow-ups retain project context; absent facts never authorize status claims.
    """
    text = question.casefold()
    return bool(re.search(
        r"\b(projekt\w*|project\w*|status|blueprint|github|git|repo\w*|branch\w*|"
        r"commit\w*|pull request|pr|ci|kod\w*|code|coding|utveckl\w*|develop\w*|"
        r"deploy\w*|release\w*|integrat\w*|test\w*|debug\w*|bug\w*|fix\w*|"
        r"plan\w*|task\w*|goal\w*|mål|arbet\w*|work\w*|agent\w*|"
        r"nästa|next|fortsätt|continue|block\w*|lifeai|l4tp|founder|alpha|r15|capabilit\w*|förmå\w*)\b", text
    )) or any(phrase in text for phrase in ("var är vi", "where are we", "vad händer", "what is happening", "hur går det", "kan du", "what can you"))


def format_project_status_instructions(db: Session, *, owner_id: uuid.UUID, question: str | None = None) -> str:
    """Compact prompt block so the founder's question "var är vi?" is answered from facts.

    Fail-open at the call site (see app/routers/chat.py): if this raises, the reply proceeds
    without it and never invents a status.
    """
    if question is not None and not project_status_relevant(question):
        return "PROJEKTSTATUS: inte hämtad för denna fråga. Gör inga påståenden om projektets aktuella status utan registerfakta."
    status = build_project_status(db, owner_id=owner_id)
    blueprint = status["blueprint"]["approved"]
    classes = status["status_classes"]
    lines = [
        "PROJEKTSTATUS (verkliga fakta från LifeAI:s register — använd dessa i stället för att gissa):",
        "- Blueprint: "
        + (f"'{blueprint['title']}' v{blueprint['version']} (grundaren har godkänt den)" if blueprint else "ingen godkänd blueprint ännu"),
        f"- Implementerat: {classes['implemented']['count']} | Verifierat: {classes['verified']['count']} "
        f"| Planerat: {classes['planned']['count']} | Blockerat: {classes['blocked']['count']}",
        f"- GitHub-integration: {'konfigurerad (' + str(status['github']['repo']) + ')' if status['github']['configured'] else 'INTE konfigurerad (ingen behörighet satt)'}"
        f"; granskning väntar: {status['github']['unreviewed_count']}",
        f"- Idéer/förslag som väntar på beslut: {len(status['ideas'])}",
        "- GitHub-verifiering: " + summary_for_prompt(dev_status(db, owner_id=owner_id)),
        f"- Arbetskraft: {status['agents']['agents']} agenter, {status['agents']['open_assignments']} öppna uppdrag",
    ]
    work = status["work"]
    lines.append(
        "- Arbetskedja just nu: "
        f"{work['goals']['active_count']} aktiva mål "
        f"({work['goals']['completed_count']} avslutade), "
        f"{work['tasks']['awaiting_approval_count']} uppgift(er) väntar på godkännande, "
        f"{work['tasks']['counts'].get('running', 0)} körs, "
        f"{work['jobs']['active']} aktiva jobb varav {work['jobs']['stalled_count']} stillastående, "
        f"{work['agents']['open_assignments']} öppna agentuppdrag "
        f"({len(work['agents']['blocked'])} blockerade)"
    )
    # R15 (2026-11-19): without the concrete goals, "vad hände med målet jag just godkände?"
    # got "det finns inget i projektregistret" even though the same block counted it as
    # completed -- the model could not see WHICH goals existed. Titles + real status only.
    recent_goals = (status.get("goals") or {}).get("recent") or []
    if recent_goals:
        lines.append("- Senaste mål (verklig registrerad status):")
        lines += [f"  - \"{g['title']}\" → {g['status']}" for g in recent_goals[:5]]
    if status["priorities"]:
        lines.append("- Låsta prioriteringar (grundarens beslut):")
        lines += [f"  - {p['content']}" for p in status["priorities"][:3]]
    if status["warnings"]:
        lines.append("- OSÄKERHET/DATAKVALITET (säg det hellre än att gissa):")
        lines += [f"  - {w}" for w in status["warnings"]]
    if status["next_steps"]:
        lines.append("- Möjliga nästa steg (FÖRSLAG, inte auktoriserade — skapa aldrig uppdrag själv):")
        lines += [f"  {i}. {s['title']} — {s['why']}" for i, s in enumerate(status["next_steps"], 1)]
    else:
        lines.append("- Inget väntar på grundaren just nu.")
    lines.append(
        "Skilj alltid på: implementerat (avslutat och spårat), verifierat (explicit verifierad kapacitet), "
        "planerat (ännu inte utfört) och blockerat (kräver beslut eller saknad förmåga)."
    )
    lines.append(
        "Använd ENDAST siffror, commits, PR:er, jobb och godkännanden som står i detta block. Hitta aldrig "
        "på projektfakta, status eller auktoriserade beslut. Om något saknas, säg att det inte är verifierat."
    )
    # R15 (2026-11-19): local models were answering "det är inte verifierat" for facts that ARE in
    # this block ("0 uppgift(er) väntar på godkännande", "ingen godkänd blueprint ännu"), which is
    # safe but useless to the founder. The rule below keeps the no-fabrication guard while telling
    # the model the difference between "stated as none/zero here" and "absent from this block".
    lines.append(
        "Om blocket säger att något inte finns eller är 0 (t.ex. 'ingen godkänd blueprint ännu' eller "
        "'0 uppgift(er) väntar på godkännande'), säg det rakt ut som det står. Säg 'inte verifierat' "
        "endast när uppgiften inte finns i blocket alls. Om frågan gäller ett mål eller en uppgift "
        "som står i listan ovan, citera dess registrerade status (t.ex. 'completed') i stället för "
        "att säga att det inte finns."
    )
    return "\n".join(lines)
