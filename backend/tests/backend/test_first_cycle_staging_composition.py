"""Composition integrity for the first-cycle staging candidate.

These checks belong to THIS integration: exact frozen-input ancestry, a single Alembic head,
exactly one npm-audit policy, no Track B, no #280 activation-security migration, and no
automatic merge/deploy. They do not implement Codex grants or Git transport.
"""

from __future__ import annotations

import json
import subprocess
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
}

EXCLUDED = {
    "pr_280": "37bca08a287f00b6282222047340bffa949ff788",
}


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO_ROOT, text=True).strip()


def test_manifest_records_exact_frozen_input_shas():
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    inputs = payload["inputs"]
    for key, sha in FROZEN_INPUTS.items():
        assert inputs[key]["sha"] == sha, key
        assert len(sha) == 40
    assert payload["excluded"]["pr_280"]["sha"] == EXCLUDED["pr_280"]
    assert payload["excluded"]["pr_280"]["reason"] == "RETURN_TO_BUILDER"
    assert payload["merge"]["strategy"] == "no-ff merge of #282 onto #284"
    merge_sha = payload["merge"]["commit"]
    assert _git("rev-parse", merge_sha) == merge_sha
    parents = _git("rev-parse", f"{merge_sha}^1", f"{merge_sha}^2").splitlines()
    assert parents == payload["merge"]["parents"]
    rc = subprocess.call(["git", "merge-base", "--is-ancestor", merge_sha, "HEAD"], cwd=REPO_ROOT)
    assert rc == 0


def test_every_accepted_input_is_an_ancestor_of_this_candidate():
    head = _git("rev-parse", "HEAD")
    for key, sha in FROZEN_INPUTS.items():
        rc = subprocess.call(["git", "merge-base", "--is-ancestor", sha, head], cwd=REPO_ROOT)
        assert rc == 0, f"{key} {sha} is not an ancestor of {head}"
    known = subprocess.call(["git", "cat-file", "-e", EXCLUDED["pr_280"] + "^{commit}"], cwd=REPO_ROOT)
    if known == 0:
        rc = subprocess.call(
            ["git", "merge-base", "--is-ancestor", EXCLUDED["pr_280"], head], cwd=REPO_ROOT
        )
        assert rc != 0, "#280 must not be an ancestor of the staging candidate"


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
    assert 'expires: EXCEPTION_REVIEW_DEADLINE' in text
    assert 'EXCEPTION_REVIEW_DEADLINE = "2027-01-15"' in text
    assert "compatibleFixAvailable" in text


def test_required_checks_still_include_frontend_npm_audit():
    names = [c.name for c in ACTIVATION_REQUIRED_CHECKS]
    assert names.count("Frontend — npm audit") == 1
    assert names[-1] == "All required checks passed"
    assert len(ACTIVATION_REQUIRED_CHECKS) == 12


def test_defaults_keep_write_merge_deploy_and_activation_off():
    fields = Settings.model_fields
    assert fields["github_write_enabled"].default is False
    assert fields["github_auto_merge_enabled"].default is False
    assert fields["mainai_activation_mode"].default is False
    assert fields["mainai_legacy_prepare_pr_writes_enabled"].default is False
    assert "track_b_enabled" not in fields
    merge_attrs = [name for name in dir(GitHubClient) if "merge" in name.lower()]
    assert merge_attrs == [], merge_attrs
