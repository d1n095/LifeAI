import logging
import smtplib
from email.message import EmailMessage

from app.config import get_settings

logger = logging.getLogger("mainai.email")
settings = get_settings()


def send_email(to: str, subject: str, body_text: str) -> None:
    """Sends via SMTP if configured; otherwise logs the email instead (dev mode) — the same
    graceful-degradation pattern used for unconfigured AI providers elsewhere in this app.

    Never raises: a bounced or misconfigured mail server must not turn into a 500 for the
    caller (registration/reset must still succeed — the account and token already exist
    regardless of whether delivery worked), but a failure IS logged loudly so it's not
    silently swallowed in production. Never logs the token/link itself at INFO level in
    production-shaped deployments beyond this dev-mode fallback path, which stands in for
    the email itself only when no real mail transport is configured."""
    if not settings.smtp_host:
        logger.info("DEV-LÄGE (SMTP ej konfigurerat) — mail till %s: %s\n%s", to, subject, body_text)
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
    msg["To"] = to
    msg.set_content(body_text)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
            if settings.smtp_use_tls:
                server.starttls()
            if settings.smtp_username and settings.smtp_password:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(msg)
    except Exception:
        logger.exception("Kunde inte skicka e-post till %s", to)
