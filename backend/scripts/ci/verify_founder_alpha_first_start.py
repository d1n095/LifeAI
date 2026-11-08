#!/usr/bin/env python3
"""Read-only preflight for the isolated Founder Alpha first-start rehearsal.

This proves repository identity and, when an env file is supplied, the deliberately local,
synthetic configuration. It does not start services, contact providers, certify security,
authorize a merge/deploy/import, or turn ``first_cycle_go`` true.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "docs" / "founder_alpha_first_start_manifest.json"
MIGRATIONS = ROOT / "backend" / "alembic" / "versions"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
PAID_KEYS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=False)


def _env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"line {number} is not KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if key in values:
            raise ValueError(f"duplicate key {key!r}")
        values[key] = value.strip().strip('"').strip("'")
    return values


def _revision_values(path: Path) -> tuple[str | None, str | None]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, str | None] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                found[target.id] = ast.literal_eval(node.value)
    return found.get("revision"), found.get("down_revision")


def _migration_map() -> dict[str, str | None]:
    revisions: dict[str, str | None] = {}
    for path in MIGRATIONS.glob("*.py"):
        revision, parent = _revision_values(path)
        if revision:
            revisions[revision] = parent
    return revisions


def _local_url(values: dict[str, str], key: str, failures: list[str]) -> None:
    value = values.get(key, "")
    host = urlsplit(value).hostname
    if host not in LOCAL_HOSTS:
        failures.append(f"{key} must use an isolated local host, got {host!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, help="synthetic rehearsal env file")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    failures: list[str] = []
    unknown: list[str] = []

    head = _git("rev-parse", "HEAD")
    head_sha = head.stdout.strip() if head.returncode == 0 else "unknown"
    source = manifest["integration_sha"]
    ancestry = _git("merge-base", "--is-ancestor", source, "HEAD").returncode == 0
    # actions/checkout intentionally uses a shallow checkout in the main backend job. In that
    # shape HEAD's commit object still contains its exact parent SHA, but the parent object is
    # absent, so merge-base and git-show cannot traverse/read it. An exact direct-parent match is
    # still cryptographic Git evidence: the child commit was created with this immutable parent
    # object id. Accept only that exact relationship; never infer ancestry from a branch name,
    # GitHub environment variable, manifest assertion, or missing history.
    parents = _git("show", "-s", "--format=%P", "HEAD")
    direct_parent = parents.returncode == 0 and source in parents.stdout.split()
    source_bound = ancestry or direct_parent
    if not source_bound:
        failures.append(f"integration SHA {source} is not an ancestor of HEAD")
    tree = _git("show", "-s", "--format=%T", source)
    if tree.returncode == 0:
        actual_tree = tree.stdout.strip()
        tree_evidence = "source_object_recomputed"
        tree_valid = actual_tree == manifest["integration_tree"]
    elif direct_parent:
        # The exact commit id already binds its tree id. We cannot print/recompute that tree in
        # a depth-1 checkout, so report the evidence form explicitly rather than pretending the
        # object was locally available. A changed tree necessarily has a different commit id and
        # cannot satisfy the exact direct-parent comparison above.
        actual_tree = "bound_by_exact_parent_commit_identity"
        tree_evidence = "exact_direct_parent_commit_identity"
        tree_valid = True
    else:
        actual_tree = "unknown"
        tree_evidence = "unavailable"
        tree_valid = False
    if not tree_valid:
        failures.append("integration tree does not match the frozen manifest")

    revisions = _migration_map()
    chain = manifest["migration_chain"]
    for parent, child in zip(chain, chain[1:]):
        if revisions.get(child) != parent:
            failures.append(
                f"migration {child} parent is {revisions.get(child)!r}, expected {parent!r}"
            )
    children = {parent for parent in revisions.values() if parent}
    heads = sorted(set(revisions) - children)
    if heads != [manifest["expected_migration_head"]]:
        failures.append(f"migration heads are {heads!r}, expected one head")

    config_checked = args.env_file is not None
    if args.env_file is None:
        unknown.append("synthetic rehearsal environment was not supplied")
    else:
        try:
            values = _env(args.env_file)
        except (OSError, ValueError) as exc:
            failures.append(f"cannot parse env file: {exc}")
            values = {}
        required = (
            "DATABASE_URL",
            "APP_DATABASE_URL",
            "REDIS_URL",
            "SECRET_KEY",
            "FOUNDER_EMAIL",
            "FOUNDER_PASSWORD",
            "FRONTEND_ORIGINS",
            "PUBLIC_APP_URL",
            "DEFAULT_LLM_PROVIDER",
            "DEFAULT_LLM_MODEL",
            "DEFAULT_EMBEDDING_PROVIDER",
            "DEFAULT_EMBEDDING_MODEL",
            "OLLAMA_BASE_URL",
            "STORAGE_ROOT",
            "FIRST_START_ISOLATED_REHEARSAL",
            "FIRST_START_SYNTHETIC_DATA_ONLY",
        )
        missing = [key for key in required if not values.get(key)]
        if missing:
            failures.append(f"missing non-empty rehearsal variables: {missing}")
        for key in ("DATABASE_URL", "APP_DATABASE_URL", "REDIS_URL", "OLLAMA_BASE_URL"):
            _local_url(values, key, failures)
        if values.get("DEFAULT_LLM_PROVIDER") != "ollama":
            failures.append("DEFAULT_LLM_PROVIDER must be ollama")
        if values.get("DEFAULT_EMBEDDING_PROVIDER") != "ollama":
            failures.append("DEFAULT_EMBEDDING_PROVIDER must be ollama")
        if values.get("CHAT_FALLBACK_ORDER", "missing") != "":
            failures.append("CHAT_FALLBACK_ORDER must be empty")
        if any(values.get(key) for key in PAID_KEYS):
            failures.append("paid-provider keys must be absent/empty")
        for key in ("GITHUB_WRITE_ENABLED", "GITHUB_AUTO_MERGE_ENABLED"):
            if values.get(key, "false").lower() != "false":
                failures.append(f"{key} must be false")
        if values.get("FIRST_START_ISOLATED_REHEARSAL", "").lower() != "true":
            failures.append("FIRST_START_ISOLATED_REHEARSAL must be true")
        if values.get("FIRST_START_SYNTHETIC_DATA_ONLY", "").lower() != "true":
            failures.append("FIRST_START_SYNTHETIC_DATA_ONLY must be true")
        storage = Path(values.get("STORAGE_ROOT", "."))
        if not storage.is_absolute() or str(storage) in {"/", "/var/lib/lifeai/uploads"}:
            failures.append("STORAGE_ROOT must be an explicit isolated absolute rehearsal path")

    if manifest.get("first_cycle_go") is not False:
        failures.append("first_cycle_go must remain false")
    if manifest.get("startup_authorized") is not False:
        failures.append("startup_authorized must remain false")
    unresolved_reviews = [g for g in manifest["review_gates"] if g["status"] != "pass"]
    if unresolved_reviews:
        unknown.append("independent exact-SHA reviews remain unresolved")

    report = {
        "schema_version": manifest["schema_version"],
        "head": head_sha,
        "integration_sha": source,
        "integration_tree": actual_tree,
        "expected_integration_tree": manifest["integration_tree"],
        "source_binding_evidence": "full_ancestry" if ancestry else (
            "exact_direct_parent" if direct_parent else "unverified"
        ),
        "tree_binding_evidence": tree_evidence,
        "repository_valid": not failures,
        "configuration_checked": config_checked,
        "rehearsal_preflight": "GO" if not failures and not unknown else "NO-GO",
        "first_cycle_go": False,
        "startup_authorized": False,
        "migration_heads": heads,
        "failures": failures,
        "unknown_required_facts": unknown,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    # Unknown required facts are NO-GO, not a successful process exit that a caller could
    # accidentally treat as authorization. Repository/configuration details remain visible in
    # the JSON report so an operator can distinguish a defect from a missing external receipt.
    return 0 if not failures and not unknown else 1


if __name__ == "__main__":
    raise SystemExit(main())
