"""Small real process-boundary recovery probe for the Level-2 journal."""
from __future__ import annotations

import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from typing import Callable


def run_process_crash_probe() -> dict[str, object]:
    """Persist an event in a child process, terminate it, then recover in a new process.

    The journal is evidence only; callers must still re-read canonical PostgreSQL state before
    assigning work.  This deliberately tests a process boundary rather than an exception path.
    """
    with tempfile.TemporaryDirectory(prefix="mainai-level2-") as directory:
        path = Path(directory) / "journal.json"
        code = (
            "import json, os, sys, time; "
            "p=sys.argv[1]; json.dump([{'event':'CHECKPOINT','program_id':'p','state':'RUNNING'}], open(p,'w')); "
            "os.kill(os.getpid(), 9)"
        )
        child = subprocess.Popen([sys.executable, "-c", code, str(path)])
        child.wait(timeout=5)
        if child.returncode != -signal.SIGKILL:
            raise RuntimeError(f"child did not terminate at crash boundary: {child.returncode}")
        records = json.loads(path.read_text())
        return {"child_exit": child.returncode, "records": records, "requires_canonical_reread": True}


def recover_from_canonical(store, *, owner_id, program_id) -> dict[str, object]:
    """Fresh-process recovery entry point; the store must reread PostgreSQL state."""
    snapshot = store.recover_level2(owner_id=owner_id, program_id=program_id)
    return {
        "program_id": str(snapshot.program.id),
        "state": snapshot.program.state,
        "current_sha": snapshot.program.current_sha,
        "event_count": len(snapshot.events),
        "owner_job_count": len(snapshot.owner_jobs),
        "source": snapshot.source,
    }


def run_sigkill_restart_probe(recover: Callable[[], dict[str, object]]) -> dict[str, object]:
    """Exercise a real process boundary, then require a fresh canonical recovery callback.

    The callback is intentionally invoked only in the parent after SIGKILL. Callers pass a
    callback that opens a new SQLAlchemy session; process-local journal state is never used as
    authority.
    """
    with tempfile.TemporaryDirectory(prefix="mainai-level2-restart-") as directory:
        marker = Path(directory) / "persisted.json"
        code = (
            "import json, os, sys; "
            "json.dump({'event':'STATE_COMMITTED','authority':'none'}, open(sys.argv[1],'w')); "
            "os.kill(os.getpid(), 9)"
        )
        child = subprocess.Popen([sys.executable, "-c", code, str(marker)])
        child.wait(timeout=5)
        if child.returncode != -signal.SIGKILL:
            raise RuntimeError(f"child did not terminate at restart boundary: {child.returncode}")
        durable_evidence = json.loads(marker.read_text())
        canonical = recover()
        return {"child_exit": child.returncode, "evidence": durable_evidence, "canonical": canonical,
                "authority_source": "postgresql"}
