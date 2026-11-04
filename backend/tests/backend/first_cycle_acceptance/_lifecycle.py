"""Deterministic first-cycle rehearsal: Goal → STOP, network-free.

Production boundaries that are missing stay FAIL/BLOCKED. The simulated effect executor
is harness-local and is the only process allowed a one-use test credential.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ._ci import ReviewReceipt, evaluate_exact_set, evaluate_review, exact_set_runs
from ._credentials import write_one_use_askpass
from ._git import IsolatedRepo, checkout_branch, git, rev_parse
from ._grants import Grant, GrantError, GrantStore
from ._publication import evaluate_publication

AUTHORIZED = ["backend/app/greeting.py", "backend/tests/test_greeting_doc.py"]
GREETING_EDIT = 'def greet(name):\n    """Return a greeting."""\n    return f\'hello {name}\'\n'
TEST_EDIT = (
    "import importlib.util, pathlib\n"
    "def test_greet_has_docstring():\n"
    "    p = pathlib.Path(__file__).resolve().parents[1] / 'app' / 'greeting.py'\n"
    "    spec = importlib.util.spec_from_file_location('greeting', p)\n"
    "    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
    "    assert m.greet.__doc__\n"
)


@dataclass
class FakeGitHub:
    prs: list[dict] = field(default_factory=list)
    reviews: list[dict] = field(default_factory=list)
    check_runs: list[dict] = field(default_factory=list)
    writes: list[tuple] = field(default_factory=list)

    def create_draft_pr(self, *, title: str, head: str, base: str, sha: str) -> dict:
        pr = {
            "number": len(self.prs) + 1,
            "draft": True,
            "head": {"ref": head, "sha": sha},
            "base": base,
            "title": title,
            "html_url": f"https://example.invalid/pull/{len(self.prs) + 1}",
        }
        self.prs.append(pr)
        return pr


@dataclass
class SimulatedEffectExecutor:
    store: GrantStore
    repo: IsolatedRepo
    identity: str = "effect-executor"

    def arm_one_use_credential(self, token: str) -> Path:
        return write_one_use_askpass(self.repo.grant_box / "askpass", token)

    def push(self, grant: Grant, *, local_sha: str, branch: str) -> str:
        consumed = self.store.consume(
            grant.id,
            kind="push",
            owner_id=grant.owner_id,
            goal_id=grant.goal_id,
            task_id=grant.task_id,
            job_id=grant.job_id,
            worktree_id=grant.worktree_id,
            repository=grant.repository,
            branch=branch,
            base_sha=grant.base_sha,
            commit_sha=local_sha,
        )
        (self.repo.grant_box / "live_push_grant").write_text(local_sha, encoding="utf-8")
        result = git("push", "origin", f"{branch}:{branch}", cwd=self.repo.work, check=False)
        if result.returncode != 0:
            raise GrantError(f"granted push failed: {(result.stderr or result.stdout)[:800]}")
        remote = git("rev-parse", f"refs/heads/{branch}", cwd=self.repo.bare).stdout.strip()
        if remote != local_sha:
            raise GrantError(f"remote observation mismatch: {remote} != {local_sha}")
        consumed.consume_result = "success"
        return remote

    def open_draft_pr(self, grant: Grant, github: FakeGitHub, *, sha: str, branch: str, base: str) -> dict:
        self.store.consume(
            grant.id,
            kind="draft_pr",
            owner_id=grant.owner_id,
            goal_id=grant.goal_id,
            task_id=grant.task_id,
            job_id=grant.job_id,
            worktree_id=grant.worktree_id,
            repository=grant.repository,
            branch=branch,
            base_sha=grant.base_sha,
            commit_sha=sha,
        )
        return github.create_draft_pr(title="Document greet()", head=branch, base=base, sha=sha)


@dataclass
class CycleResult:
    stages: list[str]
    local_sha: str
    remote_sha: str | None
    pr: dict | None
    ci_passed: bool
    review_ok: bool
    stopped: bool
    publication_ok: bool
    detail: dict


def apply_authorized_edit(repo: IsolatedRepo, *, branch: str) -> None:
    checkout_branch(repo.work, branch, repo.base_sha)
    (repo.work / "backend" / "app" / "greeting.py").write_text(GREETING_EDIT)
    (repo.work / "backend" / "tests" / "test_greeting_doc.py").write_text(TEST_EDIT)


def run_rehearsal(
    repo: IsolatedRepo,
    *,
    identities: dict | None = None,
) -> CycleResult:
    identities = identities or {
        "owner": "founder-1",
        "goal": "goal-1",
        "task": "task-1",
        "job": "job-1",
        "worktree": "wt-1",
        "repository": "test-owner/test-repo",
        "builder": "builder-agent",
        "reviewer": "founder-reviewer",
        "executor": "effect-executor",
    }
    stages = [
        "goal",
        "founder_authorization",
        "planning",
        "repo_edit",
        "verification",
        "local_commit",
    ]
    branch = f"claude/mainai-task-{identities['task']}"
    apply_authorized_edit(repo, branch=branch)
    git("add", "-A", cwd=repo.work)
    # verification: the edited test imports greeting; run a cheap syntax/docstring check
    namespace = {}
    exec((repo.work / "backend" / "app" / "greeting.py").read_text(), namespace)
    assert namespace["greet"].__doc__
    git("commit", "-q", "-m", "Document greet()", cwd=repo.work)
    local_sha = rev_parse(repo.work)
    publication = evaluate_publication(
        cwd=repo.work, base_sha=repo.base_sha, authorized_paths=AUTHORIZED
    )
    store = GrantStore()
    push_grant = store.issue(
        kind="push",
        owner_id=identities["owner"],
        goal_id=identities["goal"],
        task_id=identities["task"],
        job_id=identities["job"],
        worktree_id=identities["worktree"],
        repository=identities["repository"],
        branch=branch,
        base_sha=repo.base_sha,
        commit_sha=local_sha,
    )
    stages.append("exact_sha_push_grant")
    executor = SimulatedEffectExecutor(store=store, repo=repo, identity=identities["executor"])
    remote_sha = executor.push(push_grant, local_sha=local_sha, branch=branch)
    stages.append("simulated_push")
    observed = git("rev-parse", f"refs/heads/{branch}", cwd=repo.bare).stdout.strip()
    assert observed == remote_sha == local_sha
    stages.append("remote_observation")
    pr_grant = store.issue(
        kind="draft_pr",
        owner_id=identities["owner"],
        goal_id=identities["goal"],
        task_id=identities["task"],
        job_id=identities["job"],
        worktree_id=identities["worktree"],
        repository=identities["repository"],
        branch=branch,
        base_sha=repo.base_sha,
        commit_sha=local_sha,
    )
    github = FakeGitHub()
    pr = executor.open_draft_pr(pr_grant, github, sha=local_sha, branch=branch, base=repo.base_branch)
    stages.append("separate_draft_pr_authorization")
    github.check_runs = exact_set_runs(local_sha)
    ci = evaluate_exact_set(github.check_runs, sha=local_sha)
    stages.append("exact_sha_ci")
    receipt = ReviewReceipt(
        pr_number=pr["number"],
        head_sha=pr["head"]["sha"],
        reviewer_identity=identities["reviewer"],
        builder_identity=identities["builder"],
        state="approved",
    )
    review_ok, review_detail = evaluate_review(
        receipt=receipt, pr_head_sha=pr["head"]["sha"], builder_identity=identities["builder"]
    )
    stages.append("review_receipt")
    stopped = True
    stages.append("STOP")
    return CycleResult(
        stages=stages,
        local_sha=local_sha,
        remote_sha=remote_sha,
        pr=pr,
        ci_passed=ci.passed and ci.done,
        review_ok=review_ok,
        stopped=stopped,
        publication_ok=publication.ok,
        detail={
            "publication": publication.reasons,
            "ci": ci.evidence,
            "review": review_detail,
            "push_grant_consumed": store.get(push_grant.id).consumed,
            "pr_grant_consumed": store.get(pr_grant.id).consumed,
            "pr_grant_distinct": push_grant.id != pr_grant.id,
        },
    )
