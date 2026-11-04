"""Encryption seam for Recall snapshots; production crypto intentionally remains external."""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Protocol


class RecallSnapshotProtectionError(ValueError):
    pass


class RecallSnapshotProtector(Protocol):
    def seal(self, plaintext: bytes, *, owner_id: str, associated_data: bytes) -> bytes: ...
    def open(self, sealed: bytes, *, owner_id: str, associated_data: bytes) -> bytes: ...
    def key_reference(self, *, owner_id: str) -> str: ...
    def version(self) -> str: ...


class DeterministicTestSnapshotProtector:
    """Deterministic tamper-detecting fake. NOT encryption and forbidden in production."""

    is_test_only = True

    def __init__(self, test_key: bytes = b"personal-recall-test-protector-v1"):
        if len(test_key) < 16:
            raise ValueError("test key is too short")
        self._key = test_key

    def seal(self, plaintext: bytes, *, owner_id: str, associated_data: bytes) -> bytes:
        payload = owner_id.encode() + b"\0" + associated_data + b"\0" + plaintext
        tag = hmac.new(self._key, payload, hashlib.sha256).digest()
        return b"TEST-ONLY-V1." + base64.urlsafe_b64encode(tag + plaintext)

    def open(self, sealed: bytes, *, owner_id: str, associated_data: bytes) -> bytes:
        if not sealed.startswith(b"TEST-ONLY-V1."):
            raise RecallSnapshotProtectionError("unsupported snapshot protector version")
        try:
            decoded = base64.b64decode(sealed.split(b".", 1)[1], altchars=b"-_", validate=True)
        except ValueError as exc:
            raise RecallSnapshotProtectionError("malformed protected snapshot") from exc
        tag, plaintext = decoded[:32], decoded[32:]
        payload = owner_id.encode() + b"\0" + associated_data + b"\0" + plaintext
        expected = hmac.new(self._key, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise RecallSnapshotProtectionError("snapshot owner/context mismatch or tampering")
        return plaintext

    def key_reference(self, *, owner_id: str) -> str:
        return f"test-only://personal-recall/{owner_id}"

    def version(self) -> str:
        return "test-only-v1"
