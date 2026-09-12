"""Explicit seams for independently verified MainAI components.

The registry records immutable evidence of which candidate is being consumed.  It does not
import another branch or grant authority; every effect still goes through the runtime port.
"""
from __future__ import annotations

from dataclasses import dataclass
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

    def health(self) -> dict[str, object]:
        return {
            "component": self.component,
            "candidate_sha": self.candidate_sha,
            "bound": self.implementation is not None,
            "seam_only": self.seam_only,
            "authority": "none",
        }


class VerifiedComposition:
    """Explicit composition of verified public seams.

    Missing implementation objects are represented as seam-only and fail closed when used;
    merely recording a SHA never pretends that another branch was imported.
    """
    def __init__(self, registry: VerifiedComponentRegistry | None = None):
        self.registry = registry or VerifiedComponentRegistry()
        self.adapters: dict[str, BoundComponentAdapter] = {}

    def bind(self, name: str, implementation: Any, *, seam_only: bool = False) -> BoundComponentAdapter:
        binding = self.registry.require(name)
        adapter = BoundComponentAdapter(name, binding.sha, implementation, seam_only)
        self.adapters[name] = adapter
        return adapter

    def bind_external(self, name: str, implementation: Any, *, required_methods: tuple[str, ...]) -> BoundComponentAdapter:
        """Bind an external frozen implementation only when its public seam is present."""
        missing = [method for method in required_methods if not callable(getattr(implementation, method, None))]
        if missing:
            raise TypeError(f"component {name!r} seam mismatch: missing {','.join(missing)}")
        return self.bind(name, implementation, seam_only=False)

    def require_bound(self, name: str) -> BoundComponentAdapter:
        adapter = self.adapters.get(name)
        self.registry.require(name)
        if adapter is None or adapter.implementation is None:
            raise RuntimeError(f"verified component {name!r} has no bound implementation")
        return adapter

    def snapshot(self) -> dict[str, dict[str, object]]:
        return {name: adapter.health() for name, adapter in sorted(self.adapters.items())}
