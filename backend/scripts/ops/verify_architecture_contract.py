#!/usr/bin/env python3
"""Read-only check of the startup architecture contract, before the founder starts MainAI.

The release brief names eight architecture requirements that must survive startup: data
minimisation, a personal user-AI that shields the founder from MainAI, MainAI's own deterministic
security checks, modular Life products (LifeID/LifeSocial/LifeMessenger/LifeMusic/LifeBusiness),
one extensible research engine whose Tor/.onion access is optional, Strato as the planned
operating environment with public web access separated from founder administration, and a path to
local/offline operation with user-controlled encryption.

Most of those products do not exist yet, and building them now would be exactly the side project
the brief forbids. What *can* be checked is the part that is true today and would be expensive to
lose silently: the seams that keep those products addable, and the invariants that must not
regress while they are absent.

Each check therefore declares its strength:

  enforced  a property of the current code, verified structurally (imports, single definition).
  preserved an absence guard: the capability is not built, and this check fails the day something
            starts depending on it, so it cannot quietly become a startup requirement.

Nothing here writes, migrates, deploys or starts a process.

Exit codes:
  0  every check was evaluated and passed
  1  at least one check failed
  2  at least one check could not be evaluated (a file the contract depends on is missing)

Usage:
    python backend/scripts/ops/verify_architecture_contract.py [--out architecture-contract.json]
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND = REPO_ROOT / "backend"
APP = BACKEND / "app"
VERSIONS = BACKEND / "alembic" / "versions"

PASS, FAIL, UNVERIFIABLE = "pass", "fail", "unverifiable"


@dataclass
class Result:
    id: str
    requirement: str
    strength: str
    status: str
    detail: dict


def _imports(path: Path) -> set[str]:
    """Every module name imported by a file, as absolute dotted paths where resolvable."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


# ---------------------------------------------------------------------------------------------
# enforced: one canonical fence, and no model in the privileged decision path
# ---------------------------------------------------------------------------------------------


def _string_literals(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def check_single_prompt_fence() -> Result:
    """Exactly one module defines the fence, and every builder wraps content through it.

    Two properties are checked, because a prompt builder can be unsafe in two different ways:

    * it can *redeclare* the marker literals (a second definition that drifts), and
    * it can name the fence but append the markers around raw text, so the excerpt's own content
      decides where the boundary falls. That variant still counts the markers correctly and still
      looks fenced, which is why this check exists at all: `blueprint.py` did exactly that before
      the Phase 3 Founder Alpha staging integration, and a document containing the real end
      marker could close its own fence.

    So naming the fence obliges the module to go through one of app.rag.untrusted_content's own
    functions (`frame_untrusted_source_content`/`build_untrusted_context_block`), which defuse
    forged markers before wrapping.

    The canonical module used to be app/rag/prompt_fencing.py (two markers, BEGIN/END). The R15
    staging merge brought chat.py and workbench.py onto app/rag/untrusted_content.py instead (the
    R9 M4 -> R11 Phase 7 "unified prompt-fencing boundary" lineage, three markers: open/rule/
    close) without updating this check or blueprint.py to match — a drift this check now follows
    rather than repeats. prompt_fencing.py itself is untouched and still correct; it simply has no
    remaining router caller, so it is not the fence this check verifies against.
    """
    fence = APP / "rag" / "untrusted_content.py"
    if not fence.is_file():
        return Result("single_prompt_fence", "one canonical prompt-injection fence", "enforced", UNVERIFIABLE, {"missing": _rel(fence)})

    markers = sorted(
        s
        for s in _string_literals(fence)
        if ("DOKUMENTINNEHÅLL" in s or "DATA, INTE INSTRUKTIONER" in s) and s.lstrip().startswith(("=====", "—"))
    )
    if len(markers) != 3:
        return Result(
            "single_prompt_fence",
            "one canonical prompt-injection fence",
            "enforced",
            UNVERIFIABLE,
            {"reason": "the fence module no longer declares exactly three markers", "found": markers},
        )

    duplicated = {
        _rel(path): sorted(set(_string_literals(path)) & set(markers))
        for path in _python_files(APP)
        if path != fence and set(_string_literals(path)) & set(markers)
    }
    naming = [
        path
        for path in _python_files(APP)
        if path != fence and any("DOKUMENTINNEHÅLL" in s for s in _string_literals(path))
    ]
    fence_functions = (
        "app.rag.untrusted_content.frame_untrusted_source_content",
        "app.rag.untrusted_content.build_untrusted_context_block",
    )
    unfenced = sorted(_rel(p) for p in naming if not (_imports(p) & set(fence_functions)))
    callers = {_rel(p) for p in _python_files(APP) if _imports(p) & set(fence_functions)}
    required = {"backend/app/routers/chat.py", "backend/app/routers/workbench.py", "backend/app/routers/blueprint.py"}
    missing_callers = sorted(required - callers)
    ok = not duplicated and not unfenced and not missing_callers
    return Result(
        "single_prompt_fence",
        "one canonical prompt-injection fence",
        "enforced",
        PASS if ok else FAIL,
        {
            "definers": [_rel(fence)],
            "redeclared_markers": duplicated,
            "names_fence_without_defusing": unfenced,
            "defusing_callers": sorted(callers),
            "required_callers_missing": missing_callers,
        },
    )


def check_deterministic_privileged_path() -> Result:
    """No module that decides authorisation may reach a model.

    The brief requires MainAI's own independent, deterministic security checks. The structural
    form of that claim is an import graph: the approval gate, session/CSRF checks, RLS and token
    revocation must never be able to call a provider, because a provider can be prompt-injected
    and a decision that consults one is advisory by construction.
    """
    fixed = [
        APP / "mainai_execution" / "approval.py",
        APP / "security.py",
        APP / "rls.py",
        APP / "deps.py",
        APP / "token_revocation.py",
        APP / "cookies.py",
        APP / "password_policy.py",
    ]
    candidates = fixed + sorted(
        p for p in APP.rglob("*approval*.py") if p not in fixed and "__pycache__" not in p.parts
    )
    present = [p for p in candidates if p.is_file()]
    if not present:
        return Result("deterministic_privileged_path", "deterministic privileged-action path", "enforced", UNVERIFIABLE, {"reason": "no authorisation module found"})

    offenders = {
        _rel(path): sorted(m for m in _imports(path) if m.startswith("app.providers") or m == "app.providers")
        for path in present
        if any(m.startswith("app.providers") for m in _imports(path))
    }
    return Result(
        "deterministic_privileged_path",
        "deterministic privileged-action path",
        "enforced",
        FAIL if offenders else PASS,
        {"scanned": [_rel(p) for p in present], "model_dependencies": offenders},
    )


def check_provider_adapters_cannot_reach_data() -> Result:
    """Provider adapters receive messages, never a database handle.

    This is the checkable half of data minimisation: an adapter that cannot import the ORM cannot
    decide for itself to ship rows to a third party. What a caller *chooses* to send is a separate
    question and is not claimed here.
    """
    adapters = sorted((APP / "providers").glob("*_provider.py")) + [APP / "providers" / "base.py"]
    present = [p for p in adapters if p.is_file()]
    if not present:
        return Result("provider_adapters_cannot_reach_data", "provider adapters cannot reach stored data", "enforced", UNVERIFIABLE, {"reason": "no provider adapters found"})

    forbidden_prefixes = ("app.models", "app.db", "sqlalchemy", "app.rag")
    offenders = {}
    for path in present:
        bad = sorted(m for m in _imports(path) if m.startswith(forbidden_prefixes))
        if bad:
            offenders[_rel(path)] = bad
    return Result(
        "provider_adapters_cannot_reach_data",
        "provider adapters cannot reach stored data",
        "enforced",
        FAIL if offenders else PASS,
        {"scanned": [_rel(p) for p in present], "data_access": offenders},
    )


# ---------------------------------------------------------------------------------------------
# preserved: absence guards for capabilities that must stay optional
# ---------------------------------------------------------------------------------------------


def _token_hits(root: Path, pattern: str, *, exclude: tuple[str, ...] = ()) -> dict[str, int]:
    regex = re.compile(pattern)
    hits: dict[str, int] = {}
    for path in _python_files(root):
        rel = _rel(path)
        if any(part in rel for part in exclude):
            continue
        count = len(regex.findall(path.read_text(encoding="utf-8", errors="ignore")))
        if count:
            hits[rel] = count
    return hits


def check_tor_is_not_a_startup_dependency() -> Result:
    """Nothing in the runtime references Tor or a SOCKS proxy, so it cannot be required to boot.

    The research engine's isolated .onion access is a future adapter. If any of this appeared in
    the startup path, an unavailable Tor daemon would become an outage. Today the answer is a clean
    zero; the check exists so the day that changes, it changes as an opt-in adapter.
    """
    startup = [APP / "main.py", APP / "bootstrap.py", APP / "worker.py", APP / "config.py"]
    missing = [_rel(p) for p in startup if not p.is_file()]
    if missing:
        return Result("tor_not_a_startup_dependency", "Tor is not a hard startup dependency", "preserved", UNVERIFIABLE, {"missing": missing})

    everywhere = _token_hits(APP, r"\b(tor|onion|socks5?|tor_proxy)\b")
    outside_startup = _token_hits(
        APP,
        r"\b(tor|onion|socks5?|tor_proxy)\b",
        exclude=tuple(_rel(p) for p in startup),
    )
    # The two results partition the tree, so the detail says which half moved if this ever fails.
    return Result(
        "tor_not_a_startup_dependency",
        "Tor is not a hard startup dependency",
        "preserved",
        FAIL if everywhere else PASS,
        {"startup_files": [_rel(p) for p in startup], "references_anywhere": everywhere, "references_outside_startup": outside_startup},
    )


def check_life_products_are_not_core_coupled() -> Result:
    """Core MainAI carries no LifeID/LifeSocial/LifeMessenger/LifeMusic/LifeBusiness code.

    Those products must attach as modules later. If core already imported or migrated them, every
    future product would be a change to the founder's release path instead of an addition beside
    it. Zero today is what keeps that door open.
    """
    pattern = r"\b(LifeID|LifeSocial|LifeMessenger|LifeMusic|LifeBusiness)\b"
    hits = _token_hits(APP, pattern)
    hits.update(_token_hits(VERSIONS, pattern))
    return Result(
        "life_products_not_core_coupled",
        "Life products remain addable modules, not core coupling",
        "preserved",
        FAIL if hits else PASS,
        {"references": hits, "scanned": ["backend/app", "backend/alembic/versions"]},
    )


# ---------------------------------------------------------------------------------------------
# enforced: deployment shape and the migration DAG the R12 merge has to land on
# ---------------------------------------------------------------------------------------------


def check_public_edge_is_the_only_ingress() -> Result:
    """One published entry point (Caddy), one upstream (the frontend), founder admin behind auth.

    Strato is the planned operating environment; the requirement that matters before startup is
    that public web access and founder administration are separated by a boundary that exists in
    configuration rather than in intent. Caddy is the only service with published ports, and it
    proxies to the frontend only — the backend is never directly reachable.
    """
    caddyfile = REPO_ROOT / "Caddyfile"
    compose = REPO_ROOT / "docker-compose.vps.yml"
    missing = [_rel(p) for p in (caddyfile, compose) if not p.is_file()]
    if missing:
        return Result("public_edge_only_ingress", "public edge separated from founder administration", "enforced", UNVERIFIABLE, {"missing": missing})

    proxies = [
        line.strip()
        for line in caddyfile.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("reverse_proxy")
    ]
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML is a backend dependency
        return Result("public_edge_only_ingress", "public edge separated from founder administration", "enforced", UNVERIFIABLE, {"reason": "PyYAML unavailable"})

    spec = yaml.safe_load(compose.read_text(encoding="utf-8")) or {}
    published: dict[str, list[str]] = {}
    for name, service in (spec.get("services") or {}).items():
        ports = (service or {}).get("ports")
        if ports:
            published[name] = [str(p) for p in ports]

    problems: list[str] = []
    if len(proxies) != 1:
        problems.append(f"expected exactly one reverse_proxy, found {len(proxies)}")
    elif "frontend" not in proxies[0]:
        problems.append(f"the only reverse_proxy target is not the frontend: {proxies[0]}")
    if set(published) != {"caddy"}:
        problems.append(f"services publishing host ports: {sorted(published)} (only caddy may)")
    elif not {"80:80", "443:443"}.issubset(set(published["caddy"])):
        problems.append(f"caddy publishes {published['caddy']}, expected 80:80 and 443:443")

    return Result(
        "public_edge_only_ingress",
        "public edge separated from founder administration",
        "enforced",
        FAIL if problems else PASS,
        {"reverse_proxy": proxies, "published_ports": published, "problems": problems},
    )


def check_single_migration_head() -> Result:
    """The migration DAG has exactly one head, and it is the one R12 has to be re-based onto.

    R12's reported 0075–0077 collide with this tree's applied migrations by identity. A merge
    revision on top of the real head is the only correct resolution, so the gate needs to know the
    head precisely rather than assume it, and to notice immediately if a branch split the DAG.
    """
    if not VERSIONS.is_dir():
        return Result("single_migration_head", "one linear migration DAG", "enforced", UNVERIFIABLE, {"missing": _rel(VERSIONS)})

    revisions: dict[str, str] = {}
    parents: set[str] = set()
    for path in sorted(VERSIONS.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            return Result("single_migration_head", "one linear migration DAG", "enforced", UNVERIFIABLE, {"error": f"{_rel(path)}: {exc}"})
        values: dict[str, object] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name in {"revision", "down_revision"}:
                    try:
                        values[name] = ast.literal_eval(node.value)
                    except ValueError:
                        pass
        revision = values.get("revision")
        if not isinstance(revision, str):
            continue
        revisions[revision] = path.name
        down = values.get("down_revision")
        if isinstance(down, str):
            parents.add(down)
        elif isinstance(down, (tuple, list)):
            parents.update(str(d) for d in down)

    heads = sorted(set(revisions) - parents)
    return Result(
        "single_migration_head",
        "one linear migration DAG",
        "enforced",
        PASS if len(heads) == 1 else FAIL,
        {"head": heads[0] if len(heads) == 1 else None, "heads": heads, "revisions": len(revisions), "files": {r: revisions[r] for r in heads}},
    )


CHECKS = (
    check_single_prompt_fence,
    check_deterministic_privileged_path,
    check_provider_adapters_cannot_reach_data,
    check_tor_is_not_a_startup_dependency,
    check_life_products_are_not_core_coupled,
    check_public_edge_is_the_only_ingress,
    check_single_migration_head,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=None, help="write the JSON report to this path")
    args = parser.parse_args(argv)

    results = [check() for check in CHECKS]
    failed = [r.id for r in results if r.status == FAIL]
    unverifiable = [r.id for r in results if r.status == UNVERIFIABLE]

    report = {
        "tool": "lifeai-architecture-contract",
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "checks": [r.__dict__ for r in results],
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r.status == PASS),
            "failed": failed,
            "unverifiable": unverifiable,
            # Absence guards are not the same evidence as structural checks; never merge them.
            "enforced": [r.id for r in results if r.strength == "enforced"],
            "preserved": [r.id for r in results if r.strength == "preserved"],
        },
        "note": "A 'preserved' check passes because the capability is absent and nothing depends "
        "on it yet. It is a regression guard, not evidence that the capability works.",
    }

    for r in results:
        print(f"[{r.status:12}] {r.id:38} ({r.strength}) {r.requirement}")

    if args.out:
        out = Path(args.out).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"report: {out}")

    if failed:
        return 1
    if unverifiable:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
