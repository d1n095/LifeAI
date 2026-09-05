"""Guardian policy: versioning, signing, rollback protection (MainAI V2, Stage V2-B).

Standalone, isolated, NOT imported by any production runtime path.

HONESTY NOTE (per the build directive): the signature check below uses `hmac`/`sha256`
with a shared-secret key. This is a REAL, working integrity check -- it genuinely detects
tampering and is exercised by real tests, not a stub that always returns True. It is
deliberately NOT hardware-backed (no Secure Enclave / TPM / hardware security key
integration exists yet). Production V2-G work should replace the shared-secret HMAC with
an asymmetric, hardware-backed signature (the public verification API below,
`verify_policy_signature`, is written so that swap doesn't change any caller).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass, field

from app.guardian.types import ContainmentScope


class PolicyRejected(Exception):
    """Raised when a policy fails version or signature checks. Never silently ignored."""


@dataclass(frozen=True)
class GuardianPolicy:
    """The durable rule set Guardian's decision functions consult. Versioned; rollback
    protected (service.py's GuardianState only ever accepts a strictly newer version)."""

    version: int
    owner_id: str  # str(uuid.UUID) -- kept as str for stable, order-independent hashing
    authority_ceilings: dict[str, str] = field(default_factory=dict)  # scope.value -> max_risk_level
    known_self_diagnostic_reasons: tuple[str, ...] = (
        "prompt_injection_suspected",
        "unexpected_tool_output",
        "integrity_check_failed",
        "runtime_anomaly",
    )
    max_self_isolation_requests_per_window: int = 3
    self_isolation_window_seconds: int = 60

    def canonical_bytes(self) -> bytes:
        """Deterministic serialization for hashing/signing -- sorted keys, no whitespace
        ambiguity, so the same policy always produces the same signature input regardless
        of dict insertion order."""
        payload = asdict(self)
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_policy(policy: GuardianPolicy, *, secret_key: bytes) -> str:
    """Real HMAC-SHA256 signature over the policy's canonical bytes. Not hardware-backed
    (see module docstring) -- but genuinely verifies integrity today."""
    mac = hmac.new(secret_key, policy.canonical_bytes(), hashlib.sha256)
    return mac.hexdigest()


def verify_policy_signature(policy: GuardianPolicy, signature: str, *, secret_key: bytes) -> bool:
    """Constant-time comparison (hmac.compare_digest) -- deliberately not `==`, to avoid a
    timing side-channel on the signature check itself."""
    expected = sign_policy(policy, secret_key=secret_key)
    return hmac.compare_digest(expected, signature)


def require_valid_policy(
    policy: GuardianPolicy,
    signature: str,
    *,
    secret_key: bytes,
    current_active_version: int | None,
) -> None:
    """Raises PolicyRejected for either a stale version or a bad signature. Rollback
    protection: a policy claiming a version <= the currently active one is rejected
    outright, even before checking the signature -- an attacker replaying an old,
    validly-signed policy is exactly the attack this guards against."""
    if current_active_version is not None and policy.version <= current_active_version:
        raise PolicyRejected(
            f"policy version {policy.version} is not newer than active version {current_active_version}"
        )
    if not verify_policy_signature(policy, signature, secret_key=secret_key):
        raise PolicyRejected("policy signature does not match canonical policy bytes")


def default_policy(owner_id: str) -> GuardianPolicy:
    """Guardian's own hard-coded defaults -- never settable by MainAI. version=1, no
    ceilings raised above the conservative baseline (empty dict -> callers must get an
    explicit, owner-signed ceiling before anything beyond the baseline is ALLOWed)."""
    return GuardianPolicy(version=1, owner_id=owner_id, authority_ceilings={})
