"""Lightweight email-only login + session tracking.

Purpose (see task spec §13):
    • identify the current user by company email
    • separate concurrent sessions so future migration actions can
      record `StartedBy` / `SessionId`
    • track logins/logouts in the `user_sessions` table

Non-goals: this is NOT an authentication system.  No registration, no
roles, no MFA.  Password-based login was removed at product request —
any well-formed `@bs.nttdata.com` (configurable) email creates a session.

Public API
──────────
    validate_email_domain(email)  -> (ok, normalized_email_or_error)
    create_session(email, ua, ip) -> session_id (str)
    get_session(session_id)       -> dict | None   (fresh from DB)
    touch_session(session_id)     -> None          (bumps last_activity)
    end_session(session_id)       -> bool          (marks inactive + logout_time)
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from core.config import settings
from core.database import (
    ensure_user_sessions_table,
    get_connection,
    user_sessions_table_name,
)

logger = logging.getLogger(__name__)

# Basic RFC-5322-ish local-part check.  We keep it deliberately simple
# (letters, digits, dot, underscore, hyphen, plus) — the domain half is
# enforced exactly by string comparison against APP_COMPANY_DOMAIN.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Cache the "table exists" check so we only run DDL once per process.
_schema_ready = False


def _ensure_schema_once() -> None:
    global _schema_ready
    if _schema_ready:
        return
    ensure_user_sessions_table()
    _schema_ready = True


# ── Validation ─────────────────────────────────────────────────────────────
def validate_email_domain(email: str) -> Tuple[bool, str]:
    """Validate format + exact company domain (case-insensitive).

    Returns (True, normalized_email) or (False, error_message).

    The comparison is EXACT (no subdomain widening) — `bs.nttdata.com`
    matches only that domain, never `bs.nttdata.com.evil.com` nor
    `nttdata.com`.  This is done by splitting on the LAST '@' and
    string-comparing the domain half in lower-case.
    """
    if not email or not isinstance(email, str):
        return False, "Email is required."
    email = email.strip()
    if not email:
        return False, "Email is required."
    if not _EMAIL_RE.match(email):
        return False, "Please use your @{d} company email address.".format(
            d=settings.APP_COMPANY_DOMAIN
        )
    # rsplit guards against emails containing '@' in quoted local parts
    local, _, domain = email.rpartition("@")
    if not local or not domain:
        return False, "Please use your @{d} company email address.".format(
            d=settings.APP_COMPANY_DOMAIN
        )
    if domain.lower() != settings.APP_COMPANY_DOMAIN.lower():
        return False, "Please use your @{d} company email address.".format(
            d=settings.APP_COMPANY_DOMAIN
        )
    # Normalize: keep local-part as typed (case may matter for display),
    # lower-case the domain half so DB rows are consistent.
    return True, f"{local}@{domain.lower()}"


# ── Session lifecycle ──────────────────────────────────────────────────────
def create_session(email: str,
                   user_agent: Optional[str] = None,
                   remote_ip:  Optional[str] = None) -> str:
    """Insert a new active session row and return its session_id.

    A fresh 256-bit URL-safe token is generated per session, so two
    logins for the same email always get independent session ids.
    """
    _ensure_schema_once()
    session_id = secrets.token_urlsafe(32)          # ~43 chars
    table = user_sessions_table_name()
    sql = (
        f"INSERT INTO dbo.[{table}] "
        "(email, session_id, login_time, last_activity, is_active, user_agent, remote_ip) "
        "VALUES (?, ?, SYSUTCDATETIME(), SYSUTCDATETIME(), 1, ?, ?)"
    )
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(sql, (email, session_id,
                          (user_agent or "")[:512],
                          (remote_ip  or "")[:64]))
        cn.commit()
    return session_id


def get_session(session_id: str) -> Optional[dict]:
    """Return the session row if it is still valid, else None.

    Validity = row exists AND is_active=1 AND last_activity within TTL.
    Expired sessions are auto-deactivated as a side-effect so subsequent
    lookups short-circuit at the SQL level.
    """
    if not session_id:
        return None
    _ensure_schema_once()
    table = user_sessions_table_name()
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT id, email, session_id, login_time, last_activity, "
            f"       logout_time, is_active "
            f"FROM dbo.[{table}] WHERE session_id = ?",
            (session_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        (sid, email, sess, login_time,
         last_activity, logout_time, is_active) = row

        if not is_active:
            return None

        # TTL check — compare in UTC (SYSUTCDATETIME() produces naive UTC).
        ttl_hours = max(1, int(settings.APP_SESSION_TTL_HOURS))
        now_utc  = datetime.now(timezone.utc).replace(tzinfo=None)
        if last_activity and (now_utc - last_activity) > timedelta(hours=ttl_hours):
            cur.execute(
                f"UPDATE dbo.[{table}] SET is_active = 0, "
                f"       logout_time = SYSUTCDATETIME() "
                f"WHERE session_id = ? AND is_active = 1",
                (session_id,),
            )
            cn.commit()
            return None

        return {
            "id":            int(sid),
            "email":         email,
            "session_id":    sess,
            "login_time":    login_time,
            "last_activity": last_activity,
            "logout_time":   logout_time,
            "is_active":     bool(is_active),
        }


def touch_session(session_id: str) -> None:
    """Best-effort update of last_activity.  Silently no-ops if the row
    is gone/inactive — the caller has already been authenticated by
    `get_session` and touching is not security-critical."""
    if not session_id:
        return
    _ensure_schema_once()
    table = user_sessions_table_name()
    try:
        with get_connection() as cn:
            cur = cn.cursor()
            cur.execute(
                f"UPDATE dbo.[{table}] "
                f"SET last_activity = SYSUTCDATETIME() "
                f"WHERE session_id = ? AND is_active = 1",
                (session_id,),
            )
            cn.commit()
    except Exception:
        logger.exception("touch_session failed (non-fatal)")


def end_session(session_id: str) -> bool:
    """Mark the session inactive and stamp logout_time.

    Returns True if a row was updated, False otherwise.  Idempotent —
    logging out an already-ended session is a harmless no-op.
    """
    if not session_id:
        return False
    _ensure_schema_once()
    table = user_sessions_table_name()
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"UPDATE dbo.[{table}] "
            f"SET is_active   = 0, "
            f"    logout_time = SYSUTCDATETIME() "
            f"WHERE session_id = ? AND is_active = 1",
            (session_id,),
        )
        rows = cur.rowcount or 0
        cn.commit()
    return rows > 0
