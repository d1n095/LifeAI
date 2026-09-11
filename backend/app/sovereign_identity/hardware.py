"""Hardware-backed identity interfaces (MainAI V2, Stage V2-G1).

These are Protocol STUBS only -- no real hardware integration exists or is attempted here.
Every concrete implementation in this module reports HardwareCapabilityStatus.UNAVAILABLE
and raises NotImplementedError if actually invoked, so a caller can never mistake "the
interface exists" for "the hardware is really backing this identity."
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.sovereign_identity.types import HardwareCapabilityStatus


@runtime_checkable
class HardwareIdentityProvider(Protocol):
    """Shape a real future provider (Secure Enclave, TPM, ...) must implement. Deliberately
    minimal: status reporting + a sign() call, nothing this foundation stage needs beyond
    that seam."""

    def capability_status(self) -> HardwareCapabilityStatus: ...

    def sign(self, challenge: bytes) -> bytes: ...


class _UnavailableHardwareProvider:
    """Shared base for every stub below -- always UNAVAILABLE, always raises on sign().
    Concrete subclasses exist only to give each hardware class its own type/name for
    future callers to reference; none of them are functional yet."""

    def capability_status(self) -> HardwareCapabilityStatus:
        return HardwareCapabilityStatus.UNAVAILABLE

    def sign(self, challenge: bytes) -> bytes:
        raise NotImplementedError(
            f"{type(self).__name__} is an interface stub -- no real hardware integration exists yet"
        )


class SecureEnclaveProvider(_UnavailableHardwareProvider):
    """Apple Secure Enclave -- interface stub only."""


class TPMProvider(_UnavailableHardwareProvider):
    """TPM -- interface stub only."""


class HardwareSecurityKeyProvider(_UnavailableHardwareProvider):
    """Hardware security key (e.g. FIDO2/U2F device) -- interface stub only."""


class PasskeyProvider(_UnavailableHardwareProvider):
    """Platform passkey -- interface stub only."""


class OSKeychainProvider(_UnavailableHardwareProvider):
    """OS keychain-backed key -- interface stub only."""


class OfflineRecoveryCodeProvider(_UnavailableHardwareProvider):
    """Offline recovery code -- interface stub only. Unlike the others, a real
    implementation of this one would NOT need hardware -- it is grouped here because it
    plays the same "owner root material" role in the key hierarchy (see
    docs/mainai_v2/MAINAI_V2_SOVEREIGN_IDENTITY.md §1)."""


class TrustedDeviceShareProvider(_UnavailableHardwareProvider):
    """N-of-M trusted-device key share -- interface stub only."""


ALL_HARDWARE_PROVIDER_CLASSES: tuple[type[_UnavailableHardwareProvider], ...] = (
    SecureEnclaveProvider,
    TPMProvider,
    HardwareSecurityKeyProvider,
    PasskeyProvider,
    OSKeychainProvider,
    OfflineRecoveryCodeProvider,
    TrustedDeviceShareProvider,
)
