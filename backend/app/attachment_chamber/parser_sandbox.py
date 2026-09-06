"""Attachment Chamber -- parser sandbox contract (Milestone 5).

Interfaces only -- NOT a real sandboxed parser, matching how app.sovereign_identity.hardware's
hardware-backed-key interfaces were built: honest stubs with an explicit "not really
sandboxed yet" status, never claiming more than exists. Real enforcement of CPU/time/memory
limits needs an actual subprocess/cgroup/rlimit mechanism -- out of scope for this round,
documented honestly rather than faked.

The structural defense is in the TYPE SIGNATURE: ParserSandbox.parse() has no parameter that
COULD carry network access, Vault access, shell authority, or ambient secrets. The absence
of such a parameter is the actual defense, matching this whole campaign's "structural, not
just documented" discipline -- not a docstring promise a future implementer could ignore.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SandboxEnforcementStatus:
    """Not an enum of real enforcement levels -- a single honest constant, since this
    foundation stage has exactly one real status: NOT_ENFORCED. A future round that adds
    real OS-level enforcement should replace this with a real closed vocabulary reflecting
    what's ACTUALLY enforced, not add fake intermediate levels now."""

    NOT_ENFORCED = "NOT_ENFORCED"


@dataclass(frozen=True)
class ParserBudget:
    """Represents intended bounds -- NOT actually enforced by this foundation stage (see
    module docstring). enforcement_status is always NOT_ENFORCED here; a real implementation
    would need a genuine subprocess/cgroup/rlimit mechanism to make these real."""

    max_cpu_seconds: float
    max_wall_seconds: float
    max_memory_bytes: int
    enforcement_status: str = SandboxEnforcementStatus.NOT_ENFORCED


@dataclass(frozen=True)
class BoundedFileHandle:
    """What a ParserSandbox.parse() implementation actually receives -- deliberately just
    bytes and a filename hint. No network client, no Vault reference, no shell/subprocess
    handle, no ambient credential -- there is structurally nothing here a malicious parser
    implementation could misuse beyond the bytes it was explicitly given."""

    content: bytes
    filename_hint: str


@dataclass(frozen=True)
class ParserOutput:
    extracted_text: str | None
    structured_data: dict | None


@dataclass(frozen=True)
class ParserFailureResult:
    exception_type: str
    message: str
    occurred_at: datetime


class ParserSandbox(Protocol):
    """The seam a future real parser plugs into. This module supplies NO real sandboxed
    implementation -- see run_parser_safely()."""

    def parse(self, handle: BoundedFileHandle, budget: ParserBudget) -> ParserOutput: ...


def run_parser_safely(sandbox: ParserSandbox, handle: BoundedFileHandle, budget: ParserBudget) -> ParserOutput | ParserFailureResult:
    """PARSER CRASH MUST NOT CRASH MAINAI CORE: the ONLY sanctioned way to invoke a
    ParserSandbox.parse() implementation. Catches every exception (deliberately broad --
    a hostile or buggy parser implementation could raise anything) and converts it into a
    typed ParserFailureResult; never lets an exception from `sandbox.parse()` propagate
    past this function."""
    try:
        return sandbox.parse(handle, budget)
    except BaseException as exc:  # noqa: BLE001 -- deliberately catches everything, see docstring
        return ParserFailureResult(exception_type=type(exc).__name__, message=str(exc), occurred_at=_utcnow())
