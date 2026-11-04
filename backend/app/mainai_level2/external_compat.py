"""Compatibility probes for frozen components kept in separate worktrees."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class ExternalComponentProbe:
    name: str
    worktree: str
    expected_sha: str
    observed_sha: str
    import_module: str
    required_methods: tuple[str, ...]
    compatible: bool
    reason: str


def probe_external_component(*, name: str, worktree: str, expected_sha: str,
                             import_module: str, required_methods: tuple[str, ...] = ()) -> ExternalComponentProbe:
    """Run a detached-worktree probe; no code is copied and no authority is granted."""
    observed = subprocess.check_output(["git", "-C", worktree, "rev-parse", "HEAD"], text=True).strip()
    code = (
        "import importlib,json,sys; "
        "m=importlib.import_module(sys.argv[1]); "
        "print(json.dumps({'module':sys.argv[1], 'methods':{x:callable(getattr(m,x,None)) for x in sys.argv[2:]}}))"
    )
    try:
        payload = json.loads(subprocess.check_output([sys.executable, "-c", code, import_module, *required_methods], cwd=worktree, text=True))
    except (subprocess.CalledProcessError, OSError) as exc:
        return ExternalComponentProbe(name, worktree, expected_sha, observed, import_module,
                                      required_methods, False, f"seam import failed: {type(exc).__name__}")
    missing = [method for method, present in payload["methods"].items() if not present]
    compatible = observed == expected_sha and not missing
    reason = "exact SHA and seam match" if compatible else f"sha={observed}, missing={missing}"
    return ExternalComponentProbe(name, worktree, expected_sha, observed, import_module, required_methods, compatible, reason)
