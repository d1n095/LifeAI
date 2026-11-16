"""MainAI Resource Intelligence Round 2 -- `app.resource_intelligence.cost_projection` -- pure,
no-db unit tests: `estimated_cost_to_finish()`'s honest floor + PROVISIONAL refusal, and the
reset/handoff/compact token-reread cost model built on the real `app.providers.pricing` table."""

from __future__ import annotations

from datetime import datetime

from app.resource_intelligence.cost_projection import (
    compact_cost_estimate,
    estimated_cost_to_finish,
    handoff_cost_estimate,
    reset_session_cost_estimate,
)
from app.resource_intelligence.types import MetricEnvelope


def _metric(value, *, unit="usd", sample_size=10, uncertainty=None, population=None) -> MetricEnvelope:
    return MetricEnvelope(
        value=value, unit=unit, definition="test metric", denominator=None, time_window=None,
        population=population, sample_size=sample_size, source="test", method="test",
        missing_data=False, uncertainty=uncertainty, last_updated=datetime.utcnow(), trend=None,
    )


# ============================================================================ estimated_cost_to_finish


def test_missing_remaining_commits_is_missing_data():
    result = estimated_cost_to_finish(cost_per_accepted_commit=_metric(2.0), remaining_commits=None)
    assert result.missing_data is True


def test_known_cost_per_commit_and_remaining_commits_multiplies():
    result = estimated_cost_to_finish(cost_per_accepted_commit=_metric(2.5), remaining_commits=4)
    assert result.missing_data is False
    assert result.value == 10.0


def test_provisional_cost_per_commit_never_projected_forward():
    """ONE_RUN != LONG_TERM_PROFILE applies to forward cost projection too."""
    provisional = _metric(2.5, uncertainty="PROVISIONAL: this profile is derived from only 1 terminal assignment(s)")
    result = estimated_cost_to_finish(cost_per_accepted_commit=provisional, remaining_commits=4)
    assert result.missing_data is True
    assert "PROVISIONAL" in result.method
    assert "ONE_RUN" in result.uncertainty


def test_missing_cost_per_commit_is_missing_data():
    from app.resource_intelligence.types import unknown_metric

    result = estimated_cost_to_finish(cost_per_accepted_commit=unknown_metric(unit="usd", definition="d", source="s"), remaining_commits=4)
    assert result.missing_data is True


# ============================================================================ reset / handoff / compact cost estimates


def test_reset_cost_estimate_known_inputs():
    result = reset_session_cost_estimate(checkpoint_tokens=1000, provider="anthropic", model="claude-sonnet-5")
    assert result.missing_data is False
    assert result.value > 0


def test_reset_cost_missing_provider_is_missing_data():
    result = reset_session_cost_estimate(checkpoint_tokens=1000, provider=None, model=None)
    assert result.missing_data is True


def test_unpriced_provider_model_is_missing_data_not_fabricated():
    result = reset_session_cost_estimate(checkpoint_tokens=1000, provider="anthropic", model="not-a-real-model")
    assert result.missing_data is True


def test_reset_is_cheaper_than_compact_for_a_bulky_session_with_a_tight_checkpoint():
    """The whole point of RESET_SESSION: discard the bulky raw context in favor of the much
    smaller durable checkpoint -- reset (checkpoint-sized input) should cost less than compact
    (full-context-sized input) when the checkpoint is meaningfully smaller than the context."""
    reset = reset_session_cost_estimate(checkpoint_tokens=500, provider="anthropic", model="claude-sonnet-5")
    compact = compact_cost_estimate(context_used_tokens=50_000, provider="anthropic", model="claude-sonnet-5")
    assert reset.value < compact.value


def test_handoff_and_reset_cost_the_same_tokens_for_the_same_checkpoint():
    """The dollar delta between HANDOFF and RESET_SESSION is deliberately NOT modeled here --
    see founder_attention.py for where that distinction actually lives."""
    reset = reset_session_cost_estimate(checkpoint_tokens=800, provider="anthropic", model="claude-sonnet-5")
    handoff = handoff_cost_estimate(checkpoint_tokens=800, provider="anthropic", model="claude-sonnet-5")
    assert reset.value == handoff.value


def test_every_projection_discloses_it_is_an_estimate():
    for envelope in (
        reset_session_cost_estimate(checkpoint_tokens=100, provider="anthropic", model="claude-sonnet-5"),
        handoff_cost_estimate(checkpoint_tokens=100, provider="anthropic", model="claude-sonnet-5"),
        compact_cost_estimate(context_used_tokens=100, provider="anthropic", model="claude-sonnet-5"),
        estimated_cost_to_finish(cost_per_accepted_commit=_metric(1.0), remaining_commits=1),
    ):
        assert envelope.uncertainty is not None and "ESTIMATE" in envelope.uncertainty
