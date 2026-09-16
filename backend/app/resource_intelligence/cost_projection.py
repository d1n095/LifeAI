"""Pure cost-to-finish / reset / handoff / compact projections -- no `db: Session`, no I/O,
matching `decision.py`'s own pure-function shape exactly. See
docs/mainai_v2/MAINAI_RESOURCE_INTELLIGENCE_ROUND2_ADDENDUM.md for the architecture decision
this module implements.

ESTIMATE != ACTUAL: every function here returns a `MetricEnvelope` whose `uncertainty` field
always discloses that this is a projection, never a settled `provider_spend` actual --
`cost_bridge.py` remains the sole source of REAL, settled cost; this module only ever projects
FORWARD from real inputs (an efficiency profile's own `cost_per_accepted_commit`, a real
token-pricing table) using an openly documented, disclosed model, never a black box.

Reuses `app.providers.pricing.estimate_cost()` (the existing, real, hardcoded price table) for
every token->dollar conversion -- never a second pricing table. An unpriced provider/model
combination returns `missing_data=True`, matching that function's own `None`-on-unknown
contract, never a fabricated dollar figure."""

from __future__ import annotations

from app.providers.pricing import estimate_cost
from app.resource_intelligence.types import MetricEnvelope, unknown_metric

# A re-read's own OUTPUT (a summary, or a freshly-oriented agent's first acknowledgment) is
# assumed to be a small fraction of whatever it read as INPUT -- documented, hand-picked
# starting points (no historical transcript data exists yet to fit one against), same
# convention as every other threshold in this package.
COMPACT_OUTPUT_TOKEN_RATIO = 0.05
REORIENT_OUTPUT_TOKEN_RATIO = 0.1


def estimated_cost_to_finish(*, cost_per_accepted_commit: MetricEnvelope, remaining_commits: int | None) -> MetricEnvelope:
    """`cost_per_accepted_commit.value * remaining_commits` -- the honest floor of what this
    module can project without a real remaining-work-size signal (none exists anywhere in this
    codebase yet, matching Round 1's own documented `task_remaining_size` limitation).
    `missing_data=True` when `remaining_commits` is `None` (no caller-supplied estimate of how
    much work remains), or when the underlying `cost_per_accepted_commit` envelope is itself
    `missing_data=True` or PROVISIONAL (a provisional per-commit cost is not a stable enough
    number to multiply into a forward projection -- ONE_RUN != LONG_TERM_PROFILE applies here
    too)."""

    definition = "cost_per_accepted_commit.value * remaining_commits -- a floor estimate of remaining spend to finish the current job"
    if remaining_commits is None:
        return unknown_metric(unit="usd", definition=definition, source="resource_intelligence.cost_projection", method="no remaining_commits estimate was supplied by the caller")
    if cost_per_accepted_commit.missing_data or cost_per_accepted_commit.value is None:
        return unknown_metric(unit="usd", definition=definition, source="resource_intelligence.cost_projection", method="cost_per_accepted_commit is missing_data=True; cannot project a per-commit cost forward")
    if "PROVISIONAL" in (cost_per_accepted_commit.uncertainty or ""):
        return unknown_metric(
            unit="usd", definition=definition, source="resource_intelligence.cost_projection",
            method="cost_per_accepted_commit is still PROVISIONAL (below the minimum established sample size); not stable enough to project forward",
            uncertainty="ONE_RUN != LONG_TERM_PROFILE: a provisional per-commit cost is not multiplied into a forward estimate",
        )
    value = float(cost_per_accepted_commit.value) * remaining_commits
    return MetricEnvelope(
        value=value, unit="usd", definition=definition, denominator="remaining_commits",
        time_window=None, population=cost_per_accepted_commit.population, sample_size=cost_per_accepted_commit.sample_size,
        source="resource_intelligence.cost_projection",
        method=f"{cost_per_accepted_commit.value} usd/commit * {remaining_commits} remaining_commits",
        missing_data=False,
        uncertainty="ESTIMATE, not an actual: assumes remaining work costs the same per-commit as the established historical average; a harder remaining task will cost more",
        last_updated=cost_per_accepted_commit.last_updated, trend=None,
    )


def _token_reread_cost(*, input_tokens: int | None, provider: str | None, model: str | None, output_ratio: float, action_label: str, input_label: str) -> MetricEnvelope:
    definition = f"estimated USD for a {action_label}: {input_label} re-consumed as fresh input, plus a small output allowance"
    source = "resource_intelligence.cost_projection"
    if input_tokens is None or provider is None or model is None:
        return unknown_metric(
            unit="usd", definition=definition, source=source,
            method=f"{input_label}/provider/model must all be known to project a {action_label} cost (got {input_label}={input_tokens}, provider={provider}, model={model})",
        )
    output_tokens = round(input_tokens * output_ratio)
    cost = estimate_cost(provider, model, prompt_tokens=input_tokens, completion_tokens=output_tokens)
    if cost is None:
        return unknown_metric(unit="usd", definition=definition, source=source, method=f"no pricing table entry for provider={provider!r} model={model!r} in app.providers.pricing")
    return MetricEnvelope(
        value=float(cost), unit="usd", definition=definition, denominator=None,
        time_window=None, population=f"provider={provider} model={model}", sample_size=None, source=source,
        method=f"estimate_cost(provider={provider!r}, model={model!r}, prompt_tokens={input_tokens}, completion_tokens={output_tokens})",
        missing_data=False,
        uncertainty=f"ESTIMATE, not an actual: assumes {input_label} fully captures what must be re-read; an incomplete checkpoint/summary needs more, a tighter one needs less",
        last_updated=None, trend=None,
    )


def reset_session_cost_estimate(*, checkpoint_tokens: int | None, provider: str | None, model: str | None) -> MetricEnvelope:
    """Modeled as the durable SESSION CHECKPOINT (`session_checkpoint.py`'s own compact,
    already-summarized state) re-consumed as fresh input on the SAME provider/model, plus a
    small re-orientation output allowance -- deliberately NOT the full prior `context_used_
    tokens`: the entire point of a reset is discarding the bulky raw context in favor of the
    much smaller durable checkpoint, so a reset is normally CHEAPER in raw tokens than a
    compact, at the cost of the fine-grained conversational detail a compact would have kept
    (a quality/risk cost, not a dollar one -- see `founder_attention.py` and `decision.py`'s
    own reset-vs-compact comparison for how that risk is weighed instead)."""

    return _token_reread_cost(input_tokens=checkpoint_tokens, provider=provider, model=model, output_ratio=REORIENT_OUTPUT_TOKEN_RATIO, action_label="RESET_SESSION", input_label="checkpoint_tokens")


def handoff_cost_estimate(*, checkpoint_tokens: int | None, provider: str | None, model: str | None) -> MetricEnvelope:
    """Same durable-checkpoint re-read model as `reset_session_cost_estimate()` -- a NEW agent
    reading the same checkpoint pays the same re-read cost a reset would. The difference
    between HANDOFF and RESET_SESSION is not modeled here as a dollar delta at all: it is
    `founder_attention.py`'s own concern (a handoff also costs founder attention to choose and
    confirm a new agent identity, a reset does not) -- `decision.py` combines both signals,
    this function never blends them into one number."""

    return _token_reread_cost(input_tokens=checkpoint_tokens, provider=provider, model=model, output_ratio=REORIENT_OUTPUT_TOKEN_RATIO, action_label="HANDOFF", input_label="checkpoint_tokens")


def compact_cost_estimate(*, context_used_tokens: int | None, provider: str | None, model: str | None) -> MetricEnvelope:
    """A `COMPACT` is modeled as one summarization call over the CURRENT, still-uncompacted
    context: the full `context_used_tokens` as input (a summarizer must read everything to
    condense it), but a much smaller `COMPACT_OUTPUT_TOKEN_RATIO` fraction of that as output (a
    summary, not a restatement). Unlike reset/handoff, this one genuinely needs the FULL raw
    context size as input -- there is no smaller durable checkpoint yet at the moment a compact
    is being considered; the checkpoint is compact's own OUTPUT, not its input."""

    return _token_reread_cost(input_tokens=context_used_tokens, provider=provider, model=model, output_ratio=COMPACT_OUTPUT_TOKEN_RATIO, action_label="COMPACT", input_label="context_used_tokens")
