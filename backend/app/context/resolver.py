"""Conversation Context Resolver v1 (DEL 7) — a small, rule-based classifier that decides
what kind of turn the founder's latest message is, given recent conversation history. See
docs/CONTEXT_RESOLVER_V1.md for the full design rationale.

Deliberately NOT an LLM call: every classification here is a plain heuristic over the
message text and message timestamps, auditable and testable without a provider key. It is a
FIRST pass, not a claim of understanding — every result carries an explicit confidence, and
"uncertain_reference" is a first-class outcome, not a failure mode to hide.

Hard constraint (see the founder's own instruction): this module NEVER infers emotional or
psychological state (stress, mood, etc.). It has no vocabulary or logic for that at all —
not "detects but suppresses", genuinely absent. If a future caller wants that signal, it may
only ever come from the user explicitly stating it in their own words, verbatim, never
inferred from tone, word choice, or punctuation.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Categories from the founder's own list — kept as plain strings (not an enum) so a new
# category can be added without a migration; nothing here is persisted yet (see
# docs/CONTEXT_RESOLVER_V1.md's "not built tonight" section).
INTENT_CONTINUATION = "continuation"
INTENT_NEW_TOPIC = "new_topic"
INTENT_CORRECTION = "correction"
INTENT_QUESTION_ABOUT_PREVIOUS = "question_about_previous"
INTENT_PRONOUN_REFERENCE = "pronoun_reference"
INTENT_NAVIGATION_QUESTION = "navigation_question"
INTENT_WORK_COMMAND = "work_command"
INTENT_IDEA_WORTH_SAVING = "idea_worth_saving"
INTENT_EXPLICIT_MEMORY = "explicit_memory"
INTENT_UNCERTAIN_REFERENCE = "uncertain_reference"

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

MAX_VERBATIM_MESSAGES = 5
MAX_WEIGHTED_MESSAGES = 15
LONG_GAP = timedelta(minutes=30)

# --- Marker vocab. Swedish, lowercase, matched against a normalized message. Every list is
# short and literal on purpose — this is a heuristic first pass, not an NLP model, and a
# short, auditable list is easier to reason about (and to catch a bad entry in review) than
# a large fuzzy one. See docs/CONTEXT_RESOLVER_V1.md for the false-positive/negative
# trade-offs this implies.

_EXPLICIT_MEMORY_MARKERS = ["kom ihåg", "spara det här", "spara detta", "notera att", "minns att", "glöm inte"]
_IDEA_MARKERS = ["tänk om", "vi borde", "idé:", "ide:", "vore inte det", "skulle kunna"]
_CORRECTION_MARKERS = [
    "nej,", "nej ", "det stämmer inte", "fel,", "det är fel", "inte det", "sluta tappa tråden",
    "det var inte", "inte längre", "ändra till", "istället för", "det finns inte", "finns inte där",
]
_NAVIGATION_MARKERS = ["vad ska jag trycka", "var är", "hur gör jag", "var hittar jag", "vart ska jag", "vilken knapp"]
_CONTINUATION_MARKERS = ["nu då", "nästa", "gör samma", "fortsätt", "och sen", "vad mer", "forts"]
_QUESTION_ABOUT_PREVIOUS_MARKERS = ["vad menade du", "förklara det igen", "vad menar du med det", "utveckla det"]
_WORK_COMMAND_MARKERS = ["gör", "skapa", "bygg", "fixa", "ändra", "uppdatera", "ta bort", "radera", "skriv"]
# Deliberately excludes "där"/"dit" — both are extremely common Swedish locative adverbs
# ("där borta" = "over there") as well as referential pronouns, and in practice trigger far
# more false positives (an ordinary new sentence that happens to mention a place) than true
# references to prior conversation content. "den finns inte där" is still caught correctly —
# via the _CORRECTION_MARKERS phrase "finns inte där", not via this pronoun list.
_PRONOUNS = {"han", "hon", "den", "det", "dem", "de", "denna", "detta", "denne"}

_WORD_RE = re.compile(r"[a-zA-ZåäöÅÄÖ0-9]+")


@dataclass
class ConversationMessage:
    role: str  # "user" | "assistant"
    content: str
    created_at: datetime


@dataclass
class ContextResolution:
    intent: str
    confidence: str
    reasoning: str
    referenced_message_index: int | None = None  # index into the messages passed in, most-recent-first not assumed
    time_gap_seconds: float | None = None
    project_ambiguous: bool = False
    matched_project_id: uuid.UUID | None = None
    signals: list[str] = field(default_factory=list)


def _normalize(text: str) -> str:
    return text.strip().lower()


def _contains_any(text: str, markers: list[str]) -> str | None:
    for marker in markers:
        if marker in text:
            return marker
    return None


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) > 2}


def _is_pronoun_heavy(text: str) -> bool:
    tokens = _WORD_RE.findall(text.lower())
    if not tokens:
        return False
    pronoun_count = sum(1 for t in tokens if t in _PRONOUNS)
    # A short message dominated by pronouns ("han är klar nu", "den finns inte där") reads
    # as a reference to something already in the conversation, not a self-contained new
    # statement — the threshold (>=1 pronoun in a <=6-word message) is deliberately generous
    # since real short follow-ups are exactly this shape.
    return pronoun_count >= 1 and len(tokens) <= 6


def _topic_overlap(current: str, recent: list[ConversationMessage]) -> float:
    """Fraction of the current message's significant words that also appear in the recent
    window — a cheap, explainable stand-in for "is this about the same thing", not a claim
    of semantic understanding. 0.0 if there's no recent history or no significant words."""
    current_words = _words(current)
    if not current_words or not recent:
        return 0.0
    recent_words: set[str] = set()
    for m in recent:
        recent_words |= _words(m.content)
    if not recent_words:
        return 0.0
    return len(current_words & recent_words) / len(current_words)


def resolve_context(
    current_message: str,
    history: list[ConversationMessage],
    *,
    now: datetime | None = None,
    current_project_id: uuid.UUID | None = None,
    candidate_project_names: dict[str, uuid.UUID] | None = None,
) -> ContextResolution:
    """Classifies `current_message` given up to the last MAX_WEIGHTED_MESSAGES messages
    (`history`, oldest first — only the tail is actually used). See module docstring for the
    category list and the explicit "no psychological inference" constraint.

    `candidate_project_names` (name -> id) is used only for the "two projects with similar
    names" disambiguation test: if the message text matches more than one candidate name,
    the resolution is marked project_ambiguous rather than silently guessing one.
    """
    now = now or datetime.utcnow()
    text = _normalize(current_message)
    verbatim = history[-MAX_VERBATIM_MESSAGES:]
    weighted = history[-MAX_WEIGHTED_MESSAGES:]

    time_gap_seconds = None
    gap_is_long = False
    if history:
        last_at = history[-1].created_at
        delta = now - last_at
        time_gap_seconds = delta.total_seconds()
        gap_is_long = delta > LONG_GAP

    project_ambiguous = False
    matched_project_id = current_project_id
    if candidate_project_names:
        matched_names = [name for name in candidate_project_names if name.lower() in text]
        # If one matched name is itself a substring of another matched name (e.g. "Life OS"
        # vs. "Life OS Mobile", both of which would naively match text containing "Life OS
        # Mobile"), the longer, more specific name is what the founder actually typed —
        # drop the shorter one rather than treating this as ambiguous. Only two genuinely
        # distinct names both appearing in the text counts as real ambiguity.
        specific_names = [
            n for n in matched_names if not any(n != other and n.lower() in other.lower() for other in matched_names)
        ]
        if len(specific_names) > 1:
            project_ambiguous = True
            matched_project_id = None
        elif len(specific_names) == 1:
            matched_project_id = candidate_project_names[specific_names[0]]

    def _result(intent: str, confidence: str, reasoning: str, signals: list[str], ref_idx: int | None = None) -> ContextResolution:
        if gap_is_long and intent in (INTENT_CONTINUATION, INTENT_PRONOUN_REFERENCE) and confidence == CONFIDENCE_HIGH:
            confidence = CONFIDENCE_MEDIUM
            reasoning += " Nedgraderad p.g.a. lång tidslucka sedan senaste meddelandet."
        return ContextResolution(
            intent=intent,
            confidence=confidence,
            reasoning=reasoning,
            referenced_message_index=ref_idx,
            time_gap_seconds=time_gap_seconds,
            project_ambiguous=project_ambiguous,
            matched_project_id=matched_project_id,
            signals=signals,
        )

    if not text:
        return _result(INTENT_UNCERTAIN_REFERENCE, CONFIDENCE_LOW, "Tomt meddelande.", [])

    if marker := _contains_any(text, _EXPLICIT_MEMORY_MARKERS):
        return _result(INTENT_EXPLICIT_MEMORY, CONFIDENCE_HIGH, f"Explicit minnesmarkör: {marker!r}.", [marker])

    if marker := _contains_any(text, _CORRECTION_MARKERS):
        ref_idx = len(history) - 1 if history else None
        return _result(
            INTENT_CORRECTION, CONFIDENCE_HIGH, f"Korrigeringsmarkör: {marker!r}, syftar troligen på föregående meddelande.", [marker], ref_idx
        )

    if marker := _contains_any(text, _NAVIGATION_MARKERS):
        return _result(INTENT_NAVIGATION_QUESTION, CONFIDENCE_HIGH, f"Navigationsfråga: {marker!r}.", [marker])

    if marker := _contains_any(text, _QUESTION_ABOUT_PREVIOUS_MARKERS):
        ref_idx = len(history) - 1 if history else None
        return _result(INTENT_QUESTION_ABOUT_PREVIOUS, CONFIDENCE_HIGH, f"Fråga om tidigare svar: {marker!r}.", [marker], ref_idx)

    if marker := _contains_any(text, _IDEA_MARKERS):
        return _result(INTENT_IDEA_WORTH_SAVING, CONFIDENCE_MEDIUM, f"Idé-markör: {marker!r}.", [marker])

    if marker := _contains_any(text, _CONTINUATION_MARKERS):
        if not history:
            # "nästa" with no prior context at all has nothing to continue — genuinely
            # ambiguous, not a confident continuation of something that doesn't exist.
            return _result(INTENT_UNCERTAIN_REFERENCE, CONFIDENCE_LOW, f"Fortsättningsmarkör {marker!r} men ingen tidigare historik.", [marker])
        ref_idx = len(history) - 1
        return _result(INTENT_CONTINUATION, CONFIDENCE_HIGH, f"Fortsättningsmarkör: {marker!r}.", [marker], ref_idx)

    if _is_pronoun_heavy(text):
        if not history:
            return _result(INTENT_UNCERTAIN_REFERENCE, CONFIDENCE_LOW, "Pronomen utan någon tidigare historik att peka på.", [])
        ref_idx = len(history) - 1
        return _result(
            INTENT_PRONOUN_REFERENCE, CONFIDENCE_MEDIUM, "Kort meddelande dominerat av pronomen, syftar troligen på föregående meddelande.", [], ref_idx
        )

    overlap = _topic_overlap(text, weighted)
    if marker := _contains_any(text, _WORK_COMMAND_MARKERS):
        # A work command can itself be a continuation (high overlap with recent topic) or a
        # fresh, unrelated instruction — the command-ness and the topic-ness are orthogonal,
        # so overlap still decides which one this is, but the intent is WORK_COMMAND either
        # way (an imperative verb is the stronger signal than topic drift).
        confidence = CONFIDENCE_HIGH if overlap >= 0.2 or not history else CONFIDENCE_MEDIUM
        return _result(INTENT_WORK_COMMAND, confidence, f"Arbetskommando ({marker!r}), ämnesöverlapp={overlap:.2f}.", [marker])

    if not history:
        return _result(INTENT_NEW_TOPIC, CONFIDENCE_MEDIUM, "Inget tidigare sammanhang i den här konversationen.", [])

    if overlap >= 0.35:
        return _result(INTENT_CONTINUATION, CONFIDENCE_MEDIUM, f"Betydande ordöverlapp med senaste meddelandena ({overlap:.2f}).", [])

    if overlap <= 0.05:
        return _result(INTENT_NEW_TOPIC, CONFIDENCE_MEDIUM, f"Mycket låg ordöverlapp med senaste meddelandena ({overlap:.2f}) — troligen nytt ämne.", [])

    return _result(INTENT_UNCERTAIN_REFERENCE, CONFIDENCE_LOW, f"Varken tydlig fortsättning eller tydligt nytt ämne (överlapp={overlap:.2f}).", [])
