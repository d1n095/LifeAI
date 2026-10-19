"""Composition integrity for the first-cycle staging candidate.

These checks belong to THIS integration: exact frozen-input SHAs in the manifest, a single
Alembic head, exactly one npm-audit policy, markers from each accepted input, no Track B,
no #280 activation-security migration, and no automatic merge/deploy.

Git ancestry is recorded in the manifest for full clones. These tests do not `git fetch`
or require a non-shallow history, because GitHub Actions checks out a shallow PR merge.
"""

from __future__ import annotations

import json
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.config import Settings
from app.integrations.github_client import GitHubClient
from app.mainai_execution.required_checks import ACTIVATION_REQUIRED_CHECKS

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
MANIFEST = REPO_ROOT / "docs" / "first_cycle_staging_manifest.json"

FROZEN_INPUTS = {
    "founder_alpha": "2fbe20aacf1203fc0e16d216ef55b666b2181619",
    "pr_263": "e33f494c4fc53704c494cecea788f3e22be36330",
    "pr_276": "09b87e9b70dc02889d39c2bae3d56458509d49cb",
    "pr_281": "abc88b4f479489c8c85111d754756058ab77eb22",
    "pr_282": "1e3ee3fd604fc540600f393e215a712b283686d4",
    "pr_284": "9c978ded05b05ad174ce003a3efad22682ec187d",
    "pr_275_frozen_parent_of_282": "eed3f4a380be71398a0481414518a29506a57ccb",
    "pr_285": "f5d9bf14340b0436d7470b28f6026342aeeaf3e6",
    "pr_286": "a255fa23bf29be624d3f24e6387f7ed25defce1e",
    "pr_287": "1f1819aa46d8fd4aef18db5fb625af51c02d3394",
    "pr_288": "1ea41d7db424cba6b4d9ec93da79fa85d809d2fb",
    "pr_289": "ca60ba54febb49e1c7585fb776811564d54e3d21",
}

EXCLUDED = {
    "pr_280": "37bca08a287f00b6282222047340bffa949ff788",
}


def _text(*parts: str) -> str:
    return (REPO_ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def test_manifest_records_exact_frozen_input_shas():
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    inputs = payload["inputs"]
    for key, sha in FROZEN_INPUTS.items():
        assert inputs[key]["sha"] == sha, key
        assert len(sha) == 40
    assert payload["excluded"]["pr_280"]["sha"] == EXCLUDED["pr_280"]
    assert payload["excluded"]["pr_280"]["reason"] == "RETURN_TO_BUILDER"
    assert payload["go"] is False
    assert payload["branch"] == "cursor/first-cycle-execution-hardening-p3-91c1"
    assert payload["pr"] != 289
    assert payload["successor_of"]["pr"] == 289
    assert payload["successor_of"]["sha"] == FROZEN_INPUTS["pr_289"]
    assert "certified Path A" not in inputs["pr_276"]["role"]
    assert payload["merge"]["strategy"] == "no-ff merge of #286 onto #285"
    assert payload["merge"]["commit"] == "9c4e30637b5dc568eb160e7a67178582ec7b2da1"
    assert payload["merge"]["parents"] == [
        FROZEN_INPUTS["pr_285"],
        FROZEN_INPUTS["pr_286"],
    ]
    assert payload["merge"]["merge_base"] == FROZEN_INPUTS["pr_284"]
    assert payload["merge"]["conflicts"] == []
    assert payload["merge"]["prior_composition"]["strategy"] == "no-ff merge of #282 onto #284"
    assert payload["merge"]["prior_composition"]["commit"] == "ec2e896c4d04dfdbd0f8f556ef3f08f36a18734c"
    assert payload["merge"]["prior_composition"]["parents"] == [
        FROZEN_INPUTS["pr_284"],
        FROZEN_INPUTS["pr_282"],
    ]


def test_composition_markers_from_each_accepted_input():
    github_client = _text("backend", "app", "integrations", "github_client.py")
    assert "def list_workflow_runs_for_sha" in github_client
    assert "def list_check_runs" in github_client
    assert "def merge" not in github_client
    assert "def merge_pull_request" not in github_client

    assert (REPO_ROOT / "backend" / "app" / "mainai_execution" / "envelope_scope.py").is_file()
    ci = _text(".github", "workflows", "ci.yml")
    assert '"mainai/**"' in ci
    assert "permissions:" in ci
    assert "npm run audit:policy:test" in ci
    assert "node scripts/check-npm-audit.js" in ci

    audit = _text("frontend", "scripts", "check-npm-audit.js")
    assert 'EXCEPTION_REVIEW_DEADLINE = "2027-01-15"' in audit
    assert "compatibleFixAvailable" in audit

    assert (REPO_ROOT / "backend" / "app" / "mainai_execution" / "review_receipt.py").is_file()
    assert (REPO_ROOT / "backend" / "app" / "mainai_execution" / "first_cycle_stop.py").is_file()
    execution_job = _text("backend", "app", "mainai_execution", "execution_job.py")
    assert "evaluate_first_cycle_review_receipt" in execution_job
    assert "stop_first_cycle" in execution_job
    assert "classify_publication_failure" in execution_job
    stop = _text("backend", "app", "mainai_execution", "first_cycle_stop.py")
    assert "def goal_is_autonomous_terminal" in stop
    assert "def classify_publication_failure" in stop
    assert "remote_fetch_failed" in stop
    inspector = _text("backend", "app", "mainai_execution", "recovery_inspector.py")
    assert "remote_fetch_failed" in inspector
    takeover = _text("backend", "app", "mainai_execution", "recovery_takeover.py")
    assert "MAX_AUTO_RECOVERY_TAKEOVERS_PER_TASK" in takeover
    ops = _text("backend", "app", "mainai_execution", "first_cycle_ops.py")
    assert "def founder_diagnostics" in ops
    assert (REPO_ROOT / "backend" / "app" / "mainai_execution" / "publication_errors.py").is_file()
    pub = _text("backend", "app", "mainai_execution", "publication_errors.py")
    assert "class ExternalBoundaryError" in pub
    assert "def map_publication_boundary_error" in pub
    replan = _text("backend", "app", "mainai_execution", "replan.py")
    assert "should_suppress_paid_replan" in replan
    assert "goal_is_autonomous_terminal" in replan
    git_harness = _text("backend", "tests", "backend", "first_cycle_acceptance", "_git.py")
    assert "def isolated_git_env" in git_harness
    cred_test = _text(
        "backend", "tests", "backend", "first_cycle_acceptance", "test_credential_isolation.py"
    )
    assert "CRED-8 ordinary pytest cannot access host git credentials" in cred_test


def test_single_alembic_head_is_activation_branch_protection_not_codex_0089():
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert heads == ["0089_activation_branch_protect"], heads
    versions = BACKEND_ROOT / "alembic" / "versions"
    assert (versions / "0089_mainai_activation_branch_protection.py").is_file()
    assert not (versions / "0089_mainai_activation_security.py").exists()
    assert not list(versions.glob("*claim_action*"))


def test_exactly_one_npm_audit_policy_script():
    scripts = list((REPO_ROOT / "frontend" / "scripts").glob("check-npm-audit.js"))
    assert [p.name for p in scripts] == ["check-npm-audit.js"]
    text = scripts[0].read_text(encoding="utf-8")
    assert "expires: EXCEPTION_REVIEW_DEADLINE" in text
    assert 'EXCEPTION_REVIEW_DEADLINE = "2027-01-15"' in text
    assert "compatibleFixAvailable" in text


def test_required_checks_still_include_frontend_npm_audit():
    names = [c.name for c in ACTIVATION_REQUIRED_CHECKS]
    assert names.count("Frontend — npm audit") == 1
    assert names[-1] == "All required checks passed"
    assert len(ACTIVATION_REQUIRED_CHECKS) == 12


def test_go_nogo_stays_false_with_codex_gaps_visible():
    payload = json.loads((REPO_ROOT / "docs" / "first_cycle_staging_go_nogo.json").read_text(encoding="utf-8"))
    assert payload["go"] is False
    remaining = " ".join(payload["remaining_codex"])
    for token in ("CRED-1/2/3", "E2E-3", "GRANT-PROD", "TA-12", "CI-6", "#280"):
        assert token in remaining
    assert payload["parent_285"] == FROZEN_INPUTS["pr_285"]
    assert payload["parent_286"] == FROZEN_INPUTS["pr_286"]
    assert payload["parent_287"] == FROZEN_INPUTS["pr_287"]
    assert payload["parent_288"] == FROZEN_INPUTS["pr_288"]
    assert payload["parent_289"] == FROZEN_INPUTS["pr_289"]
    assert payload["pr"] != 289
    assert payload["branch"] == "cursor/first-cycle-execution-hardening-p3-91c1"


def test_defaults_keep_write_merge_deploy_and_activation_off():
    fields = Settings.model_fields
    assert fields["github_write_enabled"].default is False
    assert fields["github_auto_merge_enabled"].default is False
    assert fields["mainai_activation_mode"].default is False
    assert fields["mainai_legacy_prepare_pr_writes_enabled"].default is False
    assert "track_b_enabled" not in fields
    merge_attrs = [name for name in dir(GitHubClient) if "merge" in name.lower()]
    assert merge_attrs == [], merge_attrs
