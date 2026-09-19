"""Key hierarchy crypto primitives (MainAI V2, Stage V2-G2).

Real AES-256-GCM authenticated encryption via the `cryptography` package -- no custom
encryption algorithm anywhere in this module. See
docs/mainai_v2/MAINAI_V2_SOVEREIGN_IDENTITY.md §1 for the DEK/KEK hierarchy this
implements: user data -> encrypted under a DEK -> DEK wrapped by a KEK -> KEK protected by
owner-controlled root/recovery material (the KEK's own protection is V2-H1's Recovery
Capsule -- out of scope for this fork, see types.RecoveryEnvelope's docstring for the seam
left for it).

KeyPurpose, owner_id, and key_version are bound into every AES-GCM call's associated data
(AAD) -- this is what makes purpose/version binding a STRUCTURAL property of GCM's own
authentication (a single flipped label makes the auth tag check fail), not a separate
manual if-check that a future caller could forget or bypass.
"""

from __future__ import annotations

import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.sovereign_identity.types import (
    EncryptionEnvelope,
    KeyMaterialError,
    KeyPurpose,
    WrappedKey,
)

_NONCE_LENGTH_BYTES = 12  # standard/recommended AES-GCM nonce length


def generate_key_material() -> bytes:
    """A fresh, random 256-bit key. Used for both DEKs and KEKs -- the key bytes are the
    same shape; what differs is what wraps/protects them and what purpose they are bound
    to at use time, never the raw key generation itself. Callers must generate a SEPARATE
    key per KeyPurpose -- this function never returns a "shared" key, and nothing in this
    module encourages reuse across purposes (see module docstring)."""
    return AESGCM.generate_key(bit_length=256)


def _aad(*, purpose: KeyPurpose, owner_id: uuid.UUID, key_version: int) -> bytes:
    """Binds purpose + owner + version into every encrypt/decrypt/wrap/unwrap call, always
    derived from the envelope/wrapped-key's OWN declared fields at decrypt time (never from
    a caller-supplied override) -- so relabeling an envelope's purpose or version after the
    fact (e.g. a bug or an attacker claiming a MEMORY_KEY envelope is really VAULT_KEY)
    breaks decryption, because the AAD recomputed from the new label no longer matches what
    was actually authenticated at encryption time."""
    return f"{purpose.value}:{owner_id}:{key_version}".encode("utf-8")


def encrypt_with_dek(
    plaintext: bytes, *, dek: bytes, purpose: KeyPurpose, owner_id: uuid.UUID, key_version: int
) -> EncryptionEnvelope:
    nonce = os.urandom(_NONCE_LENGTH_BYTES)
    aesgcm = AESGCM(dek)
    ciphertext = aesgcm.encrypt(nonce, plaintext, _aad(purpose=purpose, owner_id=owner_id, key_version=key_version))
    return EncryptionEnvelope(
        purpose=purpose, owner_id=owner_id, key_version=key_version, ciphertext=ciphertext, nonce=nonce
    )


def decrypt_with_dek(
    envelope: EncryptionEnvelope, *, dek: bytes, expected_key_version: int | None = None
) -> bytes:
    """Raises KeyMaterialError (never returns wrong plaintext) on: tampered ciphertext,
    wrong key, a relabeled/mismatched purpose or owner, or -- if expected_key_version is
    given -- a stale/mismatched key version. The version check is explicit and happens
    BEFORE the crypto call, so a caller always gets a clear, specific KeyMaterialError
    rather than relying solely on the AAD binding to (also, independently) reject it."""
    if expected_key_version is not None and envelope.key_version != expected_key_version:
        raise KeyMaterialError(
            f"key version mismatch: envelope is version {envelope.key_version}, expected {expected_key_version}"
        )
    aesgcm = AESGCM(dek)
    aad = _aad(purpose=envelope.purpose, owner_id=envelope.owner_id, key_version=envelope.key_version)
    try:
        return aesgcm.decrypt(envelope.nonce, envelope.ciphertext, aad)
    except InvalidTag as exc:
        raise KeyMaterialError(
            "decryption failed -- tampered ciphertext, wrong key, or mismatched purpose/owner/version binding"
        ) from exc


def wrap_key(
    dek: bytes,
    *,
    kek: bytes,
    purpose: KeyPurpose,
    owner_id: uuid.UUID,
    key_version: int,
    wrapped_by_kek_version: int = 1,
) -> WrappedKey:
    """Wraps (encrypts) a DEK under a KEK -- same AEAD primitive, same AAD binding
    discipline, applied one level up the hierarchy."""
    nonce = os.urandom(_NONCE_LENGTH_BYTES)
    aesgcm = AESGCM(kek)
    wrapped = aesgcm.encrypt(nonce, dek, _aad(purpose=purpose, owner_id=owner_id, key_version=key_version))
    return WrappedKey(
        purpose=purpose,
        owner_id=owner_id,
        key_version=key_version,
        wrapped=wrapped,
        nonce=nonce,
        wrapped_by_kek_version=wrapped_by_kek_version,
    )


def unwrap_key(wrapped_key: WrappedKey, *, kek: bytes, expected_key_version: int | None = None) -> bytes:
    """Raises KeyMaterialError on tamper/wrong KEK/relabeled purpose/version mismatch --
    never returns wrong key bytes."""
    if expected_key_version is not None and wrapped_key.key_version != expected_key_version:
        raise KeyMaterialError(
            f"key version mismatch: wrapped key is version {wrapped_key.key_version}, expected {expected_key_version}"
        )
    aesgcm = AESGCM(kek)
    aad = _aad(purpose=wrapped_key.purpose, owner_id=wrapped_key.owner_id, key_version=wrapped_key.key_version)
    try:
        return aesgcm.decrypt(wrapped_key.nonce, wrapped_key.wrapped, aad)
    except InvalidTag as exc:
        raise KeyMaterialError(
            "unwrap failed -- tampered wrapped key, wrong KEK, or mismatched purpose/owner/version binding"
        ) from exc
