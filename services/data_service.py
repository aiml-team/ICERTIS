"""Contract data service — Azure SQL backed.

Public API:
    load_contracts()                       -> List[dict]   (active records)
    load_excluded()                        -> List[dict]   (excluded records)
    count_all()                            -> dict         ({active, excluded, total})
    mark_migrated(ids, migrated_at)        -> dict         (rows updated)
    mark_excluded(ids)                     -> dict         (rows MOVED to excluded table)
    restore_excluded(ids)                  -> dict         (rows MOVED back to active)

Exclusion is a **recoverable soft-delete**: the row is MOVED (INSERT + DELETE
inside a single transaction) into `ContractInventory_Excluded`.  Restore is
the reverse.  Original FileID is preserved end-to-end.  No SharePoint file
is ever touched.

The JSON keys returned by these functions are the **same camelCase names**
the existing Manual Review UI already reads.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable, List

from core.config import settings
from core.database import (
    FOLDER_LEVEL_MAX,
    ensure_excluded_column,
    ensure_excluded_table,
    ensure_exclusion_audit_table,
    ensure_folder_columns,
    ensure_migration_status_column,
    exclusion_audit_table_name,
    excluded_table_name,
    get_connection,
)

# Canonical MigrationStatus values.  Kept as constants so callers can never
# introduce drift via typos.  All comparisons/writes go through these.
STATUS_PENDING       = "Pending"
STATUS_IN_PROCESSING = "In Processing"
STATUS_MIGRATED      = "Migrated"
STATUS_FAILED        = "Failed"

logger = logging.getLogger(__name__)

# Lazily bring the schema up to date on the first DB access this process makes.
# Cached at module scope so we only run the metadata checks once per process.
_schema_ready = False


def _ensure_schema_once() -> None:
    """Idempotent, cached: add Excluded column + create excluded table +
    move any legacy Excluded='Yes' rows into the excluded table so state is
    normalised (excluded rows live ONLY in the excluded table).

    Also (folder-hierarchy feature) adds Folder1..Folder20 to both tables,
    creates the exclusion_audit sidecar, and backfills folder columns from
    SharePointPath the first time it runs.
    """
    global _schema_ready
    if _schema_ready:
        return
    try:
        ensure_excluded_column()
        ensure_excluded_table()
        ensure_migration_status_column()
        # Folder-hierarchy additions.  DDL must run BEFORE the legacy
        # excluded-row migration because that migration copies the full
        # _COLUMNS list (which now includes Folder1..Folder20) between
        # tables — both sides need the columns to exist.
        ensure_folder_columns()
        ensure_exclusion_audit_table()
        _migrate_legacy_excluded_rows()
        backfill_folder_columns()
        _schema_ready = True
    except Exception as exc:
        # Never let a schema-check failure crash reads — log and retry next call.
        logger.warning("_ensure_schema_once() failed: %s", exc)


def _migrate_legacy_excluded_rows() -> None:
    """One-time cleanup: any rows in ContractInventory that were flagged
    Excluded='Yes' by the earlier in-place implementation are MOVED into
    the excluded table (transactional) so the new architecture has a single
    source of truth per row.

    Safe to run repeatedly — no-op after the first successful migration."""
    active = settings.CONTRACT_TABLE
    ex = excluded_table_name()
    with get_connection() as cn:
        cn.autocommit = False
        cur = cn.cursor()
        try:
            # Count how many are eligible
            cur.execute(
                f"SELECT COUNT(*) FROM dbo.[{active}] WHERE ISNULL([Excluded], 'No') = 'Yes'"
            )
            n = cur.fetchone()[0] or 0
            if n == 0:
                cn.rollback()
                return

            # INSERT ... SELECT copies the row shape (all columns except the
            # active-only Excluded flag).  ExcludedDate defaults to SYSUTCDATETIME().
            copy_cols = ", ".join(f"[{c}]" for c in _COLUMNS)  # same order both sides
            cur.execute(
                f"INSERT INTO dbo.[{ex}] ({copy_cols}) "
                f"SELECT {copy_cols} FROM dbo.[{active}] "
                f"WHERE ISNULL([Excluded], 'No') = 'Yes' "
                f"  AND [FileID] NOT IN (SELECT [FileID] FROM dbo.[{ex}])"
            )
            copied = cur.rowcount

            # Delete originals (including any that were already in the excluded
            # table — dedup to that table's copy).
            cur.execute(
                f"DELETE FROM dbo.[{active}] WHERE ISNULL([Excluded], 'No') = 'Yes'"
            )
            deleted = cur.rowcount
            cn.commit()
            logger.info(
                "_migrate_legacy_excluded_rows: copied=%d deleted=%d",
                copied, deleted,
            )
        except Exception:
            cn.rollback()
            raise
        finally:
            cn.autocommit = True


# Column order used in SELECT / INSERT below. Kept in one place so seed + read
# stay in sync.
_BASE_COLUMNS = [
    "FileID", "FileName", "SharePointPath", "LastModified", "ModifiedBy",
    "ItemType", "OpportunityID", "AE", "LegalEntity", "CustomerName",
    "AgreementName", "AgreementFileName", "OrderNumber", "AutoRenewalStatus",
    "EffectiveDate", "StartDate", "EndDate", "ExpiryDate", "ContractType",
    "TypeOfContract", "AssociatedMSAFileName", "AssociatedNDAFileName",
    "VoidExclusionIndicator", "ExtractionStatus", "ReviewRequired",
    "MissingFields", "ProcessedDate", "ErrorMessage", "RunId",
    "Migrate", "Migrated", "MigratedDate", "MigrationStatus",
]

# Folder1..Folder20 columns — appended AFTER the base columns so index-based
# access in _row_to_dict stays stable and the excluded-table SELECT can
# continue to tack ExcludedDate/ExcludedBy on the very end.
FOLDER_COLUMNS = [f"Folder{i:02d}" for i in range(1, FOLDER_LEVEL_MAX + 1)]

_COLUMNS = _BASE_COLUMNS + FOLDER_COLUMNS


def _s(v) -> str | None:
    """Trim + treat empty as None."""
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _parse_bool_str(v) -> bool | None:
    s = _s(v)
    if s is None:
        return None
    low = s.lower()
    if low in ("true", "1", "yes"):
        return True
    if low in ("false", "0", "no"):
        return False
    return None


def _fmt_dt(dt) -> str | None:
    """Render DATETIME2 for the UI in the same format parseDate() expects:
    M/D/YYYY H:MM:SS AM/PM.
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    h = dt.hour
    ampm = "PM" if h >= 12 else "AM"
    h12 = h % 12 or 12
    return f"{dt.month}/{dt.day}/{dt.year} {h12}:{dt.minute:02d}:{dt.second:02d} {ampm}"


# ── Folder-hierarchy parser (server twin of client _folderSegmentsFor) ────
# Kept intentionally in sync with static/js/app.js:_folderSegmentsFor so the
# persisted Folder1..Folder20 values match what the client would derive from
# sharePointPath.  Any change here must be mirrored there.
import re as _re
from urllib.parse import unquote as _unquote, urlsplit as _urlsplit

_FOLDER_ROOT_LABEL      = "All Contracts"
_TECHNICAL_SEG_RE       = _re.compile(r"^(shared\s*documents|documents|forms|allitems\.aspx?)$", _re.I)
_FILE_EXT_RE            = _re.compile(r"\.[A-Za-z0-9]{1,6}$")
_HTTP_RE                = _re.compile(r"^https?://", _re.I)


def folder_segments_for(sharepoint_path: str | None) -> list[str]:
    """Return the list of business-meaningful folder segments for a path.

    Rules (must match the client parser):
      • strip query/fragment
      • if URL, drop scheme+host and any leading `/sites/<site>/` pair
      • otherwise treat as POSIX/Windows path
      • decode percent-encoded segments
      • drop technical segments (Shared Documents, Forms, AllItems.aspx…)
      • drop the trailing filename (last segment whose extension is 1-6 alnum)
      • collapse literal "All Contracts" segments (virtual root)
    """
    if not sharepoint_path or not isinstance(sharepoint_path, str):
        return []
    s = sharepoint_path.split("#", 1)[0].split("?", 1)[0].strip()
    if not s:
        return []

    if _HTTP_RE.match(s):
        try:
            parts = [p for p in _urlsplit(s).path.split("/") if p]
        except Exception:
            return []
        if parts and parts[0].lower() == "sites" and len(parts) >= 2:
            parts = parts[2:]
        segs = parts
    else:
        segs = [p for p in s.replace("\\", "/").split("/") if p]

    decoded: list[str] = []
    for seg in segs:
        try:
            seg = _unquote(seg)
        except Exception:
            pass
        if seg and not _TECHNICAL_SEG_RE.match(seg):
            decoded.append(seg)

    if not decoded:
        return []
    if _FILE_EXT_RE.search(decoded[-1]):
        decoded.pop()

    return [seg for seg in decoded if seg.lower() != _FOLDER_ROOT_LABEL.lower()]


def folder_columns_from_path(sharepoint_path: str | None) -> list[str | None]:
    """Return exactly FOLDER_LEVEL_MAX values, one per Folder<N> column.
    Unused levels are None (persisted as NULL) — never a placeholder."""
    segs = folder_segments_for(sharepoint_path)
    out: list[str | None] = [None] * FOLDER_LEVEL_MAX
    for i, seg in enumerate(segs[:FOLDER_LEVEL_MAX]):
        out[i] = seg[:256] if seg else None       # NVARCHAR(256) cap
    return out


def backfill_folder_columns() -> None:
    """Populate Folder1..Folder20 from SharePointPath for every row that
    still has all folder columns NULL.  Runs once per process (guarded by
    _schema_ready) so it's cheap after the first startup.

    Deliberately batched with `executemany` for throughput — one UPDATE per
    row is fine for the current ~640-row dataset; if the inventory grows
    into tens of thousands, revisit with a table-valued parameter."""
    active = settings.CONTRACT_TABLE
    ex     = excluded_table_name()
    for table in (active, ex):
        _backfill_folder_columns_for(table)


def _backfill_folder_columns_for(table: str) -> None:
    """Backfill worker for a single table."""
    folder_col_list = ", ".join(f"[{c}]" for c in FOLDER_COLUMNS)
    null_check      = " AND ".join(f"[{c}] IS NULL" for c in FOLDER_COLUMNS)
    set_clause      = ", ".join(f"[{c}] = ?" for c in FOLDER_COLUMNS)

    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT [FileID], [SharePointPath] "
            f"FROM dbo.[{table}] "
            f"WHERE ({null_check}) AND [SharePointPath] IS NOT NULL"
        )
        rows = cur.fetchall()

        if not rows:
            return

        params = []
        for file_id, path in rows:
            cols = folder_columns_from_path(path)
            params.append((*cols, str(file_id)))

        cn.autocommit = False
        try:
            # pyodbc: executemany with a parameterised UPDATE.  Fast enough
            # for the current inventory size; each row is one round-trip.
            cur.fast_executemany = True
            cur.executemany(
                f"UPDATE dbo.[{table}] SET {set_clause} WHERE [FileID] = ?",
                params,
            )
            cn.commit()
            logger.info("backfill_folder_columns: %d rows updated in dbo.%s",
                        len(params), table)
        except Exception:
            cn.rollback()
            raise
        finally:
            cn.autocommit = True


def _row_to_dict(row) -> dict:
    """Map a pyodbc Row (in _COLUMNS order) to the frontend camelCase shape.

    Includes folder1..folder20 keys derived from the persisted Folder<NN>
    columns.  Empty levels are emitted as null (never a placeholder string)
    so the UI can distinguish "no such level" from "level exists with empty
    name" and folder-filter counts stay honest.
    """
    r = dict(zip(_COLUMNS, row))
    out = {
        "fileID":                 _s(r["FileID"]) or "",
        "fileName":               _s(r["FileName"]) or "",
        "sharePointPath":         _s(r["SharePointPath"]),
        "lastModified":           _s(r["LastModified"]),
        "modifiedBy":             _s(r["ModifiedBy"]),
        "itemType":               _s(r["ItemType"]),
        "opportunityID":          _s(r["OpportunityID"]),
        "ae":                     _s(r["AE"]),
        "legalEntity":            _s(r["LegalEntity"]),
        "customerName":           _s(r["CustomerName"]),
        "agreementName":          _s(r["AgreementName"]),
        "agreementFileName":      _s(r["AgreementFileName"]),
        "orderNumber":            _s(r["OrderNumber"]),
        "autoRenewalStatus":      _s(r["AutoRenewalStatus"]),
        "effectiveDate":          _s(r["EffectiveDate"]),
        "startDate":              _s(r["StartDate"]),
        "endDate":                _s(r["EndDate"]),
        "expiryDate":             _s(r["ExpiryDate"]),
        "contractType":           _s(r["ContractType"]),
        # UI reads "contractClassification"; source column is "TypeOfContract".
        "contractClassification": _s(r["TypeOfContract"]),
        "associatedMSAFileName":  _s(r["AssociatedMSAFileName"]),
        "associatedNDAFileName":  _s(r["AssociatedNDAFileName"]),
        "voidExclusionIndicator": _s(r["VoidExclusionIndicator"]),
        "extractionStatus":       _s(r["ExtractionStatus"]),
        "reviewRequired":         _parse_bool_str(r["ReviewRequired"]),
        "missingFields":          _s(r["MissingFields"]),
        "processedDate":          _s(r["ProcessedDate"]),
        "errorMessage":           _s(r["ErrorMessage"]),
        "runId":                  _s(r["RunId"]),
        # Migration fields — the UI treats these as its source of truth.
        # Normalise to the strings the UI already handles ('Yes' / 'No').
        "migrate":                "Yes" if (_parse_bool_str(r["Migrate"]) is True
                                            or (_s(r["Migrate"]) or "").lower() == "yes")
                                        else "No",
        "migrated":               _parse_bool_str(r["Migrated"]),
        "migratedDate":           _fmt_dt(r["MigratedDate"]),
        # Canonical status field — one of: Pending / In Processing / Migrated / Failed.
        # UI treats this as the source of truth for bucket assignment.
        "migrationStatus":        _s(r["MigrationStatus"]) or STATUS_PENDING,
    }
    # Persisted folder hierarchy — one camelCase key per level.  Missing
    # levels appear as null (JSON) so the UI can render an empty cell
    # without a placeholder and folder-search checks can skip them.
    for i in range(1, FOLDER_LEVEL_MAX + 1):
        out[f"folder{i}"] = _s(r.get(f"Folder{i:02d}"))
    return out


# ── Reads ──────────────────────────────────────────────────────────────────
def load_contracts() -> List[dict]:
    """Return every ACTIVE row from ContractInventory.

    Excluded rows live in a separate table (ContractInventory_Excluded) and
    are never returned here.  Persistent — survives reload/restart.
    """
    _ensure_schema_once()
    cols = ", ".join(f"[{c}]" for c in _COLUMNS)
    sql = f"SELECT {cols} FROM dbo.[{settings.CONTRACT_TABLE}] ORDER BY [FileName]"
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
    logger.info("load_contracts: %d active rows from dbo.%s", len(rows), settings.CONTRACT_TABLE)
    return [_row_to_dict(r) for r in rows]


def load_excluded() -> List[dict]:
    """Return every row in the excluded table, newest exclusion first.

    Includes `excludedDate` (ISO-ish string via _fmt_dt) so the UI can show
    when each document was excluded.
    """
    _ensure_schema_once()
    ex = excluded_table_name()
    cols = ", ".join(f"[{c}]" for c in _COLUMNS) + ", [ExcludedDate], [ExcludedBy]"
    sql = f"SELECT {cols} FROM dbo.[{ex}] ORDER BY [ExcludedDate] DESC, [FileName]"
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
    out = []
    for row in rows:
        d = _row_to_dict(row[: len(_COLUMNS)])
        d["excludedDate"] = _fmt_dt(row[len(_COLUMNS)])
        d["excludedBy"]   = _s(row[len(_COLUMNS) + 1])
        out.append(d)
    logger.info("load_excluded: %d rows from dbo.%s", len(out), ex)
    return out


def count_all() -> dict:
    """Return per-bucket population counts.

    Response keys:
        active         — rows in the active table (Pending + In Processing + Migrated + Failed)
        excluded       — rows in the excluded table
        total          — same as active (Total Documents bucket)
        pending        — rows with MigrationStatus = 'Pending'
        in_processing  — rows with MigrationStatus = 'In Processing'
        migrated       — rows with MigrationStatus = 'Migrated'
        failed         — rows with MigrationStatus = 'Failed'

    NOTE — "total" continues to be the ACTIVE count only (not active + excluded)
    so the existing UI identity Pending + In Processing + Migrated == Total
    still holds within the active table.
    """
    _ensure_schema_once()
    active = settings.CONTRACT_TABLE
    ex = excluded_table_name()
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM dbo.[{active}]")
        a = cur.fetchone()[0] or 0
        cur.execute(f"SELECT COUNT(*) FROM dbo.[{ex}]")
        e = cur.fetchone()[0] or 0
        # Per-status counts on the active table.  Uses ISNULL so any
        # pre-existing NULL rows land in Pending (matches column default).
        cur.execute(
            f"SELECT ISNULL([MigrationStatus], '{STATUS_PENDING}') AS s, COUNT(*) "
            f"FROM dbo.[{active}] GROUP BY ISNULL([MigrationStatus], '{STATUS_PENDING}')"
        )
        by_status = {str(row[0] or STATUS_PENDING): int(row[1] or 0) for row in cur.fetchall()}
    return {
        "active":        int(a),
        "excluded":      int(e),
        "total":         int(a),
        "pending":       by_status.get(STATUS_PENDING, 0),
        "in_processing": by_status.get(STATUS_IN_PROCESSING, 0),
        "migrated":      by_status.get(STATUS_MIGRATED, 0),
        "failed":        by_status.get(STATUS_FAILED, 0),
    }


# ── Writes ─────────────────────────────────────────────────────────────────
def mark_in_processing(file_ids: Iterable[str]) -> dict:
    """Transition eligible rows from 'Pending' → 'In Processing'.

    Called immediately after the user confirms the Migrate modal, BEFORE
    the Power Automate call.  Persisting this state first ensures:
      - the Pending bucket count drops right away (UI feels responsive),
      - if the Power Automate call crashes or times out we can still see
        which files were in flight,
      - Migrate=Yes and MigratedDate are NOT touched (business rule 4).

    Only rows currently in 'Pending' are transitioned — already-processing
    or already-migrated rows are silently skipped (business rule 15).
    Returns which FileIDs actually flipped so the caller can send exactly
    those to Power Automate.
    """
    _ensure_schema_once()
    ids = [str(i).strip() for i in file_ids if str(i).strip()]
    if not ids:
        return {"succeeded": [], "skipped": [], "rowsAffected": 0}

    table = settings.CONTRACT_TABLE
    placeholders = ", ".join("?" for _ in ids)

    with get_connection() as cn:
        cn.autocommit = False
        cur = cn.cursor()
        try:
            # Eligible transitions to In Processing:
            #   Pending → In Processing   (first-time migration)
            #   Failed  → In Processing   (user retry after a prior failure)
            # Never touch rows that are already 'In Processing' (duplicate
            # submit) or 'Migrated' (§15 — cannot re-migrate).
            cur.execute(
                f"UPDATE dbo.[{table}] "
                f"SET [MigrationStatus] = ? "
                f"WHERE [FileID] IN ({placeholders}) "
                f"  AND ISNULL([MigrationStatus], '{STATUS_PENDING}') IN "
                f"       ('{STATUS_PENDING}', '{STATUS_FAILED}') "
                f"  AND ISNULL([Migrate], 'No') <> 'Yes'",
                [STATUS_IN_PROCESSING, *ids],
            )
            affected = cur.rowcount

            # Capture which IDs actually made it into In Processing.
            cur.execute(
                f"SELECT [FileID] FROM dbo.[{table}] "
                f"WHERE [FileID] IN ({placeholders}) "
                f"  AND [MigrationStatus] = ?",
                [*ids, STATUS_IN_PROCESSING],
            )
            succeeded = [str(r[0]) for r in cur.fetchall()]
            cn.commit()
        except Exception:
            cn.rollback()
            raise
        finally:
            cn.autocommit = True

    skipped = [i for i in ids if i not in set(succeeded)]
    logger.info(
        "mark_in_processing: requested=%d affected=%d skipped=%d table=dbo.%s",
        len(ids), affected, len(skipped), table,
    )
    return {"succeeded": succeeded, "skipped": skipped, "rowsAffected": affected}


def apply_migration_result(
    succeeded_ids: Iterable[str],
    failed_ids: Iterable[str],
    migrated_at: datetime | None = None,
) -> dict:
    """Apply Power Automate's per-document outcome to the database.

    For each SUCCEEDED FileID (Power Automate confirmed the copy):
        MigrationStatus = 'Migrated'
        Migrate         = 'Yes'
        Migrated        = 'True'
        MigratedDate    = <ts>

    For each FAILED FileID:
        MigrationStatus = 'Failed'
        Migrate         = 'No'       (unchanged; FAILED != MIGRATED)
        MigratedDate    = <untouched>

    Only rows currently 'In Processing' are updated on either side — this
    protects against duplicate/late webhook calls trying to re-flip an
    already-Migrated row.
    """
    _ensure_schema_once()
    succ = [str(i).strip() for i in succeeded_ids if str(i).strip()]
    fail = [str(i).strip() for i in failed_ids    if str(i).strip()]
    if not succ and not fail:
        return {"migrated": [], "failed": [], "migratedAt": None}

    ts = migrated_at or datetime.now()
    table = settings.CONTRACT_TABLE

    with get_connection() as cn:
        cn.autocommit = False
        cur = cn.cursor()
        try:
            migrated_written = []
            failed_written   = []

            if succ:
                ph = ", ".join("?" for _ in succ)
                cur.execute(
                    f"UPDATE dbo.[{table}] "
                    f"SET [MigrationStatus] = '{STATUS_MIGRATED}', "
                    f"    [Migrate] = 'Yes', "
                    f"    [Migrated] = 'True', "
                    f"    [MigratedDate] = ? "
                    f"WHERE [FileID] IN ({ph}) "
                    f"  AND [MigrationStatus] = '{STATUS_IN_PROCESSING}'",
                    [ts, *succ],
                )
                cur.execute(
                    f"SELECT [FileID] FROM dbo.[{table}] "
                    f"WHERE [FileID] IN ({ph}) AND [MigrationStatus] = '{STATUS_MIGRATED}'",
                    succ,
                )
                migrated_written = [str(r[0]) for r in cur.fetchall()]

            if fail:
                ph = ", ".join("?" for _ in fail)
                cur.execute(
                    f"UPDATE dbo.[{table}] "
                    f"SET [MigrationStatus] = '{STATUS_FAILED}' "
                    f"WHERE [FileID] IN ({ph}) "
                    f"  AND [MigrationStatus] = '{STATUS_IN_PROCESSING}'",
                    fail,
                )
                cur.execute(
                    f"SELECT [FileID] FROM dbo.[{table}] "
                    f"WHERE [FileID] IN ({ph}) AND [MigrationStatus] = '{STATUS_FAILED}'",
                    fail,
                )
                failed_written = [str(r[0]) for r in cur.fetchall()]

            cn.commit()
        except Exception:
            cn.rollback()
            raise
        finally:
            cn.autocommit = True

    logger.info(
        "apply_migration_result: migrated=%d failed=%d table=dbo.%s",
        len(migrated_written), len(failed_written), table,
    )
    return {
        "migrated":   migrated_written,
        "failed":     failed_written,
        "migratedAt": _fmt_dt(ts),
    }


def get_documents_for_migration(file_ids: Iterable[str]) -> List[dict]:
    """Return the minimal set of fields Power Automate needs to copy each
    file (FileID + FileName + source SharePoint URL + a few metadata bits
    for auditing).  Only returns rows currently 'In Processing' — this
    guarantees we never send a Pending or Migrated row to Power Automate."""
    _ensure_schema_once()
    ids = [str(i).strip() for i in file_ids if str(i).strip()]
    if not ids:
        return []
    table = settings.CONTRACT_TABLE
    ph = ", ".join("?" for _ in ids)
    sql = (
        f"SELECT [FileID], [FileName], [SharePointPath], [CustomerName], "
        f"       [AgreementName], [ContractType] "
        f"FROM dbo.[{table}] "
        f"WHERE [FileID] IN ({ph}) "
        f"  AND [MigrationStatus] = '{STATUS_IN_PROCESSING}'"
    )
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(sql, ids)
        rows = cur.fetchall()
    return [
        {
            "fileID":         _s(r[0]) or "",
            "fileName":       _s(r[1]) or "",
            "sharePointPath": _s(r[2]),
            "customerName":   _s(r[3]),
            "agreementName":  _s(r[4]),
            "contractType":   _s(r[5]),
        }
        for r in rows
    ]


def mark_excluded(file_ids: Iterable[str],
                  excluded_by: str | None = None,
                  session_id:  str | None = None,
                  reason:      str | None = None,
                  folder_filter_text: str | None = None,
                  folder_filter_mode: str | None = None,
                  matches_by_id: dict[str, dict] | None = None) -> dict:
    """MOVE the given FileIDs from the active table into the excluded table.

    Transactional:  INSERT into excluded → DELETE from active → COMMIT,
    followed by INSERTs into `exclusion_audit` (one row per succeeded
    file) inside the SAME transaction so the move and its audit trail
    are atomic together.

    Optional metadata (all default None for back-compat with the plain
    row-selection Exclude button):
      excluded_by          — logged-in company email (from session)
      session_id           — current session id (audit)
      reason               — short label, e.g. "Matched folder filter"
      folder_filter_text   — the filter's search text (e.g. "Don't Use")
      folder_filter_mode   — "contains" | "starts_with" | "exact"
      matches_by_id        — per-file matched-level info, e.g.
                             {"FID123": {"level": "Folder 4", "value": "Services"}}
                             Overrides folder_filter_text when present.

    Business rule: already-migrated rows (Migrate='Yes') are NEVER excluded.
    They stay in the active table so the migration audit trail is preserved.

    Never physically deletes anything — the row simply lives in a different
    table.  Recoverable via restore_excluded().  No SharePoint file touched.
    """
    _ensure_schema_once()
    ids = [str(i).strip() for i in file_ids if str(i).strip()]
    if not ids:
        return {"succeeded": [], "failed": [], "rowsAffected": 0}

    active = settings.CONTRACT_TABLE
    ex = excluded_table_name()
    audit = exclusion_audit_table_name()
    placeholders = ", ".join("?" for _ in ids)
    copy_cols = ", ".join(f"[{c}]" for c in _COLUMNS)

    # ExcludedDate is set via SYSUTCDATETIME() (table default) so the DB
    # is the timestamp source of truth.  We pass ExcludedDate explicitly for
    # portability: a single SYSUTCDATETIME() call for the whole batch keeps
    # them all identical (nicer for audit "run" queries).
    now = datetime.utcnow()
    excluded_by = (excluded_by or "").strip() or None
    session_id  = (session_id  or "").strip() or None
    reason      = (reason      or "").strip() or None
    folder_filter_text = (folder_filter_text or "").strip() or None
    folder_filter_mode = (folder_filter_mode or "").strip() or None
    matches_by_id      = matches_by_id or {}

    with get_connection() as cn:
        cn.autocommit = False
        cur = cn.cursor()
        try:
            # Step 1 — copy eligible rows into the excluded table.  Skip rows
            # that are already migrated (business rule), currently 'In Processing'
            # (must not disturb an in-flight Power Automate copy), or already
            # excluded (dedup safety).  ExcludedBy captured here so restore
            # queries can show provenance.
            cur.execute(
                f"INSERT INTO dbo.[{ex}] ({copy_cols}, [ExcludedDate], [ExcludedBy]) "
                f"SELECT {copy_cols}, ?, ? "
                f"FROM dbo.[{active}] "
                f"WHERE [FileID] IN ({placeholders}) "
                f"  AND ISNULL([Migrate], 'No') <> 'Yes' "
                f"  AND ISNULL([MigrationStatus], '{STATUS_PENDING}') <> '{STATUS_IN_PROCESSING}' "
                f"  AND [FileID] NOT IN (SELECT [FileID] FROM dbo.[{ex}])",
                [now, excluded_by, *ids],
            )
            copied = cur.rowcount

            # Step 2 — capture which IDs actually made it into excluded so we
            # can return an accurate succeeded list AND drive the DELETE.
            cur.execute(
                f"SELECT [FileID], [FileName] FROM dbo.[{ex}] "
                f"WHERE [FileID] IN ({placeholders}) "
                f"  AND [ExcludedDate] = ?",
                [*ids, now],
            )
            succeeded_rows = [(str(r[0]), _s(r[1])) for r in cur.fetchall()]
            succeeded = [fid for fid, _ in succeeded_rows]

            # Step 3 — delete originals from active (only the ones we
            # successfully copied).
            deleted = 0
            if succeeded:
                del_ph = ", ".join("?" for _ in succeeded)
                cur.execute(
                    f"DELETE FROM dbo.[{active}] WHERE [FileID] IN ({del_ph})",
                    succeeded,
                )
                deleted = cur.rowcount

            # Step 4 — insert one audit row per succeeded file.  Written in
            # the same transaction so a failed move never leaves an orphan
            # audit row (and vice-versa).  Manual exclusions pass None for
            # reason/level/value; the columns are NULLable so that's fine.
            if succeeded_rows:
                audit_rows = []
                for fid, fname in succeeded_rows:
                    m = matches_by_id.get(fid) or {}
                    audit_rows.append((
                        fid,
                        fname,
                        now,
                        excluded_by,
                        session_id,
                        reason,
                        (m.get("level")  or None),
                        (m.get("value")  or None),
                        folder_filter_text,
                        folder_filter_mode,
                    ))
                cur.fast_executemany = True
                cur.executemany(
                    f"INSERT INTO dbo.[{audit}] "
                    f"(file_id, file_name, excluded_at, excluded_by, session_id, "
                    f" reason, matched_folder_level, matched_folder_value, "
                    f" folder_filter_text, folder_filter_mode) "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    audit_rows,
                )

            cn.commit()
        except Exception:
            cn.rollback()
            raise
        finally:
            cn.autocommit = True

    failed = [i for i in ids if i not in set(succeeded)]
    logger.info(
        "mark_excluded: requested=%d copied=%d deleted=%d failed=%d by=%s reason=%s",
        len(ids), copied, deleted, len(failed), excluded_by or "-", reason or "-",
    )
    return {
        "succeeded":    succeeded,
        "failed":       failed,
        "rowsAffected": deleted,
    }


def restore_excluded(file_ids: Iterable[str],
                     restored_by: str | None = None) -> dict:
    """MOVE the given FileIDs from the excluded table back into the active table.

    Transactional:  INSERT into active → DELETE from excluded → COMMIT.
    Original FileID and all metadata (including Folder1..Folder20) are
    preserved.  Migration state (Migrate / Migrated / MigratedDate /
    MigrationStatus) is carried across unchanged so an accidentally-
    excluded migrated row stays migrated on restore.

    Also stamps `restored_at` + `restored_by` on the MOST RECENT
    exclusion_audit row per restored file.  The audit row itself is
    never deleted — history is append-only.
    """
    _ensure_schema_once()
    ids = [str(i).strip() for i in file_ids if str(i).strip()]
    if not ids:
        return {"succeeded": [], "failed": [], "rowsAffected": 0}

    active = settings.CONTRACT_TABLE
    ex = excluded_table_name()
    audit = exclusion_audit_table_name()
    placeholders = ", ".join("?" for _ in ids)
    copy_cols = ", ".join(f"[{c}]" for c in _COLUMNS)
    restored_by = (restored_by or "").strip() or None
    now = datetime.utcnow()

    with get_connection() as cn:
        cn.autocommit = False
        cur = cn.cursor()
        try:
            # Copy back to active — Excluded flag defaults to 'No', so no
            # need to set it explicitly.  Dedup against existing active rows.
            cur.execute(
                f"INSERT INTO dbo.[{active}] ({copy_cols}) "
                f"SELECT {copy_cols} FROM dbo.[{ex}] "
                f"WHERE [FileID] IN ({placeholders}) "
                f"  AND [FileID] NOT IN (SELECT [FileID] FROM dbo.[{active}])",
                ids,
            )
            copied = cur.rowcount

            cur.execute(
                f"SELECT [FileID] FROM dbo.[{active}] "
                f"WHERE [FileID] IN ({placeholders})",
                ids,
            )
            succeeded = [str(r[0]) for r in cur.fetchall()]

            deleted = 0
            if succeeded:
                del_ph = ", ".join("?" for _ in succeeded)
                cur.execute(
                    f"DELETE FROM dbo.[{ex}] WHERE [FileID] IN ({del_ph})",
                    succeeded,
                )
                deleted = cur.rowcount

                # Stamp the most recent audit row per restored file.
                # Uses a correlated subquery to target the newest un-restored
                # exclusion event; older events remain as-is.
                cur.execute(
                    f"UPDATE a SET a.restored_at = ?, a.restored_by = ? "
                    f"FROM dbo.[{audit}] a "
                    f"WHERE a.id IN ( "
                    f"  SELECT MAX(id) FROM dbo.[{audit}] "
                    f"  WHERE file_id IN ({del_ph}) AND restored_at IS NULL "
                    f"  GROUP BY file_id "
                    f")",
                    [now, restored_by, *succeeded],
                )

            cn.commit()
        except Exception:
            cn.rollback()
            raise
        finally:
            cn.autocommit = True

    failed = [i for i in ids if i not in set(succeeded)]
    logger.info(
        "restore_excluded: requested=%d copied=%d deleted=%d failed=%d by=%s",
        len(ids), copied, deleted, len(failed), restored_by or "-",
    )
    return {
        "succeeded":    succeeded,
        "failed":       failed,
        "rowsAffected": deleted,
    }
