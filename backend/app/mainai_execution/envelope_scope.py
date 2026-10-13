"""Path A authority derived from the current ExecutionAuthorizationEnvelope.

Routing discriminator is the explicit capability `path_a`. Supervisor envelopes
(read_file/patch_file without `path_a`) stay on the Supervisor path. Missing,
superseded, revoked, or otherwise inactive envelopes fail closed — they never
reopen envelope-blind V0.1 dispatch, and they never authorize Path A writes.

Never-governed goals (no envelope row) keep the existing V0.1 Path A behavior.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.execution_envelopes.service import (
    get_current_execution_envelope,
    goal_has_ever_been_envelope_governed,
)
from app.models.execution_envelope import ExecutionAuthorizationEnvelope

PATH_A_CAPABILITY = "path_a"
REPO_EDIT_CAPABILITIES = frozenset({"repo_edit", "patch_file"})
RUN_TESTS_CAPABILITIES = frozenset({"run_tests", "repo_edit", "patch_file"})
# Publication is an outward action. repo_edit must never imply open_pr (CAP-1).
OPEN_PR_CAPABILITIES = frozenset({"open_pr", "create_pr"})
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


def _strip_literal_dot_slash(path: str) -> str:
    """Strip only a leading `./`. Do not lstrip arbitrary dots or slashes (SCOPE-1)."""
    if path.startswith("./"):
        return path[2:]
    return path


def _normalize_scope_file(path: str) -> str | None:
    """Safe relative file path, or None. Rejects `..`, absolutes, and `.`-prefixed parts."""
    if not isinstance(path, str) or not path:
        return None
    stripped = _strip_literal_dot_slash(path)
    if not stripped:
        return None
    posix = PurePosixPath(stripped)
    if posix.is_absolute() or ".." in posix.parts:
        return None
    if any(part.startswith(".") for part in posix.parts):
        return None
    return posix.as_posix()


def _authorized_directory_prefix(entry: str) -> str | None:
    """Directory boundary only when the Founder wrote `/` or `/**`. Bare names are files."""
    if not isinstance(entry, str) or not entry:
        return None
    raw = _strip_literal_dot_slash(entry)
    if raw.endswith("/**"):
        prefix = raw[:-3]
    elif raw.endswith("/"):
        prefix = raw[:-1]
    else:
        return None
    if not prefix:
        return None
    return _normalize_scope_file(prefix)


def path_in_authorized_scope(path: str, authorized_paths: list[str] | None) -> bool:
    """Exact file match, or a directory prefix only with an explicit `/` or `/**` boundary.

    Sibling-prefix lookalikes (`greeting.py_backdoor.py`), leading-dot tricks
    (`.backend/...`), and nested suffix paths must not inherit a file grant.
    """
    prefixes = [str(prefix) for prefix in (authorized_paths or []) if str(prefix)]
    candidate = _normalize_scope_file(path)
    if not prefixes or candidate is None:
        return False
    for raw in prefixes:
        directory = _authorized_directory_prefix(raw)
        if directory is not None:
            if candidate == directory or candidate.startswith(directory + "/"):
                return True
            continue
        authorized_file = _normalize_scope_file(_strip_literal_dot_slash(raw))
        if authorized_file is not None and candidate == authorized_file:
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


def goal_has_live_supervisor_lease(db: Session, *, goal_id) -> bool:
    """True when an unexpired active Supervisor goal lease still holds this goal (ROUTE-4)."""
    live = db.execute(
        sa_text(
            "SELECT 1 FROM supervisor_goal_leases "
            "WHERE goal_id = :goal_id AND status = 'active' AND expires_at > now() "
            "LIMIT 1"
        ),
        {"goal_id": str(goal_id)},
    ).first()
    return live is not None


def path_a_dispatch_is_authorized(db: Session, *, owner_id, goal_id) -> bool:
    """Worker tick predicate. Never-governed → True unless a live Supervisor lease exists.

    Envelope-governed → active `path_a` AND no live Supervisor goal lease (ROUTE-4).
    """
    if goal_has_live_supervisor_lease(db, goal_id=goal_id):
        return False
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
