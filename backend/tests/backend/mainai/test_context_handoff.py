"""Synthetic acceptance tests for the token-efficient agent task-context handoff
(app.workforce.context_handoff). Pure — no provider, no DB, no network.

Each test maps to one of the Founder directive's nine acceptance criteria
(2026-11-09, "Token-Efficient Agent Context Broker").
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.workforce.context_handoff import (
    AgentResultRecord,
    BoundedRetrievalRequest,
    ContextHandoffError,
    SourceRef,
    build_agent_task_context,
    estimate_tokens,
    fulfill_bounded_retrieval,
    full_context_baseline_tokens,
)

_SHA_A = "64c07d5b0c4821bfb175700283021aa6e95cb61c"
_SHA_B = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _build(**overrides):
    params = dict(
        task_id="task-1",
        objective="Add a fail-closed guard to ensure_app_role.py",
        acceptance_criteria=["guard rejects placeholder in production", "dev/test unchanged"],
        repo="d1n095/LifeAI",
        branch="claude/db-bootstrap-password-guard",
        base_sha=_SHA_A,
        trust_zone="INTERNAL",
        source_refs=[
            SourceRef.from_content(
                path="backend/scripts/security/ensure_app_role.py",
                base_sha=_SHA_A,
                content="def main():\n    app_password = os.environ['MAINAI_APP_PASSWORD']\n",
            )
        ],
    )
    params.update(overrides)
    return build_agent_task_context(**params)


# --- Criterion 1: enough context to start without searching the whole repo -------------------

def test_c1_agent_can_start_a_scoped_task_without_reading_the_whole_repo():
    ctx = _build()
    packet = ctx.to_transmittable_dict()
    # Objective + acceptance + the exact file it must touch are present...
    assert packet["objective"]
    assert packet["acceptance_criteria"]
    assert packet["base_sha"] == _SHA_A
    assert packet["source_refs"][0]["path"] == "backend/scripts/security/ensure_app_role.py"
    # ...and the one relevant excerpt is included, so no repo-wide search is needed to begin.
    assert "MAINAI_APP_PASSWORD" in packet["source_refs"][0]["excerpt"]


# --- Criterion 2: source references match the exact repository SHA ----------------------------

def test_c2_source_reference_is_integrity_verifiable_against_the_exact_content():
    content = "def main():\n    return 1\n"
    ref = SourceRef.from_content(path="x.py", base_sha=_SHA_A, content=content)
    assert ref.base_sha == _SHA_A
    assert ref.verify(content) is True  # unchanged content verifies
    assert ref.verify(content + "# drift\n") is False  # any drift from the SHA fails verification


# --- Criterion 3: missing context retrieved through authorized bounded requests ---------------

def test_c3_additional_context_comes_only_through_an_authorized_bounded_fetch():
    fetched = {"backend/app/config.py": "SECRET_KEY: str\n"}

    def authorized_fetch(path, sha):
        assert sha == _SHA_A  # retrieval is SHA-bound
        return fetched[path]

    req = BoundedRetrievalRequest(path="backend/app/config.py", base_sha=_SHA_A)
    ref = fulfill_bounded_retrieval(
        req, authorized_fetch=authorized_fetch, allowed_paths=frozenset({"backend/app/config.py"})
    )
    assert ref.verify("SECRET_KEY: str\n") is True

    # A path outside the authorized scope is refused (least privilege).
    with pytest.raises(ContextHandoffError):
        fulfill_bounded_retrieval(
            BoundedRetrievalRequest(path="backend/app/secrets.py", base_sha=_SHA_A),
            authorized_fetch=authorized_fetch,
            allowed_paths=frozenset({"backend/app/config.py"}),
        )


# --- Criterion 4: a continued task receives only valid changes --------------------------------

def test_c4_continuation_delta_sends_only_changed_fields_with_immutable_ids():
    v1 = _build(context_version=1)
    v2 = _build(
        context_version=2,
        prior_attempts=["v1 attempt failed: wrong floor constant"],
        tests_ci=["ci run 123 red on test_x"],
    )
    delta = v1.delta_to(v2)
    # Changed fields are present...
    assert delta["prior_attempts"] == ["v1 attempt failed: wrong floor constant"]
    assert delta["tests_ci"] == ["ci run 123 red on test_x"]
    # ...immutable identifiers for safe validation are always carried...
    assert delta["task_id"] == "task-1"
    assert delta["base_sha"] == _SHA_A
    assert delta["context_version"] == 2
    assert delta["prev_context_version"] == 1
    # ...and an unchanged field (objective) is NOT re-sent.
    assert "objective" not in delta


# --- Criterion 5: stale context triggers revalidation -----------------------------------------

def test_c5_context_is_stale_when_sha_moves_or_ttl_elapses():
    now = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)
    ctx = _build(now=now, ttl_seconds=3600)
    # Fresh, same SHA -> not stale.
    assert ctx.is_stale(now=now + timedelta(minutes=10), current_base_sha=_SHA_A) is False
    # Base SHA moved -> stale (must revalidate even within TTL).
    assert ctx.is_stale(now=now + timedelta(minutes=10), current_base_sha=_SHA_B) is True
    # TTL elapsed -> stale.
    assert ctx.is_stale(now=now + timedelta(hours=2), current_base_sha=_SHA_A) is True


# --- Criterion 6: no unauthorized or secret data enters external API requests -----------------

def test_c6a_credential_kinds_are_denied_by_disclosure_minimization_and_noted():
    ctx = _build(
        trust_zone="EXTERNAL_PROVIDER",
        requested_items=[
            {"kind": "api_key", "ref": "env:OPENAI_API_KEY"},
            {"kind": "excerpt", "ref": "doc:1", "excerpt": "harmless architecture note"},
        ],
    )
    kinds_in_packet = {item["kind"] for item in ctx.minimized_items}
    assert "api_key" not in kinds_in_packet  # credential kind never leaves
    assert "withheld:api_key" in ctx.missing_info  # and the withholding is auditable, not silent


def test_c6b_never_egress_marked_content_refuses_the_whole_build():
    with pytest.raises(ContextHandoffError):
        _build(decisions=["NEVER_EGRESS: founder bank code 1234"])


# --- Criterion 7: agent results return to Life as structured, untrusted-by-default evidence ---

def test_c7_agent_results_are_structured_and_untrusted_until_externally_verified():
    claim = AgentResultRecord.from_agent_claim(
        task_id="task-1",
        attempted="added guard + tests",
        changed_files=("backend/scripts/security/ensure_app_role.py",),
        commit_sha="235ce1d1",
        tests_passed=True,  # the AGENT claims this...
    )
    assert claim.verified is False  # ...but Life does not trust it yet
    # Promotion requires an explicit external evidence reference Life checked itself.
    with pytest.raises(ContextHandoffError):
        claim.mark_verified(evidence_ref="")
    verified = claim.mark_verified(evidence_ref="ci-run-42521566226:green")
    assert verified.verified is True
    assert verified.verification_evidence == "ci-run-42521566226:green"


# --- Criterion 8: token usage is measurable against a full-context baseline --------------------

def test_c8_compact_context_is_measurably_smaller_than_the_full_dump_baseline():
    full_file = "x = 1\n" * 4000  # the whole file an agent would otherwise re-read
    ctx = _build(
        source_refs=[
            SourceRef.from_content(
                path="big.py", base_sha=_SHA_A, content=full_file, max_excerpt_chars=400
            )
        ]
    )
    compact = ctx.estimated_tokens()
    baseline = full_context_baseline_tokens([full_file])
    assert compact > 0 and baseline > 0
    assert compact < baseline  # the bounded excerpt is far cheaper than the full dump
    assert estimate_tokens("") == 0


# --- Criterion 9: an optimization never bypasses security or execution approvals --------------

def test_c9a_acceptance_criteria_may_never_be_trimmed_away():
    with pytest.raises(ContextHandoffError):
        _build(acceptance_criteria=[])


def test_c9b_delta_always_re_sends_safety_fields_even_when_unchanged():
    v1 = _build(
        approval_limits=["Founder approval required before merge"],
        constraints=["do not touch Codex-owned egress code"],
        context_version=1,
    )
    v2 = _build(
        approval_limits=["Founder approval required before merge"],
        constraints=["do not touch Codex-owned egress code"],
        context_version=2,
        prior_attempts=["something changed"],
    )
    delta = v1.delta_to(v2)
    # Even though approval_limits/constraints/acceptance_criteria did NOT change, a token
    # optimization must still carry them — safety is never dropped to save tokens.
    assert delta["approval_limits"] == ["Founder approval required before merge"]
    assert delta["constraints"] == ["do not touch Codex-owned egress code"]
    assert delta["acceptance_criteria"]
