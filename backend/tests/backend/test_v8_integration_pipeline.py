"""Regression contracts for the V8 secure delivery pipeline.

The fixtures are real temporary Git repositories. No network, production database or
service is used, and every bundle is generated locally.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.integration import (  # noqa: E402
    bundle_import,
    common,
    conflict_analysis,
    release_manifest,
    rollback_verify,
    staging_build,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _write(repo: Path, relative: str, text: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _manifest(repo: Path, bundle: Path) -> Path:
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    manifest = repo / "SHA256SUMS"
    manifest.write_text(f"{digest}  {bundle.name}\n", encoding="utf-8")
    return manifest


def _repo(tmp_path: Path, *, conflict: bool = False) -> dict[str, object]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "v8@example.invalid")
    _git(repo, "config", "user.name", "V8 Test")
    _write(repo, "shared.txt", "base\n")
    _write(repo, "candidate.txt", "base\n")
    _write(repo, "target.txt", "base\n")
    base = _commit(repo, "base")

    _git(repo, "checkout", "-q", "-b", "integration", "main")
    _write(repo, "target.txt", "target\n")
    if conflict:
        _write(repo, "shared.txt", "target\n")
    target = _commit(repo, "target change")

    _git(repo, "checkout", "-q", "-b", "candidate", "main")
    _write(repo, "candidate.txt", "candidate\n")
    if conflict:
        _write(repo, "shared.txt", "candidate\n")
    candidate = _commit(repo, "candidate change")

    bundle = repo / "candidate.bundle"
    _git(repo, "bundle", "create", str(bundle), "candidate")
    manifest = _manifest(repo, bundle)
    return {
        "repo": repo,
        "base": base,
        "target": target,
        "candidate": candidate,
        "bundle": bundle,
        "manifest": manifest,
    }


def _import(fixture: dict[str, object], **overrides):
    values = {
        "repo": fixture["repo"],
        "bundle": fixture["bundle"],
        "manifest": fixture["manifest"],
        "expected_commit": fixture["candidate"],
        "expected_base": fixture["base"],
        "target_ref": "integration",
    }
    values.update(overrides)
    return bundle_import.import_bundle(**values)


def test_valid_bundle_imports_without_moving_branches(tmp_path):
    fixture = _repo(tmp_path)
    repo = fixture["repo"]
    refs_before = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")
    result = _import(fixture, worktree=tmp_path / "import-worktree")
    assert result["status"] == "PASS"
    assert result["commit"] == fixture["candidate"]
    assert result["duplicate_delivery"] is False
    assert result["already_integrated"] is False
    assert result["checks"]["security"]["status"] == "PASS"
    assert _git(repo, "rev-parse", "candidate") == fixture["candidate"]
    assert _git(repo, "rev-parse", "integration") == fixture["target"]
    assert _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads") == refs_before
    assert result["worktree"] is not None
    assert Path(result["worktree"]).exists()


def test_duplicate_delivery_is_idempotent_and_reported(tmp_path):
    fixture = _repo(tmp_path)
    first = _import(fixture)
    second = _import(fixture)
    assert first["duplicate_delivery"] is False
    assert second["duplicate_delivery"] is True
    assert second["already_imported"] is True
    assert second["status"] == "PASS"


def test_already_integrated_commit_is_detected(tmp_path):
    fixture = _repo(tmp_path)
    repo = fixture["repo"]
    _git(repo, "branch", "-f", "integration", "candidate")
    result = _import(fixture)
    assert result["status"] == "PASS"
    assert result["already_integrated"] is True


def test_corrupted_bundle_is_rejected(tmp_path):
    fixture = _repo(tmp_path)
    corrupted = tmp_path / "corrupted.bundle"
    corrupted.write_bytes(Path(fixture["bundle"]).read_bytes())
    with corrupted.open("r+b") as handle:
        handle.write(b"not-a-git-bundle")
    manifest = _manifest(tmp_path, corrupted)
    with pytest.raises(common.PipelineError) as exc:
        bundle_import.import_bundle(
            repo=fixture["repo"],
            bundle=corrupted,
            manifest=manifest,
            expected_commit=fixture["candidate"],
            expected_base=fixture["base"],
            target_ref="integration",
        )
    assert exc.value.status == "FAIL"


def test_missing_prerequisite_is_blocked(tmp_path):
    fixture = _repo(tmp_path)
    repo = fixture["repo"]
    partial = tmp_path / "partial.bundle"
    _git(repo, "bundle", "create", str(partial), "candidate", "^main")
    manifest = _manifest(tmp_path, partial)
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    _git(orphan, "init", "-q", "-b", "main")
    _git(orphan, "config", "user.email", "v8@example.invalid")
    _git(orphan, "config", "user.name", "V8 Test")
    _write(orphan, "unrelated.txt", "unrelated\n")
    _commit(orphan, "unrelated")
    with pytest.raises(common.PipelineError) as exc:
        bundle_import.import_bundle(
            repo=orphan,
            bundle=partial,
            manifest=manifest,
            expected_commit=fixture["candidate"],
            expected_base=fixture["base"],
            target_ref=None,
        )
    assert exc.value.status == "BLOCKED"


def test_symlink_and_credential_files_are_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "v8@example.invalid")
    _git(repo, "config", "user.name", "V8 Test")
    _write(repo, "safe.txt", "safe\n")
    (repo / "escape").symlink_to("/etc/passwd")
    _write(repo, ".env", "SAFE_PLACEHOLDER=not-a-real-secret\n")
    commit = _commit(repo, "unsafe fixture")
    bundle = repo / "unsafe.bundle"
    _git(repo, "bundle", "create", str(bundle), "main")
    manifest = _manifest(repo, bundle)
    with pytest.raises(common.PipelineError) as exc:
        bundle_import.import_bundle(
            repo=repo,
            bundle=bundle,
            manifest=manifest,
            expected_commit=commit,
            expected_base=commit,
            target_ref=None,
        )
    assert exc.value.status == "FAIL"
    assert "symlink" in exc.value.detail or "unsafe" in exc.value.detail


def test_conflict_analysis_reports_textual_conflict_without_resolving(tmp_path):
    fixture = _repo(tmp_path, conflict=True)
    result = conflict_analysis.analyze(
        repo=fixture["repo"],
        candidate=fixture["candidate"],
        target="integration",
    )
    assert result["status"] == "FAIL"
    assert "shared.txt" in result["textual_conflicts"]
    assert result["manual_review_required"] is True
    assert _git(fixture["repo"], "rev-parse", "integration") == fixture["target"]


def test_migration_collision_and_multiple_heads_are_detected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "v8@example.invalid")
    _git(repo, "config", "user.name", "V8 Test")
    _write(repo, "backend/alembic/versions/0001_base.py", 'revision = "0001"\ndown_revision = None\n')
    _commit(repo, "base migration")
    _git(repo, "checkout", "-q", "-b", "integration", "main")
    _write(repo, "backend/alembic/versions/0002_target.py", 'revision = "0002"\ndown_revision = "0001"\n')
    target = _commit(repo, "target migration")
    _git(repo, "checkout", "-q", "-b", "candidate", "main")
    _write(repo, "backend/alembic/versions/0002_candidate.py", 'revision = "0002"\ndown_revision = "0001"\n')
    _write(repo, "backend/alembic/versions/0003_candidate.py", 'revision = "0003"\ndown_revision = "0001"\n')
    candidate = _commit(repo, "candidate migration")
    result = conflict_analysis.analyze(repo=repo, candidate=candidate, target="integration")
    assert result["status"] == "FAIL"
    assert result["migration_collisions"]["revision_collisions"]
    assert result["migration_collisions"]["multiple_heads"] is True
    assert result["candidate"] == candidate
    assert target != candidate


def test_staging_migration_graph_rejects_duplicate_revision_and_heads(tmp_path):
    root = tmp_path / "worktree"
    versions = root / "backend" / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "0001_a.py").write_text('revision = "0001"\ndown_revision = None\n', encoding="utf-8")
    (versions / "0002_b.py").write_text('revision = "0002"\ndown_revision = "0001"\n', encoding="utf-8")
    assert staging_build.check_migrations(root)["status"] == "PASS"
    (versions / "0002_c.py").write_text('revision = "0002"\ndown_revision = "0001"\n', encoding="utf-8")
    result = staging_build.check_migrations(root)
    assert result["status"] == "FAIL"
    assert result["duplicate_revisions"]


def test_release_manifest_is_immutable_and_rejects_tampering(tmp_path):
    fixture = _repo(tmp_path)
    repo = fixture["repo"]
    actual_changes = _git(repo, "diff", "--name-only", fixture["base"], fixture["candidate"]).splitlines()
    source = {
        "base": fixture["target"],
        "deliveries": [
            {
                "name": "candidate",
                "commit": fixture["candidate"],
                "base": fixture["base"],
                "bundle": str(fixture["bundle"]),
                "bundle_sha256": hashlib.sha256(Path(fixture["bundle"]).read_bytes()).hexdigest(),
                "dependencies": [],
                "applied_changes": actual_changes,
                "verification": {"status": "PASS", "evidence": []},
                "blockers": [],
            }
        ],
        "required_checks": ["backend", "security"],
        "known_blockers": [],
        "artifacts": [],
    }
    source_path = tmp_path / "input.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    out = tmp_path / "manifest.json"
    built = release_manifest.build_manifest(repo=repo, input_path=source_path, out_path=out)
    assert built["status"] == "PASS"
    assert built["staging_ready"] is True
    verified = release_manifest.verify_manifest(repo=repo, manifest_path=out)
    assert verified["status"] == "PASS"
    with pytest.raises(common.PipelineError):
        release_manifest.build_manifest(repo=repo, input_path=source_path, out_path=out)
    tampered = json.loads(out.read_text(encoding="utf-8"))
    tampered["deliveries"][0]["bundle_sha256"] = "0" * 64
    out.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(common.PipelineError):
        release_manifest.verify_manifest(repo=repo, manifest_path=out)


def test_rollback_removes_staging_state_without_losing_commits(tmp_path):
    fixture = _repo(tmp_path)
    repo = fixture["repo"]
    backup = repo / rollback_verify.BACKUP_RESTORE
    backup.parent.mkdir(parents=True)
    backup.write_text("# existing PR #461 implementation placeholder\n", encoding="utf-8")
    result = rollback_verify.verify(repo=repo, source_ref=fixture["candidate"])
    assert result["status"] == "PASS"
    assert result["checks"]["source_commit_preserved"]["status"] == "PASS"
    assert result["checks"]["refs_preserved"]["status"] == "PASS"
    assert result["checks"]["worktree_cleanup"]["status"] == "PASS"
    assert result["checks"]["no_staging_worktree"]["status"] == "PASS"
    assert result["checks"]["repository_integrity"]["status"] == "PASS"
    assert _git(repo, "rev-parse", "candidate") == fixture["candidate"]
    assert _git(repo, "rev-parse", "integration") == fixture["target"]
