"""Integration BOUNDARY contract between the agent task-context handoff
(app.workforce.context_handoff) and the existing dispatch layer (app.agent_coordination.dispatch
over AgentWorkAssignment), plus Copilot's GitHub Operations Center.

This test DEFINES and PINS the contract WITHOUT modifying #338/#339 or Copilot's #376:
a dispatch step attaches `AgentTaskContext.to_transmittable_dict()` to an already-authorized
`AgentWorkAssignment` and applies `enforce_egress_policy` at real transmission time. The packet
must therefore align with the assignment's own immutable identifiers.

`AgentWorkAssignment` (app/models/agent_coordination.py, on the default branch) carries, among
others: `task_id`, `branch`, `base_sha`, `approval_required`. The context packet must expose the
matching fields so a dispatch readiness check can (a) confirm the agent is being pointed at the
exact task/branch/SHA, and (b) treat a SHA change as stale and revalidate — never silently send
a packet bound to a different SHA than the assignment's.

Pure contract test: no DB, no provider, no network, and it imports nothing from #338/#339 (which
remain owned/unmodified) — it asserts the packet shape the boundary depends on.
"""

from app.workforce.context_handoff import SourceRef, build_agent_task_context

# The minimal field contract a dispatch step / Operations Center consumes from the packet.
# Changing this set is an intentional contract change the dispatch owner must be told about.
_REQUIRED_PACKET_KEYS = frozenset(
    {
        "task_id",            # must equal AgentWorkAssignment.task_id
        "branch",             # must equal AgentWorkAssignment.branch
        "base_sha",           # must equal AgentWorkAssignment.base_sha (staleness gate)
        "objective",
        "acceptance_criteria",
        "source_refs",
        "constraints",
        "approval_limits",
        "expires_at",
        "context_version",
    }
)

_SHA = "64c07d5b0c4821bfb175700283021aa6e95cb61c"


def _ctx(**over):
    params = dict(
        task_id="11111111-1111-1111-1111-111111111111",
        objective="Implement the guard",
        acceptance_criteria=["tests pass", "no secrets in logs"],
        repo="d1n095/LifeAI",
        branch="claude/db-bootstrap-password-guard",
        base_sha=_SHA,
        trust_zone="INTERNAL",
        source_refs=[SourceRef.from_content(path="a.py", base_sha=_SHA, content="x=1\n")],
        approval_limits=["Founder approval required before merge"],
        constraints=["do not touch Codex-owned egress code"],
    )
    params.update(over)
    return build_agent_task_context(**params)


def test_packet_exposes_every_field_the_dispatch_boundary_requires():
    packet = _ctx().to_transmittable_dict()
    missing = _REQUIRED_PACKET_KEYS - set(packet)
    assert missing == set(), f"packet is missing dispatch-contract keys: {missing}"


def test_packet_immutable_identifiers_align_with_an_assignment():
    """Simulate the dispatch step: the assignment's task_id/branch/base_sha are the source of
    truth; the packet must carry exactly those so the agent is pointed at the right place."""
    assignment = {
        "task_id": "11111111-1111-1111-1111-111111111111",
        "branch": "claude/db-bootstrap-password-guard",
        "base_sha": _SHA,
    }
    packet = _ctx(
        task_id=assignment["task_id"],
        branch=assignment["branch"],
        base_sha=assignment["base_sha"],
    ).to_transmittable_dict()
    assert packet["task_id"] == assignment["task_id"]
    assert packet["branch"] == assignment["branch"]
    assert packet["base_sha"] == assignment["base_sha"]


def test_dispatch_readiness_would_treat_a_moved_assignment_sha_as_stale():
    """If the assignment's base_sha has advanced past the packet's, the packet is stale and the
    dispatch step must revalidate (rebuild) rather than send outdated context."""
    ctx = _ctx(base_sha=_SHA)
    moved_assignment_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert ctx.is_stale(current_base_sha=moved_assignment_sha) is True
    assert ctx.is_stale(current_base_sha=_SHA) is False


def test_source_refs_are_sha_bound_so_an_agent_can_verify_before_editing():
    """The Operations Center / agent re-fetches each ref at base_sha and verifies the content
    hash before trusting or editing — a reference alone is not assumed sufficient."""
    content = "def main():\n    pass\n"
    ctx = _ctx(source_refs=[SourceRef.from_content(path="m.py", base_sha=_SHA, content=content)])
    ref = ctx.source_refs[0]
    assert ref.base_sha == _SHA
    assert ref.verify(content) is True
    assert ref.verify(content + "# drift\n") is False
