from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ALGORITHM = "AES-256-GCM-v1"
KEY_BYTES = 32
NONCE_BYTES = 12


class RecallCryptoError(ValueError):
    pass


class SystemKEK:
    """In-memory holder for the Recall system key-encryption key.

    This is intentionally not a dataclass and has no instance __dict__. Generic helpers such
    as dataclasses.asdict() and vars() must fail instead of serializing raw key bytes into
    logs, traces, or debug payloads. Internal crypto code can still access key material through
    the explicit .key property.
    """

    __slots__ = ("version", "_key")

    def __init__(self, version: str, key: bytes) -> None:
        self.version = version
        self._key = bytes(key)

    @property
    def key(self) -> bytes:
        return self._key

    def __repr__(self) -> str:
        return f"SystemKEK(version={self.version!r}, key=<redacted:{len(self._key)} bytes>)"

    __str__ = __repr__

    def safe_metadata(self) -> dict[str, int | str]:
        return {"version": self.version, "key": "<redacted>", "key_bytes": len(self._key)}


def load_system_kek_from_env() -> SystemKEK:
    raw = os.environ.get("PERSONAL_RECALL_SYSTEM_KEK_B64")
    version = os.environ.get("PERSONAL_RECALL_SYSTEM_KEK_VERSION", "env-v1")
    if not raw:
        raise RecallCryptoError("PERSONAL_RECALL_SYSTEM_KEK_B64 is required for production recall encryption")
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise RecallCryptoError("system KEK must be base64") from exc
    if len(key) != KEY_BYTES:
        raise RecallCryptoError("system KEK must be 32 bytes")
    return SystemKEK(version, key)


def generate_key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def encrypt_aead(key: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
    if len(key) != KEY_BYTES:
        raise RecallCryptoError("AEAD key must be 32 bytes")
    nonce = os.urandom(NONCE_BYTES)
    return nonce, AESGCM(key).encrypt(nonce, plaintext, aad)


def decrypt_aead(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    if len(key) != KEY_BYTES:
        raise RecallCryptoError("AEAD key must be 32 bytes")
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise RecallCryptoError("ciphertext integrity check failed") from exc
