"""Path A authority derived from the current ExecutionAuthorizationEnvelope.

Routing discriminator is the explicit capability `path_a`. Supervisor envelopes
(read_file/patch_file without `path_a`) stay on the Supervisor path. Missing,
superseded, revoked, or otherwise inactive envelopes fail closed — they never
reopen envelope-blind V0.1 dispatch, and they never authorize Path A writes.

Never-governed goals (no envelope row) keep the existing V0.1 Path A behavior.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from sqlalchemy.orm import Session

from app.execution_envelopes.service import (
    get_current_execution_envelope,
    goal_has_ever_been_envelope_governed,
)
from app.models.execution_envelope import ExecutionAuthorizationEnvelope

PATH_A_CAPABILITY = "path_a"
REPO_EDIT_CAPABILITIES = frozenset({"repo_edit", "patch_file"})
RUN_TESTS_CAPABILITIES = frozenset({"run_tests", "repo_edit", "patch_file"})
OPEN_PR_CAPABILITIES = frozenset({"open_pr", "create_pr", "repo_edit"})
READ_CAPABILITIES = frozenset({"read_file", "read_only_audit", PATH_A_CAPABILITY})


class PathAEnvelopeError(Exception):
    """Fail-closed Path A authority error. execution_job maps this to TaskExecutionError."""


def envelope_capability_set(envelope: ExecutionAuthorizationEnvelope | None) -> frozenset[str]:
    if envelope is None:
        return frozenset()
    caps = envelope.authorized_capabilities or []
    return frozenset(str(cap) for cap in caps)


def envelope_authorizes_path_a(envelope: ExecutionAuthorizationEnvelope | None) -> bool:
    """True only for a currently active envelope that explicitly authorizes Path A."""
    if envelope is None or getattr(envelope, "status", None) != "active":
        return False
    return PATH_A_CAPABILITY in envelope_capability_set(envelope)


def envelope_authorizes_any_capability(
    envelope: ExecutionAuthorizationEnvelope | None, allowed: frozenset[str]
) -> bool:
    if not envelope_authorizes_path_a(envelope):
        return False
    return bool(envelope_capability_set(envelope) & allowed)


def path_in_authorized_scope(path: str, authorized_paths: list[str] | None) -> bool:
    """Prefix/`**` matching aligned with workforce `path_allowed`, fail-closed on empty."""
    prefixes = [str(prefix) for prefix in (authorized_paths or []) if str(prefix)]
    if not prefixes or not path:
        return False
    normalized = path.lstrip("./")
    posix = PurePosixPath(normalized)
    if posix.is_absolute() or ".." in posix.parts:
        return False
    for prefix in prefixes:
        p = prefix.rstrip("*")
        if normalized == p.rstrip("/") or normalized.startswith(p.rstrip("/") + "/") or (
            prefix.endswith("**") and normalized.startswith(p.rstrip("*").rstrip("/"))
        ):
            return True
        if normalized.startswith(prefix):
            return True
    return False


def map_verification_target_to_repo_path(target: str) -> str:
    """Pytest targets run with cwd=`backend/`. Map them to repo-root write paths."""
    if not isinstance(target, str) or not target:
        return ""
    posix = PurePosixPath(target)
    if posix.is_absolute() or ".." in posix.parts:
        return target
    if target.startswith(("backend/", "frontend/", "docs/")):
        return target
    return f"backend/{target}"


def merge_repo_edit_target_files(target_files: list[str], verification_plan: list[dict] | None) -> list[str]:
    """Union planner-named files with mapped focused-test paths. Preserve order."""
    merged: list[str] = []
    seen: set[str] = set()
    for path in list(target_files or []):
        if isinstance(path, str) and path and path not in seen:
            seen.add(path)
            merged.append(path)
    for step in verification_plan or []:
        if not isinstance(step, dict) or step.get("kind") != "targeted_tests":
            continue
        mapped = map_verification_target_to_repo_path(str(step.get("target") or ""))
        if mapped and mapped not in seen:
            seen.add(mapped)
            merged.append(mapped)
    return merged


def path_a_dispatch_is_authorized(db: Session, *, owner_id, goal_id) -> bool:
    """Worker tick predicate. Never-governed → True. Envelope-governed → active `path_a`."""
    if not goal_has_ever_been_envelope_governed(db, owner_id=owner_id, goal_id=goal_id):
        return True
    envelope = get_current_execution_envelope(db, owner_id=owner_id, goal_id=goal_id)
    return envelope_authorizes_path_a(envelope)


def require_path_a_execution_envelope(db: Session, *, owner_id, goal_id) -> ExecutionAuthorizationEnvelope | None:
    """Return None for never-governed V0.1 goals. Fail closed for governed goals without Path A."""
    if not goal_has_ever_been_envelope_governed(db, owner_id=owner_id, goal_id=goal_id):
        return None
    envelope = get_current_execution_envelope(db, owner_id=owner_id, goal_id=goal_id)
    if envelope is None or envelope.status != "active":
        raise PathAEnvelopeError("Refusing Path A work: no current active execution envelope.")
    if not envelope_authorizes_path_a(envelope):
        raise PathAEnvelopeError(
            "Refusing Path A work: the active envelope does not explicitly authorize Path A."
        )
    return envelope


def assert_paths_in_envelope(envelope: ExecutionAuthorizationEnvelope | None, paths: list[str], *, action: str) -> None:
    if envelope is None:
        return
    authorized = list(envelope.authorized_paths or [])
    for path in paths:
        if not path_in_authorized_scope(path, authorized):
            raise PathAEnvelopeError(
                f"Refusing {action} {path!r}: it is outside the founder-authorized envelope paths."
            )


def assert_envelope_allows_repo_edit(envelope: ExecutionAuthorizationEnvelope | None, paths: list[str]) -> None:
    if envelope is None:
        return
    if not envelope_authorizes_any_capability(envelope, REPO_EDIT_CAPABILITIES):
        raise PathAEnvelopeError(
            "Refusing repo_edit: the active envelope does not authorize repo_edit/patch_file."
        )
    assert_paths_in_envelope(envelope, paths, action="to edit")


def assert_envelope_allows_tests(envelope: ExecutionAuthorizationEnvelope | None, repo_paths: list[str]) -> None:
    if envelope is None:
        return
    if not envelope_authorizes_any_capability(envelope, RUN_TESTS_CAPABILITIES):
        raise PathAEnvelopeError(
            "Refusing to run tests: the active envelope does not authorize run_tests/repo_edit."
        )
    assert_paths_in_envelope(envelope, repo_paths, action="to test")


def assert_envelope_allows_open_pr(envelope: ExecutionAuthorizationEnvelope | None) -> None:
    if envelope is None:
        return
    if not envelope_authorizes_any_capability(envelope, OPEN_PR_CAPABILITIES):
        raise PathAEnvelopeError(
            "Refusing open_pr: the active envelope does not authorize opening a pull request."
        )


def assert_envelope_allows_read(envelope: ExecutionAuthorizationEnvelope | None) -> None:
    if envelope is None:
        return
    if not envelope_authorizes_any_capability(envelope, READ_CAPABILITIES):
        raise PathAEnvelopeError(
            "Refusing read_only_audit: the active envelope does not authorize read_file."
        )
