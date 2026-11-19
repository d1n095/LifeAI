#!/usr/bin/env python3
"""Find dead integration seams: code that exists but is never actually reached.

The registry records what is supposed to work. This finds the opposite — the shapes that make a
system *look* complete while nothing calls them:

  * **unread settings** — a configuration flag that no code reads. It documents a behavior that
    does not exist, and an operator who sets it changes nothing.
  * **unreferenced functions** — a module-level function in `app/` that nothing outside its own
    file mentions. Either it is a hook that was never wired up, or it is dead.
  * **route paths with no consumer** — an API path in `app/routers/` that the frontend never
    references. Not automatically a defect (a documented API can have an out-of-repo client), but
    it must be *listed*, not assumed.

Static reference counting has real limits and they are stated rather than hidden: a function
reached only through a decorator, a registry dict, a `getattr`, or a background scheduler entry is
still referenced. An entry that is referenced only by *another dead entry* is NOT detected — this
tool finds seams, it does not prove reachability. That is why its output is a report and the only
enforced rule is the one that is unambiguous: a settings field nobody reads.

Exit codes: 0 no enforced violation, 1 an enforced violation, 2 unreadable input.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

APP_DIR = "backend/app"
FRONTEND_DIR = "frontend"
SETTINGS_FILE = "backend/app/config.py"

# A field whose name matches these is documentation-adjacent or consumed by pydantic itself.
_IGNORED_SETTING_PATTERN = re.compile(r"^(model_config|model_fields)$")


def _python_files(root: Path) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]


def settings_fields(settings_path: Path) -> list[str]:
    tree = ast.parse(settings_path.read_text(encoding="utf-8"))
    fields: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "Settings":
            continue
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                name = item.target.id
                if not _IGNORED_SETTING_PATTERN.match(name) and not name.startswith("_"):
                    fields.append(name)
    return fields


def unread_settings(repo_root: Path) -> list[str]:
    settings_path = repo_root / SETTINGS_FILE
    app_root = repo_root / APP_DIR
    fields = settings_fields(settings_path)
    # config.py is INCLUDED: a field read only by a property on Settings itself (e.g.
    # `frontend_origin_list` reading `self.frontend_origins`) is genuinely read. Excluding the
    # defining module reported every such field as dead, which is the classic way a reachability
    # heuristic becomes unusable.
    corpus = {path: path.read_text(encoding="utf-8", errors="replace") for path in _python_files(app_root)}
    unread = []
    for field in fields:
        # Any attribute access anywhere in app/ counts, whatever the local variable is called. The
        # definition itself is `field: type = default` (no leading dot), so it never self-matches.
        pattern = re.compile(rf"\.{re.escape(field)}\b")
        if not any(pattern.search(text) for text in corpus.values()):
            unread.append(field)
    return sorted(unread)


def module_level_functions(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Decorated functions are frequently registered by their decorator (routes, fixtures,
            # handlers); they are not "unreachable" just because nothing spells their name.
            if node.decorator_list:
                continue
            names.append(node.name)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            pass
    return names


def unreferenced_functions(repo_root: Path) -> list[dict]:
    app_root = repo_root / APP_DIR
    files = _python_files(app_root)
    corpus = {path: path.read_text(encoding="utf-8", errors="replace") for path in files}
    findings: list[dict] = []
    for path, text in corpus.items():
        for name in module_level_functions(path):
            pattern = re.compile(rf"\b{re.escape(name)}\b")
            # Same-file uses count: a module-private helper called twice in its own module is
            # reachable. Only the definition line itself is excluded (occurrence #1).
            same_file_uses = len(pattern.findall(text)) - 1
            referenced = same_file_uses > 0 or any(
                pattern.search(other_text)
                for other, other_text in corpus.items()
                if other != path
            )
            if not referenced:
                findings.append(
                    {
                        "kind": "unreferenced_function",
                        "path": path.relative_to(repo_root).as_posix(),
                        "symbol": name,
                    }
                )
    return findings


def route_paths(repo_root: Path) -> list[str]:
    routers = repo_root / APP_DIR / "routers"
    paths: set[str] = set()
    for source in _python_files(routers):
        text = source.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"@router\.(get|post|put|patch|delete)\(\s*[\"']([^\"']+)[\"']", text):
            paths.add(match.group(2))
    return sorted(paths)


def routes_without_frontend_consumer(repo_root: Path) -> list[str]:
    frontend = repo_root / FRONTEND_DIR
    if not frontend.is_dir():
        return []
    corpus = []
    for path in frontend.rglob("*"):
        if path.suffix in {".ts", ".tsx", ".js", ".jsx"} and "node_modules" not in path.parts:
            try:
                corpus.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    blob = "\n".join(corpus)
    orphans = []
    for route in route_paths(repo_root):
        # A route is consumed if the frontend mentions it, or its last static segment (routers
        # build paths like `/api/library/import`, and clients often concatenate a prefix).
        tail = [part for part in route.split("/") if part and not part.startswith("{")]
        if route in blob:
            continue
        if tail and re.search(rf"\b{re.escape(tail[-1])}\b", blob):
            continue
        orphans.append(route)
    return sorted(orphans)


def build_report(repo_root: Path) -> dict:
    return {
        "unread_settings": unread_settings(repo_root),
        "unreferenced_functions": unreferenced_functions(repo_root),
        "routes_without_frontend_consumer": routes_without_frontend_consumer(repo_root),
        "limits": "Static reference counting: a function reached only via a registry, getattr, scheduler entry or another dead function is not detected. Findings are leads for review, not proofs.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--enforce-unread-settings",
        action="store_true",
        help="fail when a settings field is never read (a flag that does not affect behavior)",
    )
    args = parser.parse_args(argv)

    try:
        report = build_report(Path(args.repo_root).resolve())
    except (OSError, SyntaxError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"unread settings: {len(report['unread_settings'])}")
        for name in report["unread_settings"]:
            print(f"  - {name}")
        print(f"unreferenced functions: {len(report['unreferenced_functions'])}")
        for finding in report["unreferenced_functions"][:40]:
            print(f"  - {finding['path']}:{finding['symbol']}")
        print(f"routes with no frontend consumer: {len(report['routes_without_frontend_consumer'])}")
        for route in report["routes_without_frontend_consumer"][:40]:
            print(f"  - {route}")

    if args.enforce_unread_settings and report["unread_settings"]:
        print(
            "FAILED: a settings field that nothing reads documents a behavior that does not exist",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
