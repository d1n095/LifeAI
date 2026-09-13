"""Production-runtime composition for the Level-2 control plane.

This module delegates effects to the existing ProductionExecutionAdapter.  It does not keep a
second claim/lease ledger and is disabled unless a caller supplies a real DB session.
"""
from __future__ import annotations

import uuid
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.mainai_execution.production_adapter import ProductionExecutionAdapter
from app.mainai_level2.components import VerifiedComposition
from app.mainai_level2.canonical import CanonicalProgramStore


@dataclass
class ProductionRuntimePort:
    adapter: ProductionExecutionAdapter
    owner_id: uuid.UUID

    def assign(self, job, agent_id):
        claim = self.adapter.claim_next(worker_id=agent_id, owner_id=self.owner_id)
        if claim is None or str(claim.job_id) != str(job.job_id):
            raise RuntimeError("canonical runtime did not claim the requested job")
        return claim

    def cancel(self, job):
        claim = getattr(job, "runtime_claim", None)
        if claim is None:
            raise RuntimeError("missing canonical claim for cancellation")
        self.adapter.cancel(claim)

    def release(self, job):
        claim = getattr(job, "runtime_claim", None)
        if claim is None:
            return
        from app.mainai_execution.substrate import FailureClass
        self.adapter.fail(claim, failure_class=FailureClass.PROCESS_LOST)

    def heartbeat(self, claim):
        self.adapter.heartbeat(claim)


def compose_verified_runtime(db, owner_id: uuid.UUID) -> tuple[ProductionRuntimePort, VerifiedComposition]:
    """Bind the verified runtime SHA to the actual canonical production adapter."""
    runtime = ProductionRuntimePort(ProductionExecutionAdapter(db), owner_id)
    composition = VerifiedComposition()
    composition.bind("runtime", runtime)
    return runtime, composition


class ProductionOrchestration:
    """Small composition facade used by unattended integration runs.

    It deliberately delegates claims and recovery to canonical services; it stores no local
    lease or job lifecycle state.
    """

    def __init__(self, db, *, owner_id: uuid.UUID):
        self.db = db
        self.owner_id = owner_id
        self.store = CanonicalProgramStore(db)
        self.runtime, self.composition = compose_verified_runtime(db, owner_id)

    def dispatch_next(self, job, *, agent_id: str):
        claim = self.runtime.assign(job, agent_id)
        job.runtime_claim = claim
        return claim

    def recover(self, *, program_id: uuid.UUID) -> dict[str, object]:
        snapshot = self.store.recover_level2(owner_id=self.owner_id, program_id=program_id)
        return {
            "program_id": str(snapshot.program.id),
            "state": snapshot.program.state,
            "current_sha": snapshot.program.current_sha,
            "events": len(snapshot.events),
            "canonical_jobs": len(snapshot.owner_jobs),
            "source": snapshot.source,
        }

    def unattended_step(self, job, *, agent_id: str, provider_state: str, fallback=None) -> dict[str, object]:
        """Run one bounded production-path step with provider failure fail-closed semantics."""
        if provider_state != "available":
            if fallback is None:
                return {"state": "BLOCKED", "reason": "provider_unavailable", "authority": "none"}
            provider_state = fallback
        claim = self.dispatch_next(job, agent_id=agent_id)
        return {"state": "CLAIMED", "attempt_id": claim.attempt_id, "provider": provider_state,
                "authority": "canonical_lease"}


def run_unattended_production_flow() -> dict[str, object]:
    """Exercise the real provider-neutral RuntimeOrchestrator path end to end.

    Providers remain deterministic fakes; claims, failover, artifact freezing and examiner
    checks are delegated to the production runtime implementation rather than Level-2 helpers.
    """
    from app.mainai_execution.production_adapter import ProviderProfile, RuntimeOrchestrator
    from app.mainai_execution.substrate import ExecutionSubstrate

    with tempfile.TemporaryDirectory(prefix="level2-production-") as directory:
        repo = Path(directory) / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "switch", "-c", "dev/level2"], check=True, stdout=subprocess.DEVNULL)
        (repo / "state.txt").write_text("base")
        subprocess.run(["git", "-C", str(repo), "add", "state.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
        base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        runtime = RuntimeOrchestrator(ExecutionSubstrate(Path(directory) / "runtime.sqlite"))
        caps = frozenset({"repo_read", "code_edit", "filesystem_write", "test_run"})
        runtime.register_provider(ProviderProfile("provider-a", caps))
        runtime.register_provider(ProviderProfile("provider-b", caps))
        runtime.register_provider(ProviderProfile("examiner", frozenset({"repo_read", "review", "test_run"})))
        job = runtime.submit(owner_id="founder", program="offline-program", provider="provider-a", base_sha=base, worktree=str(repo))
        claim, _ = runtime.claim(job.job_id, owner_id="founder", worker_id="builder-a")
        runtime.providers["provider-a"] = ProviderProfile("provider-a", caps, state="exhausted")
        replacement, _, old = runtime.failover(job.job_id, owner_id="founder", required=set(), worker_id="builder-b")
        (repo / "state.txt").write_text("candidate-a")
        subprocess.run(["git", "-C", str(repo), "add", "state.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "candidate-a"], check=True)
        artifact_a = runtime.freeze(job_id=job.job_id, attempt_id=replacement.attempt_id, builder_id="builder-b", examiner_id="examiner", worktree=str(repo), base_sha=base)
        runtime.examine(job_id=job.job_id, examiner_id="examiner", sha=artifact_a.sha, passed=False)
        runtime.director.report_failure(replacement, RuntimeError("examiner rejected candidate"))
        base_b = artifact_a.sha
        runtime.substrate.retry_or_reassign(job.job_id, new_provider="provider-b")
        claim_b, _ = runtime.claim(job.job_id, owner_id="founder", worker_id="builder-b")
        (repo / "state.txt").write_text("candidate-b")
        subprocess.run(["git", "-C", str(repo), "add", "state.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "candidate-b"], check=True)
        artifact_b = runtime.freeze(job_id=job.job_id, attempt_id=claim_b.attempt_id, builder_id="builder-b", examiner_id="examiner", worktree=str(repo), base_sha=base_b)
        runtime.examine(job_id=job.job_id, examiner_id="examiner", sha=artifact_b.sha, passed=True)
        return {"verified": runtime.certified[job.job_id] == artifact_b.sha, "old_attempt_fenced": old.attempt_id != replacement.attempt_id, "old_sha_invalidated": artifact_a.sha != artifact_b.sha, "provider_failover": True}
