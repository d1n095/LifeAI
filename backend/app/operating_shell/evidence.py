"""Show-don't-tell evidence surfaces (MainAI V2, Stage V2-I5).

The orb remains the primary UI; a produced EvidenceSurfaceResult is a subordinate,
temporary surface -- reflected structurally (dismissible/returns_focus_to_orb), not only
documented in prose.
"""

from __future__ import annotations

from app.operating_shell.types import EvidenceSurfaceKind, EvidenceSurfaceRequest, EvidenceSurfaceResult


def build_evidence_surface(request: EvidenceSurfaceRequest) -> EvidenceSurfaceResult:
    """Pure translation from request to a subordinate-surface description -- this package
    does not render anything; it only describes what a future UI layer should show and why.
    Every result is dismissible and returns focus to the orb by default (see types.py)."""
    return EvidenceSurfaceResult(kind=request.kind, target_refs=request.target_refs)


def request_incident_evidence(target_refs: tuple) -> EvidenceSurfaceRequest:
    return EvidenceSurfaceRequest(kind=EvidenceSurfaceKind.INCIDENT_EVIDENCE, target_refs=target_refs, reason="why did you block that")


def request_diff_view(target_refs: tuple) -> EvidenceSurfaceRequest:
    if len(target_refs) < 2:
        raise ValueError("a diff view requires at least two target refs to compare")
    return EvidenceSurfaceRequest(kind=EvidenceSurfaceKind.DIFF_VIEW, target_refs=target_refs, reason="show the difference")


def request_memory_evidence(target_refs: tuple) -> EvidenceSurfaceRequest:
    return EvidenceSurfaceRequest(kind=EvidenceSurfaceKind.MEMORY_EVIDENCE, target_refs=target_refs, reason="show what you remember")
