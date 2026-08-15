import hashlib
import json
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.development_driver.service import run_driver
from app.models.intelligence_governance import IntelligenceIdea
from app.models.mainai_execution import MainAICheckpoint, MainAITaskStatus
from app.models.usage import UsageLog
from app.models.work_intelligence import WorkSpecialistContribution, WorkTraceEvent
from app.provider_planning.service import (
    CandidateComponent,
    ProviderCandidateEnvelope,
    ProviderPlanningLimits,
    ProviderResponse,
    build_provider_request,
    evaluate_supplied_candidates,
    parse_provider_envelope,
    plan_with_provider,
    planning_mode,
)
from app.providers.base import ProviderError
from app.safe_planner.service import (
    CandidateStep,
    CandidateValidationError,
    PlanCandidate,
)
from tests.backend.mainai.test_development_operator import _git
from tests.backend.mainai.test_safe_planner import _scope


COMPLEX_REQUEST = (
    "Inspect the calculator module, determine what is missing for multiplication support, "
    "update the implementation safely and add a focused test."
)


class FakePlanningAdapter:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def propose(self, request_payload, *, timeout_seconds, max_output_bytes):
        self.calls.append((request_payload, timeout_seconds, max_output_bytes))
        if self.error:
            raise self.error
        return self.response


def _provider_scope(db, tmp_path, instruction=COMPLEX_REQUEST):
    owner, goal, task, job, worktree, context, request = _scope(
        db, tmp_path, instruction
    )
    calculator = "def add(left, right):\n    return left + right\n"
    (context.repository_root / "calculator.py").write_text(calculator)
    _git(context.repository_root, "add", "calculator.py")
    _git(context.repository_root, "commit", "-q", "-m", "add calculator base")
    base = _git(context.repository_root, "rev-parse", "HEAD")
    worktree.base_sha = base
    context = replace(context, expected_base_sha=base)
    task.verification_plan = [
        {"kind": "targeted_tests", "target": "test_calculator.py"}
    ]
    db.flush()
    return owner, goal, task, job, worktree, context, request, calculator


def _candidate_payload(calculator):
    updated = calculator + "\ndef multiply(left, right):\n    return left * right\n"
    test = (
        "from calculator import multiply\n\n"
        "def test_multiply():\n"
        "    assert multiply(6, 7) == 42\n"
    )
    return {
        "interpretation": "Add multiplication to the existing calculator and verify it.",
        "requested_outcome": "A focused, verified multiplication helper.",
        "rationale": "Inspect the canonical file, patch its exact hash, and run one test.",
        "facts": ["calculator.py currently contains only addition."],
        "assumptions": ["Integer multiplication is the requested behavior."],
        "unknowns": [],
        "exclusions": ["No unrelated edits or remote operations."],
        "steps": [
            {
                "step_id": "inspect",
                "purpose": "inspect current calculator",
                "expected_result": "bounded source text",
                "capability": "read_file",
                "arguments": {"path": "calculator.py"},
            },
            {
                "step_id": "patch",
                "purpose": "add multiplication helper",
                "expected_result": "new source hash",
                "capability": "patch_file",
                "arguments": {
                    "path": "calculator.py",
                    "content": updated,
                    "expected_sha256": hashlib.sha256(calculator.encode()).hexdigest(),
                },
                "depends_on": ["inspect"],
                "required_risk": "LOCAL_WRITE",
            },
            {
                "step_id": "test-file",
                "purpose": "add focused multiplication test",
                "expected_result": "test file hash",
                "capability": "create_file",
                "arguments": {
                    "path": "test_calculator.py",
                    "content": test,
                    "expected_sha256": None,
                },
                "depends_on": ["patch"],
                "required_risk": "LOCAL_WRITE",
            },
            {
                "step_id": "test",
                "purpose": "verify multiplication",
                "expected_result": "pytest exit zero",
                "capability": "run_focused_test",
                "arguments": {
                    "profile_name": "focused_pytest",
                    "arguments": ["test_calculator.py"],
                },
                "depends_on": ["test-file"],
                "required_risk": "LOCAL_EXECUTION",
                "verification_required": True,
            },
            {
                "step_id": "gate",
                "purpose": "evaluate deterministic evidence",
                "expected_result": "verification checkpoint",
                "capability": "verification_evaluate",
                "arguments": {},
                "depends_on": ["test"],
            },
        ],
    }


def _response(candidate, **values):
    envelope = {
        "candidate": candidate,
        "clarification_required": values.pop("clarification_required", False),
        "clarification_question": values.pop("clarification_question", None),
        "capability_gaps": values.pop("capability_gaps", []),
        "useful_components": values.pop("useful_components", []),
        "confidence": values.pop("confidence", 0.7),
    }
    assert not values
    return ProviderResponse(
        content=json.dumps(envelope),
        provider="fake-local",
        model="planner-v2",
        model_version="2026-08",
        raw_usage={"prompt_tokens": 123, "completion_tokens": 77},
    )


@pytest.mark.asyncio
async def test_deterministic_recipe_avoids_provider(superuser_db, tmp_path):
    _, _, _, _, _, context, request = _scope(superuser_db, tmp_path)
    adapter = FakePlanningAdapter(error=AssertionError("provider must not be called"))
    mode, _ = planning_mode(superuser_db, request)
    assert mode == "DETERMINISTIC_PLAN_AVAILABLE"
    result = await plan_with_provider(
        superuser_db,
        request=request,
        operator_context=context,
        adapter=adapter,
    )
    assert result.classification == "ACCEPTED"
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_complex_provider_plan_runs_end_to_end_and_records_usage(
    superuser_db, tmp_path
):
    owner, goal, task, job, _, context, request, calculator = _provider_scope(
        superuser_db, tmp_path
    )
    adapter = FakePlanningAdapter(_response(_candidate_payload(calculator)))
    result = await plan_with_provider(
        superuser_db,
        request=request,
        operator_context=context,
        adapter=adapter,
    )
    assert result.classification == "ACCEPTED"
    assert result.explanation["provider_planning"]["provider"] == "fake-local"
    assert goal.original_instruction == COMPLEX_REQUEST
    completed = run_driver(
        superuser_db, context=context, plan=result.plan, max_actions=10
    )
    assert completed.classification == "COMPLETE"
    assert task.status == MainAITaskStatus.completed
    assert job.status.value == "completed"
    assert "multiply" in (context.repository_root / "calculator.py").read_text()
    assert (
        superuser_db.execute(
            select(UsageLog).where(
                UsageLog.user_id == owner.id, UsageLog.role == "planning"
            )
        )
        .scalar_one()
        .prompt_tokens
        == 123
    )
    contribution = superuser_db.execute(
        select(WorkSpecialistContribution).where(
            WorkSpecialistContribution.owner_id == owner.id
        )
    ).scalar_one()
    assert contribution.contribution == "confirmed_finding"
    assert [
        event.action_detail["operator_capability"]
        for event in superuser_db.execute(
            select(WorkTraceEvent).order_by(WorkTraceEvent.sequence_number)
        ).scalars()
    ] == ["read_file", "patch_file", "create_file", "run_focused_test"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capability",
    ["shell", "merge", "deploy", "force_push", "production_mutation"],
)
async def test_provider_forbidden_capabilities_fail_closed(
    superuser_db, tmp_path, capability
):
    _, _, _, _, _, context, request, _ = _provider_scope(superuser_db, tmp_path)
    candidate = {
        "interpretation": "unsafe operational plan",
        "requested_outcome": "unsafe",
        "rationale": "attempt forbidden effect",
        "steps": [
            {
                "step_id": "bad",
                "purpose": "forbidden effect",
                "expected_result": "never",
                "capability": capability,
                "arguments": {},
            }
        ],
    }
    result = await plan_with_provider(
        superuser_db,
        request=request,
        operator_context=context,
        adapter=FakePlanningAdapter(_response(candidate)),
    )
    assert result.classification == "REJECTED_UNSAFE"
    assert result.plan is None


@pytest.mark.asyncio
async def test_hallucinated_capability_is_gap_not_shell_fallback(
    superuser_db, tmp_path
):
    _, _, _, _, _, context, request, _ = _provider_scope(superuser_db, tmp_path)
    candidate = {
        "interpretation": "use nonexistent tool",
        "requested_outcome": "parsed data",
        "rationale": "tool would be needed",
        "steps": [
            {
                "step_id": "gap",
                "purpose": "perform unsupported operation",
                "expected_result": "data",
                "capability": "nonexistent_super_tool",
                "arguments": {},
            }
        ],
    }
    result = await plan_with_provider(
        superuser_db,
        request=request,
        operator_context=context,
        adapter=FakePlanningAdapter(_response(candidate)),
    )
    assert result.classification == "CAPABILITY_MISSING"
    assert result.plan is None


@pytest.mark.asyncio
async def test_authority_and_ambiguity_precede_provider_confidence(
    superuser_db, tmp_path
):
    _, _, _, _, _, context, request, _ = _provider_scope(superuser_db, tmp_path)
    adapter = FakePlanningAdapter(error=AssertionError("provider must not be called"))
    suggestion = await plan_with_provider(
        superuser_db,
        request=replace(request, authority_kind="suggestion"),
        operator_context=context,
        adapter=adapter,
    )
    assert suggestion.classification == "NEEDS_AUTHORIZATION"
    ambiguous = await plan_with_provider(
        superuser_db,
        request=replace(request, ambiguity_refs=("multiply-or-matrix-product",)),
        operator_context=context,
        adapter=adapter,
    )
    assert ambiguous.classification == "NEEDS_CLARIFICATION"
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_provider_outage_checkpoints_without_erasing_deterministic_state(
    superuser_db, tmp_path
):
    _, _, task, _, _, context, request, _ = _provider_scope(superuser_db, tmp_path)
    result = await plan_with_provider(
        superuser_db,
        request=request,
        operator_context=context,
        adapter=FakePlanningAdapter(
            error=ProviderError("quota unavailable", category="rate_limited")
        ),
    )
    assert result.classification == "WAITING_PROVIDER"
    assert result.explanation["failure_category"] == "rate_limited"
    assert result.explanation["unrelated_deterministic_work_preserved"] is True
    checkpoint = superuser_db.get(MainAICheckpoint, result.checkpoint_id)
    assert checkpoint.executor_state["provider_planning"]["request_hash"]
    assert task.status == MainAITaskStatus.running
    assert superuser_db.execute(select(WorkTraceEvent)).scalars().all() == []


@pytest.mark.asyncio
async def test_malformed_output_and_secret_scope_fail_closed(superuser_db, tmp_path):
    _, _, _, _, _, context, request, _ = _provider_scope(superuser_db, tmp_path)
    malformed = await plan_with_provider(
        superuser_db,
        request=request,
        operator_context=context,
        adapter=FakePlanningAdapter(
            ProviderResponse("not-json", "fake-local", "bad-model")
        ),
    )
    assert malformed.classification == "PROVIDER_FAILED"
    with pytest.raises(Exception, match="sensitive credential paths"):
        build_provider_request(
            superuser_db,
            request=request,
            operator_context=replace(context, allowed_paths=(".env",)),
            limits=ProviderPlanningLimits(),
        )


def test_provider_prompt_is_bounded_and_redacted(superuser_db, tmp_path):
    _, goal, _, _, _, context, request, _ = _provider_scope(superuser_db, tmp_path)
    secret = "Plan this with password=hunter2 and token=ghp_abcdefghijklmnopqrstuvwxyz"
    goal.original_instruction = secret
    request = replace(
        request,
        original_instruction=secret,
        constraints=("bearer abcdefghijklmnopqrstuvwxyz",),
        source_ref="message:api_key=sk-abcdefghijklmnopqrstuvwxyz",
    )
    _, payload = build_provider_request(
        superuser_db,
        request=request,
        operator_context=context,
        limits=ProviderPlanningLimits(max_payload_bytes=32_000),
    )
    serialized = json.dumps(payload)
    assert "hunter2" not in serialized
    assert "abcdefghijklmnopqrstuvwxyz" not in serialized
    assert len(payload["context_references"]) <= request.max_context_members


def test_strict_envelope_rejects_malformed_boolean():
    payload = {
        "candidate": {
            "interpretation": "x",
            "requested_outcome": "y",
            "rationale": "z",
            "steps": [
                {
                    "step_id": "one",
                    "purpose": "p",
                    "expected_result": "e",
                    "capability": "read_file",
                    "arguments": {"path": "safe.txt"},
                    "verification_required": "yes",
                }
            ],
        },
        "clarification_required": False,
    }
    with pytest.raises(CandidateValidationError, match="must be boolean"):
        parse_provider_envelope(json.dumps(payload), max_output_bytes=10_000)


def test_multiple_candidates_preserve_rejected_useful_component(superuser_db, tmp_path):
    _, _, _, _, _, context, request, calculator = _provider_scope(
        superuser_db, tmp_path
    )
    accepted_candidate = parse_provider_envelope(
        _response(_candidate_payload(calculator)).content,
        max_output_bytes=64_000,
    )
    rejected = PlanCandidate(
        interpretation="unsafe but contains one useful search idea",
        requested_outcome="unsafe",
        rationale="attempt an unsupported effect",
        steps=(CandidateStep("bad", "merge automatically", "merged", "merge"),),
    )
    rejected_envelope = ProviderCandidateEnvelope(
        candidate=rejected,
        useful_components=(
            CandidateComponent(
                "narrow-search",
                "search_strategy",
                "Inspect the canonical symbol before broad repository search.",
                "accepted",
                "Useful independently of the rejected merge step.",
            ),
        ),
    )
    evaluations = evaluate_supplied_candidates(
        superuser_db,
        request=request,
        operator_context=context,
        candidates=[
            (
                ProviderResponse("candidate-a", "provider-a", "model-a"),
                accepted_candidate,
            ),
            (
                ProviderResponse("candidate-b", "provider-b", "model-b"),
                rejected_envelope,
            ),
        ],
    )
    assert [item.disposition for item in evaluations] == [
        "ACCEPTED",
        "REJECTED_UNSAFE",
    ]
    assert evaluations[1].useful_component_ids
    idea = superuser_db.get(
        IntelligenceIdea, uuid.UUID(evaluations[1].useful_component_ids[0])
    )
    assert idea.disposition == "accepted"
    assert idea.content.startswith("Inspect the canonical symbol")


@pytest.mark.asyncio
async def test_accepted_provider_candidate_replay_does_not_repeat_call_or_usage(
    superuser_db, tmp_path
):
    owner, _, _, _, _, context, request, calculator = _provider_scope(
        superuser_db, tmp_path
    )
    adapter = FakePlanningAdapter(_response(_candidate_payload(calculator)))

    first = await plan_with_provider(
        superuser_db, request=request, operator_context=context, adapter=adapter
    )
    replay = await plan_with_provider(
        superuser_db, request=request, operator_context=context, adapter=adapter
    )

    assert first.plan == replay.plan
    assert len(adapter.calls) == 1
    assert (
        len(
            superuser_db.execute(
                select(UsageLog).where(
                    UsageLog.user_id == owner.id, UsageLog.role == "planning"
                )
            )
            .scalars()
            .all()
        )
        == 1
    )
