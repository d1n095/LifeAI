"""Small real process-boundary recovery probe for the Level-2 journal."""
from __future__ import annotations

import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile


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
