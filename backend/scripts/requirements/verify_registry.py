#!/usr/bin/env python3
"""Verify the requirements registry against the repository it claims to describe.

A registry that is not checked is documentation with extra steps. This verifier makes every entry
falsifiable, and fails closed when it cannot confirm one:

  * schema: required fields present, ids unique, classes and statuses from the closed sets;
  * dependency closure: no dangling `depends_on` id, no cycle;
  * paths: every referenced module and test file exists;
  * test names: a `path::test_name` reference must actually contain that test;
  * evidence: a requirement marked `verified` must cite at least one existing test — a verified
    claim with no executable evidence is exactly the self-reporting this mission forbids;
  * gaps: anything not `verified` must state a gap in writing.

It deliberately does NOT judge whether the cited test is a *good* test. That is a review question;
pretending otherwise would be the same mistake as claiming static analysis proves absence of
malicious code.

Exit codes: 0 verified, 1 violation, 2 unreadable input.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REGISTRY_RELATIVE_PATH = "requirements/registry.json"

CLASSES = {
    "mandatory_before_private_start",
    "necessary_for_safe_autonomy",
    "required_for_production",
    "future_optional",
}
STATUSES = {"verified", "partial", "missing", "deferred", "needs_founder_decision"}
REQUIRED_FIELDS = (
    "id",
    "title",
    "class",
    "source",
    "responsible_module",
    "depends_on",
    "implementation_evidence",
    "integration_evidence",
    "security_implications",
    "test_coverage",
    "status",
    "gaps",
)
_ID_RE = re.compile(r"^REQ-[A-Z]+-[0-9]{3}$")


def load_registry(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _check_reference(repo_root: Path, reference: str, problems: list[str], requirement_id: str) -> bool:
    """`path` or `path::symbol`. Returns True when the file (and symbol, if given) exists."""
    path_part, sep, symbol = reference.partition("::")
    if not sep:
        # Implementation references use a single colon (`path.py:function`); pytest references use
        # the double form. Both must resolve to a real path, so the split is explicit rather than
        # assuming one style.
        head, colon, tail = reference.rpartition(":")
        if colon and tail and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tail):
            path_part, symbol = head, tail
    target = repo_root / path_part
    if not target.exists():
        problems.append(f"{requirement_id}: referenced path does not exist: {path_part}")
        return False
    if symbol:
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            problems.append(f"{requirement_id}: referenced path is unreadable: {path_part}")
            return False
        # A symbol reference is either a `def name`, a `class name`, or a plain identifier
        # (constant/attribute) — all three must literally appear.
        if not re.search(rf"(def |class |^|[^A-Za-z0-9_]){re.escape(symbol)}([^A-Za-z0-9_]|$)", text, re.M):
            problems.append(f"{requirement_id}: symbol '{symbol}' not found in {path_part}")
            return False
    return True


def _check_cycles(requirements: dict[str, dict]) -> list[str]:
    problems: list[str] = []
    state: dict[str, int] = {}

    def visit(node: str, path: list[str]) -> None:
        if state.get(node) == 1:
            cycle = " -> ".join(path[path.index(node):] + [node])
            problems.append(f"dependency cycle: {cycle}")
            return
        if state.get(node) == 2:
            return
        state[node] = 1
        for dependency in requirements[node].get("depends_on") or []:
            if dependency in requirements:
                visit(dependency, path + [node])
        state[node] = 2

    for node in requirements:
        visit(node, [])
    return problems


def verify(repo_root: Path, registry: dict) -> list[str]:
    problems: list[str] = []
    entries = registry.get("requirements")
    if not isinstance(entries, list) or not entries:
        return ["registry contains no requirements"]

    requirements: dict[str, dict] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.append(f"entry #{index} is not an object")
            continue
        missing = [field for field in REQUIRED_FIELDS if field not in entry]
        if missing:
            problems.append(f"entry #{index} ({entry.get('id', '?')}) is missing fields: {', '.join(missing)}")
            continue

        requirement_id = entry["id"]
        if not _ID_RE.match(requirement_id):
            problems.append(f"entry #{index}: id {requirement_id!r} does not match REQ-<AREA>-<NNN>")
        if requirement_id in requirements:
            problems.append(f"duplicate requirement id: {requirement_id}")
        requirements[requirement_id] = entry

        if entry["class"] not in CLASSES:
            problems.append(f"{requirement_id}: unknown class {entry['class']!r}")
        if entry["status"] not in STATUSES:
            problems.append(f"{requirement_id}: unknown status {entry['status']!r}")
        if not str(entry["security_implications"]).strip():
            problems.append(f"{requirement_id}: no security implications stated")

        for reference in entry["implementation_evidence"]:
            _check_reference(repo_root, reference, problems, requirement_id)
        for reference in entry["integration_evidence"]:
            _check_reference(repo_root, reference, problems, requirement_id)

        coverage = entry["test_coverage"]
        existing_tests = [
            reference for reference in coverage if _check_reference(repo_root, reference, problems, requirement_id)
        ]
        if entry["status"] == "verified" and not existing_tests:
            problems.append(
                f"{requirement_id}: marked 'verified' but cites no existing test -- an unverified claim"
            )
        if entry["status"] != "verified" and not entry["gaps"]:
            problems.append(f"{requirement_id}: status {entry['status']!r} must state at least one gap")

    for requirement_id, entry in requirements.items():
        for dependency in entry["depends_on"]:
            if dependency not in requirements:
                problems.append(f"{requirement_id}: depends_on unknown requirement {dependency!r}")
    problems.extend(_check_cycles(requirements))
    return problems


def summary(registry: dict) -> dict:
    by_class: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for entry in registry.get("requirements") or []:
        by_class[entry.get("class", "?")] = by_class.get(entry.get("class", "?"), 0) + 1
        by_status[entry.get("status", "?")] = by_status.get(entry.get("status", "?"), 0) + 1
    return {"total": len(registry.get("requirements") or []), "by_class": by_class, "by_status": by_status}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--registry", default=None)
    parser.add_argument("--json", action="store_true", help="print the machine-readable summary")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    registry_path = Path(args.registry) if args.registry else repo_root / REGISTRY_RELATIVE_PATH
    try:
        registry = load_registry(registry_path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read {registry_path}: {exc}", file=sys.stderr)
        return 2

    problems = verify(repo_root, registry)
    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems, **summary(registry)}, indent=2))
    if problems:
        if not args.json:
            print("requirements registry verification FAILED:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
        return 1

    if not args.json:
        data = summary(registry)
        print(f"requirements registry verified: {data['total']} requirements")
        for name, count in sorted(data["by_class"].items()):
            print(f"  {name}: {count}")
        for name, count in sorted(data["by_status"].items()):
            print(f"  status {name}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
