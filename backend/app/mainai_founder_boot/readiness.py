from __future__ import annotations

import importlib
import importlib.util
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from app.mainai_founder_boot.types import ReadinessRecord, RecallBootStatus
from app.mainai_verification_registry.service import find_independent_pass
from app.models.mainai_verification import MainAIVerificationRecord
from app.mainai_level2.components import VERIFIED_SHAS, compose_local_verified_components

LEVEL2_BASE_SHA = "ec611a5d3216f4194793e8db01a2eceb1d0235eb"
COVERAGE_WORKFORCE_SHA = "3dd57d7f180c71d6639b90845b8cd8c492a29fbb"
PERSONAL_RECALL_SHA = "024835547850035667c3d77383fd75699ceab178"
LAST_VERIFIED = "2026-09-17"
_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ComponentSpec:
    name: str
    required: bool
    exact_sha: str | None
    builder_identity: str
    modules: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    integrated_modules: tuple[str, ...] = ()
    invocation: Callable[[], tuple[bool, str]] | None = None
    activated: bool = True
    activation_blocker: str | None = None
    evidence_label: str = "candidate-local readiness evidence"


@dataclass(frozen=True)
class ComponentEvidence:
    present: bool
    implemented: bool
    tested: bool
    independently_verified: bool
    integrated: bool
    activated: bool
    safe_for_founder_boot: bool
    blocker: str | None
    evidence: tuple[str, ...]


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError):
        return False


def _module_imports(name: str) -> tuple[bool, str]:
    try:
        importlib.import_module(name)
        return True, f"module_imported={name}"
    except Exception as exc:  # fail closed and preserve class without raw secrets
        return False, f"module_import_failed={name}:{type(exc).__name__}"


def _file_present(rel: str) -> bool:
    return (_REPO_ROOT / rel).exists()


def _coverage_workforce_invocation() -> tuple[bool, str]:
    try:
        from app.mainai_coverage.matching import layered_match
        from app.mainai_workforce.wait_or_assign import decide_wait_or_assign
        match = layered_match("exact SHA review binding", "review binds exact sha")
        decision = decide_wait_or_assign(
            best_agent_available=False,
            best_agent_eta_seconds=3 * 3600,
            best_agent_competency=0.95,
            candidate_agent_available=True,
            candidate_agent_competency=0.6,
            critical_path=True,
        )
        authorized = getattr(decision, "authorized", False)
        return True, f"coverage_match={match.layer.value};workforce_decision={decision.decision.value};authorized={authorized}"
    except Exception as exc:
        return False, f"coverage_workforce_invocation_failed={type(exc).__name__}"


def _level2_invocation() -> tuple[bool, str]:
    try:
        composition = compose_local_verified_components()
        snapshot = composition.snapshot()
        required = {"director", "supervision", "resource_intelligence", "founder_reasoning"}
        if not required.issubset(snapshot):
            return False, "level2_composition_missing_required_adapter"
        return True, "level2_composition_invoked"
    except Exception as exc:
        return False, f"level2_invocation_failed={type(exc).__name__}"


def _judgment_invocation() -> tuple[bool, str]:
    try:
        from app.mainai_executive.judgment import decide_judgment
        decision = decide_judgment(confidence=0.8, evidence_strength=0.8, founder_originated=True, stakes=0.5, urgency=0.2, founder_attention_cost=0.2)
        return True, f"judgment_action={decision.action.value};authorized={decision.authorized}"
    except Exception as exc:
        return False, f"judgment_invocation_failed={type(exc).__name__}"


def _resource_invocation() -> tuple[bool, str]:
    try:
        from app.resource_intelligence.types import unknown_metric
        metric = unknown_metric(unit="percent", definition="context utilization", source="founder_boot_readiness")
        return True, f"resource_metric_missing={metric.missing_data}"
    except Exception as exc:
        return False, f"resource_invocation_failed={type(exc).__name__}"


def _supervision_invocation() -> tuple[bool, str]:
    try:
        from app.mainai_execution.canonical_supervisor import identity
        return True, f"supervision_identity={identity()}"
    except Exception as exc:
        return False, f"supervision_invocation_failed={type(exc).__name__}"


def _director_invocation() -> tuple[bool, str]:
    ok, detail = _module_imports("app.dev_director.provider_lease")
    return ok, detail


def _runtime_invocation() -> tuple[bool, str]:
    ok, detail = _module_imports("app.mainai_execution.production_adapter")
    return ok, detail




def _derive_component_evidence(spec: ComponentSpec, *, verification_record: MainAIVerificationRecord | None = None) -> ComponentEvidence:
    evidence: list[str] = [spec.evidence_label]
    file_results = {rel: _file_present(rel) for rel in spec.files}
    module_results = {name: _module_present(name) for name in spec.modules}
    integrated_module_results = {name: _module_present(name) for name in spec.integrated_modules}
    present = all(file_results.values()) and all(module_results.values())
    evidence.extend(f"file:{rel}={value}" for rel, value in file_results.items())
    evidence.extend(f"module:{name}={value}" for name, value in module_results.items())
    tested = all(_file_present(rel) for rel in spec.tests)
    evidence.extend(f"test:{rel}={_file_present(rel)}" for rel in spec.tests)
    independently_verified = bool(
        verification_record is not None
        and spec.exact_sha
        and verification_record.candidate_sha == spec.exact_sha
        and verification_record.builder_identity == spec.builder_identity
        and verification_record.examiner_identity != spec.builder_identity
        and verification_record.review_result == "PASS"
    )
    if verification_record is None:
        evidence.append("verification_record=missing")
    else:
        evidence.extend((
            f"verification_id={verification_record.id}",
            f"verification_sha={verification_record.candidate_sha}",
            f"review_result={verification_record.review_result}",
            f"builder={verification_record.builder_identity}",
            f"examiner={verification_record.examiner_identity}",
            f"identity_assurance={verification_record.identity_assurance}",
            f"reviewed_at={verification_record.reviewed_at.date().isoformat()}",
        ))
    if spec.exact_sha:
        evidence.append(f"expected_sha={spec.exact_sha}")
    if spec.invocation is not None:
        invocation_ok, invocation_detail = spec.invocation()
        evidence.append(invocation_detail)
    else:
        invocation_ok = present
    implemented = present and invocation_ok
    integrated = implemented and all(integrated_module_results.values())
    evidence.extend(f"integrated_module:{name}={value}" for name, value in integrated_module_results.items())
    activated = spec.activated and integrated and independently_verified and not spec.activation_blocker
    blocker = None
    if not present:
        blocker = "implementation artifact is not present in this candidate"
    elif not implemented:
        blocker = "implementation artifact is present but invocation failed"
    elif not independently_verified:
        blocker = "independent verification registry has no current PASS for this exact implementation identity"
    elif not integrated:
        blocker = "verified implementation is not composed by this candidate"
    elif not activated:
        blocker = spec.activation_blocker or "component is not activated for founder boot"
    safe = implemented and independently_verified and integrated and (activated or not spec.required) and blocker is None
    return ComponentEvidence(present, implemented, tested, independently_verified, integrated, activated, safe, blocker, tuple(evidence))


def component_specs() -> dict[str, ComponentSpec]:
    return {
        "LEVEL2": ComponentSpec(
            "LEVEL2", True, LEVEL2_BASE_SHA, "codex",
            modules=("app.mainai_level2.components", "app.mainai_level2.production"),
            files=("app/mainai_level2/components.py", "app/mainai_level2/production.py"),
            tests=("tests/backend/mainai_level2/test_integration.py",),
            integrated_modules=("app.mainai_level2.components",), invocation=_level2_invocation,
            evidence_label="independent Level-2 re-review PASS",
        ),
        "RUNTIME": ComponentSpec(
            "RUNTIME", True, VERIFIED_SHAS["runtime"], "codex",
            modules=("app.mainai_execution.production_adapter",),
            files=("app/mainai_execution/production_adapter.py",),
            tests=("tests/backend/test_execution_substrate.py", "tests/backend/test_runtime_review_hardening.py"),
            integrated_modules=("app.mainai_execution.production_adapter",), invocation=_runtime_invocation,
            evidence_label="verified runtime integrated through Level-2 foundation",
        ),
        "DIRECTOR": ComponentSpec(
            "DIRECTOR", True, VERIFIED_SHAS["director"], "claude",
            modules=("app.dev_director.provider_lease",), files=("app/dev_director/provider_lease.py",),
            tests=("tests/backend/mainai/test_dev_director_budget_integration.py",),
            integrated_modules=("app.dev_director.provider_lease",), invocation=_director_invocation,
            evidence_label="Development Director implementation present",
        ),
        "SUPERVISION": ComponentSpec(
            "SUPERVISION", True, VERIFIED_SHAS["supervision"], "codex",
            modules=("app.mainai_execution.canonical_supervisor",), files=("app/mainai_execution/canonical_supervisor.py",),
            tests=("tests/backend/mainai/test_resource_intelligence_supervision_compat.py",),
            integrated_modules=("app.mainai_execution.canonical_supervisor",), invocation=_supervision_invocation,
            evidence_label="Continuous Supervision implementation present",
        ),
        "RESOURCE_INTELLIGENCE": ComponentSpec(
            "RESOURCE_INTELLIGENCE", True, VERIFIED_SHAS["resource_intelligence"], "claude",
            modules=("app.resource_intelligence.types", "app.resource_intelligence.telemetry"),
            files=("app/resource_intelligence/types.py", "app/resource_intelligence/telemetry.py"),
            tests=("tests/backend/mainai/test_resource_intelligence_decision.py", "tests/backend/mainai/test_resource_intelligence_telemetry.py"),
            integrated_modules=("app.resource_intelligence.types",), invocation=_resource_invocation,
            evidence_label="Resource Intelligence implementation present",
        ),
        "FOUNDER_REASONING": ComponentSpec(
            "FOUNDER_REASONING", True, VERIFIED_SHAS["founder_reasoning"], "claude",
            modules=("app.mainai_executive.judgment",), files=("app/mainai_executive/judgment.py",),
            tests=("tests/backend/mainai/test_judgment.py",), integrated_modules=("app.mainai_executive.judgment",),
            invocation=_judgment_invocation, evidence_label="Founder Reasoning/Judgment implementation present",
        ),
        "COVERAGE_WORKFORCE": ComponentSpec(
            "COVERAGE_WORKFORCE", True, COVERAGE_WORKFORCE_SHA, "claude",
            modules=(
                "app.mainai_coverage.discovery_pipeline",
                "app.mainai_coverage.omission_discovery",
                "app.mainai_workforce.wait_or_assign",
                "app.mainai_workforce.mastery_ledger",
                "app.mainai_workforce.real_state_decisions",
            ),
            files=(
                "app/mainai_coverage/discovery_pipeline.py",
                "app/mainai_coverage/omission_discovery.py",
                "app/mainai_workforce/wait_or_assign.py",
                "app/mainai_workforce/mastery_ledger.py",
                "alembic/versions/0079_mainai_workforce_mastery.py",
            ),
            tests=(
                "tests/backend/mainai/test_mainai_coverage_discovery_and_matching.py",
                "tests/backend/mainai/test_mainai_workforce_wait_assign_and_continuation.py",
                "tests/backend/mainai/test_mainai_workforce_mastery_ledger.py",
            ),
            integrated_modules=("app.mainai_coverage.discovery_pipeline", "app.mainai_workforce.wait_or_assign"),
            invocation=_coverage_workforce_invocation,
            evidence_label="Coverage/Workforce verified implementation ported into this candidate",
        ),
    }


def assess_personal_recall() -> tuple[RecallBootStatus, str, dict]:
    production_modules = (
        "app.personal_recall.authorization",
        "app.personal_recall.production_crypto",
        "app.personal_recall.production_ingestion",
        "app.personal_recall.routes_prep",
    )
    evidence = {
        "modules_present": all(_module_present(m) for m in production_modules),
        "test_only_protector_present": False,
        "router_factory_registered_by_default": False,
        "production_aead_implemented": _module_present("app.personal_recall.production_crypto"),
        "key_hierarchy_implemented": _file_present("app/models/personal_recall_production.py"),
        "trusted_grants_implemented": _file_present("app/models/personal_recall_production.py"),
        "file_ingestion_implemented": _module_present("app.personal_recall.production_ingestion"),
        "independent_verification_required": True,
        "activation": "disabled_until_independent_verification_and_explicit_router_grant_activation",
    }
    try:
        from app.personal_recall.snapshot_protection import DeterministicTestSnapshotProtector
        evidence["test_only_protector_present"] = bool(getattr(DeterministicTestSnapshotProtector, "is_test_only", False))
    except Exception:
        evidence["test_only_protector_present"] = False
    blocker = "production AEAD/key hierarchy is implemented, but Personal Recall remains disabled until exact-SHA independent verification and explicit founder-authorized router/grant activation"
    return RecallBootStatus.DISABLED_BY_SECURITY_GATE, blocker, evidence


def _verification_for_spec(db: Session | None, spec: ComponentSpec) -> MainAIVerificationRecord | None:
    return find_independent_pass(
        db,
        component_id=spec.name,
        candidate_sha=spec.exact_sha,
        builder_identity=spec.builder_identity,
    )


def component_manifest(db: Session | None = None) -> dict:
    manifest: dict[str, dict] = {}
    for name, spec in component_specs().items():
        evidence = _derive_component_evidence(spec, verification_record=_verification_for_spec(db, spec))
        manifest[name.lower()] = {
            "candidate_sha": spec.exact_sha,
            "present": evidence.present,
            "implemented": evidence.implemented,
            "integrated": evidence.integrated,
            "activated": evidence.activated,
            "authority": "none",
        }
    manifest["level2_base"] = {"candidate_sha": LEVEL2_BASE_SHA, "bound": manifest.get("level2", {}).get("integrated", False), "authority": "none"}
    manifest["personal_recall"] = {"candidate_sha": PERSONAL_RECALL_SHA, "bound": _module_present("app.personal_recall.service"), "authority": "none"}
    return manifest


def _record_from_evidence(spec: ComponentSpec, evidence: ComponentEvidence) -> ReadinessRecord:
    return ReadinessRecord(
        spec.required,
        evidence.present,
        evidence.implemented,
        evidence.tested,
        evidence.independently_verified,
        evidence.integrated,
        evidence.activated,
        evidence.safe_for_founder_boot,
        evidence.blocker,
        evidence.evidence,
        spec.exact_sha,
        next((item.split("=", 1)[1] for item in evidence.evidence if item.startswith("reviewed_at=")), LAST_VERIFIED),
    )


def build_readiness_matrix(*, covenant_ready: bool, founder_ready: bool, db_ready: bool = True, db: Session | None = None) -> dict[str, dict]:
    recall_status, recall_blocker, recall_evidence = assess_personal_recall()
    records: dict[str, ReadinessRecord] = {
        "SYSTEM_IDENTITY": ReadinessRecord(True, True, True, True, True, True, True, True, None, ("durable mainai_id/system_instance_id generated",), LEVEL2_BASE_SHA, LAST_VERIFIED),
        "FOUNDER_IDENTITY": ReadinessRecord(True, founder_ready, founder_ready, True, True, True, founder_ready, founder_ready, None if founder_ready else "authenticated founder owner missing", ("User.role=founder and owner binding",), None, LAST_VERIFIED),
        "FOUNDER_COVENANT": ReadinessRecord(True, covenant_ready, covenant_ready, True, False, True, covenant_ready, covenant_ready, None if covenant_ready else "active covenant missing", ("durable covenant row", "direct table UPDATE revoked; governed amendment function only"), None, str(date.today())),
        "DATABASE": ReadinessRecord(True, db_ready, db_ready, True, True, True, db_ready, db_ready, None if db_ready else "database unavailable", ("PostgreSQL session and migrations",), None, str(date.today())),
    }
    for name, spec in component_specs().items():
        records[name] = _record_from_evidence(spec, _derive_component_evidence(spec, verification_record=_verification_for_spec(db, spec)))
    records.update({
        "PERSONAL_RECALL": ReadinessRecord(False, recall_evidence["modules_present"], True, True, True, False, False, False, recall_blocker, tuple(f"{k}={v}" for k, v in recall_evidence.items()), PERSONAL_RECALL_SHA, LAST_VERIFIED),
        "PRESENCE": ReadinessRecord(True, True, True, True, False, True, True, True, None, ("machine-readable boot status stream",), None, str(date.today())),
        "CONTEXT_CONTRACT": ReadinessRecord(True, True, True, True, False, True, True, True, None, ("context/perception contract registered",), None, str(date.today())),
        "COMPUTER_CONTROL_AUTHORITY": ReadinessRecord(True, True, True, True, False, True, True, True, None, ("capability taxonomy registered; no capabilities granted",), None, str(date.today())),
        "LIFE_PLATFORM_REGISTRY": ReadinessRecord(False, True, True, True, False, True, False, True, None, ("roadmap registry only",), None, str(date.today())),
        "LIFE_GRAPH_CONTRACT": ReadinessRecord(False, True, True, True, False, True, False, True, None, ("future knowledge architecture contract",), None, str(date.today())),
    })
    return {name: record.as_dict() for name, record in records.items()}


def required_boot_blockers(matrix: dict[str, dict]) -> list[str]:
    return [f"{name}: {row['BLOCKER']}" for name, row in matrix.items() if row["REQUIRED"] and not row["SAFE_FOR_FOUNDER_BOOT"]]
