"""CTX-1/CTX-2 re-expressed for the structured-target contract (#273+): a repo_edit must declare
target_files, so the original probes (no declared targets) now fail at plan time by design.
These variants keep the same security property under the new contract:

CTX-1' the model writes an EXISTING file it was never shown and that is not a declared target -> refused.
CTX-2' a declared target that exceeds the context cap is not shown, and a write to it is refused
       (no silent truncation of the unseen tail).
"""

from __future__ import annotations

import json

import pytest

from app.mainai_execution import execution_job, planner
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import MainAITask
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider

from test_path_a_exam import M, _goal, _privileges, owner_id  # noqa: F401


def _declared_task(db, goal, description, targets):
    planner.create_plan(db, goal=goal, rationale="exam", created_by="examiner", tasks=[PlannedTaskSpec(
        description=description, task_type="repo_edit", target_files=list(targets),
        verification_plan=[{"kind": "targeted_tests", "target": "tests/backend/test_x.py"}])])
    db.commit()
    return db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()


@pytest.mark.asyncio
async def test_ctx1_prime(db_session, owner_id, monkeypatch, tmp_path):
    core = tmp_path / "backend" / "app" / "core.py"
    core.parent.mkdir(parents=True)
    core.write_text("IMPORTANT = 'original logic'\n")
    (tmp_path / "backend" / "app" / "other.py").write_text("X = 1\n")
    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    captured = {}

    async def _chat(self, messages, model, **kwargs):
        captured["prompt"] = messages[1].content
        return ChatResult(content=json.dumps({"files": [{"path": "backend/app/core.py", "content": "IMPORTANT = 'rewritten blind'\n"}],
                                              "commit_message": "x"}), provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    task = _declared_task(db_session, _goal(db_session, owner_id), "Improve other.py.", ["backend/app/other.py"])
    try:
        await execution_job._propose_repo_edit(db_session, task)
        refused, err = False, None
    except Exception as exc:  # noqa: BLE001
        refused, err = True, str(exc)[:100]
    M("CTX-1' write to an existing file the model never saw (not a declared target) is refused", refused and "original logic" in core.read_text(),
      f"model saw core.py={'original logic' in captured.get('prompt', '')}; refused={refused} ({err}); file now={core.read_text().strip()!r}")


@pytest.mark.asyncio
async def test_ctx2_prime(db_session, owner_id, monkeypatch, tmp_path):
    big = tmp_path / "backend" / "app" / "big.py"
    big.parent.mkdir(parents=True)
    body = "".join(f"LINE_{i:05d} = {i}\n" for i in range(2000))
    big.write_text(body)
    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    captured = {}

    async def _faithful(self, messages, model, **kwargs):
        prompt = messages[1].content
        marker = "### backend/app/big.py\n```\n"
        shown = prompt.split(marker, 1)[1].split("\n```", 1)[0] if marker in prompt else ""
        captured["shown_chars"] = len(shown)
        return ChatResult(content=json.dumps({"files": [{"path": "backend/app/big.py", "content": "# edited\n" + shown}], "commit_message": "x"}),
                          provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _faithful)
    task = _declared_task(db_session, _goal(db_session, owner_id), "Add a header comment to big.py.", ["backend/app/big.py"])
    try:
        await execution_job._propose_repo_edit(db_session, task)
        refused = False
    except Exception:  # noqa: BLE001
        refused = True
    after = big.read_text()
    lost = sum(1 for i in range(2000) if f"LINE_{i:05d} = {i}\n" not in after)
    M("CTX-2' oversized declared target is not truncated by an edit", refused or lost == 0,
      f"original={len(body)} chars; shown={captured.get('shown_chars')}; lines lost={lost}/2000; refused={refused}")
