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

_DEFAULT_TIMEOUT_SECONDS = 900  # a bound always applies; 15 minutes if the founder sets none


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
