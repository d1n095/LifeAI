"""Static security analysis of settings, manifests, permissions and local diffs."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.mainai_security.leak import scan_text_for_leaks

_SENSITIVE_PATH_MARKERS = (
    ".env",
    "secret",
    "credential",
    "id_rsa",
    "id_ed25519",
    "security.py",
    "deps.py",
    "rls.py",
    "alembic/",
    "auth.py",
    "csrf.py",
    "limiter.py",
    "privilege",
    "github_client.py",
    "package-lock.json",
    "requirements.txt",
)

_SUSPICIOUS_DEP_PREFIXES = ("git+http://", "http://", "git://")
_KNOWN_TYPOSQUATS = {"lodahs", "reac", "reactt", "expresss", "webpack-dev-servr"}
_DEFAULT_SECRET = "change-me-in-production"


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "frontend" / "package.json").exists() and (parent / "backend").is_dir():
            return parent
    return here.parents[2]


def analyze_settings(*, environment_override: str | None = None) -> list[dict[str, Any]]:
    settings = get_settings()
    environment = environment_override or settings.environment
    findings: list[dict[str, Any]] = []

    def _add(**kwargs: Any) -> None:
        findings.append(
            {
                "status": "open",
                "kind": kwargs.pop("kind"),
                "confidence": kwargs.pop("confidence", "confirmed"),
                **kwargs,
            }
        )

    if settings.secret_key == _DEFAULT_SECRET:
        _add(
            id="sec_cfg_default_secret_key",
            kind="unsafe_configuration",
            category="authentication",
            severity="critical" if environment == "production" else "high",
            source="app.config.Settings.secret_key",
            component="authentication",
            evidence={"setting": "secret_key", "uses_default": True, "raw_copied": False},
            recommended_action="Set a unique SECRET_KEY outside the repository. Do not commit it.",
        )
    if settings.founder_password == _DEFAULT_SECRET:
        _add(
            id="sec_cfg_default_founder_password",
            kind="authentication_weakness",
            category="authentication",
            severity="critical" if environment == "production" else "high",
            source="app.config.Settings.founder_password",
            component="authentication",
            evidence={"setting": "founder_password", "uses_default": True, "raw_copied": False},
            recommended_action="Set FOUNDER_PASSWORD in the operator secret store. Require Founder approval to rotate it.",
        )
    if environment == "production" and not settings.cookie_secure:
        _add(
            id="sec_cfg_insecure_cookie",
            kind="unsafe_configuration",
            category="authentication",
            severity="high",
            source="app.config.Settings.cookie_secure",
            component="authentication",
            evidence={"setting": "cookie_secure", "value": False},
            recommended_action="Keep cookie_secure=true in production.",
        )
    if settings.github_write_enabled:
        _add(
            id="sec_cfg_github_writes_on",
            kind="insecure_permission",
            category="network",
            severity="high",
            source="app.config.Settings.github_write_enabled",
            component="github_transport",
            evidence={"setting": "github_write_enabled", "value": True},
            recommended_action="Leave GitHub writes off until the Founder approves a specific write profile.",
        )
    if settings.github_auto_merge_enabled:
        _add(
            id="sec_cfg_auto_merge_on",
            kind="insecure_permission",
            category="policy",
            severity="high",
            source="app.config.Settings.github_auto_merge_enabled",
            component="github_transport",
            evidence={"setting": "github_auto_merge_enabled", "value": True},
            recommended_action="Keep automatic merge disabled. Merge remains a Founder decision.",
        )
    if settings.mainai_allow_live_checkout_writes:
        _add(
            id="sec_cfg_live_checkout_writes",
            kind="insecure_permission",
            category="permission",
            severity="high",
            source="app.config.Settings.mainai_allow_live_checkout_writes",
            component="execution",
            evidence={"setting": "mainai_allow_live_checkout_writes", "value": True},
            recommended_action="Keep live-checkout writes off unless the Founder authorizes a one-off change.",
        )
    if settings.mainai_activation_mode:
        _add(
            id="sec_cfg_activation_mode",
            kind="security_regression",
            category="policy",
            severity="medium",
            source="app.config.Settings.mainai_activation_mode",
            component="first_cycle",
            evidence={"setting": "mainai_activation_mode", "value": True},
            recommended_action="Activation mode is a Founder-supervised switch. It does not itself authorize first-cycle GO.",
        )
    if settings.access_token_expire_minutes > 60:
        _add(
            id="sec_cfg_long_access_token",
            kind="authentication_weakness",
            category="authentication",
            severity="medium",
            confidence="uncertain",
            source="app.config.Settings.access_token_expire_minutes",
            component="authentication",
            evidence={"setting": "access_token_expire_minutes", "value": settings.access_token_expire_minutes},
            recommended_action="Keep access tokens short-lived. The current documented default is 15 minutes.",
        )
    return findings


def analyze_dependencies(manifest: dict[str, Any] | None = None, *, source: str = "frontend/package.json") -> list[dict[str, Any]]:
    payload = manifest if manifest is not None else _read_package_json()
    if payload is None:
        return [
            {
                "id": "sec_dep_manifest_unreadable",
                "kind": "vulnerable_dependency",
                "category": "installation",
                "severity": "low",
                "confidence": "uncertain",
                "source": source,
                "component": "frontend",
                "evidence": {"readable": False},
                "recommended_action": "Could not read the package manifest. Re-run from the repository checkout.",
                "status": "open",
            }
        ]
    findings: list[dict[str, Any]] = []
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        deps = payload.get(section) or {}
        if not isinstance(deps, dict):
            continue
        for name, spec in deps.items():
            spec_text = str(spec)
            if name in _KNOWN_TYPOSQUATS or spec_text.startswith(_SUSPICIOUS_DEP_PREFIXES) or spec_text == "*":
                findings.append(
                    {
                        "id": f"sec_dep_{name}",
                        "kind": "malicious_or_unpinned_dependency",
                        "category": "installation",
                        "severity": "high",
                        "confidence": "confirmed" if name in _KNOWN_TYPOSQUATS or spec_text.startswith("git+http://") else "uncertain",
                        "source": source,
                        "component": name,
                        "evidence": {"section": section, "spec": spec_text, "typosquat": name in _KNOWN_TYPOSQUATS},
                        "recommended_action": "Do not install this dependency. Require Founder approval before any package installation.",
                        "status": "open",
                    }
                )
    lockfile = repo_root() / "frontend" / "package-lock.json"
    if manifest is None and not lockfile.exists():
        findings.append(
            {
                "id": "sec_dep_missing_lockfile",
                "kind": "vulnerable_dependency",
                "category": "installation",
                "severity": "medium",
                "confidence": "confirmed",
                "source": "frontend/package-lock.json",
                "component": "frontend",
                "evidence": {"lockfile_present": False},
                "recommended_action": "Restore the lockfile. npm audit policy lives in frontend/scripts/check-npm-audit.js and is not reimplemented here.",
                "status": "open",
            }
        )
    findings.append(
        {
            "id": "sec_dep_policy_engine",
            "kind": "vulnerable_dependency",
            "category": "installation",
            "severity": "informational",
            "confidence": "confirmed",
            "source": "frontend/scripts/check-npm-audit.js",
            "component": "frontend",
            "evidence": {"policy_engine": "fc4_check_npm_audit", "live_audit_ran": False},
            "recommended_action": "Use the existing FC-4 npm-audit policy. This module does not install packages or change that policy.",
            "status": "observed",
        }
    )
    return findings


def analyze_python_requirements(text: str | None = None, *, source: str = "backend/requirements.txt") -> list[dict[str, Any]]:
    raw = text if text is not None else _read_text(repo_root() / "backend" / "requirements.txt")
    if raw is None:
        return []
    findings: list[dict[str, Any]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lower = stripped.lower()
        if (
            lower.startswith(("git+http://", "http://"))
            or lower.endswith("==*")
            or "@http://" in lower
            or "@ http://" in lower
            or "http://" in lower
        ):
            findings.append(
                {
                    "id": f"sec_pydep_{stripped.split('=')[0].split('@')[0]}",
                    "kind": "malicious_or_unpinned_dependency",
                    "category": "installation",
                    "severity": "high",
                    "confidence": "confirmed",
                    "source": source,
                    "component": "backend",
                    "evidence": {"spec": stripped, "raw_copied": False},
                    "recommended_action": "Reject unpinned or plaintext-HTTP Python dependencies. Installation requires Founder approval.",
                    "status": "open",
                }
            )
    return findings


def analyze_committed_text(*, paths: list[Path] | None = None) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path in paths or _default_scan_paths():
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(path)
        findings.extend(scan_text_for_leaks(text, source=rel))
    return findings


def analyze_suspicious_changes(*, names: list[str] | None = None) -> list[dict[str, Any]]:
    changed = names if names is not None else _git_changed_names()
    findings: list[dict[str, Any]] = []
    for name in changed:
        if not any(marker in name for marker in _SENSITIVE_PATH_MARKERS):
            continue
        findings.append(
            {
                "id": f"sec_diff_{name.replace('/', '_')}",
                "kind": "suspicious_code_change",
                "category": "regression",
                "severity": "medium",
                "confidence": "uncertain",
                "source": name,
                "component": name,
                "evidence": {"path": name, "patch_included": False},
                "recommended_action": "Review the change in an isolated workspace. Do not apply it to the live checkout without Founder approval.",
                "status": "open",
            }
        )
    return findings


def analyze_file_permissions(*, paths: list[Path] | None = None) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path in paths or _default_scan_paths():
        if not path.exists():
            continue
        mode = path.stat().st_mode
        if mode & 0o002:
            findings.append(
                {
                    "id": f"sec_perm_{path.name}",
                    "kind": "insecure_permission",
                    "category": "permission",
                    "severity": "high",
                    "confidence": "confirmed",
                    "source": str(path),
                    "component": str(path),
                    "evidence": {"mode": oct(mode & 0o777), "world_writable": True},
                    "recommended_action": "Remove world-writable permission. Permission changes require Founder approval.",
                    "status": "open",
                }
            )
    return findings


def collect_analysis_findings(**overrides: Any) -> list[dict[str, Any]]:
    findings = []
    findings.extend(analyze_settings(environment_override=overrides.get("environment")))
    findings.extend(analyze_dependencies(overrides.get("manifest")))
    findings.extend(analyze_python_requirements(overrides.get("requirements_text")))
    findings.extend(analyze_committed_text(paths=overrides.get("scan_paths")))
    findings.extend(analyze_suspicious_changes(names=overrides.get("changed_names")))
    findings.extend(analyze_file_permissions(paths=overrides.get("permission_paths")))
    return findings


def _read_package_json() -> dict[str, Any] | None:
    path = repo_root() / "frontend" / "package.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _default_scan_paths() -> list[Path]:
    root = repo_root()
    candidates = [
        root / ".env.example",
        root / "frontend" / "package.json",
        root / "backend" / "requirements.txt",
    ]
    return [path for path in candidates if path.is_file()]


def _git_changed_names() -> list[str]:
    root = repo_root()
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
