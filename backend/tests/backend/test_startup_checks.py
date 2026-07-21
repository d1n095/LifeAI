"""Coverage for app/main.py's fail-fast production startup checks. These run once, at
`@app.on_event("startup")`, before the app serves any traffic — the whole point is to turn a
silent/late misconfiguration into a loud, immediate one. Exercised directly against the
module-level functions (not through a live TestClient boot) so each check's exact trigger
condition and message can be verified in isolation."""

import pytest

import app.main as main_module


def test_redis_unreachable_error_redacts_password(monkeypatch):
    """Regression test: the pre-fix code embedded the raw REDIS_URL — including its
    password — directly into the RuntimeError message, which startup exceptions routinely
    end up in the application log / Render's log stream. Verified this was a real leak by
    running this test against the original code before adding _redact_url_credentials: it
    failed, with the fake password plainly visible in the exception text."""
    fake_password = "S3cretRedisPassw0rd"
    monkeypatch.setattr(
        main_module.settings, "redis_url", f"rediss://default:{fake_password}@fake-host.upstash.io:6379"
    )

    import redis as redis_module

    class _FakeClient:
        def ping(self):
            raise redis_module.RedisError("connection refused (simulated, no real server involved)")

        def close(self):
            pass

    # _check_redis_reachable() does `import redis` inside the function body — patching the
    # real, cached `redis` module's from_url is what that local import resolves to.
    monkeypatch.setattr(redis_module, "from_url", lambda *a, **kw: _FakeClient())

    with pytest.raises(RuntimeError) as exc_info:
        main_module._check_redis_reachable()

    message = str(exc_info.value)
    assert fake_password not in message
    assert "***" in message
    assert "fake-host.upstash.io" in message  # host/port still shown — only the secret is hidden


def test_redact_url_credentials_masks_password_only():
    redacted = main_module._redact_url_credentials("postgresql://myuser:mypassword@example.com:5432/db")
    assert "mypassword" not in redacted
    assert "myuser" in redacted
    assert "example.com:5432/db" in redacted
    assert redacted == "postgresql://myuser:***@example.com:5432/db"


def test_redact_url_credentials_leaves_password_free_url_unchanged():
    url = "postgresql://example.com:5432/db"
    assert main_module._redact_url_credentials(url) == url


def test_redact_url_credentials_does_not_raise_on_garbage_input():
    # An error-redaction helper must never itself raise out of an exception handler.
    assert main_module._redact_url_credentials("not a url at all") == "not a url at all"


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("secret_key", "change-me-in-production"),
        ("founder_password", "change-me-in-production"),
        ("founder_email", "founder@lifeos.local"),
    ],
)
def test_placeholder_secret_check_rejects_default_value_in_production(monkeypatch, field, bad_value):
    monkeypatch.setattr(main_module.settings, "secret_key", "a-real-generated-secret")
    monkeypatch.setattr(main_module.settings, "founder_password", "ARealFounderPassword123!")
    monkeypatch.setattr(main_module.settings, "founder_email", "real-founder@example.com")
    monkeypatch.setattr(main_module.settings, field, bad_value)

    with pytest.raises(RuntimeError) as exc_info:
        main_module._check_no_placeholder_secrets()
    assert field.upper() in str(exc_info.value)


def test_placeholder_secret_check_passes_with_real_values(monkeypatch):
    monkeypatch.setattr(main_module.settings, "secret_key", "a-real-generated-secret")
    monkeypatch.setattr(main_module.settings, "founder_password", "ARealFounderPassword123!")
    monkeypatch.setattr(main_module.settings, "founder_email", "real-founder@example.com")

    main_module._check_no_placeholder_secrets()  # must not raise


def test_cookie_security_check_rejects_insecure_cookies_in_production(monkeypatch):
    monkeypatch.setattr(main_module.settings, "cookie_secure", False)
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        main_module._check_cookies_secure()


def test_cookie_security_check_passes_when_secure(monkeypatch):
    monkeypatch.setattr(main_module.settings, "cookie_secure", True)
    main_module._check_cookies_secure()  # must not raise


def test_smtp_mode_check_rejects_both_tls_and_ssl(monkeypatch):
    monkeypatch.setattr(main_module.settings, "smtp_host", "smtp.strato.com")
    monkeypatch.setattr(main_module.settings, "smtp_use_tls", True)
    monkeypatch.setattr(main_module.settings, "smtp_use_ssl", True)
    with pytest.raises(RuntimeError, match="SMTP_USE_TLS"):
        main_module._check_smtp_mode()


def test_smtp_mode_check_passes_with_exactly_one_mode(monkeypatch):
    monkeypatch.setattr(main_module.settings, "smtp_host", "smtp.strato.com")
    monkeypatch.setattr(main_module.settings, "smtp_use_tls", False)
    monkeypatch.setattr(main_module.settings, "smtp_use_ssl", True)
    main_module._check_smtp_mode()  # must not raise


def test_smtp_mode_check_passes_with_tls_true_ssl_false(monkeypatch):
    monkeypatch.setattr(main_module.settings, "smtp_host", "smtp.strato.com")
    monkeypatch.setattr(main_module.settings, "smtp_use_tls", True)
    monkeypatch.setattr(main_module.settings, "smtp_use_ssl", False)
    main_module._check_smtp_mode()  # must not raise


def test_smtp_mode_check_rejects_both_false_in_production(monkeypatch):
    """SMTP_HOST set but neither mode true, in production: _send_via_smtp() would fall
    through to a plain, unencrypted smtplib.SMTP connection with no .starttls() call —
    real mail sent in cleartext. Must fail at startup, not on the first delivery attempt."""
    monkeypatch.setattr(main_module.settings, "environment", "production")
    monkeypatch.setattr(main_module.settings, "smtp_host", "smtp.strato.com")
    monkeypatch.setattr(main_module.settings, "smtp_use_tls", False)
    monkeypatch.setattr(main_module.settings, "smtp_use_ssl", False)
    with pytest.raises(RuntimeError, match="SMTP_USE_TLS"):
        main_module._check_smtp_mode()


def test_smtp_mode_check_allows_both_false_in_development(monkeypatch):
    """Non-production environments (dev/CI, often pointed at a throwaway/placeholder
    SMTP_HOST) keep today's behavior unchanged — this check only fails closed in
    production."""
    monkeypatch.setattr(main_module.settings, "environment", "development")
    monkeypatch.setattr(main_module.settings, "smtp_host", "smtp.strato.com")
    monkeypatch.setattr(main_module.settings, "smtp_use_tls", False)
    monkeypatch.setattr(main_module.settings, "smtp_use_ssl", False)
    main_module._check_smtp_mode()  # must not raise


def test_smtp_configured_check_rejects_missing_host(monkeypatch):
    monkeypatch.setattr(main_module.settings, "smtp_host", None)
    with pytest.raises(RuntimeError, match="SMTP_HOST"):
        main_module._check_smtp_configured()


def test_smtp_configured_check_passes_with_host_set(monkeypatch):
    monkeypatch.setattr(main_module.settings, "smtp_host", "smtp.strato.com")
    main_module._check_smtp_configured()  # must not raise
