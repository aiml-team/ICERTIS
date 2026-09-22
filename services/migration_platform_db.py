"""Read-only accessor for the migration platform's PostgreSQL database.

Role in the sync flow
─────────────────────
Per the architecture rule (task §12):

    Azure SQL                  = UI/business state
    Migration Platform HTTP    = create/control migrations, add files
    Migration Platform Postgres= AUTHORITATIVE per-file execution result
    Power Automate             = physical SharePoint copy

This module is the sole reader of the platform's PostgreSQL.  It is
consumed by /api/migrations/sync to determine whether a file has
*actually* migrated (only ``file_items.status = 'completed'`` counts
as Migrated — task §4).

Guarantees
──────────
1. READ-ONLY.  Only SELECTs are issued.  Session default is set to
   read-only so even a stray write would raise 25006.  Never INSERT /
   UPDATE / DELETE.
2. Optional.  When MIGRATION_DB_URL is empty or psycopg is unavailable,
   every fetch returns an empty list — callers treat that identically to
   "no updates this tick" and fall back to the HTTP API.
3. Fail-soft.  Any DB error is caught, logged once, and hands back [].
4. Pooled.  psycopg_pool.ConnectionPool amortises TLS handshake cost
   across sync ticks and multi-migration fetches.
5. Batched.  fetch_files_for_migrations([id1, id2, ...]) uses ONE
   connection + ONE query to fetch every file across all active
   migrations — task §14 (no per-file connection).
6. Same shape as the HTTP API.  Returned dicts carry EXACTLY the keys
   routes/contracts.py::sync_migrations already consumes.

Schema reference — DATABASE_REFERENCE.md §4 (file_items):

    file_items(
        id                   UUID PK,
        migration_request_id UUID FK,
        file_name            VARCHAR,
        source_path          VARCHAR,
        destination_path     VARCHAR,
        status               file_status_enum
            (pending|queued|in_progress|completed|failed|skipped|retrying),
        retry_count          INT,
        error_message        TEXT,
        error_code           VARCHAR,
        power_automate_run_id  VARCHAR,
        power_automate_flow_id VARCHAR,
        ...
    )
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Iterable, List, Optional

from core.config import settings

logger = logging.getLogger(__name__)

# psycopg + psycopg_pool are optional runtime dependencies — a broken/absent
# install must never crash the app on startup.  When either import fails,
# is_configured() flips to False and every public function returns [].
try:
    import psycopg  # type: ignore
    from psycopg_pool import ConnectionPool  # type: ignore
    _PSYCOPG_AVAILABLE = True
    _PSYCOPG_IMPORT_ERR: Optional[Exception] = None
except Exception as _e:  # pragma: no cover — driver missing / broken wheel
    psycopg = None  # type: ignore
    ConnectionPool = None  # type: ignore
    _PSYCOPG_AVAILABLE = False
    _PSYCOPG_IMPORT_ERR = _e


# ── Once-per-process warning cache ────────────────────────────────────────
# We log "not configured" / "unreachable" once and thereafter demote to
# DEBUG so a broken DB does not spam INFO logs every poll tick.
_health_lock = threading.Lock()
_health_state = {
    "configured_warned":  False,
    "unreachable_warned": False,
}


def _mark_warned(key: str) -> bool:
    with _health_lock:
        if _health_state.get(key):
            return False
        _health_state[key] = True
        return True


def reset_health_cache() -> None:
    """Test/ops hook — clear the once-per-process warning suppressions."""
    with _health_lock:
        for k in _health_state:
            _health_state[k] = False


# ── Connection pool (lazy, thread-safe) ───────────────────────────────────
# One shared pool per process.  Opened on first use, closed on shutdown by
# the FastAPI lifespan hook (see main.py).  Keeping the pool small is
# deliberate: the sync path runs at most once every 8s per browser tab
# and needs only a single connection per tick.
_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 4          # headroom for background submission + sync
_POOL_TIMEOUT  = 10.0       # seconds to wait for a connection

_pool_lock = threading.Lock()
_pool: Optional["ConnectionPool"] = None


def _configure_read_only(cn) -> None:
    """Belt-and-braces enforcement of read-only at the session level.
    Complements the SELECT-only query surface below."""
    try:
        with cn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = on")
    except Exception:
        # Non-fatal — the SELECT-only surface still protects us.
        pass


def _get_pool() -> Optional["ConnectionPool"]:
    """Return the shared pool, opening it on first call.  None when the
    DB integration is disabled or psycopg failed to import."""
    if not is_configured():
        return None
    global _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        dsn = settings.MIGRATION_DB_URL
        connect_timeout = int(settings.MIGRATION_DB_CONNECT_TIMEOUT or 10)
        try:
            _pool = ConnectionPool(  # type: ignore[misc]
                conninfo=dsn,
                min_size=_POOL_MIN_SIZE,
                max_size=_POOL_MAX_SIZE,
                timeout=_POOL_TIMEOUT,
                # Every checked-out connection is (a) confirmed alive via
                # SELECT 1 — cheaper than eating a stale-socket error — and
                # (b) forced into read-only mode.
                check=ConnectionPool.check_connection,  # type: ignore[attr-defined]
                configure=_configure_read_only,
                kwargs={"connect_timeout": connect_timeout,
                        "autocommit": True},
                open=True,
                name="migration_platform_db",
            )
            logger.info(
                "migration_platform_db: connection pool opened "
                "(min=%d max=%d timeout=%.1fs)",
                _POOL_MIN_SIZE, _POOL_MAX_SIZE, _POOL_TIMEOUT,
            )
            return _pool
        except Exception as e:
            if _mark_warned("unreachable_warned"):
                logger.warning(
                    "migration_platform_db: pool open failed: %s. "
                    "Direct-DB reads disabled for this process; "
                    "sync will fall back to HTTP.",
                    e,
                )
            _pool = None
            return None


def close_pool() -> None:
    """Close the shared pool (call from a FastAPI shutdown hook / tests)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close()
            except Exception:
                logger.exception("Error closing migration_platform_db pool")
            _pool = None


def is_configured() -> bool:
    """True iff a DSN was provided AND psycopg imported successfully."""
    if not (settings.MIGRATION_DB_URL or "").strip():
        return False
    if not _PSYCOPG_AVAILABLE:
        if _mark_warned("configured_warned"):
            logger.warning(
                "MIGRATION_DB_URL is set but psycopg/psycopg_pool is not "
                "importable: %s. Direct-DB reads disabled for this process.",
                _PSYCOPG_IMPORT_ERR,
            )
        return False
    return True


# ── Row shaping ───────────────────────────────────────────────────────────
def _row_to_item(row) -> Dict[str, Any]:
    """Convert a file_items row tuple → dict shaped like the HTTP payload.
    Kept as a helper so batch and single fetches produce identical output."""
    (fid, mid, name, src, dst, status,
     retry_count, err_msg, err_code, run_id) = row
    return {
        # Fields consumed by routes/contracts.py::sync_migrations —
        # identical keys to the HTTP API so downstream mapping is unified.
        "id":              fid,
        "file_name":       name,
        "source_path":     src,
        "destination_path": dst,
        "status":          status,
        "retry_count":     retry_count,
        "error_message":   err_msg,
        "error_code":      err_code,
        # Extra context — safe to include; unknown keys are ignored.
        "power_automate_run_id": run_id,
        # destination_url is not stored on file_items in the platform;
        # keep the key present with None so shape stays constant.
        "destination_url": None,
        # NEW — used by the batch path to group results by migration_id
        # without a second lookup.  Not sent by the HTTP API today, but
        # sync code that reads it is safe against absence (getdefault).
        "migration_request_id": mid,
    }


# ── Public API ────────────────────────────────────────────────────────────
def fetch_files_for_migrations(
    migration_request_ids: Iterable[str],
) -> Dict[str, List[Dict[str, Any]]]:
    """Return per-migration file rows for MANY migration IDs in one shot.

    Uses ONE connection + ONE ``WHERE ... IN (%s, %s, ...)`` query.  This
    is the primary entry point for /api/migrations/sync — one call per
    tick instead of N calls (task §14).

    Returns::
        { migration_request_id_str: [ item_dict, ... ], ... }

    Missing / disabled / unreachable → empty dict.  Callers must treat
    an empty result as "no updates this tick" and continue.
    """
    ids = [str(i).strip() for i in (migration_request_ids or []) if str(i).strip()]
    if not ids:
        return {}
    pool = _get_pool()
    if pool is None:
        return {}
    try:
        with pool.connection() as cn:
            with cn.cursor() as cur:
                # ANY(%s::uuid[]) parameter-binds the whole list in one
                # server-side array literal.  Cheaper and safer than
                # building a dynamic ", ".join("?"...) placeholder list.
                cur.execute(
                    """
                    SELECT id::text,
                           migration_request_id::text,
                           file_name,
                           source_path,
                           destination_path,
                           status::text        AS status,
                           retry_count,
                           error_message,
                           error_code,
                           power_automate_run_id
                    FROM   file_items
                    WHERE  migration_request_id = ANY(%s::uuid[])
                    """,
                    (ids,),
                )
                rows = cur.fetchall()
    except Exception as e:
        if _mark_warned("unreachable_warned"):
            logger.warning(
                "migration_platform_db.fetch_files_for_migrations failed "
                "(count=%d): %s.  Sync will fall back to HTTP; further "
                "errors log at DEBUG.",
                len(ids), e,
            )
        else:
            logger.debug(
                "migration_platform_db.fetch_files_for_migrations failed "
                "(count=%d): %s", len(ids), e,
            )
        return {}

    out: Dict[str, List[Dict[str, Any]]] = {mid: [] for mid in ids}
    for r in rows:
        item = _row_to_item(r)
        mid = item.get("migration_request_id") or ""
        if mid in out:
            out[mid].append(item)
        else:
            # Belt-and-braces — should never fire because of the WHERE clause.
            out.setdefault(mid, []).append(item)
    return out


def fetch_migration_files(migration_request_id: str) -> List[Dict[str, Any]]:
    """Return per-file rows for a SINGLE migration.

    Thin wrapper over fetch_files_for_migrations() for callers that only
    hold one id — same shape as the HTTP API.  Kept for backward compat.
    """
    mid = (migration_request_id or "").strip()
    if not mid:
        return []
    grouped = fetch_files_for_migrations([mid])
    return grouped.get(mid, [])


def ping() -> Dict[str, Any]:
    """Diagnostic used by tests / a future /admin endpoint.

    Returns:
        { "configured": bool, "ok": bool, "detail": str }

    Never raises.  Safe to call from any thread.
    """
    if not is_configured():
        return {
            "configured": False,
            "ok": False,
            "detail": "MIGRATION_DB_URL empty or psycopg unavailable",
        }
    pool = _get_pool()
    if pool is None:
        return {
            "configured": True,
            "ok": False,
            "detail": "connection pool unavailable",
        }
    try:
        with pool.connection() as cn:
            with cn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return {"configured": True, "ok": True, "detail": "pong"}
    except Exception as e:
        return {"configured": True, "ok": False, "detail": str(e)}
