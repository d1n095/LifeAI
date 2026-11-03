"""Role prompts for model-backed specialist workers.

Each prompt is built from ONLY the task's permitted context — the objective brief plus the
upstream verified outputs the broker packaged for this task — never from anything outside the
context package. The model is instructed to answer with a single JSON object matching the
capability's schema (see `schemas.SCHEMA_HINT`).
"""

from __future__ import annotations

import json

from app.mainai_business.schemas import SCHEMA_HINT
from app.providers.base import Message

_ROLE_INSTRUCTION: dict[str, str] = {
    "market_research": (
        "You are a market research specialist. Assess demand for the product described in the "
        "brief. Judge demand as low, moderate or high and give a one-sentence note."
    ),
    "unit_economics": (
        "You are a finance specialist. Judge whether the unit economics are viable (true/false) "
        "and give a one-sentence margin note. Use only the brief."
    ),
    "fulfillment_ops": (
        "You are an operations specialist. Judge whether fulfilment is feasible (true/false) and "
        "give a one-sentence risk note."
    ),
    "opportunity_synthesis": (
        "You are the lead analyst. Using ONLY the research, finance and operations findings "
        "provided, recommend one of: go, conditional-go, investigate-further, no-go, with a "
        "one-sentence reason. Do not invent facts not present in the findings."
    ),
    "assessment_publication": (
        "You are preparing the final assessment document for the Founder from the verified "
        "synthesis provided. Produce a title, the recommendation, and a one-sentence summary."
    ),
}


def build_messages(capability: str, *, objective_text: str, inputs: dict[str, dict]) -> list[Message]:
    instruction = _ROLE_INSTRUCTION.get(capability)
    if instruction is None:
        raise ValueError(f"no prompt for capability {capability!r}")
    hint = SCHEMA_HINT[capability]
    system = (
        f"{instruction}\n\n"
        f"Respond with ONE JSON object and nothing else, matching exactly this shape: {hint}\n"
        "No prose, no markdown fences, no explanation outside the JSON."
    )
    user_parts = [f"BRIEF:\n{objective_text}"]
    if inputs:
        # Only the permitted upstream findings — labelled by their task.
        user_parts.append("FINDINGS FROM EARLIER SPECIALISTS (treat as the only evidence):")
        for key, value in inputs.items():
            user_parts.append(f"- {key}: {json.dumps(value, ensure_ascii=False)}")
    user = "\n\n".join(user_parts)
    return [Message(role="system", content=system), Message(role="user", content=user)]
