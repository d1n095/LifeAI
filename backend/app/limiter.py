from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def rate_limit_key(request: Request) -> str:
    """Key by authenticated user when available (set in app/deps.py), else by IP.

    In-memory limiter — correct for a single backend instance. Move to a Redis
    storage backend before running multiple backend replicas (see docs/ROADMAP.md).
    """
    user_id = getattr(request.state, "user_id", None)
    return str(user_id) if user_id else get_remote_address(request)


limiter = Limiter(key_func=rate_limit_key)
