"""MainAI V2 -- Privacy Boundary Engine foundation tests (Stage V2-C).

Standalone package, no DB required -- app.privacy_boundary is pure in-memory logic, not wired
into any production runtime path. These tests exercise the real pipeline/sanitize/advocate
code directly, not mocks of it (except test #10, which deliberately spies on classify() to
prove OFF mode never calls it).
"""

from __future__ import annotations

import dataclasses
import uuid
from unittest.mock import patch

import pytest

from app.privacy_boundary import (
    CategoryAggregateRequest,
    DataClassification,
    DisclosureRequestRejected,
    OutboundPurpose,
    PrivacyBoundaryError,
    RawLocalSignal,
    ReceiptLog,
    TelemetryMode,
    evaluate_advocate_request,
    run_privacy_pipeline,
    sanitize_text,
)


def _owner() -> uuid.UUID:
    return uuid.uuid4()


def _signal(domain="debt_resolution", classification=DataClassification.PRIVATE) -> RawLocalSignal:
    return RawLocalSignal(owner_id=_owner(), domain=domain, raw_content="unused by the pipeline", classification=classification)


# --- 1/2: raw conversation / raw document text attempted as a "domain" category -> blocked ---


def test_raw_conversation_text_blocked():
    log = ReceiptLog()
    signal = _signal(domain="My landlord Andersson is trying to evict me over the balcony plants")
    result = run_privacy_pipeline(
        signal, mode=TelemetryMode.LEARNING, purpose=OutboundPurpose.LEARNING_SIGNAL, module="m", software_version="1.0",
        cohort_size_lookup=lambda d, s: 10, receipt_log=log,
    )
    assert result is None
    receipts = log.all()
    assert receipts[-1].decision == "denied"


def test_raw_document_content_blocked():
    log = ReceiptLog()
    signal = _signal(domain="Please see attached document at /Users/anna/Documents/lease_agreement_final.pdf for details")
    result = run_privacy_pipeline(
        signal, mode=TelemetryMode.LEARNING, purpose=OutboundPurpose.LEARNING_SIGNAL, module="m", software_version="1.0",
        cohort_size_lookup=lambda d, s: 10, receipt_log=log,
    )
    assert result is None


# --- 3: Vault-classified secret -> blocked, in every mode ---


def test_vault_classification_blocked_in_every_mode():
    for mode, purpose in (
        (TelemetryMode.LEARNING, OutboundPurpose.LEARNING_SIGNAL),
        (TelemetryMode.RESEARCH_OPT_IN, OutboundPurpose.RESEARCH_OPT_IN),
    ):
        signal = _signal(domain="finance", classification=DataClassification.VAULT)
        result = run_privacy_pipeline(
            signal, mode=mode, purpose=purpose, module="m", software_version="1.0", cohort_size_lookup=lambda d, s: 10,
        )
        assert result is None, f"VAULT must block at {mode}"


def test_secret_and_never_egress_classifications_blocked():
    for classification in (DataClassification.SECRET, DataClassification.NEVER_EGRESS):
        signal = _signal(classification=classification)
        result = run_privacy_pipeline(
            signal, mode=TelemetryMode.RESEARCH_OPT_IN, purpose=OutboundPurpose.RESEARCH_OPT_IN,
            module="m", software_version="1.0", cohort_size_lookup=lambda d, s: 10,
        )
        assert result is None


# --- 4: API key/token-shaped string -> blocked ---


def test_token_shaped_value_in_category_field_blocked():
    log = ReceiptLog()
    signal = _signal()
    result = run_privacy_pipeline(
        signal, mode=TelemetryMode.LEARNING, purpose=OutboundPurpose.LEARNING_SIGNAL, module="m", software_version="1.0",
        failure_class="opaque_test_token_9f8a7b6c5d4e3f2a1b0c9d8e",  # token-shaped
        cohort_size_lookup=lambda d, s: 10, receipt_log=log,
    )
    assert result is None
    assert log.all()[-1].decision == "denied"


# --- 5: email/name/path inside a crash trace -> stripped, remainder allowed through ---


def test_crash_trace_pii_stripped_remainder_preserved():
    trace = (
        'Traceback (most recent call last):\n'
        '  File "/Users/anna_svensson/project/app.py", line 42, in handler\n'
        "    raise ValueError('bad input from anna@example.com')\n"
        "ValueError: bad input from anna@example.com"
    )
    sanitized, categories = sanitize_text(trace)
    assert "anna@example.com" not in sanitized
    assert "anna_svensson" not in sanitized
    assert "stack_trace" in categories or "file_path" in categories
    assert "email" in categories
    # the non-PII structural remainder (that it's a ValueError) is still present
    assert "ValueError" in sanitized


# --- 6: exact debt amount -> generalized/removed, never passed as an exact number ---


def test_exact_money_value_removed_not_passed():
    log = ReceiptLog()
    signal = _signal()
    result = run_privacy_pipeline(
        signal, mode=TelemetryMode.LEARNING, purpose=OutboundPurpose.LEARNING_SIGNAL, module="m", software_version="1.0",
        resolution_class="paid 4500 kr",  # exact amount smuggled into a category field
        cohort_size_lookup=lambda d, s: 10, receipt_log=log,
    )
    assert result is None
    sanitized, categories = sanitize_text("the debt was exactly 4500 kr")
    assert "4500 kr" not in sanitized
    assert "exact_money_value" in categories


# --- 7: stable device identifier -> removed ---


def test_device_identifier_removed():
    device_id = "550e8400-e29b-41d4-a716-446655440000"
    sanitized, categories = sanitize_text(f"device {device_id} connected")
    assert device_id not in sanitized
    assert "uuid_or_session_id" in categories

    log = ReceiptLog()
    signal = _signal()
    result = run_privacy_pipeline(
        signal, mode=TelemetryMode.LEARNING, purpose=OutboundPurpose.LEARNING_SIGNAL, module="m", software_version="1.0",
        missing_capability_class=device_id, cohort_size_lookup=lambda d, s: 10, receipt_log=log,
    )
    assert result is None


# --- 8: unknown/unrecognized personal-looking blob -> blocked, fail closed ---


def test_unknown_personal_blob_blocked():
    signal = _signal(domain="the whole complicated situation with my sister about the inheritance that we discussed")
    result = run_privacy_pipeline(
        signal, mode=TelemetryMode.LEARNING, purpose=OutboundPurpose.LEARNING_SIGNAL, module="m", software_version="1.0",
        cohort_size_lookup=lambda d, s: 10,
    )
    assert result is None


# --- 9: safe generic failure signal -> allowed ---


def test_safe_generic_failure_signal_allowed():
    signal = _signal(domain="debt_resolution")
    result = run_privacy_pipeline(
        signal, mode=TelemetryMode.LEARNING, purpose=OutboundPurpose.LEARNING_SIGNAL, module="finance_dept",
        software_version="1.2.3", skill="rule_application", failure_class="knowledge_gap",
        resolution_class="escalated", success=False, cohort_size_lookup=lambda d, s: 10,
    )
    assert result is not None
    assert result.payload["domain"] == "debt_resolution"
    assert result.payload["skill"] == "rule_application"


# --- 10: OFF mode -> classify() is never called (spy proof) ---


def test_off_mode_never_constructs_classified_signal():
    signal = _signal()
    with patch("app.privacy_boundary.pipeline.classify") as spy:
        result = run_privacy_pipeline(
            signal, mode=TelemetryMode.OFF, purpose=OutboundPurpose.LEARNING_SIGNAL, module="m", software_version="1.0",
        )
    assert result is None
    spy.assert_not_called()


def test_off_mode_records_denial_with_no_optional_content():
    log = ReceiptLog()
    signal = _signal()
    run_privacy_pipeline(signal, mode=TelemetryMode.OFF, purpose=OutboundPurpose.LEARNING_SIGNAL, module="m", software_version="1.0", receipt_log=log)
    receipt = log.all()[-1]
    assert receipt.decision == "denied"
    assert receipt.output_schema == "none"


# --- 11: MINIMAL mode -> LEARNING_SIGNAL purpose blocked ---


def test_minimal_mode_blocks_learning_signal_purpose():
    signal = _signal()
    result = run_privacy_pipeline(
        signal, mode=TelemetryMode.MINIMAL, purpose=OutboundPurpose.LEARNING_SIGNAL, module="m", software_version="1.0",
        skill="rule_application", cohort_size_lookup=lambda d, s: 10,
    )
    assert result is None


def test_minimal_mode_allows_system_health():
    signal = _signal()
    result = run_privacy_pipeline(
        signal, mode=TelemetryMode.MINIMAL, purpose=OutboundPurpose.SYSTEM_HEALTH, module="m", software_version="1.0",
        cohort_size_lookup=lambda d, s: 10,
    )
    assert result is not None


# --- 12: LEARNING mode -> properly generalized signal allowed ---


def test_learning_mode_allows_generalized_signal():
    signal = _signal(domain="security.malware_response")
    result = run_privacy_pipeline(
        signal, mode=TelemetryMode.LEARNING, purpose=OutboundPurpose.SECURITY_SIGNAL, module="sentinel",
        software_version="1.0", skill="signature_match", resolution_class="contained_locally",
        success=True, cohort_size_lookup=lambda d, s: 50,
    )
    assert result is not None
    assert result.payload["success"] is True


# --- 13: central request for a raw file -> structurally rejected ---


def test_request_for_raw_file_structurally_impossible():
    with pytest.raises(TypeError):
        CategoryAggregateRequest(  # type: ignore[call-arg]
            domain="finance", skill=None, purpose=OutboundPurpose.LEARNING_SIGNAL,
            file_id="secret-file-123",  # not a real field -- must be a TypeError, not silently accepted
        )


def test_request_with_smuggled_raw_identifier_in_domain_rejected():
    bad_request = CategoryAggregateRequest(domain="conversation:550e8400-e29b-41d4-a716-446655440000", skill=None, purpose=OutboundPurpose.LEARNING_SIGNAL)
    with pytest.raises(DisclosureRequestRejected):
        evaluate_advocate_request(bad_request, local_mode=TelemetryMode.LEARNING, allowed_purposes_for_mode=frozenset({OutboundPurpose.LEARNING_SIGNAL}))


# --- 14: malicious server request cannot override local privacy mode ---


def test_request_cannot_carry_a_mode_override_field():
    request = CategoryAggregateRequest(domain="finance", skill=None, purpose=OutboundPurpose.LEARNING_SIGNAL)
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.local_privacy_mode_override = TelemetryMode.RESEARCH_OPT_IN  # type: ignore[attr-defined]
    assert not hasattr(request, "local_privacy_mode_override")


def test_request_honored_only_against_local_mode_never_request_supplied_mode():
    # A request for LEARNING_SIGNAL purpose, evaluated against a local mode that only
    # allows SYSTEM_HEALTH -- must be rejected regardless of anything on the request.
    request = CategoryAggregateRequest(domain="finance", skill=None, purpose=OutboundPurpose.LEARNING_SIGNAL)
    with pytest.raises(DisclosureRequestRejected):
        evaluate_advocate_request(request, local_mode=TelemetryMode.MINIMAL, allowed_purposes_for_mode=frozenset({OutboundPurpose.SYSTEM_HEALTH}))


# --- 15: allowed-egress receipt never contains the raw secret ---


def test_receipt_never_contains_raw_secret():
    log = ReceiptLog()
    secret_marker = "UNIQUE_SECRET_MARKER_9f8a7b6c"
    signal = RawLocalSignal(owner_id=_owner(), domain="debt_resolution", raw_content=f"the secret is {secret_marker}", classification=DataClassification.PRIVATE)
    result = run_privacy_pipeline(
        signal, mode=TelemetryMode.LEARNING, purpose=OutboundPurpose.LEARNING_SIGNAL, module="m", software_version="1.0",
        skill="rule_application", cohort_size_lookup=lambda d, s: 10, receipt_log=log,
    )
    assert result is not None
    receipt = log.all()[-1]
    # PrivacyReceipt has no field capable of holding raw content at all -- assert the secret
    # marker is not a substring of the receipt's own serialized form either, as a second,
    # independent proof beyond "the type doesn't have the field."
    assert secret_marker not in repr(receipt)
    assert secret_marker not in str(dataclasses.asdict(receipt) if dataclasses.is_dataclass(receipt) else receipt)


# --- cross-cutting: fail-closed on any stage exception ---


def test_pipeline_fails_closed_on_unexpected_exception():
    log = ReceiptLog()
    signal = _signal()

    def _boom(d, s):
        raise RuntimeError("cohort service unreachable")

    # Even though this raises inside generalize()'s cohort lookup, generalize() itself
    # catches it and treats it as "unknown -> suppress" (not a pipeline-level failure) --
    # confirm this resolves to a SUPPRESSED but still-recorded outcome, not a raw leak.
    result = run_privacy_pipeline(
        signal, mode=TelemetryMode.LEARNING, purpose=OutboundPurpose.LEARNING_SIGNAL, module="m", software_version="1.0",
        skill="rule_application", cohort_size_lookup=_boom, receipt_log=log,
    )
    assert result is not None
    assert result.payload == {"outcome_class": None, "agent_version": None}  # suppressed shape, domain/skill dropped


def test_pipeline_fails_closed_when_classify_itself_raises_unexpectedly():
    log = ReceiptLog()
    signal = _signal()
    with patch("app.privacy_boundary.pipeline.classify", side_effect=RuntimeError("boom")):
        result = run_privacy_pipeline(
            signal, mode=TelemetryMode.LEARNING, purpose=OutboundPurpose.LEARNING_SIGNAL, module="m", software_version="1.0", receipt_log=log,
        )
    assert result is None
    assert log.all()[-1].decision == "denied"
    assert "failed closed" in log.all()[-1].reason
