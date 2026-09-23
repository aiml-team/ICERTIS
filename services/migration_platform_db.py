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


# ─────────────────────────────────────────────────────────────────────────────
# External-migration discovery
# ─────────────────────────────────────────────────────────────────────────────
#
# The migration platform can also be driven by other developers/tools/flows.
# When that happens, PostgreSQL is still authoritative but the migration_id
# is unknown to this app (record_submission() was never called).  The
# reconciliation pass in services/migration_reconciliation.py needs to
# discover those file_items and reflect them into Azure SQL.
#
# Design goals (spec §Performance):
#   • ONE query per tick, not one per migration id.
#   • Incremental window driven by an updated_at watermark + a small
#     lookback so we never miss an event that raced the previous tick.
#   • Always include currently-active statuses even if their updated_at
#     is older than the watermark (a long-running "in_progress" row would
#     otherwise stop being observed after its last update).
#   • Return the parent migration's source triple + created_by so the
#     caller can build the canonical match key AND persist created_by /
#     MigrationRequestId without a second lookup.

_DISCOVERY_ACTIVE_STATUSES = ("pending", "queued", "in_progress", "retrying")


def fetch_recent_file_items(
    updated_since,        # datetime with tz, or None to fetch active-only
    limit: int = 5000,
) -> List[Dict[str, Any]]:
    """Return file_items joined to their parent migration_requests row,
    for reconciliation of externally-triggered migrations.

    Parameters
    ----------
    updated_since : datetime | None
        Fetch every file_items row whose ``updated_at >= updated_since``
        OR whose ``status`` is one of the active states.  ``None`` restricts
        the result to active statuses only (used on the very first tick
        before any watermark exists).
    limit : int
        Hard cap on rows returned in a single call.  Protects the app from
        an unbounded scan if the migration platform sees a burst of activity.
        Rows are ordered by updated_at ASC so successive ticks pick up where
        the previous one left off.

    Returns
    -------
    List[Dict[str, Any]]
        One dict per file_items row with the shape::

            {
              "file_item_id":        "<uuid>",
              "migration_request_id":"<uuid>",
              "file_name":           str,
              "source_path":         str,       # library-relative
              "destination_path":    str,
              "status":              str,       # completed | failed | ...
              "retry_count":         int,
              "error_message":       str | None,
              "error_code":          str | None,
              "power_automate_run_id": str | None,
              "created_at":          datetime,
              "updated_at":          datetime,
              "completed_at":        datetime | None,
              "mr_source_site_url":  str,
              "mr_source_library":   str,
              "mr_source_folder_path": str | None,
              "mr_destination_site_url": str,
              "mr_destination_library":  str,
              "mr_destination_folder_path": str | None,
              "mr_created_by":       str,
              "mr_status":           str,
              "mr_completed_at":     datetime | None,
              "audit_destination_url": str | None,  # latest non-null from audit_logs.extra_data
            }

    Fail-soft: any DB / driver / import error returns [] and logs at most
    once (same suppression policy as fetch_files_for_migrations).
    """
    pool = _get_pool()
    if pool is None:
        return []

    active_tuple = _DISCOVERY_ACTIVE_STATUSES
    try:
        with pool.connection() as cn:
            with cn.cursor() as cur:
                if updated_since is None:
                    # First tick — restrict to active statuses only so we
                    # never scan the full history on startup.
                    cur.execute(
                        """
                        SELECT fi.id::text,
                               fi.migration_request_id::text,
                               fi.file_name,
                               fi.source_path,
                               fi.destination_path,
                               fi.status::text,
                               fi.retry_count,
                               fi.error_message,
                               fi.error_code,
                               fi.power_automate_run_id,
                               fi.created_at,
                               fi.updated_at,
                               fi.completed_at,
                               mr.source_site_url,
                               mr.source_library,
                               mr.source_folder_path,
                               mr.destination_site_url,
                               mr.destination_library,
                               mr.destination_folder_path,
                               mr.created_by,
                               mr.status::text AS mr_status,
                               mr.completed_at AS mr_completed_at,
                               (
                                 SELECT al.extra_data->>'destination_url'
                                 FROM   audit_logs al
                                 WHERE  al.file_item_id = fi.id
                                   AND  al.extra_data ? 'destination_url'
                                   AND  al.extra_data->>'destination_url' IS NOT NULL
                                 ORDER  BY al.created_at DESC
                                 LIMIT  1
                               ) AS audit_destination_url
                        FROM   file_items fi
                        JOIN   migration_requests mr
                          ON   mr.id = fi.migration_request_id
                        WHERE  fi.status::text = ANY(%s)
                        ORDER  BY fi.updated_at ASC
                        LIMIT  %s
                        """,
                        (list(active_tuple), int(limit)),
                    )
                else:
                    cur.execute(
                        """
                        SELECT fi.id::text,
                               fi.migration_request_id::text,
                               fi.file_name,
                               fi.source_path,
                               fi.destination_path,
                               fi.status::text,
                               fi.retry_count,
                               fi.error_message,
                               fi.error_code,
                               fi.power_automate_run_id,
                               fi.created_at,
                               fi.updated_at,
                               fi.completed_at,
                               mr.source_site_url,
                               mr.source_library,
                               mr.source_folder_path,
                               mr.destination_site_url,
                               mr.destination_library,
                               mr.destination_folder_path,
                               mr.created_by,
                               mr.status::text AS mr_status,
                               mr.completed_at AS mr_completed_at,
                               (
                                 SELECT al.extra_data->>'destination_url'
                                 FROM   audit_logs al
                                 WHERE  al.file_item_id = fi.id
                                   AND  al.extra_data ? 'destination_url'
                                   AND  al.extra_data->>'destination_url' IS NOT NULL
                                 ORDER  BY al.created_at DESC
                                 LIMIT  1
                               ) AS audit_destination_url
                        FROM   file_items fi
                        JOIN   migration_requests mr
                          ON   mr.id = fi.migration_request_id
                        WHERE  fi.updated_at >= %s
                           OR  fi.status::text = ANY(%s)
                        ORDER  BY fi.updated_at ASC
                        LIMIT  %s
                        """,
                        (updated_since, list(active_tuple), int(limit)),
                    )
                rows = cur.fetchall()
    except Exception as e:
        if _mark_warned("discovery_unreachable_warned"):
            logger.warning(
                "migration_platform_db.fetch_recent_file_items failed: %s.  "
                "Reconciliation pass skipped this tick; further errors log "
                "at DEBUG.", e,
            )
        else:
            logger.debug(
                "migration_platform_db.fetch_recent_file_items failed: %s", e,
            )
        return []

    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "file_item_id":              r[0],
            "migration_request_id":      r[1],
            "file_name":                 r[2],
            "source_path":               r[3],
            "destination_path":          r[4],
            "status":                    r[5],
            "retry_count":               r[6],
            "error_message":             r[7],
            "error_code":                r[8],
            "power_automate_run_id":     r[9],
            "created_at":                r[10],
            "updated_at":                r[11],
            "completed_at":              r[12],
            "mr_source_site_url":        r[13],
            "mr_source_library":         r[14],
            "mr_source_folder_path":     r[15],
            "mr_destination_site_url":   r[16],
            "mr_destination_library":    r[17],
            "mr_destination_folder_path": r[18],
            "mr_created_by":             r[19],
            "mr_status":                 r[20],
            "mr_completed_at":           r[21],
            "audit_destination_url":     r[22],
        })
    return out


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
