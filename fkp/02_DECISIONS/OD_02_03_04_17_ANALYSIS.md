# Analysis: OD-02, OD-03, OD-04, OD-17
**Purpose:** Per the founder's explicit 2026-07-19 instruction, this document investigates alternatives and compares risk/cost/dependencies for four open decisions, **without implementing any of them and without asking the founder to pick before this analysis existed.** Each section ends with a recommendation — a recommendation is not a decision; the founder still has the final say and nothing here has been coded.
**Status:** Analysis only. No code, migration, or schema change accompanies this document.
**Method:** Alternatives compared on risk (what happens if we get it wrong / cost of being wrong), cost (implementation + operational), and dependencies (what it blocks or is blocked by), grounded in LifeAI's actual current code (verified directly in this repository, not assumed from FKP v1.0's stale material).

---

## OD-02 — Will founder access require passkey/MFA before production use?

**Current state:** Login is Argon2id-hashed password + strong password policy (`backend/app/password_policy.py`) + HttpOnly/Secure/SameSite session cookies + CSRF token + IP-based rate limiting (Redis-backed) + session revocation (`sessions_valid_after`). No second factor exists.

**Why this matters more than usual:** There is exactly one account. It is not "an admin among many" — compromising it compromises the entire product, including (once built) the Founder Vault. A publicly reachable login endpoint at a known URL is a plausible credential-stuffing/phishing target the moment this deploys.

| Alternative | Risk if chosen | Implementation cost | Dependencies |
|---|---|---|---|
| **A. No MFA (status quo)** | Single point of total failure on password compromise; mitigated but not eliminated by current password/session hardening | None | None |
| **B. TOTP (authenticator app)** | Low residual risk; standard, well-audited pattern (`pyotp`-class libraries); recovery via backup codes is a solved UX pattern | Low — one new encrypted-secret column, one enrollment/verification flow, backup-code generation | Needs a place to store the shared secret at rest (encrypted) — no new infra |
| **C. WebAuthn/Passkey (FIDO2)** | Lowest residual risk, phishing-resistant; but device loss without a second registered authenticator = lockout | Medium-high — browser WebAuthn ceremony, credential public-key storage table, attestation handling | Needs a credible recovery path before enabling, or it creates its own single point of failure (device loss) |
| **D. Email OTP as 2nd factor** | Weak: password reset already goes through the same email account, so this doesn't add an independent factor — email compromise already bypasses password auth today | Very low — reuses existing Strato SMTP wiring | None |

**Recommendation:** TOTP (option B). It gives a real security uplift for low implementation cost and mature, boring, well-understood tooling — appropriate for a single irreplaceable account. WebAuthn/Passkey (C) is the stronger long-term target but its recovery-path problem needs its own design pass first; reasonable as a *later* upgrade, not the first move. Email OTP (D) is not recommended — it doesn't add an independent factor given the current reset-via-email flow. **Not a blocker for merging the current founder-only-launch branch** — today's password/session hardening already exceeds typical baseline — but should be sequenced early, before the account holds anything more valuable than login access itself (i.e., before knowledge import). See `09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md`.

---

## OD-03 — Where will private Founder Vault originals be stored and encrypted?

**Current state:** The Founder Vault does not exist in LifeAI yet — no table, no storage bucket, no encryption key management. This is a green-field design question, not a migration question.

| Alternative | Risk if chosen | Implementation cost | Dependencies |
|---|---|---|---|
| **A. Structured data in Postgres, application-level envelope encryption** | Key custody is the whole risk: if the master key (held in a Render env var) leaks, the vault leaks | Low-medium — encrypt-before-write/decrypt-after-read helpers, one master key in `render.yaml` (`sync: false`, generated, never committed) | None beyond what's already provisioned (Supabase Postgres) |
| **B. Large binaries in object storage (e.g. Supabase Storage free tier), DB holds only encrypted-at-upload pointers/hashes** | Same key-custody risk as A, plus a second system to keep consistent with the DB (orphaned blobs, dangling pointers) | Medium — upload/download plumbing, consistency checks | Needs A's key-management design decided first |
| **C. Client-side/passphrase-derived encryption (founder holds the only key)** | Highest security **if** the founder never loses the passphrase; but there is no recovery path — a forgotten passphrase means permanent, unrecoverable data loss, and no UI for this exists | High — key-derivation UX, no server-side recovery possible by design | None technically, but operationally risky for a single irreplaceable owner |
| **D. Third-party hosted vault/secrets service** | Adds an external paid dependency and a new attack surface/ToS dependency | Likely violates the 0 SEK/month MVP constraint (D-07) unless a genuine free tier exists | New vendor onboarding, likely conflicts with D-07/D-08 |

**Recommendation:** A two-layer combination of A + B — small structured/metadata records envelope-encrypted in the existing Postgres, larger original documents encrypted-before-upload into the already-available Supabase Storage free tier, referenced by pointer from Postgres. This costs nothing new infrastructure-wise (reuses D-11's already-provisioned services) and keeps the key-custody problem in one place (a single Render-managed master key) rather than two. Do **not** adopt client-side-only encryption (C) for v1 — the unrecoverable-data-loss failure mode is a bad fit for a single founder with no backup identity, and no recovery UX exists to make it safe. Rule out D on cost grounds per D-07. This is explicitly not implemented; it should be built only once Founder Vault becomes an active work item (see dependency staircase, not before knowledge import's simpler document storage).

---

## OD-04 — What is the exact ownership/RLS migration for `documents`, `projects`, and `tasks`?

**Current state verified in this repository:** `documents`, `projects`, and their related tables have no explicit per-row `owner_id`/scope column today — they implicitly belong to whichever single account exists (the founder). LifeAI's actual RLS/isolation mechanism (`backend/app/rls.py`) is its own SQLAlchemy-session-scoped pattern, not Supabase's `auth.uid()`-based Postgres RLS policies described in `05_SECURITY_AND_TRUST/SECURITY_REQUIREMENTS.md` — that document's specific RLS pattern is written for a different stack and needs translation before it can be applied literally to LifeAI, per `03_ARCHITECTURE/TARGET_ARCHITECTURE.md`'s caveat.

| Alternative | Risk if chosen | Implementation cost | Dependencies |
|---|---|---|---|
| **A. Do nothing now, defer until a second identity exists** | Low risk *today* (only one account, one owner); becomes a retrofit/backfill migration later, at exactly the moment (Phase 4/7) when the system has the most data and the least tolerance for migration mistakes | None now | Defers, doesn't eliminate, the eventual cost |
| **B. Add an explicit `owner_id` column now, defaulting to the founder's fixed UUID, no new scope taxonomy yet** | Very low risk — additive migration on currently small tables; no behavior change since there's still one owner | Low — one migration, one default value, no new enforcement logic needed yet | None; sets up but doesn't require Phase 4 work |
| **C. Build the full data-zone/scope taxonomy now** (founder-private / UserAI-private / project-shared / org-shared / public, per `SECURITY_REQUIREMENTS.md`) | Premature — this taxonomy was designed against Supabase's RLS pattern and hasn't been translated to LifeAI's actual isolation mechanism; building it now risks locking in an untranslated design | High — new enums, new enforcement paths, new tests, for a use case (multiple users) that doesn't exist yet | Blocked on translating the SECURITY_REQUIREMENTS RLS pattern to `app/rls.py`'s actual mechanism first |

**Recommendation:** Option B — add the `owner_id` column (defaulting to the founder) at the point these tables are first meaningfully populated, which in practice is the knowledge-import work in the dependency staircase below. This is cheap insurance: it avoids a painful backfill later without speculatively building the full multi-tenant taxonomy (option C) before there is a second identity to test it against. This does not block today's founder-only launch and is not urgent in isolation — sequence it as a prerequisite for Phase 4 (Founder UserAI), not before.

---

## OD-17 — BIGINT micro-units vs NUMERIC(18,6) for cost fields

**Correction to the question as originally framed:** FKP v1.0 framed this as a green-field schema choice. It is not — LifeAI already has a working, tested cost-tracking column: `backend/app/models/usage.py`'s `UsageLog.cost_usd`, defined as `Numeric(14, 6)`, nullable (NULL means "pricing unknown," never a fabricated 0). The real question is whether to change this existing column, not which type a new one should use.

| Alternative | Risk if chosen | Implementation cost | Dependencies |
|---|---|---|---|
| **A. Keep `NUMERIC(14,6)` as-is (status quo)** | None — Postgres `NUMERIC` is exact decimal arithmetic, not floating point, so the usual rationale for integer micro-units (avoiding float rounding) doesn't apply here. `NUMERIC(14,6)` holds values up to ~99,999,999.999999 per row — for a single provider call's cost, that headroom is not a realistic constraint | None | None |
| **B. Widen to `NUMERIC(18,6)`** | No risk, but no measurable benefit for the current per-row use case either | Small migration for zero functional gain today | None |
| **C. Migrate to BIGINT micro-units** | Touches a live, tested, working column for a benefit (float-rounding avoidance) that doesn't apply to `NUMERIC` in the first place — this is exactly the kind of unmotivated rewrite the project's own non-negotiables prohibit ("no restart or rewrite of working modules without documented technical evidence," `01_FOUNDER_VISION/NON_NEGOTIABLE_PRINCIPLES.md`) | Real migration + provider-pricing code changes, for no offsetting gain | None, but no justification either |

**Recommendation:** Keep `NUMERIC(14,6)` unchanged. There is no technical evidence motivating a change to the existing, working, tested field. If a future *cumulative* field is added (e.g., a cached "total spend this billing period" column, which could plausibly need more headroom than a single call's cost), `NUMERIC(18,6)` would be the reasonable choice for that new column specifically — that is a narrower, evidence-based recommendation for a not-yet-built field, not a reason to touch `usage_log.cost_usd` today. This effectively closes OD-17 against the current schema.

---

## Summary table

| ID | Recommendation | Blocking current merge? | Where it belongs in the sequence |
|----|----------------|--------------------------|-----------------------------------|
| OD-02 | TOTP now, WebAuthn/passkey later | No | Early — before knowledge import, see `09_DEPENDENCY_STAIRCASE/` |
| OD-03 | Envelope-encrypted Postgres + Supabase Storage, no client-side-only encryption | No — Founder Vault doesn't exist yet | When Founder Vault becomes an active work item |
| OD-04 | Add `owner_id` column now (cheap), defer full scope taxonomy | No | At knowledge-import time, prerequisite for Phase 4 |
| OD-17 | No change — `NUMERIC(14,6)` is already correct | No — closed, not a blocker | N/A |

None of the above has been implemented. This document exists so the founder can decide with full context, not to make the decision on the founder's behalf.
