#!/usr/bin/env python3
"""Portable preflight diagnostics for the LifeAI integration test lab.

The preflight never claims a missing prerequisite is working. It returns PASS only when
every required host capability is present, otherwise BLOCKED with an exact remediation.
It is safe to run before the Docker lab exists and never touches production resources.
"""

from __future__ import annotations

import argparse
import ctypes.util
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "docker-compose.testlab.yml"
PROJECT = "lifeai-v6-testlab"
EVIDENCE = Path(
    os.environ.get("TESTLAB_EVIDENCE_DIR", str(ROOT / "delivery" / "v6-testlab" / "evidence"))
).resolve()
BACKEND_URL = os.environ.get("TESTLAB_BACKEND_URL", "http://127.0.0.1:18000").rstrip("/")
FRONTEND_URL = os.environ.get("TESTLAB_FRONTEND_URL", "http://127.0.0.1:13000").rstrip("/")
OLLAMA_URL = os.environ.get("TESTLAB_OLLAMA_URL", "http://127.0.0.1:11435").rstrip("/")
PORTS = (55433, 56380, 11435, 18000, 13000)
PLAYWRIGHT_LIBS = ("atk-1.0", "atk-bridge-2.0", "gbm", "asound", "nss3", "xkbcommon")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 20) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(cmd, 124, "", f"timeout after {timeout}s: {exc}")


def _http_ok(url: str, timeout: float = 3.0) -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _port_free(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _playwright_cache() -> Path:
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured and configured != "0":
        return Path(configured)
    return Path.home() / ".cache" / "ms-playwright"


def run_preflight(*, mode: str = "docker", include_browser: bool = True) -> dict:
    if mode not in {"docker", "host"}:
        raise ValueError(f"unsupported preflight mode: {mode}")

    checks: dict[str, dict] = {}
    blockers: list[dict] = []
    warnings: list[dict] = []
    remediations: list[str] = []

    def add(
        name: str,
        status: str,
        detail: str,
        *,
        remediation: str | None = None,
    ) -> None:
        entry: dict = {"status": status, "detail": detail}
        if remediation:
            entry["remediation"] = remediation
            if remediation not in remediations:
                remediations.append(remediation)
        checks[name] = entry
        if status == "BLOCKED":
            blockers.append({"check": name, "detail": detail, "remediation": remediation})
        elif status == "WARN":
            warnings.append({"check": name, "detail": detail, "remediation": remediation})

    if sys.version_info < (3, 11):
        add(
            "python",
            "BLOCKED",
            f"Python {sys.version.split()[0]} is too old; >=3.11 required",
            remediation="install Python 3.11+ and recreate the virtualenv",
        )
    else:
        add("python", "PASS", sys.version.split()[0])

    for module in ("httpx", "psycopg2", "redis", "sqlalchemy"):
        if importlib.util.find_spec(module) is None:
            add(
                f"python_module:{module}",
                "BLOCKED",
                "missing host Python dependency",
                remediation="python3 -m pip install -r backend/requirements-dev.txt",
            )
        else:
            add(f"python_module:{module}", "PASS", "installed")

    if not COMPOSE_FILE.exists():
        add(
            "compose_file",
            "BLOCKED",
            f"missing {COMPOSE_FILE}",
            remediation="restore docker-compose.testlab.yml from the repository",
        )
    else:
        add("compose_file", "PASS", str(COMPOSE_FILE))

    docker = shutil.which("docker")
    if mode == "docker":
        if not docker:
            add(
                "docker_cli",
                "BLOCKED",
                "docker executable not found",
                remediation="install Docker Engine and the Compose plugin",
            )
        else:
            add("docker_cli", "PASS", docker)
            info = _run([docker, "info", "--format", "{{.ServerVersion}}"], timeout=10)
            if info is None or info.returncode != 0:
                detail = (info.stderr if info else "docker info could not run")[-500:]
                add(
                    "docker_daemon",
                    "BLOCKED",
                    detail,
                    remediation="start Docker and grant the current user access to /var/run/docker.sock",
                )
            else:
                add("docker_daemon", "PASS", info.stdout.strip() or "reachable")
            compose = _run([docker, "compose", "version"], timeout=10)
            if compose is None or compose.returncode != 0:
                add(
                    "docker_compose",
                    "BLOCKED",
                    "Docker Compose plugin unavailable",
                    remediation="install the Docker Compose v2 plugin",
                )
            else:
                add("docker_compose", "PASS", compose.stdout.strip())
            config = _run(
                [docker, "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), "config", "-q"],
                timeout=20,
            )
            if config is None or config.returncode != 0:
                add(
                    "compose_config",
                    "BLOCKED",
                    (config.stderr if config else "compose config could not run")[-500:],
                    remediation="fix docker-compose.testlab.yml",
                )
            else:
                add("compose_config", "PASS", "valid")
        for port in PORTS:
            if not _port_free(port):
                add(
                    f"port:{port}",
                    "WARN",
                    "already in use; the lab may already be running or another process owns it",
                    remediation=f"free 127.0.0.1:{port} or stop the conflicting process",
                )
            else:
                add(f"port:{port}", "PASS", "free")
    else:
        for name, url in (
            ("backend", f"{BACKEND_URL}/api/health"),
            ("frontend", f"{FRONTEND_URL}/login"),
            ("ollama", f"{OLLAMA_URL}/api/tags"),
        ):
            if _http_ok(url):
                add(name, "PASS", url)
            else:
                add(
                    name,
                    "BLOCKED",
                    f"unreachable: {url}",
                    remediation="start the isolated host services or use the Docker lab",
                )

    if include_browser:
        node = shutil.which("node")
        npm = shutil.which("npm")
        npx = shutil.which("npx")
        if not node or not npm or not npx:
            add(
                "node",
                "BLOCKED",
                f"node={bool(node)} npm={bool(npm)} npx={bool(npx)}",
                remediation="install Node.js 20+ (frontend/.nvmrc or CI setup-node)",
            )
        else:
            add("node", "PASS", node)
            frontend = ROOT / "frontend"
            playwright_pkg = frontend / "node_modules" / "@playwright" / "test"
            if not playwright_pkg.exists():
                add(
                    "playwright_package",
                    "BLOCKED",
                    f"missing {playwright_pkg}",
                    remediation="cd frontend && npm ci",
                )
            else:
                version = _run([npx, "playwright", "--version"], cwd=frontend, timeout=30)
                if version is None or version.returncode != 0:
                    add(
                        "playwright_cli",
                        "BLOCKED",
                        (version.stderr if version else "npx playwright could not run")[-400:],
                        remediation="cd frontend && npm ci && npx playwright install --with-deps chromium",
                    )
                else:
                    add("playwright_cli", "PASS", version.stdout.strip() or version.stderr.strip())
            cache = _playwright_cache()
            chromium = sorted(cache.glob("chromium-*")) if cache.exists() else []
            if not chromium:
                add(
                    "playwright_browser",
                    "BLOCKED",
                    f"Chromium browser cache missing: {cache}",
                    remediation="cd frontend && npx playwright install --with-deps chromium",
                )
            else:
                add("playwright_browser", "PASS", str(chromium[-1]))
            missing = []
            for lib in PLAYWRIGHT_LIBS:
                try:
                    if ctypes.util.find_library(lib) is None:
                        missing.append(lib)
                except Exception:
                    missing.append(lib)
            if missing:
                add(
                    "playwright_system_libs",
                    "BLOCKED",
                    "missing shared libraries: " + ", ".join(missing),
                    remediation=(
                        "cd frontend && npx playwright install --with-deps chromium "
                        "(requires root or sudo for apt packages)"
                    ),
                )
            else:
                add("playwright_system_libs", "PASS", "required shared libraries found")

    try:
        usage = shutil.disk_usage(ROOT)
        free_gb = usage.free / (1024**3)
        if free_gb < 5:
            add(
                "disk",
                "BLOCKED",
                f"{free_gb:.1f} GiB free; need at least 5 GiB",
                remediation="free disk space before building images and pulling models",
            )
        elif free_gb < 20:
            add("disk", "WARN", f"{free_gb:.1f} GiB free; models and images may need more")
        else:
            add("disk", "PASS", f"{free_gb:.1f} GiB free")
    except OSError as exc:
        add("disk", "WARN", str(exc))

    try:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        probe = EVIDENCE / ".preflight-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("evidence_dir", "PASS", str(EVIDENCE))
    except OSError as exc:
        add(
            "evidence_dir",
            "BLOCKED",
            f"not writable: {EVIDENCE}: {exc}",
            remediation="create a writable delivery/evidence directory",
        )

    status = "BLOCKED" if blockers else "PASS"
    return {
        "generated_at": _now(),
        "mode": mode,
        "status": status,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "remediations": remediations,
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "docker": docker,
            "backend_url": BACKEND_URL,
            "frontend_url": FRONTEND_URL,
            "ollama_url": OLLAMA_URL,
            "evidence_dir": str(EVIDENCE),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("docker", "host"), default="docker")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    payload = run_preflight(mode=args.mode, include_browser=not args.no_browser)
    out = Path(args.out) if args.out else EVIDENCE / "preflight.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0 if payload["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
