"""Round-trip tests for app/webauthn_service.py using a real software authenticator
(soft_webauthn). Exercises the authoritative verification core end-to-end — registration and
authentication ceremonies, signature-counter regression, and origin rejection — without needing
the database or the FastAPI app.

The service module is loaded by file path so these tests run even in a lean venv that doesn't
have the full backend dependency set installed (only `webauthn`, `soft_webauthn`, `pytest`).
"""

import base64
import importlib.util
import json
import os
from pathlib import Path

import pytest
from soft_webauthn import SoftWebauthnDevice

# Normally resolved relative to this file; overridable via env so the pure test can also be run
# from outside backend/tests/ (e.g. a lean venv that cannot load the DB-backed conftest.py).
_SERVICE_PATH = Path(
    os.environ.get(
        "WEBAUTHN_SERVICE_PATH",
        str(Path(__file__).resolve().parents[1] / "app" / "webauthn_service.py"),
    )
)
import sys as _sys

_spec = importlib.util.spec_from_file_location("webauthn_service", _SERVICE_PATH)
svc = importlib.util.module_from_spec(_spec)
# Register before exec so dataclass type-introspection can resolve the module.
_sys.modules["webauthn_service"] = svc
_spec.loader.exec_module(svc)

RP_ID = "localhost"
RP_NAME = "MainAI"
ORIGIN = "https://localhost"
USER_ID = b"founder-user-id-bytes"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _attestation_to_credential(att: dict) -> str:
    return json.dumps(
        {
            "id": _b64(att["rawId"]),
            "rawId": _b64(att["rawId"]),
            "response": {
                "clientDataJSON": _b64(att["response"]["clientDataJSON"]),
                "attestationObject": _b64(att["response"]["attestationObject"]),
            },
            "type": "public-key",
            "clientExtensionResults": {},
        }
    )


def _assertion_to_credential(ast: dict) -> str:
    resp = {
        "authenticatorData": _b64(ast["response"]["authenticatorData"]),
        "clientDataJSON": _b64(ast["response"]["clientDataJSON"]),
        "signature": _b64(ast["response"]["signature"]),
    }
    if ast["response"].get("userHandle"):
        resp["userHandle"] = _b64(ast["response"]["userHandle"])
    return json.dumps(
        {
            "id": _b64(ast["rawId"]),
            "rawId": _b64(ast["rawId"]),
            "response": resp,
            "type": "public-key",
            "clientExtensionResults": {},
        }
    )


# NOTE on user verification: soft_webauthn hard-codes authenticator flags without the UV bit
# (register 0x41 = AT+UP, auth 0x01 = UP), so it can only simulate a user-PRESENT, not a
# user-VERIFIED (biometric) authenticator. The crypto ceremony (attestation/assertion
# signature, challenge, rp_id, origin) is identical regardless of the UV bit, so the round-trip
# tests run with require_user_verification=False. The biometric requirement itself is covered
# separately by test_user_verification_required_rejects_non_uv. A real device / integration test
# exercises the UV-required positive path.
def _enroll(device: SoftWebauthnDevice, *, require_uv: bool = False):
    """Register a passkey on the given software device, return the stored RegistrationResult."""
    _, challenge = svc.registration_options_json(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_id=USER_ID,
        user_name="founder@example.com",
        user_display_name="Founder",
        require_user_verification=require_uv,
    )
    attestation = device.create(
        {
            "publicKey": {
                "rp": {"id": RP_ID, "name": RP_NAME},
                "user": {"id": USER_ID, "name": "founder@example.com", "displayName": "Founder"},
                "challenge": challenge,
                "pubKeyCredParams": [
                    {"alg": -7, "type": "public-key"},
                    {"alg": -257, "type": "public-key"},
                ],
            }
        },
        ORIGIN,
    )
    reg = svc.verify_registration(
        credential=_attestation_to_credential(attestation),
        expected_challenge=challenge,
        rp_id=RP_ID,
        expected_origin=ORIGIN,
        require_user_verification=require_uv,
    )
    return reg


def test_full_registration_and_authentication_roundtrip():
    device = SoftWebauthnDevice()
    reg = _enroll(device)
    assert reg.credential_id and reg.public_key

    # authenticate with the enrolled credential
    _, challenge = svc.authentication_options_json(rp_id=RP_ID, allow_credential_ids=[reg.credential_id])
    assertion = device.get(
        {
            "publicKey": {
                "challenge": challenge,
                "rpId": RP_ID,
                "allowCredentials": [{"id": reg.credential_id, "type": "public-key"}],
                "userVerification": "required",
            }
        },
        ORIGIN,
    )
    auth = svc.verify_authentication(
        credential=_assertion_to_credential(assertion),
        expected_challenge=challenge,
        rp_id=RP_ID,
        expected_origin=ORIGIN,
        public_key=reg.public_key,
        current_sign_count=0,
        require_user_verification=False,
    )
    assert auth.credential_id == reg.credential_id
    assert auth.new_sign_count >= 0


def test_signcount_regression_is_rejected():
    device = SoftWebauthnDevice()
    reg = _enroll(device)
    _, challenge = svc.authentication_options_json(rp_id=RP_ID, allow_credential_ids=[reg.credential_id])
    assertion = device.get(
        {
            "publicKey": {
                "challenge": challenge,
                "rpId": RP_ID,
                "allowCredentials": [{"id": reg.credential_id, "type": "public-key"}],
                "userVerification": "required",
            }
        },
        ORIGIN,
    )
    # Claim the stored counter is already far ahead -> the returned (lower) counter is a replay.
    with pytest.raises(Exception):
        svc.verify_authentication(
            credential=_assertion_to_credential(assertion),
            expected_challenge=challenge,
            rp_id=RP_ID,
            expected_origin=ORIGIN,
            public_key=reg.public_key,
            current_sign_count=10_000,
            require_user_verification=False,
        )


def test_wrong_origin_is_rejected():
    device = SoftWebauthnDevice()
    reg = _enroll(device)
    _, challenge = svc.authentication_options_json(rp_id=RP_ID, allow_credential_ids=[reg.credential_id])
    assertion = device.get(
        {
            "publicKey": {
                "challenge": challenge,
                "rpId": RP_ID,
                "allowCredentials": [{"id": reg.credential_id, "type": "public-key"}],
                "userVerification": "required",
            }
        },
        ORIGIN,
    )
    with pytest.raises(Exception):
        svc.verify_authentication(
            credential=_assertion_to_credential(assertion),
            expected_challenge=challenge,
            rp_id=RP_ID,
            expected_origin="https://evil.example.com",
            public_key=reg.public_key,
            current_sign_count=0,
            require_user_verification=False,
        )


def test_user_verification_required_rejects_non_uv():
    """Biometric gate: an authenticator that only asserts user-PRESENCE (soft_webauthn's
    flags, no UV bit) must be REJECTED when user verification is required — i.e. a passkey
    created/used without the fingerprint/PIN cannot satisfy a UV-required ceremony."""
    device = SoftWebauthnDevice()
    with pytest.raises(Exception):
        _enroll(device, require_uv=True)
