#!/usr/bin/env python3
"""Least-privilege enforcement check for every automated actor.

`security/agent_capabilities.json` declares what the MainAI execution worker and the development
agent may do, what they are denied, and which code enforces each denial. This script is what makes
that declaration more than documentation. It compares the declaration against the REAL runtime
contract by parsing `app/mainai_runtime_contract.py` (AST, never an import — this must run in a
dependency-free job and must never execute application code) and fails closed when:

  * a capability exists in code but is not declared here (the declaration would be stale, and a new
    privileged capability would ship unreviewed),
  * a declared capability does not exist in code (the declaration would be aspirational),
  * a provider role or write profile drifts from the code's own reviewed values,
  * an actor is granted something another actor is denied (a contradiction),
  * an actor is granted a hard-forbidden action: merging, deploying, approving its own change,
    disabling security checks, or rotating/reading secrets,
  * an `enforced_by` reference names a module that does not exist (the enforcement claim is false).

Exit codes: 0 clean, 1 violation, 2 unreadable input.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

MANIFEST_RELATIVE_PATH = "security/agent_capabilities.json"
CONTRACT_RELATIVE_PATH = "backend/app/mainai_runtime_contract.py"

# Actions no automated actor may be granted, whatever the reason. These are the boundaries the
# Founder Alpha depends on: an agent that can merge, deploy, approve itself, or switch off a check
# is not a supervised agent.
HARD_FORBIDDEN_GRANTS: tuple[str, ...] = (
    "merge_pull_request",
    "merge",
    "deploy",
    "approve_own_change",
    "approve_pull_request",
    "disable_security_checks",
    "disable_ci",
    "rotate_secrets",
    "read_production_secrets",
    "access_production_data",
)

_PROFILE_FIELDS = ("modifies_existing_data", "writes_new_records", "sandbox_only", "production_prohibited")


def _literal_assignments(tree: ast.Module) -> dict[str, object]:
    """Every module-level `NAME = <literal>` evaluated with ast.literal_eval, plus the
    `frozenset({...})` / `frozenset([...])` call shape CAPABILITY_MANIFEST uses."""
    values: dict[str, object] = {}
    for node in tree.body:
        # Both shapes appear in the contract module: plain `NAME = ...` and annotated
        # `NAME: frozenset[str] = ...` (CAPABILITY_MANIFEST and the two profile dicts are
        # annotated). Handling only one silently reads an empty contract -- which is exactly
        # what a "fail closed" verifier must never do.
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        else:
            continue
        if not isinstance(target, ast.Name) or value is None:
            continue
        try:
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "frozenset":
                values[target.id] = frozenset(ast.literal_eval(value.args[0]))
            else:
                values[target.id] = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            continue
    return values


def read_code_contract(contract_path: Path) -> dict:
    source = contract_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    values = _literal_assignments(tree)
    capabilities = values.get("CAPABILITY_MANIFEST")
    roles = values.get("_CAPABILITY_PROVIDER_ROLE")
    profiles = values.get("_CAPABILITY_WRITE_PROFILE")
    if not isinstance(capabilities, frozenset) or not capabilities:
        raise ValueError(f"{contract_path}: CAPABILITY_MANIFEST not found or empty")
    if not isinstance(roles, dict):
        raise ValueError(f"{contract_path}: _CAPABILITY_PROVIDER_ROLE not found")
    if not isinstance(profiles, dict):
        raise ValueError(f"{contract_path}: _CAPABILITY_WRITE_PROFILE not found")
    return {"capabilities": set(capabilities), "roles": roles, "profiles": profiles}


def validate(manifest: dict, code: dict, repo_root: Path) -> list[str]:
    problems: list[str] = []

    declared = manifest.get("runtime_capabilities") or {}
    declared_names = set(declared)
    if declared_names != code["capabilities"]:
        for missing in sorted(code["capabilities"] - declared_names):
            problems.append(
                f"capability '{missing}' exists in {CONTRACT_RELATIVE_PATH} but is not declared in the manifest"
            )
        for extra in sorted(declared_names - code["capabilities"]):
            problems.append(f"capability '{extra}' is declared in the manifest but does not exist in code")

    for name in sorted(declared_names & code["capabilities"]):
        entry = declared[name]
        if entry.get("provider_role") != code["roles"].get(name):
            problems.append(
                f"capability '{name}': provider_role {entry.get('provider_role')!r} != code's "
                f"{code['roles'].get(name)!r}"
            )
        profile = code["profiles"].get(name) or {}
        for field in _PROFILE_FIELDS:
            if entry.get(field) != profile.get(field):
                problems.append(
                    f"capability '{name}': {field} {entry.get(field)!r} != code's {profile.get(field)!r}"
                )

    actors = manifest.get("actors") or {}
    if not actors:
        problems.append("manifest declares no actors")

    denied_by_any: dict[str, str] = {}
    for actor_name, actor in actors.items():
        allowed = actor.get("allowed") or []
        denied = actor.get("denied") or {}
        if not allowed:
            problems.append(f"actor '{actor_name}' declares no allowed capabilities")
        if not denied:
            problems.append(f"actor '{actor_name}' declares no denials -- least privilege is unstated")
        for action in allowed:
            if action in HARD_FORBIDDEN_GRANTS:
                problems.append(f"actor '{actor_name}' is granted hard-forbidden action '{action}'")
        for action, reason in denied.items():
            if not isinstance(reason, str) or not reason.strip():
                problems.append(f"actor '{actor_name}': denial of '{action}' has no stated enforcement reason")
            denied_by_any.setdefault(action, actor_name)
        for action in allowed:
            if action in denied_by_any and denied_by_any[action] != actor_name:
                problems.append(
                    f"actor '{actor_name}' is granted '{action}', which actor "
                    f"'{denied_by_any[action]}' is denied"
                )

    for boundary in manifest.get("approval_boundaries") or []:
        refs = boundary.get("enforced_by") or []
        if not refs:
            problems.append(f"approval boundary '{boundary.get('id')}' names no enforcement point")
        for ref in refs:
            file_part = ref.split(":", 1)[0]
            if not (repo_root / file_part).exists():
                problems.append(
                    f"approval boundary '{boundary.get('id')}' cites a non-existent enforcement module: {file_part}"
                )
        if not boundary.get("summary"):
            problems.append(f"approval boundary '{boundary.get('id')}' has no summary")

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", default=".", help="repository root (default: .)")
    parser.add_argument("--manifest", default=None)
    parser.add_argument("--contract", default=None)
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    manifest_path = Path(args.manifest) if args.manifest else repo_root / MANIFEST_RELATIVE_PATH
    contract_path = Path(args.contract) if args.contract else repo_root / CONTRACT_RELATIVE_PATH

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read {manifest_path}: {exc}", file=sys.stderr)
        return 2
    try:
        code = read_code_contract(contract_path)
    except (OSError, ValueError, SyntaxError) as exc:
        print(f"error: cannot read {contract_path}: {exc}", file=sys.stderr)
        return 2

    problems = validate(manifest, code, repo_root)
    if problems:
        print("agent capability verification FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(
        f"agent capabilities verified: {len(code['capabilities'])} runtime capabilities, "
        f"{len(manifest.get('actors') or {})} actors, no forbidden grants"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
