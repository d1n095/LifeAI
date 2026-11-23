"""R15 restart evidence: after API + worker are stopped and started again, does the founder still
see the SAME durable state? Reads only -- it never creates a goal, so a difference between two
runs of this script is a real persistence/accounting bug, not new work.

Usage:
    python scripts/acceptance/restart_check.py --api http://127.0.0.1:8011 \
        --out ../docs/evidence/r15/restart_check.json --label after_restart
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8011")
    parser.add_argument("--email", default="founder@lifeos.local")
    parser.add_argument("--password", default="R15FounderPassword123!")
    parser.add_argument("--out", default="")
    parser.add_argument("--label", default="check")
    args = parser.parse_args()

    record: dict = {"label": args.label, "at": datetime.now(timezone.utc).isoformat()}
    with httpx.Client(base_url=args.api, timeout=60.0) as client:
        login = client.post("/api/auth/login", json={"email": args.email, "password": args.password})
        record["login_status"] = login.status_code
        if login.status_code == 200:
            status = client.get("/api/mainai/project-status")
            record["project_status"] = status.status_code
            body = status.json()
            record["work"] = body["work"]["goals"]
            record["tasks"] = body["work"]["tasks"]["counts"]
            record["recent_goals"] = body["goals"]["recent"][:5]
            record["priorities"] = [p["content"] for p in body["priorities"]]
            record["warnings"] = body["warnings"]

            completed = next((g for g in body["goals"]["recent"] if g["status"] == "completed"), None)
            if completed:
                report = client.get(f"/api/mainai/execution/goals/{completed['id']}/report")
                record["completed_goal_report_status"] = report.status_code
                if report.status_code == 200:
                    record["completed_goal_summary"] = report.json()["summary"]

    text = json.dumps(record, ensure_ascii=False, indent=2)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    print(text[:1500])
    return 0 if record.get("login_status") == 200 and record.get("project_status") == 200 else 2


if __name__ == "__main__":
    raise SystemExit(main())
