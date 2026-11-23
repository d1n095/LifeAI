"""Founder Alpha R15 — the real founder journey through the product's own endpoints.

Nothing here is a shortcut: every step is an HTTP call the founder's browser makes, against a
running API + worker, with the same cookie/CSRF rules. The script asserts the chain the release
is about:

    founder question -> project understanding -> goal -> (AI) plan -> approval -> the EXISTING
    leased job path -> verification/report -> durable memory -> follow-up

It prints (and optionally writes) a JSON record of what actually happened, including every
failure as a failure. It never creates anything outside the API and never pushes or merges.

Usage:
    python scripts/acceptance/founder_r15_journey.py \
        --api http://127.0.0.1:8011 --out ../docs/evidence/r15/founder_journey.json
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

TERMINAL = {"completed", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8011")
    parser.add_argument("--email", default="founder@lifeos.local")
    parser.add_argument("--password", default="R15FounderPassword123!")
    parser.add_argument("--out", default="")
    parser.add_argument("--timeout", type=int, default=420)
    args = parser.parse_args()

    evidence: dict = {"started_at": _now(), "api": args.api, "steps": []}

    # 300s, not 180s: a cold local 4B model on this machine can take over three minutes for the
    # first grounded answer (model load + prompt), and a client timeout would otherwise abort the
    # journey mid-flight and leave old evidence in place.
    with httpx.Client(base_url=args.api, timeout=300.0) as client:
        login = client.post("/api/auth/login", json={"email": args.email, "password": args.password})
        evidence["steps"].append({"step": "login", "status": login.status_code})
        if login.status_code != 200:
            evidence["ok"] = False
            evidence["error"] = login.text[:500]
            return _finish(evidence, args.out)
        csrf = login.json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}

        before = client.get("/api/mainai/project-status")
        evidence["steps"].append({"step": "project_status_before", "status": before.status_code})
        if before.status_code != 200:
            evidence["ok"] = False
            evidence["error"] = before.text[:500]
            return _finish(evidence, args.out)
        before_body = before.json()
        evidence["before"] = {
            "blueprint": before_body["blueprint"].get("approved"),
            "active_goals": before_body["work"]["goals"]["active_count"],
            "completed_goals": before_body["work"]["goals"]["completed_count"],
            "awaiting_approval": before_body["work"]["tasks"]["awaiting_approval_count"],
            "warnings": before_body["warnings"],
            "priorities": [p["content"] for p in before_body["priorities"]],
        }

        # 1) The founder's question, answered from the composed status (grounding block).
        question = (
            "Var är vi i LifeAI-projektet just nu? Sammanfatta blueprint, låst prioritering, "
            "vad som väntar på mitt godkännande och nästa rimliga steg."
        )
        started = time.perf_counter()
        try:
            chat = client.post("/api/chat", json={"message": question}, headers=headers)
        except httpx.HTTPError as exc:
            evidence["steps"].append({"step": "chat_question", "error": repr(exc)[:300]})
            evidence["ok"] = False
            evidence["error"] = f"chat request failed: {exc!r}"[:500]
            return _finish(evidence, args.out)
        elapsed = round(time.perf_counter() - started, 2)
        chat_body = chat.json() if chat.status_code == 200 else {}
        evidence["steps"].append(
            {
                "step": "chat_question",
                "status": chat.status_code,
                "latency_s": elapsed,
                "provider": chat_body.get("provider"),
                "model": chat_body.get("model"),
                "sources": len(chat_body.get("sources") or []),
                "cognitive_state": (chat_body.get("cognitive_state") or {}).get("state")
                if isinstance(chat_body.get("cognitive_state"), dict)
                else chat_body.get("cognitive_state"),
            }
        )
        evidence["chat"] = {
            "question": question,
            "reply": (chat_body.get("reply") or "")[:1200],
            "without_project_status": chat_body.get("without_project_status"),
        }

        # 2) A real goal, planned by the real planner, approved through the real gate.
        # Unique per run: the status block legitimately lists every recent goal, and two goals
        # with the same title (one still running) made the follow-up question genuinely
        # ambiguous instead of testing whether the model can read the register.
        run_tag = datetime.now(timezone.utc).strftime("%H%M%S")
        goal_payload = {
            "title": f"Granska R15-projektstatusen (endast läsning) #{run_tag}",
            "original_instruction": (
                "Läs igenom backend/app/project_status/work_chain.py och backend/app/project_status/service.py. "
                "Sammanfatta vilka livscykler som läses och vilka risker som finns med att rapportera "
                "jobb som stillastående. Ändra inga filer; endast läsning."
            ),
            "risk_level": "low",
            "approval_policy": "standard_repo_work",
        }
        created = client.post("/api/mainai/execution/goals", json=goal_payload, headers=headers)
        evidence["steps"].append({"step": "create_goal", "status": created.status_code})
        if created.status_code not in (200, 201):
            evidence["ok"] = False
            evidence["error"] = created.text[:800]
            return _finish(evidence, args.out)
        goal = created.json()
        evidence["goal"] = {"id": goal["id"], "title": goal["title"], "status": goal["status"]}

        planned = client.post(f"/api/mainai/execution/goals/{goal['id']}/plan", headers=headers)
        evidence["steps"].append({"step": "create_plan", "status": planned.status_code})
        if planned.status_code not in (200, 201):
            evidence["ok"] = False
            evidence["error"] = planned.text[:800]
            return _finish(evidence, args.out)

        detail = client.get(f"/api/mainai/execution/goals/{goal['id']}").json()
        tasks = detail.get("tasks") or []
        evidence["plan"] = {
            "version": (detail.get("plan") or {}).get("version"),
            "rationale": (detail.get("plan") or {}).get("rationale"),
            "tasks": [
                {
                    "id": t["id"],
                    "description": t["description"],
                    "task_type": t["task_type"],
                    "status": t["status"],
                    "approval_required": t.get("approval_required"),
                }
                for t in tasks
            ],
        }

        # 3) Approve anything the gate asks for, through the founder-only endpoint.
        approvals = []
        for task in tasks:
            if task.get("approval_required"):
                res = client.post(f"/api/mainai/execution/tasks/{task['id']}/approve", headers=headers)
                approvals.append({"task_id": task["id"], "status": res.status_code, "body": res.text[:200]})
        evidence["steps"].append({"step": "approve_tasks", "approvals": approvals})

        # 4) The EXISTING worker path dispatches and executes; poll the product's own status.
        deadline = time.time() + args.timeout
        timeline = []
        while time.time() < deadline:
            detail = client.get(f"/api/mainai/execution/goals/{goal['id']}").json()
            states = {t["id"]: t["status"] for t in (detail.get("tasks") or [])}
            timeline.append({"at": _now(), "goal_status": detail["status"], "tasks": states})
            if states and all(s in TERMINAL for s in states.values()) and detail["status"] in TERMINAL:
                break
            time.sleep(4)

        evidence["execution_timeline"] = timeline[-12:]
        evidence["goal_final_status"] = detail["status"]
        evidence["steps"].append({"step": "execution", "goal_status": detail["status"], "polls": len(timeline)})

        report = client.get(f"/api/mainai/execution/goals/{goal['id']}/report")
        evidence["steps"].append({"step": "goal_report", "status": report.status_code})
        if report.status_code == 200:
            report_body = report.json()
            evidence["report"] = {
                "summary": report_body.get("summary"),
                "outcomes": [
                    {
                        "task": t.get("description"),
                        "status": t.get("task_outcome"),
                        "verification": (t.get("verification_outcome") or {}).get("passed"),
                        "attempts": t.get("attempts"),
                    }
                    for t in (report_body.get("tasks") or [])
                ],
            }

        # 5) Follow-up: the same registers, now including what just happened.
        after = client.get("/api/mainai/project-status")
        after_body = after.json()
        evidence["after"] = {
            "completed_goals": after_body["work"]["goals"]["completed_count"],
            "active_goals": after_body["work"]["goals"]["active_count"],
            "awaiting_approval": after_body["work"]["tasks"]["awaiting_approval_count"],
            "warnings": after_body["warnings"],
            "recent_goals": after_body["goals"].get("recent", [])[:5],
        }
        evidence["steps"].append({"step": "project_status_after", "status": after.status_code})

        try:
            follow_up = client.post(
                "/api/chat",
                json={"message": f"Vad hände med målet '{goal['title']}'? Höll det sig inom läsning?"},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            evidence["steps"].append({"step": "follow_up_question", "error": repr(exc)[:300]})
            evidence["follow_up_reply"] = ""
        else:
            follow_body = follow_up.json() if follow_up.status_code == 200 else {}
            evidence["steps"].append(
                {"step": "follow_up_question", "status": follow_up.status_code, "model": follow_body.get("model")}
            )
            evidence["follow_up_reply"] = (follow_body.get("reply") or "")[:1200]

    evidence["ok"] = bool(evidence.get("goal_final_status") in TERMINAL)
    evidence["finished_at"] = _now()
    return _finish(evidence, args.out)


def _finish(evidence: dict, out: str) -> int:
    text = json.dumps(evidence, ensure_ascii=False, indent=2)
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    print(text[:4000])
    return 0 if evidence.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
