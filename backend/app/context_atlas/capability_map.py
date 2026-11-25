"""Evidence-backed capability inventory (Workstream D).

Builds a map of what exists from verifiable repository evidence only: routers actually
included in app/main.py, modules/models on disk, Alembic migrations, and test files that
mention a component by name. It never claims a relationship is verified because names match:
a test-file mention is recorded as `name_reference_only`, and a router being included is
recorded as wiring, not as behavioural verification.

No database, network or git call: the result is a pure function of the checkout, so the same
commit always produces the same JSON.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_ROUTER_RE = re.compile(r"app\.include_router\(\s*(\w+)\.router\s*\)")
_MIGRATION_RE = re.compile(r"^(\d{4})_.*\.py$")


@dataclass(frozen=True)
class Capability:
    component: str
    kind: str  # router | module | model
    status: str  # connected | connected_with_test_reference | implemented_with_test_reference | implemented_unverified
    evidence: tuple[str, ...]
    tests: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True)
class CapabilityMap:
    counts: dict[str, int]
    migrations_head: str | None
    capabilities: tuple[Capability, ...]
    unmapped_relationships: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "counts": dict(sorted(self.counts.items())),
            "migrations_head": self.migrations_head,
            "unmapped_relationships": list(self.unmapped_relationships),
            "capabilities": [
                {
                    "component": c.component,
                    "kind": c.kind,
                    "status": c.status,
                    "evidence": list(c.evidence),
                    "tests": list(c.tests),
                    "note": c.note,
                }
                for c in self.capabilities
            ],
            "execution_authorized": False,
        }


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def scan_capabilities(repo_root: Path) -> CapabilityMap:
    repo_root = Path(repo_root).resolve()
    app_dir = repo_root / "backend" / "app"
    main_py = app_dir / "main.py"
    if not main_py.is_file():
        raise FileNotFoundError(f"not a LifeAI checkout: {main_py} is missing")

    main_text = _read_text(main_py)
    routers = {
        match.group(1): f"backend/app/main.py:{main_text[: match.start()].count(chr(10)) + 1}"
        for match in _ROUTER_RE.finditer(main_text)
    }

    test_files = sorted((repo_root / "backend" / "tests").rglob("*.py"))
    test_texts = [(p, _read_text(p)) for p in test_files]

    def test_refs(name: str) -> tuple[str, ...]:
        if len(name) < 3:
            return ()
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        hits = [
            str(path.relative_to(repo_root))
            for path, text in test_texts
            if pattern.search(text)
        ]
        return tuple(sorted(hits)[:5])

    capabilities: list[Capability] = []

    for name, evidence in sorted(routers.items()):
        tests = test_refs(name)
        capabilities.append(
            Capability(
                component=name,
                kind="router",
                status="connected_with_test_reference" if tests else "connected",
                evidence=(evidence,),
                tests=tests,
                note="router is wired in app/main.py; wiring is not behavioural verification",
            )
        )

    module_names = sorted(
        p.name
        for p in app_dir.iterdir()
        if p.is_dir() and (p / "__init__.py").is_file() and p.name != "__pycache__"
    )
    for name in module_names:
        if name in routers:
            continue
        tests = test_refs(name)
        capabilities.append(
            Capability(
                component=name,
                kind="module",
                status=(
                    "implemented_with_test_reference"
                    if tests
                    else "implemented_unverified"
                ),
                evidence=(f"backend/app/{name}/",),
                tests=tests,
                note="module exists on disk; not necessarily activated by any router",
            )
        )

    models_dir = app_dir / "models"
    if models_dir.is_dir():
        for path in sorted(models_dir.glob("*.py")):
            if path.name == "__init__.py":
                continue
            name = path.stem
            tests = test_refs(name)
            capabilities.append(
                Capability(
                    component=name,
                    kind="model",
                    status=(
                        "implemented_with_test_reference"
                        if tests
                        else "implemented_unverified"
                    ),
                    evidence=(str(path.relative_to(repo_root)),),
                    tests=tests,
                    note="model module; table existence is confirmed by migrations, not by this scan",
                )
            )

    migrations = sorted(
        (repo_root / "backend" / "alembic" / "versions").glob("*.py")
    )
    heads = [
        int(m.group(1))
        for m in (_MIGRATION_RE.match(p.name) for p in migrations)
        if m is not None
    ]
    migrations_head = f"{max(heads):04d}" if heads else None

    from app.context_atlas.relationships import unmapped_concepts

    return CapabilityMap(
        counts={
            "routers": len(routers),
            "modules": len(module_names),
            "models": len([c for c in capabilities if c.kind == "model"]),
            "migrations": len(migrations),
            "test_files": len(test_files),
        },
        migrations_head=migrations_head,
        capabilities=tuple(sorted(capabilities, key=lambda c: (c.kind, c.component))),
        unmapped_relationships=unmapped_concepts(),
    )
