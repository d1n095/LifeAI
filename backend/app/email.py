import logging
import smtplib
import uuid
from email.message import EmailMessage
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger("mainai.email")
settings = get_settings()


def send_email(to: str, subject: str, body_text: str) -> None:
    """Sends via SMTP if configured; otherwise falls back to a local dev-only outbox — the
    same graceful-degradation pattern used for unconfigured AI providers elsewhere in this
    app.

    Never raises: a bounced or misconfigured mail server must not turn into a 500 for the
    caller (registration/reset must still succeed — the account and token already exist
    regardless of whether delivery worked), but a failure IS logged loudly so it's not
    silently swallowed in production.

    Verification and password-reset links carry a high-entropy, one-time credential in
    plain text (by necessity — that's how an email link works). It must never end up in the
    application's regular log stream, which is typically shipped to a log aggregator with
    far broader read-access and far longer retention than an actual mailbox would have. See
    docs/AUTH_THREAT_MODEL.md and docs/OPERATIONS.md."""
    if settings.smtp_host:
        _send_via_smtp(to, subject, body_text)
        return

    # No SMTP configured. Never write the token/body to the application logger. Only write
    # it anywhere at all if the operator has explicitly opted in via dev_mail_outbox_dir
    # (defaults to unset) — and even then, refuse outright in anything claiming to be a
    # production environment, in case that setting was left on by mistake.
    logger.warning(
        "SMTP ej konfigurerat — mail till %s (“%s”) skickades inte. "
        "Sätt SMTP_HOST (se .env.example) eller, för lokal utveckling, peka SMTP mot en "
        "mailfångare (t.ex. `docker compose --profile dev-mail up mailhog`, se "
        "docs/OPERATIONS.md).",
        to,
        subject,
    )
    if settings.dev_mail_outbox_dir and settings.environment != "production":
        _write_dev_outbox(to, subject, body_text)


def _send_via_smtp(to: str, subject: str, body_text: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
    msg["To"] = to
    msg.set_content(body_text)

    try:
        # Implicit TLS (smtp_use_ssl, e.g. Strato's smtp.strato.com:465) establishes the TLS
        # handshake as part of the connection itself — smtplib.SMTP_SSL, not smtplib.SMTP —
        # there is no separate .starttls() call because the connection is never plaintext.
        # STARTTLS (the smtp_use_tls default, e.g. port 587) connects in plaintext and
        # upgrades in-place instead. These are genuinely different wire protocols, not a
        # single client with two flags — see docs/RENDER_DEPLOY.md.
        if settings.smtp_use_ssl:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10) as server:
                if settings.smtp_username and settings.smtp_password:
                    server.login(settings.smtp_username, settings.smtp_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
                if settings.smtp_use_tls:
                    server.starttls()
                if settings.smtp_username and settings.smtp_password:
                    server.login(settings.smtp_username, settings.smtp_password)
                server.send_message(msg)
    except Exception:
        # Deliberately no email body/token in this log line either — only what's needed to
        # notice and investigate a delivery outage.
        logger.exception("Kunde inte skicka e-post till %s", to)


def _write_dev_outbox(to: str, subject: str, body_text: str) -> None:
    """Opt-in local-dev convenience only — see dev_mail_outbox_dir in app/config.py. Each
    email is its own file so it's easy to open the exact one you're waiting for, and never
    mixed into the structured application log stream."""
    outbox = Path(settings.dev_mail_outbox_dir)
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / f"{uuid.uuid4()}.txt"
    path.write_text(f"To: {to}\nSubject: {subject}\n\n{body_text}\n")
