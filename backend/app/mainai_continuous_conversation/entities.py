"""Entity / subject binding for software-truth lookups.

MEMORY != AUTHORITY. HARDCODED CURRENT STATE != AUTHORITY.
A request must bind entity, repository, branch/artifact role, state, source, and
observed_at from the governed registry plus an authoritative observation. Python
literals of this week's SHAs are not an authority source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.mainai_continuous_conversation.provider import ObservedRepositoryState
from app.mainai_continuous_conversation.types import ArtifactRole, BoundSubject
from app.models.continuous_conversation import (
    GovernedArtifactCertification,
    GovernedEntityRecord,
    GovernedRepositoryObservation,
)

DEFAULT_REPOSITORY = "d1n095/LifeAI"
AUTHORITATIVE_TIP_SOURCES = frozenset({"github_ref"})
MAX_TIP_OBSERVATION_AGE = timedelta(minutes=5)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def observation_is_authoritative_current(
    *,
    sha: str | None,
    source: str | None,
    observed_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    """True only for a fresh GitHub-ref tip. Stale or non-authoritative rows are not current."""

    if not sha or source not in AUTHORITATIVE_TIP_SOURCES:
        return False
    stamped = _aware(observed_at)
    if stamped is None:
        return False
    clock = _aware(now) or datetime.now(timezone.utc)
    return clock - stamped <= MAX_TIP_OBSERVATION_AGE


def _current_tip_or_unknown(
    *,
    prefetch: GovernedRepositoryObservation | None,
    observed: ObservedRepositoryState | None,
) -> tuple[str | None, str, datetime | None, str]:
    live_attempted = observed is not None
    if observation_is_authoritative_current(
        sha=observed.sha if observed is not None else None,
        source=observed.source if observed is not None else None,
        observed_at=observed.observed_at if observed is not None else None,
    ):
        assert observed is not None and observed.sha
        return observed.sha, observed.source, observed.observed_at, ""
    if live_attempted:
        source = observed.source if observed is not None else "unavailable"
        return (
            None,
            source if source else "unavailable",
            observed.observed_at if observed is not None else None,
            "Live authoritative tip unavailable or unbound. UNKNOWN — stale observations are not current truth.",
        )
    if prefetch is not None and observation_is_authoritative_current(
        sha=prefetch.sha, source=prefetch.source, observed_at=prefetch.observed_at
    ):
        return prefetch.sha, prefetch.source, prefetch.observed_at, ""
    if prefetch is not None:
        return (
            None,
            prefetch.source or "unavailable",
            prefetch.observed_at,
            "Stored observation is stale or not bound to github_ref. UNKNOWN — not current truth.",
        )
    return None, "unavailable", None, "Authoritative tip unavailable. UNKNOWN — checkout is not used."

_SOVEREIGNTY = re.compile(r"\b(founder sovereignty|suver[äa]nitet)\b", re.IGNORECASE)
_CC_PARENT = re.compile(
    r"\b(continuous conversation parent|cc parent|parent sha of (the )?continuous)\b",
    re.IGNORECASE,
)
_P1_CURRENT = re.compile(
    r"\b(p1 fix branch|this p1|p1 fix sha|continuous conversation p1|current child sha|this branch currently)\b",
    re.IGNORECASE,
)
_FOUNDER_ALPHA = re.compile(
    r"\b(frozen founder alpha|founder alpha frozen|founder alpha( branch| sha| candidate)?)\b",
    re.IGNORECASE,
)
NAMED_BRANCH = re.compile(r"\b((?:cursor|codex|claude)/[\w./-]+)\b")
_NAMED_BRANCH = NAMED_BRANCH


@dataclass(frozen=True)
class RegistrySubject:
    entity_key: str
    repository: str
    branch: str
    artifact_role: ArtifactRole
    state: str
    source: str
    detail: str = ""
    observed_at: datetime | None = None
    sha: str | None = None


def classify_requested_entity(text: str) -> str | None:
    """Return a registry key or None when the utterance does not name a known entity."""

    lowered = text.strip()
    if not lowered:
        return None
    if _SOVEREIGNTY.search(lowered):
        return "founder_sovereignty"
    if _CC_PARENT.search(lowered):
        return "continuous_conversation_parent"
    if _P1_CURRENT.search(lowered):
        return "continuous_conversation_p1"
    if _FOUNDER_ALPHA.search(lowered):
        return "founder_alpha_frozen"
    return None


def load_entity_record(db: Session, entity_key: str) -> GovernedEntityRecord | None:
    return db.get(GovernedEntityRecord, entity_key)


def load_certification(db: Session, entity_key: str) -> GovernedArtifactCertification | None:
    return db.get(GovernedArtifactCertification, entity_key)


def load_observation(db: Session, repository: str, branch: str) -> GovernedRepositoryObservation | None:
    return db.get(GovernedRepositoryObservation, (repository, branch))


def record_observation(db: Session, observed: ObservedRepositoryState) -> GovernedRepositoryObservation | None:
    if not observation_is_authoritative_current(
        sha=observed.sha, source=observed.source, observed_at=observed.observed_at
    ):
        return None
    row = load_observation(db, observed.repository, observed.branch)
    if row is None:
        row = GovernedRepositoryObservation(
            repository=observed.repository,
            branch=observed.branch,
            sha=observed.sha,
            source=observed.source,
            observed_at=observed.observed_at or datetime.utcnow(),
        )
        db.add(row)
    else:
        row.sha = observed.sha
        row.source = observed.source
        row.observed_at = observed.observed_at or datetime.utcnow()
    db.flush()
    return row


def bind_subject(
    text: str,
    *,
    record: RegistrySubject | None = None,
    sha: str | None = None,
    source: str = "unavailable",
    observed_at: datetime | None = None,
) -> BoundSubject | None:
    """Bind a previously loaded registry subject. Does not invent SHA or git state."""

    if record is None:
        named = _NAMED_BRANCH.search(text)
        if named:
            branch = named.group(1)
            return BoundSubject(
                entity_key="named_branch",
                repository=DEFAULT_REPOSITORY,
                branch=branch,
                artifact_role=ArtifactRole.CURRENT_BRANCH_TIP,
                state="named_ref",
                authoritative_source=source if sha else "unavailable",
                sha=sha,
                detail=f"Named branch {branch} — resolved from an authoritative source, not checkout.",
                observed_at=observed_at,
            )
        return None
    return BoundSubject(
        entity_key=record.entity_key,
        repository=record.repository,
        branch=record.branch,
        artifact_role=record.artifact_role,
        state=record.state,
        authoritative_source=source,
        sha=sha,
        detail=record.detail,
        observed_at=observed_at,
    )


def registry_subject_from_row(
    row: GovernedEntityRecord,
    *,
    sha: str | None = None,
    source: str | None = None,
    observed_at: datetime | None = None,
) -> RegistrySubject:
    return RegistrySubject(
        entity_key=row.entity_key,
        repository=row.repository,
        branch=row.branch,
        artifact_role=ArtifactRole(row.artifact_role),
        state=row.state,
        source=source or row.source,
        detail=row.detail,
        observed_at=observed_at if observed_at is not None else row.observed_at,
        sha=sha,
    )


def bind_subject_from_db(
    db: Session,
    text: str,
    *,
    observed: ObservedRepositoryState | None = None,
) -> BoundSubject | None:
    """Resolve entity metadata from the governed registry. SHA only from cert or observation."""

    key = classify_requested_entity(text)
    if key is None:
        named = _NAMED_BRANCH.search(text)
        if named is None:
            return None
        branch = named.group(1)
        sha, source, observed_at, unknown_detail = _current_tip_or_unknown(
            prefetch=load_observation(db, DEFAULT_REPOSITORY, branch),
            observed=observed,
        )
        return BoundSubject(
            entity_key="named_branch",
            repository=DEFAULT_REPOSITORY,
            branch=branch,
            artifact_role=ArtifactRole.CURRENT_BRANCH_TIP,
            state="named_ref",
            authoritative_source=source if sha else "unavailable",
            sha=sha,
            detail=unknown_detail or f"Named branch {branch} — resolved from an authoritative source, not checkout.",
            observed_at=observed_at,
        )

    row = load_entity_record(db, key)
    if row is None:
        return None
    role = ArtifactRole(row.artifact_role)
    if role is ArtifactRole.CURRENT_BRANCH_TIP:
        sha, source, observed_at, unknown_detail = _current_tip_or_unknown(
            prefetch=load_observation(db, row.repository, row.branch),
            observed=observed,
        )
        return BoundSubject(
            entity_key=row.entity_key,
            repository=row.repository,
            branch=row.branch,
            artifact_role=role,
            state=row.state,
            authoritative_source=source if sha else "unavailable",
            sha=sha,
            detail=row.detail if sha else (unknown_detail or "Authoritative tip unavailable. UNKNOWN — checkout is not used."),
            observed_at=observed_at,
        )

    cert = load_certification(db, key)
    if cert is None:
        return BoundSubject(
            entity_key=row.entity_key,
            repository=row.repository,
            branch=row.branch,
            artifact_role=role,
            state=row.state,
            authoritative_source="unavailable",
            sha=None,
            detail="No governed certification for this entity. UNKNOWN — will not invent a SHA.",
            observed_at=None,
        )
    return BoundSubject(
        entity_key=row.entity_key,
        repository=row.repository,
        branch=row.branch,
        artifact_role=role,
        state=row.state,
        authoritative_source=cert.source,
        sha=cert.sha,
        detail=row.detail,
        observed_at=cert.certified_at,
    )


def apply_observed_tip(subject: BoundSubject, observed: ObservedRepositoryState) -> BoundSubject:
    if subject.artifact_role is not ArtifactRole.CURRENT_BRANCH_TIP:
        return subject
    if not observation_is_authoritative_current(
        sha=observed.sha, source=observed.source, observed_at=observed.observed_at
    ):
        return replace(
            subject,
            sha=None,
            authoritative_source="unavailable",
            detail="Live authoritative tip unavailable or stale. UNKNOWN — stale observations are not current truth.",
            observed_at=observed.observed_at,
        )
    return replace(
        subject,
        sha=observed.sha,
        authoritative_source=observed.source,
        observed_at=observed.observed_at,
        detail=subject.detail,
    )
