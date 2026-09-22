"""Authentication endpoints — email-only login + session tracking.

    POST /api/auth/login    → validate email format + company domain,
                              issue an HttpOnly session cookie, insert a
                              row in `user_sessions`.  NO password required.
    POST /api/auth/logout   → deactivate the current session row and
                              clear the cookie.
    GET  /api/auth/me       → return `{ email, sessionId }` for the
                              currently-authenticated caller (used by
                              the frontend to show the user's identity
                              in the header).

Password-based login was removed at product request for this internal
app — the session is now created solely from the user's email address.

Also exports the `require_session` dependency used by protected routes:

    from routes.auth import require_session
    @router.get("/foo")
    def foo(session = Depends(require_session)):
        session["email"]      # current_user_email
        session["session_id"] # current_session_id
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from core.config import settings
from services import auth_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth"])


# ── Request/response shapes ────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: str = Field(..., description="Company email address")


class LoginResponse(BaseModel):
    email:     str
    sessionId: str


# ── Cookie helpers ─────────────────────────────────────────────────────────
def _set_session_cookie(resp: Response, session_id: str) -> None:
    resp.set_cookie(
        key=settings.APP_SESSION_COOKIE,
        value=session_id,
        max_age=settings.APP_SESSION_TTL_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.APP_SESSION_COOKIE_SECURE,
        path="/",
    )


def _clear_session_cookie(resp: Response) -> None:
    resp.delete_cookie(
        key=settings.APP_SESSION_COOKIE,
        path="/",
        samesite="lax",
        secure=settings.APP_SESSION_COOKIE_SECURE,
        httponly=True,
    )


# ── Dependency used by protected API routes ───────────────────────────────
def require_session(request: Request) -> dict:
    """FastAPI dependency: raise 401 unless the request carries a valid
    session cookie.  Returns the session record on success and bumps
    `last_activity` as a side-effect.

    Downstream routes can read the current user via the returned dict:
        session["email"]        →  current_user_email
        session["session_id"]   →  current_session_id
    """
    sid = request.cookies.get(settings.APP_SESSION_COOKIE)
    if not sid:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    session = auth_service.get_session(sid)
    if not session:
        raise HTTPException(status_code=401, detail="Session expired.")
    auth_service.touch_session(sid)
    return session


# ── Endpoints ──────────────────────────────────────────────────────────────
@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, request: Request, response: Response):
    """Validate email format + company domain, then create a new session.

    Password-based login was removed at product request — this route is
    now email-only.  Access control is still non-anonymous because:
      • the email must be well-formed, and
      • it must belong to the configured company domain.

    Response semantics:
      • 200 → session issued; HttpOnly cookie set on the response.
      • 400 → email is missing / malformed / not on the company domain
              (the message names the required domain so users know what
              to fix).
    """
    # 1. Email format + exact company-domain check (backend is authoritative).
    ok, normalized_or_error = auth_service.validate_email_domain(payload.email)
    if not ok:
        raise HTTPException(status_code=400, detail=normalized_or_error)
    email = normalized_or_error

    # 2. Create the session row + set the cookie.
    ua = (request.headers.get("user-agent") or "")[:512]
    ip = request.client.host if request.client else ""
    session_id = auth_service.create_session(email, user_agent=ua, remote_ip=ip)
    _set_session_cookie(response, session_id)

    logger.info("Login OK for %s (session=%s… ip=%s)",
                email, session_id[:8], ip or "?")
    return LoginResponse(email=email, sessionId=session_id)


@router.post("/logout")
def logout(request: Request, response: Response):
    """End the current session and clear the cookie.

    Always returns 200 with `{ ok: True }` — logging out is idempotent
    and never fails from the client's perspective.
    """
    sid = request.cookies.get(settings.APP_SESSION_COOKIE)
    if sid:
        try:
            auth_service.end_session(sid)
        except Exception:
            logger.exception("end_session failed (continuing to clear cookie)")
    _clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
def me(session: dict = Depends(require_session)):
    """Return the current user's identity — used by the frontend to
    render the logged-in email in the header."""
    return {
        "email":     session["email"],
        "sessionId": session["session_id"],
    }
