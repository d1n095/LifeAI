from __future__ import annotations

import importlib.util
from datetime import date

from app.mainai_founder_boot.types import ReadinessRecord, RecallBootStatus
from app.mainai_level2.components import VERIFIED_SHAS, compose_local_verified_components

LEVEL2_BASE_SHA = "ec611a5d3216f4194793e8db01a2eceb1d0235eb"
COVERAGE_WORKFORCE_SHA = "3dd57d7f180c71d6639b90845b8cd8c492a29fbb"
PERSONAL_RECALL_SHA = "024835547850035667c3d77383fd75699ceab178"
LAST_VERIFIED = "2026-09-17"


def _module_present(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def assess_personal_recall() -> tuple[RecallBootStatus, str, dict]:
    evidence = {
        "modules_present": all(_module_present(m) for m in (
            "app.personal_recall.authorization", "app.personal_recall.snapshot_protection", "app.personal_recall.routes_prep"
        )),
        "test_only_protector_present": False,
        "router_factory_registered_by_default": False,
        "production_aead_verified": False,
        "activation": "disabled_by_boot_gate",
    }
    try:
        from app.personal_recall.snapshot_protection import DeterministicTestSnapshotProtector
        evidence["test_only_protector_present"] = bool(getattr(DeterministicTestSnapshotProtector, "is_test_only", False))
    except Exception:
        evidence["test_only_protector_present"] = False
    blocker = "production AEAD/key hierarchy and trusted grant/router activation are not proven active in this tree"
    return RecallBootStatus.DISABLED_BY_SECURITY_GATE, blocker, evidence


def component_manifest() -> dict:
    composition = compose_local_verified_components()
    manifest = composition.snapshot()
    manifest["level2_base"] = {"candidate_sha": LEVEL2_BASE_SHA, "bound": True, "authority": "none"}
    manifest["coverage_workforce"] = {"candidate_sha": COVERAGE_WORKFORCE_SHA, "bound": _module_present("app.workforce.selector"), "authority": "none"}
    manifest["personal_recall"] = {"candidate_sha": PERSONAL_RECALL_SHA, "bound": _module_present("app.personal_recall.service"), "authority": "none"}
    return manifest


def build_readiness_matrix(*, covenant_ready: bool, founder_ready: bool, db_ready: bool = True) -> dict[str, dict]:
    recall_status, recall_blocker, recall_evidence = assess_personal_recall()
    records: dict[str, ReadinessRecord] = {
        "SYSTEM_IDENTITY": ReadinessRecord(True, True, True, True, True, True, True, True, None, ("durable mainai_id/system_instance_id generated",), LEVEL2_BASE_SHA, LAST_VERIFIED),
        "FOUNDER_IDENTITY": ReadinessRecord(True, founder_ready, founder_ready, True, True, True, founder_ready, founder_ready, None if founder_ready else "authenticated founder owner missing", ("User.role=founder and owner binding",), None, LAST_VERIFIED),
        "FOUNDER_COVENANT": ReadinessRecord(True, covenant_ready, covenant_ready, True, False, True, covenant_ready, covenant_ready, None if covenant_ready else "active covenant missing", ("durable covenant row",), None, str(date.today())),
        "DATABASE": ReadinessRecord(True, db_ready, db_ready, True, True, True, db_ready, db_ready, None if db_ready else "database unavailable", ("PostgreSQL session and migrations",), None, str(date.today())),
        "LEVEL2": ReadinessRecord(True, True, True, True, True, True, True, True, None, ("independent Level-2 re-review PASS",), LEVEL2_BASE_SHA, LAST_VERIFIED),
        "RUNTIME": ReadinessRecord(True, True, True, True, True, True, True, True, None, ("ProductionExecutionAdapter via Level-2 composition",), VERIFIED_SHAS["runtime"], LAST_VERIFIED),
        "DIRECTOR": ReadinessRecord(True, True, True, True, True, True, True, True, None, ("app.dev_director provider lease and loop modules present",), VERIFIED_SHAS["director"], LAST_VERIFIED),
        "SUPERVISION": ReadinessRecord(True, True, True, True, True, True, True, True, None, ("canonical supervisor integrated",), VERIFIED_SHAS["supervision"], LAST_VERIFIED),
        "RESOURCE_INTELLIGENCE": ReadinessRecord(True, True, True, True, True, True, True, True, None, ("resource intelligence modules and telemetry migration integrated",), VERIFIED_SHAS["resource_intelligence"], LAST_VERIFIED),
        "FOUNDER_REASONING": ReadinessRecord(True, True, True, True, True, True, True, True, None, ("deterministic judgment interface integrated",), VERIFIED_SHAS["founder_reasoning"], LAST_VERIFIED),
        "COVERAGE_WORKFORCE": ReadinessRecord(True, _module_present("app.workforce.selector"), True, True, True, True, True, True, None, ("Coverage/Workforce candidate independently PASSed; workforce modules present",), COVERAGE_WORKFORCE_SHA, LAST_VERIFIED),
        "PERSONAL_RECALL": ReadinessRecord(False, recall_evidence["modules_present"], True, True, True, False, False, False, recall_blocker, tuple(f"{k}={v}" for k, v in recall_evidence.items()), PERSONAL_RECALL_SHA, LAST_VERIFIED),
        "PRESENCE": ReadinessRecord(True, True, True, True, False, True, True, True, None, ("machine-readable boot status stream",), None, str(date.today())),
        "CONTEXT_CONTRACT": ReadinessRecord(True, True, True, True, False, True, True, True, None, ("context/perception contract registered",), None, str(date.today())),
        "COMPUTER_CONTROL_AUTHORITY": ReadinessRecord(True, True, True, True, False, True, True, True, None, ("capability taxonomy registered; no capabilities granted",), None, str(date.today())),
        "LIFE_PLATFORM_REGISTRY": ReadinessRecord(False, True, True, True, False, True, False, True, None, ("roadmap registry only",), None, str(date.today())),
        "LIFE_GRAPH_CONTRACT": ReadinessRecord(False, True, True, True, False, True, False, True, None, ("future knowledge architecture contract",), None, str(date.today())),
    }
    return {name: record.as_dict() for name, record in records.items()}


def required_boot_blockers(matrix: dict[str, dict]) -> list[str]:
    return [f"{name}: {row['BLOCKER']}" for name, row in matrix.items() if row["REQUIRED"] and not row["SAFE_FOR_FOUNDER_BOOT"]]
