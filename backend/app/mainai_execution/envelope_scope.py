"""Path A authority derived from the current ExecutionAuthorizationEnvelope.

Routing discriminator is the explicit capability `path_a`. Supervisor envelopes
(read_file/patch_file without `path_a`) stay on the Supervisor path. Missing,
superseded, revoked, or otherwise inactive envelopes fail closed — they never
reopen envelope-blind V0.1 dispatch, and they never authorize Path A writes.

Never-governed goals (no envelope row) keep the existing V0.1 Path A behavior.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath

from sqlalchemy import select, text as sa_text
from sqlalchemy.orm import Session

from app.execution_envelopes.service import (
    get_current_execution_envelope,
    goal_has_ever_been_envelope_governed,
)
from app.models.execution_envelope import ExecutionAuthorizationEnvelope

TARGET_SOURCE_PLANNER = "planner"
TARGET_SOURCE_INSERTION = "insertion"
TARGET_SOURCE_GAP = "gap"
KNOWN_TARGET_SOURCES = frozenset(
    {TARGET_SOURCE_PLANNER, TARGET_SOURCE_INSERTION, TARGET_SOURCE_GAP}
)
CAPABILITY_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

PATH_A_CAPABILITY = "path_a"
REPO_EDIT_CAPABILITIES = frozenset({"repo_edit", "patch_file"})
RUN_TESTS_CAPABILITIES = frozenset({"run_tests", "repo_edit", "patch_file"})
# Publication is an outward action. repo_edit must never imply open_pr (CAP-1).
OPEN_PR_CAPABILITIES = frozenset({"open_pr", "create_pr"})
READ_CAPABILITIES = frozenset({"read_file", "read_only_audit", PATH_A_CAPABILITY})


class PathAEnvelopeError(Exception):
    """Fail-closed Path A authority error. execution_job maps this to TaskExecutionError."""


class TargetPathError(PathAEnvelopeError):
    """A proposed repo-mutating target is not a single safe relative POSIX path."""


class CapabilityNameError(PathAEnvelopeError):
    """A provider/failure-derived capability name is not a safe identifier."""


class TargetAuthorityError(PathAEnvelopeError):
    """Structured task-target authority is missing, historical, or inconsistent."""


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
    """Union planner-named files with mapped focused-test paths. Preserve order.

    Callers that persist edit authority must pass the PRE-lesson verification plan.
    Lesson-injected regression tests are verify-only and must never enter this merge.
    """
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


def validate_target_path(path: str) -> str:
    """Canonical safe relative POSIX path for every repo-mutating task target.

    One path. Rejects whitespace, control characters, `::` (pytest node ids),
    backslashes, absolute paths, `..`, empty/`.` parts, and `.`-prefixed parts.
    """
    if not isinstance(path, str) or not path:
        raise TargetPathError(f"target path must be a non-empty string, got {path!r}")
    if path != path.strip() or any(ch.isspace() or unicodedata.category(ch)[0] == "C" for ch in path):
        raise TargetPathError(f"target path must be a single relative path with no whitespace or control characters: {path!r}")
    if "::" in path or "\\" in path:
        raise TargetPathError(f"target path must not contain '::' or backslashes: {path!r}")
    if path.endswith("/"):
        raise TargetPathError(f"target path must name a file, not a directory: {path!r}")
    stripped = _strip_literal_dot_slash(path)
    if not stripped:
        raise TargetPathError(f"target path is not a safe relative POSIX path: {path!r}")
    posix = PurePosixPath(stripped)
    if posix.is_absolute() or ".." in posix.parts or "." in posix.parts or "" in posix.parts:
        raise TargetPathError(f"target path must be a relative path with no '..' or empty parts: {path!r}")
    if any(part.startswith(".") for part in posix.parts):
        raise TargetPathError(f"target path must not contain '.'-prefixed parts: {path!r}")
    canonical = posix.as_posix()
    if not canonical or canonical == ".":
        raise TargetPathError(f"target path is empty after normalization: {path!r}")
    return canonical


def validate_capability_name(name: str) -> str:
    """Provider/failure-derived capability names cannot inject paths or target metadata."""
    if not isinstance(name, str) or not CAPABILITY_NAME_RE.fullmatch(name):
        raise CapabilityNameError(
            f"capability name must match {CAPABILITY_NAME_RE.pattern} and cannot carry paths or target metadata, got {name!r}"
        )
    return name


def _mapped_targeted_test_paths(verification_plan: list[dict] | None) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for step in verification_plan or []:
        if not isinstance(step, dict) or step.get("kind") != "targeted_tests":
            continue
        mapped = map_verification_target_to_repo_path(str(step.get("target") or ""))
        if not mapped or mapped in seen:
            continue
        try:
            canonical = validate_target_path(mapped)
        except TargetPathError:
            continue
        seen.add(canonical)
        found.append(canonical)
    return found


def structured_editable_targets(
    declared_target_files: list[str] | tuple[str, ...] | None,
    *,
    pre_lesson_verification_plan: list[dict] | tuple[dict, ...] | None = None,
    task_type: str,
) -> list[str]:
    """Editable write paths: validated declared files plus pre-lesson focused tests.

    Lesson-injected tests are intentionally absent — they are verify-only.
    """
    declared: list[str] = []
    seen: set[str] = set()
    for path in list(declared_target_files or []):
        canonical = validate_target_path(path)
        if canonical not in seen:
            seen.add(canonical)
            declared.append(canonical)
    if task_type != "repo_edit":
        return declared
    for mapped in _mapped_targeted_test_paths(list(pre_lesson_verification_plan or [])):
        if mapped not in seen:
            seen.add(mapped)
            declared.append(mapped)
    return declared


def verify_only_test_paths(
    *,
    pre_lesson_verification_plan: list[dict] | tuple[dict, ...] | None,
    post_lesson_verification_plan: list[dict] | tuple[dict, ...] | None,
) -> list[str]:
    """Mapped test paths that lessons added after the planner/insert spec was written."""
    before = set(_mapped_targeted_test_paths(list(pre_lesson_verification_plan or [])))
    after = _mapped_targeted_test_paths(list(post_lesson_verification_plan or []))
    return [path for path in after if path not in before]


def persistable_target_authority(
    *,
    declared_target_files: list[str] | tuple[str, ...] | None,
    pre_lesson_verification_plan: list[dict] | tuple[dict, ...] | None,
    post_lesson_verification_plan: list[dict] | tuple[dict, ...] | None,
    task_type: str,
    target_source: str,
) -> dict:
    """Canonical created-event payload for task target authority."""
    if target_source not in KNOWN_TARGET_SOURCES:
        raise TargetAuthorityError(f"unknown target_source {target_source!r}")
    editable = structured_editable_targets(
        declared_target_files,
        pre_lesson_verification_plan=list(pre_lesson_verification_plan or []),
        task_type=task_type,
    )
    verify_only = [
        path
        for path in verify_only_test_paths(
            pre_lesson_verification_plan=list(pre_lesson_verification_plan or []),
            post_lesson_verification_plan=list(post_lesson_verification_plan or []),
        )
        if path not in set(editable)
    ]
    if task_type == "repo_edit" and not editable:
        raise TargetAuthorityError(
            "repo_edit requires structured target_files; text-only or historical authority is refused."
        )
    return {
        "target_files": editable,
        "verify_only_tests": verify_only,
        "target_source": target_source,
    }


def display_target_files_suffix(target_files: list[str]) -> str:
    """Human-readable description suffix. Never parsed back into authority."""
    if not target_files:
        return ""
    return "\nTarget files: " + " ".join(target_files)


def load_structured_edit_targets(db: Session, task) -> list[str]:
    """Read persisted editable targets from the append-only created event. Fail closed.

    Never inspects Task.description or a 'Target files:' prose marker.
    """
    from app.models.mainai_execution import MainAITaskEvent, MainAITaskEventType

    event = db.execute(
        select(MainAITaskEvent)
        .where(
            MainAITaskEvent.task_id == task.id,
            MainAITaskEvent.event_type == MainAITaskEventType.created,
        )
        .order_by(MainAITaskEvent.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    if event is None:
        return []
    raw = (event.detail or {}).get("target_files")
    if not isinstance(raw, list) or not raw:
        return []
    try:
        return [validate_target_path(path) for path in raw]
    except TargetPathError:
        return []


def load_structured_target_authority(db: Session, task) -> dict:
    from app.models.mainai_execution import MainAITaskEvent, MainAITaskEventType

    event = db.execute(
        select(MainAITaskEvent)
        .where(
            MainAITaskEvent.task_id == task.id,
            MainAITaskEvent.event_type == MainAITaskEventType.created,
        )
        .order_by(MainAITaskEvent.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    empty = {"target_files": [], "verify_only_tests": [], "target_source": None}
    if event is None:
        return empty
    detail = dict(event.detail or {})
    files = load_structured_edit_targets(db, task)
    raw_verify = detail.get("verify_only_tests")
    verify_only: list[str] = []
    if isinstance(raw_verify, list):
        for path in raw_verify:
            try:
                verify_only.append(validate_target_path(path))
            except TargetPathError:
                continue
    source = detail.get("target_source")
    if source not in KNOWN_TARGET_SOURCES:
        source = None
    return {"target_files": files, "verify_only_tests": verify_only, "target_source": source}


def inherit_structured_edit_targets(db: Session, *, task, allowed_paths: tuple[str, ...] | list[str] | None) -> list[str]:
    """Gap-repair inherit: parent structured targets ∩ authorized paths. Never description text."""
    inherited: list[str] = []
    allowed = list(allowed_paths or [])
    for path in load_structured_edit_targets(db, task):
        if path_in_authorized_scope(path, allowed) and path not in inherited:
            inherited.append(path)
    return inherited


def assert_changed_files_within_authority(
    *,
    actual_files: list[str],
    editable_targets: list[str],
    envelope: ExecutionAuthorizationEnvelope | None,
) -> None:
    """actual files ⊆ structured editable targets ⊆ active envelope paths."""
    try:
        editable = [validate_target_path(path) for path in editable_targets]
    except TargetPathError as exc:
        raise PathAEnvelopeError(f"Refusing publication: structured targets are invalid ({exc}).") from exc
    if not editable:
        raise PathAEnvelopeError("Refusing publication: no structured editable targets.")
    editable_set = set(editable)
    for path in actual_files:
        try:
            validated = validate_target_path(path)
        except TargetPathError as exc:
            raise PathAEnvelopeError(
                f"Refusing to publish unexpected path {path!r}: it is not a safe structured target."
            ) from exc
        if validated not in editable_set:
            raise PathAEnvelopeError(
                f"Refusing to publish {validated!r}: it is not in the structured editable targets."
            )
    assert_envelope_allows_repo_edit(envelope, list(actual_files))
    assert_envelope_allows_repo_edit(envelope, editable)


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
