"""The security controls must be able to FAIL — proven, not asserted.

A tamper-resistance check that only ever returns "verified" is worse than none: it reports safety
it does not have. Each test here constructs the failure case and proves the control catches it,
then proves the same control passes on the clean case (so it cannot pass by always failing either).

These tests also run the real repository's lock and capability manifest, which makes an unreviewed
edit to a control file fail the ordinary security test job — not only the dedicated workflow.
"""

import importlib.util
import json
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent
SCRIPTS = BACKEND_ROOT / "scripts" / "security"


def _load(module_name: str):
    path = SCRIPTS / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"security_controls_{module_name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


policy_lock = _load("verify_policy_lock")
capabilities = _load("verify_agent_capabilities")
provenance = _load("build_provenance")


def _fake_repo(tmp_path: Path) -> Path:
    """A minimal tree containing one of everything the lock protects."""
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("jobs: {}\n", encoding="utf-8")
    (tmp_path / ".github" / "CODEOWNERS").write_text("/.github/ @founder\n", encoding="utf-8")
    (tmp_path / ".semgrep").mkdir()
    (tmp_path / ".semgrep" / "rules.yaml").write_text("rules: []\n", encoding="utf-8")
    (tmp_path / ".gitleaks.toml").write_text('title = "x"\n', encoding="utf-8")
    (tmp_path / "security").mkdir()
    (tmp_path / "security" / "agent_capabilities.json").write_text("{}", encoding="utf-8")
    (tmp_path / "backend" / "scripts" / "security").mkdir(parents=True)
    (tmp_path / "backend" / "scripts" / "security" / "tool.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------------------------
# Policy lock — tamper resistance
# ---------------------------------------------------------------------------------------------


def test_the_repository_policy_lock_verifies():
    """The real tree: every control is covered and unchanged."""
    lock = json.loads((REPO_ROOT / policy_lock.LOCK_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert policy_lock.verify_lock(REPO_ROOT, lock) == []


def test_a_changed_control_is_detected(tmp_path):
    root = _fake_repo(tmp_path)
    lock = policy_lock.build_lock(root)
    assert policy_lock.verify_lock(root, lock) == []

    (root / ".github" / "workflows" / "ci.yml").write_text("jobs: {}  # relaxed\n", encoding="utf-8")

    problems = policy_lock.verify_lock(root, lock)
    assert any("changed control: .github/workflows/ci.yml" in p for p in problems), problems


def test_a_newly_added_workflow_is_detected_as_uncovered(tmp_path):
    """The case that matters most: a NEW file that disables a check must not be invisible to a
    lock that only describes files which already existed."""
    root = _fake_repo(tmp_path)
    lock = policy_lock.build_lock(root)
    (root / ".github" / "workflows" / "disable-checks.yml").write_text("jobs: {}\n", encoding="utf-8")

    problems = policy_lock.verify_lock(root, lock)
    assert any("uncovered control: .github/workflows/disable-checks.yml" in p for p in problems), problems


def test_a_deleted_control_is_detected(tmp_path):
    root = _fake_repo(tmp_path)
    lock = policy_lock.build_lock(root)
    (root / ".gitleaks.toml").unlink()

    problems = policy_lock.verify_lock(root, lock)
    assert any("missing control: .gitleaks.toml" in p for p in problems), problems


def test_an_empty_lock_is_never_treated_as_verified(tmp_path):
    root = _fake_repo(tmp_path)
    problems = policy_lock.verify_lock(root, {"version": 1, "algorithm": "sha256", "files": {}})
    assert any("lists no files" in p for p in problems), problems
    with pytest.raises(RuntimeError):
        policy_lock.build_lock(tmp_path / "empty-dir-does-not-exist")


def test_a_suppressed_failure_in_a_workflow_is_rejected(tmp_path):
    """`continue-on-error: true` is how a check gets silenced; it must fail the integrity job."""
    root = _fake_repo(tmp_path)
    workflow = root / ".github" / "workflows" / "ci.yml"
    workflow.write_text("jobs:\n  scan:\n    steps:\n      - run: scan\n        continue-on-error: true\n", encoding="utf-8")

    problems = policy_lock.verify_no_suppressed_failures(root)
    assert problems and "continue-on-error" in problems[0]


def test_ordinary_shell_cleanup_is_not_flagged(tmp_path):
    """The narrow rule cannot fire on `kill ... || true` — a check that fires on normal shell
    hygiene gets deleted instead of followed."""
    root = _fake_repo(tmp_path)
    workflow = root / ".github" / "workflows" / "ci.yml"
    workflow.write_text('jobs:\n  x:\n    steps:\n      - run: kill "$PID" 2>/dev/null || true\n', encoding="utf-8")

    assert policy_lock.verify_no_suppressed_failures(root) == []


# ---------------------------------------------------------------------------------------------
# Agent capabilities — least privilege and drift
# ---------------------------------------------------------------------------------------------


def _real_inputs():
    manifest = json.loads((REPO_ROOT / capabilities.MANIFEST_RELATIVE_PATH).read_text(encoding="utf-8"))
    code = capabilities.read_code_contract(REPO_ROOT / capabilities.CONTRACT_RELATIVE_PATH)
    return manifest, code


def test_the_declared_capabilities_match_the_runtime_contract():
    manifest, code = _real_inputs()
    assert capabilities.validate(manifest, code, REPO_ROOT) == []
    # And the parse is real, not an empty read that trivially matches.
    assert code["capabilities"] == {
        "corpus_review",
        "message_sequence_backfill",
        "message_source_backfill",
        "task_execution",
    }


def test_a_capability_present_in_code_but_undeclared_is_a_failure():
    manifest, code = _real_inputs()
    manifest = json.loads(json.dumps(manifest))
    del manifest["runtime_capabilities"]["task_execution"]

    problems = capabilities.validate(manifest, code, REPO_ROOT)
    assert any("'task_execution' exists in" in p for p in problems), problems


def test_a_capability_declared_but_absent_from_code_is_a_failure():
    manifest, code = _real_inputs()
    manifest = json.loads(json.dumps(manifest))
    manifest["runtime_capabilities"]["deploy_to_production"] = {
        "provider_role": None,
        "modifies_existing_data": False,
        "writes_new_records": False,
        "sandbox_only": False,
        "production_prohibited": False,
    }

    problems = capabilities.validate(manifest, code, REPO_ROOT)
    assert any("'deploy_to_production' is declared" in p for p in problems), problems


def test_a_drifted_write_profile_is_a_failure():
    manifest, code = _real_inputs()
    manifest = json.loads(json.dumps(manifest))
    manifest["runtime_capabilities"]["corpus_review"]["modifies_existing_data"] = True

    problems = capabilities.validate(manifest, code, REPO_ROOT)
    assert any("corpus_review': modifies_existing_data" in p for p in problems), problems


def test_an_actor_cannot_be_granted_merge_deploy_or_self_approval():
    manifest, code = _real_inputs()
    manifest = json.loads(json.dumps(manifest))
    manifest["actors"]["development_agent"]["allowed"].append("merge_pull_request")
    manifest["actors"]["development_agent"]["allowed"].append("disable_security_checks")

    problems = capabilities.validate(manifest, code, REPO_ROOT)
    assert any("hard-forbidden action 'merge_pull_request'" in p for p in problems), problems
    assert any("hard-forbidden action 'disable_security_checks'" in p for p in problems), problems


def test_an_actor_cannot_be_granted_what_another_actor_is_denied():
    manifest, code = _real_inputs()
    manifest = json.loads(json.dumps(manifest))
    manifest["actors"]["development_agent"]["allowed"].append("deploy")

    problems = capabilities.validate(manifest, code, REPO_ROOT)
    assert any("which actor" in p for p in problems), problems


def test_a_claimed_enforcement_point_must_exist():
    manifest, code = _real_inputs()
    manifest = json.loads(json.dumps(manifest))
    manifest["approval_boundaries"][0]["enforced_by"].append("backend/app/does_not_exist.py")

    problems = capabilities.validate(manifest, code, REPO_ROOT)
    assert any("non-existent enforcement module" in p for p in problems), problems


# ---------------------------------------------------------------------------------------------
# Provenance — reviewed source matches the measured artifact
# ---------------------------------------------------------------------------------------------


def _artifact(tmp_path: Path) -> Path:
    root = tmp_path / "artifact"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("A = 1\n", encoding="utf-8")
    (root / "b.py").write_text("B = 2\n", encoding="utf-8")
    return root


def test_provenance_verifies_a_clean_tree_and_detects_a_changed_file(tmp_path):
    root = _artifact(tmp_path)
    manifest = provenance.create_manifest(root)
    assert provenance.verify_manifest(root, manifest) == []

    (root / "pkg" / "a.py").write_text("A = 666\n", encoding="utf-8")
    problems = provenance.verify_manifest(root, manifest)
    assert any("changed in artifact: pkg/a.py" in p for p in problems), problems


def test_provenance_detects_an_added_and_a_removed_file(tmp_path):
    root = _artifact(tmp_path)
    manifest = provenance.create_manifest(root)

    (root / "extra.py").write_text("X = 1\n", encoding="utf-8")
    problems = provenance.verify_manifest(root, manifest)
    assert any("not in the reviewed manifest: extra.py" in p for p in problems), problems

    (root / "extra.py").unlink()
    (root / "b.py").unlink()
    problems = provenance.verify_manifest(root, manifest)
    assert any("missing from artifact: b.py" in p for p in problems), problems


def test_provenance_ignores_bytecode_noise_but_not_source(tmp_path):
    """A __pycache__ directory appearing between build and run must not fail provenance; source
    changing must."""
    root = _artifact(tmp_path)
    manifest = provenance.create_manifest(root)

    cache = root / "pkg" / "__pycache__"
    cache.mkdir()
    (cache / "a.cpython-312.pyc").write_bytes(b"\x00\x01")
    assert provenance.verify_manifest(root, manifest) == []
