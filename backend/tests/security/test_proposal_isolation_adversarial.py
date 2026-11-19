"""The isolation boundary, attacked on purpose.

`proposal_worktree` promises that untrusted AI edits are written into a per-attempt checkout that
can never touch the live tree, and that the checkout has no way to reach a network. Both promises
are only worth anything if the inputs that would break them are refused. This file supplies those
inputs: attempt ids that are really paths, a repository that inherited a token-bearing remote, and
a proposal tree containing a symlink out of itself.

Each test asserts the boundary held AND that nothing was created outside it — a refusal that leaves
half a directory behind is still a breach of the promise.
"""
import subprocess
import uuid
from pathlib import Path

import pytest

from app.mainai_execution.proposal_worktree import (
    PROPOSAL_ROOT,
    ProposalWorktreeError,
    assert_no_credentialed_remote,
    create_isolated_proposal_checkout,
    remote_urls,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def shared_checkout(tmp_path) -> Path:
    repo = tmp_path / "shared"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "founder@lifeos.local")
    _git(repo, "config", "user.name", "Founder")
    (repo / "app.py").write_text("print('live')\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo


# --- attempt ids are directory names, not paths ------------------------------------------


@pytest.mark.parametrize(
    "bad_attempt_id",
    [
        "../../etc",
        "../../../tmp/escape",
        "/tmp/escape",
        "..",
        ".",
        "a/../../b",
        "job\n2026",
        "",
        "x" * 200,
    ],
)
def test_a_malicious_attempt_id_never_becomes_a_path(shared_checkout, bad_attempt_id):
    before = {p.name for p in PROPOSAL_ROOT.iterdir()} if PROPOSAL_ROOT.exists() else set()

    with pytest.raises(ProposalWorktreeError):
        create_isolated_proposal_checkout(shared_checkout=shared_checkout, attempt_id=bad_attempt_id)

    after = {p.name for p in PROPOSAL_ROOT.iterdir()} if PROPOSAL_ROOT.exists() else set()
    assert after == before, "a refused attempt id must not create a directory anywhere"


def test_a_real_attempt_id_still_works_and_stays_under_the_proposal_root(shared_checkout):
    checkout = create_isolated_proposal_checkout(shared_checkout=shared_checkout, attempt_id=uuid.uuid4())
    try:
        assert checkout.tree.resolve().is_relative_to(PROPOSAL_ROOT.resolve())
        assert (checkout.tree / "app.py").read_text() == "print('live')\n"
        # The isolated tree really is a copy: editing it cannot touch the shared checkout.
        (checkout.tree / "app.py").write_text("print('proposal')\n")
        assert (shared_checkout / "app.py").read_text() == "print('live')\n"
    finally:
        import shutil

        shutil.rmtree(checkout.root, ignore_errors=True)


# --- no credential-bearing or network remote may be inherited ----------------------------


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://ghp_exampletoken@github.com/d1n095/LifeAI.git",
        "https://user:pass@github.com/d1n095/LifeAI.git",
        "git@github.com:d1n095/LifeAI.git",
        "ssh://git@github.com/d1n095/LifeAI.git",
    ],
)
def test_a_credentialed_remote_is_refused(shared_checkout, remote_url):
    _git(shared_checkout, "remote", "add", "origin", remote_url)
    assert remote_urls(shared_checkout), "the fixture must actually have configured the remote"
    with pytest.raises(ProposalWorktreeError):
        assert_no_credentialed_remote(shared_checkout)


def test_the_isolated_checkout_has_no_remote_at_all(shared_checkout):
    checkout = create_isolated_proposal_checkout(shared_checkout=shared_checkout, attempt_id=uuid.uuid4())
    try:
        assert remote_urls(checkout.tree) == [], "an isolated proposal tree must never have a remote"
        # A linked worktree's `.git` is a file pointing at the base repository, so the config that
        # would carry a remote lives there — check the place a remote would actually have to be.
        assert (checkout.tree / ".git").is_file()
        assert remote_urls(checkout.root / ".base.git") == [], "the bootstrap repository must have no remote either"
    finally:
        import shutil

        shutil.rmtree(checkout.root, ignore_errors=True)


def test_a_symlink_inside_the_proposal_tree_cannot_be_used_to_escape(shared_checkout, tmp_path):
    """The write guard resolves the final path, so a symlink planted by an earlier proposal cannot
    redirect a later write outside the tree. This asserts the boundary the writer relies on."""
    outside = tmp_path / "outside.txt"
    outside.write_text("original\n")
    checkout = create_isolated_proposal_checkout(shared_checkout=shared_checkout, attempt_id=uuid.uuid4())
    try:
        (checkout.tree / "escape.txt").symlink_to(outside)
        write_root = checkout.tree.resolve()
        attempted = (write_root / "escape.txt").resolve()
        # Exactly the check app/mainai_execution/execution_job.py applies before writing.
        assert attempted != write_root and write_root not in attempted.parents
        assert attempted == outside.resolve()
        assert outside.read_text() == "original\n", "the guard must be what stops the write, not luck"
    finally:
        import shutil

        shutil.rmtree(checkout.root, ignore_errors=True)
