"""Model-backed specialist workers — adapter logic against the real engine + Postgres.

Real local model weights are exercised in CI (`.github/workflows/model-backed-ai-team.yml`,
like #334). Here a deterministic STUB local provider (name="ollama") drives the real
ModelBackedRunner → engine → workforce → Postgres path, so every guarantee around the model
is executed for real: schema validation, execution receipts, independent DETERMINISTIC
verification (not the builder model reviewing itself), honest failure without bypass, context
isolation, and fail-closed refusal of a non-local provider (no paid fallback).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.mainai_business.execution import ModelBackedExecutionError, ModelBackedRunner
from app.mainai_business.reference import run_ecommerce_opportunity_assessment
from app.models.user import User
from app.models.workforce import WorkforceAssignment, WorkforceDelegationRequest
from app.models.workforce_ops import WorkforceVerificationDecision
from app.providers.base import ChatResult, ProviderError

_GOOD = {
    # One response wrapped in prose + a code fence, to exercise the tolerant extractor.
    "market_research": 'Here is my finding:\n```json\n{"demand": "moderate", "note": "steady niche demand", "segments": ["value"]}\n```',
    "unit_economics": '{"viable": true, "margin_note": "positive contribution after CAC"}',
    "fulfillment_ops": '{"feasible": true, "risk_note": "single 3PL dependency"}',
    "opportunity_synthesis": '{"recommendation": "conditional-go", "reason": "viable with mitigations"}',
    "assessment_publication": '{"title": "E-commerce assessment", "recommendation": "conditional-go", "summary": "proceed with conditions"}',
}


def _capability_of(messages) -> str:
    system = next((m.content for m in messages if m.role == "system"), "")
    if "investigate-further" in system:
        return "opportunity_synthesis"
    if '"title"' in system:
        return "assessment_publication"
    if '"demand"' in system:
        return "market_research"
    if '"viable"' in system:
        return "unit_economics"
    if '"feasible"' in system:
        return "fulfillment_ops"
    raise AssertionError("could not infer capability from prompt")


class StubLocalProvider:
    """Stands in for Ollama: name='ollama', returns canned per-capability JSON."""

    name = "ollama"

    def __init__(self, responses: dict | None = None, *, raise_for: set | None = None):
        # A response may be a single string or a LIST consumed across successive calls for that
        # capability (to simulate a correction on a validator-feedback retry).
        self.responses = {**_GOOD, **(responses or {})}
        self.raise_for = raise_for or set()
        self.calls: list[tuple[str, list]] = []
        self._counts: dict[str, int] = {}

    async def chat(self, messages, model, *, timeout=None, **kwargs):
        cap = _capability_of(messages)
        self.calls.append((cap, messages))
        if cap in self.raise_for:
            raise ProviderError("Lokal modell (Ollama) inte nåbar: stub", category="unreachable")
        resp = self.responses[cap]
        if isinstance(resp, list):
            i = min(self._counts.get(cap, 0), len(resp) - 1)
            self._counts[cap] = self._counts.get(cap, 0) + 1
            resp = resp[i]
        return ChatResult(
            content=resp,
            provider=self.name,
            model=model,
            raw_usage={"prompt_tokens": 12, "completion_tokens": 7},
        )

    def call_count(self, capability: str) -> int:
        return sum(1 for c, _ in self.calls if c == capability)

    async def embed(self, texts, model, *, timeout=None):  # pragma: no cover - not used here
        raise NotImplementedError


def _owner(db) -> User:
    user = User(email=f"mbw-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _runner(stub=None, *, correction_attempts=0):
    return ModelBackedRunner(
        provider=stub or StubLocalProvider(), model="qwen2.5:0.5b", correction_attempts=correction_attempts
    )


def _assignment(db, owner_id, wf, task_key):
    for req in db.execute(
        select(WorkforceDelegationRequest).where(WorkforceDelegationRequest.owner_id == owner_id)
    ).scalars():
        prov = req.provenance or {}
        if prov.get("business_workflow_id") == wf and prov.get("task_key") == task_key:
            return db.execute(
                select(WorkforceAssignment)
                .where(WorkforceAssignment.delegation_request_id == req.id)
                .order_by(WorkforceAssignment.created_at.desc())
            ).scalars().first()
    return None


# ----------------------------------------------------------------- model-backed end to end

def test_model_backed_all_roles_real_path(superuser_db):
    owner = _owner(superuser_db)
    stub = StubLocalProvider()
    report = run_ecommerce_opportunity_assessment(
        superuser_db, owner_id=owner.id, approve_publish=True, runner=_runner(stub)
    )

    assert report.provider_invoked is True
    by_key = {t.task_key: t for t in report.tasks}
    for key in ("market_research", "unit_economics", "fulfillment_ops", "opportunity_synthesis", "publish_assessment"):
        t = by_key[key]
        assert t.status == "verified", (key, t.status, t.failure_reason)
        assert t.runtime == "model_backed"
        assert t.verification_status == "VERIFIED"
        r = t.receipt
        assert r["provider"] == "ollama" and r["model"] == "qwen2.5:0.5b"
        assert r["provider_invoked"] is True and r["parsed_ok"] is True
        assert r["prompt_sha256"] and r["raw_output_sha256"] and "usage" in r

    # Different specialists genuinely ran (one model call per task).
    assert {c for c, _ in stub.calls} == {
        "market_research", "unit_economics", "fulfillment_ops", "opportunity_synthesis", "assessment_publication"
    }
    # Schema-validated structured output, not free text.
    assert by_key["market_research"].output["demand"] in ("low", "moderate", "high")
    assert report.assessment["recommendation"] == "conditional-go"
    assert report.assessment["published"] is True

    # Independent verification is a deterministic validator applied by a DIFFERENT agent —
    # never the builder model certifying itself.
    a = _assignment(superuser_db, owner.id, report.workflow_id, "opportunity_synthesis")
    dec = superuser_db.execute(
        select(WorkforceVerificationDecision).where(
            WorkforceVerificationDecision.assignment_id == a.id,
            WorkforceVerificationDecision.decision == "VERIFIED",
        )
    ).scalars().first()
    assert dec is not None
    assert dec.deterministic_validator == "mainai_business.validators.opportunity_synthesis"
    assert dec.verifier_profile_id is not None and dec.verifier_profile_id != a.profile_id
    # Receipt persisted on the assignment provenance (durable audit).
    assert (a.provenance or {}).get("execution_receipt", {}).get("model") == "qwen2.5:0.5b"


# ----------------------------------------------------------------- context isolation

def test_worker_sees_only_permitted_context(superuser_db):
    owner = _owner(superuser_db)
    stub = StubLocalProvider()
    run_ecommerce_opportunity_assessment(superuser_db, owner_id=owner.id, approve_publish=False, runner=_runner(stub))

    calls = {cap: msgs for cap, msgs in stub.calls}
    # Synthesis prompt carries exactly the three upstream findings and nothing foreign.
    synth_user = next(m.content for m in calls["opportunity_synthesis"] if m.role == "user")
    for dep in ("market_research", "unit_economics", "fulfillment_ops"):
        assert dep in synth_user
    # Market research runs first with no upstream findings section.
    research_user = next(m.content for m in calls["market_research"] if m.role == "user")
    assert "FINDINGS FROM EARLIER SPECIALISTS" not in research_user


# ----------------------------------------------------------------- honest failure, no bypass

def test_unparseable_output_fails_task_and_blocks_dependents(superuser_db):
    owner = _owner(superuser_db)
    stub = StubLocalProvider(responses={"fulfillment_ops": "sorry, I cannot help with that"})
    report = run_ecommerce_opportunity_assessment(superuser_db, owner_id=owner.id, approve_publish=True, runner=_runner(stub))

    by_key = {t.task_key: t for t in report.tasks}
    assert by_key["fulfillment_ops"].status == "failed"
    assert by_key["fulfillment_ops"].failure_reason == "schema_validation_failed"
    a = _assignment(superuser_db, owner.id, report.workflow_id, "fulfillment_ops")
    assert a.verification_status == "REJECTED"
    # Dependent synthesis is blocked (unmet dependency) — never fabricated.
    assert by_key["opportunity_synthesis"].status == "blocked_unmet_dependency"
    assert "publish_assessment" in report.blocked_tasks
    assert report.assessment["published"] is False
    assert report.assessment["recommendation"] is None


def test_provider_unreachable_is_reported_not_bypassed(superuser_db):
    owner = _owner(superuser_db)
    stub = StubLocalProvider(raise_for={"market_research"})
    report = run_ecommerce_opportunity_assessment(superuser_db, owner_id=owner.id, approve_publish=False, runner=_runner(stub))
    mr = next(t for t in report.tasks if t.task_key == "market_research")
    assert mr.status == "failed"
    assert mr.failure_reason == "provider_error"
    assert mr.receipt.get("error")
    # Everything downstream of research is blocked; no results invented.
    assert report.assessment["recommendation"] is None


# ----------------------------------------------------------------- independent validator bites

def test_validator_rejects_output_inconsistent_with_upstream(superuser_db):
    owner = _owner(superuser_db)
    # Finance says NOT viable, but synthesis recommends "go" — the deterministic validator must
    # reject it (a model cannot talk its way past the independent check).
    stub = StubLocalProvider(responses={
        "unit_economics": '{"viable": false, "margin_note": "negative contribution"}',
        "opportunity_synthesis": '{"recommendation": "go", "reason": "ignoring the finance result"}',
    })
    report = run_ecommerce_opportunity_assessment(superuser_db, owner_id=owner.id, approve_publish=True, runner=_runner(stub))
    by_key = {t.task_key: t for t in report.tasks}
    assert by_key["unit_economics"].status == "verified"  # false-but-valid finance output
    assert by_key["opportunity_synthesis"].status == "rejected_by_validator"
    a = _assignment(superuser_db, owner.id, report.workflow_id, "opportunity_synthesis")
    assert a.verification_status == "REJECTED"
    assert report.assessment["published"] is False


# ----------------------------------------------------------------- no paid-provider fallback

def test_non_local_provider_is_refused_fail_closed(superuser_db, monkeypatch):
    owner = _owner(superuser_db)

    class PaidProvider:
        name = "openai"

        async def chat(self, *a, **k):  # pragma: no cover - must never be reached
            raise AssertionError("paid provider must never be called")

    def fake_resolve_active(db, role):
        return PaidProvider(), "gpt-4o-mini"

    monkeypatch.setattr("app.providers.registry.resolve_active", fake_resolve_active)
    # Runner with provider=None resolves from config and must refuse the non-local provider.
    runner = ModelBackedRunner(require_local=True)
    with pytest.raises(ModelBackedExecutionError):
        run_ecommerce_opportunity_assessment(superuser_db, owner_id=owner.id, approve_publish=False, runner=runner)


# ----------------------------------------------------------------- bounded authorized recovery

def test_bounded_recovery_corrects_invalid_publication(superuser_db):
    owner = _owner(superuser_db)
    # Publication is INVALID on the first attempt (wrong recommendation) then CORRECTED on the
    # feedback retry. With one correction attempt the task must recover and complete.
    stub = StubLocalProvider(responses={
        "assessment_publication": [
            '{"title": "E-commerce assessment", "recommendation": "no-go", "summary": "wrong first try"}',
            '{"title": "E-commerce assessment", "recommendation": "conditional-go", "summary": "corrected"}',
        ],
    })
    report = run_ecommerce_opportunity_assessment(
        superuser_db, owner_id=owner.id, approve_publish=True, runner=_runner(stub, correction_attempts=1)
    )
    pub = next(t for t in report.tasks if t.task_key == "publish_assessment")
    assert pub.status == "verified", (pub.status, pub.failure_reason)
    assert pub.correction_attempts == 1  # it took exactly one authorized correction
    assert stub.call_count("assessment_publication") == 2  # original + one retry (bounded)
    assert report.status == "completed"
    assert report.final_deliverable is not None
    assert report.assessment["published"] is True


def test_unrecoverable_invalid_publication_stays_rejected(superuser_db):
    owner = _owner(superuser_db)
    # Publication is ALWAYS invalid (recommendation never matches synthesis). Bounded recovery
    # must give up and leave it REJECTED — never fabricate a successful publication.
    stub = StubLocalProvider(responses={
        "assessment_publication": '{"title": "x", "recommendation": "no-go", "summary": "never matches"}',
    })
    report = run_ecommerce_opportunity_assessment(
        superuser_db, owner_id=owner.id, approve_publish=True, runner=_runner(stub, correction_attempts=2)
    )
    pub = next(t for t in report.tasks if t.task_key == "publish_assessment")
    assert pub.status == "rejected_by_validator"
    assert pub.failure_kind == "output_quality"
    assert stub.call_count("assessment_publication") == 3  # original + exactly 2 corrections (bounded, not infinite)
    assert report.status == "failed"
    assert report.final_deliverable is None
    assert report.assessment["published"] is False
    a = _assignment(superuser_db, owner.id, report.workflow_id, "publish_assessment")
    assert a.verification_status == "REJECTED"
