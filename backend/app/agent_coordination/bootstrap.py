"""Canonical Real Agent Bootstrap -- registers Life's actual, currently-used worker identities
(Claude Code, Cursor Agent, Codex) into the founder-wide `coordination_agents` registry
migration 0046 already created. Never a new registry, never a new table -- a thin, idempotent
wrapper over `register_agent()`.

Represents identity/capability/config ONLY -- never a credential, a secret, or a
machine-specific token (see `CoordinationAgent`'s own model docstring: `model_hint` is an
informational label, never a key; nothing here reads an environment variable or a secrets
store). Capabilities are the CONSERVATIVE, currently-known-true shape of each agent's role in
this project as of this bootstrap -- interactive CLI-driven repo editing, read-only review,
running tests -- never an invented performance ranking or a capability this foundation has no
actual evidence for.

Deliberately NOT wired into automatic app boot (unlike `apply_mainai_execution_privileges()`,
which every layer's own privilege grants ARE wired into, see `app/main.py`) -- granting a
privilege an already-shipped migration requires is mechanical infrastructure; seeding actual
founder-facing DATA rows about which real agents exist is a decision, not a mechanical
necessity, and stays an explicit, callable action a founder (or a deliberate ops script)
chooses to run, never a side effect of every process restart."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.agent_coordination.service import register_agent
from app.models.agent_coordination import CoordinationAgent

logger = logging.getLogger(__name__)

# Conservative, currently-known-true capability tags -- the SAME vocabulary this codebase's
# own docstrings already use as examples (see CoordinationAgent's own docstring and
# app.agent_coordination.routing's required_capabilities examples). Never a performance
# ranking, never an invented specialization this foundation has no real evidence for: every
# one of these three agents genuinely does interactive, CLI-driven repo work, read-only
# review, and can run tests -- that is the full, honest extent of what is known here.
# `concurrency_limit=1` reflects the real, current operating model this whole session has been
# managing manually (one active worker per identity at a time) -- not a measured ceiling, a
# conservative starting point a founder can raise once real evidence justifies it.
KNOWN_AGENT_DEFAULTS: dict[str, dict] = {
    "claude-code": dict(
        display_name="Claude Code",
        adapter_kind="cli",
        capabilities=["repo_edit", "read_only_review", "run_tests"],
        execution_mode="cli_interactive",
        supports_read=True,
        supports_write=True,
        concurrency_limit=1,
        cost_class="unknown",
    ),
    "cursor-agent": dict(
        display_name="Cursor Agent",
        adapter_kind="cli",
        capabilities=["repo_edit", "read_only_review", "run_tests"],
        execution_mode="cli_interactive",
        supports_read=True,
        supports_write=True,
        concurrency_limit=1,
        cost_class="unknown",
    ),
    "codex": dict(
        display_name="Codex",
        adapter_kind="cli",
        capabilities=["repo_edit", "read_only_review", "run_tests"],
        execution_mode="cli_interactive",
        supports_read=True,
        supports_write=True,
        concurrency_limit=1,
        cost_class="unknown",
    ),
}


def bootstrap_known_agents(db: Session, *, agent_keys: tuple[str, ...] | None = None) -> list[CoordinationAgent]:
    """Idempotently registers (or updates) Life's currently-known real worker identities.
    Calling this twice, or calling it again after a founder edits `KNOWN_AGENT_DEFAULTS` to
    reflect a genuine change (e.g. a real observed concurrency limit), converges the SAME rows
    to the new values via `register_agent()`'s own upsert-by-`agent_key` -- never creates a
    duplicate row, never changes an existing agent's `id` (so no already-created
    `AgentWorkAssignment`, which references an agent by id, is ever orphaned by re-running
    this).

    `agent_keys` restricts which of the known identities to (re)register -- defaults to all
    three. Passing an unknown key raises `KeyError` immediately -- fail closed on a typo,
    never silently register nothing.

    Returns the registered/updated `CoordinationAgent` rows, in the same order as `agent_keys`
    (or the default three) -- the caller's own audit trail; this function keeps no separate
    log/event table of its own, matching this module's "reuse, never a new registry" doctrine
    (each call is also logged via the standard `logging` module for operational visibility)."""

    keys = agent_keys if agent_keys is not None else tuple(KNOWN_AGENT_DEFAULTS.keys())
    registered = []
    for key in keys:
        defaults = KNOWN_AGENT_DEFAULTS[key]  # KeyError on an unknown key -- fail closed, never silently skip
        agent = register_agent(db, agent_key=key, **defaults)
        logger.info("agent_coordination.bootstrap: registered/updated agent '%s' (id=%s)", key, agent.id)
        registered.append(agent)
    return registered
