#!/usr/bin/env python3
"""MainAI capability acceptance — the ten founder-facing capabilities, verified by evidence.

This is an ORCHESTRATOR over pytest suites that already exist, not a second test framework and
not a duplicate suite. It answers exactly one question per capability:

    "which existing, real-service tests prove this, did they actually run, and what did they say?"

Why the matrix exists
----------------------
`program_matrix.py` answers the F01-F24 workstream question from evidence files produced by the
V5-V9 tools. The ten capabilities the founder named for the Founder Alpha candidate cut across
those workstreams (one capability can need three suites; one suite can prove two capabilities),
and no existing artifact maps them. This module is that mapping, and its status is derived the
same way `program_matrix.py` derives F01-F24: from terminal JUnit evidence, never from "a test
file exists".

Status semantics (fail-closed, identical vocabulary to `program_matrix.py`)
--------------------------------------------------------------------------
  * PASS:     every module mapped to this leg ran and at least one test passed, none failed.
  * FAIL:     a mapped test ran and failed/errored.
  * BLOCKED:  a mapped module produced no testcase at all (collection error, missing suite), so
              a PASS cannot be claimed.
  * NOT_RUN:  the leg's environment tier is not enabled (real Ollama absent, VPS-only), or every
              mapped test skipped itself with an environmental reason.

Tiers, so local and staging evidence are never confused
-------------------------------------------------------
  * ``local``             Postgres + Redis + the restricted app role. Runs on any dev machine.
  * ``needs_real_ollama`` a real Ollama runtime with the pinned chat model installed.
  * ``needs_vps``         16 GB VPS / production model behaviour. Never claimed locally.

Every capability is reported per leg, so "local green, model untested" cannot read as "PASS".

Safety
------
  * refuses a non-loopback database or a database name without test/staging/pytest in it
    (pytest's own conftest drops and recreates that database — see scripts/acceptance/README.md);
  * never migrates, deploys, merges, pushes, or activates autonomous execution;
  * writes evidence only under ``--out-dir``.

Usage
-----
    cd backend
    python scripts/acceptance/mainai_capability_acceptance.py --list

    DATABASE_URL=postgresql://postgres@127.0.0.1:55432/lifeos_test_capability \\
    APP_DATABASE_URL=postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_test_capability \\
    LIFEAI_TEST_DATABASE_NAME=lifeos_test_capability \\
    REDIS_URL=redis://127.0.0.1:56379/11 \\
    python scripts/acceptance/mainai_capability_acceptance.py \\
      --out-dir ../docs/evidence/mainai-capability/local
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Reused deliberately: the F01-F24 matrix already owns the one JUnit -> status interpretation,
# so capability status and workstream status can never drift apart in meaning.
from program_matrix import _junit  # noqa: E402

LOCAL = "local"
ALPHA_MERGE = "alpha_merge"
NEEDS_REAL_OLLAMA = "needs_real_ollama"
NEEDS_VPS = "needs_vps"

TIER_ORDER = {NEEDS_VPS: 4, NEEDS_REAL_OLLAMA: 3, ALPHA_MERGE: 2, LOCAL: 1}
STATUS_ORDER = {"PASS": 1, "NOT_RUN": 2, "BLOCKED": 3, "FAIL": 4}

# --------------------------------------------------------------------------------------------
# The ten capabilities, each mapped to suites that already exist in this repository.
#
# `modules` are pytest module paths (a trailing "/" means every module below that directory).
# `tier` is the environment the module really needs — never lowered to make a report greener.
# `gap` records what this repository does NOT prove today. A gap is reported, never smoothed
# over: the acceptance package exists to show what is missing, not to hide it.
# --------------------------------------------------------------------------------------------
CAPABILITIES: tuple[dict[str, Any], ...] = (
    {
        "id": "A01",
        "title": "Vanliga samtal utan kunskapsbibliotek",
        "requirement": (
            "Ett samtal i en arbetsyta utan dokument ska besvaras, sparas med riktig "
            "provider/modell-proveniens och rapportera att inget kunskapsunderlag användes — "
            "aldrig påstå sig vara grundat i källor som inte finns."
        ),
        "workstreams": ("F04", "F05", "F06"),
        "modules": (
            ("tests/backend/chat/test_chat_context_status.py", LOCAL),
            ("tests/backend/chat/test_chat_message_persistence.py", LOCAL),
            ("tests/backend/chat/test_message_out_contract.py", LOCAL),
            ("tests/backend/chat/test_chat_source_grounding.py", LOCAL),
            ("tests/backend/chat/test_chat_failure_recovery.py", LOCAL),
        ),
        "gap": None,
    },
    {
        "id": "A02",
        "title": "Beständigt minne mellan samtal och omstarter",
        "requirement": (
            "Ett projekt och dess samtal ska fortsätta i nästa session och efter en riktig "
            "OS-processomstart, med samma varaktiga tillstånd och utan dubbellagring."
        ),
        "workstreams": ("F06", "F10", "F15"),
        "modules": (
            ("tests/backend/chat/test_project_session_resume.py", LOCAL),
            ("tests/backend/mainai/test_founder_memory_foundation.py", LOCAL),
            ("tests/backend/mainai/test_inspectable_memory_foundation.py", LOCAL),
            ("tests/backend/mainai/test_founder_memory_prompt.py", LOCAL),
            ("tests/backend/chat/test_project_session_real_model.py", NEEDS_REAL_OLLAMA),
            ("tests/backend/chat/test_project_session_os_restart.py", NEEDS_REAL_OLLAMA),
        ),
        "gap": None,
    },
    {
        "id": "A03",
        "title": "Projektkontext och projektstatus",
        "requirement": (
            "Frågor om ett projekt ska besvaras ur projektets eget varaktiga tillstånd, och "
            "projektstatus ska byggas från register — inte från modellens intryck."
        ),
        "workstreams": ("F04", "F06"),
        "modules": (
            ("tests/backend/test_projects_detail.py", LOCAL),
            ("tests/backend/mainai/test_project_entities.py", LOCAL),
            ("tests/backend/mainai/test_project_entity_work_candidate_capture.py", LOCAL),
            ("tests/backend/mainai/test_founder_long_horizon_workflow.py", LOCAL),
        ),
        "gap": None,
    },
    {
        "id": "A04",
        "title": "Kontextkoppling mellan dokument, beslut och uppgifter",
        "requirement": (
            "Ett dokument, ett beslut och en uppgift ska kunna kopplas ihop och läsas tillbaka "
            "med källidentitet; motsägelser ska visas som ostridiga/ostridiga, inte tystas."
        ),
        "workstreams": ("F07", "F09", "F10"),
        "modules": (
            ("tests/backend/mainai/test_context_atlas_decision_support.py", LOCAL),
            ("tests/backend/mainai/test_memory_work_linkage.py", LOCAL),
            ("tests/backend/test_context_atlas_capability_map.py", LOCAL),
            ("tests/backend/chat/test_upload_to_grounded_answer.py", LOCAL),
            ("tests/founder_acceptance/test_founder_acceptance_suite.py", LOCAL),
        ),
        "gap": None,
    },
    {
        "id": "A05",
        "title": "Målsättning → plan → godkännande → tillåten åtgärd",
        "requirement": (
            "Ett mål ska bli en plan, planen får inte dispatcha utan ett verkligt "
            "godkännandeevenemang, och först därefter får exakt ett tillåtet jobb uppstå."
        ),
        "workstreams": ("F15", "F16"),
        "modules": (
            ("tests/backend/mainai/test_founder_long_horizon_workflow.py", LOCAL),
            ("tests/backend/mainai/test_workforce_task_approval_enforcement.py", LOCAL),
            ("tests/backend/mainai/test_plan_scope_narrowing.py", LOCAL),
            ("tests/backend/mainai/test_execution_envelopes.py", LOCAL),
            ("tests/backend/test_mainai_execution_planner.py", LOCAL),
            ("tests/backend/mainai/test_goal_intake_path_a_worker_live.py", LOCAL),
        ),
        "gap": None,
    },
    {
        "id": "A06",
        "title": "Agentstatus: ARBETAR / BLOCKERAD / KLAR",
        "requirement": (
            "Grundaren ska kunna se vad varje agent gör just nu i en deterministisk vy, där "
            "blockerad och väntande aldrig presenteras som pågående arbete och KLAR bara när "
            "verifiering factiskt passerat."
        ),
        "workstreams": ("F15", "F16"),
        "modules": (
            ("tests/backend/mainai/test_agent_runtime_control_plane.py", LOCAL),
            ("tests/backend/mainai/test_agent_execution_control.py", LOCAL),
            ("tests/backend/mainai/test_founder_behavior_requirements.py", LOCAL),
            ("tests/backend/mainai/test_mainai_executive_loop.py", LOCAL),
        ),
        # Verified by inspection of this tree, not assumed: app/agent_coordination/runtime_view.py
        # defines RuntimeStatus RUNNING/BLOCKED/COMPLETED from WorkAssignmentStatus, but no router
        # references it (no `@router.` endpoint returns the snapshot). The Swedish labels therefore
        # have no API surface for the frontend to render today.
        "gap": (
            "RuntimeStatus (RUNNING/BLOCKERAD/KLAR) finns som ren läsmodell i "
            "app/agent_coordination/runtime_view.py men exponeras inte via någon HTTP-route; "
            "Claude kan inte rendera ARBETAR/BLOCKERAD/KLAR förrän en endpoint finns. "
            "Att lägga till den är en produktändring utanför testuppdraget — dokumenterad, ej gjord."
        ),
    },
    {
        "id": "A07",
        "title": "Ingen oavsiktlig dubbelkörning eller uppdragsfördelning",
        "requirement": (
            "Samma logiska meddelande, uppdrag eller uppgift får aldrig ge två providersanrop, "
            "två jobb eller två agenttilldelningar — även vid krasch, retry eller samtidighet."
        ),
        "workstreams": ("F14", "F15"),
        "modules": (
            ("tests/backend/chat/test_chat_request_idempotency.py", LOCAL),
            ("tests/backend/mainai/test_cancel_vs_finalize_race.py", LOCAL),
            ("tests/backend/mainai/test_supervisor_goal_lease.py", LOCAL),
            ("tests/backend/mainai/test_multi_agent_work_coordination.py", LOCAL),
            ("tests/backend/mainai/test_agent_dispatch_authorization_boundary.py", LOCAL),
            ("tests/backend/test_mainai_execution_recovery_dedup.py", LOCAL),
        ),
        "gap": None,
    },
    {
        "id": "A08",
        "title": "Korrekt ägarisolering och behörighetskontroll",
        "requirement": (
            "En grundare får aldrig läsa eller påverka en annans data, och den begränsade "
            "app-rollen får inte truncate:a oföränderligt minne eller migrationsbokföring."
        ),
        "workstreams": ("F03", "F10"),
        "modules": (
            ("tests/security/", LOCAL),
            ("tests/security_r15_egress/", LOCAL),
            ("tests/security_r15_successor/", LOCAL),
            ("tests/backend/chat/test_messages_rls.py", LOCAL),
            ("tests/backend/mainai/test_mainai_executive_security.py", LOCAL),
            ("tests/backend/test_runtime_table_privileges.py", LOCAL),
            ("tests/backend/test_privilege_boot_race_hotfix.py", LOCAL),
            ("tests/backend/test_source_foundation_bootstrap_privileges.py", LOCAL),
            # Verified with `git ls-tree b313c9df`: this suite exists ONLY on the Alpha side,
            # which is why the handoff's verification command fails on this branch today.
            ("tests/backend/test_migration_state_privileges.py", ALPHA_MERGE),
        ),
        "gap": None,
    },
    {
        "id": "A09",
        "title": "Återhämtning efter avbruten körning",
        "requirement": (
            "En worker som dör mitt i arbetet får bara tas över av återhämtningspipelinen, den "
            "döda workerns leaseförnyelse ska vara fenced, och exakt ett levande jobb återstår."
        ),
        "workstreams": ("F13", "F15", "F18"),
        "modules": (
            ("tests/backend/test_mainai_execution_recovery.py", LOCAL),
            ("tests/backend/test_mainai_execution_recovery_takeover.py", LOCAL),
            ("tests/backend/test_mainai_execution_auto_recovery.py", LOCAL),
            ("tests/backend/mainai/test_worker_live_lease_expiry_takeover.py", LOCAL),
            ("tests/backend/mainai/test_recovery_takeover_authority_fencing.py", LOCAL),
            ("tests/backend/mainai/test_founder_long_horizon_workflow.py", LOCAL),
            ("tests/founder_acceptance/test_founder_acceptance_suite.py", LOCAL),
        ),
        "gap": None,
    },
    {
        "id": "A10",
        "title": "Korrekta kapacitetspåståenden",
        "requirement": (
            "Modellen får aldrig presentera påhittad identitet, minnesfunktion eller "
            "verktygsåtkomst som verifierad; en overifierad probe är aldrig 'tillgänglig'."
        ),
        "workstreams": ("F04", "F05"),
        "modules": (
            ("tests/backend/mainai/test_capability_truth.py", LOCAL),
            ("tests/backend/mainai/test_tool_capability_truth.py", LOCAL),
            ("tests/backend/mainai/test_capability_reality_foundation.py", LOCAL),
            ("tests/backend/mainai/test_capability_record_active_context_linking.py", LOCAL),
            ("tests/backend/test_mainai_capability_unavailable_finalize.py", LOCAL),
            ("tests/backend/providers/test_model_capability_routing.py", LOCAL),
        ),
        # Verified by inspection: app/mainai_runtime_contract.py only rewrites unverified
        # *execution* claims (seven categories: background work, follow-up, monitoring,
        # reviewed, done, started, notify). Identity and memory claims have no equivalent rule,
        # so the only protection there is the system prompt plus provider/model provenance.
        "gap": (
            "Endast påståenden om pågående/avslutat ARBETE neutraliseras "
            "(app/mainai_runtime_contract.py). Påståenden om identitet (\"jag är …\") och om "
            "minnesfunktion (\"jag minns …\") har ingen motsvarande strukturell regel; de vilar "
            "på systemprompt + provider/modell-proveniens. Dokumenterad lucka, ej åtgärdad här."
        ),
    },
)


# --------------------------------------------------------------------------------------------
# Matrix helpers
# --------------------------------------------------------------------------------------------
def _normalize(path: str) -> str:
    """JUnit classnames are dotted module paths without the file suffix
    (`tests.backend.chat.test_chat_context_status`), so a mapped module path is compared as a
    prefix only after dropping `.py` — otherwise no file-level module would ever match."""
    normalized = path.replace("\\", "/")
    normalized = normalized.split("::", 1)[0]
    if normalized.endswith(".py"):
        normalized = normalized[: -len(".py")]
    else:
        normalized = normalized.replace(".", "/")
    return normalized


def _modules_for(capability: dict[str, Any], tier: str) -> list[str]:
    return [module for module, module_tier in capability["modules"] if module_tier == tier]


def _tiers_for(capability: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for _module, tier in capability["modules"]:
        if tier not in seen:
            seen.append(tier)
    return sorted(seen, key=lambda value: TIER_ORDER[value])


def validate_matrix() -> list[str]:
    """Fail closed before any test runs: every referenced module must exist on disk."""
    problems: list[str] = []
    seen_ids: set[str] = set()
    for capability in CAPABILITIES:
        if capability["id"] in seen_ids:
            problems.append(f"duplicate capability id {capability['id']}")
        seen_ids.add(capability["id"])
        if not capability["modules"]:
            problems.append(f"{capability['id']} maps no module")
        for module, tier in capability["modules"]:
            if tier not in TIER_ORDER:
                problems.append(f"{capability['id']}: unknown tier {tier!r} for {module}")
            target = BACKEND_ROOT / module.rstrip("/")
            # An alpha_merge suite legitimately does not exist before Codex merges Founder
            # Alpha; its leg reports NOT_RUN instead of pretending the module is missing.
            if not target.exists() and tier != ALPHA_MERGE:
                problems.append(f"{capability['id']}: missing {module}")
    return problems


def _leg_status(tests: list[dict], modules: list[str]) -> tuple[str, dict[str, Any]]:
    matched: dict[tuple[str, str], dict] = {}
    missing: list[str] = []
    per_module: dict[str, dict[str, Any]] = {}
    for module in modules:
        needle = _normalize(module.rstrip("/"))
        selected = [test for test in tests if _normalize(test["classname"]).startswith(needle)]
        if not selected:
            missing.append(module)
            per_module[module] = {"tests": 0, "passed": 0, "failed": 0, "skipped": 0}
            continue
        failed = sum(1 for test in selected if test["failed"])
        skipped = sum(1 for test in selected if test["skipped"])
        per_module[module] = {
            "tests": len(selected),
            "passed": len(selected) - failed - skipped,
            "failed": failed,
            "skipped": skipped,
        }
        # Keyed by identity: a testcase may appear more than once in a merged/session JUnit
        # (repeat runs, teardown phases); counting it twice would misstate the evidence.
        for test in selected:
            matched[(test["classname"], test["name"])] = test

    stats = {
        "tests": len(matched),
        "failed": sum(1 for test in matched.values() if test["failed"]),
        "skipped": sum(1 for test in matched.values() if test["skipped"]),
        "modules": per_module,
        "missing_modules": missing,
        "failed_tests": [
            f"{test['classname']}::{test['name']}" for test in matched.values() if test["failed"]
        ],
    }
    if missing:
        return "BLOCKED", stats
    if stats["failed"]:
        return "FAIL", stats
    if stats["tests"] and stats["skipped"] == stats["tests"]:
        return "NOT_RUN", stats
    if stats["tests"] - stats["skipped"] > 0:
        return "PASS", stats
    return "BLOCKED", stats


def _worst(statuses: list[str]) -> str:
    if not statuses:
        return "BLOCKED"
    return max(statuses, key=lambda value: STATUS_ORDER[value])


# --------------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------------
def _guard_url(label: str, url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    database = (parsed.path or "").lstrip("/")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit(f"refusing non-loopback {label}: {host!r}")
    if not any(token in database.lower() for token in ("test", "staging", "pytest")):
        raise SystemExit(
            f"refusing {label}={database!r}: pytest's conftest drops and recreates that database, "
            "so its name must contain test/staging/pytest"
        )
    return url


def _ollama_models(base_url: str) -> set[str]:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/tags", timeout=3.0) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return set()
    names: set[str] = set()
    for entry in data.get("models") or []:
        if isinstance(entry, dict):
            for key in ("name", "model"):
                value = entry.get(key)
                if isinstance(value, str) and value:
                    names.add(value)
    return names


def _run_group(
    *,
    name: str,
    modules: list[str],
    junit_path: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        *modules,
        "-q",
        "-p",
        "no:cacheprovider",
        f"--junitxml={junit_path}",
    ]
    print(f"+ [{name}] {' '.join(modules)}", flush=True)
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=BACKEND_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
        returncode = result.returncode
        tail = (result.stdout or "")[-4000:]
        errors = (result.stderr or "")[-2000:]
    except subprocess.TimeoutExpired:
        returncode = -9
        tail = f"timed out after {timeout_seconds}s"
        errors = ""
    elapsed = round(time.monotonic() - started, 1)
    if tail:
        print(tail, flush=True)
    if errors:
        print(errors, file=sys.stderr, flush=True)
    return {
        "group": name,
        "modules": modules,
        "exit_code": returncode,
        "elapsed_seconds": elapsed,
        "junit": str(junit_path),
        "stdout_tail": tail if returncode != 0 else "",
        "stderr_tail": errors if returncode != 0 else "",
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    problems = validate_matrix()
    if problems:
        raise SystemExit("capability matrix is invalid:\n  " + "\n  ".join(problems))

    selected = [
        capability
        for capability in CAPABILITIES
        if not args.only or capability["id"] in args.only
    ]
    if not selected:
        raise SystemExit("--only matched no capability")

    enabled_tiers = {LOCAL, ALPHA_MERGE}
    ollama_models = _ollama_models(args.ollama_url)
    chat_model = args.chat_model
    if args.allow_real_ollama and chat_model in ollama_models:
        enabled_tiers.add(NEEDS_REAL_OLLAMA)
        ollama_reason = f"real Ollama reachable at {args.ollama_url} with {chat_model}"
    elif args.allow_real_ollama:
        ollama_reason = (
            f"real Ollama reachable at {args.ollama_url} but {chat_model} is not installed "
            f"(installed: {sorted(ollama_models) or 'none'})"
        )
    else:
        ollama_reason = "--allow-real-ollama not given"
    # needs_vps is never enabled: this runner must not claim 16 GB VPS behaviour.

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "PYTHONPATH": ".",
        # Some real-suite tests shell out to alembic; running pytest through a venv interpreter
        # must give those subprocesses the same bin directory, not whatever PATH happened to be.
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "DATABASE_URL": _guard_url("database-url", args.database_url),
        "APP_DATABASE_URL": _guard_url("app-database-url", args.app_database_url),
        "LIFEAI_TEST_DATABASE_NAME": urlparse(args.database_url).path.lstrip("/"),
        "REDIS_URL": args.redis_url,
        "OLLAMA_BASE_URL": args.ollama_url,
        "OLLAMA_CHAT_MODEL": chat_model,
    }
    for key in ("OPENAI_API_KEY",):
        env.setdefault(key, "fake-key-for-tests")

    groups: dict[str, list[str]] = {}
    for capability in selected:
        for tier in _tiers_for(capability):
            modules = _modules_for(capability, tier)
            if tier == ALPHA_MERGE:
                modules = [module for module in modules if (BACKEND_ROOT / module).exists()]
            if not modules:
                continue
            bucket = f"{tier}"
            existing = groups.setdefault(bucket, [])
            for module in modules:
                if module not in existing:
                    existing.append(module)

    runs: dict[str, dict[str, Any]] = {}
    for tier, modules in groups.items():
        if tier not in enabled_tiers:
            continue
        runs[tier] = _run_group(
            name=tier,
            modules=modules,
            junit_path=out_dir / f"junit-{tier}.xml",
            env=env,
            timeout_seconds=args.timeout_seconds,
        )

    tests_by_tier: dict[str, list[dict]] = {}
    for tier in runs:
        junit = Path(runs[tier]["junit"])
        tests_by_tier[tier] = _junit(str(junit)) if junit.exists() else []

    entries: list[dict[str, Any]] = []
    for capability in selected:
        legs: list[dict[str, Any]] = []
        for tier in _tiers_for(capability):
            modules = _modules_for(capability, tier)
            if not modules:
                continue
            if tier == ALPHA_MERGE:
                modules = [module for module in modules if (BACKEND_ROOT / module).exists()]
                if not modules:
                    legs.append(
                        {
                            "tier": tier,
                            "status": "NOT_RUN",
                            "note": (
                                "Alpha-only suite, absent from this tree until Codex merges "
                                "Founder Alpha; it lights up automatically afterwards"
                            ),
                            "tests": 0,
                            "failed": 0,
                            "skipped": 0,
                            "modules": _modules_for(capability, tier),
                            "missing_modules": [],
                            "failed_tests": [],
                        }
                    )
                    continue
            if tier not in enabled_tiers:
                status = "NOT_RUN"
                stats: dict[str, Any] = {
                    "tests": 0,
                    "failed": 0,
                    "skipped": 0,
                    "modules": {module: {"tests": 0, "passed": 0, "failed": 0, "skipped": 0} for module in modules},
                    "missing_modules": [],
                    "failed_tests": [],
                }
                note = f"tier {tier} not enabled: {ollama_reason if tier == NEEDS_REAL_OLLAMA else 'never claimed locally'}"
            elif tier not in runs:
                status, stats = "BLOCKED", {
                    "tests": 0,
                    "failed": 0,
                    "skipped": 0,
                    "modules": {module: {"tests": 0, "passed": 0, "failed": 0, "skipped": 0} for module in modules},
                    "missing_modules": modules,
                    "failed_tests": [],
                }
                note = f"tier {tier} produced no JUnit evidence"
            else:
                status, stats = _leg_status(tests_by_tier.get(tier, []), modules)
                note = {
                    "PASS": "every mapped module ran; no failures",
                    "FAIL": "a mapped test failed — see failed_tests",
                    "BLOCKED": "a mapped module produced no testcase (collection error/missing suite)",
                    "NOT_RUN": "every mapped test skipped itself",
                }[status]
            legs.append(
                {
                    "tier": tier,
                    "status": status,
                    "note": note,
                    "tests": stats["tests"],
                    "failed": stats["failed"],
                    "skipped": stats["skipped"],
                    "modules": modules,
                    "missing_modules": stats["missing_modules"],
                    "failed_tests": stats["failed_tests"],
                }
            )
        entries.append(
            {
                "id": capability["id"],
                "title": capability["title"],
                "requirement": capability["requirement"],
                "workstreams": list(capability["workstreams"]),
                "status": _worst([leg["status"] for leg in legs]),
                "local_status": next((leg["status"] for leg in legs if leg["tier"] == LOCAL), None),
                "alpha_merge_status": next(
                    (leg["status"] for leg in legs if leg["tier"] == ALPHA_MERGE), None
                ),
                "real_ollama_status": next(
                    (leg["status"] for leg in legs if leg["tier"] == NEEDS_REAL_OLLAMA), None
                ),
                "gap": capability["gap"],
                "legs": legs,
            }
        )

    statuses = [entry["status"] for entry in entries]
    overall = _worst(statuses)
    return {
        "status": overall,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit": _git_sha(),
        "branch": _git_branch(),
        "environment": {
            "database_url": _redacted(args.database_url),
            "app_database_url": _redacted(args.app_database_url),
            "redis_url": _redacted(args.redis_url),
            "ollama_url": args.ollama_url,
            "chat_model": chat_model,
            "real_ollama_enabled": NEEDS_REAL_OLLAMA in enabled_tiers,
            "real_ollama_reason": ollama_reason,
            "vps_enabled": False,
        },
        "runs": runs,
        "capabilities": entries,
        "summary": {
            "PASS": statuses.count("PASS"),
            "FAIL": statuses.count("FAIL"),
            "BLOCKED": statuses.count("BLOCKED"),
            "NOT_RUN": statuses.count("NOT_RUN"),
            "capabilities": len(entries),
        },
    }


def _redacted(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.username or ''}@{parsed.hostname or ''}:{parsed.port or 0}{parsed.path or ''}"


def _git(*arguments: str) -> str:
    try:
        return subprocess.run(
            ["git", *arguments], cwd=BACKEND_ROOT, text=True, capture_output=True, check=False
        ).stdout.strip()
    except FileNotFoundError:
        return ""


def _git_sha() -> str:
    return _git("rev-parse", "HEAD")


def _git_branch() -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD")


# --------------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------------
def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# MainAI capability acceptance")
    lines.append("")
    lines.append(f"- **Overall:** `{report['status']}`")
    lines.append(f"- **Commit:** `{report['commit']}` ({report['branch']})")
    lines.append(f"- **Generated:** {report['generated_at']}")
    env = report["environment"]
    lines.append(f"- **Real Ollama enabled:** {env['real_ollama_enabled']} — {env['real_ollama_reason']}")
    lines.append("- **VPS tier:** not claimed locally")
    lines.append("")
    lines.append(
        "| id | capability | status | local | alpha-merge | real Ollama | tests | failed | skipped |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for entry in report["capabilities"]:
        tests = sum(leg["tests"] for leg in entry["legs"])
        failed = sum(leg["failed"] for leg in entry["legs"])
        skipped = sum(leg["skipped"] for leg in entry["legs"])
        lines.append(
            f"| {entry['id']} | {entry['title']} | `{entry['status']}` | "
            f"{entry['local_status'] or '—'} | {entry['alpha_merge_status'] or '—'} | "
            f"{entry['real_ollama_status'] or '—'} | "
            f"{tests} | {failed} | {skipped} |"
        )
    lines.append("")
    lines.append(f"Totals: {report['summary']}")
    lines.append("")

    failures = [
        (entry["id"], leg)
        for entry in report["capabilities"]
        for leg in entry["legs"]
        if leg["status"] in {"FAIL", "BLOCKED"}
    ]
    if failures:
        lines.append("## Failures and missing evidence")
        lines.append("")
        for capability_id, leg in failures:
            lines.append(f"- **{capability_id}** `{leg['tier']}` → `{leg['status']}`: {leg['note']}")
            for name in leg["failed_tests"][:10]:
                lines.append(f"  - failed: `{name}`")
            for module in leg["missing_modules"]:
                lines.append(f"  - no testcase from: `{module}`")
        lines.append("")

    gaps = [entry for entry in report["capabilities"] if entry["gap"]]
    lines.append("## Documented gaps (reported, not hidden)")
    lines.append("")
    if not gaps:
        lines.append("None in this selection.")
    for entry in gaps:
        lines.append(f"- **{entry['id']} {entry['title']}** — {entry['gap']}")
    lines.append("")
    return "\n".join(lines)


def render_list() -> str:
    lines: list[str] = []
    lines.append("| id | capability | tier(s) | modules | workstreams |")
    lines.append("|---|---|---|---|---|")
    for capability in CAPABILITIES:
        tiers = ", ".join(_tiers_for(capability))
        modules = len(capability["modules"])
        lines.append(
            f"| {capability['id']} | {capability['title']} | {tiers} | {modules} | "
            f"{', '.join(capability['workstreams'])} |"
        )
    lines.append("")
    lines.append("Gaps:")
    for capability in CAPABILITIES:
        if capability["gap"]:
            lines.append(f"- {capability['id']}: {capability['gap']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="print the capability matrix and exit")
    parser.add_argument("--only", default="", help="comma-separated capability ids (A01,A06)")
    parser.add_argument("--out-dir", default="", help="evidence directory (required unless --list)")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="disposable test database (loopback, name contains test/staging/pytest)",
    )
    parser.add_argument(
        "--app-database-url",
        default=os.environ.get("APP_DATABASE_URL", ""),
        help="restricted app role on the same disposable database",
    )
    parser.add_argument(
        "--redis-url", default=os.environ.get("REDIS_URL", "redis://127.0.0.1:56379/11")
    )
    parser.add_argument("--ollama-url", default=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    parser.add_argument("--chat-model", default=os.environ.get("OLLAMA_CHAT_MODEL", "qwen2.5:0.5b"))
    parser.add_argument(
        "--allow-real-ollama",
        action="store_true",
        help="enable the needs_real_ollama tier (only if the pinned model is really installed)",
    )
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    args = parser.parse_args()

    if args.list:
        print(render_list())
        return 0

    if not args.out_dir:
        parser.error("--out-dir is required unless --list")
    if not args.database_url or not args.app_database_url:
        parser.error("--database-url and --app-database-url are required unless --list")
    args.only = {value.strip() for value in args.only.split(",") if value.strip()}

    report = build_report(args)
    out_dir = Path(args.out_dir).resolve()
    json_path = out_dir / "mainai-capability-acceptance.json"
    md_path = out_dir / "mainai-capability-acceptance.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(render_markdown(report))
    print(f"\nwrote {json_path}\nwrote {md_path}")
    return 0 if report["status"] == "PASS" else (2 if report["status"] == "FAIL" else 3)


if __name__ == "__main__":
    raise SystemExit(main())
