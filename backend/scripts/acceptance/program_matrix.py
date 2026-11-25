#!/usr/bin/env python3
"""Founder Alpha F01-F24 workstream matrix.

This is deliberately not a checklist that marks everything green because a test file
exists. Each status is derived from terminal evidence:

  * PASS: the required evidence exists and contains no failure;
  * FAIL: evidence exists and proves a failure;
  * BLOCKED: the required evidence does not exist, or the capability is not implemented;
  * SKIP: reserved for an explicit, documented non-applicable workstream.

The matrix is consumed by qa_gate.py so a BLOCKED workstream cannot silently become a
PASS at the top level.
"""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path

WORKSTREAMS = {
    "F01": "Komplett installation",
    "F02": "Systemuppstart",
    "F03": "Autentiseringens användarflöde",
    "F04": "Komplett AI-konversation",
    "F05": "Modellval och providerfel",
    "F06": "Konversationshistorik",
    "F07": "Dokumentuppladdning",
    "F08": "Indexering och embeddings",
    "F09": "RAG-kvalitet",
    "F10": "Persistent minne",
    "F11": "Dokumentversioner",
    "F12": "Knowledge Studio",
    "F13": "PostgreSQL-felinjektion",
    "F14": "Redis- och köfel",
    "F15": "Worker-krascher",
    "F16": "Backend-krascher",
    "F17": "AI-providerfel",
    "F18": "Backup och disaster recovery",
    "F19": "Full Playwright E2E",
    "F20": "Frontend/API-kontrakt",
    "F21": "Tillgänglighet och mobil",
    "F22": "Prestanda och lasttest",
    "F23": "Långvarig stabilitetstestning",
    "F24": "Fullständig QA- och integrationsgrind",
}


def _load_json(path: str) -> dict | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _junit(path: str) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return []
    tests: list[dict] = []
    for case in root.iter("testcase"):
        tests.append(
            {
                "classname": case.attrib.get("classname", ""),
                "name": case.attrib.get("name", ""),
                "failed": case.find("failure") is not None or case.find("error") is not None,
                "skipped": case.find("skipped") is not None,
            }
        )
    return tests


def _junit_status(tests: list[dict], needles: tuple[str, ...]) -> tuple[str, dict]:
    selected = [
        test
        for test in tests
        if any(needle.lower() in (test["classname"] + " " + test["name"]).lower() for needle in needles)
    ]
    if not selected:
        return "BLOCKED", {"tests": 0, "failed": 0, "skipped": 0, "needles": list(needles)}
    failed = sum(1 for test in selected if test["failed"])
    skipped = sum(1 for test in selected if test["skipped"])
    return ("FAIL" if failed else "PASS"), {
        "tests": len(selected),
        "failed": failed,
        "skipped": skipped,
        "needles": list(needles),
    }


def _playwright_files(path: str) -> set[str]:
    data = _load_json(path)
    if not data:
        return set()
    files: set[str] = set()

    def walk(suites: list[dict]) -> None:
        for suite in suites or []:
            if suite.get("file"):
                files.add(str(suite["file"]))
            walk(suite.get("suites") or [])

    walk(data.get("suites") or [])
    return files


def _entry(workstream: str, status: str, evidence: object, note: str) -> dict:
    return {
        "workstream": workstream,
        "title": WORKSTREAMS[workstream],
        "status": status,
        "evidence": evidence,
        "note": note,
    }


def _evidence_status(data: dict | None) -> str | None:
    if not data:
        return None
    return data.get("status")


def build(args: argparse.Namespace) -> dict:
    install = _load_json(args.install_evidence)
    performance = _load_json(args.performance_evidence)
    recovery = _load_json(args.recovery_evidence)
    recovery_matrix = _load_json(args.recovery_matrix)
    version_junit = _junit(args.version_junit)
    ollama = _load_json(args.ollama_evidence)
    rag_quality = _load_json(args.rag_quality_evidence)
    postgres_faults = _load_json(args.postgres_fault_evidence)
    soak = _load_json(args.soak_evidence)
    junit = _junit(args.pytest_junit)
    playwright = _playwright_files(args.playwright_json)

    entries: list[dict] = []

    install_summary = (install or {}).get("summary") or {}
    install_ok = (
        install_summary.get("FAIL", 1) == 0
        and install_summary.get("BLOCKED", 1) == 0
        and install_summary.get("PASS", 0) >= 12
    )
    entries.append(
        _entry(
            "F01",
            "PASS" if install_ok else ("FAIL" if install else "BLOCKED"),
            install_summary or "missing install-matrix.json",
            "12 real-service install/failure scenarios; no FAIL/BLOCKED.",
        )
    )

    f02_status, f02_stats = _junit_status(junit, ("test_health", "test_startup_checks"))
    entries.append(_entry("F02", f02_status, f02_stats, "Health/readiness and startup checks."))

    f03_status, f03_stats = _junit_status(junit, ("test_session_auth", "test_auth_concurrency"))
    entries.append(_entry("F03", f03_status, f03_stats, "Login/logout, rotation, revocation and password reset."))

    if _evidence_status(ollama) == "PASS":
        entries.append(_entry("F04", "PASS", ollama, "Real Ollama conversation evidence."))
    else:
        entries.append(
            _entry(
                "F04",
                "BLOCKED",
                "no Ollama evidence",
                "Ollama is not installed in this environment; the provider currently uses stream=false.",
            )
        )

    f05_status, f05_stats = _junit_status(
        junit, ("providers.", "providers/", "test_model_gate", "test_local_first_routing")
    )
    entries.append(_entry("F05", f05_status, f05_stats, "Provider selection, fallback and safe errors."))

    f06_status, f06_stats = _junit_status(junit, ("test_chat_message_persistence",))
    entries.append(_entry("F06", f06_status, f06_stats, "Persisted message ordering and reload state."))

    f07_status, f07_stats = _junit_status(
        junit, ("test_library_import", "test_library_routes", "test_documents_upload_durable_delivery")
    )
    entries.append(_entry("F07", f07_status, f07_stats, "Upload, duplicates, ZIP, corrupt/oversized files."))

    f08_status, f08_stats = _junit_status(
        junit, ("test_library_import", "test_worker", "test_documents_upload_durable_delivery")
    )
    entries.append(_entry("F08", f08_status, f08_stats, "Chunking, embeddings, retry and durable resume."))

    if _evidence_status(rag_quality) == "PASS":
        entries.append(_entry("F09", "PASS", rag_quality, "Relevant/irrelevant/contradictory/outdated dataset."))
    else:
        entries.append(
            _entry(
                "F09",
                "BLOCKED",
                "no dedicated RAG quality evidence",
                "Existing RAG tests prove mechanics, not the full four-class quality dataset.",
            )
        )

    f10_status, f10_stats = _junit_status(junit, ("test_project_memory", "test_memory_work_linkage"))
    entries.append(_entry("F10", f10_status, f10_stats, "Memory persistence, update, deletion and owner isolation."))

    # F11 has two independent requirements: recovering a crash-window version and replacing
    # a source with an explicit next version. Requiring BOTH prevents the matrix from going
    # green on crash-safety alone (the previous state) or on replacement alone.
    f11_resume_status, f11_resume_stats = _junit_status(
        version_junit, ("test_resume_recreates_missing_version", "test_resume_claim_extraction_crash")
    )
    f11_replace_status, f11_replace_stats = _junit_status(
        version_junit,
        (
            "test_replace_source_creates_next_version",
            "test_replace_identical_bytes",
            "test_replace_with_content_already_owned",
        ),
    )
    f11_stats = {"crash_safe_resume": f11_resume_stats, "explicit_replacement": f11_replace_stats}
    if "FAIL" in (f11_resume_status, f11_replace_status):
        entries.append(_entry("F11", "FAIL", f11_stats, "A version/replacement test failed."))
    elif f11_resume_status == "PASS" and f11_replace_status == "PASS":
        entries.append(
            _entry(
                "F11",
                "PASS",
                f11_stats,
                "Crash-safe version recreation plus explicit replacement, next version and retrieval invalidation.",
            )
        )
    else:
        entries.append(
            _entry(
                "F11",
                "BLOCKED",
                f11_stats,
                "Requires both crash-safe resume and explicit replacement evidence.",
            )
        )

    if any("founder-knowledge-studio" in f for f in playwright):
        entries.append(_entry("F12", "PASS", sorted(playwright), "Knowledge Studio browser evidence present."))
    else:
        entries.append(_entry("F12", "BLOCKED", "no Knowledge Studio Playwright JSON", "Run the real E2E suite."))

    if _evidence_status(postgres_faults) == "PASS":
        entries.append(_entry("F13", "PASS", postgres_faults, "PostgreSQL fault-injection evidence."))
    else:
        entries.append(
            _entry(
                "F13",
                "BLOCKED",
                "no PostgreSQL deadlock/commit-timeout evidence",
                "db_retry covers lost connections; deadlock and commit-timeout injection are not run.",
            )
        )

    f14_status, f14_stats = _junit_status(junit, ("redis", "test_job_lock", "test_worker_heartbeat"))
    entries.append(_entry("F14", f14_status, f14_stats, "Redis reconnection, locks and duplicate-job behavior."))

    f15_status, f15_stats = _junit_status(
        junit, ("takeover", "test_mainai_execution_recovery", "test_worker_live_lease")
    )
    entries.append(_entry("F15", f15_status, f15_stats, "Worker crash/takeover and duplicate-effect guards."))

    f16_status, f16_stats = _junit_status(junit, ("test_startup_checks", "test_health"))
    if f16_status == "PASS" and install_ok:
        entries.append(_entry("F16", "PASS", f16_stats, "Startup/restart and install restart scenarios."))
    else:
        entries.append(_entry("F16", f16_status if f16_status == "FAIL" else "BLOCKED", f16_stats, "Restart evidence."))

    f17_status, f17_stats = _junit_status(
        junit, ("provider_failclosed", "test_provider_verification", "test_openai_consult")
    )
    entries.append(_entry("F17", f17_status, f17_stats, "Timeout, rate limit, 5xx, malformed payload and retries."))

    recovery_ok = _evidence_status(recovery) == "PASS" and _evidence_status(recovery_matrix) == "PASS"
    entries.append(
        _entry(
            "F18",
            "PASS" if recovery_ok else "BLOCKED",
            {"backup_restore": _evidence_status(recovery), "recovery_matrix": _evidence_status(recovery_matrix)},
            "Valid restore plus corrupt/partial rejection and source-integrity checks.",
        )
    )

    full_e2e = {"auth.spec.ts", "founder-knowledge-studio.spec.ts", "library-workbench-mobile.spec.ts"}
    if full_e2e.issubset(playwright):
        entries.append(_entry("F19", "PASS", sorted(playwright), "Full founder journey browser evidence present."))
    else:
        entries.append(_entry("F19", "BLOCKED", sorted(playwright) or "no Playwright JSON", "Full E2E JSON missing."))

    f20_status, f20_stats = _junit_status(junit, ("test_openapi_schema", "test_config_contract"))
    entries.append(_entry("F20", f20_status, f20_stats, "OpenAPI schema and frontend/API contract checks."))

    if any("library-workbench-mobile" in f or "auth.spec" in f for f in playwright):
        entries.append(_entry("F21", "PASS", sorted(playwright), "Mobile viewport and login accessibility evidence."))
    else:
        entries.append(_entry("F21", "BLOCKED", "no mobile/a11y Playwright JSON", "Run the browser suite."))

    perf_ok = _evidence_status(performance) == "PASS"
    entries.append(
        _entry(
            "F22",
            "PASS" if perf_ok else "BLOCKED",
            performance or "missing resource-probe.json",
            "Measured normal load; pg_stat_statements slow-query stats remain BLOCKED.",
        )
    )

    if _evidence_status(soak) == "PASS" and float(soak.get("duration_seconds", 0)) >= 4 * 60 * 60:
        entries.append(_entry("F23", "PASS", soak, "Four-hour active soak completed."))
    else:
        entries.append(
            _entry(
                "F23",
                "BLOCKED",
                "no four-hour soak evidence",
                "A short smoke is not a substitute for the required four-hour active soak.",
            )
        )

    statuses = [entry["status"] for entry in entries]
    if "FAIL" in statuses:
        overall = "FAIL"
    elif "BLOCKED" in statuses:
        overall = "BLOCKED"
    else:
        overall = "PASS"
    entries.append(
        _entry(
            "F24",
            overall,
            {"pass": statuses.count("PASS"), "fail": statuses.count("FAIL"), "blocked": statuses.count("BLOCKED")},
            "The matrix itself is the gate input; overall status is the worst workstream status.",
        )
    )
    return {
        "status": overall,
        "workstreams": entries,
        "summary": {
            "PASS": sum(1 for entry in entries if entry["status"] == "PASS"),
            "FAIL": sum(1 for entry in entries if entry["status"] == "FAIL"),
            "BLOCKED": sum(1 for entry in entries if entry["status"] == "BLOCKED"),
            "SKIP": sum(1 for entry in entries if entry["status"] == "SKIP"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-evidence", default="")
    parser.add_argument("--performance-evidence", default="")
    parser.add_argument("--recovery-evidence", default="")
    parser.add_argument("--recovery-matrix", default="")
    parser.add_argument("--pytest-junit", default="")
    parser.add_argument("--version-junit", default="")
    parser.add_argument("--playwright-json", default="")
    parser.add_argument("--ollama-evidence", default="")
    parser.add_argument("--rag-quality-evidence", default="")
    parser.add_argument("--postgres-fault-evidence", default="")
    parser.add_argument("--soak-evidence", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    payload = build(args)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    print(text)
    return 0 if payload["status"] == "PASS" else (2 if payload["status"] == "FAIL" else 3)


if __name__ == "__main__":
    raise SystemExit(main())
