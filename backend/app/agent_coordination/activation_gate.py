"""`first_cycle_go` -- Founder directive's own "Hard limits" section says: "Do not deploy,
merge protected changes, grant yourself new privileges or override first_cycle_go=false." A
repo-wide grep (2026-11-14, confirmed independently twice this session) found ZERO existing
matches for this term anywhere in the codebase -- it was a decision this codebase was ASSUMED
to already enforce, but never actually did.

Two layers live in this module, added in two passes of the same Founder directive:

1. `first_cycle_go()`/`require_first_cycle_go()` -- the original, cheapest possible check: one
   environment flag. Deliberately NOT a database flag, NOT a founder-facing API, NOT something
   any in-process code (including MainAI itself) can set -- the directive frames
   `first_cycle_go` as the founder's OWN external release decision, so this reads a single env
   var the founder sets outside application code; there is no `set_*` function here, on
   purpose, and never should be.

2. `require_system_activation_authority()` -- a SECOND, later-directive follow-up ("An
   environment variable alone must not constitute production activation authority"). Requires
   BOTH the env var above AND a current row in `system_activation_authorizations`
   (`app.agent_coordination.system_activation.current_system_activation()`) -- a table no
   application code path can ever write to (migration 0069 revokes INSERT/UPDATE/DELETE from
   `mainai_app`; see that migration and `SystemActivationAuthorization`'s own docstring).
   Neither the env var nor the DB row is sufficient alone; both fail closed independently.

Wiring: `app.agent_coordination.adapters.LocalCLIAdapter.start_assignment()` -- the actual,
literal point a real subprocess gets spawned, regardless of how the adapter instance was
constructed or which caller reached it. An earlier pass of this directive wired the check only
into `get_real_adapter()` (the factory), which a security review correctly found insufficient:
`dispatch.dispatch_assignment()` takes an `AgentAdapter` as a caller-supplied parameter and
never calls `get_real_adapter()` itself, so a directly-constructed `LocalCLIAdapter` passed
into it would have bypassed that earlier, shallower check entirely. Moving the check to the
adapter's own real-effect method closes that: there is no lower level left to invoke around.
`get_real_adapter()` still checks `first_cycle_go()` too, as a cheap early-exit that avoids
even constructing a `LocalCLIAdapter` when the simplest precondition already fails -- but it is
no longer the ONLY enforcement point, and was never meant to be.

Fail-closed by construction throughout: unset, empty, or any value other than exactly `"true"`
means NOT gone for the env var; missing, revoked, or expired means NOT authorized for the DB
evidence -- the same discipline `app.mainai_execution.approval.requires_approval()`'s
unknown-policy fallback and `app.mainai_execution.recovery_approval.
recovery_requires_approval()`'s unknown-classification fallback already apply to their own
gates."""

import os

from sqlalchemy.orm import Session

_ENV_FIRST_CYCLE_GO = "FIRST_CYCLE_GO"


def first_cycle_go() -> bool:
    """The founder's own external release decision. Fail-closed: anything other than the exact
    string `"true"` (case-sensitive, no alternate spellings like "1"/"yes"/"on" -- unlike the
    looser adapter-enablement flags in `adapter_config.py`, this one flag is deliberately strict
    so a stray env var meant for something else can never accidentally satisfy it) means NOT
    gone."""
    return os.environ.get(_ENV_FIRST_CYCLE_GO) == "true"


class FirstCycleNotGoError(Exception):
    """Raised by `require_first_cycle_go()` -- for a caller that wants a hard stop rather than a
    silent fallback. `get_real_adapter()` itself does NOT raise this (see that function's own
    "never raises, always returns an adapter" contract); it uses `first_cycle_go()`'s plain
    boolean instead and falls back to the existing `NotConfiguredAdapter` default. This
    exception exists for any FUTURE caller (e.g. a router or CLI entrypoint) that wants an
    explicit, unroutable-around stop instead of a quiet no-op."""

    def __init__(self, action: str):
        super().__init__(
            f"first_cycle_go is not set (action: {action!r}) -- set {_ENV_FIRST_CYCLE_GO}=true "
            "to authorize this action. This is the founder's own external release decision; no "
            "code path in this repository may set it on its own behalf."
        )
        self.action = action


def require_first_cycle_go(*, action: str) -> None:
    """THE enforcement point for a caller that wants a real, unroutable-around stop. Raises
    `FirstCycleNotGoError` unless the founder has explicitly set `FIRST_CYCLE_GO=true`."""
    if not first_cycle_go():
        raise FirstCycleNotGoError(action)


class SystemActivationNotAuthorizedError(Exception):
    """Raised by `require_system_activation_authority()` when EITHER the env var or the durable
    DB evidence is missing -- the caller must treat this as a hard stop. Distinct from
    `FirstCycleNotGoError` (env var alone) because this is the one callers doing anything with
    real external effect must use; `first_cycle_go()`'s bare env var is no longer, by itself,
    sufficient authorization for that."""

    def __init__(self, action: str, *, reason: str):
        super().__init__(
            f"system activation is not authorized (action: {action!r}): {reason}. Both "
            f"{_ENV_FIRST_CYCLE_GO}=true AND a current row in system_activation_authorizations "
            "are required -- no code path in this repository may create that row on its own "
            "behalf (see migration 0069)."
        )
        self.action = action
        self.reason = reason


def require_system_activation_authority(db: Session, *, action: str) -> None:
    """THE authoritative enforcement point: requires BOTH the env var AND current, valid,
    non-expired, non-revoked DB evidence. Re-queries fresh on every call -- never cached --
    so revoking authorization or letting it expire takes effect on the very next check, closing
    the "concurrent changes and stale authorization" scenario a security review flagged.
    Raises `SystemActivationNotAuthorizedError`, naming exactly which half is missing, rather
    than a single undifferentiated failure."""
    from app.agent_coordination.system_activation import current_system_activation

    if not first_cycle_go():
        raise SystemActivationNotAuthorizedError(action, reason=f"{_ENV_FIRST_CYCLE_GO} is not set to \"true\"")
    if current_system_activation(db) is None:
        raise SystemActivationNotAuthorizedError(
            action, reason="no current (active, non-expired) row exists in system_activation_authorizations"
        )
