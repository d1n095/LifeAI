"""MainAI Founder communication policy: accuracy over agreement, no fabricated progress."""

from app.mainai_founder_ops.communication import (
    apply_communication_policy,
    contradiction_notices,
    is_simple_question,
)


def test_simple_question_is_detected():
    assert is_simple_question("Är GO true?")
    assert not is_simple_question("Det här är inget frågetecken")


def test_false_go_claim_is_contradicted():
    notices = contradiction_notices(
        founder_message="First-cycle GO är true och systemet är redo.",
        evidence={"first_cycle_go": False},
    )
    assert notices
    assert "GO är false" in notices[0]


def test_apply_policy_disagrees_when_founder_claims_go():
    text = apply_communication_policy(
        "Jag håller med, ni är good to go.",
        founder_message="Är GO true? Allt är operationellt.",
        evidence={"first_cycle_go": False},
    )
    assert text.startswith("Nej. First-cycle GO är false")
    assert "bakgrund" not in text.lower() or "inget bakgrundsjobb" in text.lower()


def test_unverified_execution_claim_is_stripped():
    text = apply_communication_policy(
        "Jag arbetar med det i bakgrunden. Sedan kan vi prata om nästa steg.",
        founder_message="Kan du fixa det?",
        evidence={"first_cycle_go": False},
    )
    assert "arbetar med det i bakgrunden" not in text.lower()
    assert "inget bakgrundsjobb" in text.lower() or "hela mitt arbete" in text.lower()


def test_redundant_identical_reply_is_collapsed():
    prior = "Databasen är källan till sanning. Tilldelning är inte körning."
    text = apply_communication_policy(
        prior,
        founder_message="Vad är status?",
        prior_assistant=prior,
        evidence={"first_cycle_go": False},
    )
    assert text == "Jag har inget nytt att tillägga."


def test_simple_question_strips_filler_agreement():
    text = apply_communication_policy(
        "Självklart! STOP är inaktiv.",
        founder_message="Är STOP inaktiv?",
        evidence={"first_cycle_go": False},
    )
    assert not text.lower().startswith("självklart")
    assert "STOP är inaktiv" in text


def test_verified_claim_without_evidence_is_rejected():
    text = apply_communication_policy(
        "Arbetet ser bra ut.",
        founder_message="All work is independently verified.",
        evidence={"independently_verified": False, "first_cycle_go": False},
    )
    assert "Självständig verifiering kräver" in text


def test_github_write_claim_is_rejected_when_disabled():
    text = apply_communication_policy(
        "Inget skrivs just nu.",
        founder_message="GitHub-skrivning är på.",
        evidence={"github_write_enabled": False, "first_cycle_go": False},
    )
    assert "GitHub-skrivning" in text
    assert text.startswith("Nej.")
