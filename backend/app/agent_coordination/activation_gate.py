"""`first_cycle_go` -- Founder directive's own "Hard limits" section says: "Do not deploy,
merge protected changes, grant yourself new privileges or override first_cycle_go=false." A
repo-wide grep (2026-11-14, confirmed independently twice this session) found ZERO existing
matches for this term anywhere in the codebase -- it was a decision this codebase was ASSUMED
to already enforce, but never actually did.

This module is the smallest possible fail-closed implementation, not a new security system:
ONE environment flag, ONE check function, wired into the ONE place in this codebase that would
ever invoke a REAL external AI provider adapter --
`app.agent_coordination.adapters.get_real_adapter()`. That path has ZERO live callers anywhere
in `app/routers/` or `app/worker.py` today (confirmed this session), so wiring this gate in
changes NOTHING about current live behavior -- there is nothing live to block yet. It becomes
load-bearing only once (if ever) `app.agent_coordination.dispatch.dispatch_assignment()` is
wired to a real request/worker path in a future increment, per the migration boundary recorded
in `docs/LIFE_EXECUTION_AUTHORITY_CANONICAL_MAPPING.md`.

Deliberately NOT a database flag, NOT a founder-facing API, NOT something any in-process code
(including MainAI itself) can set: the directive frames `first_cycle_go` as the founder's OWN
external release decision (the same kind of thing a human flips when they decide Founder Alpha
has survived its first real activation cycle), so this module only ever READS a single
environment variable the founder sets outside application code -- there is no `set_*` function
here, on purpose, and never should be. Fail-closed by construction: unset, empty, or any value
other than exactly `"true"` means NOT gone -- the same discipline
`app.mainai_execution.approval.requires_approval()`'s unknown-policy fallback and
`app.mainai_execution.recovery_approval.recovery_requires_approval()`'s unknown-classification
fallback already apply to their own gates."""

import os

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
