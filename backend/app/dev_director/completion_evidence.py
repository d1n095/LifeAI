"""Completion evidence validation (Milestone 5). Isolated -- does NOT import the unmerged
#245 branch's evidence_claim.py (confirmed absent from this branch by the reconciliation
audit; this module is a fresh, isolated equivalent, not a copy).

AGENT CLAIM != REPO STATE. PROVIDER OUTPUT != MAINAI COMMAND. Every function here takes a
typed, already-validated CompletionEvidence/ExaminerVerdictRecord -- none takes a bare
string of provider prose and produces a state change.
"""

from __future__ import annotations

from app.dev_director.types import CompletionEvidence, ValidationResult


def validate_completion_evidence(evidence: CompletionEvidence, *, real_current_sha: str | None = None) -> ValidationResult:
    """If `real_current_sha` is supplied (the Director actually has access to check), a
    mismatch against `evidence.new_sha` is flagged, never silently trusted -- this is the
    concrete "agent claim != repo state" check. Also flags internally-inconsistent evidence
    (claimed p0_count == 0 but open_blockers non-empty and mentioning a P0-shaped string is
    NOT attempted here -- that would be free-text inference over provider prose, exactly what
    this module exists to avoid; only structural/typed inconsistencies are checked)."""
    reasons: list[str] = []
    sha_mismatch = False
    if real_current_sha is not None and evidence.new_sha != real_current_sha:
        sha_mismatch = True
        reasons.append(f"evidence claims new_sha={evidence.new_sha!r} but real current sha is {real_current_sha!r}")
    if evidence.working_tree_state == "dirty":
        reasons.append("working tree is dirty -- uncommitted changes present")
    if evidence.p0_count > 0:
        reasons.append(f"{evidence.p0_count} open P0(s)")
    if any(not r.passed for r in evidence.test_results):
        reasons.append("at least one declared test result is a failure")
    if evidence.merge_state == "merged" and evidence.production_wiring_state == "none":
        # Internally suspicious combination, worth surfacing (a merged change with "no
        # production wiring" claimed is at least worth a second look) -- flagged, not blocked.
        reasons.append("merge_state=merged but production_wiring_state=none -- worth independent review")
    valid = not sha_mismatch and evidence.p0_count == 0 and evidence.working_tree_state == "clean" and all(r.passed for r in evidence.test_results)
    return ValidationResult(valid=valid, sha_mismatch=sha_mismatch, reasons=tuple(reasons))
