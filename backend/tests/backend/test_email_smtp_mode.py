"""Verifies app/email.py picks the right smtplib client for each SMTP connection mode —
without a real network connection or real credentials. smtplib.SMTP/SMTP_SSL are mocked;
nothing here talks to an actual mail server (see app/main.py's _check_smtp_mode for the
startup-time guard against configuring both modes at once)."""

from unittest.mock import MagicMock

import app.email as email_module


def _mock_smtp_class(monkeypatch, attr: str) -> MagicMock:
    instance = MagicMock()
    instance.__enter__.return_value = instance
    instance.__exit__.return_value = False
    cls = MagicMock(return_value=instance)
    monkeypatch.setattr(email_module.smtplib, attr, cls)
    return cls


def test_implicit_ssl_uses_smtp_ssl_not_starttls(monkeypatch):
    monkeypatch.setattr(email_module.settings, "smtp_host", "smtp.strato.com")
    monkeypatch.setattr(email_module.settings, "smtp_port", 465)
    monkeypatch.setattr(email_module.settings, "smtp_use_tls", False)
    monkeypatch.setattr(email_module.settings, "smtp_use_ssl", True)
    monkeypatch.setattr(email_module.settings, "smtp_username", "life@4thepeople.se")
    monkeypatch.setattr(email_module.settings, "smtp_password", "fake-not-a-real-secret")

    ssl_cls = _mock_smtp_class(monkeypatch, "SMTP_SSL")
    starttls_cls = _mock_smtp_class(monkeypatch, "SMTP")

    email_module._send_via_smtp("user@example.com", "Subject", "Body")

    ssl_cls.assert_called_once_with("smtp.strato.com", 465, timeout=10)
    starttls_cls.assert_not_called()
    ssl_instance = ssl_cls.return_value
    ssl_instance.login.assert_called_once_with("life@4thepeople.se", "fake-not-a-real-secret")
    ssl_instance.starttls.assert_not_called()
    ssl_instance.send_message.assert_called_once()


def test_starttls_mode_still_uses_smtp_and_starttls(monkeypatch):
    monkeypatch.setattr(email_module.settings, "smtp_host", "smtp.exempel.se")
    monkeypatch.setattr(email_module.settings, "smtp_port", 587)
    monkeypatch.setattr(email_module.settings, "smtp_use_tls", True)
    monkeypatch.setattr(email_module.settings, "smtp_use_ssl", False)
    monkeypatch.setattr(email_module.settings, "smtp_username", None)
    monkeypatch.setattr(email_module.settings, "smtp_password", None)

    ssl_cls = _mock_smtp_class(monkeypatch, "SMTP_SSL")
    starttls_cls = _mock_smtp_class(monkeypatch, "SMTP")

    email_module._send_via_smtp("user@example.com", "Subject", "Body")

    starttls_cls.assert_called_once_with("smtp.exempel.se", 587, timeout=10)
    ssl_cls.assert_not_called()
    starttls_instance = starttls_cls.return_value
    starttls_instance.starttls.assert_called_once()
    starttls_instance.login.assert_not_called()
    starttls_instance.send_message.assert_called_once()


def test_send_email_never_raises_on_smtp_failure(monkeypatch):
    monkeypatch.setattr(email_module.settings, "smtp_host", "smtp.strato.com")
    monkeypatch.setattr(email_module.settings, "smtp_port", 465)
    monkeypatch.setattr(email_module.settings, "smtp_use_tls", False)
    monkeypatch.setattr(email_module.settings, "smtp_use_ssl", True)

    def _raise(*args, **kwargs):
        raise OSError("connection refused (simulated, no real server involved)")

    monkeypatch.setattr(email_module.smtplib, "SMTP_SSL", _raise)

    email_module.send_email("user@example.com", "Subject", "Body")  # must not raise
