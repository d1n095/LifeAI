"""Entity / subject binding for software-truth lookups.

INTERNAL LOOKUP != CORRECT SUBJECT BINDING. A request must bind the requested entity,
repository, branch/artifact role, state, and authoritative source. Returning whichever
branch happens to be current, or the current checkout SHA, is a correctness failure.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import replace
from pathlib import Path

from app.mainai_continuous_conversation.types import ArtifactRole, BoundSubject

DEFAULT_REPOSITORY = "d1n095/LifeAI"

FROZEN_FOUNDER_ALPHA_SHA = "2fbe20aacf1203fc0e16d216ef55b666b2181619"
FROZEN_FOUNDER_ALPHA_BRANCH = "codex/founder-alpha-final-composed-candidate"
CONTINUOUS_CONVERSATION_PARENT_SHA = "691490edd82fa4bff6188f4f038f7579ee4f3df5"
CONTINUOUS_CONVERSATION_PARENT_BRANCH = "cursor/mainai-continuous-conversation-foundation"
CONTINUOUS_CONVERSATION_P1_BRANCH = "cursor/mainai-continuous-conversation-p1-fix"
FOUNDER_SOVEREIGNTY_SHA = "ffbdb6328b8ff594caf2eea71c77c32ef96e516b"
FOUNDER_SOVEREIGNTY_BRANCH = "cursor/mainai-founder-sovereignty-family-delegation"

_REGISTRY: dict[str, BoundSubject] = {
    "founder_alpha_frozen": BoundSubject(
        entity_key="founder_alpha_frozen",
        repository=DEFAULT_REPOSITORY,
        branch=FROZEN_FOUNDER_ALPHA_BRANCH,
        artifact_role=ArtifactRole.FROZEN_CANDIDATE,
        state="frozen",
        authoritative_source="founder_alpha_registry",
        sha=FROZEN_FOUNDER_ALPHA_SHA,
        detail="Frozen Founder Alpha candidate. Not the current checkout and not a later child tip.",
    ),
    "continuous_conversation_parent": BoundSubject(
        entity_key="continuous_conversation_parent",
        repository=DEFAULT_REPOSITORY,
        branch=CONTINUOUS_CONVERSATION_PARENT_BRANCH,
        artifact_role=ArtifactRole.PARENT_SHA,
        state="parent",
        authoritative_source="continuous_conversation_registry",
        sha=CONTINUOUS_CONVERSATION_PARENT_SHA,
        detail="Continuous Conversation foundation parent. Not Founder Alpha and not the P1 child.",
    ),
    "founder_sovereignty": BoundSubject(
        entity_key="founder_sovereignty",
        repository=DEFAULT_REPOSITORY,
        branch=FOUNDER_SOVEREIGNTY_BRANCH,
        artifact_role=ArtifactRole.EXAMINED_LANE_SHA,
        state="frozen_lane",
        authoritative_source="founder_sovereignty_registry",
        sha=FOUNDER_SOVEREIGNTY_SHA,
        detail="Founder Sovereignty examined SHA. Workspace sharing with this lane is forbidden.",
    ),
    "continuous_conversation_p1": BoundSubject(
        entity_key="continuous_conversation_p1",
        repository=DEFAULT_REPOSITORY,
        branch=CONTINUOUS_CONVERSATION_P1_BRANCH,
        artifact_role=ArtifactRole.CURRENT_BRANCH_TIP,
        state="current_child",
        authoritative_source="remote_branch_tip",
        sha=None,
        detail="Current Continuous Conversation P1 fix branch tip. Resolved from the remote ref, never from checkout.",
    ),
}

_SOVEREIGNTY = re.compile(
    r"\b(founder sovereignty|suver[äa]nitet)\b",
    re.IGNORECASE,
)
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
_NAMED_BRANCH = re.compile(r"\b((?:cursor|codex|claude)/[\w./-]+)\b")


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


def registry_subject(entity_key: str) -> BoundSubject | None:
    return _REGISTRY.get(entity_key)


def read_remote_branch_sha(branch: str, *, repo_dir: str | Path | None = None) -> str | None:
    """Authoritative remote tip. Never uses `git rev-parse HEAD` / current checkout."""

    cwd = str(repo_dir or Path.cwd())
    try:
        completed = subprocess.run(
            ["git", "ls-remote", "origin", f"refs/heads/{branch}"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = (completed.stdout or "").strip().splitlines()
    if not line:
        return None
    sha = line[0].split()[0].strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", sha):
        return sha
    return None


def bind_subject(text: str, *, remote_sha: str | None = None) -> BoundSubject | None:
    """Bind the requested entity. Does not invent a subject from the current checkout."""

    key = classify_requested_entity(text)
    if key is None:
        named = _NAMED_BRANCH.search(text)
        if named:
            branch = named.group(1)
            sha = remote_sha if remote_sha is not None else read_remote_branch_sha(branch)
            return BoundSubject(
                entity_key="named_branch",
                repository=DEFAULT_REPOSITORY,
                branch=branch,
                artifact_role=ArtifactRole.CURRENT_BRANCH_TIP,
                state="named_ref",
                authoritative_source="remote_branch_tip" if sha else "unavailable",
                sha=sha,
                detail=f"Named branch {branch} — resolved from the remote ref, not checkout.",
            )
        return None
    subject = _REGISTRY[key]
    if subject.artifact_role is ArtifactRole.CURRENT_BRANCH_TIP:
        sha = remote_sha if remote_sha is not None else read_remote_branch_sha(subject.branch)
        return replace(
            subject,
            sha=sha,
            authoritative_source="remote_branch_tip" if sha else "unavailable",
            detail=subject.detail if sha else "Remote tip is unavailable. UNKNOWN — will not fabricate or use checkout.",
        )
    return subject
