"""The AgentAdapter shape -- defines the interface an Agent Runtime implements to actually
drive an external CLI/API coding agent (Claude Code, Cursor Agent, Codex, ...) against an
`AgentWorkAssignment`, exactly like `app.provider_planning.service.PlanningAdapter` already
does for provider-assisted planning (a `typing.Protocol`, not a base class instances are
required to inherit from).

`NotConfiguredAdapter` is the REAL default for every provider -- invokes nothing, ever.
`LocalCLIAdapter` is the one bounded, provider-neutral REAL implementation this module
contains: a single subprocess-invocation mechanism (never per-provider duplicated logic) that
remains fully inert unless `app.agent_coordination.adapter_config.get_real_adapter()` (the
founder-controlled enablement gate) explicitly constructs it. See that module's own docstring
for the five-way supported/executable-found/credentials/enabled/dispatch-authorized
distinction and why none of this reads a credential, a secret, or a session token."""

from __future__ import annotations

import asyncio
import shutil
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


class AdapterProcessLostError(RuntimeError):
    """The real adapter's subprocess could not be started, or was being tracked and is no
    longer traceable -- distinct from `ProviderNotConfiguredError` (which means "never even
    tried") and distinct from an ordinary non-zero exit code (captured in
    `AgentResult.succeeded=False`, not raised as an exception). Life must never read this as
    "completed" -- see `app.agent_coordination.dispatch.dispatch_assignment()`'s own handling."""


class AdapterTimeoutError(RuntimeError):
    """The real adapter's subprocess exceeded its configured, ALWAYS-present bound. The
    process is killed before this is raised -- never left running unbounded in the
    background."""


class LocalCLIAdapter:
    """Provider-neutral real adapter over a LOCAL CLI executable -- the SAME bounded
    subprocess mechanism serves Claude Code, Cursor Agent, or Codex, parametrized entirely by
    externally-supplied configuration (`executable`/`args_template`/`timeout_seconds`, from
    `app.agent_coordination.adapter_config.real_adapter_config()`) -- never a per-provider
    duplicated implementation.

    Every invocation is bounded, by construction:
    - exact `cwd` -- the assignment's own `worktree_path`, never inferred, never the caller's
      own process working directory;
    - a real, always-present `timeout_seconds` -- `asyncio.wait_for()` enforces it and the
      process is killed on expiry, never left running;
    - list-form `argv` via `asyncio.create_subprocess_exec()` -- NEVER `shell=True`, NEVER a
      string command a shell would re-interpret; there is no code path in this class capable
      of unrestricted shell passthrough;
    - a minimized environment -- only what the caller explicitly supplies via `env`, never a
      blind inheritance of this process's own full environment (which could leak unrelated
      secrets this adapter has no business seeing).

    `send_instruction()`/`resume()` are deliberately `NotImplementedError` -- this is a bounded,
    single-shot, non-interactive invocation (start, wait up to the bound, capture the result),
    not an interactive session this coordination layer would have to trust mid-flight."""

    def __init__(
        self,
        *,
        provider_key: str,
        executable: str,
        args_template: tuple[str, ...],
        cwd: str,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ):
        self._provider_key = provider_key
        self._executable = executable
        self._args_template = args_template
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds
        self._env = dict(env or {})
        self._processes: dict[UUID, asyncio.subprocess.Process] = {}

    async def health(self) -> AgentHealth:
        found = shutil.which(self._executable) is not None
        return AgentHealth(healthy=found, detail=f"executable '{self._executable}' {'found' if found else 'NOT found'} on PATH")

    def capabilities(self) -> tuple[str, ...]:
        return ("repo_edit", "read_only_review", "run_tests")

    async def start_assignment(self, assignment_id: UUID) -> None:
        argv = [self._executable, *self._args_template]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=self._cwd, env=self._env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, FileNotFoundError) as exc:
            raise AdapterProcessLostError(f"failed to start '{self._executable}' for provider '{self._provider_key}': {exc}") from exc
        self._processes[assignment_id] = proc

    async def send_instruction(self, assignment_id: UUID, instruction: str) -> None:
        raise NotImplementedError("LocalCLIAdapter is a bounded, single-shot invocation -- interactive mid-run instructions are out of scope")

    async def observe(self, assignment_id: UUID) -> AgentObservation:
        proc = self._processes.get(assignment_id)
        if proc is None:
            return AgentObservation(raw_status="not_started")
        if proc.returncode is None:
            return AgentObservation(raw_status="running")
        return AgentObservation(raw_status="exited", detail={"returncode": proc.returncode})

    async def cancel(self, assignment_id: UUID) -> None:
        proc = self._processes.get(assignment_id)
        if proc is not None and proc.returncode is None:
            proc.terminate()

    async def resume(self, assignment_id: UUID) -> None:
        raise NotImplementedError("LocalCLIAdapter is a bounded, single-shot invocation -- resume is out of scope")

    async def collect_result(self, assignment_id: UUID) -> AgentResult:
        proc = self._processes.get(assignment_id)
        if proc is None:
            raise AdapterProcessLostError(f"no tracked process for assignment {assignment_id} -- start_assignment() was never called or it failed silently")
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout_seconds)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise AdapterTimeoutError(f"provider '{self._provider_key}' exceeded its {self._timeout_seconds}s bound; process killed") from exc
        succeeded = proc.returncode == 0
        return AgentResult(
            succeeded=succeeded,
            output_refs=({"kind": "stdout_bytes", "length": len(stdout)}, {"kind": "stderr_bytes", "length": len(stderr)}),
            summary=f"'{self._provider_key}' exited with code {proc.returncode}",
        )


def get_real_adapter(provider_key: str, *, cwd: str, env: dict[str, str] | None = None) -> "AgentAdapter":
    """The founder-controlled factory: returns a real `LocalCLIAdapter` ONLY when
    `app.agent_coordination.adapter_config.real_adapter_config()` confirms every precondition
    (enabled, executable found, an explicit args_template configured); returns
    `NotConfiguredAdapter` -- the same honest, fail-closed default -- in every other case.
    NEVER silently substitutes a fake/mock adapter; callers that want deterministic test
    behavior construct their own fake explicitly (see
    tests/backend/mainai/test_agent_dispatch_foundation.py's own `_FakeAgentAdapter`)."""

    from app.agent_coordination.adapter_config import real_adapter_config

    config = real_adapter_config(provider_key)
    if config is None:
        return NotConfiguredAdapter(provider_key=provider_key)
    executable, args_template, timeout_seconds = config
    return LocalCLIAdapter(provider_key=provider_key, executable=executable, args_template=args_template, cwd=cwd, timeout_seconds=timeout_seconds, env=env)
