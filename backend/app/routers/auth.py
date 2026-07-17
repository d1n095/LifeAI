from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.db import get_db
from app.deps import get_current_user
from app.limiter import limiter
from app.models.user import User
from app.schemas import LoginIn, TokenOut, UserOut
from app.security import create_access_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenOut)
@limiter.limit("10/minute")
def login(request: Request, payload: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=payload.email).first()
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        record_audit(db, user_id=None, action="login_failed", detail=payload.email, request=request)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Fel e-post eller lösenord.")

    record_audit(db, user_id=user.id, action="login_success", request=request)
    token = create_access_token(user.id, user.role.value)
    return TokenOut(access_token=token)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user
