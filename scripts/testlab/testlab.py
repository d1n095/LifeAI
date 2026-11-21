#!/usr/bin/env python3
"""LifeAI V7 test-lab controller.

One command for an isolated, disposable Docker environment and the acceptance evidence:

    python scripts/testlab/testlab.py preflight
    python scripts/testlab/testlab.py up
    python scripts/testlab/testlab.py acceptance
    python scripts/testlab/testlab.py soak --duration-seconds 14400
    python scripts/testlab/testlab.py gate
    python scripts/testlab/testlab.py down

Safety is structural:
  * only docker-compose.testlab.yml is ever used;
  * the Compose project name is fixed to lifeai-v6-testlab;
  * a conflicting COMPOSE_PROJECT_NAME aborts;
  * teardown removes only this project's containers and named volumes.

V7 additions: preflight diagnostics, model readiness, complete evidence collection even when
one acceptance leg fails, a host `--no-docker` soak mode, and deterministic `all` teardown.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from preflight import run_preflight  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "docker-compose.testlab.yml"
PROJECT = "lifeai-v6-testlab"
EVIDENCE = Path(
    os.environ.get("TESTLAB_EVIDENCE_DIR", str(ROOT / "delivery" / "v6-testlab" / "evidence"))
).resolve()
BACKEND_URL = os.environ.get("TESTLAB_BACKEND_URL", "http://127.0.0.1:18000").rstrip("/")
FRONTEND_URL = os.environ.get("TESTLAB_FRONTEND_URL", "http://127.0.0.1:13000").rstrip("/")
OLLAMA_URL = os.environ.get("TESTLAB_OLLAMA_URL", "http://127.0.0.1:11435").rstrip("/")
DATABASE_URL = os.environ.get(
    "TESTLAB_DATABASE_URL",
    "postgresql://lifeai:lifeai_testlab@127.0.0.1:55433/lifeai_testlab",
)
REDIS_URL = os.environ.get("TESTLAB_REDIS_URL", "redis://127.0.0.1:56380/0")
CHAT_MODEL = os.environ.get("OLLAMA_CHAT_MODEL", "qwen2.5:0.5b")
EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "all-minilm")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guard() -> None:
    if not COMPOSE_FILE.exists():
        raise SystemExit(f"missing {COMPOSE_FILE}")
    configured = os.environ.get("COMPOSE_PROJECT_NAME")
    if configured and configured != PROJECT:
        raise SystemExit(
            f"refusing to run: COMPOSE_PROJECT_NAME={configured!r} is not the isolated {PROJECT!r}"
        )


def _run(
    cmd: list[str],
    *,
    cwd: Path = ROOT,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    try:
        result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, env=env)
    except FileNotFoundError as exc:
        result = subprocess.CompletedProcess(cmd, 127, "", f"{type(exc).__name__}: {exc}")
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)
    if check and result.returncode != 0:
        raise SystemExit(result.returncode)
    return result


def _compose(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return _run(["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args], check=check)


def _http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _wait_http(url: str, *, timeout: float = 240.0, label: str = "") -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _http_ok(url):
            return True
        time.sleep(1)
    print(f"timed out waiting for {label or url}", file=sys.stderr)
    return False


def _service_running(service: str) -> bool:
    result = _compose(["ps", "--status", "running", "--services"], check=False)
    return service in {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _installed_models() -> set[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=10) as response:
            payload = json.loads(response.read())
    except Exception:
        return set()
    return {str(model.get("name", "")) for model in payload.get("models", []) if model.get("name")}


def model_installed(configured: str, installed: set[str]) -> bool:
    """Accept an untagged config name only when Ollama reports the :latest tag."""
    return configured in installed or (":" not in configured and f"{configured}:latest" in installed)


def _wait_models(*, timeout: float = 300.0) -> None:
    pending = {CHAT_MODEL, EMBED_MODEL}
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        installed = _installed_models()
        pending = {model for model in pending if not model_installed(model, installed)}
        if pending:
            time.sleep(2)
    if pending:
        raise SystemExit(f"Ollama models not ready: {', '.join(sorted(pending))}")


def _wait_services() -> None:
    deadline = time.monotonic() + 300
    checks = {
        "postgres": lambda: _compose(
            ["exec", "-T", "postgres", "pg_isready", "-U", "lifeai", "-d", "lifeai_testlab"],
            check=False,
        ).returncode
        == 0,
        "redis": lambda: _compose(
            ["exec", "-T", "redis", "redis-cli", "ping"], check=False
        ).stdout.strip()
        == "PONG",
        "ollama": lambda: _http_ok(f"{OLLAMA_URL}/api/tags"),
        "backend": lambda: _http_ok(f"{BACKEND_URL}/api/health"),
        "frontend": lambda: _http_ok(f"{FRONTEND_URL}/login"),
        "worker": lambda: _service_running("worker"),
    }
    pending = set(checks)
    while pending and time.monotonic() < deadline:
        for name in list(pending):
            try:
                if checks[name]():
                    pending.remove(name)
                    print(f"ready: {name}", flush=True)
            except Exception:
                pass
        if pending:
            time.sleep(2)
    if pending:
        _compose(["ps"], check=False)
        _compose(["logs", "--tail=80", *sorted(pending)], check=False)
        raise SystemExit(f"services not ready: {', '.join(sorted(pending))}")


def _migration_status() -> dict:
    current = _compose(["exec", "-T", "backend", "alembic", "current"], check=False)
    heads = _compose(["exec", "-T", "backend", "alembic", "heads"], check=False)
    return {
        "current_returncode": current.returncode,
        "current": current.stdout.strip(),
        "heads_returncode": heads.returncode,
        "heads": heads.stdout.strip(),
        "single_head": heads.stdout.count("(head)") == 1,
    }


def _write_status() -> dict:
    installed = _installed_models()
    status = {
        "generated_at": _now(),
        "project": PROJECT,
        "compose_file": str(COMPOSE_FILE),
        "services": {
            "postgres": _service_running("postgres"),
            "redis": _service_running("redis"),
            "ollama": _service_running("ollama"),
            "backend": _service_running("backend"),
            "worker": _service_running("worker"),
            "frontend": _service_running("frontend"),
        },
        "http": {
            "backend_health": _http_ok(f"{BACKEND_URL}/api/health"),
            "frontend_login": _http_ok(f"{FRONTEND_URL}/login"),
            "ollama_tags": _http_ok(f"{OLLAMA_URL}/api/tags"),
        },
        "migration": _migration_status(),
        "models": {
            "chat": CHAT_MODEL,
            "embedding": EMBED_MODEL,
            "chat_installed": model_installed(CHAT_MODEL, installed),
            "embedding_installed": model_installed(EMBED_MODEL, installed),
        },
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "testlab-status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    return status


def _write_preflight(report: dict) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "preflight.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def _require_preflight(*, mode: str = "docker", include_browser: bool = False) -> dict:
    report = run_preflight(mode=mode, include_browser=include_browser)
    _write_preflight(report)
    if report["status"] != "PASS":
        print(json.dumps(report["blockers"], indent=2), file=sys.stderr)
        raise SystemExit(3)
    return report


def cmd_preflight(args: argparse.Namespace) -> None:
    report = run_preflight(mode=args.mode, include_browser=not args.no_browser)
    out = Path(args.out) if args.out else EVIDENCE / "preflight.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["status"] == "PASS" else 3)


def cmd_up(args: argparse.Namespace) -> None:
    _guard()
    _require_preflight(mode="docker", include_browser=False)
    try:
        _compose(["up", "-d", *(["--no-build"] if args.no_build else ["--build"])])
        _wait_services()
        if args.pull_models:
            for model in (EMBED_MODEL, CHAT_MODEL):
                _compose(["exec", "-T", "ollama", "ollama", "pull", model])
        _wait_models(timeout=300 if args.pull_models else 15)
        status = _write_status()
        print(json.dumps(status, indent=2))
    except BaseException:
        _compose(["down", "-v", "--remove-orphans", "--timeout", "30"], check=False)
        raise


def cmd_down(args: argparse.Namespace) -> None:
    _guard()
    _compose(["down", "-v", "--remove-orphans", "--timeout", "30"])
    leftovers = _compose(["ps", "-q"], check=False).stdout.strip()
    if leftovers:
        raise SystemExit(f"teardown left containers behind: {leftovers}")
    print("test lab removed; no production volumes were referenced")


def cmd_status(args: argparse.Namespace) -> None:
    _guard()
    print(json.dumps(_write_status(), indent=2))


def cmd_pull_models(args: argparse.Namespace) -> None:
    _guard()
    for model in (EMBED_MODEL, CHAT_MODEL):
        _compose(["exec", "-T", "ollama", "ollama", "pull", model])
    _wait_models()


def _evidence_path(name: str) -> Path:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    return EVIDENCE / name


def failed_legs(results: list[tuple[str, int]]) -> list[str]:
    return [f"{name} (exit {code})" for name, code in results if code != 0]


def _leg(
    name: str,
    cmd: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> tuple[str, int]:
    result = _run(cmd, cwd=cwd, env=env, check=False)
    return name, result.returncode


def cmd_acceptance(args: argparse.Namespace) -> None:
    _guard()
    legs: list[tuple[str, int]] = []
    journey = _evidence_path("founder-journey.json")
    journey_cmd = [
        sys.executable,
        "scripts/acceptance/v6_founder_journey.py",
        "--base-url",
        BACKEND_URL,
        "--frontend-url",
        FRONTEND_URL,
        "--database-url",
        DATABASE_URL,
        "--redis-url",
        REDIS_URL,
        "--evidence",
        str(journey),
    ]
    if args.allow_fake_provider:
        journey_cmd.append("--allow-fake-provider")
    legs.append(_leg("journey", journey_cmd, cwd=ROOT / "backend"))

    if not args.skip_playwright:
        env = {
            **os.environ,
            "E2E_FRONTEND_URL": FRONTEND_URL,
            "V6_PLAYWRIGHT_JSON": str(_evidence_path("playwright-founder-v6.json")),
            "CI": "1",
        }
        legs.append(
            _leg(
                "playwright",
                ["npx", "playwright", "test", "--config=playwright.testlab.config.ts"],
                cwd=ROOT / "frontend",
                env=env,
            )
        )

    legs.append(
        _leg(
            "provider_egress",
            [
                sys.executable,
                "scripts/acceptance/v6_provider_egress.py",
                "--base-url",
                BACKEND_URL,
                "--compose-file",
                str(COMPOSE_FILE),
                "--project-name",
                PROJECT,
                "--out",
                str(_evidence_path("provider-egress.json")),
            ],
            cwd=ROOT,
        )
    )

    junit_cmd = [
        "docker",
        "compose",
        "-p",
        PROJECT,
        "-f",
        str(COMPOSE_FILE),
        "exec",
        "-T",
        "-e",
        "DATABASE_URL=postgresql://lifeai:lifeai_testlab@postgres:5432/lifeai_v6_tests",
        "-e",
        "APP_DATABASE_URL=postgresql://mainai_app:mainai_app_testlab@postgres:5432/lifeai_v6_tests",
        "-e",
        "LIFEAI_TEST_DATABASE_NAME=lifeai_v6_tests",
        "-e",
        "REDIS_URL=redis://redis:6379/15",
        "-e",
        "LIFEAI_TEST_REDIS_DB=15",
        "-e",
        "OPENAI_API_KEY=fake-key-for-tests",
        "-e",
        "STORAGE_ROOT=/data/storage",
        "backend",
        "python",
        "-m",
        "pytest",
        "-q",
        "tests/backend/providers/test_r15_integration_security.py",
        "tests/backend/test_rls_policy_registry.py",
        "--junitxml=/data/storage/backend-junit.xml",
    ]
    junit = _run(junit_cmd, cwd=ROOT, check=False)
    copied = _compose(
        ["cp", "backend:/data/storage/backend-junit.xml", str(_evidence_path("backend-junit.xml"))],
        check=False,
    )
    if junit.returncode != 0 or copied.returncode != 0:
        legs.append(("backend_junit", junit.returncode or copied.returncode))

    failures = failed_legs(legs)
    if failures:
        print("acceptance legs failed: " + ", ".join(failures), file=sys.stderr)
        raise SystemExit(1)


def cmd_soak(args: argparse.Namespace) -> None:
    _guard()
    if not args.no_docker:
        _require_preflight(mode="docker", include_browser=False)
    cmd = [
        sys.executable,
        "scripts/acceptance/v6_reliability.py",
        "--base-url",
        BACKEND_URL,
        "--database-url",
        DATABASE_URL,
        "--redis-url",
        REDIS_URL,
        "--compose-file",
        str(COMPOSE_FILE),
        "--project-name",
        PROJECT,
        "--duration-seconds",
        str(args.duration_seconds),
        "--sample-every-seconds",
        str(args.sample_every_seconds),
        "--fault-every-seconds",
        str(args.fault_every_seconds),
        "--ollama-url",
        OLLAMA_URL,
        "--out",
        str(_evidence_path("reliability.json")),
    ]
    if args.no_docker:
        cmd.append("--no-docker")
    _run(cmd, cwd=ROOT / "backend")


def cmd_gate(args: argparse.Namespace) -> None:
    _guard()
    _run(
        [
            sys.executable,
            "scripts/acceptance/v6_release_gate.py",
            "--expected-sha",
            _run(["git", "rev-parse", "HEAD"], cwd=ROOT).stdout.strip(),
            "--evidence-dir",
            str(EVIDENCE),
            "--database-url",
            DATABASE_URL,
            "--redis-url",
            REDIS_URL,
            "--base-url",
            BACKEND_URL,
            "--frontend-url",
            FRONTEND_URL,
            "--out-dir",
            str(EVIDENCE),
        ],
        cwd=ROOT / "backend",
    )


def cmd_all(args: argparse.Namespace) -> None:
    had_error = False
    try:
        cmd_up(args)
        cmd_acceptance(args)
        cmd_soak(args)
        cmd_gate(args)
    except BaseException:
        had_error = True
        raise
    finally:
        if not args.keep:
            try:
                cmd_down(args)
            except SystemExit as exc:
                if not had_error:
                    raise
                print(f"teardown failed after the primary error: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser("preflight", help="diagnose Docker, host deps and browser libs")
    preflight.add_argument("--mode", choices=("docker", "host"), default="docker")
    preflight.add_argument("--no-browser", action="store_true")
    preflight.add_argument("--out", default="")
    preflight.set_defaults(func=cmd_preflight)

    up = sub.add_parser("up", help="build/start the isolated lab and pull models")
    up.add_argument("--no-build", action="store_true")
    up.add_argument("--no-pull-models", dest="pull_models", action="store_false")
    up.set_defaults(func=cmd_up, pull_models=True)

    down = sub.add_parser("down", help="remove containers and volumes")
    down.set_defaults(func=cmd_down)

    status = sub.add_parser("status", help="write machine-readable service/migration status")
    status.set_defaults(func=cmd_status)

    pull = sub.add_parser("pull-models", help="pull the configured local Ollama models")
    pull.set_defaults(func=cmd_pull_models)

    acceptance = sub.add_parser("acceptance", help="run the founder journey, browser, egress and JUnit")
    acceptance.add_argument("--skip-playwright", action="store_true")
    acceptance.add_argument("--allow-fake-provider", action="store_true")
    acceptance.set_defaults(func=cmd_acceptance)

    soak = sub.add_parser("soak", help="run the fault-injecting reliability runner")
    soak.add_argument("--duration-seconds", type=int, default=4 * 60 * 60)
    soak.add_argument("--sample-every-seconds", type=int, default=30)
    soak.add_argument("--fault-every-seconds", type=int, default=300)
    soak.add_argument("--no-docker", action="store_true", help="API-only smoke; Docker faults are BLOCKED")
    soak.set_defaults(func=cmd_soak)

    gate = sub.add_parser("gate", help="run the fail-closed release gate")
    gate.set_defaults(func=cmd_gate)

    all_cmd = sub.add_parser("all", help="up + acceptance + soak + gate, then deterministic teardown")
    all_cmd.add_argument("--duration-seconds", type=int, default=4 * 60 * 60)
    all_cmd.add_argument("--sample-every-seconds", type=int, default=30)
    all_cmd.add_argument("--fault-every-seconds", type=int, default=300)
    all_cmd.add_argument("--skip-playwright", action="store_true")
    all_cmd.add_argument("--allow-fake-provider", action="store_true")
    all_cmd.add_argument("--no-docker", action="store_true")
    all_cmd.add_argument("--keep", action="store_true")
    all_cmd.set_defaults(func=cmd_all, no_build=False, pull_models=True)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
