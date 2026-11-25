"""Context Atlas acceptance — capability inventory from repository evidence."""

import json
from pathlib import Path

from app.context_atlas.capability_map import scan_capabilities

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_scan_reports_wired_routers_with_real_evidence_paths():
    capability_map = scan_capabilities(REPO_ROOT)
    by_name = {c.component: c for c in capability_map.capabilities}

    assert "health" in by_name
    assert by_name["health"].kind == "router"
    assert by_name["health"].status in (
        "connected",
        "connected_with_test_reference",
    )
    evidence = by_name["health"].evidence[0]
    assert evidence.startswith("backend/app/main.py:")
    line = int(evidence.rsplit(":", 1)[1])
    assert line > 0


def test_scan_reads_the_actual_migration_head():
    capability_map = scan_capabilities(REPO_ROOT)

    versions = sorted((REPO_ROOT / "backend" / "alembic" / "versions").glob("*.py"))
    expected = max(int(p.name[:4]) for p in versions if p.name[:4].isdigit())
    assert capability_map.migrations_head == f"{expected:04d}"
    assert capability_map.counts["migrations"] == len(versions)


def test_test_reference_is_recorded_as_name_reference_not_verification():
    capability_map = scan_capabilities(REPO_ROOT)
    by_name = {c.component: c for c in capability_map.capabilities}

    assert "rag" in by_name
    rag = by_name["rag"]
    # Whatever the test hits are, the note must not claim behavioural verification.
    assert "not necessarily activated" in rag.note or rag.kind != "module"
    assert all(path.startswith("backend/tests/") for path in rag.tests)


def test_scan_is_deterministic_and_json_serializable():
    first = scan_capabilities(REPO_ROOT).to_dict()
    second = scan_capabilities(REPO_ROOT).to_dict()

    assert first == second
    assert first["execution_authorized"] is False
    encoded = json.dumps(first)
    assert '"execution_authorized": false' in encoded
    assert first["unmapped_relationships"], "relationship gaps must stay visible"
