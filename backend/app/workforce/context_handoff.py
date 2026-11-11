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
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.workforce.context import (
    EXTERNALISH_ZONES,
    classify_trust_zone,
    minimize_for_trust_zone,
)
from app.workforce.injection import looks_like_prompt_injection

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


# Material that must NEVER leave the vault, checked on EVERY nested string field (a sentinel scan
# alone is insufficient): the NEVER_EGRESS marker and private-key block headers.
_HARD_DENY_MARKERS = (
    NEVER_EGRESS_SENTINEL,
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
)


def _iter_strings(value):
    """Yield every nested string in a dict/list/scalar — so a scan covers ALL lesson fields, not
    just a top-level sentinel check."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_strings(v)


# Structured, controlled-vocabulary fields a lesson summary carries (see
# integration_intelligence.summarize_integration_lesson) that are not free text and therefore
# need no per-zone prose disclosure decision (they are still subject to the hard-deny scan).
_STRUCTURED_LESSON_FIELDS = (
    "lesson_id",
    "severity",
    "confidence",
    "status",
    "fix_status",
    "has_regression_test",
    # Founder decision #380 (2026-11-10): names whether CANONICAL EXECUTABLE evidence backs the
    # verification claim ("executable_regression_run") or it is merely stored/self-asserted
    # ("self_asserted"). Controlled vocabulary, no free text — safe to pass through.
    "verification_evidence",
)

# Free-text / provenance-bearing fields named explicitly in the 2026-11-10 independent-review
# handoff (PR #380, SHA 7ea2234 and f39a119 FAILs): each one gets an EXPLICIT, individual
# disclosure decision below — never a blanket `**lesson` copy, which was the root cause both
# prior remediation attempts left in place.
#
# Priority-1 fix (founder architecture decision, PR #380 third FAIL): NOT every restricted field
# carries the same disclosure risk. `symptom`/`root_cause`/`fix`/`affected_component`/`source_ref`/
# `applies_to` are free-form incident narrative and provenance -- they can describe a specific
# private situation, mention other projects, or simply be too open-ended to safety-review
# per-field -- so they are NEVER sent to an external zone, full stop, matching the founder's own
# "fail closed and omit sensitive content" instruction: `EngineeringLesson` carries no
# owner/project/disclosure-approval metadata today, so there is nothing that could ever authorize
# sending narrative content externally. `general_rule`/`regression_test` are different in KIND,
# not just degree: `general_rule` is a short, author-distilled technical rule (not an incident
# retelling), and `regression_test` is already independently validated elsewhere in this
# codebase as a structurally-safe repository-relative path
# (`app.mainai_execution.verify.validate_targeted_tests_target`, reused via
# `_regression_target_is_allowlist_shaped`) -- exactly the "minimal approved technical rule or
# regression requirement" the founder's directive asks to prefer over a full historical
# incident. They may reach an external zone, but ONLY after their OWN individual disclosure
# check (same hard-deny/injection scan every other field gets, PLUS the allowlist-shape check for
# `regression_test`, PLUS a hard length cap so "general_rule" can never be used to smuggle a full
# narrative past the other fields' withholding). `applies_to` tags are explicitly NOT treated as
# a disclosure grant anywhere in this module -- matching a task's tags only ever affected
# RETRIEVAL (`app.mainai_execution.lessons.lookup_lessons`), never whether content may leave the
# process once retrieved.
_NARRATIVE_LESSON_FIELDS = ("affected_component", "symptom", "root_cause", "fix", "source_ref", "applies_to")
_MINIMAL_TECHNICAL_LESSON_FIELDS = ("general_rule", "regression_test")
_MAX_MINIMAL_TECHNICAL_FIELD_LEN = 400

# verification_status / verified are CLAIMS, never facts a caller-supplied dict may assert —
# see _clamp_verification_claim below. Listed separately so they are never copied verbatim from
# the input dict under any code path.
_VERIFICATION_CLAIM_FIELDS = ("verification_status", "verified")


def _revalidate_lesson_against_db(db: Session | None, lesson_id: str) -> tuple[str, dict | None]:
    """Re-fetch the CANONICAL `EngineeringLesson` row by id and rebuild its summary fresh FROM
    THE DATABASE — the only authority allowed to assert this lesson's content or verification
    state. A caller-supplied dict (as `build_agent_task_context(integration_lessons=...)`
    receives) can claim anything; this is what actually confirms it.

    Returns (confirmation, canonical_summary_or_None):
      - ("confirmed_active", summary)   — the id resolves to a currently-active row; `summary`
        is a FRESH `summarize_integration_lesson()` result that supersedes the caller's dict.
      - ("confirmed_inactive", None)    — the id resolves to a row that is no longer `active`
        (historical/superseded/disputed): the caller's dict must never be treated as current
        guidance, regardless of what it claims — the lesson is excluded entirely by the caller.
      - ("unconfirmed", None)           — no `db` was given, the id is not a real lesson UUID, or
        no such row exists. The caller's dict is neither authenticated nor refuted; its content
        may still flow (subject to every other check below) but any verification CLAIM in it
        must be clamped down, never trusted at face value.
    """
    if db is None:
        return "unconfirmed", None
    try:
        lid = uuid.UUID(str(lesson_id))
    except (ValueError, TypeError, AttributeError):
        return "unconfirmed", None

    from sqlalchemy import select

    from app.models.mainai_execution import EngineeringLesson, EngineeringLessonStatus

    row = db.execute(select(EngineeringLesson).where(EngineeringLesson.id == lid)).scalar_one_or_none()
    if row is None:
        return "unconfirmed", None
    if row.status != EngineeringLessonStatus.active:
        return "confirmed_inactive", None

    from app.mainai_execution.integration_intelligence import summarize_integration_lesson

    return "confirmed_active", summarize_integration_lesson(row, db=db)


def _regression_target_is_allowlist_shaped(target) -> bool:
    """A `regression_test` target must be a relative, non-escaping path — the SAME structural
    allowlist check already enforced at verification-plan time
    (`app.mainai_execution.verify.validate_targeted_tests_target`, reused here rather than
    reinvented). This proves the target is SHAPED like a real repository test path; it does
    NOT by itself prove the test was ever executed or passed — see `_clamp_verification_claim`'s
    docstring for the gap this still leaves open."""
    from app.mainai_execution.verify import VerificationStepError, validate_targeted_tests_target

    try:
        validate_targeted_tests_target(target)
        return True
    except VerificationStepError:
        return False


def _has_executed_regression_run_evidence(effective: dict, *, db: Session | None) -> bool:
    """FOUNDER DECISION #380 (2026-11-10), evidence-based resolution. Delegates to the real,
    MainAITaskEvent-backed check in
    `app.mainai_execution.integration_intelligence._has_executed_regression_run_evidence` (see
    that function's docstring for the full mechanism and the founder's 7 verification
    requirements it satisfies) — kept as a single implementation, never duplicated, so this
    packet-level gate and the direct-summary path (`summarize_integration_lesson`) can never
    silently diverge on what counts as proof."""
    from app.mainai_execution.integration_intelligence import (
        _has_executed_regression_run_evidence as _real_check,
    )

    return _real_check(db, lesson_id=effective.get("lesson_id"), regression_test=effective.get("regression_test"))


def _clamp_verification_claim(effective: dict, *, confirmation: str, db: Session | None = None) -> tuple[dict, bool]:
    """Never let a `verified` / `verification_status` CLAIM in a lesson dict stand on its own
    say-so (independent-review handoff requirement 3c: "no caller/external agent can self-assert
    verification_status/verified"), AND never present a lesson as regression-proven without
    CANONICAL EXECUTABLE evidence (Founder decision #380, evidence-based resolution). The claim
    may stand ONLY when ALL of:
      1. `confirmation == "confirmed_active"` — `_revalidate_lesson_against_db` independently
         re-derived THIS summary from the live, active `EngineeringLesson` row itself (so the
         claim was never taken from the caller-supplied dict at all);
      2. the freshly re-derived `verification_status` is exactly `"verified_by_regression_test"`;
      3. its `regression_test` target is allowlist-shaped (see `_regression_target_is_allowlist_shaped`);
      4. an EXECUTED regression-run record backs it (`_has_executed_regression_run_evidence`,
         delegating to the real `MainAITaskEvent`-based check in
         `app.mainai_execution.integration_intelligence` — a genuine `lessons_applied` +
         `verification_passed` trail for THIS exact lesson id, never a stored column alone).
    Anything else is clamped down to `verified=False` / `verification_status="unverified"` —
    including a lesson this function cannot reach a DB for at all (`confirmation ==
    "unconfirmed"`, e.g. no `db` was passed) — never clamped UP.

    Conditions 1–3 only prove the lesson's OWN stored `verification_status` column says
    `verified_by_regression_test` and that its `regression_test` target is a structurally valid
    path — they do NOT by themselves prove the test for this exact lesson version ran and passed
    (a lesson's `verification_status` column can still be set post-hoc by any code path with DB
    write access without ever running a test — see the test fixtures' `_record()` helper). Only
    condition 4's real event-trail check supplies that proof. Returns
    (effective_with_clamped_fields, claim_was_unconfirmed)."""
    status_claim = effective.get("verification_status")
    verified_claim = bool(effective.get("verified"))
    allowlisted = bool(effective.get("regression_test")) and _regression_target_is_allowlist_shaped(
        effective.get("regression_test")
    )
    can_stand = (
        confirmation == "confirmed_active"
        and status_claim == "verified_by_regression_test"
        and allowlisted
        and _has_executed_regression_run_evidence(effective, db=db)
    )
    claim_was_unconfirmed = (not can_stand) and (verified_claim or status_claim not in (None, "unverified"))
    clamped = {
        **effective,
        "verified": bool(can_stand),
        "verification_status": "verified_by_regression_test" if can_stand else "unverified",
        # Never present a stored/self-asserted claim as regression-proven (Founder decision #380):
        # the proven fix label is reserved for executed-run evidence; otherwise reflect the honest
        # state. A lesson that merely CLAIMS regression validation is marked unproven, not proposed.
        "verification_evidence": "executable_regression_run" if can_stand else "self_asserted",
        "fix_status": (
            "validated_by_regression_test"
            if can_stand
            else ("claimed_validated_unproven" if status_claim == "verified_by_regression_test" else "proposed")
        ),
    }
    return clamped, claim_was_unconfirmed


def _field_is_injection_or_deny_shaped(value) -> bool:
    field_strings = list(_iter_strings(value))
    if any(any(m in s for m in _HARD_DENY_MARKERS) for s in field_strings):
        return True
    return any(looks_like_prompt_injection(s) for s in field_strings)


def _minimize_lesson_fields(effective: dict, *, external: bool) -> tuple[dict, list[str]]:
    """Give EVERY restricted, free-text/provenance-bearing lesson field (the exact list named in
    the independent-review handoff) an explicit, individual disclosure decision — never a
    blanket `**lesson` copy, which is the bug both prior remediation attempts (SHA 7ea2234 and
    f39a119) left in place at this choke point.

    - `_NARRATIVE_LESSON_FIELDS` (incident narrative + provenance): for an external/untrusted
      zone, WITHHELD OUTRIGHT, unconditionally (never partial-redacted-and-guessed — the same
      fail-closed posture `app.egress_policy.enforce_egress_policy`'s own docstring already
      documents: "refuse the field, never guess how much of it is safe to keep"). There is no
      metadata on `EngineeringLesson` today that could ever prove a specific narrative field is
      safe for an external audience (founder architecture decision, PR #380 third FAIL) — so
      "cannot prove safe" always means "omit," never "send and hope."
    - `_MINIMAL_TECHNICAL_LESSON_FIELDS` (`general_rule`, `regression_test`): the founder's own
      "prefer a minimal approved technical rule... over exposing a full historical incident" —
      these are structurally different from narrative (a distilled rule / an
      already-independently-validated repository-relative test path, never a story), so they get
      their OWN per-field check even for an external zone: hard-deny/injection scan (same as
      internal), a hard length cap (so this can't become a side door for a full narrative dump),
      and — for `regression_test` specifically — the same structural path-allowlist check
      `apply_lessons_to_verification_plan` already enforces at plan time
      (`_regression_target_is_allowlist_shaped`).
    - Internal zones: EVERY restricted field (narrative and minimal-technical alike) is withheld
      individually only if ITS OWN value carries a hard-deny marker or injection-shaped text —
      "untrusted data" is never conflated with "safe to keep verbatim", even for a trusted
      internal audience.
    - `applies_to` (a narrative-tier field) is explicitly NEVER treated as a disclosure grant by
      this function or any caller of it — matching a task's tags only ever affects RETRIEVAL
      (`app.mainai_execution.lessons.lookup_lessons`), never whether content may leave the
      process once retrieved.

    Structured, controlled-vocabulary fields and the already-clamped verification-claim fields
    pass through unchanged (they carry no free text). Returns (field_dict, withheld_field_names).
    """
    out: dict = {}
    for field in _STRUCTURED_LESSON_FIELDS + _VERIFICATION_CLAIM_FIELDS:
        out[field] = effective.get(field)

    withheld_fields: list[str] = []

    for field in _NARRATIVE_LESSON_FIELDS:
        value = effective.get(field)
        if value in (None, "", []):
            out[field] = value
            continue
        if external:
            deny = True
        else:
            deny = _field_is_injection_or_deny_shaped(value)
        if deny:
            withheld_fields.append(field)
            out[field] = None
        else:
            out[field] = value

    for field in _MINIMAL_TECHNICAL_LESSON_FIELDS:
        value = effective.get(field)
        if value in (None, "", []):
            out[field] = value
            continue
        deny = _field_is_injection_or_deny_shaped(value)
        if not deny and external:
            too_long = len(str(value)) > _MAX_MINIMAL_TECHNICAL_FIELD_LEN
            unsafe_target = field == "regression_test" and not _regression_target_is_allowlist_shaped(value)
            deny = too_long or unsafe_target
        if deny:
            withheld_fields.append(field)
            out[field] = None
        else:
            out[field] = value

    return out, withheld_fields


def _sanitize_integration_lessons(
    lessons, *, trust_zone: str, db: Session | None = None
) -> tuple[list[dict], list[str]]:
    """Trust-zone + authorization filter for integration lessons entering a task packet.

    Integration lessons are RETRIEVED, founder-wide, UNTRUSTED data. Before they may ride in a
    packet — especially one bound for an EXTERNAL provider/untrusted remote — each lesson is
    checked, over ALL its nested fields INDIVIDUALLY (never a sentinel-only top-level scan, and
    never a blanket copy of the original dict), and either included-as-labelled-data,
    field-minimized, or EXCLUDED entirely (fail closed). Reuses the existing workforce
    disclosure/injection primitives and `app.mainai_execution.lessons`/`.verify`; introduces no
    second store, no parallel verification mechanism. Returns (safe_lessons, withheld_reasons).

    Rules (independent-review handoff, PR #380):
      1. ANY zone: a lesson carrying a NEVER_EGRESS marker or private-key material in ANY nested
         field is EXCLUDED entirely (never a sentinel-only top-level check).
      2. If `db` is given and the lesson's id resolves to a row that is no longer `active`
         (superseded/historical/disputed), the lesson is EXCLUDED entirely — a stale/superseded
         lesson is never presented as current guidance, no matter what the caller's dict claims.
      3. If `db` is given and the id resolves to an ACTIVE row, the row's OWN freshly re-derived
         summary REPLACES the caller-supplied dict before any further check — closing the gap
         where a caller hands in arbitrary/tampered field values for a real lesson id.
      4. EXTERNAL zones (EXTERNAL_PROVIDER/UNTRUSTED_REMOTE, and any UNKNOWN zone, which
         `classify_trust_zone` already fails closed to UNTRUSTED_REMOTE): the lesson-as-a-whole
         is run through `minimize_for_trust_zone` (disclosure-kind authorization) and its content
         is scanned for injection-shaped prose; either denies the WHOLE lesson.
      5. `verification_status`/`verified` are NEVER copied from the caller's dict — see
         `_clamp_verification_claim`.
      6. EVERY restricted/free-text field gets its own disclosure decision — see
         `_minimize_lesson_fields`; withheld fields are recorded by NAME only, never by value.
      7. Every INCLUDED lesson is marked `untrusted_data=True` (never higher-priority
         instructions) and `requires_egress_authorization` for external zones — the AUTHORITATIVE
         outbound gate (`app.egress_policy.enforce_egress_policy`, which writes durable
         disclosure receipts) must still run before any real external transmission; this filter
         never substitutes for it (see `dispatch_agent_task_context` below, which wires it in).
    """
    zone = classify_trust_zone(trust_zone)
    external = zone in EXTERNALISH_ZONES
    safe: list[dict] = []
    withheld: list[str] = []
    for lesson in lessons or ():
        if not isinstance(lesson, dict):
            withheld.append("?:not_a_lesson_dict")
            continue
        lid = str(lesson.get("lesson_id", "?"))

        confirmation, canonical = _revalidate_lesson_against_db(db, lid)
        if confirmation == "confirmed_inactive":
            withheld.append(f"{lid}:superseded_or_inactive")
            continue
        effective = dict(canonical) if canonical is not None else dict(lesson)

        strings = list(_iter_strings(effective))
        if any(any(m in s for m in _HARD_DENY_MARKERS) for s in strings):
            withheld.append(f"{lid}:never_egress_or_private_key")
            continue

        if external:
            _accepted, denied = minimize_for_trust_zone(
                trust_zone=zone, requested_items=[{"kind": "engineering_lesson", "ref": lid}]
            )
            if denied:
                withheld.append(f"{lid}:disclosure_denied:{denied[0]}")
                continue
            if any(looks_like_prompt_injection(s) for s in strings):
                withheld.append(f"{lid}:prompt_injection_shaped")
                continue

        effective, claim_was_unconfirmed = _clamp_verification_claim(effective, confirmation=confirmation, db=db)
        if claim_was_unconfirmed:
            withheld.append(f"{lid}:verification_claim_unconfirmed")

        field_dict, withheld_fields = _minimize_lesson_fields(effective, external=external)

        safe.append(
            {
                **field_dict,
                "untrusted_data": True,
                "trust_zone": zone,
                "requires_egress_authorization": external,
                "withheld_fields": withheld_fields,
            }
        )
    return safe, withheld


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
    # Relevant PAST integration failures (compact, honestly-labelled summaries from
    # app.mainai_execution.integration_intelligence) so the agent avoids repeating known
    # problems — each carries its own confidence/status, never upgraded to 'fact'.
    integration_lessons: tuple[dict, ...] = ()
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
            "integration_lessons": list(self.integration_lessons),
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
    integration_lessons: list[dict] | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    context_version: int = 1,
    now: datetime | None = None,
    db: Session | None = None,
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

    `db`, if given, lets `integration_lessons` be independently re-verified against the
    canonical `EngineeringLesson` rows (see `_revalidate_lesson_against_db` /
    `_clamp_verification_claim`) rather than trusted at face value from the caller's dicts.
    Omitting it does not widen trust — a lesson's verification CLAIM is clamped down (never
    up) when it cannot be confirmed this way.
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
    # Integration lessons are RETRIEVED, untrusted data: trust-zone + authorization filter every
    # lesson (over ALL nested fields) against THIS packet's trust zone BEFORE inclusion. Unsafe
    # lessons are excluded (fail closed) and recorded in missing_info, never silently dropped.
    safe_lessons, lesson_withheld = _sanitize_integration_lessons(
        integration_lessons or (), trust_zone=trust_zone, db=db
    )
    missing.extend(f"lesson_withheld:{reason}" for reason in lesson_withheld)
    lessons = tuple(safe_lessons)
    # Security pre-check on CALLER-PROVIDED core fields (lessons already hard-denied above).
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
        integration_lessons=lessons,
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


# --- Egress-before-dispatch: THE transmission boundary (independent-review handoff requirement
# 4) --------------------------------------------------------------------------------------------


def dispatch_agent_task_context(
    db: Session,
    *,
    ctx: AgentTaskContext,
    owner_id: uuid.UUID,
    provider: str,
    model: str,
    purpose: str,
    requested_by: str,
    send: Callable[[dict], Any],
    task_id: uuid.UUID | None = None,
    goal_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
) -> Any:
    """THE boundary a built `AgentTaskContext` must pass through before it reaches any real
    provider/agent call. No caller may hand `ctx`'s serialized payload to a destination any
    other way — `send` is only ever invoked FROM HERE, and only after
    `app.egress_policy.enforce_egress_policy` (the authoritative outbound secret/NEVER_EGRESS
    gate this module's own header already names) has run over the COMPLETE serialized outbound
    payload (`ctx.to_transmittable_dict()`) and the exact destination
    (provider/model/purpose/requested_by), AFTER final context assembly.

    PRODUCTION DISPATCH STATUS — EXPLICITLY BLOCKED, re-confirmed at this exact HEAD
    (independent review's original finding, re-verified directly: a repo-wide grep for either
    `dispatch_agent_task_context` or `AgentTaskContext` outside this module and its own tests
    returns NOTHING). No router, worker, or other module in this codebase imports
    `context_handoff` or calls `send` with this packet's payload today. This function is the
    wiring a real dispatch owner (the founder's own #338/#339 cycle) will call INTO once it
    exists — it is deliberately NOT a competing dispatcher built ahead of that ownership, and
    the tests below exercise it only via a synthetic, clearly-labelled `send` spy, never a real
    provider call. Do not read passing tests here as "wired into production" — they prove this
    boundary is correct FOR WHENEVER a real caller is wired to it, not that one exists yet.

    Fail-closed, with ZERO calls to `send` in every one of these cases:
      - `enforce_egress_policy` DENIES the call (`EgressDeniedError`) — propagated, never
        swallowed, matching the identical calling convention every other egress-sensitive call
        site in this codebase already uses (see `app.provider_planning.service.plan_with_provider`,
        wrapped in `try/except EgressDeniedError` immediately before the payload would leave the
        process).
      - the policy check itself raises ANY other exception (unavailable/erroring) — never
        treated as "policy satisfied by default"; wrapped and re-raised as
        `ContextHandoffError`, `send` is never reached.
    Only the SANITIZED payload `enforce_egress_policy` returns (never the original) is ever
    passed to `send`.
    """
    from app.egress_policy import EgressDeniedError, enforce_egress_policy

    payload = ctx.to_transmittable_dict()
    try:
        sanitized = enforce_egress_policy(
            db,
            owner_id=owner_id,
            provider=provider,
            model=model,
            purpose=purpose,
            requested_by=requested_by,
            payload=payload,
            task_id=task_id,
            goal_id=goal_id,
            job_id=job_id,
        )
    except EgressDeniedError:
        raise
    except Exception as exc:  # policy unavailable/erroring — fail closed, never dispatch
        raise ContextHandoffError(
            f"egress policy unavailable or errored while checking an agent task-context "
            f"dispatch; refusing to send (fail closed): {exc}"
        ) from exc
    return send(sanitized)
