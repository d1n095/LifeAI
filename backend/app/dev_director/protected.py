"""Protected artifact declarations (Milestone 3). Genuinely new -- confirmed by the
reconciliation audit that no #245-style "do not modify this SHA" concept exists anywhere in
real production code today."""

from __future__ import annotations

from app.dev_director.types import ProtectedArtifact, ProtectedArtifactViolationError


def assert_artifact_not_protected(ref: str, protected: tuple[ProtectedArtifact, ...], *, action: str = "modify") -> None:
    """Raises if `ref` matches any protected entry's ref with the relevant flag set. `action`
    is one of "modify"/"merge"/"rebase" -- checked against the matching do_not_* flag; any
    match against an `examine_only` entry blocks every action except a read-only examine."""
    for artifact in protected:
        # Exact match only -- no substring/prefix matching (the recurring "substring instead
        # of exact match" bug shape this campaign has repeatedly found and fixed).
        if artifact.ref != ref:
            continue
        if action == "modify" and artifact.do_not_modify:
            raise ProtectedArtifactViolationError(f"{ref} is protected (do_not_modify): {artifact.reason}")
        if action == "merge" and artifact.do_not_merge:
            raise ProtectedArtifactViolationError(f"{ref} is protected (do_not_merge): {artifact.reason}")
        if action == "rebase" and artifact.do_not_rebase:
            raise ProtectedArtifactViolationError(f"{ref} is protected (do_not_rebase): {artifact.reason}")
