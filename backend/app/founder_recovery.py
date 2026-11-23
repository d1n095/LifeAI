"""Secure founder account recovery — email change when the previous inbox is inaccessible.

THREAT MODEL AND PROOF CHAIN
============================
The founder has lost reliable access to the account's original email inbox. Password
reset (app/routers/auth.py /forgot-password) is therefore useless: it emails the OLD
address. Possession of the NEW email address proves NOTHING about the old account — an
attacker who wants to hijack the founder account would love an "email change" endpoint
that trusts a new address alone.

The independent proof of account ownership is therefore a TWO-CHANNEL chain, each channel
worthless without the other:

1. INITIATE — CLI-ONLY, requires BOTH:
   a) shell access on the founder's own deployment (`python -m app.founder_recovery
      initiate ...`). There is deliberately NO HTTP initiation endpoint: the deployment
      itself is the founder's root-of-trust, and a stolen recovery code alone must never
      be enough to start a takeover.
   b) the provisioning-time founder recovery code (high-entropy secret; ONLY its SHA-256
      is stored in configuration, so even a leaked config never yields a usable code).

2. CONFIRM — single-use emailed link to the NEW address (hashed token, short TTL),
   proving control of the new inbox.

Only after BOTH does the change commit — and it commits as a minimal, reversible-by-
no-one-but-the-new-owner mutation: users.id, role, password_hash and every other column
are UNTOUCHED (same row, same primary key — conversations, documents, memories and all
associated data survive by construction). After the commit:

* every session is revoked everywhere (sessions_valid_after bump + refresh-token rows),
* every outstanding recovery/verification/reset token for the account is invalidated,
* an audit row is written (WITHOUT email addresses in the detail — no PII in logs),
* a confirmation email goes to the new address.

FAIL-CLOSED: if FOUNDER_RECOVERY_ENABLED is false, or the recovery code hash is not
configured, or any founder gate fails, nothing happens. This module NEVER changes an
account on its own — it only ever runs when the founder explicitly performs the procedure
below. No code path here touches password hashes beyond not touching them at all.

FOUNDERS: the exact procedure (commands, prerequisites, what changes, what does not) is
in docs/FOUNDER_ACCOUNT_RECOVERY.md — do not improvise.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import sys
import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.audit import record_audit
from app.config import Settings, get_settings
from app.email import send_email
from app.models.email_verification_token import EmailVerificationToken
from app.models.founder_email_change import FounderEmailChangeRequest
from app.models.password_reset_token import PasswordResetToken
from app.models.user import User, UserRole
from app.security import generate_opaque_token, hash_opaque_token
from app.token_revocation import revoke_all_sessions_for_user


class FounderRecoveryError(RuntimeError):
    """Recovery refused before anything changed. `detail` is CLI/UI-safe (no secrets)."""


def _recoverable_user(db: Session, settings: Settings) -> User:
    """The ONLY account this module may ever touch: the one pinned founder row."""
    from app.founder import FOUNDER_USER_ID

    user = db.get(User, FOUNDER_USER_ID)
    if user is None or not user.is_active or user.role != UserRole.founder or user.id != FOUNDER_USER_ID:
        raise FounderRecoveryError("återställning vägras: grundarkontot är inte i ett återställningsbart tillstånd")
    return user


def _assert_recovery_armed(settings: Settings) -> None:
    """Fail-closed arming check — shared by initiate and confirm (a request created while
    armed must not be confirmable after the feature is switched off)."""
    if not settings.founder_recovery_enabled:
        raise FounderRecoveryError("grundar-återställning är avstängd (FOUNDER_RECOVERY_ENABLED=false)")
    if not settings.founder_recovery_code_sha256:
        raise FounderRecoveryError("ingen återställningskod är konfigurerad (FOUNDER_RECOVERY_CODE_SHA256 saknas)")


def _verify_recovery_code(settings: Settings, presented_code: str) -> None:
    if not presented_code:
        raise FounderRecoveryError("återställningskod saknas")
    presented = hashlib.sha256(presented_code.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(presented, settings.founder_recovery_code_sha256):
        raise FounderRecoveryError("återställningskoden är fel")


def _validate_new_email(new_email: str, current_email: str) -> str:
    candidate = new_email.strip().lower()
    if not candidate or "@" not in candidate or len(candidate) > 320 or len(candidate) < 5:
        raise FounderRecoveryError("den nya e-postadressen är inte en giltig adress")
    if candidate == current_email.strip().lower():
        raise FounderRecoveryError("den nya adressen är identisk med den nuvarande")
    return candidate


def initiate_founder_email_change(
    db: Session, *, new_email: str, presented_code: str, settings: Settings | None = None
) -> bool:
    """CLI entry point. Requires shell access + recovery code; emails the verification
    link to the NEW address. Returns whether delivery actually succeeded (the request is
    valid either way; a failed delivery is audited and the CLI prints an honest warning —
    re-initiating invalidates the old request). Raises FounderRecoveryError on any
    refusal. NEVER returns or logs the token — it travels by email only."""
    settings = settings or get_settings()
    _assert_recovery_armed(settings)
    user = _recoverable_user(db, settings)
    _verify_recovery_code(settings, presented_code)
    candidate = _validate_new_email(new_email, user.email)

    # Only ONE outstanding request at a time — a new initiation invalidates prior ones.
    db.query(FounderEmailChangeRequest).filter_by(user_id=user.id, used_at=None).update(
        {"used_at": datetime.utcnow()}
    )
    token_plain = generate_opaque_token()
    db.add(
        FounderEmailChangeRequest(
            user_id=user.id,
            new_email=candidate,
            old_email=user.email,
            token_hash=hash_opaque_token(token_plain),
            expires_at=datetime.utcnow() + timedelta(hours=settings.founder_recovery_token_expire_hours),
        )
    )
    db.commit()

    link = f"{settings.public_app_url}/founder-recovery/confirm?token={token_plain}"
    try:
        delivered = send_email(
            candidate,
            "Bekräfta grundarkonto-återställning – MainAI",
            "Någon med serveråtkomst och återställningskoden har begärt att flytta detta "
            "grundarkonto till den här adressen.\n\n"
            "Om det är du: klicka på länken nedan för att bekräfta. Ändringen sker först då.\n\n"
            f"{link}\n\n"
            f"Länken är giltig i {settings.founder_recovery_token_expire_hours} timme(-ar) och kan bara användas en gång.\n\n"
            "Om det INTE är du: gör inget. Ditt konto ändras inte utan denna bekräftelse — "
            "och kontot kan bara nås av den som har serveråtkomst och återställningskoden, "
            "så överväg att byta återställningskod på servern.",
        )
    except Exception:
        # send_email never raises by contract; this is defense in depth. A delivery-layer
        # crash must NOT leave a zombie outstanding request the operator believes was
        # delivered — invalidate it and surface a clean refusal.
        db.query(FounderEmailChangeRequest).filter_by(user_id=user.id, used_at=None).update(
            {"used_at": datetime.utcnow()}
        )
        db.commit()
        record_audit(db, user_id=user.id, action="founder_email_recovery_delivery_failed", detail="email_change")
        raise FounderRecoveryError("leverans av bekräftelsemejlet misslyckades — kontrollera SMTP och försök igen") from None
    record_audit(
        db,
        user_id=user.id,
        action="founder_email_recovery_initiated",
        detail="email_change delivery_ok" if delivered else "email_change delivery_failed",
    )
    return delivered


def confirm_founder_email_change(db: Session, *, token_plain: str, settings: Settings | None = None) -> str:
    """Redeem the emailed link: commit the email change, revoke every session, invalidate
    every outstanding recovery/verification/reset token, audit. Returns the new email
    (for the confirmation email only — never for logs)."""
    settings = settings or get_settings()
    _assert_recovery_armed(settings)

    request = (
        db.query(FounderEmailChangeRequest)
        .filter_by(token_hash=hash_opaque_token(token_plain))
        .one_or_none()
    )
    if request is None or request.used_at is not None or request.expires_at < datetime.utcnow():
        raise FounderRecoveryError("bekräftelselänken är ogiltig eller har gått ut — begär en ny via servern")

    user = _recoverable_user(db, settings)  # re-verified at confirm time, fail-closed
    if user.email.strip().lower() != request.old_email.strip().lower():
        # The account email changed by some other path since initiation — re-verify.
        raise FounderRecoveryError("kontoadressen har ändrats sedan begäran — begär en ny återställning")

    # THE change: same row, same primary key, same role, same password hash. Only the
    # email identity and its verification stamp move.
    #
    # ATOMIC single-use claim (same fix as refresh rotation): a SELECT-then-UPDATE window
    # would let two concurrent confirmations both pass the used_at check. The conditional
    # UPDATE grants the commit to exactly one caller; the loser sees the neutral refusal.
    claimed = (
        db.query(FounderEmailChangeRequest)
        .filter(FounderEmailChangeRequest.id == request.id, FounderEmailChangeRequest.used_at.is_(None))
        .update({"used_at": datetime.utcnow()}, synchronize_session=False)
    )
    db.commit()
    if claimed != 1:
        raise FounderRecoveryError("bekräftelselänken är ogiltig eller har gått ut — begär en ny via servern")

    user.email = request.new_email
    user.email_verified = True
    user.email_verified_at = datetime.utcnow()

    # Stale credentials for the old identity must all die with it — including any
    # outstanding email-verification tokens (they are recovery-adjacent credentials and
    # a live one after a completed recovery would be an invalidation gap).
    db.query(FounderEmailChangeRequest).filter_by(user_id=user.id, used_at=None).update(
        {"used_at": datetime.utcnow()}
    )
    db.query(PasswordResetToken).filter_by(user_id=user.id, used_at=None).update(
        {"used_at": datetime.utcnow()}
    )
    db.query(EmailVerificationToken).filter_by(user_id=user.id, used_at=None).update(
        {"used_at": datetime.utcnow()}
    )
    db.add(user)
    db.commit()

    # A recovery is exactly a moment when old sessions must not survive.
    revoke_all_sessions_for_user(db, user.id)

    record_audit(db, user_id=user.id, action="founder_email_recovery_completed", detail="email_change")
    try:
        delivered = send_email(
            user.email,
            "Grundarkonto återställt – MainAI",
            "E-postadressen för grundarkontot har nu ändrats till den här adressen och alla "
            "tidigare sessioner har avslutats. Logga in på nytt med ditt lösenord.\n\n"
            "Om du INTE gjorde detta: kontakta din serveradministratör omedelbart — kontot "
            "kan bara ha ändrats av någon med serveråtkomst och återställningskoden.",
        )
    except Exception:  # defense in depth — the change is already committed; never roll it back
        delivered = False
    # The identity change and revocations are committed regardless of whether the
    # confirmation mail went out; a delivery outage is audited, not fatal.
    record_audit(
        db,
        user_id=user.id,
        action="founder_email_recovery_confirmation_mail",
        detail="delivery_ok" if delivered else "delivery_failed",
    )
    return user.email


def _cli() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.founder_recovery",
        description="Initiate a secure founder email recovery (requires server shell access).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_init = sub.add_parser("initiate", help="email a single-use confirmation link to the NEW address")
    p_init.add_argument("--new-email", required=True, help="the replacement address (verified before anything changes)")
    p_init.add_argument("--code", required=True, help="the provisioning-time founder recovery code")
    args = parser.parse_args()

    if args.command != "initiate":  # pragma: no cover — argparse enforces this
        parser.error("unknown command")

    from app.db import SessionLocal

    db = SessionLocal()
    try:
        delivered = initiate_founder_email_change(db, new_email=args.new_email, presented_code=args.code)
    except FounderRecoveryError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()
    if delivered:
        print("OK: en bekräftelselänk har skickats till den nya adressen.")
        print("Ändringen sker först när länken används. Alla sessioner avslutas vid bekräftelse.")
    else:
        print("VARNING: begäran skapades men mejlet skickades INTE (SMTP-utgång misslyckades — se serverloggen).")
        print("Ingen länk är på väg. Åtgärda SMTP och kör initiate igen (den gamla begäran blir då ogiltig).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_cli())
