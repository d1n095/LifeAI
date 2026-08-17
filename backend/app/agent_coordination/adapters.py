"""The AgentAdapter shape -- defines the interface a future, separately-reviewed Agent Runtime
would implement to actually drive an external CLI/API coding agent (Claude Code, Cursor Agent,
Codex, ...) against a `AgentWorkAssignment`.

NOTHING in this module invokes a real external agent, opens a subprocess, makes a network
call, or reads/stores a credential -- this foundation defines only the SHAPE future code would
implement, exactly like `app.provider_planning.service.PlanningAdapter` already does for
provider-assisted planning (a `typing.Protocol`, not a base class instances are required to
inherit from). A concrete implementation is deliberately out of scope here; see
docs/LIFE_MULTI_AGENT_WORK_COORDINATION.md's "Future Agent Runtime integration" section for
what a real implementation would need to add (process/session management, credential handling
outside this codebase, output streaming, cancellation semantics) and why none of that belongs
in a coordination-layer foundation PR."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class AgentHealth:
    healthy: bool
    detail: str = ""


@dataclass(frozen=True)
class AgentObservation:
    """A point-in-time read of an in-flight assignment's external state -- never itself an
    authority to change `AgentWorkAssignment.status`; a caller still goes through
    `app.agent_coordination.service.transition_status()` to make any observed change durable."""

    raw_status: str
    summary: str = ""
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AgentResult:
    """What a completed/failed assignment produced -- pointers only (branch/PR/commit refs),
    never raw diff/log content this coordination layer would have to trust blindly; see
    `AgentWorkAssignment.output_refs` for where a caller records these durably."""

    succeeded: bool
    output_refs: tuple[dict, ...] = ()
    summary: str = ""


class AgentAdapter(Protocol):
    """One adapter per `CoordinationAgent` kind. A concrete implementation is NOT part of this
    foundation -- see module docstring."""

    async def health(self) -> AgentHealth: ...

    def capabilities(self) -> tuple[str, ...]: ...

    async def start_assignment(self, assignment_id: UUID) -> None: ...

    async def send_instruction(self, assignment_id: UUID, instruction: str) -> None: ...

    async def observe(self, assignment_id: UUID) -> AgentObservation: ...

    async def cancel(self, assignment_id: UUID) -> None: ...

    async def resume(self, assignment_id: UUID) -> None: ...

    async def collect_result(self, assignment_id: UUID) -> AgentResult: ...


class ProviderNotConfiguredError(RuntimeError):
    """Raised by every `NotConfiguredAdapter` method below -- the explicit, honest signal that
    no real external agent invocation is wired up yet for a given provider. Never returns a
    fabricated success, a fabricated `AgentObservation`, or a fabricated `AgentResult`.
    `app.agent_coordination.dispatch.dispatch_assignment()` treats this as a hard, structured
    dispatch failure (`REAL_PROVIDER_NOT_CONFIGURED`), never a crash to propagate raw."""


class NotConfiguredAdapter:
    """The REAL default `AgentAdapter` for every provider until a genuine, separately-reviewed
    Agent Runtime implementation exists (see module docstring's own "process/session
    management, credential handling outside this codebase, output streaming, cancellation
    semantics" list of what that would need to add). Implements the FULL `AgentAdapter` shape
    so it satisfies the Protocol structurally, but every method that would actually touch an
    external agent raises `ProviderNotConfiguredError` -- this class opens no subprocess,
    makes no network call, and reads no credential, by construction; there is nothing in it
    capable of doing any of those things."""

    def __init__(self, *, provider_key: str):
        self._provider_key = provider_key

    async def health(self) -> AgentHealth:
        return AgentHealth(healthy=False, detail=f"REAL_PROVIDER_NOT_CONFIGURED: no Agent Runtime is wired up for '{self._provider_key}' yet")

    def capabilities(self) -> tuple[str, ...]:
        return ()

    async def start_assignment(self, assignment_id: UUID) -> None:
        raise ProviderNotConfiguredError(
            f"REAL_PROVIDER_NOT_CONFIGURED: cannot start_assignment for '{self._provider_key}' -- no real Agent Runtime is wired up yet"
        )

    async def send_instruction(self, assignment_id: UUID, instruction: str) -> None:
        raise ProviderNotConfiguredError(f"REAL_PROVIDER_NOT_CONFIGURED: cannot send_instruction for '{self._provider_key}'")

    async def observe(self, assignment_id: UUID) -> AgentObservation:
        raise ProviderNotConfiguredError(f"REAL_PROVIDER_NOT_CONFIGURED: cannot observe for '{self._provider_key}'")

    async def cancel(self, assignment_id: UUID) -> None:
        raise ProviderNotConfiguredError(f"REAL_PROVIDER_NOT_CONFIGURED: cannot cancel for '{self._provider_key}'")

    async def resume(self, assignment_id: UUID) -> None:
        raise ProviderNotConfiguredError(f"REAL_PROVIDER_NOT_CONFIGURED: cannot resume for '{self._provider_key}'")

    async def collect_result(self, assignment_id: UUID) -> AgentResult:
        raise ProviderNotConfiguredError(f"REAL_PROVIDER_NOT_CONFIGURED: cannot collect_result for '{self._provider_key}'")
