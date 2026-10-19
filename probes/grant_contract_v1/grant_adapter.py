"""Adapter layer between the examiner's grant CONTRACT tests and whatever grant / remote-effect API a
candidate exposes. Assertions live in test_grant_contract.py and never change per candidate; only
this file learns a new API.

Rules:
- discovery is by inspection of the candidate's importable modules; nothing is assumed;
- an API that is present but not understood raises AdapterNeeded (reported as a BLOCKER, never PASS);
- a capability the candidate does not offer (e.g. a distinct draft-PR grant) raises CapabilityMissing
  (reported as BLOCKED, never PASS);
- adapters only translate calls; they never catch, soften or reinterpret a denial.

Contract vocabulary follows Codex's transport spec (codex/mainai-git-transport-credential-audit
@ bf9616e: effect_bindings, push_branch vs open_pr effect kinds, consume-before-network).
"""

from __future__ import annotations

import importlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

EFFECT_PUSH = "push_branch"
EFFECT_OPEN_PR = "open_pr"

# Module names a RemoteEffectExecutor-style implementation is expected to use (spec §3B). Probed in order.
REMOTE_EFFECT_MODULE_CANDIDATES = (
    "app.mainai_execution.remote_effects",
    "app.mainai_execution.remote_effect_executor",
    "app.mainai_execution.transport",
    "app.mainai_execution.git_transport",
    "app.remote_effects",
)


class AdapterNeeded(Exception):
    """A grant API exists but this adapter does not know how to drive it. BLOCKER."""


class CapabilityMissing(Exception):
    """The candidate does not provide the capability under test. BLOCKED (never PASS)."""


@dataclass
class GrantContext:
    owner_id: uuid.UUID
    task_id: uuid.UUID
    job_id: uuid.UUID
    local_commit_sha: str
    base_sha: str
    authority_version: int
    extra: dict[str, Any] = field(default_factory=dict)


class Grant280Adapter:
    """Drives Codex #280 `app.mainai_execution.activation_security` (push grants only)."""

    name = "activation_security(#280)"

    def __init__(self, mod):
        self.m = mod

    def supports(self, effect_kind: str) -> bool:
        return effect_kind == EFFECT_PUSH

    def denial_types(self) -> tuple[type, ...]:
        return (self.m.ActivationAuthorityError,)

    # ---- graph: everything the API requires before a grant can be minted
    def build_context(self, db, owner_id) -> GrantContext:
        from decimal import Decimal

        from app.execution_envelopes.service import authorize_execution_scope, propose_execution_scope
        from app.mainai_execution import planner
        from app.mainai_execution.planner import PlannedTaskSpec
        from app.models.mainai_execution import MainAITask, MainAITaskEvent, MainAITaskEventType
        from app.models.mainai_job import MainAIJob, MainAIJobStatus
        from app.models.mainai_recovery import MainAITaskWorktree, MainAITaskWorktreeStatus
        from app.provider_spend.service import authorize_provider_spend
        from sqlalchemy import text

        repo, base, local, act = "d1n095/LifeAI", "a" * 40, "b" * 40, "c" * 40
        integration = "mainai/integration-founder-supervised-v1"
        db.execute(text("SET LOCAL app.current_user_id = :u"), {"u": str(owner_id)})
        goal = planner.create_goal(db, owner_id=owner_id, title="grant", original_instruction="grant contract", created_by="founder")
        proposal = propose_execution_scope(db, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"p:{uuid.uuid4()}", repository_identity=repo)
        _, env = authorize_execution_scope(
            db, owner_id=owner_id, proposal_id=proposal.id, authorized_by="founder", authorized_paths=["backend/app/"],
            authorized_capabilities=["path_a", "repo_edit", "push"], authorized_risk="low", envelope_idempotency_key=f"e:{uuid.uuid4()}",
            repository_identity=repo, base_branch="main", approved_base_sha=base,
            required_check_contexts=[{"name": "backend", "app_slug": "github-actions"}], authority_version=7,
        )
        spend = authorize_provider_spend(
            db, owner_id=owner_id, goal_id=goal.id, execution_envelope_id=env.id, authorized_by="founder", max_cost_usd=Decimal("1"),
            max_requests=1, max_cost_per_request_usd=Decimal("1"), allowed_providers=["openai"], allowed_models=["gpt-4o-mini"],
            expires_at=datetime.utcnow() + timedelta(hours=1), idempotency_key=f"s:{uuid.uuid4()}",
        )
        kw = dict(description="edit", task_type="repo_edit", verification_plan=[{"kind": "targeted_tests", "target": "tests/x.py"}])
        import dataclasses

        if "target_files" in {f.name for f in dataclasses.fields(PlannedTaskSpec)}:
            kw["target_files"] = ["backend/app/x.py"]
        planner.create_plan(db, goal=goal, rationale="g", created_by="founder", tasks=[PlannedTaskSpec(**kw)])
        task = db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
        now = datetime.utcnow()
        job = MainAIJob(owner_id=owner_id, job_type="task_execution", status=MainAIJobStatus.completed, started_at=now, completed_at=now,
                        idempotency_key=f"j:{uuid.uuid4()}", created_by="founder")
        db.add(job)
        db.flush()
        task.mainai_job_id = job.id
        # NOTE: #280 requires the worktree to sit ON the integration branch (its P1-4); the adapter
        # reproduces what the API accepts so the one-use/race contract can still be exercised.
        wt = MainAITaskWorktree(task_id=task.id, owner_id=owner_id, job_id=job.id, lease_generation=1, executor_id="t", repo=repo,
                                base_sha=base, branch=integration, path="/nonexistent/grant-contract", marker_token="m",
                                current_commit=local, status=MainAITaskWorktreeStatus.active)
        db.add(wt)
        db.add(MainAITaskEvent(task_id=task.id, owner_id=owner_id, event_type=MainAITaskEventType.verification_passed, detail={"sha": local}))
        db.flush()
        facts = self.m.AuthoritativePreflightFacts(
            activation_sha=act, repository_identity=repo, integration_branch=integration, observed_base_sha=base,
            runtime_config={"w": True}, runtime_token_identity="github-app:1", runtime_token_attestation_digest="1" * 64,
            ruleset_attestation_digest="2" * 64, founder_attestation_digest="3" * 64, negative_authority_confirmed=True,
        )
        receipt = self.m.create_preflight_receipt(
            db, owner_id=owner_id, goal_id=goal.id, execution_envelope_id=env.id, provider_spend_authorization_id=spend.id,
            expected_repository_identity=repo, expected_integration_branch=integration, expected_base_branch="main",
            expected_base_sha=base, expected_activation_sha=act, expected_authority_version=7, facts=facts,
            expires_at=datetime.utcnow() + timedelta(minutes=14), founder_authorized=True,
        )
        db.commit()
        return GrantContext(owner_id=owner_id, task_id=task.id, job_id=job.id, local_commit_sha=local, base_sha=base, authority_version=7,
                            extra={"goal_id": goal.id, "worktree_id": wt.id, "envelope_id": env.id, "receipt_id": receipt.id,
                                   "repo": repo, "branch": integration})

    @staticmethod
    def _owner_scope(db, owner_id):
        """#280's API is RLS-scoped: every call runs in the owner's row-security context."""
        from sqlalchemy import text

        db.execute(text("SET LOCAL app.current_user_id = :u"), {"u": str(owner_id)})

    def mint(self, db, ctx: GrantContext, *, effect_kind: str = EFFECT_PUSH, expires_in=timedelta(minutes=5), idempotency_key=None):
        self._owner_scope(db, ctx.owner_id)
        if not self.supports(effect_kind):
            raise CapabilityMissing(f"{self.name} has no {effect_kind} grant")
        e = ctx.extra
        row = self.m.create_push_grant(
            db, owner_id=ctx.owner_id, goal_id=e["goal_id"], task_id=ctx.task_id, job_id=ctx.job_id, worktree_id=e["worktree_id"],
            execution_envelope_id=e["envelope_id"], preflight_receipt_id=e["receipt_id"], repository_identity=e["repo"], branch=e["branch"],
            base_branch="main", base_sha=ctx.base_sha, local_commit_sha=ctx.local_commit_sha, authority_version=ctx.authority_version,
            expires_at=datetime.utcnow() + expires_in, idempotency_key=idempotency_key or f"g:{uuid.uuid4()}", authorized_by="founder",
            founder_authorized=True,
        )
        db.flush()
        return row.id

    def consume(self, db, ctx: GrantContext, grant_id, *, effect_kind: str = EFFECT_PUSH, job_id=None, local_commit_sha=None,
                observed_base_sha=None, authority_version=None, now=None):
        self._owner_scope(db, ctx.owner_id)
        if not self.supports(effect_kind):
            raise CapabilityMissing(f"{self.name} has no {effect_kind} grant")
        return self.m.consume_push_grant(
            db, owner_id=ctx.owner_id, grant_id=grant_id, job_id=job_id or ctx.job_id,
            local_commit_sha=local_commit_sha or ctx.local_commit_sha, observed_base_sha=observed_base_sha or ctx.base_sha,
            authority_version=authority_version or ctx.authority_version, now=now,
        )

    def cancel(self, db, ctx: GrantContext, grant_id):
        self._owner_scope(db, ctx.owner_id)
        return self.m.cancel_push_grant(db, owner_id=ctx.owner_id, grant_id=grant_id, founder_authorized=True)

    def status(self, db, grant_id) -> str:
        from sqlalchemy import text

        return db.execute(text("SELECT status FROM mainai_push_grants WHERE id=:i"), {"i": str(grant_id)}).scalar_one()


def discover():
    """Return an adapter for the candidate's grant API, or raise AdapterNeeded / CapabilityMissing."""
    found = []
    for name in REMOTE_EFFECT_MODULE_CANDIDATES:
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        found.append((name, sorted(n for n in dir(mod) if not n.startswith("_"))[:40]))
    if found:
        raise AdapterNeeded(f"remote-effect module(s) present but no adapter written yet: {found}")
    try:
        mod = importlib.import_module("app.mainai_execution.activation_security")
    except ImportError:
        raise CapabilityMissing("candidate exposes no grant / remote-effect API (Codex transport not composed)")
    needed = ("create_push_grant", "consume_push_grant", "cancel_push_grant", "create_preflight_receipt", "ActivationAuthorityError")
    missing = [n for n in needed if not hasattr(mod, n)]
    if missing:
        raise AdapterNeeded(f"activation_security present but API changed (missing {missing}); write an adapter")
    return Grant280Adapter(mod)
