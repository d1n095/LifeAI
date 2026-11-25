"""Stable, read-only runtime interface over the existing Context Atlas analyses.

The atlas already has small pure modules for capability inventory, dependency impact,
source identity, traceability, recommendations, ownership and development recovery. What was
missing was one place existing LifeAI services could call without re-implementing the glue or
parsing CLI output.

This module is deliberately a *composition* layer:

- it caches the static checkout scan by local git SHA / dirty state / captured-PR file
  fingerprint, so repeated questions do not rescan the repository;
- it turns unknown modules, missing refs, unavailable git and corrupt captured-PR files into
  structured `unavailable` / `not_found` answers instead of exceptions or fabricated facts;
- it never calls a model, the network, a queue or an agent adapter, and every result carries
  `execution_authorized: false`.

It adds no table, migration, task engine or second memory store.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.context_atlas.capability_map import CapabilityMap, scan_capabilities
from app.context_atlas.consistency import check_consistency
from app.context_atlas.development_state import (
    DevelopmentState,
    RecoveryReport,
    load_development_state,
    recover_development_state,
)
from app.context_atlas.git_evidence import GitEvidence, PullRequestRef, scan_local_git
from app.context_atlas.ownership import (
    OWNED,
    UNCLEAR,
    OwnershipReport,
    find_work_owners,
)
from app.context_atlas.project_scope import ProjectScopeReport, ScopeStatus
from app.context_atlas.recommendation import (
    CAPABILITY_ALIASES,
    CapabilityFact,
    EngineeringRequest,
    Recommendation,
    recommend,
)
from app.context_atlas.system_map import ChangeImpact, ModuleNode, SystemMap, scan_system_map
from app.context_atlas.traceability import (
    EvidenceRef,
    RequirementTrace,
    Stage,
    build_trace,
)

# A captured PR file is one observation at one point in time. Older than this it is reported
# as stale instead of being presented as current remote state.
_PR_CAPTURE_MAX_AGE = timedelta(hours=24)

_SUPPORTED_QUESTIONS = (
    "which components are implemented",
    "which components are tested",
    "which functions are only documented",
    "which changes have not been integrated",
    "what dependencies does this feature have",
    "which branch contains the latest verified implementation",
    "which capabilities overlap",
    "what would likely break if this module changed",
    "which security controls protect this capability",
    "what remains before this feature can be used",
)


@dataclass(frozen=True)
class CapabilityAnswer:
    query: str
    status: str  # found | ambiguous | not_found | unavailable
    component: str | None = None
    kind: str | None = None
    path: str | None = None
    wired: bool | None = None
    test_referenced: bool | None = None
    endpoints: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    maturity: dict[str, bool | None] | None = None
    evidence: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    interpretation: str = ""
    execution_authorized: bool = False

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "status": self.status,
            "component": self.component,
            "kind": self.kind,
            "path": self.path,
            "wired": self.wired,
            "test_referenced": self.test_referenced,
            "endpoints": list(self.endpoints),
            "tests": list(self.tests),
            "maturity": dict(self.maturity) if self.maturity is not None else None,
            "evidence": list(self.evidence),
            "limitations": list(self.limitations),
            "interpretation": self.interpretation,
            "execution_authorized": False,
        }


@dataclass(frozen=True)
class DependencyAnswer:
    module: str
    status: str  # found | not_found | unavailable
    impact: ChangeImpact | None = None
    evidence: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    execution_authorized: bool = False

    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "status": self.status,
            "impact": self.impact.to_dict() if self.impact is not None else None,
            "evidence": list(self.evidence),
            "limitations": list(self.limitations),
            "execution_authorized": False,
        }


@dataclass(frozen=True)
class TraceabilityAnswer:
    requirement_id: str
    status: str  # traced | insufficient_evidence
    trace: RequirementTrace | None
    unverified_refs: tuple[str, ...]
    limitations: tuple[str, ...]
    execution_authorized: bool = False

    def to_dict(self) -> dict:
        return {
            "requirement_id": self.requirement_id,
            "status": self.status,
            "trace": self.trace.to_dict() if self.trace is not None else None,
            "unverified_refs": list(self.unverified_refs),
            "limitations": list(self.limitations),
            "execution_authorized": False,
        }


@dataclass(frozen=True)
class ProposalAssessment:
    request_text: str
    capability_aliases: tuple[str, ...]
    project_ref: str
    recommendation: Recommendation
    scope: ProjectScopeReport | None
    ownership: OwnershipReport | None
    trace: RequirementTrace | None
    evidence: tuple[str, ...]
    limitations: tuple[str, ...]
    execution_authorized: bool = False

    @property
    def outcome(self):
        return self.recommendation.outcome

    def to_dict(self) -> dict:
        return {
            "request": {
                "text": self.request_text,
                "capability_aliases": list(self.capability_aliases),
                "project_ref": self.project_ref,
            },
            "recommendation": self.recommendation.to_dict(),
            "scope": self.scope.to_dict() if self.scope is not None else None,
            "ownership": self.ownership.to_dict() if self.ownership is not None else None,
            "trace": self.trace.to_dict() if self.trace is not None else None,
            "evidence": list(self.evidence),
            "limitations": list(self.limitations),
            "execution_authorized": False,
        }


@dataclass(frozen=True)
class IntegrationStatus:
    branch: str | None
    head_sha: str | None
    dirty_tracked_tree: bool | None
    local_branch_count: int
    captured_pull_requests: tuple[PullRequestRef, ...]
    matching_pull_requests: tuple[PullRequestRef, ...]
    state: str  # captured_pr | not_in_captured_prs | unknown
    local_only: bool | None
    notes: tuple[str, ...]
    evidence: tuple[str, ...]
    capture_stale: bool | None = None
    capture_age_hours: float | None = None
    execution_authorized: bool = False

    def to_dict(self) -> dict:
        return {
            "branch": self.branch,
            "head_sha": self.head_sha,
            "dirty_tracked_tree": self.dirty_tracked_tree,
            "local_branch_count": self.local_branch_count,
            "captured_pull_requests": [
                {
                    "number": pr.number,
                    "state": pr.state,
                    "draft": pr.draft,
                    "head_ref": pr.head_ref,
                    "head_sha": pr.head_sha,
                    "base_ref": pr.base_ref,
                    "updated_at": pr.updated_at,
                    "url": pr.url,
                }
                for pr in self.captured_pull_requests
            ],
            "matching_pull_requests": [
                {
                    "number": pr.number,
                    "state": pr.state,
                    "draft": pr.draft,
                    "head_ref": pr.head_ref,
                    "head_sha": pr.head_sha,
                    "base_ref": pr.base_ref,
                    "updated_at": pr.updated_at,
                    "url": pr.url,
                }
                for pr in self.matching_pull_requests
            ],
            "state": self.state,
            "local_only": self.local_only,
            "notes": list(self.notes),
            "evidence": list(self.evidence),
            "capture_stale": self.capture_stale,
            "capture_age_hours": self.capture_age_hours,
            "execution_authorized": False,
        }


@dataclass(frozen=True)
class RecoveryView:
    status: str
    report: RecoveryReport
    state: DevelopmentState | None
    evidence: tuple[str, ...]
    limitations: tuple[str, ...]
    execution_authorized: bool = False

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "report": self.report.to_dict(),
            "state": self.state.to_dict() if self.state is not None else None,
            "evidence": list(self.evidence),
            "limitations": list(self.limitations),
            "execution_authorized": False,
        }


@dataclass(frozen=True)
class OverlapAssessment:
    requested_paths: tuple[str, ...]
    ownership: OwnershipReport
    git: GitEvidence
    notes: tuple[str, ...]
    execution_authorized: bool = False

    def to_dict(self) -> dict:
        return {
            "requested_paths": list(self.requested_paths),
            "ownership": self.ownership.to_dict(),
            "git": self.git.to_dict(),
            "notes": list(self.notes),
            "execution_authorized": False,
        }


@dataclass(frozen=True)
class UntrustedSourceReport:
    source_ref: str
    treated_as_data: bool
    instruction_like: bool
    matched_patterns: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    note: str
    execution_authorized: bool = False

    def to_dict(self) -> dict:
        return {
            "source_ref": self.source_ref,
            "treated_as_data": self.treated_as_data,
            "instruction_like": self.instruction_like,
            "matched_patterns": list(self.matched_patterns),
            "allowed_actions": list(self.allowed_actions),
            "note": self.note,
            "execution_authorized": False,
        }


@dataclass(frozen=True)
class SelfKnowledgeAnswer:
    question: str
    intent: str
    status: str  # answered | needs_input | unknown | unavailable
    answer: str
    facts: dict[str, Any]
    evidence: tuple[str, ...]
    limitations: tuple[str, ...]
    interpretation: str | None
    execution_authorized: bool = False

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "intent": self.intent,
            "status": self.status,
            "answer": self.answer,
            "facts": self.facts,
            "evidence": list(self.evidence),
            "limitations": list(self.limitations),
            "interpretation": self.interpretation,
            "execution_authorized": False,
        }


def default_repo_root() -> Path:
    """Return the checkout containing this module (backend/app/context_atlas/runtime.py)."""
    return Path(__file__).resolve().parents[3]


class SystemIntelligence:
    """Read-only composition service for existing LifeAI services.

    The instance is intentionally cheap to create and safe to keep for the lifetime of a
    request/worker. `refresh()` (or a changed local HEAD/PR file) invalidates the scan cache.
    """

    def __init__(
        self,
        repo_root: Path | str | None = None,
        *,
        pull_requests_path: Path | str | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve() if repo_root is not None else default_repo_root()
        self.pull_requests_path = (
            Path(pull_requests_path).resolve() if pull_requests_path is not None else None
        )
        self._system: SystemMap | None = None
        self._capability_map: CapabilityMap | None = None
        self._git: GitEvidence | None = None
        self._cache_key: tuple[Any, ...] | None = None
        self._warnings: list[str] = []
        self._error: str | None = None
        self._ensure()

    def _pr_fingerprint(self) -> tuple[Any, ...] | None:
        if self.pull_requests_path is None:
            return None
        try:
            stat = self.pull_requests_path.stat()
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return ("missing",)

    def _collect_warnings(self, git: GitEvidence) -> list[str]:
        warnings: list[str] = []
        if git.current_sha is None:
            warnings.append("local git evidence is unavailable; branch and SHA are unknown")
        if self.pull_requests_path is not None and not self.pull_requests_path.is_file():
            warnings.append(
                f"captured pull-request file is missing: {self.pull_requests_path.name}"
            )
        if not git.pull_requests:
            warnings.append("no captured pull-request metadata; remote integration is unknown")
        if git.dirty_tracked_tree:
            warnings.append(
                "tracked files are modified; HEAD does not represent the full working tree"
            )
        return warnings

    def _ensure(self) -> None:
        git = scan_local_git(
            self.repo_root, pull_requests_path=self.pull_requests_path
        )
        key = (git.current_sha, git.dirty_tracked_tree, self._pr_fingerprint())
        if key == self._cache_key and self._system is not None:
            self._git = git
            return

        self._git = git
        self._warnings = []
        try:
            system = scan_system_map(self.repo_root, git=git)
            capability_map = scan_capabilities(self.repo_root)
        except (FileNotFoundError, OSError, ValueError) as exc:
            self._system = None
            self._capability_map = None
            self._error = f"repository scan failed: {exc}"
            self._warnings.append(self._error)
            self._cache_key = key
            return

        self._system = system
        self._capability_map = capability_map
        self._error = None
        self._cache_key = key
        self._warnings.extend(self._collect_warnings(git))

    def refresh(self) -> None:
        """Force a rescan on the next access (call after a checkout/PR capture changes)."""
        self._cache_key = None
        self._ensure()

    def _system_map(self) -> SystemMap:
        if self._system is None:
            raise RuntimeError(self._error or "system intelligence is unavailable")
        return self._system

    def _facts(self, system: SystemMap | None = None) -> dict[str, CapabilityFact]:
        system = system or self._system_map()
        return {
            node.module: CapabilityFact(
                component=node.module,
                present=node.maturity.present,
                wired=node.maturity.wired,
                test_referenced=node.maturity.test_referenced,
                path=node.path,
                endpoints=node.endpoints,
            )
            for node in system.modules
        }

    @staticmethod
    def _maturity_dict(node: ModuleNode) -> dict[str, bool | None]:
        return node.maturity.to_dict()

    def _capability_answer(self, query: str, node: ModuleNode) -> CapabilityAnswer:
        limitations = [
            "present/wired/test_referenced come from static checkout analysis",
            "reviewed/integrated/activated/observed are unknown unless external evidence is supplied",
            "a test-file mention is not behavioural coverage",
        ]
        if not node.maturity.wired:
            limitations.append("no app import or app/main.py router wiring was found")
        if not node.maturity.test_referenced:
            limitations.append("no test file names this component")

        if node.maturity.wired and node.maturity.test_referenced:
            interpretation = (
                "The component is present, wired through an import/router reference, and at "
                "least one test file names it. Runtime behaviour is not observed."
            )
        elif node.maturity.wired:
            interpretation = (
                "The component is present and wired, but no test file names it. Wiring is not "
                "behavioural verification."
            )
        elif node.maturity.test_referenced:
            interpretation = (
                "The component is present and test-referenced, but no active import/router "
                "wiring was found."
            )
        else:
            interpretation = (
                "The component is present on disk, but neither wiring nor a test reference was "
                "found. This is presence, not proof of use."
            )

        return CapabilityAnswer(
            query=query,
            status="found",
            component=node.module,
            kind="module",
            path=node.path,
            wired=node.maturity.wired,
            test_referenced=node.maturity.test_referenced,
            endpoints=node.endpoints,
            tests=node.tests,
            maturity=self._maturity_dict(node),
            evidence=(node.path, *node.tests, *node.endpoints),
            limitations=tuple(limitations),
            interpretation=interpretation,
        )

    def capability(self, component: str) -> CapabilityAnswer:
        """Return evidence for one module, or for a curated capability alias."""
        query = (component or "").strip()
        if not query:
            return CapabilityAnswer(
                query=query,
                status="not_found",
                limitations=("an empty component name cannot be resolved",),
            )

        try:
            system = self._system_map()
        except RuntimeError as exc:
            return CapabilityAnswer(
                query=query,
                status="unavailable",
                limitations=(str(exc),),
            )

        node = system.module(query)
        if node is not None:
            return self._capability_answer(query, node)

        prefixes = CAPABILITY_ALIASES.get(query.casefold(), ())
        candidates = [
            candidate
            for candidate in system.modules
            if any(
                candidate.module == prefix or candidate.module.startswith(prefix + ".")
                for prefix in prefixes
            )
        ]
        if len(candidates) == 1:
            return self._capability_answer(query, candidates[0])
        if len(candidates) > 1:
            return CapabilityAnswer(
                query=query,
                status="ambiguous",
                component=None,
                evidence=tuple(candidate.path for candidate in candidates),
                limitations=(
                    "the alias matches more than one component; choose an exact module name",
                ),
                interpretation=(
                    "Alias resolution was intentionally not guessed. Candidates: "
                    + ", ".join(candidate.module for candidate in candidates)
                ),
            )

        return CapabilityAnswer(
            query=query,
            status="not_found",
            limitations=("the component does not exist in this checkout",),
        )

    def dependencies(self, module: str) -> DependencyAnswer:
        """Return the bounded reverse-import impact for one module."""
        name = (module or "").strip()
        try:
            system = self._system_map()
        except RuntimeError as exc:
            return DependencyAnswer(
                module=name, status="unavailable", limitations=(str(exc),)
            )
        try:
            impact = system.impact(name)
        except KeyError:
            return DependencyAnswer(
                module=name,
                status="not_found",
                limitations=("the module does not exist in this checkout",),
            )

        node = system.module(name)
        evidence = tuple(
            item
            for item in (
                node.path if node is not None else None,
                *impact.endpoints,
                *impact.tests,
            )
            if item
        )
        return DependencyAnswer(
            module=name,
            status="found",
            impact=impact,
            evidence=evidence,
            limitations=impact.unknowns,
        )

    def _verified_path_ref(
        self, kind: str, ref: str
    ) -> tuple[EvidenceRef | None, str | None]:
        path_ref = ref.split("::", 1)[0].strip()
        if not path_ref:
            return None, f"{kind}:{ref} has no path"
        path = (self.repo_root / path_ref).resolve()
        try:
            path.relative_to(self.repo_root)
        except ValueError:
            return None, f"{kind}:{ref} resolves outside the repository"
        if not path.is_file():
            return None, f"{kind}:{ref} does not exist"
        if kind == "test" and "::" in ref:
            test_name = ref.split("::", 1)[1].split("[", 1)[0].strip()
            if test_name:
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    return None, f"{kind}:{ref} could not be read"
                if f"def {test_name}(" not in text:
                    return None, f"{kind}:{ref} names a test function absent from the file"
        return EvidenceRef(kind=kind, ref=ref, verified=True), None

    def _trace_evidence(
        self,
        evidence: Mapping[Stage, Sequence[EvidenceRef]] | None,
        doc_paths: Sequence[str],
        code_paths: Sequence[str],
        test_paths: Sequence[str],
        approved_ref: str | None,
        approval_verified: bool,
    ) -> tuple[dict[Stage, list[EvidenceRef]], tuple[str, ...]]:
        collected: dict[Stage, list[EvidenceRef]] = {}
        unverified: list[str] = []

        for stage, refs in (evidence or {}).items():
            for ref in refs:
                if ref.kind in {"doc", "code", "test"}:
                    verified, problem = self._verified_path_ref(ref.kind, ref.ref)
                    if verified is None:
                        unverified.append(problem or f"{ref.kind}:{ref.ref}")
                    else:
                        collected.setdefault(stage, []).append(verified)
                else:
                    collected.setdefault(stage, []).append(ref)

        for kind, refs, stage in (
            ("doc", doc_paths, Stage.requirement),
            ("code", code_paths, Stage.implementation),
            ("test", test_paths, Stage.test),
        ):
            for ref in refs:
                verified, problem = self._verified_path_ref(kind, ref)
                if verified is None:
                    unverified.append(problem or f"{kind}:{ref}")
                else:
                    collected.setdefault(stage, []).append(verified)

        if approved_ref:
            collected.setdefault(Stage.decision, []).append(
                EvidenceRef(
                    kind="founder_decision",
                    ref=approved_ref,
                    verified=approval_verified,
                )
            )

        return collected, tuple(unverified)

    def trace_requirement(
        self,
        requirement_id: str,
        title: str,
        project_ref: str,
        *,
        evidence: Mapping[Stage, Sequence[EvidenceRef]] | None = None,
        doc_paths: Sequence[str] = (),
        code_paths: Sequence[str] = (),
        test_paths: Sequence[str] = (),
        approved_ref: str | None = None,
        approval_verified: bool = False,
        rejected: bool = False,
        superseded_by: str | None = None,
        open_question: bool = False,
    ) -> TraceabilityAnswer:
        """Build a trace from refs that actually exist in this checkout.

        Path refs (`doc`, `code`, `test`) are verified against the checkout and dropped into
        `unverified_refs` when they do not exist. PR/runtime/founder-decision refs remain
        caller-supplied; they are never fetched over the network. Approval is only counted
        when the caller explicitly passes `approval_verified=True`.
        """
        if not requirement_id.strip():
            raise ValueError("requirement_id must not be empty")

        collected, unverified = self._trace_evidence(
            evidence,
            doc_paths,
            code_paths,
            test_paths,
            approved_ref,
            approval_verified,
        )
        trace = build_trace(
            requirement_id,
            title,
            project_ref,
            evidence=collected,
            rejected=rejected,
            superseded_by=superseded_by,
            open_question=open_question,
        )
        limitations = [
            "only path refs that exist in this checkout are counted as evidence",
            "PR/runtime refs are caller-supplied and are not fetched or verified over the network",
            "approval is not inferred from historical wording; it requires approval_verified=True",
        ]
        if unverified:
            limitations.append(
                "unverified refs were excluded rather than treated as implementation evidence"
            )
        return TraceabilityAnswer(
            requirement_id=requirement_id,
            status="traced" if collected else "insufficient_evidence",
            trace=trace,
            unverified_refs=unverified,
            limitations=tuple(limitations),
        )

    def assess(
        self,
        request: EngineeringRequest,
        *,
        scope: ProjectScopeReport | None = None,
        ownership: OwnershipReport | None = None,
        trace: RequirementTrace | None = None,
    ) -> ProposalAssessment:
        """Compose scope, ownership, recommendation and optional trace evidence."""
        try:
            system = self._system_map()
        except RuntimeError as exc:
            raise RuntimeError(str(exc)) from exc

        effective = request
        unresolved = list(request.unresolved_evidence)
        if scope is not None and scope.status is not ScopeStatus.isolated:
            effective = replace(
                effective,
                conflicting_sources=True,
                unresolved_evidence=tuple(unresolved + [scope.reason]),
            )
        if ownership is not None:
            if ownership.status == OWNED:
                effective = replace(
                    effective,
                    active_assignments=tuple(effective.active_assignments)
                    + ownership.evidence_refs(),
                )
            elif ownership.status == UNCLEAR:
                effective = replace(
                    effective,
                    unresolved_evidence=tuple(unresolved + [ownership.reason]),
                )

        recommendation = recommend(effective, self._facts(system))
        limitations = [
            "the recommendation uses static checkout evidence, not runtime observation",
            "no model, network call, dispatch or execution authorization is made",
        ]
        if scope is None:
            limitations.append(
                "project scope was not supplied; project boundary remains unknown"
            )
        if ownership is None:
            limitations.append(
                "ownership was not supplied; duplicate-work risk is unknown"
            )
        if trace is not None and trace.canonical_truth is not None:
            limitations.append(
                "a trace canonical_truth was supplied by a caller; this service does not verify it"
            )

        evidence = tuple(recommendation.evidence)
        if trace is not None:
            evidence += tuple(
                f"{link.stage.value}: {ref.ref}"
                for link in trace.links
                for ref in link.evidence
            )

        return ProposalAssessment(
            request_text=request.request_text,
            capability_aliases=request.capability_aliases,
            project_ref=request.project_ref,
            recommendation=recommendation,
            scope=scope,
            ownership=ownership,
            trace=trace,
            evidence=evidence,
            limitations=tuple(limitations),
        )

    def integration_status(self) -> IntegrationStatus:
        """Report local vs captured-PR state without claiming remote integration."""
        git = self._git or scan_local_git(
            self.repo_root, pull_requests_path=self.pull_requests_path
        )
        matching = tuple(
            pr
            for pr in git.pull_requests
            if pr.head_sha and git.current_sha and pr.head_sha == git.current_sha
        )
        if git.current_sha is None:
            state = "unknown"
            local_only: bool | None = None
        elif not git.pull_requests:
            state = "unknown"
            local_only = None
        elif matching:
            state = "captured_pr"
            local_only = False
        else:
            state = "not_in_captured_prs"
            local_only = True

        notes = [
            "no network call was made; PR state is limited to captured metadata",
            "a local branch/SHA is not proof of remote publication, review, merge or deployment",
        ]
        if not git.pull_requests:
            notes.append("no captured pull-request metadata; remote integration is unknown")
        if git.dirty_tracked_tree:
            notes.append(
                "tracked files are modified; HEAD does not represent the full working tree"
            )

        capture_stale: bool | None = None
        capture_age_hours: float | None = None
        if self.pull_requests_path is not None and self.pull_requests_path.is_file():
            try:
                captured_mtime = datetime.fromtimestamp(
                    self.pull_requests_path.stat().st_mtime, tz=timezone.utc
                )
            except OSError:
                captured_mtime = None
            if captured_mtime is not None:
                age = datetime.now(timezone.utc) - captured_mtime
                capture_age_hours = round(age.total_seconds() / 3600, 1)
                capture_stale = age > _PR_CAPTURE_MAX_AGE
                if capture_stale:
                    notes.append(
                        "captured pull-request metadata is "
                        f"{capture_age_hours} h old; remote state may have changed"
                    )
        unknown_state_prs = sorted({pr.number for pr in matching if pr.state == "unknown"})
        if unknown_state_prs:
            notes.append(
                "captured state is unknown for PR "
                + ", ".join(f"#{number}" for number in unknown_state_prs)
            )

        evidence = [
            f"branch: {git.current_branch or 'unknown'}",
            f"head: {git.current_sha or 'unknown'}",
            f"local branches: {len(git.local_branches)}",
        ]
        if git.pull_requests:
            evidence.append(
                "captured PRs: "
                + ", ".join(
                    f"#{pr.number} {pr.state} {pr.head_sha[:12]}"
                    for pr in git.pull_requests
                )
            )

        return IntegrationStatus(
            branch=git.current_branch,
            head_sha=git.current_sha,
            dirty_tracked_tree=git.dirty_tracked_tree,
            local_branch_count=len(git.local_branches),
            captured_pull_requests=git.pull_requests,
            matching_pull_requests=matching,
            state=state,
            local_only=local_only,
            notes=tuple(notes),
            evidence=tuple(evidence),
            capture_stale=capture_stale,
            capture_age_hours=capture_age_hours,
        )

    def recovery(
        self,
        db: Session,
        *,
        owner_id: uuid.UUID,
        repository_identity: str | None = None,
        branch: str | None = None,
    ) -> RecoveryView:
        """Recover the persisted development state, or explain why it is ambiguous."""
        report = recover_development_state(
            db,
            owner_id=owner_id,
            repository_identity=repository_identity,
            branch=branch,
        )
        state = (
            load_development_state(
                db, owner_id=owner_id, assignment_id=uuid.UUID(report.selected_assignment_id)
            )
            if report.selected_assignment_id
            else None
        )
        limitations = [
            "github_observed is false; no GitHub network call is made",
            "observed means a recent dispatch heartbeat, not an OS process probe",
            "recovery never dispatches or resumes an adapter",
        ]
        return RecoveryView(
            status=report.status,
            report=report,
            state=state,
            evidence=tuple(state.evidence) if state is not None else (),
            limitations=tuple(limitations),
        )

    def overlap(
        self,
        db: Session,
        *,
        owner_id: uuid.UUID,
        paths: Sequence[str],
    ) -> OverlapAssessment:
        """Report path-scoped assignments plus local/captured-PR evidence."""
        requested = tuple(path for path in paths if path and path.strip())
        ownership = find_work_owners(db, owner_id=owner_id, paths=requested)
        git = self._git or scan_local_git(
            self.repo_root, pull_requests_path=self.pull_requests_path
        )
        notes = [
            "a non-terminal assignment is evidence of an assignment, not proof of a running process",
            "lease fields distinguish a live lease from a stale or absent one",
            "PR evidence is captured metadata only; no network call is made",
        ]
        return OverlapAssessment(
            requested_paths=requested,
            ownership=ownership,
            git=git,
            notes=tuple(notes),
        )

    @staticmethod
    def _intent(question: str) -> str:
        q = question.casefold()
        if "security" in q or "säkerhet" in q or "authorization" in q:
            return "security"
        if "break" in q or "impact" in q or "påverkar" in q:
            return "impact"
        if "depend" in q or "beroende" in q:
            return "dependencies"
        if "tested" in q or "test" in q and "component" in q:
            return "tested"
        if "only documented" in q or "documented only" in q or "dokumenterade" in q:
            return "documented_only"
        if (
            "not been integrated" in q
            or "not integrated" in q
            or "unintegrated" in q
            or "inte integrerade" in q
        ):
            return "unintegrated"
        if "overlap" in q or "duplicate" in q or "överlapp" in q:
            return "overlap"
        if "latest" in q or "branch" in q or "senaste" in q:
            return "latest"
        if "remains" in q or "remaining" in q or "before" in q or "återstår" in q:
            return "remaining"
        if (
            "which components" in q
            or "implemented" in q
            or "capabilities" in q
            or "komponenter" in q
        ):
            return "capabilities"
        return "unknown"

    def self_knowledge(
        self,
        question: str,
        *,
        component: str | None = None,
        module: str | None = None,
    ) -> SelfKnowledgeAnswer:
        """Answer a supported self-knowledge question from deterministic evidence.

        This is an intent router over the same analyses, not free-text generation. Unsupported
        wording returns `unknown` with the supported questions rather than guessing.
        """
        intent = self._intent(question)
        try:
            system = self._system_map()
        except RuntimeError as exc:
            return SelfKnowledgeAnswer(
                question=question,
                intent=intent,
                status="unavailable",
                answer="System Intelligence could not scan this checkout.",
                facts={},
                evidence=(),
                limitations=(str(exc),),
                interpretation=None,
            )

        base_limitations = (
            "static checkout analysis; runtime activation and merge state are not observed",
            "test references are file mentions, not coverage",
        )
        focus = module or component

        if intent == "capabilities":
            wired = [node for node in system.modules if node.maturity.wired]
            tested = [node for node in system.modules if node.maturity.test_referenced]
            unwired = [node for node in system.modules if not node.maturity.wired]
            facts = {
                "modules": len(system.modules),
                "wired": len(wired),
                "test_referenced": len(tested),
                "unwired": len(unwired),
                "examples": [node.module for node in wired[:10]],
            }
            return SelfKnowledgeAnswer(
                question=question,
                intent=intent,
                status="answered",
                answer=(
                    f"{len(system.modules)} modules are present; {len(wired)} are wired and "
                    f"{len(tested)} are named by at least one test file."
                ),
                facts=facts,
                evidence=tuple(node.path for node in wired[:10]),
                limitations=base_limitations,
                interpretation=(
                    "Wiring and test references are static facts. They do not prove that a "
                    "request reaches the code or that the behaviour is covered."
                ),
            )

        if intent == "tested":
            tested = [node for node in system.modules if node.maturity.test_referenced]
            modules = [node.module for node in tested]
            facts = {
                "test_referenced": len(tested),
                "modules": modules[:50],
                "truncated": len(modules) > 50,
            }
            evidence = tuple(
                dict.fromkeys(path for node in tested for path in node.tests)
            )[:50]
            return SelfKnowledgeAnswer(
                question=question,
                intent=intent,
                status="answered",
                answer=f"{len(tested)} modules are named by at least one test file.",
                facts=facts,
                evidence=evidence,
                limitations=base_limitations,
                interpretation=(
                    "A name reference is not a guarantee that the test exercises the module."
                ),
            )

        if intent == "documented_only":
            candidates = [
                node
                for node in system.modules
                if not node.maturity.wired and not node.maturity.test_referenced
            ]
            modules = [node.module for node in candidates]
            facts = {
                "present_without_wiring_or_test_reference": len(candidates),
                "modules": modules[:50],
                "truncated": len(modules) > 50,
            }
            return SelfKnowledgeAnswer(
                question=question,
                intent=intent,
                status="answered",
                answer=(
                    f"{len(candidates)} modules are present but neither wired nor named by a "
                    "test file."
                ),
                facts=facts,
                evidence=tuple(node.path for node in candidates)[:50],
                limitations=(
                    "presence without wiring/test reference is not proof that a module is "
                    "documented-only; it may be dead code or used through indirection",
                )
                + base_limitations,
                interpretation=(
                    "Treat this as a review queue for documentation/dead-code classification, "
                    "not as a completed classification."
                ),
            )

        if intent == "unintegrated":
            status = self.integration_status()
            facts = status.to_dict()
            return SelfKnowledgeAnswer(
                question=question,
                intent=intent,
                status="answered",
                answer=(
                    f"Local HEAD {status.head_sha or 'unknown'} on "
                    f"{status.branch or 'unknown'}; captured-PR state is {status.state}."
                ),
                facts=facts,
                evidence=status.evidence,
                limitations=status.notes,
                interpretation=(
                    "Local implementation and remote integration are separate states. "
                    "Only captured PR metadata can show a remote match; no merge is claimed."
                ),
            )

        if intent == "latest":
            status = self.integration_status()
            facts = status.to_dict()
            return SelfKnowledgeAnswer(
                question=question,
                intent=intent,
                status="answered",
                answer=(
                    f"The local checkout is at {status.head_sha or 'unknown'} on "
                    f"{status.branch or 'unknown'}. I cannot prove which branch is 'verified'."
                ),
                facts=facts,
                evidence=status.evidence,
                limitations=(
                    "verified/reviewed/merged/deployed are not observable from a checkout",
                )
                + status.notes,
                interpretation=(
                    "The latest local commit is evidence of local state, not of review or "
                    "integration."
                ),
            )

        if intent == "overlap":
            consistency = check_consistency(system, repo_root=self.repo_root)
            facts = consistency.to_dict()
            return SelfKnowledgeAnswer(
                question=question,
                intent=intent,
                status="answered",
                answer=(
                    f"{len(consistency.findings)} deterministic overlap/consistency findings "
                    "were found."
                ),
                facts=facts,
                evidence=tuple(
                    finding.evidence
                    for finding in consistency.findings
                    if getattr(finding, "evidence", None)
                ),
                limitations=(
                    "only five deterministic checks are performed; semantic overlap is not inferred",
                )
                + base_limitations,
                interpretation=(
                    "Zero findings means these checks found nothing, not that the architecture "
                    "is free of duplication."
                ),
            )

        if intent in {"dependencies", "impact", "security", "remaining"}:
            if not focus:
                return SelfKnowledgeAnswer(
                    question=question,
                    intent=intent,
                    status="needs_input",
                    answer="Name a module (or component) to answer this question.",
                    facts={"supported_questions": list(_SUPPORTED_QUESTIONS)},
                    evidence=(),
                    limitations=("a module name is required for dependency/impact evidence",),
                    interpretation=None,
                )
            dependency = self.dependencies(focus)
            if dependency.status != "found" or dependency.impact is None:
                return SelfKnowledgeAnswer(
                    question=question,
                    intent=intent,
                    status="unavailable" if dependency.status == "unavailable" else "unknown",
                    answer=f"No dependency evidence was found for {focus}.",
                    facts=dependency.to_dict(),
                    evidence=dependency.evidence,
                    limitations=dependency.limitations,
                    interpretation=None,
                )
            impact = dependency.impact
            if intent == "security":
                facts = {
                    "module": focus,
                    "security_notes": list(impact.security_notes),
                    "endpoints": list(impact.endpoints),
                }
                answer = "Security-relevant static evidence: " + "; ".join(
                    impact.security_notes
                )
            elif intent == "remaining":
                node = system.module(focus)
                facts = {
                    "module": focus,
                    "wired": node.maturity.wired if node else None,
                    "test_referenced": node.maturity.test_referenced if node else None,
                    "integration": self.integration_status().to_dict(),
                    "unknowns": list(impact.unknowns),
                }
                answer = (
                    "Remaining checks: wiring/test status, runtime activation, review and "
                    "integration evidence. Static unknowns are listed in `facts`."
                )
            else:
                facts = dependency.to_dict()
                answer = (
                    f"{focus} has {len(impact.direct_dependents)} direct and "
                    f"{len(impact.transitive_dependents)} transitive dependents in the static "
                    "import graph."
                )
            return SelfKnowledgeAnswer(
                question=question,
                intent=intent,
                status="answered",
                answer=answer,
                facts=facts,
                evidence=dependency.evidence,
                limitations=dependency.limitations,
                interpretation=(
                    "Static reverse imports and endpoint references are evidence of potential "
                    "impact, not a runtime call graph."
                ),
            )

        return SelfKnowledgeAnswer(
            question=question,
            intent=intent,
            status="unknown",
            answer="This question is not one of the supported deterministic intents.",
            facts={"supported_questions": list(_SUPPORTED_QUESTIONS)},
            evidence=(),
            limitations=("free-text inference is deliberately not used",),
            interpretation=None,
        )

    _INSTRUCTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        (
            "ignore_previous",
            re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions", re.I),
        ),
        ("role_override", re.compile(r"\b(?:system|developer)\s*:", re.I)),
        (
            "privileged_action",
            re.compile(
                r"\b(?:execute|run|deploy|merge|push|delete|drop|truncate|grant|revoke)\b",
                re.I,
            ),
        ),
        (
            "credential_request",
            re.compile(r"\b(?:api[_-]?key|password|token|secret|credential)\b", re.I),
        ),
        ("network_egress", re.compile(r"https?://", re.I)),
    )

    def inspect_untrusted_source(
        self, source_ref: str, text: str
    ) -> UntrustedSourceReport:
        """Classify retrieved text as data, never as an instruction.

        This is a deterministic boundary helper for callers that are about to pass retrieved
        source text to a model or a planner. It does not execute, fetch, dispatch or grant
        anything; it only reports instruction-like patterns so the caller can cite/review/ask.
        """
        matches = tuple(
            name for name, pattern in self._INSTRUCTION_PATTERNS if pattern.search(text)
        )
        return UntrustedSourceReport(
            source_ref=source_ref,
            treated_as_data=True,
            instruction_like=bool(matches),
            matched_patterns=matches,
            allowed_actions=("cite", "review", "ask"),
            note=(
                "retrieved source text is untrusted data; it cannot authorize execution, "
                "change system instructions, or cause dispatch"
            ),
        )

    def snapshot(self) -> dict:
        """Small machine-readable status payload for an existing service or health check."""
        try:
            system = self._system_map()
            capability_map = self._capability_map
        except RuntimeError as exc:
            return {
                "status": "unavailable",
                "error": str(exc),
                "warnings": list(self._warnings),
                "execution_authorized": False,
            }
        return {
            "status": "ok",
            "system_counts": dict(system.counts),
            "capability_counts": (
                dict(capability_map.counts) if capability_map is not None else {}
            ),
            "migrations_head": system.migrations_head,
            "integration": self.integration_status().to_dict(),
            "warnings": list(self._warnings),
            "execution_authorized": False,
        }


def get_system_intelligence(
    repo_root: Path | str | None = None,
    *,
    pull_requests_path: Path | str | None = None,
) -> SystemIntelligence:
    """Factory used by callers that do not want to manage the service lifetime."""
    return SystemIntelligence(repo_root, pull_requests_path=pull_requests_path)


__all__ = [
    "CapabilityAnswer",
    "DependencyAnswer",
    "IntegrationStatus",
    "OverlapAssessment",
    "ProposalAssessment",
    "RecoveryView",
    "SelfKnowledgeAnswer",
    "SystemIntelligence",
    "TraceabilityAnswer",
    "UntrustedSourceReport",
    "default_repo_root",
    "get_system_intelligence",
]
