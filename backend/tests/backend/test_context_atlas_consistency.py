"""B3 acceptance: contradictions are reported with evidence, never auto-repaired."""

from __future__ import annotations

from pathlib import Path

from app.context_atlas.consistency import Severity, check_consistency
from app.context_atlas.system_map import Endpoint, Maturity, ModuleNode, SystemMap

REPO_ROOT = Path(__file__).resolve().parents[3]


def _map(
    *,
    endpoints: tuple[Endpoint, ...] = (),
    modules: tuple[ModuleNode, ...] = (),
) -> SystemMap:
    return SystemMap(
        counts={},
        migrations_head=None,
        modules=modules,
        endpoints=endpoints,
        workers=(),
        events=(),
        git=None,
    )


def _module(name: str) -> ModuleNode:
    return ModuleNode(
        module=name,
        path=f"backend/app/{name.replace('.', '/')}.py",
        imports=(),
        imported_by=(),
        endpoints=(),
        tests=(),
        maturity=Maturity(present=True, wired=True, test_referenced=True),
    )


def test_duplicate_endpoint_is_reported_with_both_routers():
    report = check_consistency(
        _map(
            endpoints=(
                Endpoint("GET", "/api/things", "a", "router_a", "founder", "a.py:1"),
                Endpoint("GET", "/api/things", "b", "router_b", "founder", "b.py:1"),
            )
        )
    )

    finding = next(f for f in report.findings if f.code == "duplicate_endpoint")
    assert finding.severity is Severity.high
    assert "router_a" in finding.evidence[0]
    assert "router_b" in finding.evidence[1]
    assert finding.execution_authorized is False
    assert "keep one source of truth" in finding.review_action


def test_missing_router_file_is_critical_and_not_assumed_working(tmp_path):
    main = tmp_path / "backend" / "app" / "main.py"
    main.parent.mkdir(parents=True)
    main.write_text("app.include_router(missing.router)\n", encoding="utf-8")

    report = check_consistency(_map(), repo_root=tmp_path)

    finding = next(f for f in report.findings if f.code == "missing_router_file")
    assert finding.severity is Severity.critical
    assert "missing.py" in finding.evidence[1]
    assert "do not assume the route works" in finding.review_action


def test_duplicate_migration_prefix_is_critical(tmp_path):
    versions = tmp_path / "backend" / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "0001_first.py").write_text("", encoding="utf-8")
    (versions / "0001_second.py").write_text("", encoding="utf-8")

    report = check_consistency(_map(), repo_root=tmp_path)

    finding = next(f for f in report.findings if f.code == "duplicate_migration_prefix")
    assert finding.severity is Severity.critical
    assert set(finding.evidence) == {"0001_first.py", "0001_second.py"}
    assert "never renumber" in finding.review_action


def test_duplicate_table_name_is_critical(tmp_path):
    models = tmp_path / "backend" / "app" / "models"
    models.mkdir(parents=True)
    (models / "one.py").write_text('__tablename__ = "same_table"\n', encoding="utf-8")
    (models / "two.py").write_text('__tablename__ = "same_table"\n', encoding="utf-8")

    report = check_consistency(_map(), repo_root=tmp_path)

    finding = next(f for f in report.findings if f.code == "duplicate_table_name")
    assert finding.severity is Severity.critical
    assert len(finding.evidence) == 2
    assert "do not drop either table" in finding.review_action


def test_real_checkout_report_is_read_only_and_every_finding_has_a_review_action():
    from app.context_atlas.system_map import scan_system_map

    report = check_consistency(scan_system_map(REPO_ROOT), repo_root=REPO_ROOT)
    payload = report.to_dict()

    assert payload["execution_authorized"] is False
    assert all(finding.review_action for finding in report.findings)
    assert all(finding.execution_authorized is False for finding in report.findings)
    assert "duplicate_migration_prefix" not in {f.code for f in report.findings}
    assert "duplicate_table_name" not in {f.code for f in report.findings}
