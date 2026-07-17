from dataclasses import dataclass

HIGH_THRESHOLD = 0.75
MEDIUM_THRESHOLD = 0.55


@dataclass
class TrustAssessment:
    level: str  # "high" | "medium" | "low" | "none"
    score: float


def assess_confidence(hits: list[dict]) -> TrustAssessment:
    """Confidence is derived from the strongest retrieved source, not from the model itself —
    an LLM cannot reliably self-report how well-grounded its own answer is, but a similarity
    score against the actual knowledge base is a concrete, checkable signal."""
    if not hits:
        return TrustAssessment(level="none", score=0.0)

    top_score = max(hit["score"] for hit in hits)
    if top_score >= HIGH_THRESHOLD:
        level = "high"
    elif top_score >= MEDIUM_THRESHOLD:
        level = "medium"
    else:
        level = "low"
    return TrustAssessment(level=level, score=top_score)


# Appended to the system prompt based on confidence — this is the actual enforcement
# mechanism (a prompt instruction), not just a cosmetic label in the response. It cannot
# guarantee the model complies, but it makes uncertainty the explicit default framing
# instead of something the model has to volunteer on its own.
TRUST_INSTRUCTIONS: dict[str, str] = {
    "high": (
        "Underlaget från kunskapsbiblioteket är starkt (hög likhet med frågan). Svara normalt "
        "utifrån källorna, men ange alltid vilka källor du bygger svaret på."
    ),
    "medium": (
        "Underlaget från kunskapsbiblioteket är begränsat (måttlig likhet med frågan). Du får "
        "svara, men måste tydligt markera vilka delar av svaret som är osäkra eller bygger på "
        "ofullständig information — skriv t.ex. 'Detta är osäkert utifrån tillgängligt underlag'."
    ),
    "low": (
        "Underlaget från kunskapsbiblioteket är svagt (låg likhet med frågan). Du FÅR INTE "
        "presentera gissningar som fakta. Säg uttryckligen att underlaget är otillräckligt för "
        "att svara säkert, och skilj tydligt mellan det du faktiskt vet från källorna och det "
        "du inte vet."
    ),
    "none": (
        "Ingen relevant källa hittades i kunskapsbiblioteket. Du FÅR INTE hitta på ett "
        "faktapåstående och presentera det som om det kom från företagets kunskap. Säg tydligt "
        "att du saknar underlag i kunskapsbiblioteket för denna fråga. Om du ändå svarar utifrån "
        "allmän kunskap, markera explicit att svaret INTE kommer från företagets egna källor."
    ),
}


def build_trust_instructions(level: str) -> str:
    return TRUST_INSTRUCTIONS.get(level, TRUST_INSTRUCTIONS["none"])
