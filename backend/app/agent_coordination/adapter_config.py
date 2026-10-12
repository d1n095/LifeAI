"""Founder-Controlled Real-Agent Enablement -- the deterministic boundary between "this
codebase knows how to drive an agent CLI" and "this agent is actually allowed to be invoked."

Five DISTINCT facts, each computed independently, NEVER conflated into one another:

1. `supported`     -- this codebase has a real adapter implementation registered for this
                       provider key (a code-level fact, true for every entry in
                       `SUPPORTED_LOCAL_CLI_PROVIDERS` below, regardless of the local machine).
2. `executable_found` -- `shutil.which()` found a binary on THIS machine's PATH. Detection
                       only -- finding the binary NEVER, by itself, authorizes invoking it.
3. `credentials_state` -- always `"unknown"` unless the founder EXPLICITLY asserts
                       `"configured"` via `LIFE_AGENT_ADAPTER_CREDENTIALS_CONFIRMED__<KEY>`.
                       This module never inspects auth files, never runs a status subcommand,
                       never guesses -- "no automatic credential discovery that bypasses
                       founder intent" is not a suggestion, it is what `credentials_state`
                       being an honest, mostly-`"unknown"` field means in practice.
4. `enabled`       -- the founder's own explicit opt-in,
                       `LIFE_AGENT_ADAPTER_ENABLED__<KEY>=true`. Defaults to `False`. This is
                       the ONLY thing that turns "code exists" into "code may run."
5. `dispatch_authorized` -- computed separately, per assignment, by
                       `app.agent_coordination.dispatch.evaluate_dispatch_readiness()` --
                       out of scope for this module entirely; NOT part of `AdapterAvailability`
                       below, listed here only so the five-way distinction stays complete in
                       one place.

No credential, secret, session token, or API key is ever read, stored, or referenced by this
module. Every environment variable this module reads is a plain boolean/string configuration
flag, never a secret value itself -- `LIFE_AGENT_ADAPTER_COMMAND__<KEY>` overrides which
executable NAME to look for, it is never a path to a credential file and is never logged
alongside anything sensitive."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

# The provider-neutral real-adapter boundary this module governs. Adding a new provider means
# adding one entry here -- never a new adapter class per provider (see
# app.agent_coordination.adapters.LocalCLIAdapter's own docstring for why one bounded
# subprocess mechanism serves all three). `default_executable` is only ever used to constitute
# argv -- never invoked without the caller ALSO supplying a real, founder-configured
# `args_template` (see `get_real_adapter()` below); an executable existing on PATH with no
# configured invocation shape is still fully disabled.
SUPPORTED_LOCAL_CLI_PROVIDERS: dict[str, dict] = {
    "claude-code": {"default_executable": "claude"},
    "cursor-agent": {"default_executable": "cursor-agent"},
    "codex": {"default_executable": "codex"},
}

_ENV_ENABLED = "LIFE_AGENT_ADAPTER_ENABLED__{key}"
_ENV_COMMAND = "LIFE_AGENT_ADAPTER_COMMAND__{key}"
_ENV_ARGS_TEMPLATE = "LIFE_AGENT_ADAPTER_ARGS__{key}"  # space-separated argv template, founder-supplied
_ENV_TIMEOUT_SECONDS = "LIFE_AGENT_ADAPTER_TIMEOUT_SECONDS__{key}"
_ENV_CREDENTIALS_CONFIRMED = "LIFE_AGENT_ADAPTER_CREDENTIALS_CONFIRMED__{key}"
_ENV_CREDENTIAL_REF = "LIFE_AGENT_ADAPTER_CREDENTIAL_REF__{key}"  # an opaque LABEL, never a secret
_ENV_ENV_ALLOWLIST = "LIFE_AGENT_ADAPTER_ENV_ALLOWLIST__{key}"  # comma-separated ambient VAR NAMES

_ENV_MAX_BUDGET_USD = "LIFE_AGENT_ADAPTER_MAX_BUDGET_USD__{key}"  # per-turn spend ceiling, where the CLI supports one
_ENV_HOME = "LIFE_AGENT_ADAPTER_HOME__{key}"  # dedicated HOME/session store for MainAI's own agent sessions
_ENV_MAX_TURNS = "LIFE_AGENT_ADAPTER_MAX_TURNS__{key}"  # instruction turns per assignment
_ENV_ALLOW_UNBUDGETED = "LIFE_AGENT_ADAPTER_ALLOW_UNBUDGETED__{key}"  # explicit ack for a CLI with no budget flag

_DEFAULT_TIMEOUT_SECONDS = 900  # a bound always applies; 15 minutes if the founder sets none
_DEFAULT_MAX_BUDGET_USD = 1.0  # per conversation turn, if the founder sets none
HARD_MAX_BUDGET_USD = 25.0  # a configured value above this ceiling is refused, never clamped silently
_DEFAULT_MAX_TURNS = 5
HARD_MAX_TURNS = 20

# Variables that would point an agent CLI back at a personal HOME/config/session store. They are
# never forwarded from this process's environment -- the isolated HOME below replaces them.
FORBIDDEN_FORWARDED_ENV = frozenset({
    "HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
    "CLAUDE_CONFIG_DIR", "CODEX_HOME", "CURSOR_CONFIG_DIR",
})
# Personal agent stores under the Founder's HOME that an isolated HOME may never be or sit inside.
_PERSONAL_AGENT_DIRS = (".claude", ".codex", ".cursor", ".config")


class AdapterConfigurationError(ValueError):
    """A founder-supplied adapter setting that would weaken isolation or bounds -- the adapter
    is then not constructed at all (fail closed), never constructed with a guessed fallback."""


def _env_key(agent_key: str) -> str:
    return agent_key.upper().replace("-", "_")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class AdapterAvailability:
    provider_key: str
    supported: bool
    executable_found: bool
    executable_path: str | None
    credentials_state: str  # "unknown" | "configured" -- see module docstring
    enabled: bool
    reason: str


def adapter_availability(provider_key: str) -> AdapterAvailability:
    """Computes all four locally-knowable facts (the fifth, `dispatch_authorized`, is
    per-assignment and lives entirely in `app.agent_coordination.dispatch`) -- never caches,
    never authorizes anything itself. Safe to call at any time; performs no I/O beyond a PATH
    lookup and reading a handful of plain environment variables."""

    provider = SUPPORTED_LOCAL_CLI_PROVIDERS.get(provider_key)
    if provider is None:
        return AdapterAvailability(
            provider_key=provider_key, supported=False, executable_found=False, executable_path=None,
            credentials_state="unknown", enabled=False, reason=f"'{provider_key}' has no registered real adapter implementation",
        )

    env_key = _env_key(provider_key)
    executable = os.environ.get(_ENV_COMMAND.format(key=env_key)) or provider["default_executable"]
    executable_path = shutil.which(executable)
    credentials_state = "configured" if _env_flag(_ENV_CREDENTIALS_CONFIRMED.format(key=env_key)) else "unknown"
    enabled = _env_flag(_ENV_ENABLED.format(key=env_key))

    if not enabled:
        reason = f"disabled -- set {_ENV_ENABLED.format(key=env_key)}=true to enable"
    elif executable_path is None:
        reason = f"enabled but executable '{executable}' was not found on PATH"
    else:
        reason = f"enabled, executable found at '{executable_path}'"

    return AdapterAvailability(
        provider_key=provider_key, supported=True, executable_found=executable_path is not None, executable_path=executable_path,
        credentials_state=credentials_state, enabled=enabled, reason=reason,
    )


def real_adapter_config(provider_key: str) -> tuple[str, tuple[str, ...], int] | None:
    """Returns `(executable, args_template, timeout_seconds)` -- but ONLY when the adapter is
    fully, explicitly ready to be constructed: `enabled=True`, the executable is genuinely
    found, AND the founder has supplied a real invocation `args_template` (never a hardcoded
    per-provider guess -- see module docstring on why this codebase does not invent one).
    Returns `None` in every other case, fail-closed -- the caller
    (`app.agent_coordination.adapters.get_real_adapter()`) falls back to `NotConfiguredAdapter`
    whenever this returns `None`."""

    availability = adapter_availability(provider_key)
    if not availability.enabled or availability.executable_path is None:
        return None
    env_key = _env_key(provider_key)
    raw_args = os.environ.get(_ENV_ARGS_TEMPLATE.format(key=env_key))
    if not raw_args:
        return None  # enabled + executable found is NOT enough on its own -- see docstring
    args_template = tuple(raw_args.split())
    timeout_raw = os.environ.get(_ENV_TIMEOUT_SECONDS.format(key=env_key))
    try:
        timeout_seconds = int(timeout_raw) if timeout_raw else _DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        timeout_seconds = _DEFAULT_TIMEOUT_SECONDS
    return availability.executable_path, args_template, timeout_seconds


def adapter_max_budget_usd(provider_key: str) -> float | None:
    """The per-turn spend ceiling handed to a CLI that supports one (Claude Code's
    `--max-budget-usd`). Defaults to `_DEFAULT_MAX_BUDGET_USD`; returns `None` -- which makes
    a budget-capable adapter refuse to construct -- for a non-numeric, non-positive or
    above-`HARD_MAX_BUDGET_USD` value. Fail closed, never clamped."""

    raw = os.environ.get(_ENV_MAX_BUDGET_USD.format(key=_env_key(provider_key)), "").strip()
    if not raw:
        return _DEFAULT_MAX_BUDGET_USD
    try:
        value = float(raw)
    except ValueError:
        return None
    if not 0 < value <= HARD_MAX_BUDGET_USD:
        return None
    return value


def adapter_max_turns(provider_key: str) -> int | None:
    """Instruction turns allowed per assignment (start + follow-ups). Default 5; `None` -- which
    makes every turn refuse -- for a non-integer, non-positive or above-`HARD_MAX_TURNS` value."""

    raw = os.environ.get(_ENV_MAX_TURNS.format(key=_env_key(provider_key)), "").strip()
    if not raw:
        return _DEFAULT_MAX_TURNS
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 0 < value <= HARD_MAX_TURNS else None


def adapter_unbudgeted_allowed(provider_key: str) -> bool:
    """A CLI with no per-turn budget flag (Cursor Agent, Codex) is only constructed after the
    founder explicitly accepts that its timeout is its only cost bound."""

    return _env_flag(_ENV_ALLOW_UNBUDGETED.format(key=_env_key(provider_key)))


def _founder_home() -> Path:
    return Path.home().resolve()


def validate_isolated_home(raw: str | None, *, founder_home: Path | None = None) -> Path:
    """The dedicated HOME a MainAI agent CLI runs with. It must be an existing absolute
    directory that is NOT the Founder's own HOME, not an ancestor of it, and not inside one of
    the Founder's personal agent stores (`~/.claude`, `~/.codex`, `~/.cursor`, `~/.config`) --
    so the CLI's session/transcript store is MainAI's alone and a Founder session can never be
    listed or resumed through it."""

    if not raw or not raw.strip():
        raise AdapterConfigurationError("an isolated agent HOME is required (LIFE_AGENT_ADAPTER_HOME__<KEY>)")
    path = Path(raw.strip())
    if not path.is_absolute():
        raise AdapterConfigurationError(f"isolated agent HOME must be an absolute path, got {raw!r}")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise AdapterConfigurationError(f"isolated agent HOME {resolved} does not exist")
    home = (founder_home or _founder_home()).resolve()
    if resolved == home or resolved in home.parents:
        raise AdapterConfigurationError(f"isolated agent HOME {resolved} is (or contains) the Founder's personal HOME")
    for personal in _PERSONAL_AGENT_DIRS:
        store = home / personal
        if resolved == store or store in resolved.parents:
            raise AdapterConfigurationError(f"isolated agent HOME {resolved} is inside the Founder's personal store {store}")
    return resolved


def isolated_agent_env(provider_key: str) -> dict[str, str]:
    """The full environment a MainAI agent CLI receives: the founder's allowlisted ambient
    names (never a HOME/config variable) plus HOME, XDG and CLI config directories all rooted in
    the dedicated isolated HOME. Raises `AdapterConfigurationError` when isolation is not
    configured or the allowlist names a forbidden variable."""

    env_key = _env_key(provider_key)
    allowlist_raw = os.environ.get(_ENV_ENV_ALLOWLIST.format(key=env_key), "")
    names = {name.strip() for name in allowlist_raw.split(",") if name.strip()}
    forbidden = sorted(names & FORBIDDEN_FORWARDED_ENV)
    if forbidden:
        raise AdapterConfigurationError(f"env allowlist may not forward {forbidden} -- they would expose a personal agent store")
    home = validate_isolated_home(os.environ.get(_ENV_HOME.format(key=env_key)))
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CODEX_HOME": str(home / ".codex"),
    })
    return env


def credential_reference(provider_key: str) -> str | None:
    """Returns an opaque, founder-supplied credential REFERENCE identifier -- e.g. a label
    like `"vault:codex-oauth"` naming which credential a future secret-storage integration
    would need to resolve -- NEVER the secret itself. This module has no secret-storage
    backend of any kind; a non-`None` return value means "unresolved / config-required," not
    "usable." Always `None` unless the founder explicitly sets
    `LIFE_AGENT_ADAPTER_CREDENTIAL_REF__<KEY>`; this function performs no lookup, no file
    read, no network call -- reading a single plain environment variable is its entire
    behavior."""

    env_key = _env_key(provider_key)
    ref = os.environ.get(_ENV_CREDENTIAL_REF.format(key=env_key), "").strip()
    return ref or None


def resolve_adapter_env(provider_key: str) -> dict[str, str]:
    """Selectively forwards ONLY the ambient environment variables the founder has explicitly
    allowlisted for this exact provider, via `LIFE_AGENT_ADAPTER_ENV_ALLOWLIST__<KEY>` -- a
    comma-separated list of VARIABLE NAMES, never values, never a path, never a secret in
    itself. Never a blind inheritance of this process's own full environment (see
    `app.agent_coordination.adapters.LocalCLIAdapter`'s own docstring on why that would risk
    leaking unrelated secrets this adapter has no business seeing). A name in the allowlist
    that happens not to be set in `os.environ` is simply absent from the result -- never an
    error, never a fabricated empty-string value. Performs no file read, no secret-store
    lookup, no scanning of any kind -- reads only names the founder explicitly listed, from
    the current process's own already-present environment."""

    env_key = _env_key(provider_key)
    allowlist_raw = os.environ.get(_ENV_ENV_ALLOWLIST.format(key=env_key), "")
    names = [name.strip() for name in allowlist_raw.split(",") if name.strip()]
    return {name: os.environ[name] for name in names if name in os.environ}
