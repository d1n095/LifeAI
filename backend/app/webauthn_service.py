"""Pure WebAuthn/FIDO2 ceremony logic — the authoritative verification core for passkeys.

Deliberately has NO dependency on the DB, FastAPI, or app.config: every relying-party value
(rp_id, rp_name, expected origin(s), user-verification policy) is passed in by the caller
(app/routers/webauthn.py reads them from settings). That keeps this module a small, directly
unit-testable wrapper around the well-reviewed `webauthn` (py_webauthn) library — no hand-rolled
crypto — and lets the round-trip tests exercise it with a software authenticator without standing
up the whole app. See docs/PASSKEYS_WEBAUTHN_DESIGN.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

# Biometric Founder auth: platform authenticators (Touch ID / Android fingerprint), a
# discoverable (resident) credential so no username is typed, and user verification REQUIRED
# (the fingerprint/PIN itself) — not merely user presence.
_UV_REQUIRED = UserVerificationRequirement.REQUIRED


@dataclass
class RegistrationResult:
    credential_id: bytes
    public_key: bytes
    sign_count: int
    aaguid: str | None


@dataclass
class AuthenticationResult:
    new_sign_count: int
    credential_id: bytes


def registration_options_json(
    *,
    rp_id: str,
    rp_name: str,
    user_id: bytes,
    user_name: str,
    user_display_name: str,
    exclude_credential_ids: list[bytes] | None = None,
    require_user_verification: bool = True,
) -> tuple[str, bytes]:
    """Build PublicKeyCredentialCreationOptions. Returns (options_json, challenge_bytes).

    `exclude_credential_ids` are the user's already-registered credentials, so the same
    authenticator cannot be enrolled twice.
    """
    uv = _UV_REQUIRED if require_user_verification else UserVerificationRequirement.PREFERRED
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=rp_name,
        user_id=user_id,
        user_name=user_name,
        user_display_name=user_display_name,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=cid) for cid in (exclude_credential_ids or [])
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=uv,
        ),
    )
    return options_to_json(options), options.challenge


def verify_registration(
    *,
    credential: str | dict,
    expected_challenge: bytes,
    rp_id: str,
    expected_origin: str | list[str],
    require_user_verification: bool = True,
) -> RegistrationResult:
    """Verify an attestation response. Raises on any mismatch (origin, rp_id, challenge,
    signature, or missing user verification when required)."""
    v = verify_registration_response(
        credential=credential,
        expected_challenge=expected_challenge,
        expected_rp_id=rp_id,
        expected_origin=expected_origin,
        require_user_verification=require_user_verification,
    )
    return RegistrationResult(
        credential_id=v.credential_id,
        public_key=v.credential_public_key,
        sign_count=v.sign_count,
        aaguid=getattr(v, "aaguid", None),
    )


def authentication_options_json(
    *,
    rp_id: str,
    allow_credential_ids: list[bytes] | None = None,
    require_user_verification: bool = True,
) -> tuple[str, bytes]:
    """Build PublicKeyCredentialRequestOptions. Returns (options_json, challenge_bytes).

    `allow_credential_ids` empty/None => usernameless (discoverable credential) login.
    """
    uv = _UV_REQUIRED if require_user_verification else UserVerificationRequirement.PREFERRED
    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=cid) for cid in (allow_credential_ids or [])
        ]
        or None,
        user_verification=uv,
    )
    return options_to_json(options), options.challenge


def verify_authentication(
    *,
    credential: str | dict,
    expected_challenge: bytes,
    rp_id: str,
    expected_origin: str | list[str],
    public_key: bytes,
    current_sign_count: int,
    require_user_verification: bool = True,
) -> AuthenticationResult:
    """Verify an assertion. Raises on origin/rp_id/challenge/signature mismatch AND on a
    signature-counter regression (a cloned/replayed authenticator) — the library enforces
    `new_sign_count > current_sign_count` whenever the authenticator uses counters."""
    v = verify_authentication_response(
        credential=credential,
        expected_challenge=expected_challenge,
        expected_rp_id=rp_id,
        expected_origin=expected_origin,
        credential_public_key=public_key,
        credential_current_sign_count=current_sign_count,
        require_user_verification=require_user_verification,
    )
    return AuthenticationResult(new_sign_count=v.new_sign_count, credential_id=v.credential_id)
