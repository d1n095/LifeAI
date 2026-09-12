"""Process-isolated calls into the exact frozen component worktrees.

This is a compatibility mechanism, not a code copy: the child process imports the frozen
implementation from its own checkout and returns bounded JSON evidence.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class FrozenCall:
    name: str
    sha: str
    module: str
    function: str
    result: dict[str, object]


def call_frozen_json(*, name: str, worktree: str, expected_sha: str, module: str, function: str,
                     kwargs: dict[str, object] | None = None, args: tuple[object, ...] = ()) -> FrozenCall:
    observed = subprocess.check_output(["git", "-C", worktree, "rev-parse", "HEAD"], text=True).strip()
    if observed != expected_sha:
        raise RuntimeError(f"frozen component {name} SHA mismatch")
    script = (
        "import dataclasses,importlib,json,sys; "
        "m=importlib.import_module(sys.argv[1]); f=getattr(m,sys.argv[2]); "
        "v=f(*json.loads(sys.argv[3]),**json.loads(sys.argv[4])); "
        "print(json.dumps(dataclasses.asdict(v) if dataclasses.is_dataclass(v) else {'value':v}, default=str))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(worktree, "backend")
    raw = subprocess.check_output([sys.executable, "-c", script, module, function, json.dumps(args), json.dumps(kwargs or {})], cwd=worktree, env=env, text=True)
    return FrozenCall(name, observed, module, function, json.loads(raw))
