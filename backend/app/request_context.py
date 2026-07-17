from contextvars import ContextVar

# Isolated per request by Starlette/FastAPI's per-task context — never leaks between
# concurrent requests even though the underlying DB connection is pooled and reused.
current_user_id: ContextVar[str | None] = ContextVar("current_user_id", default=None)
