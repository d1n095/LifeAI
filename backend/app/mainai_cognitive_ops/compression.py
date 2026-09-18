"""Compression / Recovery Checkpoints. See docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md.

A compressed checkpoint must be sufficient for a fresh competent agent to resume without
rereading the entire history. COMPRESSION MUST PRESERVE TRACEABILITY. SESSION RESET != PROGRAM
RESET. PROCESS DEATH != PROGRAM LOSS.

`RecoveryCheckpoint` names every field §19 requires. `validate_checkpoint_reconstructability()`
is the §15 compression-quality check: if a required field is empty, compression was too
aggressive. `parse_handoff_markdown()` is a REAL check against an actual handoff file on disk
(this project's own `docs/mainai_v2/HANDOFF_*.md` convention), not a hypothetical schema."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

REQUIRED_CHECKPOINT_FIELDS = (
    "current_objective", "why", "program", "status", "exact_sha", "branch", "worktree",
    "active_agent", "dependencies", "what_was_tried", "what_failed", "what_passed",
    "important_findings", "test_evidence", "next_action", "do_not_repeat",
    "authority_boundaries", "unresolved_questions", "remote_backup_state",
)
# open_p0/open_p1 are allowed to be empty tuples (no known issues is a valid, honest state).


@dataclass(frozen=True)
class RecoveryCheckpoint:
    current_objective: str
    why: str
    program: str
    status: str
    exact_sha: str
    branch: str
    worktree: str
    active_agent: str
    open_p0: tuple[str, ...] = field(default_factory=tuple)
    open_p1: tuple[str, ...] = field(default_factory=tuple)
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    what_was_tried: tuple[str, ...] = field(default_factory=tuple)
    what_failed: tuple[str, ...] = field(default_factory=tuple)
    what_passed: tuple[str, ...] = field(default_factory=tuple)
    important_findings: tuple[str, ...] = field(default_factory=tuple)
    test_evidence: str = ""
    next_action: str = ""
    do_not_repeat: tuple[str, ...] = field(default_factory=tuple)
    authority_boundaries: tuple[str, ...] = field(default_factory=tuple)
    unresolved_questions: tuple[str, ...] = field(default_factory=tuple)
    remote_backup_state: str = ""


def validate_checkpoint_reconstructability(checkpoint: RecoveryCheckpoint) -> tuple[str, ...]:
    """Returns the required fields that are empty/falsy. A non-empty result means compression
    was too aggressive -- §15's own stated failure condition."""

    missing = []
    for name in REQUIRED_CHECKPOINT_FIELDS:
        value = getattr(checkpoint, name)
        if not value:
            missing.append(name)
    return tuple(missing)


def parse_handoff_markdown(path: str) -> dict[str, bool]:
    """Reads a real `docs/mainai_v2/HANDOFF_*.md` file and reports, per required section,
    whether a matching `## ` heading is present. This is the concrete, file-backed version of
    `validate_checkpoint_reconstructability()` for the markdown convention this project already
    uses, rather than a hypothetical in-memory-only schema."""

    text = Path(path).read_text(encoding="utf-8")
    headings = {h.strip().upper() for h in re.findall(r"^##\s+(.+)$", text, flags=re.MULTILINE)}

    required_sections = (
        "CURRENT OBJECTIVE", "EXACT SHA", "WHAT IS IMPLEMENTED", "WHAT IS NOT IMPLEMENTED",
        "TEST EVIDENCE", "KNOWN P0", "KNOWN P1", "NEXT REVIEW REQUIRED",
    )
    return {section: any(section in h for h in headings) for section in required_sections}
