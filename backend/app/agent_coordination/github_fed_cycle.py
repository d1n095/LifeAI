"""Phase 3 — feed the authorized operating cycle from real read-only GitHub intake.

This is the smallest coherent missing integration between two already-verified layers that
both live on this branch: the read-only GitHub development intake + durable ledger backing
(#300/#303) and the authorized operating cycle (#314). It adds NO new framework and NO new
STOP/spend/verification/messaging system — it only composes existing PUBLIC primitives:

    load_owner_ledger                         (restart-safe reload)
      -> GitHubDevelopmentIntake.refresh      (read-only discovery; BLOCKED on rate/forbidden)
      -> persist_owner_ledger                 (durable, DEF-1/GH-4-safe cold start)
      -> load_owner_ledger                    (reload the persisted, deduped ledger)
      -> run_authorized_operating_cycle       (authority-gated, SPEND-STOP, fail-closed)
      -> founder_coordination_snapshot        (one Founder-facing view)

Effect: Life selects its next task from REAL observed GitHub PR / CI / review state instead of
the Founder hand-copying that state between agents. Read-only: no GitHub writes, no delivery
claims. A provider outage or rate-limit pauses ONLY GitHub intake (reported BLOCKED with a named
missing capability) while the rest of the authorized cycle continues. `first_cycle_go` stays
false; this is engineering evidence, not an autonomy GO. Independent review required — the
author never certifies this as independently approved.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.agent_coordination.autonomous_cycle import run_authorized_operating_cycle
from app.agent_coordination.durable import (
    FIRST_CYCLE_GO,
    founder_coordination_snapshot,
    load_owner_ledger,
)
from app.agent_coordination.github_intake import GitHubDevelopmentIntake, GitHubReadClient
from app.agent_coordination.persist import persist_owner_ledger


def run_github_fed_operating_cycle(
    db,
    *,
    owner_id: uuid.UUID | str,
    goal_text: str,
    client: GitHubReadClient | None = None,
    pr_numbers: list[int] | None = None,
    required_capability: str = "review",
    absence_enabled: bool = False,
    standing_grants: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One supervised tick whose task selection is fed by read-only GitHub intake.

    `client` is an injected GET-only GitHub reader (public-unauthenticated by default). When it
    is None, no new discovery is performed and the cycle runs against the already-persisted
    ledger (still restart-safe). Discovery failures never raise: they surface as
    `github.outcome == "blocked"` with a named missing capability, and the authorized cycle
    still runs so unrelated approved work is not blocked by a GitHub outage.
    """
    owner_uuid = owner_id if isinstance(owner_id, uuid.UUID) else uuid.UUID(str(owner_id))

    # 1) Restart-safe reload of the durable ledger.
    service = load_owner_ledger(db, owner_id=owner_uuid)
    intake = GitHubDevelopmentIntake(service, owner_id=str(owner_uuid))

    # 2) Read-only discovery (optional). Blocked reads are reported, never raised.
    github_result: dict[str, Any] = {"outcome": "skipped", "reason": "no client supplied"}
    if client is not None:
        github_result = intake.refresh(client, numbers=pr_numbers)

    # 3) Durably persist whatever the ledger now holds (cold-start safe via persist.py).
    persist_owner_ledger(db, owner_id=owner_uuid, service=service, actor="github_fed_cycle")
    db.flush()

    # 4) Reload the persisted, deduped ledger (equivalent to a fresh process) and run the cycle.
    cycle_service = load_owner_ledger(db, owner_id=owner_uuid)
    cycle = run_authorized_operating_cycle(
        db,
        cycle_service,
        owner_id=str(owner_uuid),
        goal_text=goal_text,
        required_capability=required_capability,
        absence_enabled=absence_enabled,
        standing_grants=standing_grants,
    )

    # 5) One Founder-facing view reassembled from the database.
    founder_view = founder_coordination_snapshot(db, owner_id=owner_uuid)

    github_blocked = github_result.get("outcome") == "blocked"
    intake_view = intake.view()
    return {
        "outcome": "github_fed_cycle",
        "github": {
            "outcome": github_result.get("outcome"),
            "access": github_result.get("access") or intake.state.get("access"),
            "missing_capability": github_result.get("missing_capability"),
            "blocked": github_blocked,
            "view": intake_view,
        },
        "cycle": cycle,
        "founder_view": founder_view,
        # Phase-3 Founder-facing summary: what to do next from real GitHub state.
        "next_eligible": cycle.get("next_eligible"),
        "needed_review": intake_view.get("needed_review", []),
        "write_attempted": False,
        "delivery_claimed": False,
        "first_cycle_go": FIRST_CYCLE_GO,
    }
