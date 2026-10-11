"""Read existing files into the Path A code-agent prompt. Not a second client — it only
reads from a local checkout the executor already owns (shared repo root or a worktree)."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

_PATH_RE = re.compile(
    r"(?:^|[\s`'\"(\[])((?:backend|frontend|docs|app|tests|scripts)/[A-Za-z0-9_./-]+\.[A-Za-z0-9]+)"
)
_MAX_FILES = 20
_MAX_CHARS = 80_000
_MAX_FILE_CHARS = 16_000


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


def read_existing_file_contents(root: Path, paths: list[str]) -> dict[str, str]:
    contents: dict[str, str] = {}
    used = 0
    resolved_root = root.resolve()
    for path in paths[:_MAX_FILES]:
        abs_path = (resolved_root / path).resolve()
        if abs_path != resolved_root and resolved_root not in abs_path.parents:
            continue
        if not abs_path.is_file():
            continue
        try:
            text = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(text) > _MAX_FILE_CHARS:
            text = text[:_MAX_FILE_CHARS] + "\n...[truncated]...\n"
        if used + len(text) > _MAX_CHARS:
            break
        contents[path] = text
        used += len(text)
    return contents


def build_code_agent_user_prompt(description: str, root: Path, *, failure_context: str | None = None) -> str:
    paths = candidate_paths_from_text(description)
    existing = read_existing_file_contents(root, paths)
    parts = [description.strip()]
    if existing:
        parts.append("## Existing file contents (read from the current checkout; return complete new file text)")
        for path, text in existing.items():
            parts.append(f"### {path}\n```\n{text}\n```")
    else:
        parts.append("## Existing file contents\nNo matching existing files were found in the checkout for paths named in the task.")
    if failure_context:
        parts.append("## Previous verification failure (fix these targeted tests on this retry/replan)\n" + failure_context.strip())
    return "\n\n".join(parts)
