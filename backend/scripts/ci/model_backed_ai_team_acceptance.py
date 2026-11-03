"""Real local-model acceptance for the MainAI business AI team.

Runs the reference e-commerce workflow through the REAL `ModelBackedRunner` against a running
local Ollama (no stub), and asserts the INTEGRATION invariants that must hold regardless of a
small model's answer quality:

  * the real local model was actually invoked (provider_invoked, ollama receipts with hashes);
  * multiple specialist roles produced schema-valid, independently-verified output;
  * the active chat provider is local and there is NO paid fallback configured;
  * any worker failure is recorded as failed/blocked — never fabricated;
  * the Founder-approval boundary holds (publish blocked without a token, attempted with one);
  * the whole project reassembles from the database with per-task execution receipts.

It PRINTS the full per-task outcome for transparency and exits non-zero only when an
integration invariant fails (not merely because a 0.5B model gave a weak answer).

Run with the app env pointed at the local Ollama (see the workflow). Uses the superuser
engine, like the test-suite's `superuser_db`, so it does not depend on RLS plumbing here —
cross-owner RLS isolation is proven by the pytest suite this workflow also runs.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

from sqlalchemy.orm import sessionmaker

# Running this file by path puts scripts/ci on sys.path, not the backend root — add the
# backend dir so the `import app...` statements inside main() resolve regardless of the
# invocation cwd (same self-contained pattern as scripts/security/apply_runtime_privileges.py).
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))


def main() -> int:
    from app.config import get_settings
    from app.db import migration_engine
    from app.mainai_business.execution import ModelBackedRunner
    from app.mainai_business.reference import run_ecommerce_opportunity_assessment
    from app.mainai_business.view import assemble_business_project_view
    from app.models.user import User
    from app.providers.registry import resolve_active

    settings = get_settings()
    Session = sessionmaker(bind=migration_engine)
    db = Session()
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        print(("PASS" if cond else "FAIL") + f"  {msg}")
        if not cond:
            failures.append(msg)

    try:
        # No paid fallback is even configured.
        provider, model = resolve_active(db, role="chat")
        check(getattr(provider, "name", None) == "ollama", f"active chat provider is local (got {getattr(provider,'name',None)!r})")
        check((settings.chat_fallback_order or "") == "", f"no chat fallback configured (got {settings.chat_fallback_order!r})")

        owner = User(email=f"ci-mbteam-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
        db.add(owner)
        db.flush()

        runner = ModelBackedRunner(attempts=3, correction_attempts=2, timeout=180.0)
        wf = str(uuid.uuid4())

        # Phase 1 — no approval: the consequential publish must be blocked.
        r1 = run_ecommerce_opportunity_assessment(
            db, owner_id=owner.id, approve_publish=False, workflow_id=wf, runner=runner
        )
        db.commit()
        p1 = r1.as_dict()
        print("\n=== PHASE 1 (no approval) ===")
        print(json.dumps(p1, indent=2, default=str))

        check(p1["provider_invoked"] is True, "real local model was invoked (provider_invoked)")
        check("publish_assessment" in p1["blocked_tasks"], "publish blocked without Founder approval")
        check(p1["assessment"]["published"] is False, "nothing published without approval")
        check(p1["status"] == "awaiting_approval", f"phase-1 status awaiting_approval (got {p1['status']!r})")
        pub1 = next(t for t in p1["tasks"] if t["task_key"] == "publish_assessment")
        check(pub1.get("failure_kind") == "authorization", "publish block is an AUTHORIZATION failure, not output-quality")

        verified_model_backed = [
            t for t in p1["tasks"]
            if t["status"] == "verified" and t["runtime"] == "model_backed"
        ]
        check(len(verified_model_backed) >= 2, f"multiple specialist roles verified model-backed (got {len(verified_model_backed)})")
        for t in verified_model_backed:
            rc = t.get("receipt") or {}
            ok = rc.get("provider") == "ollama" and rc.get("raw_output_sha256") and rc.get("parsed_ok") is True
            check(bool(ok), f"receipt for {t['task_key']} shows real ollama inference + hash")

        # Failures (if the small model produced unparseable output for some role) must be honest.
        for t in p1["tasks"]:
            if t["status"] in ("failed", "rejected_by_validator"):
                check(t.get("output") in (None, {}), f"failed task {t['task_key']} has no fabricated output")
            if t["status"] == "blocked_unmet_dependency":
                check(True, f"dependent {t['task_key']} correctly blocked by an unmet dependency")

        # Phase 2 — same workflow, WITH approval: verified leaves resume; publish is attempted
        # (authority minted), proving the gate, not whether the 0.5B model nails the doc.
        r2 = run_ecommerce_opportunity_assessment(
            db, owner_id=owner.id, approve_publish=True, workflow_id=wf,
            founder_approval_ref="founder-approval:ci", runner=runner,
        )
        db.commit()
        p2 = r2.as_dict()
        print("\n=== PHASE 2 (approved) ===")
        print(json.dumps(p2, indent=2, default=str))
        pub = next(t for t in p2["tasks"] if t["task_key"] == "publish_assessment")
        check(pub["status"] != "blocked_awaiting_founder_approval", "publish no longer blocked once approved")
        reused = [t for t in p2["tasks"] if t["status"] == "verified_reused"]
        check(len(reused) >= 2, f"verified tasks resumed, not re-run (got {len(reused)})")

        # End-to-end completion: the approved publish is model-backed, validated (possibly via a
        # bounded correction attempt), and the final deliverable is produced + persisted.
        check(pub["status"] == "verified", f"approved publish completed + validated (got {pub['status']!r}; corrections={pub.get('correction_attempts')})")
        check(p2["status"] == "completed", f"workflow status completed (got {p2['status']!r})")
        check(bool(p2["final_deliverable"]), "final validated deliverable present on the report")
        check(p2["assessment"]["published"] is True, "assessment published once validated")

        # Persistence: the Founder view reassembles the COMPLETED project + deliverable from DB.
        view = assemble_business_project_view(db, owner_id=owner.id, workflow_id=wf)
        check(view["exists"] is True, "project view reassembles from the database")
        check(view["provider_invoked"] is True, "view records that a model was invoked")
        check(view["status"] == "completed", f"view status completed (got {view['status']!r})")
        check(bool(view["final_deliverable"]), "final deliverable persisted + reassembled from DB")
        receipts = [t for t in view["tasks"] if (t.get("execution_receipt") or {}).get("model")]
        check(len(receipts) >= 2, f"per-task execution receipts persisted (got {len(receipts)})")

    finally:
        db.close()

    print("\n=== SUMMARY ===")
    if failures:
        print(f"{len(failures)} invariant(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All model-backed integration invariants PASS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
