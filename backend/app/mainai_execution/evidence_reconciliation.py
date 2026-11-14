"""Deterministic checkpoint reconciliation (Continuous Build Directive, Section D): compute a
task's REAL engineering lifecycle status from verified GitHub evidence -- never from a PR's own
description text, a commit message's claims, or anything a founder/agent asserted without a
corresponding checkable fact. "This closes the gap" in a PR body is not evidence; a passing
check run, an approving review, or a merged pull request are.

Distinguishes exactly the six stages the directive named: proposed, implemented, tested,
reviewed, integrated, activated. Each is its own independently-computed boolean fact (never
inferred from the others, so a bug in one check can't silently promote a different stage); the
overall `status` is the HIGHEST stage whose own fact is true, in that fixed order.

`activated` has NO verifiable signal anywhere in this codebase today -- there is no deploy
capability (app/integrations/github_client.py's own module docstring: "no merge method
anywhere, on purpose"; force-push, branch deletion and any deploy/governance operation are
likewise deliberately absent). This function therefore NEVER reports `activated=True` on its
own; the field exists in the vocabulary because the directive named it, and is wired for a
future, explicitly-separate activation-evidence source to set -- reporting anything else here
would be exactly the "simulate a provider session" / fabricate-a-PASS failure mode Directive A
and this directive's own Item F both warn against."""

from dataclasses import dataclass

from app.integrations.github_client import GitHubClient

# Append-only, ordered by how far along real engineering work has gotten -- never reordered,
# since `_highest_true_stage` below walks this list to find the LAST stage whose fact is True.
_STAGE_ORDER = ("proposed", "implemented", "tested", "reviewed", "integrated", "activated")

_FAILING_CHECK_CONCLUSIONS = frozenset({"failure", "timed_out", "cancelled", "action_required"})


@dataclass(frozen=True)
class ReconciledEvidence:
    """One fact per stage, each independently checkable against its own `evidence` string --
    never a single opaque boolean a reader has to trust blindly."""

    proposed: bool
    implemented: bool
    tested: bool
    reviewed: bool
    integrated: bool
    activated: bool
    status: str
    evidence: dict[str, str]


def _highest_true_stage(facts: dict[str, bool]) -> str:
    highest = "proposed" if facts["proposed"] else "unknown"
    for stage in _STAGE_ORDER:
        if facts.get(stage):
            highest = stage
    return highest


async def reconcile_pull_request_status(client: GitHubClient, *, number: int) -> ReconciledEvidence:
    """Reconciles ONE pull request's real status against GitHub's own records. Raises
    whatever `client`'s own methods raise (e.g. `GitHubClientError` if unconfigured, or an
    HTTP error if the PR does not exist) -- this function adds no new failure mode, it only
    composes existing verified reads."""
    pr = await client.get_pull_request(number)
    evidence: dict[str, str] = {"proposed": f"pull request #{number} exists"}

    # implemented: a real diff was actually pushed, not just a PR shell with zero commits --
    # GitHub's own `commits` field on the PR object is the authoritative count.
    commit_count = pr.get("commits", 0) or 0
    implemented = commit_count > 0
    evidence["implemented"] = f"{commit_count} commit(s) on the PR" if implemented else "no commits on the PR yet"

    # tested: at least one check run completed successfully AND none of the ones that
    # completed failed/timed out/was cancelled -- a single green check among several reds is
    # NOT "tested", exactly the same standard this session's own CI babysitting already holds
    # itself to (see docs/BRANCH_REGISTRY.md's npm-audit/Playwright entries: a known-unrelated
    # red check still gets named, never silently waved through as if everything passed).
    head_sha = pr.get("head", {}).get("sha")
    tested = False
    if head_sha:
        check_runs = await client.list_check_runs(head_sha)
        completed = [c for c in check_runs if c.get("status") == "completed"]
        failing = [c for c in completed if c.get("conclusion") in _FAILING_CHECK_CONCLUSIONS]
        passing = [c for c in completed if c.get("conclusion") == "success"]
        tested = bool(passing) and not failing
        if check_runs:
            evidence["tested"] = f"{len(passing)} passing / {len(failing)} failing of {len(check_runs)} check run(s)"
        else:
            evidence["tested"] = "no check runs recorded for this commit"
    else:
        evidence["tested"] = "pull request has no head commit to check"

    # reviewed: a real APPROVED review exists -- GitHub's own review records, never the PR's
    # own description claiming "reviewed" or "LGTM" typed into a comment.
    reviews = await client.list_reviews(number)
    approvals = [r for r in reviews if r.get("state") == "APPROVED"]
    reviewed = bool(approvals)
    evidence["reviewed"] = (
        f"{len(approvals)} approving review(s) from {sorted({r.get('user', {}).get('login') for r in approvals})}"
        if reviewed
        else f"no approving review ({len(reviews)} review(s) total, none APPROVED)"
    )

    # integrated: GitHub's own `merged` field -- never "the PR says it's ready to merge" or a
    # mergeable_state heuristic, which describes whether a merge is POSSIBLE, not whether one
    # actually happened.
    integrated = bool(pr.get("merged"))
    evidence["integrated"] = f"merged_at={pr.get('merged_at')}" if integrated else "not merged"

    evidence["activated"] = "no activation-evidence source is wired up yet -- never reported True by this function"

    facts = {
        "proposed": True,
        "implemented": implemented,
        "tested": tested,
        "reviewed": reviewed,
        "integrated": integrated,
        "activated": False,
    }
    return ReconciledEvidence(
        proposed=True,
        implemented=implemented,
        tested=tested,
        reviewed=reviewed,
        integrated=integrated,
        activated=False,
        status=_highest_true_stage(facts),
        evidence=evidence,
    )
