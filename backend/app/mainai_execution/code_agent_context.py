"""Read existing files into the Path A code-agent prompt. Not a second client — it only
reads from a local checkout the executor already owns (shared repo root or a worktree).

P1 (Claude exam of #264 @ 21ff3cc): never silently truncate an existing file and then
accept a whole-file replacement. Writes are allowed only to explicit target paths whose
complete, untruncated contents were shown to the editing model (or to new files that
were explicitly named and therefore have no hidden tail)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

_PATH_RE = re.compile(
    r"(?:^|[\s`'\"(\[])("
    r"(?:(?:backend|frontend|docs|app|tests|scripts)/[A-Za-z0-9_./-]+\.[A-Za-z0-9]+)"
    r"|(?:[A-Za-z0-9_.-]+\.(?:py|ts|tsx|js|jsx|md|txt|json|yml|yaml|toml|css|html))"
    r")"
)
_MAX_FILES = 20
_MAX_CHARS = 80_000
_MAX_FILE_CHARS = 16_000


@dataclass(frozen=True)
class CodeAgentPrompt:
    """Prompt text plus the write-acceptance facts derived from it."""

    text: str
    explicit_paths: tuple[str, ...]
    fully_shown_paths: frozenset[str]
    oversized_paths: frozenset[str]
    unseen_existing_paths: frozenset[str] = field(default_factory=frozenset)


def candidate_paths_from_text(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _PATH_RE.finditer(text or ""):
        path = match.group(1)
        if path in seen:
            continue
        posix = PurePosixPath(path)
        if posix.is_absolute() or ".." in posix.parts:
            continue
        seen.add(path)
        found.append(path)
    return found


def explicit_target_paths(*texts: str, extra: list[str] | None = None) -> list[str]:
    """Union of paths named in planner/task text and an explicit extra list. Fail closed
    for absolute/`..` extras rather than silently dropping them — the caller must name
    a safe relative path."""
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for path in candidate_paths_from_text(text):
            if path not in seen:
                seen.add(path)
                found.append(path)
    for path in extra or []:
        if not isinstance(path, str) or not path:
            raise ValueError(f"target file path must be a non-empty string, got {path!r}")
        posix = PurePosixPath(path)
        if posix.is_absolute() or ".." in posix.parts:
            raise ValueError(f"Refusing absolute or '..'-escaping target file: {path!r}")
        if path not in seen:
            seen.add(path)
            found.append(path)
    return found


def inspect_existing_file_contents(root: Path, paths: list[str]) -> dict[str, dict[str, object]]:
    """Read existing files without silently truncating. Oversized files are marked and
    omitted from the prompt rather than shown as a partial body the model is told to
    return complete."""
    inspected: dict[str, dict[str, object]] = {}
    used = 0
    resolved_root = root.resolve()
    for path in paths[:_MAX_FILES]:
        abs_path = (resolved_root / path).resolve()
        if abs_path != resolved_root and resolved_root not in abs_path.parents:
            inspected[path] = {"exists": False, "complete": False, "oversized": False, "content": None, "reason": "outside_root"}
            continue
        if not abs_path.is_file():
            inspected[path] = {"exists": False, "complete": False, "oversized": False, "content": None, "reason": "missing"}
            continue
        try:
            text = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            inspected[path] = {"exists": True, "complete": False, "oversized": False, "content": None, "reason": "unreadable"}
            continue
        if len(text) > _MAX_FILE_CHARS:
            inspected[path] = {
                "exists": True,
                "complete": False,
                "oversized": True,
                "content": None,
                "reason": "oversized",
                "size": len(text),
            }
            continue
        if used + len(text) > _MAX_CHARS:
            inspected[path] = {
                "exists": True,
                "complete": False,
                "oversized": True,
                "content": None,
                "reason": "budget_exceeded",
                "size": len(text),
            }
            continue
        inspected[path] = {"exists": True, "complete": True, "oversized": False, "content": text, "reason": None}
        used += len(text)
    return inspected


def read_existing_file_contents(root: Path, paths: list[str]) -> dict[str, str]:
    """Fully-shown existing files only. Oversized/unreadable files are omitted rather than
    truncated — callers that need the refusal reasons should use inspect_existing_file_contents."""
    return {
        path: str(info["content"])
        for path, info in inspect_existing_file_contents(root, paths).items()
        if info.get("complete") and isinstance(info.get("content"), str)
    }


def prepare_code_agent_prompt(
    description: str,
    root: Path,
    *,
    failure_context: str | None = None,
    extra_target_files: list[str] | None = None,
) -> CodeAgentPrompt:
    explicit = explicit_target_paths(description, extra=extra_target_files)
    inspected = inspect_existing_file_contents(root, explicit)
    fully_shown = frozenset(path for path, info in inspected.items() if info.get("complete"))
    oversized = frozenset(path for path, info in inspected.items() if info.get("oversized"))
    unseen_existing = frozenset(
        path
        for path, info in inspected.items()
        if info.get("exists") and not info.get("complete")
    )

    parts = [description.strip()]
    if explicit:
        parts.append(
            "## Explicit target files\n"
            "Only these paths may be returned in `files`. Existing files may be rewritten "
            "only when their complete, untruncated contents appear below. Oversized or "
            "unreadable existing files must not be rewritten."
        )
        parts.append("\n".join(f"- {path}" for path in explicit))
    else:
        parts.append(
            "## Explicit target files\n"
            "None were named by the planner or task. Do not rewrite any existing file."
        )

    shown = [(path, inspected[path]["content"]) for path in explicit if path in fully_shown]
    if shown:
        parts.append("## Existing file contents (complete and untruncated; return complete new file text)")
        for path, text in shown:
            parts.append(f"### {path}\n```\n{text}\n```")
    else:
        parts.append("## Existing file contents\nNo complete existing-file contents were included.")

    if oversized:
        notes = []
        for path in explicit:
            info = inspected.get(path) or {}
            if not info.get("oversized"):
                continue
            size = info.get("size", "?")
            notes.append(
                f"- {path} is {size} characters and exceeds the per-file context cap of "
                f"{_MAX_FILE_CHARS} (or the total prompt budget). It was not shown. Do not rewrite it."
            )
        if notes:
            parts.append("## Oversized existing files (not shown; do not rewrite)\n" + "\n".join(notes))

    if failure_context:
        parts.append(
            "## Previous verification failure (fix these targeted tests on this retry/replan)\n"
            + failure_context.strip()
        )
    return CodeAgentPrompt(
        text="\n\n".join(parts),
        explicit_paths=tuple(explicit),
        fully_shown_paths=fully_shown,
        oversized_paths=oversized,
        unseen_existing_paths=unseen_existing,
    )


def build_code_agent_user_prompt(description: str, root: Path, *, failure_context: str | None = None) -> str:
    return prepare_code_agent_prompt(description, root, failure_context=failure_context).text
