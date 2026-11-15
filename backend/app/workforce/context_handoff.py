"""Token-efficient agent task-context handoff (first-start increment).

Founder directive (2026-11-09, "Token-Efficient Agent Context Broker"): give a development agent
*just enough* source-grounded context to start a scoped task without re-reading the whole
repository, with full auditability and no redundant tokens.

THIS MODULE IS A BOUNDED INCREMENT, NOT A NEW SUBSYSTEM. The existing layer already provides:
  - `app.workforce.context.create_context_package` / `minimize_for_trust_zone` — disclosure
    minimization + least-privilege (credentials/vault/api_key always denied; external zones deny
    more) and the owner-scoped, content-fingerprinted `WorkforceContextPackage`.
  - `app.workforce.broker.ingest_untrusted_result` / `mark_verification` — agent results come
    back as UNTRUSTED evidence that must be independently verified (not trusted instructions).
  - `app.workforce.injection` — authority/secret scrub of inbound agent output.
  - `app.egress_policy.enforce_egress_policy` — the AUTHORITATIVE outbound secret/NEVER_EGRESS
    gate, called at real transmission time.

What those did NOT yet carry, and this module adds — PURELY, with no new table/migration and no
change to the above — are the four distinctive, independently-testable pieces the directive
names:
  1. SHA-bound, integrity-verifiable source references (an agent can re-fetch `path@base_sha`
     and confirm the exact bytes before trusting/editing).
  2. Explicit expiry + stale-context detection (regenerate when the base SHA moved or TTL
     elapsed) — acceptance criterion 5.
  3. Continuation delta (send only changed fields vs an authenticated prior version, always
     carrying immutable identifiers and the safety fields) — acceptance criterion 4.
  4. Token measurement vs a full-context baseline — acceptance criterion 8.

Life owns the canonical knowledge; this only SHAPES an already-minimized, owner-scoped packet for
an execution interface. It never stores a second memory DB, never widens authority, and reuses
the existing security primitives rather than reimplementing them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.workforce.context import minimize_for_trust_zone

# Public, documented sentinel (a convention, NOT a copy of egress-policy-private logic): a packet
# carrying it must never be transmitted. The authoritative outbound gate remains
# `app.egress_policy.enforce_egress_policy`, called at real transmission time; this is a cheap,
# pure pre-check so a handoff is refused before it ever reaches that gate.
NEVER_EGRESS_SENTINEL = "NEVER_EGRESS:"

DEFAULT_TTL_SECONDS = 3600

# Safety fields that a token optimization (delta/minimization) must NEVER drop — acceptance
# criterion 9: "A context optimization never bypasses security or execution approvals."
_ALWAYS_PRESENT_SAFETY_FIELDS = ("acceptance_criteria", "constraints", "approval_limits")


class ContextHandoffError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def estimate_tokens(text: str) -> int:
    """Deterministic, provider-agnostic token estimate (~4 chars/token). Good enough to MEASURE
    and compare prompt sizes without calling any tokenizer/provider."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def full_context_baseline_tokens(full_source_texts: list[str]) -> int:
    """The token cost of the naive 'dump everything' baseline this broker exists to beat:
    every full source text an agent would otherwise re-read."""
    return sum(estimate_tokens(t) for t in full_source_texts)


def _assert_no_never_egress(values: list[str | None]) -> None:
    for v in values:
        if isinstance(v, str) and NEVER_EGRESS_SENTINEL in v:
            raise ContextHandoffError(
                "refusing to build an agent context packet containing a NEVER_EGRESS-marked "
                "value (authoritative gate: app.egress_policy.enforce_egress_policy)"
            )


@dataclass(frozen=True)
class SourceRef:
    """An integrity-verifiable reference to a bounded slice of a source file at an EXACT SHA.
    The agent re-fetches `path@base_sha` and confirms `content_sha256` before trusting/editing —
    a reference alone is never assumed sufficient if the content cannot be verified."""

    path: str
    base_sha: str
    content_sha256: str
    start_line: int | None = None
    end_line: int | None = None
    excerpt: str | None = None  # bounded; None = reference-only (fetch on demand)

    @classmethod
    def from_content(
        cls,
        *,
        path: str,
        base_sha: str,
        content: bytes | str,
        start_line: int | None = None,
        end_line: int | None = None,
        include_excerpt: bool = True,
        max_excerpt_chars: int = 2000,
    ) -> "SourceRef":
        text = content.decode("utf-8", "ignore") if isinstance(content, bytes) else content
        excerpt = None
        if include_excerpt:
            excerpt = text if len(text) <= max_excerpt_chars else text[:max_excerpt_chars]
        return cls(
            path=path,
            base_sha=base_sha,
            content_sha256=_sha256_hex(content),
            start_line=start_line,
            end_line=end_line,
            excerpt=excerpt,
        )

    def verify(self, current_content: bytes | str) -> bool:
        """True iff the agent's freshly-fetched bytes match the exact content this ref was built
        from — proving the source has not drifted from the SHA the context was generated against."""
        return _sha256_hex(current_content) == self.content_sha256

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "base_sha": self.base_sha,
            "content_sha256": self.content_sha256,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True)
class AgentTaskContext:
    """The compact, source-grounded packet handed to an agent before delegation. Only fields
    relevant to THIS assignment; never a dump of whole documents or histories."""

    task_id: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    repo: str
    branch: str
    base_sha: str
    generated_at: str
    expires_at: str
    source_refs: tuple[SourceRef, ...] = ()
    minimized_items: tuple[dict, ...] = ()  # disclosure-minimized extras (see build_* below)
    decisions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    prior_attempts: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    ownership_boundaries: tuple[str, ...] = ()
    tests_ci: tuple[str, ...] = ()
    approval_limits: tuple[str, ...] = ()
    missing_info: tuple[str, ...] = ()
    uncertainty: tuple[str, ...] = ()
    context_version: int = 1

    def is_stale(self, *, now: datetime | None = None, current_base_sha: str | None = None) -> bool:
        """Stale when the repository has moved past the SHA this context was bound to, or the TTL
        has elapsed — either condition must trigger revalidation before the agent acts."""
        now = now or _utcnow()
        if current_base_sha is not None and current_base_sha != self.base_sha:
            return True
        return now >= datetime.fromisoformat(self.expires_at)

    def to_transmittable_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "repo": self.repo,
            "branch": self.branch,
            "base_sha": self.base_sha,
            "generated_at": self.generated_at,
            "expires_at": self.expires_at,
            "context_version": self.context_version,
            "source_refs": [r.to_dict() for r in self.source_refs],
            "minimized_items": list(self.minimized_items),
            "decisions": list(self.decisions),
            "constraints": list(self.constraints),
            "prior_attempts": list(self.prior_attempts),
            "dependencies": list(self.dependencies),
            "ownership_boundaries": list(self.ownership_boundaries),
            "tests_ci": list(self.tests_ci),
            "approval_limits": list(self.approval_limits),
            "missing_info": list(self.missing_info),
            "uncertainty": list(self.uncertainty),
        }

    def estimated_tokens(self) -> int:
        import json

        return estimate_tokens(json.dumps(self.to_transmittable_dict(), sort_keys=True, default=str))

    def delta_to(self, nxt: "AgentTaskContext") -> dict:
        """Continuation: only the fields that actually changed from THIS version to `nxt`, plus
        the immutable identifiers needed for safe validation, plus the safety fields that an
        optimization may never silently drop (acceptance criterion 9). If a prior authenticated
        context exists, an agent receives only valid changes (acceptance criterion 4)."""
        if nxt.task_id != self.task_id:
            raise ContextHandoffError("delta across different task_id is not valid")
        this_d = self.to_transmittable_dict()
        next_d = nxt.to_transmittable_dict()
        changed = {k: v for k, v in next_d.items() if this_d.get(k) != v}
        # Always carry immutable identifiers for safe validation...
        changed.setdefault("task_id", nxt.task_id)
        changed["base_sha"] = nxt.base_sha
        changed["context_version"] = nxt.context_version
        changed["prev_context_version"] = self.context_version
        # ...and never omit the safety fields, even when unchanged.
        for safety_field in _ALWAYS_PRESENT_SAFETY_FIELDS:
            changed[safety_field] = next_d[safety_field]
        return changed


def build_agent_task_context(
    *,
    task_id: str,
    objective: str,
    acceptance_criteria: list[str],
    repo: str,
    branch: str,
    base_sha: str,
    trust_zone: str,
    source_refs: list[SourceRef] | None = None,
    requested_items: list[dict] | None = None,
    decisions: list[str] | None = None,
    constraints: list[str] | None = None,
    prior_attempts: list[str] | None = None,
    dependencies: list[str] | None = None,
    ownership_boundaries: list[str] | None = None,
    tests_ci: list[str] | None = None,
    approval_limits: list[str] | None = None,
    missing_info: list[str] | None = None,
    uncertainty: list[str] | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    context_version: int = 1,
    now: datetime | None = None,
) -> AgentTaskContext:
    """Assemble a compact, source-grounded, security-checked packet.

    Security (criterion 6), by REUSE not reimplementation:
      - extra `requested_items` pass through `app.workforce.context.minimize_for_trust_zone`,
        which denies credential/vault/api_key kinds (and more for external zones); denied kinds
        are recorded in `missing_info` as "withheld:<kind>" rather than silently dropped.
      - every string value is scanned for the NEVER_EGRESS sentinel and the build is refused if
        present (the authoritative durable gate remains enforce_egress_policy at transmit time).
    A token optimization may never drop acceptance criteria (criterion 9): an empty
    acceptance_criteria is refused here.
    """
    if not acceptance_criteria:
        raise ContextHandoffError(
            "acceptance_criteria is mandatory — a context may never be trimmed to save tokens "
            "by dropping what 'done' means"
        )

    accepted_items, denied_kinds = minimize_for_trust_zone(
        trust_zone=trust_zone, requested_items=list(requested_items or [])
    )
    missing = list(missing_info or [])
    missing.extend(f"withheld:{kind}" for kind in denied_kinds)

    refs = tuple(source_refs or ())
    # Security pre-check across all free-text + excerpts.
    _assert_no_never_egress(
        [objective, *acceptance_criteria, *(decisions or []), *(constraints or []),
         *(prior_attempts or []), *(dependencies or []), *(ownership_boundaries or []),
         *(approval_limits or []), *(uncertainty or []), *missing]
        + [r.excerpt for r in refs]
        + [str(item.get("excerpt")) for item in accepted_items]
    )

    now = now or _utcnow()
    return AgentTaskContext(
        task_id=task_id,
        objective=objective,
        acceptance_criteria=tuple(acceptance_criteria),
        repo=repo,
        branch=branch,
        base_sha=base_sha,
        generated_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
        source_refs=refs,
        minimized_items=tuple(accepted_items),
        decisions=tuple(decisions or ()),
        constraints=tuple(constraints or ()),
        prior_attempts=tuple(prior_attempts or ()),
        dependencies=tuple(dependencies or ()),
        ownership_boundaries=tuple(ownership_boundaries or ()),
        tests_ci=tuple(tests_ci or ()),
        approval_limits=tuple(approval_limits or ()),
        missing_info=tuple(missing),
        uncertainty=tuple(uncertainty or ()),
        context_version=context_version,
    )


# --- Progressive, bounded, AUTHORIZED retrieval (directive §2, acceptance criterion 3) --------

@dataclass(frozen=True)
class BoundedRetrievalRequest:
    """An agent's request for *additional* context — always bounded to an explicit path (and
    optional line range) at an explicit SHA. The agent never gets raw repo access; retrieval is
    fulfilled through an injected AUTHORIZED fetcher."""

    path: str
    base_sha: str
    start_line: int | None = None
    end_line: int | None = None


def fulfill_bounded_retrieval(
    request: BoundedRetrievalRequest,
    *,
    authorized_fetch: Callable[[str, str], bytes | str],
    allowed_paths: frozenset[str] | None = None,
) -> SourceRef:
    """Fulfill a bounded retrieval through an authorized fetcher, returning an integrity-verifiable
    SourceRef. If `allowed_paths` is given, a path outside it is refused (least privilege). The
    fetcher is the ONLY source of bytes — this function never opens the repo itself."""
    if allowed_paths is not None and request.path not in allowed_paths:
        raise ContextHandoffError(f"bounded retrieval refused: '{request.path}' is outside the authorized scope")
    content = authorized_fetch(request.path, request.base_sha)
    ref = SourceRef.from_content(
        path=request.path,
        base_sha=request.base_sha,
        content=content,
        start_line=request.start_line,
        end_line=request.end_line,
    )
    _assert_no_never_egress([ref.excerpt])
    return ref


# --- Results come back as UNTRUSTED structured evidence (directive §4, criterion 7) -----------

@dataclass(frozen=True)
class AgentResultRecord:
    """A structured agent outcome. By construction `verified=False` until Life independently
    confirms it against external evidence (a real commit/PR/CI reference) — an agent's own claim
    is never trusted as fact. Persistence goes through the EXISTING canonical path
    (`app.workforce.broker.ingest_untrusted_result` / founder/inspectable memory); this is the
    in-memory contract, not a second store."""

    task_id: str
    attempted: str
    changed_files: tuple[str, ...] = ()
    commit_sha: str | None = None
    pr_ref: str | None = None
    tests_passed: bool | None = None
    discoveries: tuple[str, ...] = ()
    failed_approaches: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    decisions_needing_founder: tuple[str, ...] = ()
    verified: bool = False
    verification_evidence: str | None = None

    @classmethod
    def from_agent_claim(cls, *, task_id: str, attempted: str, **kwargs) -> "AgentResultRecord":
        kwargs.pop("verified", None)
        kwargs.pop("verification_evidence", None)
        return cls(task_id=task_id, attempted=attempted, verified=False, **kwargs)

    def mark_verified(self, *, evidence_ref: str) -> "AgentResultRecord":
        """Only an explicit external evidence reference (e.g. a CI run / commit SHA Life itself
        checked) may promote a claim to verified — never the agent's own say-so."""
        if not evidence_ref:
            raise ContextHandoffError("verification requires an explicit external evidence reference")
        return replace(self, verified=True, verification_evidence=evidence_ref)
