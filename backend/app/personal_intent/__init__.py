"""Stage J — personal intent learning."""

from app.personal_intent.service import (
    AmbiguityClass,
    IntentResolution,
    PersonalIntentError,
    classify_ambiguity,
    correct_intent_binding,
    record_intent_binding,
    resolve_with_learned_intent,
)

__all__ = [
    "AmbiguityClass",
    "IntentResolution",
    "PersonalIntentError",
    "classify_ambiguity",
    "correct_intent_binding",
    "record_intent_binding",
    "resolve_with_learned_intent",
]
