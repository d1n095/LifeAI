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


def build_messages(
    capability: str,
    *,
    objective_text: str,
    inputs: dict[str, dict],
    feedback: str | None = None,
) -> list[Message]:
    instruction = _ROLE_INSTRUCTION.get(capability)
    if instruction is None:
        raise ValueError(f"no prompt for capability {capability!r}")
    hint = SCHEMA_HINT[capability]
    system_parts = [
        instruction,
        f"Respond with ONE JSON object and nothing else, matching exactly this shape: {hint}",
        "No prose, no markdown fences, no explanation outside the JSON.",
    ]
    # Publication must reflect the verified synthesis exactly — give the model the exact
    # recommendation string to copy, so the independent validator (which requires it) passes.
    if capability == "assessment_publication":
        rec = (inputs.get("opportunity_synthesis") or {}).get("recommendation")
        if rec:
            system_parts.append(
                f'Set "recommendation" to EXACTLY this string, copied verbatim: "{rec}". '
                "Do not paraphrase or change it."
            )
    system = "\n\n".join(system_parts)

    user_parts = [f"BRIEF:\n{objective_text}"]
    if inputs:
        # Only the permitted upstream findings — labelled by their task.
        user_parts.append("FINDINGS FROM EARLIER SPECIALISTS (treat as the only evidence):")
        for key, value in inputs.items():
            user_parts.append(f"- {key}: {json.dumps(value, ensure_ascii=False)}")
    if feedback:
        # Bounded, authorized correction attempt: the previous output was rejected by the
        # independent validator. This re-prompt NEVER bypasses validation — it only asks for a
        # corrected JSON that will be validated again from scratch.
        user_parts.append(
            f"YOUR PREVIOUS ANSWER WAS REJECTED by an independent validator: {feedback}\n"
            "Return a corrected JSON object that fixes this. Keep to the required shape."
        )
    user = "\n\n".join(user_parts)
    return [Message(role="system", content=system), Message(role="user", content=user)]
