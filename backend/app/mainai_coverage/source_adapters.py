"""Source Adapters -- real, read-only ingestion from durable sources this repo can actually
reach. See docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md §A.

DURABLE SOURCES -> SOURCE ADAPTERS -> NORMALIZED OBSERVATIONS. Every adapter here reads a REAL
file or REAL git history via fixed, read-only operations (no shell=True, no string-interpolated
subprocess argv, mirroring `app.mainai_cognitive_ops.repo_backup_intelligence`'s own safety
discipline). `conversation_history_availability()` is the one source this codebase genuinely
cannot reach -- UNKNOWN != ABSENT: it is reported explicitly unavailable, never fabricated."""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.mainai_coverage.types import ExtractionMethod, NormalizedObservation, SourceAvailability, SourceKind

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", flags=re.MULTILINE)


class SourceAdapterError(RuntimeError):
    """Raised when a source that IS supposed to be available fails to read (e.g. a path that
    was supposed to exist does not) -- never raised for a source that is honestly unavailable
    (see `SourceAvailability` for that case instead)."""


def _split_markdown_sections(text: str) -> list[tuple[str, str]]:
    """Splits on `#`/`##`/`###` headings -- returns (heading_text, body_text) pairs. A
    deterministic structural split, never a semantic one."""

    matches = list(_HEADING_RE.finditer(text))
    sections = []
    for i, match in enumerate(matches):
        heading = match.group(2).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        sections.append((heading, body))
    return sections


def ingest_markdown_doc(path: str, *, source_kind: SourceKind) -> tuple[NormalizedObservation, ...]:
    """Real file read. One `NormalizedObservation` per `#`/`##`/`###` section -- confidence is
    high (0.8) because a full section body is a direct, unambiguous extraction, never a guess."""

    file_path = Path(path)
    if not file_path.is_file():
        raise SourceAdapterError(f"{path} does not exist -- this source was expected to be available")

    text = file_path.read_text(encoding="utf-8")
    mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
    sections = _split_markdown_sections(text)
    if not sections:
        return (NormalizedObservation(
            source_kind=source_kind, source_identifier=str(file_path), text=text[:2000], location=None,
            timestamp=mtime, exact_sha=None, extraction_method=ExtractionMethod.MARKDOWN_HEADING_SECTION, confidence=0.4,
        ),)

    return tuple(
        NormalizedObservation(
            source_kind=source_kind, source_identifier=str(file_path), text=f"{heading}\n{body}"[:4000],
            location=heading, timestamp=mtime, exact_sha=None,
            extraction_method=ExtractionMethod.MARKDOWN_HEADING_SECTION, confidence=0.8,
        )
        for heading, body in sections
    )


def discover_docs(docs_dir: str, *, name_predicate) -> tuple[str, ...]:
    """Real, bounded directory scan (non-recursive by default caller usage) -- returns matching
    file paths. `name_predicate(filename) -> bool` lets the caller decide what counts as e.g. a
    handoff doc vs a reconciliation doc, without this function hardcoding a naming convention
    that could silently miss a real, differently-named future doc."""

    base = Path(docs_dir)
    if not base.is_dir():
        return ()
    return tuple(str(p) for p in sorted(base.glob("*.md")) if name_predicate(p.name))


def ingest_branch_registry(path: str) -> tuple[NormalizedObservation, ...]:
    """`docs/BRANCH_REGISTRY.md` is a markdown table, not heading-sectioned prose -- one
    observation per real table row (a program/branch entry), split on the `|` delimiters this
    project's own registry convention uses."""

    file_path = Path(path)
    if not file_path.is_file():
        raise SourceAdapterError(f"{path} does not exist -- this source was expected to be available")

    text = file_path.read_text(encoding="utf-8")
    mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
    observations = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped.startswith("|---") or set(stripped) <= {"|", "-", " "}:
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2 or cells[0] in ("Sub-program", "Program", ""):
            continue
        observations.append(
            NormalizedObservation(
                source_kind=SourceKind.BRANCH_REGISTRY, source_identifier=str(file_path), text=" | ".join(cells)[:4000],
                location=cells[0][:120], timestamp=mtime, exact_sha=None,
                extraction_method=ExtractionMethod.MARKDOWN_TABLE_ROW, confidence=0.7,
            )
        )
    return tuple(observations)


def ingest_git_commit_log(repo_path: str, *, max_commits: int = 200) -> tuple[NormalizedObservation, ...]:
    """Real, read-only `git log` (fixed argv, `shell=False`, no string interpolation of
    untrusted input -- `max_commits` is the only variable part and is always an int, never
    passed through a shell). One observation per commit subject line, with the real commit SHA
    as provenance."""

    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "log", f"-{int(max_commits)}", "--pretty=format:%H%x1f%ct%x1f%s"],
            capture_output=True, text=True, timeout=15, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise SourceAdapterError(f"git log failed in {repo_path}: {exc}") from exc

    observations = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        sha, epoch_str, subject = parts
        observations.append(
            NormalizedObservation(
                source_kind=SourceKind.GIT_COMMIT_LOG, source_identifier=f"git:{repo_path}", text=subject,
                location=None, timestamp=datetime.fromtimestamp(int(epoch_str), tz=timezone.utc), exact_sha=sha,
                extraction_method=ExtractionMethod.GIT_LOG_SUBJECT, confidence=0.9,
            )
        )
    return tuple(observations)


def ingest_test_evidence(tests_dir: str, *, keyword: str) -> tuple[NormalizedObservation, ...]:
    """Real, bounded filesystem scan for test files whose NAME contains `keyword` -- evidence
    that *some* test exists for a capability, never a claim about what it actually asserts
    (that would require reading and understanding the test body, which this deterministic scan
    does not attempt)."""

    base = Path(tests_dir)
    if not base.is_dir():
        return ()
    matches = sorted(p for p in base.rglob(f"test_*{keyword}*.py"))
    if not matches:
        return ()
    return tuple(
        NormalizedObservation(
            source_kind=SourceKind.TEST_EVIDENCE, source_identifier=str(p), text=p.stem,
            location=None, timestamp=datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc), exact_sha=None,
            extraction_method=ExtractionMethod.TEST_FILE_SCAN, confidence=0.6,
        )
        for p in matches
    )


def conversation_history_availability() -> SourceAvailability:
    """UNKNOWN != ABSENT: conversation/chat history is explicitly reported as NOT ingested --
    this codebase has no durable store of it anywhere (confirmed by direct inspection of
    `app/` for any conversation-transcript table/model; none exists). A future durable store
    (e.g. a MainAI memory-thread table) would get its own adapter function here, not a
    fabricated read of something that does not currently exist."""

    return SourceAvailability(
        source_kind=SourceKind.CONVERSATION_HISTORY, available=False,
        reason="no durable conversation/chat-history store exists anywhere in this codebase as of this branch -- not ingested, not simulated",
    )
