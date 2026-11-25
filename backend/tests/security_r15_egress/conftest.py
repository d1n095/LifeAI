"""Minimal, DB-free environment for the R15 egress-owner regression tests.

These tests exercise the authorization boundary before any provider call. They
intentionally do not use the shared database fixtures; run them with
``--confcutdir=tests/security_r15_egress`` from ``backend/`` when no disposable
PostgreSQL is available.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://lifeos@localhost:5433/lifeos_test_r15_egress")
os.environ.setdefault(
    "APP_DATABASE_URL",
    "postgresql://mainai_app:mainai_app_pw@localhost:5433/lifeos_test_r15_egress",
)
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-r15-egress")
os.environ.setdefault("FOUNDER_EMAIL", "founder@lifeos.local")
os.environ.setdefault("FOUNDER_PASSWORD", "TestFounderPassword123!")
os.environ.setdefault("FRONTEND_ORIGINS", "http://127.0.0.1:3020")
os.environ.setdefault("PUBLIC_APP_URL", "http://127.0.0.1:3020")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
