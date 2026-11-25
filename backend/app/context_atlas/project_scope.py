"""Deterministic project-boundary checks for historical and current context (C3).

The atlas must never combine requirements from LifeAI, L4TP, LifeMusic, LifeInvest,
4ThePeople or an unrelated historical project merely because the documents use similar
words. This module therefore resolves *explicit* project references against a caller-supplied
project registry and fails closed when a reference is missing, unknown or ambiguous.

It performs no I/O and no model inference. A project registry is data the caller has already
reviewed (the real `projects` table, a curated list, or a test fixture); the module does not
guess a project from free text. Similar terminology is not a boundary signal.

`ProjectScopeReport.canonical_truth` is always None and `execution_authorized` is always
False. Only `isolated` permits implementation; every other status is a review outcome.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum


class ScopeStatus(str, Enum):
    isolated = "isolated"
    mixed = "mixed"
    partial = "partial"
    ambiguous = "ambiguous"
    unknown = "unknown"


@dataclass(frozen=True)
class ProjectDefinition:
    """One reviewed project identity and its explicit aliases."""

    project_id: str
    name: str
    aliases: tuple[str, ...] = ()
    archived: bool = False


@dataclass(frozen=True)
class ScopeClaim:
    """One statement whose project ownership is being checked."""

    source_id: str
    project_ref: str | None
    statement: str = ""
    evidence: str = ""


@dataclass(frozen=True)
class ProjectScopeReport:
    status: ScopeStatus
    project_refs: tuple[str, ...]
    matched_project_ids: tuple[str, ...]
    unmatched_refs: tuple[str, ...]
    ambiguous_refs: tuple[str, ...]
    claims: tuple[ScopeClaim, ...]
    reason: str
    requires_review: bool
    canonical_truth: str | None = None
    execution_authorized: bool = False

    @property
    def allows_implementation(self) -> bool:
        return self.status is ScopeStatus.isolated

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "project_refs": list(self.project_refs),
            "matched_project_ids": list(self.matched_project_ids),
            "unmatched_refs": list(self.unmatched_refs),
            "ambiguous_refs": list(self.ambiguous_refs),
            "claims": [
                {
                    "source_id": claim.source_id,
                    "project_ref": claim.project_ref,
                    "statement": claim.statement,
                    "evidence": claim.evidence,
                }
                for claim in self.claims
            ],
            "reason": self.reason,
            "requires_review": self.requires_review,
            "allows_implementation": self.allows_implementation,
            "canonical_truth": None,
            "execution_authorized": False,
        }

    def to_reconciliation_observation(self):
        """Map the boundary result onto the existing fail-closed reconciler.

        Imported lazily so this pure module does not create an import cycle with
        `reconciliation.py`.
        """

        from app.context_atlas.reconciliation import ContextObservation

        if self.status is ScopeStatus.isolated:
            return ContextObservation(project_scope="this_project")
        return ContextObservation(
            project_scope="unknown",
            ambiguous=True,
            evidence_sufficient=False,
            missing_evidence=(
                "an explicit, non-ambiguous project reference for every claim",
                *self.unmatched_refs,
                *self.ambiguous_refs,
            ),
        )


def _normalize(value: str) -> tuple[str, str]:
    """Return (space-separated, compact) normalized forms of a project reference.

    Unicode compatibility normalization and casefolding are deterministic. Punctuation and
    whitespace are collapsed, so "Life 4ThePeople", "life-4-the-people" and
    "LIFE 4 THE PEOPLE" share the space-separated key. The compact form additionally lets
    "4ThePeople" match "4 The People" without treating unrelated words as equal.
    """

    folded = unicodedata.normalize("NFKC", value).casefold()
    parts = re.findall(r"[a-z0-9]+", folded)
    spaced = " ".join(parts)
    compact = "".join(parts)
    return spaced, compact


def _project_index(
    projects: tuple[ProjectDefinition, ...],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    spaced_index: dict[str, set[str]] = {}
    compact_index: dict[str, set[str]] = {}
    for project in projects:
        if not project.project_id.strip():
            raise ValueError("project_id must not be empty")
        keys = {project.project_id, project.name, *project.aliases}
        for key in keys:
            spaced, compact = _normalize(key)
            if not spaced:
                continue
            spaced_index.setdefault(spaced, set()).add(project.project_id)
            compact_index.setdefault(compact, set()).add(project.project_id)
    return spaced_index, compact_index


def _match(
    ref: str,
    spaced_index: dict[str, set[str]],
    compact_index: dict[str, set[str]],
) -> tuple[str, ...]:
    spaced, compact = _normalize(ref)
    if not spaced:
        return ()
    matches = spaced_index.get(spaced)
    if matches is None:
        matches = compact_index.get(compact)
    return tuple(sorted(matches or ()))


def resolve_project_scope(
    claims: tuple[ScopeClaim, ...] | list[ScopeClaim],
    projects: tuple[ProjectDefinition, ...] | list[ProjectDefinition],
) -> ProjectScopeReport:
    """Resolve every claim's explicit project reference and report the boundary.

    Rules, in order:

    - no claims or no registry -> `unknown` (nothing can be inferred);
    - an alias shared by two projects -> `ambiguous` (never pick one);
    - references to more than one known project -> `mixed` (never combine);
    - one project plus unassigned claims -> `partial` (review the unassigned ones);
    - one project plus unknown references -> `unknown` (they cannot be placed);
    - every claim on the same known project -> `isolated` (implementation may proceed).

    A statement's wording is never used to decide ownership. Similar terminology across
    projects is therefore irrelevant to this decision.
    """

    claim_tuple = tuple(claims)
    project_tuple = tuple(projects)
    if not claim_tuple:
        return ProjectScopeReport(
            status=ScopeStatus.unknown,
            project_refs=(),
            matched_project_ids=(),
            unmatched_refs=(),
            ambiguous_refs=(),
            claims=(),
            reason="no claims were supplied; project ownership cannot be inferred",
            requires_review=True,
        )
    if not project_tuple:
        return ProjectScopeReport(
            status=ScopeStatus.unknown,
            project_refs=tuple(
                claim.project_ref.strip()
                for claim in claim_tuple
                if claim.project_ref and claim.project_ref.strip()
            ),
            matched_project_ids=(),
            unmatched_refs=(),
            ambiguous_refs=(),
            claims=claim_tuple,
            reason="no reviewed project registry was supplied; do not guess an owner",
            requires_review=True,
        )

    spaced_index, compact_index = _project_index(project_tuple)

    project_refs: list[str] = []
    matched_ids: set[str] = set()
    unmatched: list[str] = []
    ambiguous: list[str] = []
    unassigned_count = 0

    for claim in claim_tuple:
        ref = (claim.project_ref or "").strip()
        if not ref:
            unassigned_count += 1
            continue
        project_refs.append(ref)
        matches = _match(ref, spaced_index, compact_index)
        if not matches:
            unmatched.append(ref)
        elif len(matches) > 1:
            ambiguous.append(ref)
        else:
            matched_ids.add(matches[0])

    if ambiguous:
        return ProjectScopeReport(
            status=ScopeStatus.ambiguous,
            project_refs=tuple(project_refs),
            matched_project_ids=tuple(sorted(matched_ids)),
            unmatched_refs=tuple(unmatched),
            ambiguous_refs=tuple(sorted(set(ambiguous))),
            claims=claim_tuple,
            reason=(
                "at least one project reference matches more than one reviewed project; "
                "do not choose an owner without a founder decision"
            ),
            requires_review=True,
        )

    if len(matched_ids) > 1:
        return ProjectScopeReport(
            status=ScopeStatus.mixed,
            project_refs=tuple(project_refs),
            matched_project_ids=tuple(sorted(matched_ids)),
            unmatched_refs=tuple(unmatched),
            ambiguous_refs=(),
            claims=claim_tuple,
            reason=(
                "claims belong to more than one project; keep them separate even when their "
                "wording is similar"
            ),
            requires_review=True,
        )

    if not matched_ids:
        return ProjectScopeReport(
            status=ScopeStatus.unknown,
            project_refs=tuple(project_refs),
            matched_project_ids=(),
            unmatched_refs=tuple(sorted(set(unmatched))),
            ambiguous_refs=(),
            claims=claim_tuple,
            reason=(
                "no project reference matched the reviewed registry; the claims may be "
                "historical or belong to a project that is not registered"
            ),
            requires_review=True,
        )

    project_id = next(iter(matched_ids))
    if unmatched:
        return ProjectScopeReport(
            status=ScopeStatus.unknown,
            project_refs=tuple(project_refs),
            matched_project_ids=(project_id,),
            unmatched_refs=tuple(sorted(set(unmatched))),
            ambiguous_refs=(),
            claims=claim_tuple,
            reason=(
                "one project is identified, but other references could not be placed; do not "
                "merge them into that project"
            ),
            requires_review=True,
        )

    if unassigned_count:
        return ProjectScopeReport(
            status=ScopeStatus.partial,
            project_refs=tuple(project_refs),
            matched_project_ids=(project_id,),
            unmatched_refs=(),
            ambiguous_refs=(),
            claims=claim_tuple,
            reason=(
                "one project is identified, but some claims have no project reference; "
                "review those claims instead of assuming they belong here"
            ),
            requires_review=True,
        )

    return ProjectScopeReport(
        status=ScopeStatus.isolated,
        project_refs=tuple(project_refs),
        matched_project_ids=(project_id,),
        unmatched_refs=(),
        ambiguous_refs=(),
        claims=claim_tuple,
        reason="every claim resolves to the same reviewed project",
        requires_review=False,
    )


def project_scope_for_refs(
    project_refs: tuple[str, ...] | list[str],
    projects: tuple[ProjectDefinition, ...] | list[ProjectDefinition],
) -> ProjectScopeReport:
    """Convenience wrapper for callers that only have project references."""

    claims = [
        ScopeClaim(source_id=f"ref:{index}", project_ref=ref)
        for index, ref in enumerate(project_refs, start=1)
    ]
    return resolve_project_scope(claims, projects)
