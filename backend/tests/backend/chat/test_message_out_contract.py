"""DB-free contract test for a reloaded conversation's failed assistant row.

The live /api/chat response already exposes ``assistant_status="failed"``. The reload path
uses ``MessageOut`` and previously returned only id/role/content/provider/model/created_at,
so the frontend could not tell a failed empty assistant row from an ordinary empty message
and silently dropped it. These fields are the durable reload contract.
"""

from datetime import datetime
from types import SimpleNamespace

from app.models.conversation import MessageRole, MessageStatus
from app.schemas import MessageOut


def test_failed_assistant_message_keeps_status_and_retry_target():
    row = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        role=MessageRole.assistant,
        content="",
        provider=None,
        model=None,
        created_at=datetime(2026, 11, 20, 12, 0, 0),
        status=MessageStatus.failed,
        error_category="unreachable",
        in_reply_to_id="00000000-0000-0000-0000-000000000002",
    )

    out = MessageOut.model_validate(row)

    assert out.status == "failed"
    assert out.error_category == "unreachable"
    assert str(out.in_reply_to_id) == "00000000-0000-0000-0000-000000000002"


def test_succeeded_message_defaults_are_unchanged():
    row = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000003",
        role=MessageRole.user,
        content="Hej",
        provider=None,
        model=None,
        created_at=datetime(2026, 11, 20, 12, 0, 0),
        status=MessageStatus.succeeded,
        error_category=None,
        in_reply_to_id=None,
    )

    out = MessageOut.model_validate(row)

    assert out.status == "succeeded"
    assert out.error_category is None
    assert out.in_reply_to_id is None
