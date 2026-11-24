"""Static architecture-consistency findings (B3).

This is deliberately a *detector*, not a repairer. It reports contradictions with evidence and
the smallest review action; it never deletes, rewrites or auto-merges a component. A finding
is a lead for a human reviewer, not an authorization to change code.

Checks that are deterministic from one checkout:

- two routers exposing the same method + full path,
- a router included in `app/main.py` whose file is missing,
- two Alembic migrations sharing the same numeric prefix,
- two ORM models declaring the same table name,
- an endpoint whose router module is absent from the scanned system map.

Documentation-vs-code claims and "deprecated implementation still referenced" need a
reviewed source list and are intentionally not guessed here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from app.context_atlas.system_map import Endpoint, SystemMap

_INCLUDE_RE = re.compile(r"app\.include_router\(\s*(\w+)\.router\s*\)")
_TABLE_RE = re.compile(r"__tablename__\s*=\s*[\"']([^\"']+)[\"']")


class Severity(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


@dataclass(frozen=True)
class ConsistencyFinding:
    code: str
    severity: Severity
    summary: str
    evidence: tuple[str, ...]
    review_action: str
    execution_authorized: bool = False

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "review_action": self.review_action,
            "execution_authorized": False,
        }


@dataclass(frozen=True)
class ConsistencyReport:
    findings: tuple[ConsistencyFinding, ...]
    counts: dict[str, int]
    execution_authorized: bool = False

    def to_dict(self) -> dict:
        return {
            "counts": dict(sorted(self.counts.items())),
            "findings": [finding.to_dict() for finding in self.findings],
            "execution_authorized": False,
        }


def _duplicate_endpoints(
    endpoints: tuple[Endpoint, ...],
) -> tuple[ConsistencyFinding, ...]:
    grouped: dict[tuple[str, str], list[Endpoint]] = {}
    for endpoint in endpoints:
        grouped.setdefault((endpoint.method, endpoint.path), []).append(endpoint)

    findings: list[ConsistencyFinding] = []
    for (method, path), matches in sorted(grouped.items()):
        routers = sorted({endpoint.router for endpoint in matches})
        if len(routers) <= 1:
            continue
        findings.append(
            ConsistencyFinding(
                code="duplicate_endpoint",
                severity=Severity.high,
                summary=f"{method} {path} is exposed by more than one router",
                evidence=tuple(
                    f"{endpoint.router}: {endpoint.function} ({endpoint.evidence})"
                    for endpoint in matches
                ),
                review_action=(
                    "compare the handlers and their authorization; keep one source of truth "
                    "or document why both routes are intentional"
                ),
            )
        )
    return tuple(findings)


def _missing_router_files(
    repo_root: Path | None, system_map: SystemMap
) -> tuple[ConsistencyFinding, ...]:
    if repo_root is None:
        return ()
    main_py = repo_root / "backend" / "app" / "main.py"
    if not main_py.is_file():
        return ()
    text = main_py.read_text(encoding="utf-8", errors="ignore")
    findings: list[ConsistencyFinding] = []
    for match in _INCLUDE_RE.finditer(text):
        name = match.group(1)
        path = repo_root / "backend" / "app" / "routers" / f"{name}.py"
        if path.is_file():
            continue
        findings.append(
            ConsistencyFinding(
                code="missing_router_file",
                severity=Severity.critical,
                summary=f"app/main.py includes router '{name}' but its file is missing",
                evidence=(
                    f"backend/app/main.py:{text[: match.start()].count(chr(10)) + 1}",
                    f"expected backend/app/routers/{name}.py",
                ),
                review_action=(
                    "restore the router file or remove the include; do not assume the route works"
                ),
            )
        )
    return tuple(findings)


def _missing_endpoint_modules(
    system_map: SystemMap,
) -> tuple[ConsistencyFinding, ...]:
    findings: list[ConsistencyFinding] = []
    for endpoint in system_map.endpoints:
        module = f"app.routers.{endpoint.router}"
        if system_map.module(module) is not None:
            continue
        findings.append(
            ConsistencyFinding(
                code="endpoint_without_module",
                severity=Severity.medium,
                summary=f"{endpoint.method} {endpoint.path} has no scanned router module",
                evidence=(endpoint.evidence,),
                review_action="verify the router file and include path; the map may be stale",
            )
        )
    return tuple(findings)


def _duplicate_migration_prefixes(repo_root: Path | None) -> tuple[ConsistencyFinding, ...]:
    if repo_root is None:
        return ()
    versions = repo_root / "backend" / "alembic" / "versions"
    if not versions.is_dir():
        return ()
    grouped: dict[str, list[str]] = {}
    for path in sorted(versions.glob("*.py")):
        prefix = path.name[:4]
        if prefix.isdigit():
            grouped.setdefault(prefix, []).append(path.name)
    return tuple(
        ConsistencyFinding(
            code="duplicate_migration_prefix",
            severity=Severity.critical,
            summary=f"migration prefix {prefix} is used by more than one file",
            evidence=tuple(sorted(names)),
            review_action=(
                "determine the real Alembic head and composition order; never renumber a "
                "migration that may already be applied"
            ),
        )
        for prefix, names in sorted(grouped.items())
        if len(names) > 1
    )


def _duplicate_table_names(repo_root: Path | None) -> tuple[ConsistencyFinding, ...]:
    if repo_root is None:
        return ()
    models = repo_root / "backend" / "app" / "models"
    if not models.is_dir():
        return ()
    grouped: dict[str, list[str]] = {}
    for path in sorted(models.glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in _TABLE_RE.finditer(text):
            grouped.setdefault(match.group(1), []).append(
                f"{path.relative_to(repo_root)}:{text[: match.start()].count(chr(10)) + 1}"
            )
    return tuple(
        ConsistencyFinding(
            code="duplicate_table_name",
            severity=Severity.critical,
            summary=f"table name '{table}' is declared by more than one model",
            evidence=tuple(sorted(locations)),
            review_action=(
                "identify the canonical model and migration owner; do not drop either table "
                "without an explicit migration review"
            ),
        )
        for table, locations in sorted(grouped.items())
        if len(locations) > 1
    )


def check_consistency(
    system_map: SystemMap, *, repo_root: Path | None = None
) -> ConsistencyReport:
    """Run all deterministic checks and return review findings.

    `repo_root` enables filesystem checks (missing routers, migration prefixes, table names).
    Without it, only the in-memory system map is checked.
    """

    root = Path(repo_root).resolve() if repo_root is not None else None
    findings = (
        _duplicate_endpoints(system_map.endpoints)
        + _missing_router_files(root, system_map)
        + _missing_endpoint_modules(system_map)
        + _duplicate_migration_prefixes(root)
        + _duplicate_table_names(root)
    )
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity.value] = counts.get(finding.severity.value, 0) + 1
    return ConsistencyReport(findings=tuple(findings), counts=counts)
