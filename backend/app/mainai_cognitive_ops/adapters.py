"""Composition seams for `app.mainai_cognitive_ops`. See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md.

NO SECOND CANONICAL TRUTH: real composition happens directly inside each module that needs it
(`repo_backup_intelligence.py` calls real git; `provider_economics_bridge.py` calls real
`app.resource_intelligence`; `research_reopen_trigger.py` calls real
`app.mainai_research.research_ledger`) -- this file exists only to disclose the seams that are
NOT yet real: Dev Director / Continuous Supervision / Personal Recall program state is not
present, queryable, or owned by anything on this branch, matching the same honest disclosure
pattern as `app.mainai_vision.adapters` / `app.mainai_research.adapters`."""

from __future__ import annotations

from typing import Protocol


class DevDirectorProgramStateAdapter(Protocol):
    """Would supply active/idle/blocked agent snapshots for `situational_awareness.py`. Not
    present on this branch as a queryable interface yet."""

    def snapshot_agent_states(self) -> tuple: ...


class ContinuousSupervisionAdapter(Protocol):
    """Would supply examiner-activity/pending-review state. Not present on this branch as a
    queryable interface yet."""

    def snapshot_supervision_state(self) -> tuple: ...


class PersonalRecallAdapter(Protocol):
    """Would supply Personal Recall's own knowledge-item state for cross-referencing with
    `knowledge_ingestion.py` (owned by `app.mainai_research`, not duplicated here). Lives on a
    separate, unmerged branch (Codex's Level-2 integration lane) -- deliberately untouched."""

    def snapshot_recall_state(self) -> tuple: ...
