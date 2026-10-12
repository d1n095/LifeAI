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
import json
import re
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
    """What a completed/failed assignment produced -- pointers (branch/PR/commit refs) plus,
    for a conversational CLI agent, the agent's own bounded final message and session id.
    The final message is the agent's SELF-REPORT: recorded verbatim (bounded), never treated
    as verified truth about the repository; see `AgentWorkAssignment.output_refs` for where a
    caller records these durably."""

    succeeded: bool
    output_refs: tuple[dict, ...] = ()
    summary: str = ""
    final_message: str | None = None
    session_id: str | None = None
    structured: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AdapterCapabilities:
    """What an adapter INSTANCE actually declares it can do -- a truthful, per-provider fact,
    never a wishful default. `app.agent_coordination.execution_control` fails closed on every
    one of these: attempting `send_instruction()` against an adapter that declares
    `supports_instruction=False` is rejected as `UNSUPPORTED_CAPABILITY` before the adapter's
    own method is ever called, never a silent no-op and never a raised
    `NotImplementedError` a caller would have to specifically catch. Do not pretend every
    provider exposes the same fidelity -- an honest `False` here is correct, not a
    limitation to work around."""

    supports_streaming: bool = False
    supports_instruction: bool = False
    supports_resume: bool = False
    supports_cancel: bool = False
    supports_structured_events: bool = False


class AgentAdapter(Protocol):
    """One adapter per `CoordinationAgent` kind. A concrete implementation is NOT part of this
    foundation -- see module docstring."""

    async def health(self) -> AgentHealth: ...

    def capabilities(self) -> tuple[str, ...]: ...

    def control_capabilities(self) -> AdapterCapabilities: ...

    async def start_assignment(self, assignment_id: UUID, instruction: str | None = None) -> None: ...

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

    _DEFAULT_REASON = "no real Agent Runtime is wired up yet"

    def __init__(self, *, provider_key: str, reason: str = _DEFAULT_REASON):
        self._provider_key = provider_key
        self._reason = reason

    async def health(self) -> AgentHealth:
        if self._reason == self._DEFAULT_REASON:
            return AgentHealth(healthy=False, detail=f"REAL_PROVIDER_NOT_CONFIGURED: no Agent Runtime is wired up for '{self._provider_key}' yet")
        return AgentHealth(healthy=False, detail=f"REAL_PROVIDER_NOT_CONFIGURED: '{self._provider_key}': {self._reason}")

    def capabilities(self) -> tuple[str, ...]:
        return ()

    def control_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities()  # every flag False -- there is nothing here capable of any of them

    async def start_assignment(self, assignment_id: UUID, instruction: str | None = None) -> None:
        raise ProviderNotConfiguredError(
            f"REAL_PROVIDER_NOT_CONFIGURED: cannot start_assignment for '{self._provider_key}' -- {self._reason}"
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




class AdapterInstructionError(ValueError):
    """An instruction, session reference or invocation setting this adapter refuses to use --
    raised BEFORE any subprocess is started, never after. A refusal here is a structured
    configuration/input failure, distinct from `AdapterProcessLostError` (a process that could
    not run) and from an agent-reported failure (`AgentResult.succeeded=False`)."""


class AdapterSessionUnknownError(RuntimeError):
    """`send_instruction()` was asked to continue a session for which this adapter holds no
    MainAI-created session id. Continuation only ever targets a session this adapter itself
    started (or one bound from this assignment's own recorded result) -- never a guessed,
    caller-invented or Founder-owned session."""


# ============================================================================ conversational CLI
# dialects -- how ONE bounded subprocess mechanism (LocalCLIAdapter below) delivers an
# instruction to, and reads the answer from, each supported agent CLI. Every safety flag is
# fixed here in code; the founder-supplied args template may only add a model selection.

MAX_INSTRUCTION_CHARS = 32_000
MAX_FINAL_MESSAGE_CHARS = 16_000
MAX_STDERR_TAIL_CHARS = 2_000
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
_MODEL_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]=,-]{0,120}$")
_EXTRA_ARG_FLAGS = frozenset({"--model", "-m"})


def build_prompt(assignment_id: UUID, instruction: str | None) -> str:
    """The exact text an agent receives. The fixed header guarantees the argv element can
    never be parsed as a command-line option (it can never start with `-`), and ties the
    session to the assignment it serves."""

    if not isinstance(instruction, str) or not instruction.strip():
        raise AdapterInstructionError("instruction must be a non-empty string")
    if "\x00" in instruction:
        raise AdapterInstructionError("instruction must not contain NUL bytes")
    if len(instruction) > MAX_INSTRUCTION_CHARS:
        raise AdapterInstructionError(f"instruction exceeds {MAX_INSTRUCTION_CHARS} characters")
    return f"MainAI assignment {assignment_id}. Founder-authorized instruction follows.\n\n{instruction}"


def validate_session_id(session_id: object) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID.match(session_id):
        raise AdapterInstructionError(f"refusing a malformed session id: {session_id!r}")
    return session_id


def validate_extra_args(args: tuple[str, ...]) -> tuple[str, ...]:
    """The founder-supplied args template for a conversational dialect may ONLY select a
    model (`--model X` / `-m X`). Everything that affects authority -- permission mode,
    sandbox, budget, workspace, output format, resume -- is fixed by the dialect itself and
    cannot be overridden or weakened from configuration."""

    if len(args) % 2 != 0:
        raise AdapterInstructionError(f"args template must be '--model <name>' pairs only, got {list(args)}")
    for flag, value in zip(args[::2], args[1::2]):
        if flag not in _EXTRA_ARG_FLAGS or not _MODEL_VALUE.match(value):
            raise AdapterInstructionError(f"args template may only select a model; refusing {flag!r} {value!r}")
    return tuple(args)


@dataclass(frozen=True)
class ParsedAgentOutput:
    final_message: str | None
    session_id: str | None
    is_error: bool
    structured: dict
    parse_error: str | None = None


def _json_result_object(stdout: str) -> ParsedAgentOutput:
    """`--output-format json` result object (Claude Code; Cursor Agent assumed compatible --
    see `CursorAgentDialect`)."""

    try:
        data = json.loads(stdout.strip())
    except (json.JSONDecodeError, ValueError) as exc:
        return ParsedAgentOutput(None, None, True, {}, parse_error=f"output is not a JSON object: {exc}")
    if not isinstance(data, dict):
        return ParsedAgentOutput(None, None, True, {}, parse_error="output JSON is not an object")
    message = data.get("result")
    session = data.get("session_id") or data.get("chat_id") or data.get("chatId")
    structured = {
        key: data[key]
        for key in ("type", "subtype", "is_error", "num_turns", "duration_ms", "total_cost_usd", "request_id")
        if key in data
    }
    return ParsedAgentOutput(
        final_message=message if isinstance(message, str) else None,
        session_id=session if isinstance(session, str) else None,
        is_error=bool(data.get("is_error")) or data.get("subtype") not in (None, "success"),
        structured=structured,
        parse_error=None if isinstance(message, str) else "result field missing",
    )


def mainai_session_id(*, owner_id: UUID, assignment_id: UUID, provider_key: str) -> str:
    """The ONLY session id MainAI will ever start or resume for an assignment: derived from the
    server secret with HMAC over (owner, assignment, provider), formatted as a UUID. It is never
    read from the database, from `output_refs`, from `AgentDispatchExecution.process_ref` or
    from the agent's own output -- so a forged or poisoned id has no effect, a worker restart
    re-derives the same id, and no Founder-owned session id can ever be produced (finding a
    preimage would require the secret)."""

    import hmac
    import uuid as uuid_module
    from hashlib import sha256

    from app.config import get_settings

    secret = (get_settings().secret_key or "").encode()
    if not secret or secret == b"change-me-in-production":
        raise AdapterInstructionError("a real SECRET_KEY is required to derive MainAI agent session ids")
    digest = hmac.new(secret, f"mainai-agent-session:v1:{owner_id}:{assignment_id}:{provider_key}".encode(), sha256).digest()
    return str(uuid_module.UUID(bytes=digest[:16], version=4))


class CLIDialect:
    provider_key: str = ""
    supports_budget: bool = False
    # True only where the CLI accepts a caller-chosen session id at start AND resume by that id
    # has been verified. Everything else is single-shot: no continuation at all.
    supports_bound_session: bool = False

    def start_argv(self, executable: str, prompt: str, *, cwd: str, read_only: bool, max_budget_usd: float | None,
                   extra_args: tuple[str, ...], session_id: str | None) -> list[str]:
        raise NotImplementedError

    def resume_argv(self, executable: str, session_id: str, prompt: str, *, cwd: str, read_only: bool, max_budget_usd: float | None,
                    extra_args: tuple[str, ...]) -> list[str]:
        raise AdapterInstructionError(f"'{self.provider_key}' session resume is disabled until its CLI session semantics are verified")

    def parse(self, stdout: str) -> ParsedAgentOutput:
        raise NotImplementedError


class ClaudeCodeDialect(CLIDialect):
    """`claude -p --output-format json` (headless). MainAI chooses the session id itself
    (`--session-id <uuid>`) and continues only that id (`--resume <uuid>`). Read-only work runs
    in `plan` permission mode; write work in `acceptEdits` (never `bypassPermissions`).
    `--max-budget-usd` is always set."""

    provider_key = "claude-code"
    supports_budget = True
    supports_bound_session = True

    def _base(self, read_only: bool, max_budget_usd: float | None) -> list[str]:
        if max_budget_usd is None:
            raise AdapterInstructionError("claude-code requires a max budget")
        return ["-p", "--output-format", "json", "--permission-mode", "plan" if read_only else "acceptEdits", "--max-budget-usd", f"{max_budget_usd:.2f}"]

    def start_argv(self, executable, prompt, *, cwd, read_only, max_budget_usd, extra_args, session_id):
        if session_id is None:
            raise AdapterInstructionError("claude-code sessions must start with a MainAI-chosen session id")
        return [executable, *self._base(read_only, max_budget_usd), "--session-id", validate_session_id(session_id), *extra_args, prompt]

    def resume_argv(self, executable, session_id, prompt, *, cwd, read_only, max_budget_usd, extra_args):
        return [executable, *self._base(read_only, max_budget_usd), "--resume", validate_session_id(session_id), *extra_args, prompt]

    def parse(self, stdout):
        return _json_result_object(stdout)


class CursorAgentDialect(CLIDialect):
    """`cursor-agent -p --output-format json`, sandbox explicitly enabled, never `--force`.
    Read-only work runs in `--mode plan`. SINGLE-SHOT ONLY: resume is disabled until Cursor's
    session id semantics and JSON shape are verified live. No budget flag exists -- the adapter
    timeout is the only cost bound, so the founder must explicitly accept that."""

    provider_key = "cursor-agent"

    def _base(self, cwd: str, read_only: bool) -> list[str]:
        args = ["-p", "--output-format", "json", "--sandbox", "enabled", "--trust", "--workspace", cwd]
        return [*args, "--mode", "plan"] if read_only else args

    def start_argv(self, executable, prompt, *, cwd, read_only, max_budget_usd, extra_args, session_id):
        return [executable, *self._base(cwd, read_only), *extra_args, prompt]

    def parse(self, stdout):
        return _json_result_object(stdout)


class CodexDialect(CLIDialect):
    """`codex exec --json` with the sandbox fixed through `-c sandbox_mode=...` (`read-only` or
    `workspace-write`; never `danger-full-access`, never the bypass flag). SINGLE-SHOT ONLY:
    resume is disabled until Codex's session semantics and JSONL events are verified live. No
    budget flag exists -- the adapter timeout is the only cost bound."""

    provider_key = "codex"

    @staticmethod
    def _sandbox(read_only: bool) -> list[str]:
        return ["-c", f'sandbox_mode="{"read-only" if read_only else "workspace-write"}"']

    def start_argv(self, executable, prompt, *, cwd, read_only, max_budget_usd, extra_args, session_id):
        return [executable, "exec", "--json", "-C", cwd, *self._sandbox(read_only), *extra_args, prompt]

    def parse(self, stdout):
        session = None
        message = None
        is_error = False
        structured: dict = {}
        events = 0
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            events += 1
            kind = str(event.get("type", ""))
            for key in ("thread_id", "session_id", "conversation_id"):
                if isinstance(event.get(key), str):
                    session = session or event[key]
            item = event.get("item") if isinstance(event.get("item"), dict) else None
            if item is not None and item.get("type") in ("agent_message", "assistant_message") and isinstance(item.get("text"), str):
                message = item["text"]
            if kind in ("error", "turn.failed"):
                is_error = True
            if kind == "turn.completed" and isinstance(event.get("usage"), dict):
                structured["usage"] = event["usage"]
        structured["events"] = events
        if events == 0:
            return ParsedAgentOutput(None, None, True, structured, parse_error="no JSONL events in output")
        return ParsedAgentOutput(message, session, is_error or message is None, structured, None if message is not None else "no agent message event")


DIALECTS: dict[str, CLIDialect] = {d.provider_key: d for d in (ClaudeCodeDialect(), CursorAgentDialect(), CodexDialect())}


def _redact(text: str) -> str:
    from app.development_operator.service import redact

    return redact(text)


def _require_isolated_home(env: dict[str, str]) -> None:
    """Defense in depth for any directly-constructed conversational adapter: its environment
    must carry an isolated HOME, never the Founder's own (see adapter_config.validate_isolated_home)."""

    from app.agent_coordination.adapter_config import AdapterConfigurationError, validate_isolated_home

    try:
        validate_isolated_home(env.get("HOME"))
    except AdapterConfigurationError as exc:
        raise AdapterInstructionError(str(exc)) from exc


class LocalCLIAdapter:
    """Provider-neutral real adapter over a LOCAL CLI executable -- the SAME bounded
    subprocess mechanism serves Claude Code, Cursor Agent, or Codex, parametrized by
    externally-supplied configuration and, for conversational use, a `CLIDialect`.

    Every invocation is bounded, by construction:
    - exact `cwd` -- the assignment's own `worktree_path`, never inferred;
    - a real, always-present `timeout_seconds` -- the process is killed on expiry;
    - list-form `argv` via `asyncio.create_subprocess_exec()` -- NEVER `shell=True`;
    - a minimized environment -- only what the caller explicitly supplies via `env`, which for
      a conversational dialect must carry an ISOLATED HOME (never the Founder's).

    Without a dialect this is the original single-shot invocation (static argv, no
    instruction, no content captured). With a dialect it is a conversation turn. Session
    authority: for a dialect that supports bound sessions, `bind_mainai_session()` derives the
    session id from the server secret (`mainai_session_id`) BEFORE the session starts; the
    session is started with that id and only that id is ever resumed. Ids reported by the agent
    are compared against it, never adopted; nothing a caller or the database supplies can
    select a session."""

    def __init__(
        self,
        *,
        provider_key: str,
        executable: str,
        args_template: tuple[str, ...],
        cwd: str,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
        dialect: CLIDialect | None = None,
        read_only: bool = True,
        max_budget_usd: float | None = None,
    ):
        self._provider_key = provider_key
        self._executable = executable
        self._args_template = tuple(args_template)
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds
        self._env = dict(env or {})
        self._dialect = dialect
        self._read_only = read_only
        self._max_budget_usd = max_budget_usd
        self._processes: dict[UUID, asyncio.subprocess.Process] = {}
        self._sessions: dict[UUID, str] = {}
        if dialect is not None:
            validate_extra_args(self._args_template)
            _require_isolated_home(self._env)
            if dialect.supports_budget and (max_budget_usd is None or max_budget_usd <= 0):
                raise AdapterInstructionError(f"'{provider_key}' requires a positive max budget")

    async def health(self) -> AgentHealth:
        found = shutil.which(self._executable) is not None
        return AgentHealth(healthy=found, detail=f"executable '{self._executable}' {'found' if found else 'NOT found'} on PATH")

    def capabilities(self) -> tuple[str, ...]:
        return ("repo_edit", "read_only_review", "run_tests")

    def control_capabilities(self) -> AdapterCapabilities:
        # Without a dialect: single-shot, only cancel(). With a dialect: structured results; a
        # follow-up instruction to the same session only where MainAI owns the session id.
        if self._dialect is None:
            return AdapterCapabilities(supports_cancel=True)
        return AdapterCapabilities(
            supports_cancel=True, supports_instruction=self._dialect.supports_bound_session, supports_structured_events=True
        )

    def bind_mainai_session(self, assignment_id: UUID, owner_id: UUID) -> str | None:
        """Derives and binds MainAI's own session id for this assignment (start or, after a
        worker restart, resume). Returns `None` for a dialect without bound sessions."""

        if self._dialect is None or not self._dialect.supports_bound_session:
            return None
        session_id = mainai_session_id(owner_id=owner_id, assignment_id=assignment_id, provider_key=self._provider_key)
        self._sessions[assignment_id] = session_id
        return session_id

    def session_id(self, assignment_id: UUID) -> str | None:
        return self._sessions.get(assignment_id)

    async def _spawn(self, assignment_id: UUID, argv: list[str]) -> None:
        running = self._processes.get(assignment_id)
        if running is not None and running.returncode is None:
            raise AdapterInstructionError("the previous turn for this assignment is still running")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=self._cwd, env=self._env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, FileNotFoundError) as exc:
            raise AdapterProcessLostError(f"failed to start '{self._executable}' for provider '{self._provider_key}': {exc}") from exc
        self._processes[assignment_id] = proc

    async def start_assignment(self, assignment_id: UUID, instruction: str | None = None) -> None:
        if self._dialect is None:
            if instruction is not None:
                raise AdapterInstructionError(f"'{self._provider_key}' has no conversational dialect -- it cannot deliver an instruction")
            await self._spawn(assignment_id, [self._executable, *self._args_template])
            return
        prompt = build_prompt(assignment_id, instruction)
        session_id = self._sessions.get(assignment_id)
        if self._dialect.supports_bound_session and session_id is None:
            raise AdapterInstructionError("bind a MainAI session (bind_mainai_session) before starting a conversation")
        argv = self._dialect.start_argv(
            self._executable, prompt, cwd=self._cwd, read_only=self._read_only, max_budget_usd=self._max_budget_usd,
            extra_args=self._args_template, session_id=session_id,
        )
        await self._spawn(assignment_id, argv)

    async def send_instruction(self, assignment_id: UUID, instruction: str) -> None:
        if self._dialect is None:
            raise NotImplementedError("LocalCLIAdapter without a dialect is a bounded, single-shot invocation -- no mid-run instructions")
        if not self._dialect.supports_bound_session:
            raise AdapterInstructionError(f"'{self._provider_key}' session resume is disabled until its CLI session semantics are verified")
        session_id = self._sessions.get(assignment_id)
        if session_id is None:
            raise AdapterSessionUnknownError(f"no MainAI-bound session for assignment {assignment_id}")
        prompt = build_prompt(assignment_id, instruction)
        argv = self._dialect.resume_argv(
            self._executable, session_id, prompt, cwd=self._cwd, read_only=self._read_only, max_budget_usd=self._max_budget_usd,
            extra_args=self._args_template,
        )
        await self._spawn(assignment_id, argv)

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
        raise NotImplementedError("LocalCLIAdapter does not resume a paused process -- continue a session with send_instruction()")

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
        if self._dialect is None:
            return AgentResult(
                succeeded=proc.returncode == 0,
                output_refs=({"kind": "stdout_bytes", "length": len(stdout)}, {"kind": "stderr_bytes", "length": len(stderr)}),
                summary=f"'{self._provider_key}' exited with code {proc.returncode}",
            )
        return self._conversation_result(assignment_id, proc.returncode, stdout, stderr)

    def _conversation_result(self, assignment_id: UUID, returncode: int | None, stdout: bytes, stderr: bytes) -> AgentResult:
        parsed = self._dialect.parse(stdout.decode("utf-8", errors="replace"))
        bound = self._sessions.get(assignment_id)
        reported = parsed.session_id
        parse_error = parsed.parse_error
        # The agent's reported id is evidence, never authority: for a MainAI-bound session it must
        # equal the bound id, otherwise the turn fails (the agent did not run in our session).
        if bound is not None and reported is not None and reported != bound:
            parse_error = "session_mismatch: agent reported a session MainAI did not bind"

        message = _redact(parsed.final_message) if parsed.final_message is not None else None
        truncated = message is not None and len(message) > MAX_FINAL_MESSAGE_CHARS
        if truncated:
            message = message[:MAX_FINAL_MESSAGE_CHARS]
        succeeded = returncode == 0 and parse_error is None and not parsed.is_error

        refs: list[dict] = []
        if message is not None:
            # The agent's own report -- partial and unverified by definition.
            refs.append({"kind": "agent_final_message", "provider": self._provider_key, "text": message, "truncated": truncated, "self_report": True})
        if bound is not None and succeeded:
            # Audit record of the MainAI-bound session -- never read back as authority.
            refs.append({"kind": "agent_session", "provider": self._provider_key, "session_id": bound, "mainai_bound": True})
        if not succeeded:
            tail = _redact(stderr.decode("utf-8", errors="replace"))[-MAX_STDERR_TAIL_CHARS:]
            refs.append({"kind": "agent_failure", "provider": self._provider_key, "returncode": returncode, "parse_error": parse_error,
                         "stderr_tail": tail, "reported_session_id": _redact(str(reported)) if reported is not None else None})

        if succeeded:
            summary = f"'{self._provider_key}' answered ({len(message or '')} chars)"
        else:
            summary = f"'{self._provider_key}' turn failed: exit={returncode} parse_error={parse_error} is_error={parsed.is_error}"
        return AgentResult(
            succeeded=succeeded, output_refs=tuple(refs), summary=summary, final_message=message,
            session_id=bound if succeeded else None, structured={**parsed.structured, "returncode": returncode},
        )


def get_real_adapter(provider_key: str, *, cwd: str, env: dict[str, str] | None = None, read_only: bool = True) -> "AgentAdapter":
    """The founder-controlled factory: returns a real `LocalCLIAdapter` ONLY when
    `app.agent_coordination.adapter_config.real_adapter_config()` confirms every precondition
    (enabled, executable found, an explicit args_template configured) AND, for a
    conversational provider: a valid model-only args template, a valid budget (or, for a CLI
    without a budget flag, the founder's explicit unbudgeted acknowledgement) and a dedicated
    isolated HOME. Returns `NotConfiguredAdapter` -- the honest, fail-closed default -- in
    every other case. NEVER silently substitutes a fake/mock adapter.

    For a conversational provider the environment is ALWAYS
    `adapter_config.isolated_agent_env(provider_key)`; a caller-supplied `env` is only honoured
    for a provider without a dialect (unchanged legacy behaviour). `read_only` selects the
    dialect's read-only permission/sandbox mode; it defaults to True."""

    from app.agent_coordination.adapter_config import (
        AdapterConfigurationError,
        adapter_max_budget_usd,
        adapter_unbudgeted_allowed,
        isolated_agent_env,
        real_adapter_config,
        resolve_adapter_env,
    )

    config = real_adapter_config(provider_key)
    if config is None:
        return NotConfiguredAdapter(provider_key=provider_key)
    executable, args_template, timeout_seconds = config
    dialect = DIALECTS.get(provider_key)
    if dialect is None:
        resolved_env = resolve_adapter_env(provider_key) if env is None else env
        return LocalCLIAdapter(provider_key=provider_key, executable=executable, args_template=args_template, cwd=cwd,
                               timeout_seconds=timeout_seconds, env=resolved_env)
    if not dialect.supports_budget and not adapter_unbudgeted_allowed(provider_key):
        return NotConfiguredAdapter(provider_key=provider_key, reason=(
            f"'{provider_key}' has no budget flag; set LIFE_AGENT_ADAPTER_ALLOW_UNBUDGETED__<KEY>=true to accept the timeout as its only cost bound"))
    try:
        return LocalCLIAdapter(
            provider_key=provider_key, executable=executable, args_template=args_template, cwd=cwd, timeout_seconds=timeout_seconds,
            env=isolated_agent_env(provider_key), dialect=dialect, read_only=read_only, max_budget_usd=adapter_max_budget_usd(provider_key),
        )
    except (AdapterInstructionError, AdapterConfigurationError) as exc:
        return NotConfiguredAdapter(provider_key=provider_key, reason=str(exc))
