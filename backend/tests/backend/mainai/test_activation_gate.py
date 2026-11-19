"""app/agent_coordination/activation_gate.py -- Founder directive 2026-11-14 ("Resolve
execution authority and security gaps"): `first_cycle_go` was named in an earlier directive's
own "Hard limits" section ("Do not ... override first_cycle_go=false") but had ZERO matches
anywhere in this codebase before this module -- it was assumed, never enforced. These are pure
unit tests (no DB, no fixtures) proving the fail-closed contract directly."""

import pytest

from app.agent_coordination.activation_gate import (
    FirstCycleNotGoError,
    first_cycle_go,
    require_first_cycle_go,
)


def test_first_cycle_go_is_false_when_unset(monkeypatch):
    monkeypatch.delenv("FIRST_CYCLE_GO", raising=False)
    assert first_cycle_go() is False


@pytest.mark.parametrize("value", ["1", "yes", "YES", "True", "TRUE", "on", "", "false", " true", "true "])
def test_first_cycle_go_rejects_every_spelling_except_the_exact_lowercase_string(monkeypatch, value):
    """Deliberately stricter than adapter_config.py's own `_env_flag()` -- a stray "1"/"yes"
    meant to enable an unrelated adapter flag must never accidentally satisfy this one."""
    monkeypatch.setenv("FIRST_CYCLE_GO", value)
    assert first_cycle_go() is False


def test_first_cycle_go_is_true_only_for_the_exact_string(monkeypatch):
    monkeypatch.setenv("FIRST_CYCLE_GO", "true")
    assert first_cycle_go() is True


def test_require_first_cycle_go_raises_with_the_action_name_when_not_set(monkeypatch):
    monkeypatch.delenv("FIRST_CYCLE_GO", raising=False)
    with pytest.raises(FirstCycleNotGoError) as exc_info:
        require_first_cycle_go(action="get_real_adapter:codex")
    assert exc_info.value.action == "get_real_adapter:codex"
    assert "get_real_adapter:codex" in str(exc_info.value)


def test_require_first_cycle_go_does_not_raise_when_set(monkeypatch):
    monkeypatch.setenv("FIRST_CYCLE_GO", "true")
    require_first_cycle_go(action="anything")  # must not raise
