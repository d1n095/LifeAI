"""MainAI Execution Loop V0.1 — GOAL -> PLAN -> DURABLE TASKS -> EXECUTOR -> HEARTBEAT/
CHECKPOINT -> VERIFY -> APPROVAL GATE -> FINAL REPORT. See
docs/MAINAI_EXECUTION_LOOP_V0_1.md for the full design and its REAL/STUBBED/LIMITED status,
and app/models/mainai_execution.py for the durable schema this package operates on.

Sits ABOVE the existing `mainai_jobs` durable runtime (app/rag/mainai_jobs_service.py,
app/jobs/mainai_job_lease.py) rather than duplicating its lease/heartbeat/retry machinery —
a task's actual execution unit is always a real `mainai_jobs` row (job_type=`task_execution`),
never a parallel queue."""
