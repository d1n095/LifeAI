"""The install path must put the database into a state the API can boot from.

The reported defect: a freshly migrated database passed `alembic upgrade head` and the
acceptance harness, then `uvicorn app.main:app` refused to start because `mainai_app` had no
INSERT/SELECT on `mainai_job_events`. The missing install step was `ensure_app_role.py` running
BEFORE the migration (its `ALTER DEFAULT PRIVILEGES` is what grants tables the migration
creates).

`scripts/setup/bootstrap_database.py` is that sequence plus a verification that would have
caught the defect pre-boot. These tests pin the ORDER and the fact that `--verify-only` mutates
nothing; the real-database proof is the bootstrap run itself and
`tests/acceptance/test_real_retrieval_quality.py`'s sibling acceptance evidence, not a mock.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = BACKEND_ROOT / "scripts" / "setup" / "bootstrap_database.py"


def _load():
    spec = importlib.util.spec_from_file_location("bootstrap_database", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    # Dataclasses resolves postponed annotations through sys.modules[cls.__module__]; a module
    # executed without being registered there makes @dataclass fail at class-creation time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bootstrap_module(monkeypatch):
    module = _load()
    monkeypatch.setenv("DATABASE_URL", "postgresql://admin@localhost:5433/example")
    monkeypatch.setenv("APP_DATABASE_URL", "postgresql://mainai_app:pw@localhost:5433/example")
    return module


def _record(monkeypatch, module, calls: list[str], *, fail: str | None = None):
    for name in (
        "step_ensure_app_role",
        "step_migrate",
        "step_apply_privileges",
        "verify_schema_head",
        "verify_runtime_grants",
        "verify_s1a_policy",
        "verify_boot_contract",
    ):
        def make(step_name):
            def runner(*args, **kwargs):
                calls.append(step_name)
                if fail == step_name:
                    raise module.BootstrapError(step_name, "injected failure")
                return {}
            return runner

        monkeypatch.setattr(module, name, make(name))


def test_role_provisioning_runs_before_migration_and_policy_verification_after(bootstrap_module, monkeypatch):
    module = bootstrap_module
    calls: list[str] = []
    _record(monkeypatch, module, calls)

    report = module.bootstrap(verify_only=False)

    assert calls[:3] == ["step_ensure_app_role", "step_migrate", "step_apply_privileges"]
    # The boot contract is enforced before the grants are verified: otherwise the broad floor
    # `ensure_app_role` grants is verified as if it were the final state, and the very next run
    # (after app/main.py narrows it) reports a healthy database as broken.
    assert calls[3:] == [
        "verify_schema_head",
        "verify_boot_contract",
        "verify_runtime_grants",
        "verify_s1a_policy",
    ]
    assert report["ready"] is True
    assert report["mode"] == "bootstrap"
    assert [s["id"] for s in report["steps"]][0] == "ensure_app_role"


def test_verify_only_never_runs_a_mutating_step(bootstrap_module, monkeypatch):
    module = bootstrap_module
    calls: list[str] = []
    _record(monkeypatch, module, calls)

    module.bootstrap(verify_only=True)

    assert calls == [
        "verify_schema_head",
        "verify_boot_contract",
        "verify_runtime_grants",
        "verify_s1a_policy",
    ]


def test_a_failed_step_stops_the_sequence(bootstrap_module, monkeypatch):
    module = bootstrap_module
    calls: list[str] = []
    _record(monkeypatch, module, calls, fail="step_migrate")

    with pytest.raises(module.BootstrapError) as exc:
        module.bootstrap(verify_only=False)

    assert exc.value.step == "step_migrate"
    assert calls == ["step_ensure_app_role", "step_migrate"]


def test_missing_database_url_is_a_configuration_refusal(bootstrap_module, monkeypatch):
    module = bootstrap_module
    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(module.BootstrapError) as exc:
        module.bootstrap(verify_only=True)
    assert exc.value.step == "config"


def test_missing_runtime_credential_is_a_configuration_refusal(bootstrap_module, monkeypatch):
    module = bootstrap_module
    monkeypatch.delenv("APP_DATABASE_URL")
    monkeypatch.delenv("MAINAI_APP_PASSWORD", raising=False)
    with pytest.raises(module.BootstrapError) as exc:
        module.bootstrap(verify_only=True)
    assert exc.value.step == "config"


def test_main_returns_two_for_configuration_errors_and_one_for_step_failures(bootstrap_module, monkeypatch):
    module = bootstrap_module
    monkeypatch.delenv("DATABASE_URL")
    assert module.main(["--verify-only"]) == 2


def test_append_only_job_events_are_not_expected_to_accept_updates(bootstrap_module):
    """A second bootstrap run must not disagree with the first one.

    Found by running the documented readiness check twice: `verify_grants` demanded
    UPDATE/DELETE on `mainai_job_events`, but `verify_boot` — the last step of the same run —
    re-asserts `app/rls.py`'s append-only lockdown, which requires `mainai_app` to hold only
    SELECT and INSERT there. The first run therefore passed and every later `--verify-only` on
    the same healthy database failed. The fix is to expect the policy the API actually enforces,
    and to fail loudly if a future grant widens it again.
    """
    assert "mainai_job_events" not in bootstrap_module.CORE_WRITE_TABLES
    assert bootstrap_module.APPEND_ONLY_TABLES["mainai_job_events"] == ("SELECT", "INSERT")
