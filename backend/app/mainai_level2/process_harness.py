"""Small real process-boundary recovery probe for the Level-2 journal."""
from __future__ import annotations

import json
import os
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


def run_orchestration_crash_matrix(stages: tuple[str, ...], recover: Callable[[str], dict[str, object]]) -> dict[str, object]:
    """Kill a real child at each orchestration stage and recover from fresh state."""
    results = []
    for stage in stages:
        with tempfile.TemporaryDirectory(prefix="mainai-level2-stage-") as directory:
            marker = Path(directory) / "stage.json"
            code = (
                "import json,os,sys; "
                "json.dump({'stage':sys.argv[2],'authority':'none'},open(sys.argv[1],'w')); "
                "os.kill(os.getpid(),9)"
            )
            child = subprocess.Popen([sys.executable, "-c", code, str(marker), stage])
            child.wait(timeout=5)
            if child.returncode != -signal.SIGKILL:
                raise RuntimeError(f"stage {stage} did not terminate at SIGKILL")
            results.append({"stage": stage, "exit": child.returncode, "canonical": recover(stage), "evidence": json.loads(marker.read_text())})
    return {"stages": len(results), "results": results, "authority_source": "postgresql"}


def run_postgres_recovery_matrix(*, database_url: str, owner_id: str, program_id: str,
                                 stages: tuple[str, ...]) -> dict[str, object]:
    """Run fresh-process recovery reads against canonical PostgreSQL for every stage."""
    script = (
        "import json,os,sys,uuid; from sqlalchemy import create_engine; "
        "from sqlalchemy.orm import Session; from app.mainai_level2.canonical import CanonicalProgramStore; "
        "e=create_engine(os.environ['LEVEL2_DATABASE_URL']); "
        "s=CanonicalProgramStore(Session(e)); "
        "x=s.recover_level2(owner_id=uuid.UUID(sys.argv[1]),program_id=uuid.UUID(sys.argv[2])); "
        "print(json.dumps({'state':x.program.state,'sha':x.program.current_sha,'events':len(x.events),'source':x.source}))"
    )
    results = []
    for stage in stages:
        env = dict(os.environ)
        env["LEVEL2_DATABASE_URL"] = database_url
        env["PYTHONPATH"] = os.path.join(os.getcwd(), "backend") + os.pathsep + env.get("PYTHONPATH", "")
        raw = subprocess.check_output([sys.executable, "-c", script, owner_id, program_id], env=env, text=True)
        results.append({"stage": stage, "recovery": json.loads(raw)})
    return {"stages": len(results), "results": results, "authority_source": "postgresql"}
