# MainAI V2 — Sovereign Recovery, Encrypted Life Image, Fast Restore (Stage V2-H)

**Status:** design-only, isolated lane. Does not modify PR #245 (SHA `818dfb732da47901eb5ae06ffdd9c829fe00c4c5`)
or `claude/final-blocker-closeout`. Branched from `claude/det-kommer-mer-879lcm`.
Covers the founder's brief Parts K (reset levels), L (Encrypted Life Image), N (fast restore).

## 0. What this reuses vs. what's genuinely new

This session proved — empirically, with 10 genuinely separate OS subprocesses, zero shared
Python state — that MainAI's V1 authority/continuity state already survives a real process
restart with **zero fidelity decay**: append-only checkpoint chains, correct supersession,
kill-switch state byte-identical across independent fresh-process reads. The mechanism behind
this is `app.workforce.failure.resume_after_restart()` → `record_checkpoint()`, which always
reads the *prior* checkpoint via `latest_checkpoint()` and writes a new one referencing it —
never mutates history in place.

**This proof covers**: same-device, same-database, process-death-and-restart. It does **not**
cover: a brand-new device that has never seen this owner's data before. That's the genuinely
new territory V2-H adds — "Fast Restore" (§3) is real new design, but its *durability
primitive* (durable, append-only, always-reconstructible-from-a-fresh-session state) is the
exact same pattern, generalized from `WorkforceAssignmentCheckpoint` to the full
`INTENT_OBJECT`/memory/settings scope. V2-H does not invent a second continuity mechanism.

## 1. Three reset levels — decision table

| Level | What is destroyed | What survives | Mechanism | Recovery |
|---|---|---|---|---|
| **RESET LIFEAI** | Local session, local UI cache, in-memory workspace state | All durable encrypted data, all keys, software installation | Delete session cookies/tokens, clear local cache directories, do NOT touch the local DEK/KEK material | Re-authenticate; Fast Restore (§3) rehydrates from durable local+remote state; near-instant since keys never left |
| **SECURE RESET** | Local DEK (data encryption key) and all local credential material — **cryptographic erase**: overwrite/discard the key, not the ciphertext | Ciphertext itself (now permanently unreadable without a wrapped-key copy elsewhere, per V2-G's key hierarchy), the *account* (identity, subscription, device-registry entry) | Destroy the local unwrapped DEK immediately (in-memory zeroing + delete the key file); do not attempt to also delete the (now-cryptographically-inert) ciphertext synchronously — that can happen lazily | Only via a full re-provisioning: new device flow (§3) using a *different*, still-valid recovery path (Recovery Capsule per V2-G, or another trusted device's wrapped-key share) — this level assumes the LOCAL key material itself is the thing being discarded (lost device, compromised device, "I want a clean local slate but keep my account") |
| **FULL DEVICE RESET** | Everything SECURE RESET destroys, plus the managed user/device environment itself, where the platform actually allows it (varies: full on a company-managed OS profile, partial-to-none on a personal phone/laptop the platform doesn't let MainAI manage at the OS level) | The account and its durable server-side ciphertext + wrapped keys only | Same cryptographic-erase-first step as SECURE RESET, then platform-specific device wipe API if available; if not available, explicitly report "software-level reset only — the device itself was not wiped, tell the owner why" rather than silently claiming a stronger guarantee than was actually performed | New device required, or the same device fully re-provisioned by the platform |

**Hard rule, stated once so it never drifts per-level:** every level starts with the
**cryptographic erase of local key material**, never with a bulk-delete-then-maybe-forget-a-row
approach. `BACKUP EXISTS != BACKUP RESTORES` and `SECURITY FAILURE -> REDUCE AUTHORITY` both
argue for the same thing here: destroying the key is atomic and immediate; destroying the
ciphertext (which is now inert anyway) can be lazy, best-effort, and is not the thing recovery
correctness depends on.

## 2. Encrypted Life Image

### 2.1 Contents (as specified) and their actual source system

| Content | Actual source (existing or planned) |
|---|---|
| MainAI memory | `app.founder_memory`, `app.inspectable_memory` (already exists) |
| Intent objects | New (V2-I's `INTENT_OBJECT`, this program) |
| Workspace memory | New (V2-I) |
| Local agents | `app.workforce` agent registry (already exists) |
| Knowledge packs | New (V2-F) |
| Settings | New, per-owner config table |
| Documents | `app.models.document` (already exists) |
| Vault | Existing Life Vault egress-controlled storage |
| Local model configuration | New (V2-E's `local_model_requirement` field) |
| App settings / LifeAI configuration | New, minimal |
| OS/user-profile configuration | New, platform-dependent, best-effort only — explicitly the least-guaranteed item on this list; the design must never imply this is portable the same way encrypted data is |

### 2.2 Storage mode matrix

| Mode | Where ciphertext lives | Blind to LifeAI? | Use case |
|---|---|---|---|
| `LOCAL_ONLY` | This device only | N/A (nothing leaves) | Maximum sovereignty, no cross-device, no loss-recovery beyond local backup discipline |
| `LOCAL_BACKUP` | This device + an owner-controlled local backup target (external drive, home server) | Yes | Loss-recovery without any third party |
| `PRIVATE_NAS` | Owner's own network storage | Yes | Multi-device within a household, no cloud dependency |
| `ENCRYPTED_CLOUD_BACKUP` | LifeAI-operated or third-party cloud, **ciphertext + wrapped keys only** | Yes, by construction (§V2-G key hierarchy: server never holds a usable root decryption capability) | Loss-recovery with off-site durability |
| `HYBRID` | Local + encrypted cloud | Yes | Fast local access, cloud as durability backstop |
| `CLOUD_FIRST` | Cloud primary, local is a cache | Yes | Owner explicitly prioritizes cross-device availability over local-first |
| `MULTI_DEVICE` | Same encrypted image, independently key-wrapped per device (V2-G device trust) | Yes | Owner's stated multi-device household/work pattern |

"Blind storage where possible" is not a mode-specific property — it is a property of *every*
mode above `LOCAL_ONLY`, guaranteed by the key hierarchy (V2-G), not by a promise made at the
storage layer. A storage mode design that could theoretically be blind but is implemented in a
way that lets the operator request plaintext is a V2-G violation, not a V2-H one — this
document does not re-derive that guarantee, it depends on it.

## 3. Fast Restore / Progressive Hydration

### 3.1 Priority tiers (as specified, with concrete content)

| Tier | Contents | Approx. why-first |
|---|---|---|
| 0 — Identity | Owner's root identity proof, device trust record for *this* device | Nothing else can be decrypted without this |
| 1 — Security policy | Guardian's current authority ceiling, active kill-switch state (if any), activation gates | MainAI must not act with more authority than the owner last granted, even transiently during restore |
| 2 — Essential memory | Most-recent `FounderMemoryNote`s, active `INTENT_OBJECT`s, current self-model summary | What the owner needs to feel MainAI "remembers who she is" for them, immediately |
| 3 — Active intents | Full `INTENT_OBJECT` records (goal/state/context/blockers/next_actions) for anything not yet completed | Lets the owner resume work in the first conversation turn |
| 4 — Critical settings | Telemetry mode, notification preferences, department/specialist enablement | Needed for MainAI to behave correctly, not needed to *start* being useful |
| 5 — Vault metadata/access structure | Which Vault items exist and their access rules — **not** their content yet | Lets MainAI correctly refuse/allow disclosure decisions before the content itself has synced |
| 6 — Long history/files/media | Everything else | Bulk, safe to stream in background |

### 3.2 Honest progress reporting

`"Background restore 18%"` must be a real fraction, not a fabricated animation. Concretely:
compute it as `bytes_or_records_hydrated / total_bytes_or_records_expected`, where
`total_expected` is known upfront (the manifest — §3.3 — declares total size per tier before
hydration starts, the same way `app.evidence_claim`'s discipline this session required a claim
never assert a state it can't back with a real count). If the total is genuinely unknown at
some point (a tier's true size can't be determined before fetching it), report `unknown` for
that tier's contribution rather than guessing a percentage — `SAID != VERIFIED` applies to
progress bars exactly as it applies to capability claims.

### 3.3 Restore manifest (schema sketch)

```python
@dataclass(frozen=True)
class RestoreManifestTier:
    tier: int
    label: str
    expected_records: int
    expected_bytes: int | None  # None if genuinely unknown ahead of fetch

@dataclass(frozen=True)
class RestoreManifest:
    owner_id: UUID
    device_id: UUID
    generated_at: datetime
    tiers: tuple[RestoreManifestTier, ...]
    recovery_capsule_ref: str  # opaque pointer, not the capsule itself

@dataclass
class RestoreProgress:
    tier: int
    records_hydrated: int
    bytes_hydrated: int | None
    tier_complete: bool

    def fraction(self, manifest_tier: RestoreManifestTier) -> float | None:
        if manifest_tier.expected_records == 0:
            return 1.0
        return self.records_hydrated / manifest_tier.expected_records
```

### 3.4 Worked example: lost laptop, new device

1. Owner buys a new laptop, installs MainAI, opens the orb for the first time.
2. Orb: *"I don't recognize this device yet. Let's verify it's really you."* → Sovereign
   Identity flow (V2-G) — owner authenticates via whichever root method they've enrolled
   (passkey, hardware key, recovery code). **Nothing below this line executes before this
   succeeds** — Tier 0 is a hard gate, not a soft first step.
3. New device gets a fresh device-trust record (V2-G, `DEVICE WAS TRUSTED != DEVICE IS
   TRUSTED` — this device starts with zero prior trust regardless of the owner's identity
   being valid).
4. A tiny encrypted Recovery Capsule is fetched (small enough to be near-instant even on a
   slow connection) — it does not contain the data itself, only the wrapped-key material
   needed to unwrap this device's own copy of the DEK once the owner's root key material has
   unlocked it locally (V2-G key hierarchy).
5. Local key unlock happens **on-device** — the unwrapped DEK never transits the network in
   usable form.
6. Tiers 0–2 hydrate (identity confirmation record, security policy, essential memory) — orb
   becomes conversationally useful: *"MainAI restored. Essential memory ready. Background
   restore 4%."*
7. Tiers 3–6 hydrate in the background with honest, manifest-driven progress reporting (§3.2).
   If the owner asks about something not yet hydrated, MainAI says so plainly ("I don't have
   that yet — still restoring, want me to prioritize it?") rather than guessing or silently
   returning nothing.

## 4. What V2-H deliberately leaves to other stages

- The actual identity-proof mechanism and key-wrapping chain: V2-G.
- What "essential memory" and "active intents" actually contain as live objects: V2-I.
- The specific cryptographic primitives for cross-device key wrapping: explicitly deferred
  per V2-A §7 (implementation-phase decision).
