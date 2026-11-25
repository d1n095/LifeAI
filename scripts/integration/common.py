"""Shared primitives for the LifeAI V8 secure delivery pipeline.

The pipeline is deliberately conservative: it reads Git objects, verifies hashes and
ancestry, and never checks out or executes code while validating an untrusted bundle.
Importing a bundle only creates a namespaced ``refs/imports/...`` ref and an optional
detached worktree; it never updates a branch, never force-pushes and never runs a
repository script.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TARGET_REF = "origin/integration/founder-alpha-20261121"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
STATUSES = ("PASS", "FAIL", "BLOCKED")


class PipelineError(RuntimeError):
    """A deterministic pipeline failure with an explicit status."""

    def __init__(self, message: str, status: str = "FAIL", *, detail: str = ""):
        if status not in STATUSES:
            raise ValueError(f"invalid pipeline status {status!r}")
        super().__init__(message)
        self.status = status
        self.detail = detail


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


def run(
    args: Iterable[str],
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 120,
    check: bool = False,
) -> CommandResult:
    cmd = [str(part) for part in args]
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        raise PipelineError(
            f"command timed out after {timeout}s: {' '.join(cmd)}",
            "BLOCKED",
            detail=f"stdout={stdout[-2000:]}\nstderr={stderr[-2000:]}",
        ) from exc
    result = CommandResult(cmd, completed.returncode, completed.stdout, completed.stderr)
    if check and completed.returncode != 0:
        raise PipelineError(
            f"command failed ({completed.returncode}): {' '.join(cmd)}",
            detail=f"stdout={completed.stdout[-2000:]}\nstderr={completed.stderr[-2000:]}",
        )
    return result


def git(repo: Path | str, *args: str, timeout: int = 120, check: bool = False) -> CommandResult:
    return run(["git", *args], cwd=repo, timeout=timeout, check=check)


def git_output(repo: Path | str, *args: str, timeout: int = 120) -> str:
    return git(repo, *args, timeout=timeout, check=True).stdout.strip()


def git_ok(repo: Path | str, *args: str, timeout: int = 120) -> bool:
    return git(repo, *args, timeout=timeout).returncode == 0


def resolve_commit(repo: Path | str, ref: str) -> str:
    result = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if result.returncode != 0:
        raise PipelineError(f"unknown commit or ref: {ref}", "BLOCKED", detail=result.stderr.strip())
    return result.stdout.strip()


def is_ancestor(repo: Path | str, ancestor: str, descendant: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def merge_base(repo: Path | str, left: str, right: str) -> str | None:
    result = git(repo, "merge-base", left, right)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def commit_metadata(repo: Path | str, sha: str) -> dict[str, Any]:
    raw = git_output(repo, "show", "-s", "--format=%H%x00%P%x00%an%x00%ae%x00%cn%x00%ce%x00%s%x00", sha)
    parts = raw.split("\x00")
    if len(parts) < 7:
        raise PipelineError(f"could not parse commit metadata for {sha}", "FAIL")
    return {
        "sha": parts[0],
        "parents": parts[1].split() if parts[1] else [],
        "author_name": parts[2],
        "author_email": parts[3],
        "committer_name": parts[4],
        "committer_email": parts[5],
        "subject": parts[6],
    }


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_json(path: Path | str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"invalid JSON at {path}: {exc}", "FAIL") from exc


def write_json(path: Path | str, value: Any, *, immutable: bool = False) -> None:
    """Write JSON atomically, or create it exactly once when immutable=True."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if immutable:
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError as exc:
            raise PipelineError(f"refusing to overwrite immutable manifest: {target}", "FAIL") from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, target)


def status_exit(status: str) -> int:
    return {"PASS": 0, "FAIL": 2, "BLOCKED": 3}[status]


def worst_status(statuses: Iterable[str]) -> str:
    values = set(statuses)
    if "FAIL" in values:
        return "FAIL"
    if "BLOCKED" in values:
        return "BLOCKED"
    return "PASS"


def redact_url(url: str | None) -> str | None:
    if not url:
        return url
    parsed = urlparse(url)
    if not parsed.password:
        return url
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    user = parsed.username or ""
    return parsed._replace(netloc=f"{user}:***@{host}").geturl()


def guard_test_database(url: str, label: str = "database") -> None:
    """Refuse any database that is not an explicitly disposable loopback test DB."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    name = (parsed.path or "").lstrip("/")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise PipelineError(f"{label} must be loopback for staging tests, got host {host!r}", "BLOCKED")
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise PipelineError(f"{label} has an unsafe database name: {name!r}", "FAIL")
    if not re.search(r"(test|staging|pytest)", name, re.IGNORECASE):
        raise PipelineError(
            f"{label} name must contain test/staging/pytest; refusing to risk non-test data: {name!r}",
            "BLOCKED",
        )


def guard_redis(url: str, label: str = "redis") -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise PipelineError(f"{label} must be loopback for staging tests, got host {host!r}", "BLOCKED")


def safe_repo_path(repo: Path | str, value: str | Path) -> Path:
    """Resolve a path and require it to stay inside the repository."""
    root = Path(repo).resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved == root or root not in resolved.parents:
        raise PipelineError(f"path escapes repository: {value}", "FAIL")
    return resolved


def safe_tree_path(path: str) -> tuple[bool, str]:
    if not path or "\x00" in path or "\\" in path or any(ord(char) < 32 for char in path):
        return False, "control character, NUL, or backslash in path"
    if path.startswith("/"):
        return False, "absolute path"
    pure = PurePosixPath(path)
    if any(part in {"", ".", "..", ".git"} for part in pure.parts):
        return False, "dot, parent, or .git path component"
    return True, ""


def _normalise_manifest_entries(raw: Any, base_dir: Path) -> list[tuple[Path, str]]:
    entries: list[tuple[Path, str]] = []
    if isinstance(raw, dict):
        if "artifacts" in raw:
            raw = raw["artifacts"]
        elif "files" in raw:
            raw = raw["files"]
        elif "bundle" in raw and isinstance(raw["bundle"], dict):
            raw = [raw["bundle"]]
        else:
            raw = [{"path": key, "sha256": value} for key, value in raw.items()]
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                raise PipelineError("manifest artifact entries must be objects", "FAIL")
            path = item.get("path") or item.get("file")
            digest = item.get("sha256") or item.get("hash")
            if not path or not digest:
                raise PipelineError("manifest artifact entry is missing path/sha256", "FAIL")
            entries.append((base_dir / str(path), str(digest).lower()))
    else:
        raise PipelineError("manifest artifacts must be a list or object", "FAIL")
    return entries


def parse_manifest(path: Path | str, *, base_dir: Path | str | None = None) -> list[tuple[Path, str]]:
    """Parse SHA256SUMS or a JSON artifact manifest into absolute path/hash pairs."""
    manifest = Path(path).resolve()
    root = Path(base_dir).resolve() if base_dir else manifest.parent
    text = manifest.read_text(encoding="utf-8")
    if manifest.suffix.lower() == ".json" or text.lstrip().startswith("{"):
        return _normalise_manifest_entries(read_json(manifest), root)
    entries: list[tuple[Path, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line)
        if not match:
            raise PipelineError(f"invalid SHA256SUMS line {line_number}", "FAIL")
        digest, name = match.group(1).lower(), match.group(2)
        if name.startswith("/") or ".." in PurePosixPath(name).parts:
            raise PipelineError(f"manifest path escapes its directory: {name}", "FAIL")
        entries.append((root / name, digest))
    if not entries:
        raise PipelineError("manifest contains no artifacts", "FAIL")
    return entries


def verify_manifest(
    manifest_path: Path | str,
    *,
    required_path: Path | str | None = None,
    base_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Verify every artifact in a manifest and return the matching required hash."""
    manifest = Path(manifest_path).resolve()
    entries = parse_manifest(manifest, base_dir=base_dir)
    seen: set[Path] = set()
    verified: list[dict[str, str]] = []
    required = Path(required_path).resolve() if required_path else None
    required_hash: str | None = None
    for artifact, expected in entries:
        resolved = artifact.resolve()
        if resolved in seen:
            raise PipelineError(f"duplicate manifest entry: {artifact}", "FAIL")
        seen.add(resolved)
        if not resolved.is_file():
            raise PipelineError(f"manifest artifact is missing: {artifact}", "FAIL")
        actual = sha256_file(resolved)
        if actual != expected:
            raise PipelineError(
                f"SHA-256 mismatch for {artifact.name}: expected {expected}, got {actual}", "FAIL"
            )
        verified.append({"path": str(artifact), "sha256": actual})
        if required is not None and resolved == required:
            required_hash = actual
    if required is not None and required_hash is None:
        raise PipelineError(f"required artifact is not listed in the manifest: {required}", "FAIL")
    return {
        "manifest": str(manifest),
        "manifest_sha256": sha256_file(manifest),
        "artifacts": verified,
        "required_sha256": required_hash,
    }


SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret_key", re.compile(r"(?i)\baws_secret_access_key\b\s*[:=]\s*[\"']?[A-Za-z0-9/+=]{40}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
)

CREDENTIAL_PATH = re.compile(
    r"(^|/)(?:\.env(?:\.[^/]+)?|"
    r"id_rsa|id_ed25519|credentials(?:\.json)?|"
    r"[^/]+\.(?:pem|p12|pfx))$",
    re.IGNORECASE,
)
TEST_DOC_PATH = re.compile(
    r"(^|/)(?:tests?|docs?|evidence|delivery)(/|$)|\.(?:md|xml)$",
    re.IGNORECASE,
)


def _is_test_or_doc(path: str) -> bool:
    return bool(TEST_DOC_PATH.search(path))


def _is_template_env(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name.endswith((".example", ".sample", ".template"))


def _has_private_key_material(text: str) -> bool:
    """A header alone is a redaction marker; a real key has a base64 body and footer."""
    match = re.search(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----(.*?)-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
        text,
        re.DOTALL,
    )
    if not match:
        return False
    body = re.sub(r"\s+", "", match.group(1))
    return len(body) >= 64 and re.fullmatch(r"[A-Za-z0-9+/=]+", body) is not None


def scan_tree(repo: Path | str, sha: str, *, base: str | None = None) -> dict[str, Any]:
    """Scan a commit's tree and messages without checking it out or executing it.

    Findings intentionally contain only paths, commit IDs and rule names -- never the
    matching source line, because the line may itself be the secret.
    """
    tree = git(repo, "ls-tree", "-r", "-z", "--full-tree", sha, check=True)
    unsafe: list[dict[str, str]] = []
    symlinks: list[str] = []
    submodules: list[str] = []
    credential_files: list[str] = []
    files: list[str] = []
    for record in tree.stdout.split("\x00"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        fields = meta.split()
        if len(fields) != 3:
            raise PipelineError(f"malformed git ls-tree record for {sha}", "FAIL")
        mode, kind, _object = fields
        ok, reason = safe_tree_path(path)
        if not ok:
            unsafe.append({"path": path, "reason": reason})
            continue
        files.append(path)
        if kind == "commit" or mode == "160000":
            submodules.append(path)
        elif mode == "120000":
            symlinks.append(path)
        if CREDENTIAL_PATH.search(path) and not _is_template_env(path):
            credential_files.append(path)

    findings: list[dict[str, str]] = []
    review_findings: list[dict[str, str]] = []
    prefix = f"{sha}:"
    for rule, pattern in SECRET_PATTERNS:
        result = git(
            repo,
            "grep",
            "-I",
            "-l",
            "-P",
            "-e",
            pattern.pattern,
            sha,
            "--",
            ".",
        )
        if result.returncode not in (0, 1):
            raise PipelineError(f"git grep failed while scanning {sha}", "FAIL", detail=result.stderr)
        for raw_path in sorted(set(result.stdout.splitlines())):
            if not raw_path:
                continue
            path = raw_path[len(prefix):] if raw_path.startswith(prefix) else raw_path
            if rule == "private_key":
                content = git(repo, "show", f"{sha}:{path}").stdout
                if not _has_private_key_material(content):
                    continue
            item = {"kind": "content", "rule": rule, "path": path}
            if _is_test_or_doc(path):
                review_findings.append(item)
            else:
                findings.append(item)

    message_findings: list[dict[str, str]] = []
    log_args = ["log", "--format=%H%x00%B%x00", sha]
    if base:
        log_args.extend(["--not", base])
    messages = git(repo, *log_args)
    if messages.returncode == 0:
        parts = messages.stdout.split("\x00")
        for index in range(0, len(parts) - 1, 2):
            commit = parts[index].strip()
            message = parts[index + 1]
            if not COMMIT_RE.fullmatch(commit):
                continue
            for rule, pattern in SECRET_PATTERNS:
                if pattern.search(message):
                    message_findings.append({"kind": "commit_message", "rule": rule, "commit": commit})

    return {
        "status": "FAIL" if (unsafe or symlinks or submodules or credential_files or findings or message_findings) else "PASS",
        "review_required": bool(review_findings),
        "scanned_files": len(files),
        "unsafe_paths": unsafe,
        "symlinks": symlinks,
        "submodules": submodules,
        "credential_files": credential_files,
        "findings": findings,
        "review_findings": review_findings,
        "message_findings": message_findings,
    }


def check_no_secret_strings(text: str, *, label: str = "manifest") -> None:
    for rule, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise PipelineError(f"{label} contains a high-confidence secret pattern ({rule})", "FAIL")


def worktree_add(repo: Path | str, path: Path | str, sha: str) -> Path:
    target = Path(path).resolve()
    if target.exists():
        raise PipelineError(f"worktree path already exists: {target}", "FAIL")
    target.parent.mkdir(parents=True, exist_ok=True)
    result = git(repo, "worktree", "add", "--detach", str(target), sha)
    if result.returncode != 0:
        raise PipelineError(f"could not create isolated worktree: {result.stderr.strip()}", "FAIL")
    return target


def worktree_remove(repo: Path | str, path: Path | str) -> None:
    target = Path(path).resolve()
    if not target.exists():
        git(repo, "worktree", "prune")
        return
    result = git(repo, "worktree", "remove", "--force", str(target))
    if result.returncode != 0:
        # Only remove a path this pipeline created, and only after Git has stopped
        # tracking it. Refuse to guess about arbitrary directories.
        if (target / ".git").exists() or (target / ".git").is_file():
            shutil.rmtree(target)
        else:
            raise PipelineError(f"could not remove worktree {target}: {result.stderr.strip()}", "FAIL")
    git(repo, "worktree", "prune")


def temporary_directory(prefix: str = "lifeai-v8-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))
