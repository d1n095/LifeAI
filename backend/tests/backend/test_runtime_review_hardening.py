"""Independent-review regressions for the provider-agnostic execution runtime."""
from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.mainai_execution.substrate import (
    DirectorContract,
    ExecutionSubstrate,
    LeaseLostError,
    SubstrateError,
    completion_evidence,
)
from app.mainai_execution.production_adapter import (
    ProductionRuntimeError,
    ProviderProfile,
    RuntimeOrchestrator,
    freeze_artifact,
)


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def _repo(root: Path, branch: str = "work/runtime-review") -> tuple[Path, str]:
    path = root / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", branch)
    _git(path, "config", "user.email", "runtime@test.local")
    _git(path, "config", "user.name", "Runtime")
    (path / "value.txt").write_text("base\n")
    _git(path, "add", "value.txt")
    _git(path, "commit", "-q", "-m", "base")
    return path, _git(path, "rev-parse", "HEAD")


def test_completion_rechecks_exact_sha_at_effect_point(tmp_path):
    path, base = _repo(tmp_path)
    db = ExecutionSubstrate(tmp_path / "runtime.sqlite")
    claim = db.claim(db.create_job(base_sha=base, worktree=str(path), provider="fake"), worker_id="builder")
    (path / "value.txt").write_text("changed\n")
    _git(path, "add", "value.txt")
    _git(path, "commit", "-q", "-m", "edit")
    evidence = completion_evidence(claim)
    (path / "value.txt").write_text("stale-after-evidence\n")
    _git(path, "add", "value.txt")
    _git(path, "commit", "-q", "-m", "race")
    with pytest.raises(SubstrateError, match="stale"):
        db.complete(claim, evidence=evidence)


def test_protected_ref_is_rejected_before_completion_effect(tmp_path):
    path, base = _repo(tmp_path, branch="main")
    db = ExecutionSubstrate(tmp_path / "runtime.sqlite")
    claim = db.claim(db.create_job(base_sha=base, worktree=str(path), provider="fake"), worker_id="builder")
    (path / "value.txt").write_text("changed\n")
    _git(path, "add", "value.txt")
    _git(path, "commit", "-q", "-m", "edit")
    with pytest.raises(SubstrateError, match="protected"):
        completion_evidence(claim)


def test_cancel_then_restart_cannot_resurrect_claim(tmp_path):
    path, base = _repo(tmp_path)
    db_path = tmp_path / "runtime.sqlite"
    db = ExecutionSubstrate(db_path)
    director = DirectorContract(db)
    job = director.submit_job(owner_id="alice", program="cancel", provider="fake", base_sha=base, worktree=str(path)).job_id
    claim = director.claim_job(job, owner_id="alice", worker_id="builder")
    director.cancel_job(job)
    recovered = DirectorContract(ExecutionSubstrate(db_path)).recover_incomplete_jobs()
    assert recovered
    with pytest.raises(LeaseLostError):
        db.renew(claim)
    assert DirectorContract(ExecutionSubstrate(db_path)).inspect_job(job)["state"] == "cancelled"


def test_release_is_fenced_and_claim_race_has_one_winner(tmp_path):
    path, base = _repo(tmp_path)
    db = ExecutionSubstrate(tmp_path / "runtime.sqlite")
    job = db.create_job(base_sha=base, worktree=str(path), provider="fake")

    def claim():
        try:
            return db.claim(job, worker_id="worker")
        except SubstrateError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(), range(2)))
    winner = next(item for item in claims if item is not None)
    db.release_claim(winner)
    with pytest.raises(LeaseLostError):
        db.release_claim(winner)


def test_production_review_rechecks_frozen_sha_and_examiner(tmp_path):
    path, base = _repo(tmp_path)
    (path / "value.txt").write_text("changed\n")
    _git(path, "add", "value.txt")
    _git(path, "commit", "-q", "-m", "edit")
    orchestrator = RuntimeOrchestrator(ExecutionSubstrate(tmp_path / "runtime.sqlite"))
    orchestrator.register_provider(ProviderProfile("examiner", frozenset({"review"})))
    artifact = freeze_artifact(job_id="job", attempt_id="attempt", builder_id="builder", examiner_id="examiner", worktree=str(path), base_sha=base)
    orchestrator.frozen["job"] = artifact
    (path / "value.txt").write_text("moved\n")
    _git(path, "add", "value.txt")
    _git(path, "commit", "-q", "-m", "moved-after-freeze")
    with pytest.raises(ProductionRuntimeError, match="moved"):
        orchestrator.examine(job_id="job", examiner_id="examiner", sha=artifact.sha, passed=True)
