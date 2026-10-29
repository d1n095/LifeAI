# MainAI passkeys / WebAuthn — design & build plan

Secure passkey **enrollment, authentication, management and revocation** for the Founder
account, using platform authenticators (Google Pixel fingerprint / Android passkeys, Mac
Touch ID / macOS passkeys) with **authoritative backend verification**. Branch:
`claude/mainai-passkeys-webauthn` (off `64c07d5b0`).

This document is the contract the migration + router commits implement. **Foundation commit
(this one):** configurable RP settings + this design. **Next commits:** migration 0069 +
models, the router + verification, the frontend UI, and the tests described in §7.

## 1. Standards & library
- W3C WebAuthn / FIDO2, platform authenticators, discoverable credentials (resident keys) so
  the Pixel/Mac can offer the passkey without the user typing an email.
- Server verification via **`py_webauthn`** (PyPI `webauthn`) — `generate_registration_options`,
  `verify_registration_response`, `generate_authentication_options`,
  `verify_authentication_response`. No hand-rolled crypto. Exact pinned version is added in the
  router commit after `pip install` confirms it against `backend/requirements.txt`.

## 2. RP configuration (already added in app/config.py)
`webauthn_rp_id` (registrable domain, no scheme/port), `webauthn_rp_name`, `webauthn_rp_origin`
(full origin(s), comma-separated). **Defaults are localhost-only.** A credential is bound to
`rp_id`; it must NOT be set to the real domain until the Founder approves it, or existing
passkeys would be invalidated when it changes. The router splits `webauthn_rp_origin` on commas
and verifies the assertion origin is one of them.

## 3. Data model (migration 0069, next commit)

### `webauthn_credentials` — RLS owner-scoped (one row per passkey)
```
id              uuid PK default gen_random_uuid()
owner_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE
credential_id   bytea NOT NULL                     -- raw credential id
public_key      bytea NOT NULL                     -- COSE public key
sign_count      bigint NOT NULL DEFAULT 0
transports      varchar(16)[] NOT NULL DEFAULT '{}' -- e.g. internal, hybrid
aaguid          uuid
backup_eligible boolean NOT NULL DEFAULT false
backup_state    boolean NOT NULL DEFAULT false
label           varchar(100) NOT NULL              -- "Pixel 8", "MacBook Touch ID"
created_at      timestamptz NOT NULL DEFAULT now()
last_used_at    timestamptz
CONSTRAINT uq_webauthn_credentials_credential_id UNIQUE (credential_id)
```
RLS (same pattern as mainai_jobs, migration 0068; register in `app/rls.py`):
```
ALTER TABLE webauthn_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE webauthn_credentials FORCE ROW LEVEL SECURITY;
CREATE POLICY webauthn_credentials_isolation ON webauthn_credentials
  USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
  WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
```

### `webauthn_challenges` — single-use, time-boxed, NOT RLS-owner-scoped
Authentication happens **before** login, so there is no `app.current_user_id` yet; an
owner-scoped RLS table would hide the row. So this table is **not** RLS-protected and holds no
sensitive data — only a random challenge, keyed by a random opaque `handle` returned to the
client, with the ceremony type and an optional candidate user. It is read back by `handle` only.
```
handle        varchar(64) PK                 -- random, returned to client, looked up on complete
challenge     bytea NOT NULL                 -- the random WebAuthn challenge
ceremony      varchar(16) NOT NULL CHECK (ceremony IN ('register','authenticate'))
user_id       uuid REFERENCES users(id) ON DELETE CASCADE  -- set for register; null for usernameless auth
created_at    timestamptz NOT NULL DEFAULT now()
expires_at    timestamptz NOT NULL           -- ~5 min
consumed_at   timestamptz                    -- single-use: set on complete, reject if already set
```
Hardening: challenge is 32 random bytes; row deleted/!consumed check on complete; a periodic
cleanup (reuse app/cleanup.py) drops expired rows. `handle` is unguessable (≥256-bit).

## 4. Endpoints (router `app/routers/webauthn.py`, mounted under `/api/auth/webauthn`)
- `POST /register/begin`  — **auth required** (get_current_user). Generates creation options
  with `exclude_credentials` = the user's existing credentials (no double-registration),
  `authenticator_selection` = platform + resident key + user verification "preferred", stores a
  `register` challenge for `user_id`, returns {handle, options}.
- `POST /register/complete` — **auth required + CSRF**. `verify_registration_response` against
  the stored challenge, `rp_id`, and allowed origin(s); on success inserts a credential row
  (owner = current user) with a client-supplied `label`. Consumes the challenge.
- `POST /authenticate/begin` — **unauthenticated**. Generates request options (usernameless /
  discoverable allowed), stores an `authenticate` challenge, returns {handle, options}.
- `POST /authenticate/complete` — **unauthenticated**. Looks up the credential by raw id,
  `verify_authentication_response` (origin + rp_id + challenge + **sign_count regression check**:
  reject if returned counter ≤ stored when the authenticator uses counters). On success updates
  sign_count/last_used_at and **issues the normal session** via the existing
  `_issue_session` / `_session_response` (same cookies, same CSRF, same family logic) — passkeys
  are an additional factor/credential on the existing Founder account, not a parallel auth system.
- `GET  /credentials` — **auth required**. List the user's passkeys (id, label, created_at,
  last_used_at, transports) — never the public key bytes.
- `DELETE /credentials/{id}` — **auth required + CSRF + fresh step-up** (§5). Revokes one passkey.

## 5. Step-up for sensitive operations
Revoking a passkey (and, if the account later supports it, disabling the password factor) is
sensitive: require **fresh step-up** — a recent successful authentication (password OR passkey)
within a short window, or a re-entered password at the time of the call — before the delete
proceeds. Reuse the account module's existing reauthentication pattern rather than inventing a
new one; if none is reusable, require the current password in the DELETE body and verify it.
Enrollment (`register/*`) requires an authenticated session (already a logged-in Founder).

## 6. What is preserved (explicit)
Existing password login, email verification, refresh/rotation, CSRF, owner isolation (RLS),
logout/logout-all, and password recovery are **unchanged**. Passkey login reuses the same
session issuance; no token handling moves to the client. No weakening of any trust boundary.

## 7. Test plan (round-trip, next commit)
Using the backend test harness (`backend/tests/conftest.py`) + a **software authenticator** in
the test (e.g. `soft_webauthn`, or constructed with `cryptography`) so the full ceremony is
exercised, not mocked:
1. register/begin → complete stores a credential (owner-scoped; RLS: another user can't see it).
2. authenticate/begin → complete issues a valid session (cookies + CSRF) for the right user.
3. **sign_count regression** rejected (replay / cloned authenticator).
4. **wrong origin / wrong rp_id** rejected.
5. challenge **single-use** (second complete with same handle rejected) and **expiry** rejected.
6. exclude_credentials prevents re-registering the same authenticator.
7. delete requires step-up; after delete the credential can no longer authenticate.

## 8. Rollout / domain binding
Keep `webauthn_rp_id` = `localhost` until the Founder approves the HTTPS domain
(`docs/PWA_DEPLOY_READINESS.md`). Real passkeys for the Pixel/Mac require that trusted HTTPS
origin (WebAuthn needs a secure context and a matching rp_id) — so end-to-end on-device
enrollment is gated on the same domain decision as the Pixel PWA install. All code is written
domain-agnostically so only config changes when the domain is approved.
```
