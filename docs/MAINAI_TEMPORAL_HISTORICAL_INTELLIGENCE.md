# MAINAI Temporal Historical Intelligence (Stage D)

**Branch:** `cursor/mainai-temporal-historical-intelligence`  
**Depends on:** Stage C (`cursor/mainai-memory-work-linkage` / #211)

## Purpose

Evidence-backed recap for founder questions like:

- “vad gjorde vi idag?”
- “vad har hänt senaste veckan?”
- “vad gjorde vi förra månaden?”
- “vad har ändrats i år?”
- “vad har vi försökt flera gånger?”

Recap is assembled **only** from durable tables (memory, goals/tasks/events, candidates,
entities, lessons, recovery, threads, project sources/PR/checkpoints).  
**No fake recap from model context.**

## Windows

`hour` · `day` · `week` · `month` · `quarter` · `year` · `custom` · `entire_project`

## API

```python
from app.temporal_intelligence import build_recap, answer_founder_recap_question, RecapWindow

report = build_recap(db, owner_id=..., window=RecapWindow.WEEK)
report = answer_founder_recap_question(db, owner_id=..., question="vad har hänt senaste veckan?")
```

`report.repeated_titles` supports “försökt flera gånger” via normalized title counts.

## Hard rules

- Read-only
- Evidence-only (`evidence_only=True`)
- Owner-scoped for RLS tables; founder-wide project memory/lessons included optionally
