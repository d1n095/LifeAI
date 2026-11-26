#!/usr/bin/env python3
"""Executable Founder Alpha merge invariants — the conflict rules, as checks.

`docs/FREEBUFF_FOUNDER_ALPHA_INTEGRATION_HANDOFF.md` §6 assigns, per conflicting file, which
side wins and why. A prose directive is not verification: after Codex resolves the 17 conflicts,
nothing in the repository would notice if a resolution silently dropped the other side's
guarantee. This script turns each directive into a deterministic check over the resolved tree.

It is a post-merge checker, NOT a pytest module, and deliberately not part of the collected
suite: on an un-merged branch several invariants are *supposed* to be unmet, and a collected
test would either fail normal runs or need a skip that hides the truth. Run it on the candidate:

    cd backend
    python scripts/acceptance/alpha_merge_invariants.py --merged origin/scratch/alpha-merge

Statuses
--------
  * PASS:     the resolved tree satisfies the invariant; evidence is quoted.
  * FAIL:     the resolved tree violates it — the merge lost a guarantee, stop before staging.
  * BLOCKED:  the invariant could not be evaluated here (git parent missing, file absent).

Safety: read-only. It parses files and runs `git ls-tree`/`git diff`; it never writes, migrates,
deploys or resolves anything.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(relative: str) -> str | None:
    path = REPO_ROOT / relative
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _tree(relative: str) -> ast.Module | None:
    source = _read(relative)
    if source is None:
        return None
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node  # type: ignore[return-value]
    return None


def _class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _arg_names(function: ast.FunctionDef) -> list[str]:
    arguments = function.args
    names = [argument.arg for argument in (*arguments.posonlyargs, *arguments.args)]
    names += [argument.arg for argument in arguments.kwonlyargs]
    return names


def _is_mandatory(function: ast.FunctionDef, name: str) -> bool | None:
    """True when `name` is a required parameter; None when it is not a parameter at all.

    Defaults live on `arguments.defaults`/`kw_defaults`, never on the `ast.arg` node, so a
    keyword-only parameter like `authorized_by_user_id` needs the kwonly index, not `args.args`.
    """
    arguments = function.args
    keyword_names = [argument.arg for argument in arguments.kwonlyargs]
    if name in keyword_names:
        return arguments.kw_defaults[keyword_names.index(name)] is None
    positional = [*arguments.posonlyargs, *arguments.args]
    positional_names = [argument.arg for argument in positional]
    if name not in positional_names:
        return None
    first_with_default = len(positional) - len(arguments.defaults)
    return positional_names.index(name) < first_with_default


def _git(*arguments: str) -> tuple[int, str]:
    result = subprocess.run(
        ["git", *arguments], cwd=REPO_ROOT, text=True, capture_output=True, check=False
    )
    return result.returncode, result.stdout


def _repo_lock_verifier() -> Any | None:
    """The repository's own POLICY_LOCK verifier, loaded the way its test loads it.

    INV-12 must not grow a second, subtly different implementation of the gate: the hash and
    coverage rules stay in `backend/scripts/security/verify_policy_lock.py`, which is what A08's
    `test_the_repository_policy_lock_verifies` asserts on.
    """
    path = REPO_ROOT / "backend" / "scripts" / "security" / "verify_policy_lock.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("alpha_merge_policy_lock", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------------------------
# Invariants. Each returns (status, evidence).
# --------------------------------------------------------------------------------------------
def _requires(path: str) -> None:
    if not (REPO_ROOT / path).exists():
        raise FileNotFoundError(path)


def i01_streaming_contract(_args: argparse.Namespace) -> tuple[str, str]:
    """Alpha's StreamChunk/chat_stream is the candidate contract; my parallel API must not remain."""
    _requires("backend/app/providers/base.py")
    tree = _tree("backend/app/providers/base.py")
    if tree is None:
        return "BLOCKED", "providers/base.py could not be parsed"
    has_alpha = _class(tree, "StreamChunk") is not None
    has_mine = _class(tree, "ChatStreamChunk") is not None
    if has_alpha and not has_mine:
        return "PASS", "StreamChunk present; ChatStreamChunk absent"
    if not has_alpha:
        return "FAIL", "StreamChunk missing — Alpha's streaming contract was lost"
    return "FAIL", "both StreamChunk and ChatStreamChunk present — two streaming contracts"


def i02_ollama_egress_flags(_args: argparse.Namespace) -> tuple[str, str]:
    """Alpha's chat_stream plus my follow_redirects=False/trust_env=False on the streaming client."""
    _requires("backend/app/providers/ollama_provider.py")
    source = _read("backend/app/providers/ollama_provider.py") or ""
    missing = [flag for flag in ("follow_redirects=False", "trust_env=False") if flag not in source]
    if not missing:
        return "PASS", "streaming client keeps follow_redirects=False and trust_env=False"
    return "FAIL", f"missing egress client flags: {missing}"


def i03_local_only_mode(_args: argparse.Namespace) -> tuple[str, str]:
    """LOCAL_ONLY_MODE must survive the egress merge (handoff §7.1)."""
    _requires("backend/app/config.py")
    source = _read("backend/app/config.py") or ""
    if "local_only_mode" in source:
        return "PASS", "settings.local_only_mode is defined"
    return "FAIL", "settings.local_only_mode is absent — LOCAL_ONLY_MODE would be silently lost"


def i04_per_attempt_egress_recheck(_args: argparse.Namespace) -> tuple[str, str]:
    """Alpha's lease assertion AND my per-attempt policy recheck must both exist."""
    _requires("backend/app/providers/registry.py")
    source = _read("backend/app/providers/registry.py") or ""
    present = {
        "alpha_lease_assertion": "_verify_lease_assertion" in source,
        "local_only_filter": "_filter_local_only" in source,
        "per_attempt_policy_recheck": "_require_policy_candidate" in source,
    }
    missing = [name for name, ok in present.items() if not ok]
    if not missing:
        return "PASS", json.dumps(present)
    return "FAIL", f"egress model incomplete, missing: {missing} (present: {present})"


def _called_names(function: ast.AST) -> set[str]:
    """Names actually *called* in a function body.

    Deliberately AST-based, not textual: Alpha's fail-closed `detect_conflict` explains in its
    own docstring that it makes no `chat_with_fallback()` call, and a substring scan reads that
    sentence as a call site. A docstring may therefore never decide a security invariant.
    """
    names: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def i05_detect_conflict_fail_closed(_args: argparse.Namespace) -> tuple[str, str]:
    """Alpha wins (§7.2): detect_conflict must make zero provider calls and invent no owner."""
    _requires("backend/app/mainai_execution/lesson_conflicts.py")
    tree = _tree("backend/app/mainai_execution/lesson_conflicts.py")
    if tree is None:
        return "BLOCKED", "lesson_conflicts.py could not be parsed"
    function = _function(tree, "detect_conflict")
    if function is None:
        return "FAIL", "detect_conflict is missing entirely"
    calls = sorted(
        _called_names(function) & {"chat_with_fallback", "resolve_egress_owner", "invoke_provider"}
    )
    if calls:
        return "FAIL", f"detect_conflict still calls a provider path: {calls}"
    if "owner_id" in _arg_names(function):
        return "FAIL", "detect_conflict still requires owner_id — founder-wide state must not be given a fabricated owner"
    return "PASS", "detect_conflict is fail-closed: no provider call, no owner_id parameter"


def i06_lease_contract(_args: argparse.Namespace) -> tuple[str, str]:
    """Alpha's lease contract (lease_worker_id/lease_generation) must be what callers see."""
    _requires("backend/app/providers/registry.py")
    tree = _tree("backend/app/providers/registry.py")
    if tree is None:
        return "BLOCKED", "providers/registry.py could not be parsed"
    function = _function(tree, "chat_with_fallback")
    if function is None:
        return "FAIL", "chat_with_fallback is missing"
    names = _arg_names(function)
    if "lease_worker_id" in names and "lease_generation" in names:
        return "PASS", f"chat_with_fallback({', '.join(names)})"
    return "FAIL", f"lease contract not Alpha's: {names}"


def i07_agent_authorization_contract(_args: argparse.Namespace) -> tuple[str, str]:
    """`authorized_by_user_id` (mandatory) and the aligned disclosure purpose."""
    _requires("backend/app/agent_orchestration.py")
    source = _read("backend/app/agent_orchestration.py") or ""
    problems = []
    if "authorized_by_user_id" not in source:
        problems.append("authorized_by_user_id missing")
    if "agent_orchestration_dispatch_task" not in source:
        problems.append("dispatch purpose not aligned")
    if "agent_orchestration_review_task" not in source:
        problems.append("review purpose not aligned")
    tree = _tree("backend/app/agent_orchestration.py")
    if tree is not None:
        for name in ("dispatch_task", "review_task"):
            function = _function(tree, name)
            if function is None:
                problems.append(f"{name} missing")
                continue
            mandatory = _is_mandatory(function, "authorized_by_user_id")
            if mandatory is None:
                problems.append(f"{name} has no authorized_by_user_id")
            elif not mandatory:
                problems.append(f"{name}.authorized_by_user_id must stay mandatory (has a default)")
    if problems:
        return "FAIL", "; ".join(problems)
    return "PASS", "authorized_by_user_id mandatory on dispatch_task/review_task; purposes aligned"


def i08_message_out_superset(_args: argparse.Namespace) -> tuple[str, str]:
    """My MessageOut superset (validator coercing ORM enum -> str) is kept."""
    _requires("backend/app/schemas.py")
    source = _read("backend/app/schemas.py") or ""
    tree = _tree("backend/app/schemas.py")
    if tree is None:
        return "BLOCKED", "schemas.py could not be parsed"
    message_out = _class(tree, "MessageOut")
    if message_out is None:
        return "FAIL", "MessageOut is missing"
    segment = ast.get_source_segment(source, message_out) or ""
    if "field_validator" not in segment:
        return "FAIL", "MessageOut lost its status field_validator"
    if "status" not in segment:
        return "FAIL", "MessageOut has no status field"
    return "PASS", "MessageOut keeps status + field_validator"


def i09_corpus_review_union(_args: argparse.Namespace) -> tuple[str, str]:
    """Both sides' protections: bounded tokens (Alpha) and effect guard (mine)."""
    _requires("backend/app/jobs/handlers/corpus_review.py")
    source = _read("backend/app/jobs/handlers/corpus_review.py") or ""
    missing = [token for token in ("max_tokens=400", "effect_guard=True") if token not in source]
    if not missing:
        return "PASS", "max_tokens=400 and effect_guard=True both present"
    return "FAIL", f"lost protections: {missing}"


def i10_single_migration_head(_args: argparse.Namespace) -> tuple[str, str]:
    """Exactly one alembic head, additive chain, no renumbering collision."""
    versions = REPO_ROOT / "backend" / "alembic" / "versions"
    if not versions.exists():
        return "BLOCKED", "alembic/versions is missing"
    revisions: dict[str, str] = {}
    parents: set[str] = set()
    for path in sorted(versions.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        revision = down = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    value = node.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        if target.id == "revision":
                            revision = value.value
                        else:
                            down = value.value
        if revision:
            revisions[revision] = down or ""
            if down:
                parents.add(down)
    heads = sorted(set(revisions) - parents)
    if len(heads) != 1:
        return "FAIL", f"expected exactly one alembic head, found {heads}"
    head = heads[0]
    if _args.expected_head and head != _args.expected_head:
        return "FAIL", f"head is {head}, expected {_args.expected_head}"
    chain = []
    cursor = head
    while cursor:
        chain.append(cursor)
        cursor = revisions.get(cursor, "")
        if len(chain) > 500:
            return "FAIL", "revision chain does not terminate — cycle"
    return "PASS", f"single head {head}; chain length {len(chain)}"


def i11_no_dropped_tests(args: argparse.Namespace) -> tuple[str, str]:
    """Neither parent's test files may disappear in the merge."""
    if not args.merged:
        return "BLOCKED", "--merged not given"
    code, parents = _git("rev-list", "--parents", "-n", "1", args.merged)
    if code != 0:
        return "BLOCKED", f"cannot resolve {args.merged}"
    parts = parents.split()
    if len(parts) < 3:
        return "BLOCKED", f"{args.merged} is not a merge commit ({len(parts) - 1} parent(s))"
    dropped: list[str] = []
    checked = 0
    for parent in parts[1:]:
        code, listing = _git("ls-tree", "-r", "--name-only", parent)
        if code != 0:
            return "BLOCKED", f"cannot list {parent}"
        for name in listing.splitlines():
            if "/tests/" not in f"/{name}":
                continue
            checked += 1
            code, _ = _git("cat-file", "-e", f"{args.merged}:{name}")
            if code != 0:
                dropped.append(name)
    if dropped:
        return "FAIL", f"{len(dropped)} test file(s) dropped by the merge: {dropped[:10]}"
    return "PASS", f"no test file from either parent was dropped (checked {checked} paths)"


def i12_policy_lock_integrity(args: argparse.Namespace) -> tuple[str, str]:
    """POLICY_LOCK is Z.ai's (§7.3); this delivery must not move it on its own authority.

    Three cases are BLOCKED rather than PASS, because the file is **Alpha-only**: neither this
    branch nor `86671468` carries it, so diffing against a base without a lock returns an empty
    diff and would report "untouched" about a file that is not there at all. A vacuous PASS on a
    security control is worse than no check. Pass the sha that actually owns the lock (the Alpha
    candidate, `b313c9df`) once it is on the tree.

    On the merged candidate two further things must hold, and they are checked rather than
    assumed:

    * **the lock verifies against the resolved tree.** That is exactly what A08's
      `test_the_repository_policy_lock_verifies` asserts, so a PASS without it would report a
      security gate as satisfied while the gate is red.
    * **if the lock differs from the base, the difference is a mechanical regeneration**: the
      version/algorithm/description header is untouched, no control the base locked has been
      delisted, and every added path is a file the verifier itself considers a protected control.

    Why the lock legitimately differs: two Alpha commits (`28954b31` added real streaming checks,
    `eddddf53` fixed a database name that made the step error out before running) edited
    `.github/workflows/real-local-ai-verification.yml` without re-locking it. The base `b313c9df`
    therefore does NOT verify, and copying its lock into the merge reproduces that failure. This
    invariant reports FAIL on such a tree — the truth about it, not a merge defect — and the fix
    is the reviewed regeneration in `docs/POLICY_LOCK_merged_candidate.patch`, never an edit here.
    """
    if not args.policy_lock_base:
        return "BLOCKED", "--policy-lock-base not given"
    relative = "security/POLICY_LOCK.json"
    base = args.policy_lock_base
    base_code, base_source = _git("show", f"{base}:{relative}")
    here = (REPO_ROOT / relative).exists()
    if not here and base_code != 0:
        return "BLOCKED", (
            f"no {relative} on this tree and none in {base} — the lock is Alpha-only, "
            "so there is nothing to compare"
        )
    if not here:
        return "BLOCKED", (
            f"{relative} is not on this tree yet (it arrives from Alpha at merge); nothing to compare"
        )
    if base_code != 0:
        return "BLOCKED", (
            f"{base} has no {relative} — pass the sha that owns the lock (the Alpha candidate)"
        )

    verifier = _repo_lock_verifier()
    if verifier is None:
        return "BLOCKED", (
            "backend/scripts/security/verify_policy_lock.py is not on this tree, "
            "so the lock cannot be verified (it arrives from Alpha at merge)"
        )
    try:
        lock = json.loads((REPO_ROOT / relative).read_text(encoding="utf-8"))
        base_lock = json.loads(base_source)
    except (OSError, json.JSONDecodeError) as exc:
        return "BLOCKED", f"cannot read {relative} or its {base} copy: {exc}"

    breakages = verifier.verify_lock(REPO_ROOT, lock)
    if breakages:
        shown = "; ".join(breakages[:3])
        more = f" (+{len(breakages) - 3} more)" if len(breakages) > 3 else ""
        return "FAIL", (
            f"{relative} does not verify against this tree ({len(breakages)} problems): "
            f"{shown}{more}"
        )

    controls = lock.get("files") or {}
    base_controls = base_lock.get("files") or {}
    if lock == base_lock:
        return "PASS", f"{relative} identical to {base} and verifies ({len(controls)} controls)"

    structural: list[str] = []
    for field in ("version", "algorithm", "description"):
        if lock.get(field) != base_lock.get(field):
            structural.append(f"header field {field!r} changed")
    delisted = sorted(set(base_controls) - set(controls))
    if delisted:
        structural.append(f"{len(delisted)} control(s) delisted: {', '.join(delisted[:3])}")
    protected = set(verifier.protected_files(REPO_ROOT))
    stray = sorted(set(controls) - protected)
    if stray:
        structural.append(f"{len(stray)} entry(ies) are not protected controls: {', '.join(stray[:3])}")
    if structural:
        return "FAIL", f"{relative} differs from {base} and the difference is not mechanical: " + "; ".join(structural)

    changed = sorted(path for path in controls if path in base_controls and controls[path] != base_controls[path])
    added = sorted(set(controls) - set(base_controls))
    return "PASS", (
        f"regenerated mechanically against {base}: {len(changed)} changed, {len(added)} added, "
        f"0 delisted; verifies clean ({len(controls)} controls)"
    )


def i13_first_cycle_go_false(_args: argparse.Namespace) -> tuple[str, str]:
    """first_cycle_go must not have been flipped by the integration."""
    hits: list[str] = []
    for relative in ("backend/app/config.py", "backend/.env.example", "security/POLICY_LOCK.json"):
        source = _read(relative)
        if source is None:
            continue
        for line in source.splitlines():
            stripped = line.strip()
            if "first_cycle_go" in stripped and ("=true" in stripped.replace(" ", "") or "=True" in stripped.replace(" ", "")):
                hits.append(f"{relative}: {stripped}")
    if hits:
        return "FAIL", f"first_cycle_go appears enabled: {hits}"
    return "PASS", "no first_cycle_go=true found in config templates/policy"


def i14_chat_router_union(_args: argparse.Namespace) -> tuple[str, str]:
    """`routers/chat.py` is a real conflict that the handoff matrix did not list.

    Alpha restructured this router around `_ChatContext`/`_build_chat_context`/
    `_stream_chat_reply` (SSE streaming); my branch restructured the same entry points around
    `_get_or_create_chat_turn`/`_replay_chat_request` (request idempotency, migration 0090).
    Taking either side wholesale is silent damage: no streaming, or a second billable provider
    call on a retried send. Both must survive, and the streaming entry point must resolve its
    turn through the idempotent resolver — not a resurrected second turn path.
    """
    relative = "backend/app/routers/chat.py"
    _requires(relative)
    tree = _tree(relative)
    if tree is None:
        return "BLOCKED", "routers/chat.py could not be parsed"
    streaming = all(_function(tree, name) is not None for name in ("_build_chat_context", "_stream_chat_reply"))
    idempotent = all(_function(tree, name) is not None for name in ("_get_or_create_chat_turn", "_replay_chat_request"))
    dual_path = _function(tree, "_create_or_get_conversation_and_user_message") is not None
    problems: list[str] = []
    if not streaming:
        problems.append("Alpha's streaming path (_build_chat_context/_stream_chat_reply) is missing")
    if not idempotent:
        problems.append("idempotent turn resolver (_get_or_create_chat_turn/_replay_chat_request) is missing")
    if dual_path:
        problems.append("a second turn path (_create_or_get_conversation_and_user_message) survived")
    stream_entry = _function(tree, "stream_chat")
    if streaming and idempotent and stream_entry is not None:
        wired = _called_names(stream_entry)
        if "_create_or_get_conversation_and_user_message" in wired:
            problems.append("stream_chat still uses the old non-idempotent turn path")
        elif "_get_or_create_chat_turn" not in wired and "_stream_chat_reply" not in wired:
            problems.append("stream_chat does not resolve its turn through a known path")
    if problems:
        return "FAIL", "; ".join(problems)
    return "PASS", "streaming and idempotent turn resolution both present, one turn path"


INVARIANTS: tuple[tuple[str, str, str, Callable[[argparse.Namespace], tuple[str, str]]], ...] = (
    ("INV-01", "Strömningskontrakt", "Alpha: StreamChunk/chat_stream, ingen ChatStreamChunk", i01_streaming_contract),
    ("INV-02", "Ollama egress-flaggor", "Alpha:s chat_stream + follow_redirects/trust_env=False", i02_ollama_egress_flags),
    ("INV-03", "LOCAL_ONLY_MODE", "settings.local_only_mode finns (§7.1)", i03_local_only_mode),
    ("INV-04", "Egress-modell komplett", "lease-assertion + filter + per-försök-omverifiering", i04_per_attempt_egress_recheck),
    ("INV-05", "detect_conflict fail-closed", "noll provideranrop, ingen påhittad ägare (§7.2)", i05_detect_conflict_fail_closed),
    ("INV-06", "Lease-kontrakt", "chat_with_fallback(lease_worker_id, lease_generation)", i06_lease_contract),
    ("INV-07", "Agentauktorisering", "authorized_by_user_id obligatorisk, purpose alignad", i07_agent_authorization_contract),
    ("INV-08", "MessageOut-superset", "status + field_validator bevarade", i08_message_out_superset),
    ("INV-09", "Corpus review-union", "max_tokens=400 och effect_guard=True", i09_corpus_review_union),
    ("INV-10", "En migrationshead", "exakt en head, additiv kedja", i10_single_migration_head),
    ("INV-11", "Inga tappade tester", "ingen parents testfil försvinner i mergen", i11_no_dropped_tests),
    (
        "INV-12",
        "POLICY_LOCK intakt",
        "Z.ai äger R31 (§7.3); en regenerering ska vara mekanisk och verifiera",
        i12_policy_lock_integrity,
    ),
    ("INV-13", "first_cycle_go=false", "ingen aktivering i denna leverans", i13_first_cycle_go_false),
    ("INV-14", "Chattrouter-union", "streaming + idempotent turn-resolver, en turn-väg", i14_chat_router_union),
)


def build(args: argparse.Namespace) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for identifier, title, rule, check in INVARIANTS:
        try:
            status, evidence = check(args)
        except FileNotFoundError as exc:
            status, evidence = "BLOCKED", f"file missing: {exc}"
        except Exception as exc:  # a checker bug must never look like a PASS
            status, evidence = "BLOCKED", f"{type(exc).__name__}: {exc}"
        results.append(
            {
                "id": identifier,
                "title": title,
                "rule": rule,
                "status": status,
                "evidence": evidence,
            }
        )
    statuses = [result["status"] for result in results]
    if "FAIL" in statuses:
        overall = "FAIL"
    elif "BLOCKED" in statuses:
        overall = "BLOCKED"
    else:
        overall = "PASS"
    return {
        "status": overall,
        "generated_at": _now(),
        "commit": (_git("rev-parse", "HEAD")[1] or "").strip(),
        "branch": (_git("rev-parse", "--abbrev-ref", "HEAD")[1] or "").strip(),
        "merged": args.merged or None,
        "expected_head": args.expected_head or None,
        "invariants": results,
        "summary": {status: statuses.count(status) for status in ("PASS", "FAIL", "BLOCKED")},
    }


def render(report: dict[str, Any]) -> str:
    lines = [
        "# Founder Alpha merge invariants",
        "",
        f"- **Overall:** `{report['status']}`",
        f"- **Commit:** `{report['commit']}` ({report['branch']})",
        f"- **Merged candidate:** {report['merged'] or '—'}",
        "",
        "| id | invariant | status | evidence |",
        "|---|---|---|---|",
    ]
    for item in report["invariants"]:
        lines.append(
            f"| {item['id']} | {item['title']} | `{item['status']}` | {item['evidence']} |"
        )
    lines.append("")
    lines.append(f"Totals: {report['summary']}")
    unmet = [item for item in report["invariants"] if item["status"] != "PASS"]
    if unmet:
        lines.append("")
        lines.append("## Not satisfied on this tree")
        for item in unmet:
            lines.append(f"- **{item['id']} {item['title']}** `{item['status']}` — {item['evidence']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--merged", default="", help="merge commit to check parents of (enables INV-11)")
    parser.add_argument(
        "--expected-head",
        default="0095",
        help="expected single alembic head (default 0095; pass '' to only check single-headedness)",
    )
    parser.add_argument(
        "--policy-lock-base",
        default="",
        help=(
            "sha that owns security/POLICY_LOCK.json — pass the Alpha candidate (b313c9df); "
            "86671468 has no lock, so it yields BLOCKED, not PASS"
        ),
    )
    parser.add_argument("--out", default="", help="write the JSON report here")
    args = parser.parse_args()

    report = build(args)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    print(render(report))
    return 0 if report["status"] == "PASS" else (2 if report["status"] == "FAIL" else 3)


if __name__ == "__main__":
    raise SystemExit(main())
