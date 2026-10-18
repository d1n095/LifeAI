"""TA-12 / TA-13 and related publication attacks. Oracle PASS; production FAIL until it matches."""

from __future__ import annotations

from pathlib import Path

import pytest

from first_cycle_acceptance._adapters import PRODUCTION
from first_cycle_acceptance._git import add_commit, checkout_branch, git, rev_parse
from first_cycle_acceptance._matrix import FAIL, PASS
from first_cycle_acceptance._publication import evaluate_publication
from first_cycle_acceptance.conftest import conclude

pytestmark = pytest.mark.first_cycle_acceptance

AUTHORIZED = ["backend/app/greeting.py", "backend/tests/test_greeting_doc.py"]


def _production_publication_accepts(cwd: Path, base_sha: str, authorized: list[str]) -> tuple[bool, str]:
    bound = PRODUCTION["envelope_scope"]
    worktree = PRODUCTION["worktree"]
    assert_fn = bound.symbols.get("assert_changed_files_within_authority")
    error_cls = bound.symbols.get("PathAEnvelopeError") or Exception
    diff_fn = worktree.symbols.get("git_changed_paths_no_renames")
    if assert_fn is None or diff_fn is None:
        return False, "production publication helpers missing"
    actual = diff_fn(cwd=cwd, base_sha=base_sha)
    try:
        assert_fn(actual_files=actual, editable_targets=authorized, envelope=None)
        return True, f"accepted actual={actual}"
    except error_cls as exc:
        return False, str(exc)


def test_oracle_accepts_single_regular_commit(isolated_repo):
    checkout_branch(isolated_repo.work, "ok", isolated_repo.base_sha)
    (isolated_repo.work / "backend" / "app" / "greeting.py").write_text(
        'def greet(name):\n    """Return a greeting."""\n    return f\'hello {name}\'\n'
    )
    add_commit(isolated_repo.work, "document greet")
    verdict = evaluate_publication(
        cwd=isolated_repo.work, base_sha=isolated_repo.base_sha, authorized_paths=AUTHORIZED
    )
    conclude(
        "PUB-1 oracle accepts one regular-file commit",
        PASS if verdict.ok else FAIL,
        boundary="harness.publication_oracle",
        detail=f"ok={verdict.ok}; reasons={verdict.reasons}; commits={verdict.commits}",
    )
    assert verdict.ok
    assert verdict.commit_count == 1


def test_ta12_intermediate_commit_not_concealed_by_clean_final_diff(isolated_repo):
    checkout_branch(isolated_repo.work, "ta12", isolated_repo.base_sha)
    secrets = isolated_repo.work / "backend" / "app" / "secrets.py"
    secrets.write_text("TOKEN='exfil'\n")
    add_commit(isolated_repo.work, "smuggle secrets")
    secrets.unlink()
    (isolated_repo.work / "backend" / "app" / "greeting.py").write_text(
        'def greet(name):\n    """Return a greeting."""\n    return f\'hello {name}\'\n'
    )
    add_commit(isolated_repo.work, "document greet and drop secrets")
    oracle = evaluate_publication(
        cwd=isolated_repo.work, base_sha=isolated_repo.base_sha, authorized_paths=AUTHORIZED
    )
    prod_ok, prod_detail = _production_publication_accepts(
        isolated_repo.work, isolated_repo.base_sha, AUTHORIZED
    )
    conclude(
        "TA-12 oracle rejects intermediate out-of-scope commit",
        PASS if (not oracle.ok and any("secrets.py" in r for r in oracle.reasons)) else FAIL,
        boundary="harness.publication_oracle.history",
        detail=f"oracle={oracle.reasons}; commits={oracle.commits}",
    )
    conclude(
        "TA-12 production publication inspects full history",
        FAIL if prod_ok else (PASS if not prod_ok else FAIL),
        boundary="production.assert_changed_files_within_authority",
        detail=(
            f"production_accepted={prod_ok} ({prod_detail}); "
            "net diff conceals intermediate secrets.py if only base..HEAD names are checked"
        ),
        production_module="app.mainai_execution.envelope_scope",
    )


def test_ta13_authorized_file_replaced_with_symlink_fails(isolated_repo):
    checkout_branch(isolated_repo.work, "ta13", isolated_repo.base_sha)
    greeting = isolated_repo.work / "backend" / "app" / "greeting.py"
    greeting.unlink()
    greeting.symlink_to("/etc/passwd")
    add_commit(isolated_repo.work, "symlink greet")
    oracle = evaluate_publication(
        cwd=isolated_repo.work, base_sha=isolated_repo.base_sha, authorized_paths=AUTHORIZED
    )
    prod_ok, prod_detail = _production_publication_accepts(
        isolated_repo.work, isolated_repo.base_sha, AUTHORIZED
    )
    conclude(
        "TA-13 oracle rejects authorized-path symlink",
        PASS if not oracle.ok else FAIL,
        boundary="harness.publication_oracle.symlink",
        detail=f"oracle={oracle.reasons}; objects={oracle.objects}",
    )
    conclude(
        "TA-13 production rejects authorized-path symlink",
        FAIL if prod_ok else PASS,
        boundary="production.assert_changed_files_within_authority",
        detail=f"production_accepted={prod_ok} ({prod_detail})",
        production_module="app.mainai_execution.envelope_scope",
    )


def test_gitlink_submodule_fails(isolated_repo):
    checkout_branch(isolated_repo.work, "gitlink", isolated_repo.base_sha)
    git(
        "update-index",
        "--add",
        "--cacheinfo",
        "160000,deadbeefdeadbeefdeadbeefdeadbeefdeadbeef,backend/app/greeting.py",
        cwd=isolated_repo.work,
    )
    git("commit", "-q", "-m", "gitlink", cwd=isolated_repo.work)
    oracle = evaluate_publication(
        cwd=isolated_repo.work, base_sha=isolated_repo.base_sha, authorized_paths=AUTHORIZED
    )
    prod_ok, prod_detail = _production_publication_accepts(
        isolated_repo.work, isolated_repo.base_sha, AUTHORIZED
    )
    conclude(
        "PUB-2 oracle rejects gitlink/submodule",
        PASS if not oracle.ok else FAIL,
        boundary="harness.publication_oracle.gitlink",
        detail=f"oracle={oracle.reasons}",
    )
    conclude(
        "PUB-2 production rejects gitlink/submodule",
        FAIL if prod_ok else PASS,
        boundary="production.assert_changed_files_within_authority",
        detail=f"production_accepted={prod_ok} ({prod_detail})",
        production_module="app.mainai_execution.envelope_scope",
    )


def test_untracked_and_staged_discrepancy_fails(isolated_repo):
    checkout_branch(isolated_repo.work, "dirty", isolated_repo.base_sha)
    (isolated_repo.work / "backend" / "app" / "greeting.py").write_text(
        'def greet(name):\n    """Return a greeting."""\n    return f\'hello {name}\'\n'
    )
    add_commit(isolated_repo.work, "document greet")
    (isolated_repo.work / "backend" / "app" / "stowaway.py").write_text("print('untracked')\n")
    git("add", "backend/app/stowaway.py", cwd=isolated_repo.work)
    oracle = evaluate_publication(
        cwd=isolated_repo.work, base_sha=isolated_repo.base_sha, authorized_paths=AUTHORIZED
    )
    conclude(
        "PUB-3 untracked/staged discrepancy fails",
        PASS if not oracle.ok else FAIL,
        boundary="harness.publication_oracle.clean_tree",
        detail=f"oracle={oracle.reasons}",
    )
    assert not oracle.ok


def test_deleted_out_of_scope_path_fails(isolated_repo):
    checkout_branch(isolated_repo.work, "delete", isolated_repo.base_sha)
    (isolated_repo.work / "README.md").unlink()
    (isolated_repo.work / "backend" / "app" / "greeting.py").write_text(
        'def greet(name):\n    """Return a greeting."""\n    return f\'hello {name}\'\n'
    )
    add_commit(isolated_repo.work, "delete readme plus greet")
    oracle = evaluate_publication(
        cwd=isolated_repo.work, base_sha=isolated_repo.base_sha, authorized_paths=AUTHORIZED
    )
    prod_ok, prod_detail = _production_publication_accepts(
        isolated_repo.work, isolated_repo.base_sha, AUTHORIZED
    )
    conclude(
        "PUB-4 oracle rejects deleted out-of-scope path",
        PASS if not oracle.ok else FAIL,
        boundary="harness.publication_oracle.deletion",
        detail=f"oracle={oracle.reasons}",
    )
    conclude(
        "PUB-4 production rejects deleted out-of-scope path",
        FAIL if prod_ok else PASS,
        boundary="production.assert_changed_files_within_authority",
        detail=f"production_accepted={prod_ok} ({prod_detail})",
        production_module="app.mainai_execution.envelope_scope",
    )


def test_two_commits_fail_first_cycle_one_commit_rule(isolated_repo):
    checkout_branch(isolated_repo.work, "two", isolated_repo.base_sha)
    (isolated_repo.work / "backend" / "app" / "greeting.py").write_text("def greet(name):\n    return 'a'\n")
    add_commit(isolated_repo.work, "first")
    (isolated_repo.work / "backend" / "app" / "greeting.py").write_text("def greet(name):\n    return 'b'\n")
    add_commit(isolated_repo.work, "second")
    oracle = evaluate_publication(
        cwd=isolated_repo.work, base_sha=isolated_repo.base_sha, authorized_paths=AUTHORIZED
    )
    conclude(
        "PUB-5 first cycle requires exactly one commit on the approved base",
        PASS if (not oracle.ok and oracle.commit_count == 2) else FAIL,
        boundary="harness.publication_oracle.one_commit",
        detail=f"commit_count={oracle.commit_count}; reasons={oracle.reasons}",
    )
    assert not oracle.ok


def test_exact_history_from_base_to_head_is_enumerated(isolated_repo):
    checkout_branch(isolated_repo.work, "hist", isolated_repo.base_sha)
    (isolated_repo.work / "backend" / "app" / "greeting.py").write_text(
        'def greet(name):\n    """Return a greeting."""\n    return f\'hello {name}\'\n'
    )
    sha = add_commit(isolated_repo.work, "document greet")
    oracle = evaluate_publication(
        cwd=isolated_repo.work, base_sha=isolated_repo.base_sha, authorized_paths=AUTHORIZED
    )
    holds = oracle.commits == [sha] and oracle.commit_count == 1
    conclude(
        "PUB-6 exact commit history from base to pushed SHA",
        PASS if holds else FAIL,
        boundary="harness.publication_oracle.rev_list",
        detail=f"base={isolated_repo.base_sha}; head={rev_parse(isolated_repo.work)}; commits={oracle.commits}",
    )
    assert holds
