"""Initial provider selection (Milestone 2, Part 2) -- distinct from provider_lease.py's
select_failover_provider(), which is specifically the AFTER-A-FAILURE path. This is the
INITIAL selection for a fresh job. Entirely criteria-driven over the candidate tuple the
caller supplies -- no special-cased branch for any specific provider name anywhere in this
module (see test_no_hardcoded_provider_names in the test file, which asserts this via
inspecting the function's own source for literal provider-name string comparisons)."""

from __future__ import annotations

from app.dev_director.types import (
    Job,
    NoAvailableProvider,
    Program,
    ProviderCapabilityProfile,
    ProviderUsageState,
)


def select_builder_provider(
    candidates: tuple[ProviderCapabilityProfile, ...],
    *,
    job: Job,
    program: Program,
    required_capabilities: tuple[str, ...] = (),
    examiner_role: bool = False,
    excluding: tuple[str, ...] = (),
) -> ProviderCapabilityProfile | NoAvailableProvider:
    """The INITIAL selection for a job (not a post-failure failover -- see
    provider_lease.select_failover_provider() for that). Filters by:
    program.allowed_providers (if non-empty, only those identities are eligible),
    usage_state == AVAILABLE, required_capabilities subset match, examiner eligibility when
    requested, and `excluding` (e.g. the job's own builder identity, when selecting an
    examiner). No hard-coded provider name anywhere -- purely criteria over the tuple."""
    allowed = set(program.allowed_providers) if program.allowed_providers else None
    eligible = [
        c
        for c in candidates
        if c.provider_identity not in excluding
        and c.usage_state == ProviderUsageState.AVAILABLE
        and (allowed is None or c.provider_identity in allowed)
        and set(required_capabilities).issubset(set(c.capabilities))
        and (not examiner_role or c.is_examiner_eligible)
    ]
    if not eligible:
        return NoAvailableProvider(reason="no candidate is AVAILABLE, allow-listed, capability-matching, and not excluded")
    # Deterministic tie-break: lowest recent failure rate, then identity for stability --
    # never an arbitrary/unstable pick.
    eligible.sort(key=lambda c: (c.recent_failure_rate, c.provider_identity))
    return eligible[0]
