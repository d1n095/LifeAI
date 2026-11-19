#!/usr/bin/env python3
"""One founder-release acceptance for the private LifeAI release candidate.

`founder_alpha_acceptance.py` answers "can this database boot and is the install sound?".
`founder_workflow.py` answers "can a founder do the twelve real steps?".
Neither answers the release question on its own, and a green test file is not a green release.
This script runs the fourteen release items once, in one report, and refuses to imply more than
it measured.

Every item declares how its evidence was produced:

  local_real     executed against this host: real PostgreSQL, real Redis, real Ollama models,
                 real Chromium, real encrypted backup artifacts.
  ci             produced by the repository's CI on a known commit. It is *recorded*, never
                 re-run here, and never upgraded to local evidence.
  external_blocked
                 cannot be executed because an artifact does not exist on this host. The item is
                 reported blocked with the exact transfer requirement, not skipped silently.

Exit codes:
  0  every executed check passed and every item is verified
  1  at least one executed check failed
  2  nothing failed, but at least one item is blocked on an external artifact

Exit 2 is deliberately distinct from 0: "we could not test it" is not "it works".

Usage:
    python backend/scripts/acceptance/founder_release_acceptance.py \
        --out release-acceptance.json [--skip-browser] [--skip-install] [--skip-restore]

Environment (all optional, feature detected):
    DATABASE_URL, APP_DATABASE_URL   required for --install/--restore scratch databases
    MAINAI_BACKUP_KEY_FILE           key for the encrypted backup roundtrip
    FOUNDER_EMAIL, FOUNDER_PASSWORD  required for the workflow and browser items
    E2E_INTERNAL_API_URL, E2E_FRONTEND_URL  target of the browser run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse, urlunparse

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
FRONTEND_ROOT = REPO_ROOT / "frontend"

PASSED, FAILED, SKIPPED, BLOCKED = "passed", "failed", "skipped", "blocked"

# Z.ai Round 12, the Cognitive Core that item 4 depends on. Kept as data so the probe and the
# report can never disagree about which commit is being looked for.
ROUND12_COMMIT = "cc4d2804"
ROUND12_PACKAGE = "lifeai-cognitive-core-r12-release"


# ---------------------------------------------------------------------------------------------
# Command helpers
# ---------------------------------------------------------------------------------------------


def _tail(text: str, lines: int = 6) -> list[str]:
    return (text or "").strip().splitlines()[-lines:]


def _run(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    unset: tuple[str, ...] = (),
    timeout: int = 600,
) -> tuple[str, dict]:
    started = time.monotonic()
    child_env = {**os.environ, **(env or {})}
    for key in unset:
        child_env.pop(key, None)
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return FAILED, {"error": f"timed out after {timeout}s", "command": " ".join(cmd[:3])}
    detail = {
        "exit_code": result.returncode,
        "seconds": round(time.monotonic() - started, 1),
        "output": _tail(result.stdout + result.stderr),
    }
    return (PASSED if result.returncode == 0 else FAILED), detail


def _pytest(*paths: str, timeout: int = 900):
    """A suite check, always against a disposable database we do not name.

    The suite drops the database `DATABASE_URL` points at (session setup and teardown). A release
    gate that passes the founder's live URL through deletes it — which is exactly what happened
    once. These variables are therefore removed from the child environment, so `conftest.py`
    chooses its own per-process `lifeos_test_<pid>` and its guard rejects anything else.
    """
    # Same credentials, disposable database name. Dropping the URLs entirely would also drop the
    # runtime role's real password, and `conftest.py` would then try a default one that this host
    # does not use. Pointing both URLs at `lifeos_test_gate_*` keeps the real role, gives pytest a
    # database it may legitimately drop, and leaves the live one untouched.
    def _disposable_urls() -> dict[str, str]:
        base, app = os.environ.get("DATABASE_URL"), os.environ.get("APP_DATABASE_URL")
        if not base or not app:
            return {}
        name = f"lifeos_test_gate_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        return {"DATABASE_URL": _with_database(base, name), "APP_DATABASE_URL": _with_database(app, name)}

    # Provider selection is isolated too. Several suites monkeypatch the fake provider and assert
    # on the messages it received; an exported `DEFAULT_LLM_PROVIDER=ollama` (correct for the
    # running app) sends chat down a real path those fixtures never capture, so the suite fails
    # for a reason that has nothing to do with the code under test. Measured: 3 injection and 1
    # cancellation test failed with the app's provider env exported, all passed without it.
    test_only_unset = (
        "LIFEAI_TEST_DATABASE_NAME",
        "STORAGE_ROOT",
        "REDIS_URL",
        "ENVIRONMENT",
        "OLLAMA_BASE_URL",
        "LOCAL_LLM_BASE_URL",
        "DEFAULT_LLM_PROVIDER",
        "DEFAULT_LLM_MODEL",
        "DEFAULT_EMBEDDING_PROVIDER",
        "DEFAULT_EMBEDDING_MODEL",
        # Cookie flags are a deployment choice (this host intentionally relaxes them for plain
        # http://localhost). The suite asserts the production Secure/SameSite=None contract, so
        # exporting the local relaxation into it tests the wrong configuration.
        "COOKIE_SECURE",
        "COOKIE_SAMESITE",
    )

    def runner() -> tuple[str, dict]:
        return _run(
            [sys.executable, "-m", "pytest", *paths, "-q", "-p", "no:cacheprovider"],
            cwd=BACKEND_ROOT,
            env=_disposable_urls(),
            unset=test_only_unset,
            timeout=timeout,
        )

    return runner


# ---------------------------------------------------------------------------------------------
# Scratch databases — a real fresh install, not a claim about one
# ---------------------------------------------------------------------------------------------


def _with_database(url: str, database: str) -> str:
    parts = urlparse(url)
    return urlunparse(parts._replace(path=f"/{database}"))


def _admin_connection(database_url: str):
    """Admin connection on the same server, used only to create/drop the scratch databases."""
    import psycopg2

    admin = _with_database(database_url, "postgres")
    conn = psycopg2.connect(admin, connect_timeout=10)
    conn.autocommit = True
    return conn


def _create_database(database_url: str, name: str) -> None:
    # `psycopg2.sql.Identifier` rather than an f-string: the names here are generated, but the
    # repository's own Semgrep rule (mainai-sql-text-interpolation) rightly refuses interpolated
    # SQL anywhere, and the driver's identifier quoting is the correct API regardless.
    from psycopg2 import sql

    conn = _admin_connection(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    finally:
        conn.close()


def _drop_database(database_url: str, name: str) -> None:
    from psycopg2 import sql

    conn = _admin_connection(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
    finally:
        conn.close()


def _bootstrap(scratch_url: str, scratch_app_url: str, storage_root: Path, *, verify_only: bool):
    args = [sys.executable, str(BACKEND_ROOT / "scripts" / "setup" / "bootstrap_database.py")]
    if verify_only:
        args.append("--verify-only")
    env = {
        "DATABASE_URL": scratch_url,
        "APP_DATABASE_URL": scratch_app_url,
        "STORAGE_ROOT": str(storage_root),
    }
    # `ensure_app_role.py` re-provisions the runtime role and needs its password explicitly. It is
    # already present in APP_DATABASE_URL, so the operator never has to export it a second time —
    # and this script never prints or writes it.
    app_password = unquote(urlparse(scratch_app_url).password or "")
    if app_password:
        env["MAINAI_APP_PASSWORD"] = app_password
    return _run(
        args,
        cwd=BACKEND_ROOT,
        env=env,
        timeout=900,
    )


def check_fresh_install() -> tuple[str, dict]:
    """Install into a database that does not exist yet, then prove the boot contract twice.

    The second `--verify-only` run is what makes this an idempotency test rather than a one-shot
    install: a bootstrap that only works on its first run is the defect that produced the original
    "fresh database cannot boot" report.
    """
    database_url = os.environ.get("DATABASE_URL")
    app_url = os.environ.get("APP_DATABASE_URL")
    if not database_url or not app_url:
        return SKIPPED, {"reason": "DATABASE_URL / APP_DATABASE_URL not set in this shell"}

    suffix = f"{datetime.now(timezone.utc):%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"
    name = f"lifeos_release_gate_{suffix}"
    scratch_url, scratch_app_url = _with_database(database_url, name), _with_database(app_url, name)
    storage = Path(tempfile.mkdtemp(prefix="release-gate-storage-"))
    created = False
    try:
        _create_database(database_url, name)
        created = True
        install_status, install_detail = _bootstrap(scratch_url, scratch_app_url, storage, verify_only=False)
        if install_status != PASSED:
            return FAILED, {"stage": "bootstrap", **install_detail}
        verify_status, verify_detail = _bootstrap(scratch_url, scratch_app_url, storage, verify_only=True)
        return verify_status, {
            "database": name,
            "install_seconds": install_detail.get("seconds"),
            "idempotent_reverify": verify_detail.get("output"),
            "dropped_after_run": True,
        }
    finally:
        if created:
            _drop_database(database_url, name)
        for leftover in storage.glob("*"):
            leftover.unlink(missing_ok=True)
        storage.rmdir()


def check_backup_roundtrip() -> tuple[str, dict]:
    """Encrypt, ship, verify, download, decrypt and restore into an empty database — then boot it.

    A restore that produces a database nobody proved bootable is half a recovery. The last step is
    the same `bootstrap --verify-only` the API's startup contract runs.
    """
    database_url = os.environ.get("DATABASE_URL")
    app_url = os.environ.get("APP_DATABASE_URL")
    key_file = os.environ.get("MAINAI_BACKUP_KEY_FILE", "/tmp/mainai-backup-key.txt")
    if not database_url or not app_url:
        return SKIPPED, {"reason": "DATABASE_URL / APP_DATABASE_URL not set in this shell"}
    if not Path(key_file).is_file():
        return SKIPPED, {"reason": f"no encryption key at {key_file} (MAINAI_BACKUP_KEY_FILE)"}

    script = str(BACKEND_ROOT / "scripts" / "ops" / "offsite_backup.py")
    destination = Path(tempfile.mkdtemp(prefix="release-gate-offsite-"))
    suffix = f"{datetime.now(timezone.utc):%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"
    target = f"lifeos_release_gate_restore_{suffix}"
    target_url, target_app_url = _with_database(database_url, target), _with_database(app_url, target)
    storage = Path(tempfile.mkdtemp(prefix="release-gate-restore-storage-"))
    created = False
    try:
        push_status, push_detail = _run(
            [
                sys.executable,
                script,
                "push",
                "--database-url",
                database_url,
                "--destination",
                f"file://{destination}",
                "--key-file",
                key_file,
            ],
            cwd=BACKEND_ROOT,
            timeout=900,
        )
        if push_status != PASSED:
            return FAILED, {"stage": "push", **push_detail}

        transport_files = sorted(destination.glob("*.dump.transport.json"))
        if not transport_files:
            return FAILED, {"stage": "push", "error": "no transport manifest was written"}
        # `--name` is the dump's base name (e.g. `lifeos-...Z.dump`), not the bare timestamp:
        # the tool appends `.enc` / `.manifest.json` / `.transport.json` itself.
        name = transport_files[-1].name.removesuffix(".transport.json")

        verify_status, verify_detail = _run(
            [sys.executable, script, "verify", "--destination", f"file://{destination}", "--name", name],
            cwd=BACKEND_ROOT,
        )
        if verify_status != PASSED:
            return FAILED, {"stage": "verify", **verify_detail}

        _create_database(database_url, target)
        created = True
        restore_status, restore_detail = _run(
            [
                sys.executable,
                script,
                "restore",
                "--destination",
                f"file://{destination}",
                "--name",
                name,
                "--target-url",
                target_url,
                "--confirm",
                "--key-file",
                key_file,
            ],
            cwd=BACKEND_ROOT,
            env={"APP_DATABASE_URL": target_app_url, "STORAGE_ROOT": str(storage)},
            timeout=900,
        )
        if restore_status != PASSED:
            return FAILED, {"stage": "restore", **restore_detail}

        boot_status, boot_detail = _bootstrap(target_url, target_app_url, storage, verify_only=True)
        return boot_status, {
            "artifact": name,
            "encrypted": True,
            "off_host_verified": False,
            "off_host_note": "file:// is a controlled remote-equivalent, not off-host retention",
            "restored_database_bootable": boot_status == PASSED,
            "boot_output": boot_detail.get("output"),
        }
    finally:
        if created:
            _drop_database(database_url, target)
        for leftover in storage.glob("*"):
            leftover.unlink(missing_ok=True)
        storage.rmdir()


# ---------------------------------------------------------------------------------------------
# Round 12 — the one item that cannot be executed on this host
# ---------------------------------------------------------------------------------------------


def check_cognitive_core() -> tuple[str, dict]:
    """Probe for the Cognitive Core artifact and report the exact gap, never a substitute.

    Documentation of Z.ai's C1–C7 guarantees is not evidence that this tree enforces them, and a
    "similar branch" is not the delivered commit.
    """
    probes: dict[str, str] = {}
    present = False

    local_object = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "-t", ROUND12_COMMIT],
        capture_output=True,
        text=True,
    )
    probes["git cat-file -t"] = (local_object.stdout or local_object.stderr).strip().splitlines()[0]
    present = present or local_object.returncode == 0

    package = REPO_ROOT / ROUND12_PACKAGE
    probes[f"{ROUND12_PACKAGE}/"] = "present" if package.exists() else "absent"
    present = present or package.exists()

    if present:
        # Found but not in the conversation path is still not integrated. Say that precisely.
        return FAILED, {
            "probes": probes,
            "reason": "artifact is reachable but has not been verified, merged or wired into the "
            "MainAI conversation path; the integration phases are still outstanding",
        }

    return BLOCKED, {
        "probes": probes,
        "commit": ROUND12_COMMIT,
        "reason": "no Git object, ref, package directory or bundle containing the Cognitive Core",
        "required_transfer": [
            f"a git bundle containing {ROUND12_COMMIT} plus its SHA-256 "
            "(git bundle create lifeai-cognitive-core-r12-release.bundle <range>)",
            "a git format-patch series whose cover letter names the produced SHAs",
            f"or a pushed scratch ref in this repository carrying {ROUND12_COMMIT}",
        ],
        "known_collision": {
            "zai_reported_migrations": "0075-0077",
            "this_tree": {
                "0075": "system_activation_authorization",
                "0076": "github_evidence_staging",
                "0077": "calendar_foundation",
                "0078": "repository_file_source_units",
                "0079": "audit_log_append_only (head)",
            },
            "consequence": "the chain collides by identity, not just by number; resolving it "
            "requires a merge migration and a reviewed equivalence argument, never renumbering "
            "migrations that have already been applied",
        },
    }


# ---------------------------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------------------------


@dataclass
class Run:
    id: str
    title: str
    evidence: str
    checks: list[str]
    mandatory: bool = True
    extra: dict = field(default_factory=dict)


def build_checks(args) -> dict[str, object]:
    checks: dict[str, object] = {
        "structural": lambda: _run(
            [
                sys.executable,
                str(BACKEND_ROOT / "scripts" / "acceptance" / "founder_alpha_acceptance.py"),
                "--out",
                str(Path(args.out).with_name("founder-alpha-acceptance.json")),
                "--skip-suites",
            ],
            cwd=BACKEND_ROOT,
            timeout=900,
        ),
        "workflow": lambda: _run(
            [
                sys.executable,
                str(BACKEND_ROOT / "scripts" / "acceptance" / "founder_workflow.py"),
                "--base-url",
                os.environ.get("E2E_FRONTEND_URL", "http://127.0.0.1:3020"),
                "--out",
                str(Path(args.out).with_name("founder-workflow.json")),
            ],
            cwd=BACKEND_ROOT,
            timeout=900,
        ),
        "suites.auth": _pytest("tests/security/test_session_auth.py", "tests/security/test_security_controls.py"),
        "suites.rls": _pytest("tests/security/test_rls_isolation.py"),
        "suites.injection": _pytest("tests/security/test_prompt_injection_fencing.py"),
        "suites.cancel": _pytest(
            "tests/backend/test_mainai_execution_cancellation.py",
            "tests/backend/mainai/test_cancel_vs_finalize_race.py",
            "tests/backend/mainai/test_founder_cancel_before_write.py",
        ),
        "suites.providers": _pytest(
            "tests/backend/providers/test_chat_fallback_logging.py",
            "tests/backend/providers/test_ollama_provider_failclosed.py",
        ),
        "suites.memory": _pytest("tests/backend/test_founder_alpha_runtime_flow.py"),
        "cognitive": check_cognitive_core,
    }
    if not args.skip_install:
        checks["install"] = check_fresh_install
    if not args.skip_restore:
        checks["restore"] = check_backup_roundtrip
    if not args.skip_browser:
        checks["browser"] = lambda: _run(
            [
                "npx",
                "playwright",
                "test",
                "e2e/founder-console.spec.ts",
                "e2e/founder-alpha-workflow.spec.ts",
            ],
            cwd=FRONTEND_ROOT,
            timeout=900,
        )
    return checks


def build_items(args) -> list[Run]:
    return [
        Run("1.fresh_install", "Fresh installation from an empty database", "local_real", ["install", "structural"]),
        Run("2.login", "Real founder authentication through the same-origin proxy", "local_real", ["workflow"]),
        Run("3.model_conversation", "A real local model answers a real question", "local_real", ["workflow", "structural"]),
        Run(
            "4.cognitive_reasoning",
            "Cognitive reasoning and premise challenge (Z.ai R12)",
            "external_blocked",
            ["cognitive"],
        ),
        Run("5.provenance", "Evidence provenance and citations from indexed documents", "local_real", ["workflow"]),
        Run("6.memory_continuity", "Cross-session memory and decision continuity", "local_real", ["suites.memory", "workflow"]),
        Run("7.agent_execution", "Safe agent execution", "local_real", ["structural"]),
        Run("8.approval_enforcement", "Approval and denial enforcement", "local_real", ["suites.auth"]),
        Run("9.injection_resistance", "Prompt-injection resistance under the canonical fence", "local_real", ["suites.injection"]),
        Run("10.worker_cancellation", "Worker cancellation and recovery", "local_real", ["suites.cancel"]),
        Run("11.browser", "Real browser functionality (Chromium, real backend)", "local_real", ["browser"]),
        Run("12.backup_restore", "Encrypted backup and clean restore that boots", "local_real", ["restore"]),
        Run("13.provider_switching", "Model-provider switching and fail-closed fallback", "local_real", ["suites.providers"]),
        Run("14.security_regression", "Security and regression suites", "local_real", ["structural", "suites.rls"]),
    ]


def _combine(statuses: list[str]) -> str:
    if FAILED in statuses:
        return FAILED
    if BLOCKED in statuses:
        return BLOCKED
    if statuses and all(s == PASSED for s in statuses):
        return PASSED
    return SKIPPED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="release-acceptance.json")
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument("--skip-install", action="store_true", help="skip the scratch-database install")
    parser.add_argument("--skip-restore", action="store_true", help="skip the backup roundtrip")
    args = parser.parse_args(argv)
    # The sub-reports are written by child processes that run from `backend/`, so a relative
    # `--out` (for example `docs/evidence/...` from the repository root) must be resolved here or
    # the children fail to find the directory.
    args.out = str(Path(args.out).resolve())

    checks = build_checks(args)
    results: dict[str, dict] = {}
    for check_id, runner in checks.items():
        print(f"… {check_id}", flush=True)
        try:
            status, detail = runner()
        except Exception as exc:  # a crashing check is a failed check, never a pass
            status, detail = FAILED, {"error": f"{type(exc).__name__}: {exc}"}
        results[check_id] = {"status": status, "detail": detail}

    items = []
    for item in build_items(args):
        statuses = [results[c]["status"] for c in item.checks if c in results]
        status = _combine(statuses) if statuses else SKIPPED
        items.append(
            {
                "id": item.id,
                "title": item.title,
                "evidence": item.evidence,
                "mandatory": item.mandatory,
                "status": status,
                "checks": item.checks,
                "detail": [results[c]["detail"] for c in item.checks if c in results and results[c]["status"] != PASSED],
            }
        )

    failed = [i["id"] for i in items if i["status"] == FAILED]
    blocked = [i["id"] for i in items if i["status"] == BLOCKED]
    unverified = [i["id"] for i in items if i["status"] == SKIPPED]

    report = {
        "tool": "lifeai-founder-release-acceptance",
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "git_commit": _git_commit(),
        "branch": _git_branch(),
        "items": items,
        "checks": results,
        "summary": {
            "items_total": len(items),
            "items_passed": sum(1 for i in items if i["status"] == PASSED),
            "items_failed": failed,
            "items_blocked": blocked,
            "items_not_verified": unverified,
            "external_blocked": [i["id"] for i in items if i["evidence"] == "external_blocked"],
        },
        # Two separate answers, because they are two separate questions.
        "ready_for_private_founder_use": not failed and not blocked and not unverified,
        "ready_without_cognitive_core": not failed and set(blocked) <= {"4.cognitive_reasoning"},
        "first_cycle_go": False,
        "authorization": "founder_authorization_required",
        "note": "A passing suite is not a passing release. Read each item's evidence field: "
        "external_blocked items were not executed at all.",
    }
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    for item in items:
        print(f"[{item['status']:7}] {item['id']:22} ({item['evidence']}) {item['title']}")
    print()
    print(f"ready_for_private_founder_use: {report['ready_for_private_founder_use']}")
    print(f"ready_without_cognitive_core:  {report['ready_without_cognitive_core']}")
    if blocked:
        print(f"blocked: {', '.join(blocked)}")
    if unverified:
        print(f"not verified: {', '.join(unverified)}")
    print(f"report: {args.out}")

    if failed:
        return 1
    if blocked or unverified:
        return 2
    return 0


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _git_commit() -> str | None:
    return _git("rev-parse", "HEAD")


def _git_branch() -> str | None:
    return _git("rev-parse", "--abbrev-ref", "HEAD")


if __name__ == "__main__":
    raise SystemExit(main())
