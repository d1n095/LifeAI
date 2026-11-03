"""Deterministic independent validators for model-backed outputs.

IMPORTANT (directive): independent verification here is a DETERMINISTIC VALIDATOR run by the
verifier agent — real code checking the output against its schema and against the permitted
upstream findings. It is NOT the builder model reviewing its own output, and it is NOT a
second model rating the first. That distinction is the whole point: a model is never trusted
to certify itself.

A validator returns (ok, reason). The engine promotes an output to VERIFIED only when its
validator passes; otherwise the output stays untrusted (REJECTED).
"""

from __future__ import annotations

from typing import Callable

_GO_LIKE = {"go", "conditional-go"}


def _v_market_research(data: dict, inputs: dict) -> tuple[bool, str]:
    if data.get("demand") not in ("low", "moderate", "high"):
        return False, "demand not in {low,moderate,high}"
    if not str(data.get("note") or "").strip():
        return False, "missing note"
    return True, "demand classified with a note"


def _v_unit_economics(data: dict, inputs: dict) -> tuple[bool, str]:
    if not isinstance(data.get("viable"), bool):
        return False, "viable not boolean"
    if not str(data.get("margin_note") or "").strip():
        return False, "missing margin_note"
    return True, "viability is a boolean with a note"


def _v_fulfillment_ops(data: dict, inputs: dict) -> tuple[bool, str]:
    if not isinstance(data.get("feasible"), bool):
        return False, "feasible not boolean"
    if not str(data.get("risk_note") or "").strip():
        return False, "missing risk_note"
    return True, "feasibility is a boolean with a note"


def _v_opportunity_synthesis(data: dict, inputs: dict) -> tuple[bool, str]:
    rec = data.get("recommendation")
    if rec not in ("go", "conditional-go", "investigate-further", "no-go"):
        return False, "recommendation not in allowed set"
    if not str(data.get("reason") or "").strip():
        return False, "missing reason"
    # Consistency with the permitted upstream findings: a go/conditional-go must not contradict
    # finance viability or operations feasibility that were handed to the synthesiser.
    finance = inputs.get("unit_economics") or {}
    ops = inputs.get("fulfillment_ops") or {}
    if rec in _GO_LIKE:
        if finance.get("viable") is False:
            return False, "recommends go while finance reported not viable"
        if ops.get("feasible") is False:
            return False, "recommends go while operations reported not feasible"
    return True, "recommendation consistent with upstream findings"


def _v_assessment_publication(data: dict, inputs: dict) -> tuple[bool, str]:
    for field in ("title", "recommendation", "summary"):
        if not str(data.get(field) or "").strip():
            return False, f"missing {field}"
    synthesis = inputs.get("opportunity_synthesis") or {}
    expected = synthesis.get("recommendation")
    if expected and expected not in str(data.get("recommendation")):
        return False, "published recommendation does not match the verified synthesis"
    return True, "publication reflects the verified synthesis"


_VALIDATORS: dict[str, Callable[[dict, dict], tuple[bool, str]]] = {
    "market_research": _v_market_research,
    "unit_economics": _v_unit_economics,
    "fulfillment_ops": _v_fulfillment_ops,
    "opportunity_synthesis": _v_opportunity_synthesis,
    "assessment_publication": _v_assessment_publication,
}


def validate_output(capability: str, data: dict, inputs: dict) -> tuple[bool, str]:
    validator = _VALIDATORS.get(capability)
    if validator is None:
        return False, f"no validator for capability {capability!r}"
    return validator(data or {}, inputs or {})
