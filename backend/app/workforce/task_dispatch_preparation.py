"""Integration glue: wires three independently-built, already-tested modules into one
demonstrable agent-task-context-preparation chain.

Founder directive 2026-11-15 ("PR consolidation and executable integration"): the two modules
this wires together both explicitly documented, in their own PR bodies, that their own built
functions had zero callers anywhere in the codebase --

  - `app.mainai_execution.integration_intelligence.integration_lessons_for_task_context()`
    (its own docstring: "The attach point: ... ready to drop into an AgentTaskContext via
    app.workforce.context_handoff.build_agent_task_context(integration_lessons=...)")
  - `app.workforce.context_handoff.build_agent_task_context()` (accepts `integration_lessons`
    but nothing in the codebase ever retrieved any and passed them in)

`app.session_context.assemble_session_context()` is independent of both and was never wired to
either. This module is the missing caller -- not a new capability, purely composition of what
already exists and is already tested (77 passing tests across the three source PRs).

Deliberately stops short of a live external-agent dispatch
(`app.agent_coordination.dispatch.dispatch_assignment()`) -- that step is already built and
fail-closed-gated (verified extensively in this session's own PR #408-410 activation-authority
work), but the stack that was meant to wire THIS specific context/lessons packet into that call
(#338/#339) is stale (based on a much older ancestor, 5-6 days before this chain) and explicitly
self-flagged as "independent review required -- the author cannot certify own changes," with no
live agent CLI available anywhere in this environment to prove a real round-trip either way.
Wiring to it is a separate, later task, not bundled into this one.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.mainai_execution.integration_intelligence import (
    integration_lessons_for_task_context,
)
from app.session_context import assemble_session_context
from app.workforce.context_handoff import (
    AgentTaskContext,
    SourceRef,
    build_agent_task_context,
)


def prepare_agent_task_context(
    db: Session,
    *,
    owner_id: uuid.UUID,
    session_id: str,
    task_id: str,
    objective: str,
    acceptance_criteria: list[str],
    repo: str,
    branch: str,
    base_sha: str,
    trust_zone: str,
    source_refs: list[SourceRef] | None = None,
    lesson_components: list[str] | None = None,
    lesson_providers: list[str] | None = None,
    lesson_interfaces: list[str] | None = None,
    lesson_dependencies: list[str] | None = None,
    max_lessons: int = 5,
    decisions: list[str] | None = None,
    constraints: list[str] | None = None,
    prior_attempts: list[str] | None = None,
    dependencies: list[str] | None = None,
    ownership_boundaries: list[str] | None = None,
    tests_ci: list[str] | None = None,
    approval_limits: list[str] | None = None,
    missing_info: list[str] | None = None,
    uncertainty: list[str] | None = None,
) -> AgentTaskContext:
    """The actual missing wiring. Three steps, each delegating entirely to its own already-
    reviewed module -- this function adds no new logic of its own beyond composing them:

    1. `assemble_session_context()` (#381) -- the current read-only snapshot of readiness,
       founder memory, active work and truth-claim state for this owner/session.
    2. `integration_lessons_for_task_context()` (#380) -- past integration failures relevant
       to THIS task's declared `lesson_*` tags (named distinctly from `build_agent_task_context`'s
       own `dependencies` param below -- `lesson_dependencies` tags lessons by dependency name
       for retrieval, e.g. "postgres"; `dependencies` is free-text for the agent packet, e.g.
       "waits on PR #123" -- these are NOT the same field and must never be confused).
    3. `build_agent_task_context()` (#377) -- the compact, security-checked packet, with the
       retrieved lessons attached and the session bundle's own advisory observations folded
       into `missing_info` (never silently dropped, never escalated past the "advisory
       observation" semantics `SessionContextBundle.to_dict()` itself declares -- this is a
       READ of that bundle, not a reinterpretation of its enforcement status).
    """
    bundle = assemble_session_context(db, owner_id=owner_id, session_id=session_id)

    effective_missing_info = list(missing_info or [])
    if bundle.blocked:
        effective_missing_info.extend(f"session_context_advisory:{reason}" for reason in bundle.blocked_reasons)

    lessons = integration_lessons_for_task_context(
        db,
        components=lesson_components,
        providers=lesson_providers,
        interfaces=lesson_interfaces,
        dependencies=lesson_dependencies,
        max_lessons=max_lessons,
    )

    return build_agent_task_context(
        task_id=task_id,
        objective=objective,
        acceptance_criteria=acceptance_criteria,
        repo=repo,
        branch=branch,
        base_sha=base_sha,
        trust_zone=trust_zone,
        source_refs=source_refs,
        integration_lessons=lessons,
        decisions=decisions,
        constraints=constraints,
        prior_attempts=prior_attempts,
        dependencies=dependencies,
        ownership_boundaries=ownership_boundaries,
        tests_ci=tests_ci,
        approval_limits=approval_limits,
        missing_info=effective_missing_info,
        uncertainty=uncertainty,
        db=db,
    )
