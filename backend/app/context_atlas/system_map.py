"""Self-discovering system map with explicit maturity states (Phase 2).

Static, deterministic analysis of one checkout. It answers "what exists, what is wired, what
is referenced by tests, and what is still unknown" — never "this works". The distinction is
the point:

    present      the file/class exists in the checkout
    wired        referenced from app/main.py or imported by another app module
    test_referenced  a test file names the component
    reviewed     UNKNOWN unless external review evidence is supplied
    integrated   UNKNOWN unless merge/base ancestry evidence is supplied
    activated    UNKNOWN unless runtime configuration evidence is supplied
    observed     UNKNOWN unless real runtime evidence is supplied

`None` means unknown, `True` means evidence exists, `False` is not used for the last four
because absence of evidence is not evidence of absence. Static parsing cannot observe a
request, a worker run or a deployment, so those states stay unknown by construction.

Also provides `change_impact()`: reverse import closure, endpoints, tests, model
dependencies, migration/security notes and the unknowns a reviewer must check manually.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.context_atlas.git_evidence import GitEvidence

_INCLUDE_RE = re.compile(r"app\.include_router\(\s*(\w+)\.router\s*\)")
_PREFIX_RE = re.compile(r"APIRouter\([^)]*prefix\s*=\s*[\"']([^\"']*)[\"']", re.S)
_ENDPOINT_RE = re.compile(
    r"^@router\.(get|post|put|patch|delete)\(\s*[\"']([^\"']*)[\"']", re.M
)
_FROM_RE = re.compile(r"^\s*from\s+(app(?:\.[\w]+)+)\s+import\s+([^\n#]+)", re.M)
_IMPORT_RE = re.compile(r"^\s*import\s+(app(?:\.[\w]+)+)", re.M)
_JOB_RE = re.compile(r"^def\s+(run_\w+|_?poll\w+|_?reconcile\w+)\(", re.M)
_SCHEDULE_RE = re.compile(r"add_job\(\s*([^,\n)]+)", re.M)
_EVENT_CLASS_RE = re.compile(r"^class\s+(\w*Event\w*)\(Base\):", re.M)
_TABLE_RE = re.compile(r"__tablename__\s*=\s*[\"']([^\"']+)[\"']")

MAX_IMPACT_DEPENDENTS = 50


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    function: str
    router: str
    auth: str  # founder | user | none | unknown
    evidence: str

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "path": self.path,
            "function": self.function,
            "router": self.router,
            "auth": self.auth,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class Maturity:
    present: bool
    wired: bool
    test_referenced: bool
    reviewed: bool | None = None
    integrated: bool | None = None
    activated: bool | None = None
    observed: bool | None = None

    def to_dict(self) -> dict:
        return {
            "present": self.present,
            "wired": self.wired,
            "test_referenced": self.test_referenced,
            "reviewed": self.reviewed,
            "integrated": self.integrated,
            "activated": self.activated,
            "observed": self.observed,
        }


@dataclass(frozen=True)
class ModuleNode:
    module: str
    path: str
    imports: tuple[str, ...]
    imported_by: tuple[str, ...]
    endpoints: tuple[str, ...]
    tests: tuple[str, ...]
    maturity: Maturity

    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "path": self.path,
            "imports": list(self.imports),
            "imported_by": list(self.imported_by),
            "endpoints": list(self.endpoints),
            "tests": list(self.tests),
            "maturity": self.maturity.to_dict(),
        }


@dataclass(frozen=True)
class Worker:
    name: str
    kind: str  # scheduled_job | worker_loop | recovery_hook
    evidence: str

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "evidence": self.evidence}


@dataclass(frozen=True)
class EventStream:
    model: str
    table: str
    import_references: tuple[str, ...]
    note: str

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "table": self.table,
            "import_references": list(self.import_references),
            "note": self.note,
        }


@dataclass(frozen=True)
class ChangeImpact:
    module: str
    direct_dependents: tuple[str, ...]
    transitive_dependents: tuple[str, ...]
    endpoints: tuple[str, ...]
    tests: tuple[str, ...]
    model_dependencies: tuple[str, ...]
    migration_note: str
    security_notes: tuple[str, ...]
    unknowns: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "direct_dependents": list(self.direct_dependents),
            "transitive_dependents": list(self.transitive_dependents),
            "endpoints": list(self.endpoints),
            "tests": list(self.tests),
            "model_dependencies": list(self.model_dependencies),
            "migration_note": self.migration_note,
            "security_notes": list(self.security_notes),
            "unknowns": list(self.unknowns),
            "execution_authorized": False,
        }


@dataclass(frozen=True)
class SystemMap:
    counts: dict[str, int]
    migrations_head: str | None
    modules: tuple[ModuleNode, ...]
    endpoints: tuple[Endpoint, ...]
    workers: tuple[Worker, ...]
    events: tuple[EventStream, ...]
    git: GitEvidence | None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def module(self, name: str) -> ModuleNode | None:
        for node in self.modules:
            if node.module == name:
                return node
        return None

    def impact(self, module: str) -> ChangeImpact:
        node = self.module(module)
        if node is None:
            raise KeyError(f"unknown module in this checkout: {module}")

        by_module = {m.module: m for m in self.modules}
        direct = set(node.imported_by)
        transitive: set[str] = set()
        frontier = list(direct)
        for _ in range(3):
            next_frontier: list[str] = []
            for name in frontier:
                node_at_name = by_module.get(name)
                for parent in node_at_name.imported_by if node_at_name else ():
                    if parent not in direct and parent not in transitive:
                        transitive.add(parent)
                        next_frontier.append(parent)
            frontier = next_frontier
        transitive_list = sorted(transitive)[:MAX_IMPACT_DEPENDENTS]

        endpoints = sorted(
            f"{e.method.upper()} {e.path}"
            for e in self.endpoints
            if e.router in {name.rsplit(".", 1)[-1] for name in direct}
        )
        model_deps = tuple(sorted(m for m in node.imports if m.startswith("app.models.")))
        security_notes: list[str] = []
        sensitive = ("auth", "security", "rls", "token", "credential", "secret", "permission")
        if any(word in module.lower() for word in sensitive):
            security_notes.append(
                "module name suggests an authorization/credential boundary; require security review"
            )
        if any(m.startswith(("app.security", "app.rls", "app.deps")) for m in node.imports):
            security_notes.append("module imports the auth/RLS layer; re-verify tenant isolation")
        if not security_notes:
            security_notes.append("no authorization dependency detected by static import analysis")

        migration_note = (
            "no model import detected; a migration is unlikely but cannot be ruled out"
            if not model_deps
            else "module imports ORM models; check whether the change alters a table or index"
        )

        return ChangeImpact(
            module=module,
            direct_dependents=tuple(sorted(direct)),
            transitive_dependents=tuple(transitive_list),
            endpoints=tuple(endpoints),
            tests=tuple(node.tests),
            model_dependencies=model_deps,
            migration_note=migration_note,
            security_notes=tuple(security_notes),
            unknowns=(
                "runtime callers reached through indirection/dynamic dispatch are not visible",
                "no coverage measurement is performed; test references are names only",
                "deployment/activation state is not observable from a checkout",
            ),
        )

    def to_dict(self) -> dict:
        return {
            "counts": dict(sorted(self.counts.items())),
            "migrations_head": self.migrations_head,
            "modules": [m.to_dict() for m in self.modules],
            "endpoints": [e.to_dict() for e in self.endpoints],
            "workers": [w.to_dict() for w in self.workers],
            "events": [e.to_dict() for e in self.events],
            "git": self.git.to_dict() if self.git else None,
            "notes": list(self.notes),
            "generated_from": "static_checkout_analysis",
            "execution_authorized": False,
        }


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _module_id(rel: Path) -> str:
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return "app." + ".".join(parts)


def _existing_modules(app_dir: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in sorted(app_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(app_dir)
        module = _module_id(rel)
        if module != "app":
            found[module] = path
    return found


def _resolve_imports(text: str, known: set[str]) -> tuple[str, ...]:
    targets: set[str] = set()
    for match in _FROM_RE.finditer(text):
        base = match.group(1)
        names = [n.strip().split(" as ")[0].strip("() ") for n in match.group(2).split(",")]
        resolved = False
        for name in names:
            if not name or name == "*":
                continue
            candidate = f"{base}.{name}"
            if candidate in known:
                targets.add(candidate)
                resolved = True
        if not resolved and base in known:
            targets.add(base)
    for match in _IMPORT_RE.finditer(text):
        target = match.group(1)
        if target in known:
            targets.add(target)
    return tuple(sorted(targets))


def _auth_mode(block: str) -> str:
    if "require_founder" in block:
        return "founder"
    if "get_current_user" in block:
        return "user"
    if "Depends(" in block:
        return "unknown"
    return "none"


def scan_system_map(repo_root: Path, *, git: GitEvidence | None = None) -> SystemMap:
    repo_root = Path(repo_root).resolve()
    app_dir = repo_root / "backend" / "app"
    main_py = app_dir / "main.py"
    if not main_py.is_file():
        raise FileNotFoundError(f"not a LifeAI checkout: {main_py} is missing")

    main_text = _read(main_py)
    routers = {
        m.group(1): main_text[: m.start()].count("\n") + 1
        for m in _INCLUDE_RE.finditer(main_text)
    }

    test_files = sorted((repo_root / "backend" / "tests").rglob("*.py"))
    test_texts = [(p, _read(p)) for p in test_files]

    def tests_for(*needles: str) -> tuple[str, ...]:
        hits: set[str] = set()
        for path, text in test_texts:
            if any(n and n in text for n in needles):
                hits.add(str(path.relative_to(repo_root)))
        return tuple(sorted(hits)[:8])

    endpoints: list[Endpoint] = []
    for router_name, line in sorted(routers.items()):
        path = app_dir / "routers" / f"{router_name}.py"
        if not path.is_file():
            continue
        text = _read(path)
        prefix_match = _PREFIX_RE.search(text)
        prefix = prefix_match.group(1).rstrip("/") if prefix_match else ""
        matches = list(_ENDPOINT_RE.finditer(text))
        for index, match in enumerate(matches):
            block_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            block = text[match.start() : block_end]
            function_match = re.search(r"^def\s+(\w+)\(", block, re.M)
            endpoints.append(
                Endpoint(
                    method=match.group(1).upper(),
                    path=f"{prefix}{match.group(2)}" or "/",
                    function=function_match.group(1) if function_match else "<unknown>",
                    router=router_name,
                    auth=_auth_mode(block),
                    evidence=f"backend/app/routers/{router_name}.py:{text[: match.start()].count(chr(10)) + 1}",
                )
            )

    module_files = _existing_modules(app_dir)
    known = set(module_files)
    imports_by_module: dict[str, tuple[str, ...]] = {}
    for module, path in module_files.items():
        imports_by_module[module] = _resolve_imports(_read(path), known)

    imported_by: dict[str, set[str]] = {m: set() for m in module_files}
    for module, targets in imports_by_module.items():
        for target in targets:
            imported_by.setdefault(target, set()).add(module)

    endpoint_paths: dict[str, list[str]] = {}
    for endpoint in endpoints:
        endpoint_paths.setdefault(f"app.routers.{endpoint.router}", []).append(
            f"{endpoint.method} {endpoint.path}"
        )

    router_modules = {f"app.routers.{r}" for r in routers}

    def _is_wired(module: str) -> bool:
        """A package is wired when it, or any module beneath it, is imported.

        `app.rag/__init__.py` is never imported directly — callers import
        `app.rag.trust`/`app.rag.ingest`. Without the descendant check, every package
        looked unwired even while its code was serving requests.
        """
        if module in router_modules or imported_by.get(module):
            return True
        prefix = module + "."
        return any(
            imported_by.get(candidate)
            for candidate in imported_by
            if candidate.startswith(prefix)
        )

    modules: list[ModuleNode] = []
    for module, path in sorted(module_files.items()):
        rel = path.relative_to(repo_root)
        direct_importers = tuple(sorted(imported_by.get(module, set())))
        tests = tests_for(module, path.stem)
        wired = _is_wired(module)
        modules.append(
            ModuleNode(
                module=module,
                path=str(rel),
                imports=imports_by_module[module],
                imported_by=direct_importers,
                endpoints=tuple(sorted(endpoint_paths.get(module, []))),
                tests=tests,
                maturity=Maturity(
                    present=True,
                    wired=wired,
                    test_referenced=bool(tests),
                ),
            )
        )

    workers: list[Worker] = []
    for name in ("worker.py", "scheduler.py"):
        path = app_dir / name
        if not path.is_file():
            continue
        text = _read(path)
        for match in _JOB_RE.finditer(text):
            workers.append(
                Worker(
                    name=match.group(1),
                    kind="worker_loop" if name == "worker.py" else "recovery_hook",
                    evidence=f"backend/app/{name}:{text[: match.start()].count(chr(10)) + 1}",
                )
            )
        for match in _SCHEDULE_RE.finditer(text):
            workers.append(
                Worker(
                    name=match.group(1).strip(),
                    kind="scheduled_job",
                    evidence=f"backend/app/{name}:{text[: match.start()].count(chr(10)) + 1}",
                )
            )

    events: list[EventStream] = []
    for module, path in sorted(module_files.items()):
        if not module.startswith("app.models."):
            continue
        text = _read(path)
        table_match = _TABLE_RE.search(text)
        for match in _EVENT_CLASS_RE.finditer(text):
            class_name = match.group(1)
            events.append(
                EventStream(
                    model=class_name,
                    table=table_match.group(1) if table_match else "<unknown>",
                    import_references=tuple(sorted(imported_by.get(module, set())))[:10],
                    note=(
                        "import references show potential producers/consumers only; "
                        "no publish/subscribe call was traced"
                    ),
                )
            )

    migration_files = sorted((repo_root / "backend" / "alembic" / "versions").glob("*.py"))
    heads = [int(p.name[:4]) for p in migration_files if p.name[:4].isdigit()]
    head = f"{max(heads):04d}" if heads else None

    notes = [
        "reviewed/integrated/activated/observed are unknown: a checkout cannot prove them",
        "test_referenced means a test file names the component, not that behaviour is covered",
        "wired means referenced from app/main.py or imported by another app module",
    ]
    if git is None:
        notes.append("no git evidence supplied; branch/PR state is unknown")
    else:
        notes.append("git evidence covers local refs and captured PR metadata only")

    return SystemMap(
        counts={
            "modules": len(modules),
            "endpoints": len(endpoints),
            "routers": len(routers),
            "workers": len(workers),
            "events": len(events),
            "migrations": len(migration_files),
            "test_files": len(test_files),
        },
        migrations_head=head,
        modules=tuple(modules),
        endpoints=tuple(sorted(endpoints, key=lambda e: (e.router, e.path, e.method))),
        workers=tuple(workers),
        events=tuple(events),
        git=git,
        notes=tuple(notes),
    )
