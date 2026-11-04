"""External Agents as Teachers / Capability Learning Loop. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

EXTERNAL AGENT WORK -> OBSERVE METHOD -> EXTRACT REUSABLE PROCEDURE -> LOCAL PRACTICE ->
VERIFY RESULT -> INDEPENDENT EXAM -> UPDATE CAPABILITY PROFILE -> GRADUALLY REDUCE EXTERNAL
DEPENDENCE.

AGENT OUTPUT != LEARNING. COPYING CODE != ACQUIRING CAPABILITY. Does NOT store hidden
chain-of-thought -- `extract_reusable_procedure()` only ever restructures the OBSERVABLE
process-evidence fields already on `TeacherObservation` (files inspected, tools used, debug
method, tests selected, errors, corrections); it never accepts or stores free-form reasoning
text beyond those named, observable fields."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.mainai_workforce.mastery_ledger import get_or_create_mastery_record, record_observation
from app.mainai_workforce.types import TeacherObservation


@dataclass(frozen=True)
class ReusableProcedure:
    """A restructured, reusable summary of ONE external teacher observation -- deterministic
    field-copy, never an inference or summarization the caller didn't already supply."""

    task_class: str
    provider: str
    decomposition_steps: tuple[str, ...]
    inspection_targets: tuple[str, ...]
    tools: tuple[str, ...]
    debug_method: str | None
    test_selection_strategy: tuple[str, ...]
    root_cause_method: str | None
    dependency_tracing_steps: tuple[str, ...]
    corrections_to_avoid_repeating: tuple[str, ...]


def extract_reusable_procedure(observation: TeacherObservation) -> ReusableProcedure:
    """AGENT OUTPUT != LEARNING: this function only restructures what was OBSERVED about HOW
    the external agent worked -- it never touches whatever code/text the agent actually
    produced (that is a separate, unrelated concern this function has no access to)."""

    return ReusableProcedure(
        task_class=observation.task_class, provider=observation.provider,
        decomposition_steps=observation.problem_decomposition_steps, inspection_targets=observation.files_inspected,
        tools=observation.tools_used, debug_method=observation.debug_method,
        test_selection_strategy=observation.tests_selected, root_cause_method=observation.root_cause_method,
        dependency_tracing_steps=observation.dependency_tracing_steps,
        corrections_to_avoid_repeating=observation.examiner_corrections,
    )


def apply_teacher_observation(
    db: Session, *, owner_id: uuid.UUID, capability_key: str, observation: TeacherObservation, idempotency_key: str,
) -> tuple[ReusableProcedure, dict]:
    """Composes the durable ledger with the pure extraction step: records ONE observation
    against the (capability_key, task_class, provider) mastery row, creating it if this is the
    first time this exact combination has been observed. `local_practice`/`local_success`/
    `examiner_pass` are NOT set here -- those are separate, later facts a caller records via
    `mastery_ledger.record_observation()` directly once local practice/exam actually happens
    (ONE LOCAL SUCCESS != MASTERY: observing the teacher is not itself local practice)."""

    procedure = extract_reusable_procedure(observation)
    mastery = get_or_create_mastery_record(
        db, owner_id=owner_id, capability_key=capability_key, task_class=observation.task_class,
        external_teacher=observation.provider, idempotency_key=idempotency_key,
    )
    updated = record_observation(
        db, owner_id=owner_id, mastery_id=mastery["id"],
        failure_mode=observation.errors_encountered[0] if observation.errors_encountered else None,
    )
    return procedure, updated
