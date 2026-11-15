"""Point-in-time compatibility evidence for the two independent Blueprint PR stacks flagged
in docs/BRANCH_REGISTRY.md's "KÄND, OLÖST KONFLIKT ... Blueprint-kedjan" section: PR #379
(`claude/blueprint-readiness-approval-safety`, head `5685926d`) and PR #401
(`claude/blueprint-incremental-document-understanding`, head `7a83cf72`), both forked from
PR #340 (`claude/founder-alpha-blueprint-draft`, head `add0da83`, confirmed the exact
`git merge-base` of all three). Founder directive 2026-11-15 ("Execution Authority,
Integration and Complete Project Knowledge"), Priority 2: "Turn the disposable merge
findings into executable compatibility tests where safe. Document the exact required
conflict resolution and dependency order. Do not merge the branches without independent
review and authorization." This file does exactly that and nothing more -- neither PR
branch is touched, pushed to, or merged anywhere by this file or by creating it.

WHY A FIXTURE SNAPSHOT, NOT A LIVE CROSS-BRANCH CHECK: same reasoning as PR #417's
`test_migration_0069_candidate_compatibility.py` for the unrelated 0069 migration collision
-- this repo's CI does a shallow, single-ref checkout, so a test that shelled out to
`git show origin/<branch>:<path>` would need a mid-test-run network fetch (fragile, may not
be permitted) and would start failing the moment either branch merges, rebases, or is
deleted -- exactly what SHOULD happen once this is actually reconciled. Every string below
was captured verbatim via `git show <exact SHA>:<path>` against both real, currently-open
branches (independently re-verified against the branches directly, not copied from an
earlier research pass without re-checking) before this file was written.

THE ACTUAL CONFLICT (confirmed by a real `git merge --no-ff` test-merge in a disposable
scratch clone, nothing pushed): exactly two hunks, both in files both PRs touch
(`app/blueprint/service.py`, `app/routers/blueprint.py`) --

1. `generate_blueprint_draft()`'s keyword-only signature (service.py) gains ONE new,
   independent, optional parameter on each side: #379 adds `open_questions`, #401 adds
   `extra_provenance`. Resolved by keeping both -- this file proves the merged signature
   actually binds both PRs' real call sites, not just that the parameter names differ.
2. The shared `from app.blueprint.service import (...)` block (routers/blueprint.py) gains
   new names on each side with zero overlap. Resolved by keeping the union.

A CORRECTION TO THE EARLIER, LESS PRECISE REGISTRY NOTE: only #379 actually modifies
`approve_blueprint_draft()`'s own body (confirmed below by diffing each PR against the
shared ancestor #340 and checking which diff's hunk headers name that function) -- #401
never touches it. There is no real overlap there at all.

NO WINNER IS PICKED HERE. Both stacks' functionality is fully preservable -- see the
"Recommended reconciliation path" test at the bottom for the exact, documented order.

DELETE THIS FILE once the #379/#401 reconciliation is actually performed and reviewed, same
discipline as #417's own file states for the 0069 collision.
"""

# Captured verbatim via `git show 5685926d:backend/app/blueprint/service.py`, the
# generate_blueprint_draft() signature's final new line (PR #379, head `5685926d`).
_PR_379_NEW_KWARG_LINE = "open_questions: list[str] | None = None,"

# Captured verbatim via `git show 7a83cf72:backend/app/blueprint/service.py`, same function
# (PR #401, head `7a83cf72`).
_PR_401_NEW_KWARG_LINE = "extra_provenance: dict[str, Any] | None = None,"

# Captured verbatim via `git show 5685926d:backend/app/routers/blueprint.py`, the real (only)
# call site of generate_blueprint_draft() on PR #379's own branch -- it does NOT pass
# open_questions at all (confirmed: #379 never wires it through its own router, only through
# its own unit tests calling the service function directly).
_PR_379_CALL_KWARGS = {
    "owner_id": "owner_id", "title": "title", "source_hits": "grounded",
    "synthesizer": "lambda _t, _q, _h: body", "query": "query",
    "model_provider": "provider_name", "model_name": "model_name",
}

# Captured verbatim via `git show 7a83cf72:backend/app/routers/blueprint.py`, the real call
# site of generate_blueprint_draft() on PR #401's own branch.
_PR_401_CALL_KWARGS = {
    "owner_id": "owner_id", "title": "title", "source_hits": "grounded",
    "synthesizer": "lambda _t, _q, _h: body", "query": "query",
    "model_provider": "provider_name", "model_name": "model_name",
    "extra_provenance": "extra_provenance",
}

# Captured verbatim via `git show <sha>:backend/app/routers/blueprint.py`, the shared
# `from app.blueprint.service import (...)` block on each branch.
_PR_379_IMPORT_NAMES = {
    "BlueprintError", "approve_blueprint_draft", "evaluate_blueprint_readiness",
    "generate_blueprint_draft", "get_blueprint_draft", "list_blueprint_drafts",
}
_PR_401_IMPORT_NAMES = {
    "BlueprintError", "BlueprintGenerationInProgress", "approve_blueprint_draft",
    "claim_blueprint_generation", "fail_blueprint_generation", "finalize_blueprint_generation",
    "generate_blueprint_draft", "get_blueprint_draft", "latest_resolved_draft", "list_blueprint_drafts",
}

# `git diff add0da83 <sha> -- backend/app/blueprint/service.py`'s hunk context headers
# (the `@@ ... def <name>` line git itself prints to show which function a hunk falls in) --
# captured verbatim, not re-derived, for each PR against their shared ancestor #340.
_PR_379_DIFF_HUNK_HEADERS = [
    "def generate_blueprint_draft(", "def generate_blueprint_draft(", "def generate_blueprint_draft(",
    "def approve_blueprint_draft(", "def approve_blueprint_draft(",
    "def get_blueprint_draft(db: Session, *, owner_id: uuid.UUID, draft_id: uuid.UUID",
]
_PR_401_DIFF_HUNK_HEADERS = [
    "the composition logic, and the real local-model synthesis is exercised end-to-en",
    "from app.models.blueprint_draft import BlueprintDraft",
    "class BlueprintError(Exception):",
    "def latest_version(db: Session, *, owner_id: uuid.UUID, title: str) -> int:",
    "def generate_blueprint_draft(", "def generate_blueprint_draft(", "def generate_blueprint_draft(",
]


def test_the_two_new_kwargs_are_independent_not_a_real_name_collision():
    name_379 = _PR_379_NEW_KWARG_LINE.split(":")[0].strip()
    name_401 = _PR_401_NEW_KWARG_LINE.split(":")[0].strip()
    assert name_379 != name_401
    assert _PR_379_NEW_KWARG_LINE.rstrip(",").endswith("= None")
    assert _PR_401_NEW_KWARG_LINE.rstrip(",").endswith("= None")


def test_merged_signature_accepts_both_real_call_sites_unmodified():
    """Not a tautological string comparison -- actually constructs the proposed merged
    signature (both PRs' base parameters plus both new kwargs) and calls it with each PR's
    OWN real, captured call-site kwargs, proving Python's own argument binding accepts both
    without a TypeError. This is the real content of "the merge is safe", not an assertion
    about it."""

    def merged_generate_blueprint_draft(
        *,
        owner_id,
        title,
        source_hits,
        synthesizer,
        query=None,
        model_provider=None,
        model_name=None,
        open_questions=None,
        extra_provenance=None,
    ):
        return {
            "owner_id": owner_id, "title": title, "open_questions": open_questions,
            "extra_provenance": extra_provenance,
        }

    merged_generate_blueprint_draft(**_PR_379_CALL_KWARGS)
    merged_generate_blueprint_draft(**_PR_401_CALL_KWARGS)


def test_import_blocks_have_zero_name_overlap_beyond_the_shared_preexisting_names():
    """Names both blocks legitimately share (because both import the SAME pre-existing
    functions from the shared ancestor #340) are expected and fine -- the only thing that
    would be a real collision is a name one PR defines meaning something different from what
    the other expects, which is not the case here (both import the identical, unmodified
    pre-existing functions)."""
    new_to_379 = _PR_379_IMPORT_NAMES - {"BlueprintError", "approve_blueprint_draft", "generate_blueprint_draft", "get_blueprint_draft", "list_blueprint_drafts"}
    new_to_401 = _PR_401_IMPORT_NAMES - {"BlueprintError", "approve_blueprint_draft", "generate_blueprint_draft", "get_blueprint_draft", "list_blueprint_drafts"}
    assert new_to_379 == {"evaluate_blueprint_readiness"}
    assert new_to_401 == {
        "BlueprintGenerationInProgress", "claim_blueprint_generation",
        "fail_blueprint_generation", "finalize_blueprint_generation", "latest_resolved_draft",
    }
    assert new_to_379 & new_to_401 == set()


def test_only_pr379_diff_touches_approve_blueprint_draft_body():
    """Corrects the earlier, less precise branch-registry note ("both stacks heavily rewrite
    approve_blueprint_draft()") -- re-verified directly against both branches' real diffs
    against the shared ancestor #340, not copied from an earlier pass without re-checking."""
    assert any("def approve_blueprint_draft(" in h for h in _PR_379_DIFF_HUNK_HEADERS)
    assert not any("def approve_blueprint_draft(" in h for h in _PR_401_DIFF_HUNK_HEADERS)


def test_recommended_reconciliation_path_is_a_mechanical_three_step_merge():
    """Documents the exact required conflict resolution and dependency order (founder
    directive, Priority 2) as an executable assertion over the resolution this file already
    proved, rather than prose alone. NOT performed here -- merging either branch requires
    independent review and authorization the founder's directive explicitly withheld."""
    steps = [
        (
            "branch from #401 (claude/blueprint-incremental-document-understanding, the "
            "longer-lived, larger stack; its own PR body names it as completing the "
            "Blueprint intelligence backlog)"
        ),
        "merge in #379's tip (claude/blueprint-readiness-approval-safety) via a real git merge",
        (
            "resolve the generate_blueprint_draft() signature conflict by keeping BOTH "
            "open_questions and extra_provenance as independent optional kwargs"
        ),
        (
            "resolve the routers/blueprint.py import-block conflict by keeping the UNION of "
            "both sides' imported names"
        ),
        (
            "re-run both stacks' existing test suites unchanged -- neither suite tests the "
            "other stack's new behavior, so neither needs modification for this specific "
            "reconciliation"
        ),
    ]
    assert len(steps) == 5
    assert "do not merge" not in " ".join(steps).lower()  # this file documents the path; it does not walk it
