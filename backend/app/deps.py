import uuid

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.user import User, UserRole
from app.request_context import current_user_id as current_user_id_var
from app.security import decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Inloggning krävs.")
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Ogiltig eller utgången token.") from exc

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Kontot finns inte eller är inaktiverat.")

    # Bind this request to the user for Postgres Row-Level Security. The contextvar covers
    # any later transaction in this request (see the after_begin listener in app/db.py);
    # the explicit SET LOCAL below covers the transaction that's already open right now,
    # since after_begin already fired for it before we knew who the user was.
    current_user_id_var.set(str(user.id))
    db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})

    # Lets the rate limiter key on the authenticated user instead of just IP (app/limiter.py).
    request.state.user_id = user.id

    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Kräver adminbehörighet.")
    return user
