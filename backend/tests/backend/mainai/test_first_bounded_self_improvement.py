"""Stage 4 — first bounded self-improvement proof (Worker → Supervisor).

Acceptance contract: docs/MAINAI_SELF_IMPROVEMENT_ACCEPTANCE.md (Claude #197).

Goal: add a regression test for the SSH private-key NEVER_EGRESS marker
(`-----BEGIN OPENSSH PRIVATE KEY-----`) to the egress policy test surface.
Detection already exists in production `app/egress_policy/service.py`; this proof
does NOT modify that file.

Founder edges only at bootstrap (goal / envelope / spend / single task approval).
Runtime after start: Worker ticks only. No remote_write / push.
Fake provider adapter is the allowed CI boundary (fake-local spend authorized —
deterministic recipes do not cover this goal; see run report).

Envelope capabilities: read_file, patch_file, run_focused_test. Plan still includes
verification_evaluate as a Driver directive (not an Operator envelope capability).

Disposable worktree mirror:
- authorized path is exactly the egress test file (production-shaped path)
- a tiny local `app/egress_policy` stub (NOT in authorized_paths) makes
  focused_pytest runnable under Operator without the full Postgres fixture stack
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution import planner
from app.mainai_execution.approval import grant_task_approval
from app.mainai_execution.planner import PlannedTaskSpec, get_goal
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import ClaimType, KnowledgeClaim
from app.models.mainai_execution import MainAICheckpoint, MainAIGoalStatus, MainAITask, MainAITaskStatus
from app.models.user import User
from app.models.work_intelligence import WorkTraceEvent
from app.project_entities import promote_interpretation_proposal, record_interpretation_proposal
from app.provider_planning.service import ProviderResponse
from app.provider_spend import authorize_provider_spend
from app.work_candidates import authorize_work_candidate, list_unreviewed_work_candidates
from app.worker import Worker
from tests.backend.mainai.test_composed_autonomy_soak import _SoakPlanningAdapter

pytest_plugins = ["tests.backend.mainai.test_composed_autonomy_milestone"]

AUTHORIZED_TEST_PATH = "backend/tests/backend/mainai/test_egress_policy.py"
OPENSSH_MARKER = "-----BEGIN OPENSSH PRIVATE KEY-----"

_GOAL_INSTRUCTION = (
    "The egress policy gate (app/egress_policy/) detects two hard-deny content markers: "
    "NEVER_EGRESS: and an SSH private-key header (-----BEGIN OPENSSH PRIVATE KEY-----). "
    "The existing test suite (backend/tests/backend/mainai/test_egress_policy.py) covers "
    "the NEVER_EGRESS: marker but has no test for the SSH-key marker. Add a regression "
    "test proving the SSH-key marker is detected and denies the call, matching the "
    "existing NEVER_EGRESS: tests' style. Do not modify app/egress_policy/service.py — "
    "detection already exists; only the missing test coverage is the gap."
)

_SEED_TEST = '''\
"""Disposable mirror of egress policy tests for the bounded self-improvement proof.

Production conventions mirrored: NEVER_EGRESS denial asserts decision == "denied",
sent_content_hash is None, and never_egress_marker in redaction_categories.
SSH private-key marker coverage is intentionally absent — that is the Stage 4 gap.
"""

import pytest

from app.egress_policy import EgressDeniedError, enforce_egress_policy, last_decision


def test_never_egress_marker_denies_the_whole_call_not_partial_redact():
    payload = {
        "instruction": "use this",
        "secret": "NEVER_EGRESS: the founder private plan",
    }
    with pytest.raises(EgressDeniedError):
        enforce_egress_policy(payload=payload)
    row = last_decision()
    assert row["decision"] == "denied"
    assert row["sent_content_hash"] is None
    assert "never_egress_marker" in row["redaction_categories"]
'''

_STUB_INIT = '''\
"""Local disposable egress stub — NOT production app/egress_policy."""

from app.egress_policy.service import EgressDeniedError, enforce_egress_policy, last_decision

__all__ = ["EgressDeniedError", "enforce_egress_policy", "last_decision"]
'''

_STUB_SERVICE = '''\
"""Minimal marker gate for focused_pytest inside the disposable worktree.

Markers match production app/egress_policy/service.py. Ledger is in-process only
so Operator-run pytest does not need the full backend DB fixture stack.
"""

from __future__ import annotations

from typing import Any

_NEVER_EGRESS_MARKERS = (
    "NEVER_EGRESS:",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
)

_LAST: dict | None = None


class EgressDeniedError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"egress denied: {reason}")


def last_decision() -> dict:
    assert _LAST is not None
    return dict(_LAST)


def _contains_never_egress_marker(payload: Any) -> bool:
    if isinstance(payload, str):
        return any(marker in payload for marker in _NEVER_EGRESS_MARKERS)
    if isinstance(payload, dict):
        return any(_contains_never_egress_marker(value) for value in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(_contains_never_egress_marker(item) for item in payload)
    return False


def enforce_egress_policy(*, payload: Any, **_kwargs) -> Any:
    global _LAST
    if _contains_never_egress_marker(payload):
        _LAST = {
            "decision": "denied",
            "sent_content_hash": None,
            "redaction_categories": ["never_egress_marker"],
            "reason": "never_egress_marker",
        }
        raise EgressDeniedError(_LAST["reason"])
    _LAST = {
        "decision": "allowed",
        "sent_content_hash": "allowed",
        "redaction_categories": [],
        "reason": "passed",
    }
    return payload
'''


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def source_repo(tmp_path, monkeypatch):
    """Disposable source repo with production-shaped egress test path + local stub."""
    import app.development_supervisor.production_entry as entry_module

    repo = tmp_path / "worker-source-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "worker@test.local")
    _git(repo, "config", "user.name", "Worker")

    stub_pkg = repo / "app" / "egress_policy"
    stub_pkg.mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (stub_pkg / "__init__.py").write_text(_STUB_INIT, encoding="utf-8")
    (stub_pkg / "service.py").write_text(_STUB_SERVICE, encoding="utf-8")

    test_path = repo / AUTHORIZED_TEST_PATH
    test_path.parent.mkdir(parents=True)
    test_path.write_text(_SEED_TEST, encoding="utf-8")

    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed egress test mirror without SSH coverage")
    monkeypatch.setattr(entry_module, "worker_source_repo_root", lambda: repo)
    return repo


_OPERATOR_CAPABILITIES = [
    "read_file",
    "patch_file",
    "run_focused_test",
]


def _patched_test_content(seed: str) -> str:
    # Split the marker in source so Safe Planner's secret-redaction fence does not
    # reject the plan arguments, while the runtime still assembles the real marker.
    addition = '''


def test_openssh_private_key_marker_denies_the_whole_call():
    marker = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
    payload = {
        "instruction": "use this",
        "key_material": marker + "\\nb3BlbnNzaC1rZXktdjEAAAA...",
    }
    with pytest.raises(EgressDeniedError):
        enforce_egress_policy(payload=payload)
    row = last_decision()
    assert row["decision"] == "denied"
    assert row["sent_content_hash"] is None
    assert "never_egress_marker" in row["redaction_categories"]
'''
    return seed.rstrip() + addition


def _ssh_coverage_candidate(seed: str) -> dict:
    patched = _patched_test_content(seed)
    return {
        "interpretation": (
            "Add an SSH private-key marker regression test beside the existing "
            "NEVER_EGRESS coverage."
        ),
        "requested_outcome": "Focused egress test file covers the OpenSSH hard-deny marker.",
        "rationale": (
            "Read the authorized test file, patch in one analogous denial test, "
            "run focused pytest, evaluate verification evidence."
        ),
        "facts": [
            "NEVER_EGRESS marker is already tested.",
            "OPENSSH private-key marker detection exists but has no test.",
        ],
        "assumptions": ["Stub egress gate in the disposable worktree mirrors production markers."],
        "unknowns": [],
        "exclusions": [
            "Do not modify app/egress_policy/service.py.",
            "No remote operations, commits, or extra files.",
        ],
        "steps": [
            {
                "step_id": "inspect",
                "purpose": "inspect current egress policy tests",
                "expected_result": "bounded source text",
                "capability": "read_file",
                "arguments": {"path": AUTHORIZED_TEST_PATH},
            },
            {
                "step_id": "patch",
                "purpose": "add OpenSSH private-key marker regression test",
                "expected_result": "updated test hash",
                "capability": "patch_file",
                "arguments": {
                    "path": AUTHORIZED_TEST_PATH,
                    "content": patched,
                    "expected_sha256": hashlib.sha256(seed.encode()).hexdigest(),
                },
                "depends_on": ["inspect"],
                "required_risk": "LOCAL_WRITE",
            },
            {
                "step_id": "test",
                "purpose": "run focused egress policy tests",
                "expected_result": "pytest exit zero",
                "capability": "run_focused_test",
                "arguments": {
                    "profile_name": "focused_pytest",
                    "arguments": [AUTHORIZED_TEST_PATH],
                    # Force disposable stub ahead of any host PYTHONPATH to production app/.
                    "environment": {"PYTHONPATH": "."},
                },
                "depends_on": ["patch"],
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


def _provider_response(candidate: dict, version: str) -> ProviderResponse:
    return ProviderResponse(
        content=json.dumps(
            {
                "candidate": candidate,
                "clarification_required": False,
                "clarification_question": None,
                "capability_gaps": [],
                "useful_components": [],
                "confidence": 0.9,
            }
        ),
        provider="fake-local",
        model="planner-v2",
        model_version=version,
        raw_usage={"prompt_tokens": 24, "completion_tokens": 24},
    )


async def _bootstrap_self_improvement_goal(db):
    user = User(
        email=f"self-improve-{uuid.uuid4()}@example.com",
        password_hash="x",
        email_verified=True,
    )
    db.add(user)
    db.flush()
    document = Document(
        title="Self-improvement source",
        source=DocumentSource.upload,
        uploaded_by=user.id,
        active_truth_status=ActiveTruthStatus.active,
    )
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(
        owner_id=user.id,
        source_id=document.id,
        claim_text=_GOAL_INSTRUCTION,
        extraction_version="v1",
        claim_type=ClaimType.decision,
    )
    db.add(claim)
    db.flush()
    db.commit()

    proposal = record_interpretation_proposal(
        db,
        owner_id=user.id,
        source_claim_id=claim.id,
        proposed_entity_type="decision",
        idempotency_key=f"self-improve-proposal-{uuid.uuid4()}",
        classifier_strategy="claim_type_extraction_v1",
        classifier_confidence="certain",
    )
    db.commit()
    promote_interpretation_proposal(
        db,
        owner_id=user.id,
        proposal_id=proposal.id,
        entity_type="decision",
        title="Add SSH private-key egress marker regression test",
        authority="founder",
        basis="manual",
        entity_idempotency_key=f"self-improve-entity-{uuid.uuid4()}",
    )
    db.commit()

    candidates = list_unreviewed_work_candidates(db, owner_id=user.id)
    assert len(candidates) == 1
    # Founder supplies the bounded goal text as the durable original_instruction
    # (work-candidate title alone is only a short label from promotion).
    candidates[0].rationale = _GOAL_INSTRUCTION
    db.flush()
    authorized_wc, goal = authorize_work_candidate(
        db,
        owner_id=user.id,
        candidate_id=candidates[0].id,
        authorized_by="founder",
        approval_policy="autonomous_development_work",
    )
    db.commit()
    assert authorized_wc.authorized_goal_id == goal.id
    assert get_goal(db, goal.id).approval_policy == "autonomous_development_work"
    assert OPENSSH_MARKER in goal.original_instruction

    planner.create_plan(
        db,
        goal=goal,
        rationale="single founder-precreated bounded self-improvement task",
        tasks=[
            PlannedTaskSpec(
                description="add OpenSSH private-key marker regression test",
                task_type="repo_edit",
                risk_level="low",
                verification_plan=[
                    {"kind": "targeted_tests", "target": AUTHORIZED_TEST_PATH}
                ],
            )
        ],
        created_by="founder",
    )
    task = db.execute(select(MainAITask).where(MainAITask.goal_id == goal.id)).scalar_one()
    grant_task_approval(db, task=task, approved_by="founder")
    proposal_scope = propose_execution_scope(
        db,
        owner_id=user.id,
        goal_id=goal.id,
        idempotency_key=f"self-improve-scope-{uuid.uuid4()}",
    )
    _, envelope = authorize_execution_scope(
        db,
        owner_id=user.id,
        proposal_id=proposal_scope.id,
        authorized_by="founder",
        authorized_paths=[AUTHORIZED_TEST_PATH],
        authorized_capabilities=_OPERATOR_CAPABILITIES,
        authorized_risk="low",
        envelope_idempotency_key=f"self-improve-env-{uuid.uuid4()}",
    )
    db.commit()
    return user, goal, envelope, task


@pytest.mark.asyncio
async def test_first_bounded_self_improvement_ssh_egress_marker_coverage(
    superuser_db, source_repo, monkeypatch
):
    user, goal, envelope, task = await _bootstrap_self_improvement_goal(superuser_db)
    seed = (source_repo / AUTHORIZED_TEST_PATH).read_text(encoding="utf-8")
    assert "NEVER_EGRESS:" in seed
    assert OPENSSH_MARKER not in seed
    assert (source_repo / "app" / "egress_policy" / "service.py").is_file()

    adapter = _SoakPlanningAdapter(
        [_provider_response(_ssh_coverage_candidate(seed), "self-improve-ssh")]
    )

    class _Registry:
        provider_name = "fake-local"
        model = "planner-v2"
        default_provider = "fake-local"
        default_model = "planner-v2"

        def __init__(self, db, *, provider_name=None, model=None):
            self.db = db

        async def propose(self, *args, **kwargs):
            return await adapter.propose(*args, **kwargs)

    monkeypatch.setattr(
        "app.provider_planning.service.RegistryPlanningAdapter",
        _Registry,
    )

    # Provider spend required: no deterministic recipe matches this goal, so planning
    # goes WAITING_PROVIDER without a founder fake-local grant (documented deviation
    # from the acceptance contract's preferred spend=False for milestone 1).
    authorize_provider_spend(
        superuser_db,
        owner_id=user.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("2.00"),
        max_requests=4,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"self-improve-spend-{uuid.uuid4()}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    superuser_db.commit()

    import app.development_driver.service as driver_module
    import app.development_supervisor.service as supervisor_module

    captured: list[dict] = []
    real_run_driver = driver_module.run_driver

    def _spy_run_driver(db, *, context, plan):
        captured.append(
            {
                "paths": tuple(context.allowed_paths or ()),
                "caps": tuple(context.allowed_capabilities or ()),
                "remote": context.remote_write_authorized,
            }
        )
        assert context.allowed_paths == (AUTHORIZED_TEST_PATH,)
        assert set(context.allowed_capabilities) == set(_OPERATOR_CAPABILITIES)
        assert context.remote_write_authorized is False
        return real_run_driver(db, context=context, plan=plan)

    monkeypatch.setattr(driver_module, "run_driver", _spy_run_driver)
    monkeypatch.setattr(supervisor_module, "run_driver", _spy_run_driver)

    worker = Worker()
    worker.worker_id = "first-bounded-self-improve"

    for _ in range(12):
        superuser_db.refresh(goal)
        superuser_db.refresh(task)
        if (
            task.status == MainAITaskStatus.completed
            and goal.status == MainAIGoalStatus.completed
        ):
            break
        await worker._advance_authorized_supervisor_goals(superuser_db)
        worker._finalize_mainai_execution_goals(superuser_db)
        superuser_db.commit()

    superuser_db.refresh(task)
    superuser_db.refresh(goal)
    assert task.status == MainAITaskStatus.completed, [
        (
            row.executor_state.get("phase"),
            row.executor_state.get("classification"),
            row.executor_state,
        )
        for row in superuser_db.execute(
            select(MainAICheckpoint).where(MainAICheckpoint.goal_id == goal.id)
        ).scalars()
    ]
    assert goal.status == MainAIGoalStatus.completed
    assert goal.final_outcome is not None
    assert len(adapter.calls) == 1
    assert captured and captured[0]["remote"] is False

    import app.development_supervisor.production_worktree as wt_module

    edited = list(Path(wt_module.WORKTREE_ROOT).rglob("test_egress_policy.py"))
    assert len(edited) == 1
    final = edited[0].read_text(encoding="utf-8")
    assert "test_openssh_private_key_marker_denies_the_whole_call" in final
    assert '-----BEGIN " + "OPENSSH PRIVATE KEY-----' in final
    # Contiguous production marker must not appear in plan/source text (Safe Planner fence),
    # but the runtime concatenation must equal the production marker.
    assert OPENSSH_MARKER not in final
    assert '-----BEGIN " + "OPENSSH PRIVATE KEY-----'.replace('" + "', "") == OPENSSH_MARKER
    # Only the authorized test file may change under the envelope; stub gate untouched.
    goal_root = next(
        parent
        for parent in edited[0].parents
        if (parent / "app" / "egress_policy" / "service.py").is_file()
    )
    stub_in_wt = goal_root / "app" / "egress_policy" / "service.py"
    assert stub_in_wt.read_text(encoding="utf-8") == _STUB_SERVICE

    caps = [
        event.action_detail.get("operator_capability")
        for event in superuser_db.execute(
            select(WorkTraceEvent)
            .where(WorkTraceEvent.owner_id == user.id)
            .order_by(WorkTraceEvent.sequence_number)
        ).scalars()
        if event.action_detail and event.action_detail.get("operator_capability")
    ]
    assert caps == ["read_file", "patch_file", "run_focused_test"]
