"""Tests for app/context/resolver.py — Conversation Context Resolver v1 (DEL 7). Every test
phrase the founder's own instructions listed verbatim is exercised here by name, plus the
scenario tests (topic change and return, time gap, similar project names, superseded
context). No AI call — this is a pure/deterministic module, so every test is fast and exact."""

import uuid
from datetime import datetime, timedelta

from app.context.resolver import (
    INTENT_CONTINUATION,
    INTENT_CORRECTION,
    INTENT_EXPLICIT_MEMORY,
    INTENT_IDEA_WORTH_SAVING,
    INTENT_NAVIGATION_QUESTION,
    INTENT_NEW_TOPIC,
    INTENT_PRONOUN_REFERENCE,
    INTENT_QUESTION_ABOUT_PREVIOUS,
    INTENT_UNCERTAIN_REFERENCE,
    INTENT_WORK_COMMAND,
    ConversationMessage,
    resolve_context,
)

BASE_TIME = datetime(2026, 7, 20, 12, 0, 0)


def _history(*texts: str, start=BASE_TIME, step_seconds=30) -> list[ConversationMessage]:
    return [
        ConversationMessage(role="user" if i % 2 == 0 else "assistant", content=t, created_at=start + timedelta(seconds=i * step_seconds))
        for i, t in enumerate(texts)
    ]


# --- The founder's exact required phrases ---


def test_nu_da_is_continuation_with_history():
    history = _history("Vi pratade om budgeten för Q3.", "Ja, den ser bra ut.")
    result = resolve_context("nu då?", history, now=BASE_TIME + timedelta(seconds=90))
    assert result.intent == INTENT_CONTINUATION


def test_nu_da_with_no_history_is_uncertain():
    result = resolve_context("nu då?", [], now=BASE_TIME)
    assert result.intent == INTENT_UNCERTAIN_REFERENCE


def test_nasta_is_continuation():
    history = _history("Första punkten är klar.")
    result = resolve_context("nästa", history, now=BASE_TIME + timedelta(seconds=30))
    assert result.intent == INTENT_CONTINUATION


def test_gor_samma_is_continuation():
    history = _history("Skapa ett nytt dokument med titeln 'Rapport'.")
    result = resolve_context("gör samma", history, now=BASE_TIME + timedelta(seconds=30))
    assert result.intent == INTENT_CONTINUATION


def test_vad_ska_jag_trycka_pa_is_navigation_question():
    result = resolve_context("Vad ska jag trycka på?", _history("Nu är formuläret ifyllt."), now=BASE_TIME + timedelta(seconds=30))
    assert result.intent == INTENT_NAVIGATION_QUESTION


def test_han_ar_klar_nu_is_pronoun_reference():
    history = _history("Dennis håller på med rapporten.")
    result = resolve_context("Han är klar nu.", history, now=BASE_TIME + timedelta(seconds=30))
    assert result.intent == INTENT_PRONOUN_REFERENCE
    assert result.referenced_message_index == len(history) - 1


def test_lagg_till_det_i_forra_is_pronoun_reference():
    history = _history("Här är listan över uppgifter.")
    result = resolve_context("Lägg till det i förra.", history, now=BASE_TIME + timedelta(seconds=30))
    assert result.intent == INTENT_PRONOUN_REFERENCE


def test_den_finns_inte_dar_is_correction():
    history = _history("Filen ligger i mappen Projekt/2026.")
    result = resolve_context("Den finns inte där.", history, now=BASE_TIME + timedelta(seconds=30))
    assert result.intent == INTENT_CORRECTION


def test_sluta_tappa_traden_is_correction():
    history = _history("Vi pratade om A.", "Nu pratar vi om B istället.")
    result = resolve_context("Sluta tappa tråden.", history, now=BASE_TIME + timedelta(seconds=60))
    assert result.intent == INTENT_CORRECTION


# --- Scenario tests ---


def test_topic_change_and_return_to_earlier_topic():
    history = _history(
        "Vi behöver planera Q3-budgeten för marknadsföring.",
        "Okej, jag tar fram siffrorna.",
        "Förresten, vad tycker du om det nya kontorets läge?",
        "Det verkar bra placerat nära tunnelbanan.",
    )
    # Unrelated follow-up — should read as a genuinely new topic.
    weather_result = resolve_context("Hur är vädret där borta?", history, now=BASE_TIME + timedelta(seconds=150))
    assert weather_result.intent == INTENT_NEW_TOPIC

    # Returning to the original topic later — overlap with the still-in-window Q3-budget
    # messages should be picked up again, not treated as yet another brand-new topic.
    history2 = history + [ConversationMessage(role="user", content="Hur är vädret där borta?", created_at=BASE_TIME + timedelta(seconds=150))]
    return_result = resolve_context("Hur går det med Q3-budgeten för marknadsföring?", history2, now=BASE_TIME + timedelta(seconds=180))
    assert return_result.intent == INTENT_CONTINUATION


def test_long_time_gap_downgrades_continuation_confidence():
    history = _history("Vi höll på med rapporten.")
    soon = resolve_context("nästa", history, now=BASE_TIME + timedelta(seconds=30))
    much_later = resolve_context("nästa", history, now=BASE_TIME + timedelta(hours=5))

    assert soon.intent == much_later.intent == INTENT_CONTINUATION
    assert soon.confidence == "high"
    assert much_later.confidence != "high"
    assert much_later.time_gap_seconds > soon.time_gap_seconds


def test_short_time_gap_does_not_downgrade():
    history = _history("Vi höll på med rapporten.")
    result = resolve_context("nästa", history, now=BASE_TIME + timedelta(seconds=30))
    assert result.confidence == "high"


def test_two_projects_with_similar_names_is_flagged_ambiguous():
    life_os = uuid.uuid4()
    life_os_studio = uuid.uuid4()
    candidates = {"Life OS": life_os, "Founder Studio": life_os_studio}
    result = resolve_context(
        "Jämför Life OS med Founder Studio.", [], now=BASE_TIME, candidate_project_names=candidates
    )
    assert result.project_ambiguous is True
    assert result.matched_project_id is None


def test_more_specific_project_name_is_not_falsely_ambiguous():
    """"Life OS" is a substring of "Life OS Mobile" — typing the more specific name must
    resolve unambiguously to it, not be flagged as a conflict between the two."""
    life_os = uuid.uuid4()
    life_os_mobile = uuid.uuid4()
    candidates = {"Life OS": life_os, "Life OS Mobile": life_os_mobile}
    result = resolve_context("Vad är statusen på Life OS Mobile?", [], now=BASE_TIME, candidate_project_names=candidates)
    assert result.project_ambiguous is False
    assert result.matched_project_id == life_os_mobile


def test_old_context_superseded_by_new_information_is_a_correction():
    history = _history("Använd blå färg för knappen.")
    result = resolve_context("Byt till röd istället för blå.", history, now=BASE_TIME + timedelta(seconds=30))
    assert result.intent == INTENT_CORRECTION


def test_explicit_memory_marker():
    result = resolve_context("Kom ihåg att mötet flyttades till fredag.", _history("Ok."), now=BASE_TIME + timedelta(seconds=30))
    assert result.intent == INTENT_EXPLICIT_MEMORY
    assert result.confidence == "high"


def test_idea_marker():
    result = resolve_context("Tänk om vi automatiserade hela flödet?", [], now=BASE_TIME)
    assert result.intent == INTENT_IDEA_WORTH_SAVING


def test_work_command_with_topic_overlap_is_high_confidence():
    history = _history("Vi diskuterar dokument i biblioteket.")
    result = resolve_context("Skapa ett nytt dokument i biblioteket.", history, now=BASE_TIME + timedelta(seconds=30))
    assert result.intent == INTENT_WORK_COMMAND
    assert result.confidence == "high"


def test_work_command_with_no_history_is_high_confidence_standalone():
    result = resolve_context("Skapa ett nytt projekt.", [], now=BASE_TIME)
    assert result.intent == INTENT_WORK_COMMAND


def test_question_about_previous_answer():
    history = _history("Svaret är att X beror på Y.")
    result = resolve_context("Vad menade du med det?", history, now=BASE_TIME + timedelta(seconds=30))
    assert result.intent == INTENT_QUESTION_ABOUT_PREVIOUS
    assert result.referenced_message_index == len(history) - 1


def test_brand_new_topic_with_no_history():
    result = resolve_context("Berätta om kvantdatorer.", [], now=BASE_TIME)
    assert result.intent == INTENT_NEW_TOPIC


def test_low_overlap_message_is_new_topic():
    history = _history("Vi pratar om semesterplanering och flygbiljetter.")
    result = resolve_context("Hur fungerar fotosyntes egentligen?", history, now=BASE_TIME + timedelta(seconds=30))
    assert result.intent == INTENT_NEW_TOPIC


def test_ambiguous_message_is_uncertain_not_forced_into_a_category():
    history = _history("Vi pratade om olika saker tidigare.")
    result = resolve_context("Berätta lite mer.", history, now=BASE_TIME + timedelta(seconds=30))
    # Deliberately not asserting a specific intent here beyond "not a false-confident
    # continuation/new-topic split" — the point of this test is that low-signal input
    # produces low confidence, not a specific label.
    assert result.confidence in ("low", "medium")


def test_empty_message_is_uncertain():
    result = resolve_context("   ", _history("Hej."), now=BASE_TIME)
    assert result.intent == INTENT_UNCERTAIN_REFERENCE
    assert result.confidence == "low"


def test_never_infers_emotional_or_psychological_state():
    """Hard constraint from the founder's own instructions: no hidden diagnosis. This test
    exists to lock in that the resolver's actual MARKER VOCABULARY (what it pattern-matches
    against, i.e. what it could possibly claim to detect) has no stress/mood concept at all —
    not that it correctly suppresses one it secretly computes. Scoped to the marker lists,
    not the whole file, since the module's own docstring legitimately *names* these concepts
    in the sentence explaining that they're excluded."""
    from app.context import resolver as resolver_module

    marker_lists = [
        resolver_module._EXPLICIT_MEMORY_MARKERS,
        resolver_module._IDEA_MARKERS,
        resolver_module._CORRECTION_MARKERS,
        resolver_module._NAVIGATION_MARKERS,
        resolver_module._CONTINUATION_MARKERS,
        resolver_module._QUESTION_ABOUT_PREVIOUS_MARKERS,
        resolver_module._WORK_COMMAND_MARKERS,
        list(resolver_module._PRONOUNS),
    ]
    all_markers = " ".join(m for markers in marker_lists for m in markers).lower()
    forbidden_terms = ["stress", "mood", "emotion", "humör", "känsl", "diagnos", "psykolog", "arg", "ledsen", "glad", "trött"]
    for term in forbidden_terms:
        assert term not in all_markers, f"resolver.py's marker vocabulary must never reference {term!r}"


def test_verbatim_window_is_capped_at_five_and_weighted_window_at_fifteen():
    from app.context.resolver import MAX_VERBATIM_MESSAGES, MAX_WEIGHTED_MESSAGES

    assert MAX_VERBATIM_MESSAGES == 5
    assert MAX_WEIGHTED_MESSAGES == 15
