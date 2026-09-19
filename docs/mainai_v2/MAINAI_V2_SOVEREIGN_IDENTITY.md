# MainAI V2 — Sovereign Identity (Stage V2-G + Part O device trust)

**Status:** design-only, isolated lane (branch `claude/mainai-v2-workforce-identity`). Does
not touch PR #245 / candidate SHA `818dfb732da47901eb5ae06ffdd9c829fe00c4c5`. References
`docs/mainai_v2/MAINAI_V2_ARCHITECTURE_MAP.md` (V2-A) for vocabulary, trust boundaries
(Guardian referenced only as the V2-A §2 concept — Guardian's own internals are a separate
document written in parallel, not duplicated here), and the constitution.

## 0. The boundary: today's session auth vs. sovereign identity

This codebase already has a real, threat-modeled session layer (`docs/AUTH_THREAT_MODEL.md`):
HttpOnly access token (15 min JWT) + HttpOnly refresh token (14 days, opaque, rotated with
family-wide revocation on reuse detection) + a JS-readable CSRF value delivered once in the
response body. This is genuinely good session-security design — V2 does not replace it.

**The founder's own distinction draws the exact boundary:** `SESSION ACCESS != ROOT
AUTHORITY`. Today's cookie session answers "is this request from a browser that recently
logged in" — a question about *reachability*, renewed automatically, silently, every 15
minutes. Sovereign Identity answers a categorically different question: "does the entity
performing THIS SPECIFIC high-stakes action actually possess the owner's own root capability"
— a question that must NEVER be answerable by session-cookie possession alone, no matter how
well-protected that cookie is (an XSS bug, a compromised browser extension, or a coerced
device unlock all defeat cookie-based session auth without defeating a hardware-backed root
key).

**What stays exactly as-is:** login, ordinary API access, RLS scoping (`app.current_user_id`),
refresh rotation, CSRF. Sovereign Identity does not touch any of this.

**What's genuinely new territory:** anything the constitution marks as requiring more than
`LOGIN SUCCESS`: clearing a kill-switch stop, approving a `founder_approval_ref` for HIGH-RISK
work (`app.workforce.verification`'s existing policy field — today satisfied by an opaque
string; V2 must make it satisfiable ONLY by a real root-authority act), Guardian recovery
entry, device revocation, and (V2-H) any Sovereign Recovery action.

## 1. Key hierarchy

```
                    ┌─────────────────────────┐
                    │   OWNER ROOT MATERIAL    │   never leaves owner control;
                    │  (Secure Enclave / TPM / │   never transmitted to server
                    │   hardware key / passkey │   in usable form
                    │   / offline recovery code)│
                    └────────────┬─────────────┘
                                 │ unlocks
                                 ▼
                    ┌─────────────────────────┐
                    │           KEK            │   Key-Encryption-Key,
                    │   (root capability)       │   one per owner
                    └────────────┬─────────────┘
                                 │ wraps
                                 ▼
                    ┌─────────────────────────┐
                    │           DEK             │   Data-Encryption-Key,
                    │  (one or more, per data   │   rotatable independently
                    │   class: memory / vault /  │   of KEK
                    │   documents / device sync) │
                    └────────────┬─────────────┘
                                 │ encrypts
                                 ▼
                    ┌─────────────────────────┐
                    │        ACTUAL DATA        │
                    └─────────────────────────┘
```

**What the server holds:** encrypted DATA (ciphertext), the DEK **wrapped by** the KEK
(useless without the KEK), and minimized operational metadata (which DEK version applies to
which record, for routing — never the key material itself). **What the server never holds:**
the KEK itself, or owner root material in any form that could reconstruct it. This is the
literal meaning of `CLOUD STORAGE != CLOUD TRUST` — the server can be fully compromised and an
attacker still cannot decrypt.

**Recovery Capsule (optional, owner opt-in):** a small, separately-encrypted object holding
the *wrapped* KEK (wrapped again, by owner-chosen recovery material — e.g. a printed offline
code, or N-of-M trusted-device shares). The Capsule **controls access to** the wrapped KEK; it
cannot itself decrypt anything without the recovery material being supplied. Losing the
Capsule without recovery material means losing data — this is a real, disclosed trade-off, not
a hidden backdoor (`RECOVERY != BACKDOOR`): there is no path that lets LifeAI (the company)
reconstruct a KEK from server-held material alone, Capsule included.

**Root authority verification for a high-stakes act (concrete replacement for the weak
`founder_ack` string check found broken tonight):**

Today: `clear_kill_switch_for_recovery(*, founder_ack: str)` — accepts any non-empty string.

V2 replacement, concretely:
```python
def clear_kill_switch_for_recovery(
    db: Session, *, owner_id: UUID, root_authority_proof: RootAuthorityProof,
) -> KillSwitchState:
    """root_authority_proof must be a fresh (short-TTL, single-use) signature produced by
    the owner's actual root key material (hardware key / passkey / Secure Enclave), over a
    server-issued challenge that names the EXACT action being authorized (scope_key, current
    epoch, timestamp) -- not a bare token. Verified against the owner's registered public
    key, never against a shared secret the server could itself forge."""
```
`RootAuthorityProof` = `{challenge_id, signature, device_id, timestamp}`. The server issues
`challenge_id` fresh per attempt (single-use, short TTL — same replay-protection shape as
tonight's already-built `clear_request_id`/`expected_sequence` mechanism for the kill-switch
epoch, just backed by a real signature instead of a caller-supplied ack string). This closes
the exact gap found tonight: a self-constructed string can no longer satisfy this check,
because nothing server-side can forge a valid signature over a challenge without the owner's
actual private key material.

## 2. Device trust

`DEVICE WAS TRUSTED != DEVICE IS TRUSTED` — trust is a current, revocable state per device,
not a one-time onboarding fact.

```python
@dataclass
class DeviceTrustRecord:
    device_id: UUID
    owner_id: UUID
    trust_state: str        # "trusted" | "revoked" | "frozen" | "unknown"
    public_key_fingerprint: str
    last_key_wrap_at: datetime | None
    revoked_at: datetime | None
    revocation_reason: str | None
```

**Revocation flow** (all four steps in one transaction, matching this session's own
"blocker aggregation must not drop earlier reasons" discipline — a partial revoke that
disables sync but forgets to invalidate a lease is a real, dangerous half-state):
1. `trust_state -> "revoked"`.
2. Freeze sync for this device (stop accepting/serving wrapped-key material to it).
3. Invalidate any active session/lease this device currently holds (reuses the existing
   refresh-token-family revocation mechanism — a device revocation IS a forced full family
   revocation for that device's sessions, not a new parallel mechanism).
4. Prevent future key wrapping to this device's public key (remove it from the owner's
   registered-device set entirely — a revoked device cannot be un-revoked by re-presenting
   its old key, it must re-onboard as a new device with a fresh keypair).
Encrypted recovery material for the owner's data must remain intact via OTHER trusted devices
or the Recovery Capsule — revoking one device never destroys data, only that device's access.

**Remote lock vs. remote destructive wipe — genuinely different authorization bars:**
- **Remote lock** (stop the device from decrypting further, block new sessions): authorizable
  from ANY other trusted device via ordinary session auth + a confirmation step. Reversible.
- **Remote destructive wipe** (cryptographic erase — destroy the DEK/KEK wrapping on that
  device so its local ciphertext becomes permanently unusable): requires the SAME
  `RootAuthorityProof` mechanism as §1's high-stakes act, not ordinary session auth. This is
  irreversible and must never be triggerable by a compromised session alone — exactly the
  `SESSION ACCESS != ROOT AUTHORITY` line, applied concretely.

## 3. New-device onboarding — worked flow

1. Owner authenticates on the NEW device via ordinary session auth (today's existing login —
   unchanged) — this establishes *reachability*, not root authority yet.
2. New device generates its own local keypair (private key never leaves the device — Secure
   Enclave/TPM-backed where available, software-protected fallback otherwise, but always
   device-local generation, never server-generated-and-transmitted).
3. New device's public key is registered server-side as `trust_state="unknown"` (visible, not
   yet trusted for key-wrapping).
4. Owner proves root authority using an ALREADY-trusted device or recovery material (§1's
   `RootAuthorityProof` mechanism) to explicitly approve the new device — this is the one
   step that requires more than ordinary login, by design.
5. On approval: the already-trusted device (or a server-mediated flow that never sees
   plaintext key material) wraps the owner's DEKs for the new device's public key.
   `DeviceTrustRecord.trust_state -> "trusted"`, `last_key_wrap_at` set.
6. New device can now unwrap its DEK copies locally and begin progressive hydration (see
   V2-H, written separately, for the fast-restore priority order).

**Failure mode, explicit:** if step 4 is skipped or bypassed (e.g. a bug lets a device reach
`trusted` via session auth alone), that is a `SESSION ACCESS != ROOT AUTHORITY` violation and
must be treated with the same severity as this session's own kill-switch authority-widening
race (PR #243, the single most severe finding of tonight's campaign) — this is the same class
of bug, one layer up: authority granted without the caller's own genuine, fresh assertion.
