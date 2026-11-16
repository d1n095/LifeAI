"""Field-shape compatibility seam with Codex's Continuous Supervision candidate (SHA
`a7df7f90dba9f8bc993005b2cce1d4c8cb7dcec4`, `mainai_supervision_telemetry` table) -- see
docs/mainai_v2/MAINAI_RESOURCE_INTELLIGENCE_ROUND2_ADDENDUM.md for the architecture decision
this module implements.

That candidate lives on a completely separate, unmerged branch lineage (confirmed via
`git merge-base --is-ancestor` in both directions, same as every other sibling-program check
this program's own handoffs already record) -- this module NEVER imports anything from it,
never queries its table, and is not itself imported by it. It exists purely so that IF/WHEN
both candidates eventually land on the same trunk, this package's own telemetry model already
knows how to accept a plain dict shaped like that table's own columns, without either side
having to import the other or without inventing a THIRD, incompatible telemetry shape in the
meantime.

TRANSLATION ONLY, NEVER A SECOND WRITE PATH: `from_supervision_telemetry_row()` is a pure
function -- it takes a plain `dict` (column_name -> value, exactly matching
`mainai_supervision_telemetry`'s own schema as reviewed) and returns plain kwargs shaped for
this package's own `telemetry.record_telemetry_sample()` / `MetricEnvelope`-producing
functions. It never calls `record_telemetry_sample()` itself and never touches a `db: Session`
-- the caller decides whether/how to persist the translated result, exactly like every other
pure function in this package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The exact column names on `mainai_supervision_telemetry`, as reviewed directly in that
# candidate's own migration (`0075_supervision_resource_telemetry.py`) -- kept as an explicit,
# checkable set so a future rename on either side fails loudly (via `translate()`'s own
# `unrecognized` return) rather than silently mismapping data.
SUPERVISION_TELEMETRY_COLUMNS = frozenset(
    {
        "owner_id", "agent_id", "job_id", "attempt_id", "provider", "model", "state",
        "productive_seconds", "idle_seconds", "blocked_seconds", "stalled_seconds",
        "continuation_count", "premature_return_count", "restart_count",
        "context_input_tokens", "context_output_tokens", "context_cached_tokens",
        "context_limit_tokens", "provider_quota_remaining", "estimated_cost", "reported_cost",
        "validated_cost", "retries", "failed_attempts", "rework_count", "examiner_outcome",
        "last_progress_at", "handoff_ready", "context_risk", "observed_at",
    }
)

@dataclass(frozen=True)
class TranslationResult:
    """`sample_kwargs` -- ready to splat into `telemetry.record_telemetry_sample(db, owner_id=,
    assignment_id=, attempt_id=, **sample_kwargs)` once a caller has resolved the real
    `assignment_id` this supervision `job_id` corresponds to (this module has no such mapping
    of its own -- job_id/assignment_id correlation is the caller's own responsibility, exactly
    like every other cross-boundary id this package never invents a join for on its own).
    `unrecognized` -- any input key that is NOT a real `mainai_supervision_telemetry` column,
    surfaced rather than silently dropped, so a future rename on either side is caught."""

    sample_kwargs: dict[str, Any]
    provenance: dict[str, Any]
    notes: tuple[str, ...]
    unrecognized: tuple[str, ...]


def from_supervision_telemetry_row(row: dict[str, Any]) -> TranslationResult:
    """Pure translation, `dict` in, `TranslationResult` out. Every field the source row does
    not carry (a `None` value, or a key simply absent from `row`) is left `None` in
    `sample_kwargs` -- UNKNOWN STAYS UNKNOWN across the translation, never fabricated as `0`."""

    unrecognized = tuple(sorted(set(row) - SUPERVISION_TELEMETRY_COLUMNS))
    notes: list[str] = []

    sample_kwargs: dict[str, Any] = {
        "context_used_tokens": row.get("context_input_tokens"),
        "context_window_tokens": row.get("context_limit_tokens"),
        "input_tokens": row.get("context_input_tokens"),
        "output_tokens": row.get("context_output_tokens"),
        "cached_tokens": row.get("context_cached_tokens"),
    }
    if row.get("context_input_tokens") is not None:
        notes.append(
            "context_used_tokens is mapped from supervision's own context_input_tokens (tokens "
            "consumed so far), not input_tokens+output_tokens combined -- the closer analogue "
            "of 'current context size' this package's own context_utilization() expects"
        )

    provenance: dict[str, Any] = {
        "translated_from": "mainai_supervision_telemetry",
        "source_agent_id": row.get("agent_id"),
        "source_job_id": row.get("job_id"),
        "source_attempt_id": row.get("attempt_id"),
        "source_provider": row.get("provider"),
        "source_model": row.get("model"),
        "source_state": row.get("state"),
        "source_observed_at": row.get("observed_at"),
    }

    return TranslationResult(
        sample_kwargs=sample_kwargs,
        provenance={k: v for k, v in provenance.items() if v is not None},
        notes=tuple(notes),
        unrecognized=unrecognized,
    )


def supervision_cost_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Cost-related fields only (`estimated_cost`/`reported_cost`/`validated_cost`), separated
    from `from_supervision_telemetry_row()` because this package's own cost model
    (`cost_bridge.py`) is settled-ledger-only -- a supervision row's `estimated_cost`/`reported_
    cost` are exactly the self-reported, NOT-YET-VERIFIED figures that Continuous Supervision's
    own `settle()` already treats as `UNCERTAIN` unless independently verified (see that
    candidate's own `supervision_spend.py`). This function never upgrades them to `VERIFIED`;
    a caller wanting `cost_bridge.py`'s own real settled truth must still go there."""

    return {
        "estimated_cost": row.get("estimated_cost"),
        "reported_cost": row.get("reported_cost"),
        "validated_cost": row.get("validated_cost"),
        "classification": "UNCERTAIN" if row.get("validated_cost") is None else "VALIDATED_BY_SUPERVISION",
    }
