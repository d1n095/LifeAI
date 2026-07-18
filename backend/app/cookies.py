from fastapi import Response

from app.config import get_settings

settings = get_settings()

ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"

# Refresh token is scoped to /api/auth so it's only ever sent to login/refresh/logout —
# not attached to every regular API call the way the access token cookie is. Narrower
# exposure surface for the longest-lived, most sensitive credential.
REFRESH_COOKIE_PATH = "/api/auth"

# No separate CSRF cookie: frontend and backend are different origins, so a cookie set by
# the backend can never be read by frontend JavaScript via document.cookie regardless of
# its HttpOnly flag — that's not a security feature we're opting out of, it's a basic
# same-origin-policy fact about cookies. The CSRF value is instead delivered once, in the
# login/refresh JSON response body (readable cross-origin because our explicit CORS
# allow-list permits it), and the frontend holds it in memory. See docs/AUTH_THREAT_MODEL.md
# and app/deps.py / app/routers/auth.py for how it's verified server-side.


def _common_kwargs() -> dict:
    return {
        "secure": settings.cookie_secure,
        "samesite": settings.cookie_samesite,
        "domain": settings.cookie_domain,
    }


def set_session_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        max_age=settings.access_token_expire_minutes * 60,
        httponly=True,
        path="/",
        **_common_kwargs(),
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=settings.refresh_token_expire_days * 24 * 60 * 60,
        httponly=True,
        path=REFRESH_COOKIE_PATH,
        **_common_kwargs(),
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/", domain=settings.cookie_domain)
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH, domain=settings.cookie_domain)
