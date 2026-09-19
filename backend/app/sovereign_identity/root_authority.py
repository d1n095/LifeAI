"""Root authority proof -- real Ed25519 challenge/signature verification (MainAI V2, Stage
V2-G1).

Concrete replacement for the weak `founder_ack`-style string checks found elsewhere in this
codebase (see docs/mainai_v2/MAINAI_V2_SOVEREIGN_IDENTITY.md §1's
`clear_kill_switch_for_recovery` example): a server-issued, single-use, short-TTL challenge,
signed by the owner's REAL registered Ed25519 root key. Nothing server-side can forge a
valid signature without the owner's private key material, which this module never holds --
only the public key is ever registered here (see types.OwnerRootCapability).

This models the SHAPE a hardware-backed root key would have (asymmetric, private half
never leaves the owner) even though no real hardware integration exists yet (see
hardware.py) -- a caller can generate a real Ed25519 keypair today, register the public
half, and produce genuinely-verified OWNER_ROOT proofs; only the "the private key lives in
a Secure Enclave" part is still a stub.

OWNER != SESSION, LOGIN SUCCESS != ROOT AUTHORITY: evaluate_identity_assertion() only ever
returns OWNER_ROOT when a real signature verifies -- an absent or invalid proof always falls
back to whatever lower level the caller already established through ordinary session/device
checks, never silently escalated.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.sovereign_identity.types import ProofLevel, SessionIdentity

_CHALLENGE_TTL_SECONDS = 120


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def generate_owner_root_keypair() -> tuple[bytes, bytes]:
    """Returns (private_key_bytes, public_key_bytes). The private half is returned ONLY so
    a caller (a test, or a future real hardware-integration layer) can hold it -- this
    module itself never stores a private key anywhere in RootAuthorityState."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    from cryptography.hazmat.primitives import serialization

    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = public_key.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    return private_bytes, public_bytes


def sign_challenge(private_key_bytes: bytes, nonce: bytes) -> bytes:
    """Stands in for a hardware-backed signing operation -- a real Secure Enclave/TPM/
    passkey provider (see hardware.py) would perform this ON-DEVICE and never expose the
    private key bytes to this module at all; this function exists so tests (and any
    software-only fallback path) can exercise the real verification logic below."""
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    return private_key.sign(nonce)


@dataclass(frozen=True)
class RootAuthorityProof:
    """{challenge_id, signature, device_id, timestamp} -- matches
    docs/mainai_v2/MAINAI_V2_SOVEREIGN_IDENTITY.md §1's RootAuthorityProof shape exactly."""

    challenge_id: uuid.UUID
    signature: bytes
    device_id: str | None
    timestamp: datetime = field(default_factory=_utcnow)


@dataclass
class ChallengeRecord:
    """Not underscore-prefixed despite being an internal bookkeeping type -- service.py's
    from_snapshot() needs to reconstruct these across the module boundary, and an
    underscore-prefixed cross-module import reads as accessing something it shouldn't."""

    owner_id: uuid.UUID
    nonce: bytes
    issued_at: datetime
    expires_at: datetime
    used: bool = False


@dataclass
class RootAuthorityState:
    """Holds registered owner root PUBLIC keys and outstanding challenges only -- never a
    private key. Owned by app.sovereign_identity.service.IdentityState (see service.py),
    not meant to be constructed standalone in real use, but kept as its own small dataclass
    for testability."""

    _root_public_keys: dict[str, bytes] = field(default_factory=dict)
    _challenges: dict[str, ChallengeRecord] = field(default_factory=dict)

    def register_root_public_key(self, *, owner_id: uuid.UUID, public_key: bytes) -> None:
        self._root_public_keys[str(owner_id)] = public_key

    def has_registered_root_key(self, owner_id: uuid.UUID) -> bool:
        return str(owner_id) in self._root_public_keys


def issue_challenge(state: RootAuthorityState, *, owner_id: uuid.UUID) -> tuple[uuid.UUID, bytes]:
    """Returns (challenge_id, nonce). `nonce` is what the owner's root key must sign;
    `challenge_id` is the opaque reference used to redeem it. Single-use, short-TTL (120s)."""
    import os

    challenge_id = uuid.uuid4()
    nonce = os.urandom(32)
    now = _utcnow()
    state._challenges[str(challenge_id)] = ChallengeRecord(
        owner_id=owner_id, nonce=nonce, issued_at=now, expires_at=now + timedelta(seconds=_CHALLENGE_TTL_SECONDS)
    )
    return challenge_id, nonce


def verify_root_authority_proof(state: RootAuthorityState, proof: RootAuthorityProof, *, claimed_owner_id: uuid.UUID) -> bool:
    """Real, working verification -- NOT a stub. Returns False (never raises) for: unknown
    challenge_id, already-used challenge (single-use, consumed on first attempt regardless
    of outcome -- prevents brute-force retry against the same challenge), expired challenge,
    owner mismatch, no registered root key for this owner, or a signature that does not
    verify. An unsigned or wrongly-signed assertion is genuinely rejected, never accepted by
    default."""
    record = state._challenges.get(str(proof.challenge_id))
    if record is None:
        return False
    if record.used:
        return False
    record.used = True  # single-use: consumed on this attempt, success or failure alike

    if record.owner_id != claimed_owner_id:
        return False
    if _utcnow() > record.expires_at:
        return False

    public_key_bytes = state._root_public_keys.get(str(claimed_owner_id))
    if public_key_bytes is None:
        return False

    try:
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        public_key.verify(proof.signature, record.nonce)
        return True
    except InvalidSignature:
        return False


def evaluate_identity_assertion(
    state: RootAuthorityState,
    *,
    owner_id: uuid.UUID,
    claimed_level: ProofLevel,
    device_id: str | None,
    proof: RootAuthorityProof | None,
) -> SessionIdentity:
    """OWNER != SESSION: `claimed_level` is whatever ordinary session/device-trust checks
    already established (never higher than TRUSTED_DEVICE in practice) -- this function
    only ever RAISES that to OWNER_ROOT, and only when `proof` is present and genuinely
    verifies. Absent or invalid proof: the caller keeps exactly `claimed_level`, never
    silently escalated to OWNER_CONFIRMED or OWNER_ROOT."""
    proof_level = claimed_level
    if proof is not None and verify_root_authority_proof(state, proof, claimed_owner_id=owner_id):
        proof_level = ProofLevel.OWNER_ROOT
    return SessionIdentity(session_id=uuid.uuid4(), owner_id=owner_id, proof_level=proof_level, device_id=device_id)
