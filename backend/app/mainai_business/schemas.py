"""Structured, schema-validated outputs for model-backed specialist workers.

A local model returns free text; a specialist's output is only usable if it parses into a
known shape. These Pydantic models are that contract. `parse_capability_output` extracts the
first JSON object from raw model text (tolerating code fences and surrounding prose) and
validates it — a model that cannot produce the shape fails the task honestly, it is never
coerced into a fake success.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError


class SchemaValidationError(Exception):
    pass


class MarketResearchOut(BaseModel):
    demand: Literal["low", "moderate", "high"]
    note: str
    segments: list[str] = Field(default_factory=list)


class UnitEconomicsOut(BaseModel):
    viable: bool
    margin_note: str


class FulfillmentOpsOut(BaseModel):
    feasible: bool
    risk_note: str


class OpportunitySynthesisOut(BaseModel):
    recommendation: Literal["go", "conditional-go", "investigate-further", "no-go"]
    reason: str


class PublicationOut(BaseModel):
    title: str
    recommendation: str
    summary: str


_SCHEMA_BY_CAPABILITY: dict[str, type[BaseModel]] = {
    "market_research": MarketResearchOut,
    "unit_economics": UnitEconomicsOut,
    "fulfillment_ops": FulfillmentOpsOut,
    "opportunity_synthesis": OpportunitySynthesisOut,
    "assessment_publication": PublicationOut,
}

# Field list surfaced to the model in the prompt, so even a tiny local model is told exactly
# what JSON to emit.
SCHEMA_HINT: dict[str, str] = {
    "market_research": '{"demand": "low|moderate|high", "note": "<short>", "segments": ["<seg>"]}',
    "unit_economics": '{"viable": true|false, "margin_note": "<short>"}',
    "fulfillment_ops": '{"feasible": true|false, "risk_note": "<short>"}',
    "opportunity_synthesis": '{"recommendation": "go|conditional-go|investigate-further|no-go", "reason": "<short>"}',
    "assessment_publication": '{"title": "<short>", "recommendation": "<short>", "summary": "<short>"}',
}


def _extract_json_object(raw: str) -> dict:
    """Find the first balanced {...} object in free text. Tolerates ```json fences and prose."""
    if raw is None:
        raise SchemaValidationError("empty model output")
    text = raw.strip()
    # Strip a leading code fence if present.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start == -1:
        raise SchemaValidationError("no JSON object in model output")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    obj = json.loads(candidate)
                except json.JSONDecodeError as exc:
                    raise SchemaValidationError(f"invalid JSON: {exc}") from exc
                if not isinstance(obj, dict):
                    raise SchemaValidationError("top-level JSON is not an object")
                return obj
    raise SchemaValidationError("unbalanced JSON object in model output")


def parse_capability_output(capability: str, raw: str) -> dict:
    """Parse + schema-validate a model's raw text for a capability. Raises on any mismatch."""
    schema = _SCHEMA_BY_CAPABILITY.get(capability)
    if schema is None:
        raise SchemaValidationError(f"no output schema for capability {capability!r}")
    obj = _extract_json_object(raw)
    try:
        model = schema.model_validate(obj)
    except ValidationError as exc:
        raise SchemaValidationError(f"schema mismatch for {capability}: {exc}") from exc
    data = model.model_dump()
    data["capability"] = capability
    return data
