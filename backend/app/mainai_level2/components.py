"""Explicit composition for independently verified MainAI components.

The integration candidate must contain the code it claims to compose.  A SHA is retained as
provenance, but it is not treated as implementation presence or execution authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any, Protocol


VERIFIED_SHAS = {
    "runtime": "1951ccef16f6e165092cf85e0bd545505b69f8b1",
    "supervision": "a7df7f90dba9f8bc993005b2cce1d4c8cb7dcec4",
    "director": "ab1c0ce03a7a0f7b11f2f716304f9e1235a54d3e",
    "founder_reasoning": "4814d785954a747f5c2eed8842817222cb5147e4",
    "v1_readiness": "818dfb732da47901eb5ae06ffdd9c829fe00c4c5",
    "resource_intelligence": "063c2569a170ccc3eb7887eadd2ed1b7caed73ff",
}


@dataclass(frozen=True)
class ComponentSource:
    name: str
    source_sha: str
    import_module: str
    callable_name: str
    integrated_files: tuple[str, ...]
    source_branch: str | None = None


COMPONENT_SOURCES = {
    "director": ComponentSource(
        name="director",
        source_sha=VERIFIED_SHAS["director"],
        source_branch="dev_director frozen candidate",
        import_module="app.dev_director.provider_lease",
        callable_name="new_external_provider_lease",
        integrated_files=("backend/app/dev_director/",),
    ),
    "supervision": ComponentSource(
        name="supervision",
        source_sha=VERIFIED_SHAS["supervision"],
        source_branch="codex/mainai-continuous-supervision frozen candidate",
        import_module="app.mainai_execution.canonical_supervisor",
        callable_name="identity",
        integrated_files=("backend/app/mainai_execution/canonical_supervisor.py",),
    ),
    "resource_intelligence": ComponentSource(
        name="resource_intelligence",
        source_sha=VERIFIED_SHAS["resource_intelligence"],
        source_branch="Resource Intelligence frozen candidate",
        import_module="app.resource_intelligence.types",
        callable_name="unknown_metric",
        integrated_files=(
            "backend/app/resource_intelligence/",
            "backend/app/models/resource_intelligence.py",
            "backend/alembic/versions/0074_resource_intelligence_telemetry_for_level2.py",
        ),
    ),
    "founder_reasoning": ComponentSource(
        name="founder_reasoning",
        source_sha=VERIFIED_SHAS["founder_reasoning"],
        source_branch="Founder Reasoning/Judgment frozen candidate",
        import_module="app.mainai_executive.judgment",
        callable_name="decide_judgment",
        integrated_files=("backend/app/mainai_executive/judgment.py",),
    ),
}


@dataclass(frozen=True)
class ComponentBinding:
    name: str
    sha: str
    verified: bool = True


class VerifiedComponentRegistry:
    def __init__(self, bindings: tuple[ComponentBinding, ...] | None = None):
        self.bindings = {b.name: b for b in (bindings or tuple(ComponentBinding(k, v) for k, v in VERIFIED_SHAS.items()))}

    def require(self, name: str, sha: str | None = None) -> ComponentBinding:
        binding = self.bindings.get(name)
        expected = VERIFIED_SHAS.get(name)
        if binding is None or not binding.verified or expected is None or binding.sha != expected:
            raise ValueError(f"component {name!r} is not independently verified")
        if sha is not None and sha != binding.sha:
            raise ValueError(f"component {name!r} SHA is stale")
        return binding

    def snapshot(self) -> dict[str, str]:
        return {name: self.require(name).sha for name in sorted(self.bindings)}


class ComponentAdapter(Protocol):
    """A narrow, evidence-bound adapter; adapters never grant runtime authority."""
    component: str
    candidate_sha: str

    def health(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class BoundComponentAdapter:
    component: str
    candidate_sha: str
    implementation: Any
    seam_only: bool = False
    source: ComponentSource | None = None

    def health(self) -> dict[str, object]:
        return {
            "component": self.component,
            "candidate_sha": self.candidate_sha,
            "bound": self.implementation is not None,
            "seam_only": self.seam_only,
            "authority": "none",
            "source_sha": self.source.source_sha if self.source else self.candidate_sha,
            "import_module": self.source.import_module if self.source else None,
            "callable": self.source.callable_name if self.source else None,
        }

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        if self.seam_only or self.implementation is None:
            raise RuntimeError(f"verified component {self.component!r} has no executable implementation")
        return self.implementation(*args, **kwargs)


class VerifiedComposition:
    """Explicit composition of verified public seams.

    Missing implementation objects fail closed; merely recording a SHA never pretends another
    branch was imported. Advisory components return advisory data only and never grant runtime
    authority.
    """
    def __init__(self, registry: VerifiedComponentRegistry | None = None):
        self.registry = registry or VerifiedComponentRegistry()
        self.adapters: dict[str, BoundComponentAdapter] = {}

    def bind(self, name: str, implementation: Any, *, seam_only: bool = False, source: ComponentSource | None = None) -> BoundComponentAdapter:
        binding = self.registry.require(name)
        if source is not None and source.source_sha != binding.sha:
            raise ValueError(f"component {name!r} source SHA mismatch")
        adapter = BoundComponentAdapter(name, binding.sha, implementation, seam_only, source)
        self.adapters[name] = adapter
        return adapter

    def bind_external(self, name: str, implementation: Any, *, required_methods: tuple[str, ...]) -> BoundComponentAdapter:
        """Bind an object seam only when its public methods are present."""
        missing = [method for method in required_methods if not callable(getattr(implementation, method, None))]
        if missing:
            raise TypeError(f"component {name!r} seam mismatch: missing {','.join(missing)}")
        return self.bind(name, implementation, seam_only=False)

    def bind_imported(self, name: str) -> BoundComponentAdapter:
        source = COMPONENT_SOURCES[name]
        self.registry.require(name, source.source_sha)
        module = import_module(source.import_module)
        implementation = getattr(module, source.callable_name, None)
        if not callable(implementation):
            raise TypeError(f"component {name!r} implementation missing {source.import_module}.{source.callable_name}")
        return self.bind(name, implementation, seam_only=False, source=source)

    def require_bound(self, name: str) -> BoundComponentAdapter:
        adapter = self.adapters.get(name)
        self.registry.require(name)
        if adapter is None or adapter.implementation is None:
            raise RuntimeError(f"verified component {name!r} has no bound implementation")
        return adapter

    def snapshot(self) -> dict[str, dict[str, object]]:
        return {name: adapter.health() for name, adapter in sorted(self.adapters.items())}


def compose_local_verified_components(registry: VerifiedComponentRegistry | None = None) -> VerifiedComposition:
    """Bind all verified components whose code is physically integrated in this tree."""
    composition = VerifiedComposition(registry)
    for name in ("director", "supervision", "resource_intelligence", "founder_reasoning"):
        composition.bind_imported(name)
    return composition
