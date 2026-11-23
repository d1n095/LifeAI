# FOUNDER ACCOUNT RECOVERY — PROCEDURE FOR FOUNDER APPROVAL

**Status: IMPLEMENTED, NOT EXECUTED.** No account, email, credential or database record
has been changed. Nothing below runs until the founder explicitly approves and performs it.
`first_cycle_go=false` remains in force.

## Situation

The founder account (`app/founder.py` `FOUNDER_USER_ID`, the single pinned founder row)
currently authenticates against an email address hosted on the old STRATO account whose
inbox can no longer be reliably accessed. Password reset cannot help: `/forgot-password`
emails the OLD address by design (it must — that is what makes it safe).

## Replacement address (pending founder approval + verification)

`dennis.torildsson@proton.me`

## Security properties of the implemented recovery

1. **Same account, same identity.** The recovery moves ONLY the email identity on the
   EXISTING row: `users.id`, `role`, `password_hash`, conversations, documents, memories,
   and every other associated record survive untouched (same primary key). A second
   founder account is never created; the row is pinned to `FOUNDER_USER_ID` and any other
   row — even one marked role=founder — is refused.
2. **Independent proof of ownership, two channels.** Neither channel alone can change
   anything:
   - **Initiate (CLI-only):** `python -m app.founder_recovery initiate --new-email ... --code ...`
     requires server shell access (the founder's deployment is the root of trust) AND the
     provisioning-time recovery code. There is NO HTTP initiation endpoint. The code is
     stored ONLY as a SHA-256 hash in configuration — even a leaked config never yields a
     usable code.
   - **Confirm (emailed link):** a single-use, hashed, short-TTL token is emailed to the
     NEW address. Possession of the new inbox alone proves nothing and changes nothing;
     possession of the code alone changes nothing.
3. **New address verified before commit.** The email change commits ONLY when the emailed
   link is redeemed. If the account email changed by any other path after initiation, the
   confirmation refuses.
4. **Post-recovery revocation.** On success: every session is revoked on every device
   (`sessions_valid_after` bump + all refresh-token rows), every outstanding recovery,
   verification and password-reset token for the account is invalidated, an audit row is
   written (no email addresses in audit details), and a confirmation email goes to the
   new address.
5. **Fail-closed.** Everything is disabled unless `FOUNDER_RECOVERY_ENABLED=true` AND
   `FOUNDER_RECOVERY_CODE_SHA256` is configured. Re-checks happen at confirm time, so a
   link minted while armed cannot complete after disarm.
6. **No secret exposure.** No password hashes in responses, logs or audit rows; the
   plaintext token exists only in the emailed link; HTTP refusals are neutral.

## Exact procedure (requires founder authorization)

```bash
# 0. (once) Provision the recovery code — choose a high-entropy secret, then store ONLY
#    its SHA-256 in the deployment's environment:
printf '%s' '<CHOOSE-A-LONG-RANDOM-CODE>' | sha256sum   # → FOUNDER_RECOVERY_CODE_SHA256
#    In .env / deployment config:
#    FOUNDER_RECOVERY_ENABLED=true
#    FOUNDER_RECOVERY_CODE_SHA256=<hash from the command above>
#    FOUNDER_RECOVERY_TOKEN_EXPIRE_HOURS=1

# 1. Apply migrations (one new table: founder_email_change_requests):
#    alembic upgrade head   (revision 0069_founder_email_recovery)

# 2. From the server shell, initiate:
python -m app.founder_recovery initiate \
  --new-email dennis.torildsson@proton.me \
  --code '<the code whose SHA-256 you stored>'

# 3. In the NEW inbox (dennis.torildsson@proton.me), open the confirmation link.
#    The change commits, all sessions are revoked, old tokens die, audit is written.

# 4. Log in again with the EXISTING password. Optionally rotate the recovery code
#    afterwards by replacing FOUNDER_RECOVERY_CODE_SHA256.
```

## Delivery-truth semantics (verified by tests)

`send_email` never raises and now reports whether the message actually went out. The CLI
prints an honest result: `OK: ... skickats` on delivery, or a WARNING that no link is on
its way (fix SMTP, run initiate again — the old request is invalidated automatically). A
delivery outage is audited as `founder_email_recovery_delivery_failed`; the confirmation
mail after a completed recovery is audited as `founder_email_recovery_confirmation_mail`
and can never roll the change back. If the delivery layer crashed despite its contract,
initiate leaves NO outstanding request behind (clean refusal, no zombie state).

## Code vs token semantics

The RECOVERY CODE is a static provisioning secret and deliberately does NOT expire — it
is rotated by replacing `FOUNDER_RECOVERY_CODE_SHA256` in the deployment environment.
What expires is the single-use TOKEN (1 h TTL), enforced at confirm time.

## Rollback / abort

Before confirmation: do nothing — the request expires (1 h TTL) or is superseded by the
next initiation; the account is untouched. After confirmation: the email is changed; the
password is unchanged, so re-initiating recovery to a corrected address is possible the
same way.

## Regression coverage (15 tests, all passing)

`backend/tests/backend/test_founder_recovery.py` — fail-closed arming, wrong code,
unconfigured code, non-pinned founder row, identical email, single outstanding request,
hashed-token-only storage, mail to the new address only, re-initiation invalidation,
happy-path identity preservation (id/role/password unchanged) + full session/token
revocation, unknown/replayed/expired tokens, disarm-after-initiation, mid-flight email
change refusal, neutral HTTP 400s, secret-free response bodies.
