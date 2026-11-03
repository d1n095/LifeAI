"""In-process specialist workers for the business workflow.

HONESTY — these are DETERMINISTIC, LOCAL, IN-PROCESS computations, NOT model calls. They
take only the task-scoped context the broker packaged (upstream verified outputs + the
objective text) and return a structured result. No network, no provider, no credentials —
consistent with `workforce.vertical_slice` / `safe_internal_run`, and with the boundary that
provider activation stays gated (first_cycle_go=false). Wiring the verified local model from
PR #334 (Ollama all-minilm/qwen) in as the real worker inference is the honest next step; the
*orchestration* around these workers (selection, scoped authority, verification, persistence,
recovery) is fully real and is what this increment exercises.

A worker receives untrusted upstream data as DATA only (the broker already scrubbed authority
mutations) and must never read anything outside what it is handed.
"""

from __future__ import annotations

from typing import Any, Callable


class WorkerError(Exception):
    pass


def _market_research(objective_text: str, inputs: dict[str, Any]) -> dict[str, Any]:
    # Deterministic synthetic market read. In a real run this is the research specialist's
    # provider-backed output; here it is a fixed, inspectable analysis of the brief.
    return {
        "capability": "market_research",
        "segments": ["value-seekers", "convenience-first households"],
        "estimated_tam_units_per_month": 42000,
        "demand_signal": "moderate-positive",
        "top_competitors": ["incumbent-A", "marketplace-B"],
        "differentiation": "bundling + faster local fulfilment",
        "sources": ["synthetic:market-brief", "synthetic:category-trend"],
        "confidence": 0.62,
    }


def _unit_economics(objective_text: str, inputs: dict[str, Any]) -> dict[str, Any]:
    # Transparent arithmetic over synthetic assumptions.
    aov = 48.0
    cogs_pct = 0.44
    shipping = 6.5
    payment_pct = 0.029
    cac = 18.0
    gross_margin = aov * (1 - cogs_pct) - shipping - aov * payment_pct
    contribution_after_cac = gross_margin - cac
    breakeven_units = None
    if contribution_after_cac > 0:
        fixed_monthly = 9000.0
        breakeven_units = round(fixed_monthly / contribution_after_cac, 1)
    return {
        "capability": "unit_economics",
        "aov_usd": aov,
        "cogs_pct": cogs_pct,
        "gross_margin_usd": round(gross_margin, 2),
        "cac_usd": cac,
        "contribution_after_cac_usd": round(contribution_after_cac, 2),
        "breakeven_units_per_month": breakeven_units,
        "viable_contribution": contribution_after_cac > 0,
        "confidence": 0.7,
    }


def _fulfillment_ops(objective_text: str, inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "capability": "fulfillment_ops",
        "model": "3PL + regional micro-hub",
        "avg_lead_time_days": 2.5,
        "returns_rate_est": 0.08,
        "risk_factors": ["single-3PL dependency", "peak-season capacity"],
        "mitigations": ["second 3PL on standby", "pre-booked peak capacity"],
        "feasible": True,
        "confidence": 0.66,
    }


def _opportunity_synthesis(objective_text: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """Consume the three verified upstream outputs and recommend. Requires them present."""
    research = inputs.get("market_research")
    finance = inputs.get("unit_economics")
    ops = inputs.get("fulfillment_ops")
    missing = [k for k, v in (("market_research", research), ("unit_economics", finance), ("fulfillment_ops", ops)) if not v]
    if missing:
        raise WorkerError(f"synthesis missing verified upstream inputs: {missing}")

    viable_finance = bool(finance.get("viable_contribution"))
    feasible_ops = bool(ops.get("feasible"))
    demand_ok = research.get("demand_signal") in ("moderate-positive", "positive")

    if viable_finance and feasible_ops and demand_ok:
        recommendation = "conditional-go"
    elif viable_finance and (feasible_ops or demand_ok):
        recommendation = "investigate-further"
    else:
        recommendation = "no-go"

    conditions = []
    if recommendation == "conditional-go":
        conditions = [
            f"hold CAC at or below ${finance.get('cac_usd')}",
            "secure the standby 3PL before launch",
            f"validate demand against ~{research.get('estimated_tam_units_per_month')} unit TAM",
        ]
    confidences = [c for c in (research.get("confidence"), finance.get("confidence"), ops.get("confidence")) if isinstance(c, (int, float))]
    blended = round(sum(confidences) / len(confidences), 3) if confidences else None
    return {
        "capability": "opportunity_synthesis",
        "recommendation": recommendation,
        "rationale": {
            "finance_viable": viable_finance,
            "ops_feasible": feasible_ops,
            "demand_ok": demand_ok,
        },
        "conditions": conditions,
        "blended_confidence": blended,
        "inputs_considered": ["market_research", "unit_economics", "fulfillment_ops"],
    }


def _assessment_publication(objective_text: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """Consequential step — only ever reached after an explicit Founder approval gate in the
    engine. Produces the Founder-facing assessment document. Still no external effect: it
    returns the document as data for the Founder interface; it does not post anywhere."""
    synthesis = inputs.get("opportunity_synthesis")
    if not synthesis:
        raise WorkerError("publication requires a verified synthesis")
    return {
        "capability": "assessment_publication",
        "document": {
            "title": "E-commerce opportunity assessment",
            "recommendation": synthesis.get("recommendation"),
            "conditions": synthesis.get("conditions"),
            "confidence": synthesis.get("blended_confidence"),
        },
        "published_channel": "founder_interface",  # internal surface only, not external
        "external_effect": False,
    }


_WORKERS: dict[str, Callable[[str, dict[str, Any]], dict[str, Any]]] = {
    "market_research": _market_research,
    "unit_economics": _unit_economics,
    "fulfillment_ops": _fulfillment_ops,
    "opportunity_synthesis": _opportunity_synthesis,
    "assessment_publication": _assessment_publication,
}


def run_worker(capability: str, *, objective_text: str, inputs: dict[str, Any]) -> dict[str, Any]:
    worker = _WORKERS.get(capability)
    if worker is None:
        raise WorkerError(f"no in-process worker for capability {capability!r}")
    result = worker(objective_text, dict(inputs or {}))
    result.setdefault("provider_invoked", False)
    result.setdefault("runtime", "in_process_deterministic")
    return result
