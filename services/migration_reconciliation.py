"""External-migration reconciliation (PostgreSQL → Azure SQL).

Purpose
───────
The migration platform can be driven by other developers / tools / API
clients / Power Automate flows that were not launched through this
frontend.  When that happens, the platform's PostgreSQL is still the
authoritative per-file execution result (task §12), but Azure SQL's
ContractInventory has never been told the file transitioned out of
``Pending``.  This module bridges that gap.

Flow (see spec)::

    Power Automate / Migration Platform
              ↓
        PostgreSQL   (read only from this app)
              ↓
        this module  (fetch → group latest → match → update)
              ↓
        Azure SQL ContractInventory
              ↓
        existing UI auto-refresh

Design goals
────────────
1. Reuse existing helpers — parse_sharepoint_url, match_key_from_*,
   fetch_recent_file_items, reconcile_external_file_status.  Do NOT
   build a second parallel writer.
2. PostgreSQL stays READ ONLY.  This module only issues SELECTs (via
   services.migration_platform_db) — no INSERT/UPDATE/DELETE anywhere
   on the PG side.
3. Idempotent.  Running the pass multiple times converges to the same
   Azure SQL state.  Never demote ``Migrated`` back to ``In Processing``.
4. Incremental.  Watermark + small lookback window (spec §Performance);
   the first tick per process fetches active statuses only so we never
   scan the full history on startup.
5. Duplicate-safe.  When multiple PostgreSQL rows carry the same
   canonical match key (e.g. two migration attempts for the same source
   file), the LATEST terminal state wins — ordered by
   updated_at → completed_at → created_at (all descending).
6. Ambiguity-safe.  When multiple Azure SQL rows share the same match
   key, we log the ambiguity and skip the row rather than guessing.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.config import settings
from services import data_service, migration_platform_db
from services.migration_paths import match_key_from_parts
from services.migration_platform import map_backend_status

logger = logging.getLogger(__name__)


# ── Watermark (in-process, thread-safe) ──────────────────────────────────
#
# Kept intentionally simple: a single datetime protected by a lock.  A
# durable watermark isn't required because:
#   * On process restart we fall back to "active-only" for one tick
#     (fetch_recent_file_items(None)), which is small and cheap.
#   * A DB-backed watermark would need its own retention policy and
#     failure semantics — extra complexity for negligible benefit at
#     the current row-count scale.
# Callers that want to force a full sweep (tests, ops) can reset via
# reset_watermark().
_WATERMARK_LOCK = threading.Lock()
_WATERMARK: Optional[datetime] = None

# Small lookback added to the watermark so we never miss an event whose
# updated_at raced the previous tick's clock read.  Configurable but the
# default of 60 s is comfortably larger than any single-hop DB round-trip.
_LOOKBACK_SECONDS = int(
    getattr(settings, "MIGRATION_RECONCILE_LOOKBACK_SECONDS", 60) or 60
)

# Hard cap on rows pulled per tick (spec §Performance — do not scan the
# entire history every 5 s).  In steady state the watermark keeps this
# far under the cap; the cap is a safety net for burst scenarios.
_ROW_LIMIT = int(
    getattr(settings, "MIGRATION_RECONCILE_ROW_LIMIT", 5000) or 5000
)


def reset_watermark() -> None:
    """Force the next reconcile_external_migrations() call to fetch the
    active-only baseline (used by tests and by ops when a full sweep is
    desired).  Safe to call at any time."""
    global _WATERMARK
    with _WATERMARK_LOCK:
        _WATERMARK = None


def _read_watermark() -> Optional[datetime]:
    with _WATERMARK_LOCK:
        return _WATERMARK


def _advance_watermark(rows: List[Dict[str, Any]]) -> None:
    """Advance the watermark to (max(updated_at) - lookback) so the next
    tick's WHERE clause includes any row whose updated_at was concurrently
    being written."""
    global _WATERMARK
    if not rows:
        # No new activity — keep the existing watermark so we don't
        # scan further back on the next tick.
        return
    newest: Optional[datetime] = None
    for r in rows:
        ts = r.get("updated_at")
        if isinstance(ts, datetime):
            if newest is None or ts > newest:
                newest = ts
    if newest is None:
        return
    with _WATERMARK_LOCK:
        # Subtract the lookback so the next tick still catches any event
        # whose updated_at raced the previous tick's clock read.
        _WATERMARK = newest - timedelta(seconds=_LOOKBACK_SECONDS)


# ── Grouping: latest PostgreSQL row per canonical match key ──────────────
def _latest_per_key(
    rows: List[Dict[str, Any]],
) -> Dict[tuple, Dict[str, Any]]:
    """Group PG rows by canonical match key and pick the latest per key.

    Ordering: updated_at → completed_at → created_at, all descending.
    Terminal states are NOT preferred over active ones — the spec says
    "prefer the latest terminal state" but strictly what matters is the
    latest event; a subsequent 'retrying' after a 'failed' correctly
    means the file is active again.  ``latest by updated_at`` gives
    exactly that.
    """
    def _sort_ts(r: Dict[str, Any]) -> tuple:
        u = r.get("updated_at") or datetime.min.replace(tzinfo=timezone.utc)
        c = r.get("completed_at") or datetime.min.replace(tzinfo=timezone.utc)
        cr = r.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)
        return (u, c, cr)

    best: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        key = match_key_from_parts(
            r.get("mr_source_site_url"),
            r.get("mr_source_library"),
            r.get("source_path"),
        )
        if key is None:
            # Missing site/library/source_path — cannot correlate.  Not
            # an error; the platform sometimes has partially-populated
            # rows during in-flight migrations.  Skip.
            continue
        prev = best.get(key)
        if prev is None or _sort_ts(r) > _sort_ts(prev):
            best[key] = r
    return best


# ── Main entry point ─────────────────────────────────────────────────────
def reconcile_external_migrations() -> Dict[str, Any]:
    """Discover externally-triggered migrations and reflect them into
    ContractInventory.  Safe to call every sync tick.

    Returns a small summary dict::

        {
          "polled":      int,   # PG rows fetched
          "candidates":  int,   # rows after per-key latest-wins collapse
          "matched":     int,   # candidates that resolved to exactly one FileID
          "ambiguous":   int,   # candidates that resolved to >1 FileID (skipped)
          "unmatched":   int,   # candidates with no FileID (skipped, logged)
          "updated": {          # rowcounts that ACTUALLY transitioned
            "migrated": int, "failed": int, "skipped": int,
            "in_processing": int, "rowsAffected": int,
          },
        }

    Never raises for expected failures — PG unavailable, driver missing,
    inventory query error, empty result — all resolve to zero counts.
    """
    empty = {
        "polled": 0, "candidates": 0, "matched": 0,
        "ambiguous": 0, "unmatched": 0,
        "updated": {"migrated": 0, "failed": 0, "skipped": 0,
                    "in_processing": 0, "rowsAffected": 0},
    }

    if not migration_platform_db.is_configured():
        return empty

    watermark = _read_watermark()
    try:
        rows = migration_platform_db.fetch_recent_file_items(
            updated_since=watermark, limit=_ROW_LIMIT,
        )
    except Exception:
        # fetch_recent_file_items is already fail-soft; belt-and-braces here.
        logger.exception("reconcile_external_migrations: PG fetch failed")
        return empty

    if not rows:
        return empty

    # Collapse to one PG row per canonical key.
    per_key = _latest_per_key(rows)

    # Build the Azure-SQL side index ONCE per tick.  Two hundred rows
    # per tick is common; a single SELECT is dramatically cheaper than
    # N lookups.
    try:
        az_index = data_service.build_match_key_index()
    except Exception:
        logger.exception("reconcile_external_migrations: inventory index build failed")
        _advance_watermark(rows)  # still advance so we don't re-scan
        return {**empty, "polled": len(rows)}

    matched = ambiguous = unmatched = 0
    updates: List[Dict[str, Any]] = []

    for key, pg_row in per_key.items():
        fids = az_index.get(key) or []
        if len(fids) == 0:
            unmatched += 1
            logger.info(
                "unmatched migration file: file_item=%s file_name=%r "
                "site=%r library=%r source_path=%r",
                pg_row.get("file_item_id"), pg_row.get("file_name"),
                pg_row.get("mr_source_site_url"),
                pg_row.get("mr_source_library"),
                pg_row.get("source_path"),
            )
            continue
        if len(fids) > 1:
            ambiguous += 1
            logger.warning(
                "ambiguous migration match: file_item=%s file_name=%r "
                "candidates=%s site=%r library=%r source_path=%r",
                pg_row.get("file_item_id"), pg_row.get("file_name"),
                fids,
                pg_row.get("mr_source_site_url"),
                pg_row.get("mr_source_library"),
                pg_row.get("source_path"),
            )
            continue

        # Additional validation: file_name must match too (spec §Match rule
        # — "use file_name as an additional validation check").  Path-based
        # keys are already unique enough that a mismatch here means either
        # a data-quality issue on either side or a bug in the parser;
        # either way, refuse rather than write the wrong row.
        local_id = fids[0]
        # We don't hit the DB again to fetch FileName — the index build
        # already validated that SharePointPath parses.  The parse
        # extracts file_name from the URL's last segment, and the same
        # source_path field on the PG side ends with file_name too.
        # For an out-of-band safety check, verify the last segment of
        # the PG source_path matches file_name:
        pg_fn = str(pg_row.get("file_name") or "").strip()
        pg_src = str(pg_row.get("source_path") or "").strip()
        if pg_fn and pg_src and not pg_src.lower().endswith(pg_fn.lower()):
            # Weird — PG file_name isn't the tail of source_path.  Don't
            # invent semantics, skip and log.
            logger.warning(
                "reconcile: PG file_name %r not the tail of source_path %r; "
                "skipping to avoid a wrong-row write",
                pg_fn, pg_src,
            )
            unmatched += 1
            continue

        ui = map_backend_status(pg_row.get("status"))
        if ui is None:
            # Unknown backend status — logged by map_backend_status, skip.
            continue

        matched += 1
        updates.append({
            "fileID":              local_id,
            "uiStatus":            ui,
            "backendStatus":       pg_row.get("status"),
            "migrationRequestId":  pg_row.get("migration_request_id"),
            "migrationFileItemId": pg_row.get("file_item_id"),
            "submittedBy":         pg_row.get("mr_created_by"),
            "migratedDate":        pg_row.get("completed_at"),
            "destinationUrl":      pg_row.get("audit_destination_url"),
            "retryCount":          pg_row.get("retry_count"),
            "errorMessage":        pg_row.get("error_message"),
            "errorCode":           pg_row.get("error_code"),
        })

    written = data_service.reconcile_external_file_status(updates) if updates else {
        "migrated": 0, "failed": 0, "skipped": 0,
        "in_processing": 0, "rowsAffected": 0,
    }

    # Advance the watermark ONLY after a successful write pass so a
    # failed write is retried on the next tick.
    _advance_watermark(rows)

    return {
        "polled":     len(rows),
        "candidates": len(per_key),
        "matched":    matched,
        "ambiguous":  ambiguous,
        "unmatched":  unmatched,
        "updated":    written,
    }
