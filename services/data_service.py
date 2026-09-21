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
    ensure_excluded_column,
    ensure_excluded_table,
    excluded_table_name,
    get_connection,
)

logger = logging.getLogger(__name__)

# Lazily bring the schema up to date on the first DB access this process makes.
# Cached at module scope so we only run the metadata checks once per process.
_schema_ready = False


def _ensure_schema_once() -> None:
    """Idempotent, cached: add Excluded column + create excluded table +
    move any legacy Excluded='Yes' rows into the excluded table so state is
    normalised (excluded rows live ONLY in the excluded table)."""
    global _schema_ready
    if _schema_ready:
        return
    try:
        ensure_excluded_column()
        ensure_excluded_table()
        _migrate_legacy_excluded_rows()
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
_COLUMNS = [
    "FileID", "FileName", "SharePointPath", "LastModified", "ModifiedBy",
    "ItemType", "OpportunityID", "AE", "LegalEntity", "CustomerName",
    "AgreementName", "AgreementFileName", "OrderNumber", "AutoRenewalStatus",
    "EffectiveDate", "StartDate", "EndDate", "ExpiryDate", "ContractType",
    "TypeOfContract", "AssociatedMSAFileName", "AssociatedNDAFileName",
    "VoidExclusionIndicator", "ExtractionStatus", "ReviewRequired",
    "MissingFields", "ProcessedDate", "ErrorMessage", "RunId",
    "Migrate", "Migrated", "MigratedDate",
]


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


def _row_to_dict(row) -> dict:
    """Map a pyodbc Row (in _COLUMNS order) to the frontend camelCase shape."""
    r = dict(zip(_COLUMNS, row))
    return {
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
    }


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
    """Return {active, excluded, total}.

    IMPORTANT — semantics: "total" is the ACTIVE document count, i.e. the
    documents currently available for review/migration.  Excluded records
    live in a separate table and are surfaced only via the "excluded" key.
    This matches the UI's Total Documents bucket, which shows only active
    rows (a Pending+Migrated+Excluded=Total identity does NOT hold).
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
    return {"active": int(a), "excluded": int(e), "total": int(a)}


# ── Writes ─────────────────────────────────────────────────────────────────
def mark_migrated(file_ids: Iterable[str], migrated_at: datetime | None = None) -> dict:
    """Mark ONLY the given FileIDs as Migrate='Yes' + set MigratedDate.

    Uses parameterised queries.  Returns counts + timestamp.
    """
    ids = [str(i).strip() for i in file_ids if str(i).strip()]
    if not ids:
        return {"succeeded": [], "failed": [], "migratedAt": None, "rowsAffected": 0}

    ts = migrated_at or datetime.now()
    table = settings.CONTRACT_TABLE

    # Only flip rows that are still pending — never re-stamp an already
    # migrated document (protects any auditor timestamp on re-runs).
    placeholders = ", ".join("?" for _ in ids)
    sql = (
        f"UPDATE dbo.[{table}] "
        f"SET [Migrate] = 'Yes', [Migrated] = 'True', [MigratedDate] = ? "
        f"WHERE [FileID] IN ({placeholders}) AND (ISNULL([Migrate], 'No') <> 'Yes')"
    )
    params = [ts, *ids]

    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(sql, params)
        affected = cur.rowcount
        cn.commit()

    logger.info(
        "mark_migrated: requested=%d affected=%d table=dbo.%s",
        len(ids), affected, table,
    )
    return {
        "succeeded":    ids,
        "failed":       [],
        "migratedAt":   _fmt_dt(ts),
        "rowsAffected": affected,
    }


def mark_excluded(file_ids: Iterable[str]) -> dict:
    """MOVE the given FileIDs from the active table into the excluded table.

    Transactional:  INSERT into excluded → DELETE from active → COMMIT.
    Either both succeed or neither does — no orphaned or duplicated rows.

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
    placeholders = ", ".join("?" for _ in ids)
    copy_cols = ", ".join(f"[{c}]" for c in _COLUMNS)

    # ExcludedDate is set via SYSUTCDATETIME() (table default) so the DB
    # is the timestamp source of truth.  We pass ExcludedDate explicitly for
    # portability: a single SYSUTCDATETIME() call for the whole batch keeps
    # them all identical (nicer for audit "run" queries).
    now = datetime.utcnow()

    with get_connection() as cn:
        cn.autocommit = False
        cur = cn.cursor()
        try:
            # Step 1 — copy eligible rows into the excluded table.  Skip rows
            # that are already migrated (business rule) or already excluded
            # (dedup safety).
            cur.execute(
                f"INSERT INTO dbo.[{ex}] ({copy_cols}, [ExcludedDate]) "
                f"SELECT {copy_cols}, ? "
                f"FROM dbo.[{active}] "
                f"WHERE [FileID] IN ({placeholders}) "
                f"  AND ISNULL([Migrate], 'No') <> 'Yes' "
                f"  AND [FileID] NOT IN (SELECT [FileID] FROM dbo.[{ex}])",
                [now, *ids],
            )
            copied = cur.rowcount

            # Step 2 — capture which IDs actually made it into excluded so we
            # can return an accurate succeeded list AND drive the DELETE.
            cur.execute(
                f"SELECT [FileID] FROM dbo.[{ex}] "
                f"WHERE [FileID] IN ({placeholders}) "
                f"  AND [ExcludedDate] = ?",
                [*ids, now],
            )
            succeeded = [str(r[0]) for r in cur.fetchall()]

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

            cn.commit()
        except Exception:
            cn.rollback()
            raise
        finally:
            cn.autocommit = True

    failed = [i for i in ids if i not in set(succeeded)]
    logger.info(
        "mark_excluded: requested=%d copied=%d deleted=%d failed=%d",
        len(ids), copied, deleted, len(failed),
    )
    return {
        "succeeded":    succeeded,
        "failed":       failed,
        "rowsAffected": deleted,
    }


def restore_excluded(file_ids: Iterable[str]) -> dict:
    """MOVE the given FileIDs from the excluded table back into the active table.

    Transactional:  INSERT into active → DELETE from excluded → COMMIT.
    Original FileID and all metadata are preserved.  Migration state
    (Migrate / Migrated / MigratedDate) is carried across unchanged so
    an accidentally-excluded migrated row stays migrated on restore.
    """
    _ensure_schema_once()
    ids = [str(i).strip() for i in file_ids if str(i).strip()]
    if not ids:
        return {"succeeded": [], "failed": [], "rowsAffected": 0}

    active = settings.CONTRACT_TABLE
    ex = excluded_table_name()
    placeholders = ", ".join("?" for _ in ids)
    copy_cols = ", ".join(f"[{c}]" for c in _COLUMNS)

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

            cn.commit()
        except Exception:
            cn.rollback()
            raise
        finally:
            cn.autocommit = True

    failed = [i for i in ids if i not in set(succeeded)]
    logger.info(
        "restore_excluded: requested=%d copied=%d deleted=%d failed=%d",
        len(ids), copied, deleted, len(failed),
    )
    return {
        "succeeded":    succeeded,
        "failed":       failed,
        "rowsAffected": deleted,
    }
