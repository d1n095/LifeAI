from decimal import Decimal

# USD per 1,000 tokens (input, output). Approximate public list prices — NOT billing-grade
# accuracy, meant for a directional cost dashboard in the admin panel. Update as providers
# change pricing; unknown provider/model combinations return None rather than a fabricated
# number (see estimate_cost below).
PRICING_PER_1K_TOKENS: dict[str, dict[str, tuple[Decimal, Decimal]]] = {
    "openai": {
        "gpt-4o-mini": (Decimal("0.00015"), Decimal("0.0006")),
        "gpt-4o": (Decimal("0.0025"), Decimal("0.01")),
        "text-embedding-3-small": (Decimal("0.00002"), Decimal("0")),
    },
    "anthropic": {
        "claude-sonnet-5": (Decimal("0.003"), Decimal("0.015")),
        "claude-haiku-4-5-20251001": (Decimal("0.0008"), Decimal("0.004")),
        "claude-opus-4-8": (Decimal("0.015"), Decimal("0.075")),
    },
    "gemini": {
        "gemini-2.5-flash": (Decimal("0.000075"), Decimal("0.0003")),
        "gemini-2.5-pro": (Decimal("0.00125"), Decimal("0.005")),
    },
    "deepseek": {
        "deepseek-chat": (Decimal("0.00027"), Decimal("0.0011")),
    },
    "ollama": {
        # Local inference — always free regardless of model.
    },
}


def estimate_cost(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> Decimal | None:
    if provider == "ollama":
        return Decimal("0")

    rates = PRICING_PER_1K_TOKENS.get(provider, {}).get(model)
    if rates is None:
        return None

    input_rate, output_rate = rates
    cost = (Decimal(prompt_tokens) / 1000) * input_rate + (Decimal(completion_tokens) / 1000) * output_rate
    return cost.quantize(Decimal("0.000001"))
