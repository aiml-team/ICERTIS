"""Contract data service — Azure SQL backed.

Public API:
    load_contracts(include_excluded=False) -> List[dict]   (all inventory)
    load_excluded()                        -> List[dict]   (Excluded='Yes' rows)
    count_all()                            -> dict         ({active, excluded, total, per-status})
    mark_in_processing(ids)                -> dict         (rows updated)
    mark_excluded(ids)                     -> dict         (rows FLAGGED Excluded='Yes')
    restore_excluded(ids)                  -> dict         (rows FLAGGED Excluded='No')

Exclusion model (post 2026-09-22 refactor)
──────────────────────────────────────────
Excluded documents live in the SAME master inventory as everything else.
There is no separate "excluded" table on the read path — Excluded is just
a status flag (`Excluded='Yes'`, plus `ExcludedDate` / `ExcludedBy`
metadata columns).  Views:

    ContractInventory
        ├── Excluded = 'No'  → active inventory (review UI)
        │       ├── MigrationStatus = 'Pending'
        │       ├── MigrationStatus = 'In Processing'
        │       ├── MigrationStatus = 'Migrated'
        │       └── MigrationStatus = 'Failed'
        └── Excluded = 'Yes' → excluded view (recoverable soft-delete)

The legacy ContractInventory_Excluded table is preserved for AUDIT ONLY:
_ensure_schema_once() runs a one-time back-migration that copies any rows
still in that table back into the master with Excluded='Yes' (dedup on
FileID + preserves ExcludedDate/ExcludedBy).  After that first pass the
legacy table is never written to and no read path touches it.

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
    ensure_excluded_metadata_columns,
    ensure_excluded_table,
    ensure_exclusion_audit_table,
    ensure_folder_columns,
    ensure_migration_integration_columns,
    ensure_migration_status_column,
    ensure_retry_history_table,
    ensure_row_number_column,
    ensure_submitted_by_column,
    exclusion_audit_table_name,
    excluded_table_name,
    get_connection,
    retry_history_table_name,
)

# Canonical MigrationStatus values.  Kept as constants so callers can never
# introduce drift via typos.  All comparisons/writes go through these.
STATUS_PENDING       = "Pending"
STATUS_IN_PROCESSING = "In Processing"
STATUS_MIGRATED      = "Migrated"
# Terminal-failure state.  Historically stored as "Failed"; the product
# rename to "Error" (per §Required top buckets) keeps the same semantics
# but changes the on-disk enum value.  STATUS_FAILED is kept as a legacy
# alias so any test/import that still references the old name resolves to
# the new value — every WRITE now emits "Error", so within one deploy the
# on-disk data converges.  Reads treat both spellings as equivalent via
# _ERROR_MIGRATION_STATUSES below.
STATUS_ERROR         = "Error"
STATUS_FAILED        = STATUS_ERROR  # legacy alias; do not use in new code

# All spellings that mean "terminal failure".  Every WHERE that wants to
# include failed rows should reference this tuple; every WRITE always
# emits STATUS_ERROR so on-disk data converges to the new spelling as
# rows are touched.
_ERROR_MIGRATION_STATUSES = ("Error", "Failed")

# Statuses that count as "actively occupying" the platform queue for the
# per-user workload lock.  Rows in any of these statuses block their
# SubmittedBy owner from starting another migration batch.  Terminal
# statuses (Migrated / Failed / Skipped / Excluded) do NOT block.
#
# Includes 'In Processing' (our canonical MigrationStatus value) plus the
# raw backend enum values that transient sync races might leave in
# MigrationBackendStatus but the sync poller then rolls up into
# MigrationStatus.  Filtering on MigrationStatus alone is sufficient because
# apply_backend_file_status() always maps queued/retrying/pending-with-a-
# request-id back to STATUS_IN_PROCESSING on our side.
ACTIVE_MIGRATION_STATUSES = (STATUS_IN_PROCESSING,)


class ActiveMigrationExistsError(RuntimeError):
    """Raised by mark_in_processing() when the caller (identified by email)
    already has one or more rows in an active migration status.

    Carries the current count so /api/migrate can echo it in the 409
    response body without re-querying.
    """
    def __init__(self, email: str, active_count: int):
        self.email = email
        self.active_count = int(active_count)
        super().__init__(
            f"User {email!r} already has {active_count} active migration row(s)."
        )


logger = logging.getLogger(__name__)

# Lazily bring the schema up to date on the first DB access this process makes.
# Cached at module scope so we only run the metadata checks once per process.
_schema_ready = False


def _ensure_schema_once() -> None:
    """Idempotent, cached: bring the schema up to date for the current
    process on the first DB access.

    Steps (in order — each is idempotent on its own):
      1. Add [Excluded] column to master inventory (legacy schemas).
      2. Add [ExcludedDate] + [ExcludedBy] to master inventory (unified
         inventory model — replaces the metadata that used to live on
         ContractInventory_Excluded).
      3. Create the legacy excluded table if missing — needed only so the
         back-migration in step 8 has something to read from.  After the
         back-migration runs successfully, this table is no longer written
         to (kept for historical audit only).
      4. Add [MigrationStatus] to both tables.
      5. Add Folder01..Folder20 to both tables.
      6. Add migration-platform integration columns to both tables.
      7. Create the exclusion_audit sidecar.
      8. Back-migrate any rows still in ContractInventory_Excluded into
         the master inventory with Excluded='Yes' (one-shot; dedup on
         FileID; preserves ExcludedDate/ExcludedBy).
      9. Backfill Folder01..Folder20 from SharePointPath.

    Never let a schema-check failure crash reads — log and retry next call.
    """
    global _schema_ready
    if _schema_ready:
        return
    try:
        ensure_excluded_column()
        ensure_excluded_metadata_columns()
        ensure_excluded_table()                    # legacy — retained for step 8
        ensure_migration_status_column()
        ensure_folder_columns()
        ensure_migration_integration_columns()
        ensure_submitted_by_column()               # per-user workload lock
        ensure_row_number_column()                 # permanent sequential "#"
        ensure_exclusion_audit_table()
        ensure_retry_history_table()               # retry audit trail
        _migrate_legacy_excluded_table_back()      # one-shot back-migration
        backfill_folder_columns()
        _backfill_row_numbers_one_shot()           # number existing NULL rows
        _schema_ready = True
    except Exception as exc:
        logger.warning("_ensure_schema_once() failed: %s", exc)


def _migrate_legacy_excluded_table_back() -> None:
    """One-shot back-migration for the unified-inventory refactor.

    Prior to the refactor, excluded documents were physically moved into
    a separate ContractInventory_Excluded table.  The new model keeps them
    on the master ContractInventory row with `Excluded='Yes'` and inline
    exclusion metadata (`ExcludedDate`, `ExcludedBy`).  This function
    reconciles those two models the first time it runs on a database that
    still has rows in the legacy excluded table.

    Behaviour (all inside one transaction per call — idempotent):
      1. Any excluded-table row whose FileID does NOT exist in the master
         → INSERT into master with the row's business + folder + migration
         columns, `Excluded='Yes'`, and its original ExcludedDate/ExcludedBy.
      2. Any excluded-table row whose FileID DOES exist in the master →
         UPDATE the master row to `Excluded='Yes'` (+ ExcludedDate/ExcludedBy
         from the legacy row) so state is unified.  No column values from
         the master are lost — we only flip the Excluded flag and stamp
         the two metadata columns.
      3. Legacy rows are NEVER deleted here.  The legacy table is kept as
         a historical audit artefact.  After this back-migration runs, the
         two exclusion write paths (mark_excluded, restore_excluded) only
         touch the master table.

    Safe to run repeatedly: step 1 uses `NOT IN` so no duplicates are
    created; step 2 is an idempotent UPDATE.  Fails-soft: any exception
    rolls back and leaves state untouched — the caller (which is the
    schema-check pass) logs a warning and the app continues.
    """
    active = settings.CONTRACT_TABLE
    ex     = excluded_table_name()
    with get_connection() as cn:
        cn.autocommit = False
        cur = cn.cursor()
        try:
            # How many legacy rows exist?  Cheap gate — most calls will
            # short-circuit here after the first successful migration
            # (once the legacy table has been fully back-migrated, ops
            # will typically leave the empty table alone or drop it).
            cur.execute(f"SELECT COUNT(*) FROM dbo.[{ex}]")
            legacy_n = cur.fetchone()[0] or 0
            if legacy_n == 0:
                cn.rollback()
                return

            # Step 1 — INSERT rows that are ONLY in the legacy table into
            # the master with Excluded='Yes' and the preserved metadata.
            # Build the column list from the INTERSECTION of both tables'
            # actual columns — the master schema evolves independently
            # (SubmittedBy is one such newer addition) and the legacy
            # excluded table is frozen, so any master-only column must be
            # skipped in the copy or the INSERT SELECT fails with
            # "Invalid column name".  Master-only columns simply keep
            # their default (NULL) on the newly inserted rows.
            cur.execute(
                "SELECT name FROM sys.columns "
                "WHERE object_id = OBJECT_ID(?)",
                [f"dbo.[{ex}]"],
            )
            legacy_cols = {str(r[0]) for r in cur.fetchall()}
            # Never copy the master IDENTITY PK (`Id`) — SQL Server would
            # reject explicit inserts into it without SET IDENTITY_INSERT.
            # The new master row gets a fresh Id assigned on INSERT.
            shared_cols = [c for c in _COLUMNS if c in legacy_cols and c != "Id"]
            copy_cols_sql = ", ".join(f"[{c}]" for c in shared_cols)
            cur.execute(
                f"INSERT INTO dbo.[{active}] "
                f"    ({copy_cols_sql}, [Excluded], [ExcludedDate], [ExcludedBy]) "
                f"SELECT {copy_cols_sql}, 'Yes', [ExcludedDate], [ExcludedBy] "
                f"FROM   dbo.[{ex}] AS L "
                f"WHERE  L.[FileID] NOT IN (SELECT [FileID] FROM dbo.[{active}])"
            )
            inserted = cur.rowcount

            # Step 2 — for FileIDs that ALREADY exist in master, just flag
            # them Excluded and pull across the exclusion metadata (leave
            # every other column value in the master untouched).  We only
            # touch rows currently Excluded='No' so a repeat run is a
            # true no-op.
            cur.execute(
                f"UPDATE M "
                f"SET    M.[Excluded]     = 'Yes', "
                f"       M.[ExcludedDate] = COALESCE(M.[ExcludedDate], L.[ExcludedDate]), "
                f"       M.[ExcludedBy]   = COALESCE(M.[ExcludedBy],   L.[ExcludedBy]) "
                f"FROM   dbo.[{active}] AS M "
                f"JOIN   dbo.[{ex}]     AS L ON L.[FileID] = M.[FileID] "
                f"WHERE  ISNULL(M.[Excluded], 'No') <> 'Yes'"
            )
            updated = cur.rowcount

            cn.commit()
            if inserted or updated:
                logger.info(
                    "_migrate_legacy_excluded_table_back: legacy_rows=%d "
                    "inserted_into_master=%d flagged_in_master=%d "
                    "(legacy table retained for audit)",
                    legacy_n, inserted, updated,
                )
        except Exception:
            cn.rollback()
            raise
        finally:
            cn.autocommit = True


# Column order used in SELECT / INSERT below. Kept in one place so seed + read
# stay in sync.  Matches the physical schema of dbo.ContractInventory1
# (see results (4).xlsx) — new fields (Id, FolderName, FolderType, BatchID,
# BatchNumber, PageCount, TextCharacters, EndCustomerName, QuoteID,
# AnnualFeeIncrease, FeeIncreaseDate) are appended so downstream index-based
# access stays stable.
_BASE_COLUMNS = [
    "FileID", "FileName", "SharePointPath", "LastModified", "ModifiedBy",
    "ItemType", "OpportunityID", "AE", "LegalEntity", "CustomerName",
    "AgreementName", "AgreementFileName", "OrderNumber", "AutoRenewalStatus",
    "EffectiveDate", "StartDate", "EndDate", "ExpiryDate", "ContractType",
    "TypeOfContract", "AssociatedMSAFileName", "AssociatedNDAFileName",
    "VoidExclusionIndicator", "ExtractionStatus", "ReviewRequired",
    "MissingFields", "ProcessedDate", "ErrorMessage", "RunId",
    "Migrate", "Migrated", "MigratedDate", "MigrationStatus",
    # ── New schema fields (added in ContractInventory1) ─────────────────
    "Id", "FolderName", "FolderType", "BatchID", "BatchNumber",
    "PageCount", "TextCharacters",
    "EndCustomerName", "QuoteID", "AnnualFeeIncrease", "FeeIncreaseDate",
    # Permanent per-file sequential number ("#" column in the UI).  Added
    # LAST so downstream index-based access into _COLUMNS stays stable.
    "RowNumber",
]

# Folder1..Folder20 columns — appended AFTER the base columns so index-based
# access in _row_to_dict stays stable and the excluded-table SELECT can
# continue to tack ExcludedDate/ExcludedBy on the very end.
FOLDER_COLUMNS = [f"Folder{i:02d}" for i in range(1, FOLDER_LEVEL_MAX + 1)]

# Migration-platform integration columns — MUST match the order added by
# core.database.ensure_migration_integration_columns() and appear LAST in
# _COLUMNS so restoring an excluded row still picks up ExcludedDate /
# ExcludedBy at fixed tail offsets in load_excluded().
MIGRATION_INTEGRATION_COLUMNS = [
    "MigrationRequestId", "MigrationFileItemId", "MigrationSubmittedAt",
    "MigrationBackendStatus", "MigrationRetryCount", "MigrationErrorCode",
    "DestinationUrl", "MigrationLastSyncedAt",
    # SubmittedBy is populated by mark_in_processing() with the session
    # email of the user who clicked Migrate.  Exposed to the UI so any
    # user viewing the global In Processing bucket can see who initiated
    # each migration ("Submitted By" column).  Does NOT change any
    # filtering: rows are visible to every session regardless of value.
    "SubmittedBy",
]

_COLUMNS = _BASE_COLUMNS + FOLDER_COLUMNS + MIGRATION_INTEGRATION_COLUMNS


def _s(v) -> str | None:
    """Trim + treat empty as None.

    Handles Python `date`/`datetime` values (returned by pyodbc for the
    new-schema DATE / DATETIME columns) by rendering them in the format
    the frontend `parseDate()` helper already understands.
    """
    if v is None:
        return None
    # Import locally so this stays a hot-path free of module-level cost.
    import datetime as _dt
    if isinstance(v, _dt.datetime):
        return _fmt_dt(v)
    if isinstance(v, _dt.date):
        # ISO-ish M/D/YYYY (matches the client date parser expectations).
        return f"{v.month}/{v.day}/{v.year}"
    s = str(v).strip()
    return s if s else None


def _parse_bool_str(v) -> bool | None:
    # pyodbc may return a real Python bool for BIT columns (new schema:
    # ReviewRequired is BIT).  Preserve legacy NVARCHAR handling for
    # backwards compatibility with any string-typed source (CSV seed).
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return bool(v)
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
# The document library at the site root is named "Contracts" — same word
# also appears as a real folder further down (e.g.
#   /sites/US-Contracts_Management/Contracts/All Contracts/Contracts/Client/…
#    library-name-^^^^^^^^^^^                 real-folder-^^^^^^^^^
# ).  We strip it ONLY when it is the very first segment after the
# /sites/<site>/ prefix so the inner "Contracts" folder is preserved.
_LIBRARY_NAME_LEADING   = "Contracts"


def folder_segments_for(sharepoint_path: str | None) -> list[str]:
    """Return the list of business-meaningful folder segments for a path.

    Rules (must match the client parser):
      • strip query/fragment
      • if URL, drop scheme+host and any leading `/sites/<site>/` pair
      • otherwise treat as POSIX/Windows path
      • decode percent-encoded segments
      • drop technical segments (Shared Documents, Forms, AllItems.aspx…)
      • drop the trailing filename (last segment whose extension is 1-6 alnum)

    "All Contracts" is a REAL folder in the SharePoint hierarchy — it is
    preserved verbatim (was previously collapsed as a "virtual root" but
    that hid a level the business asked to see).  The value shows up
    exactly as it does in the URL path.
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
        # Strip the SharePoint document-library name when it's the very
        # first segment (see _LIBRARY_NAME_LEADING).  Only touches the
        # LEADING occurrence — an identically-named real folder deeper
        # in the tree is preserved verbatim.
        if parts and parts[0].lower() == _LIBRARY_NAME_LEADING.lower():
            parts = parts[1:]
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

    return decoded


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
    still has all folder columns NULL.

    Called from two places:
      1. `_ensure_schema_once()` — once per process on the first DB access,
         so a fresh startup on an un-backfilled database catches up.
      2. `load_contracts()` — on EVERY read, via `backfill_folder_columns_if_needed()`.
         Cost is a single indexed SELECT (rows-that-need-work) — a no-op
         return when nothing matches.  Guarantees that rows inserted by
         the upstream pipeline WHILE the app is running have their folder
         columns filled in by the very next UI refresh, instead of having
         to wait for a server restart.

    Deliberately batched with `executemany` for throughput — one UPDATE per
    row is fine for the current ~640-row dataset; if the inventory grows
    into tens of thousands, revisit with a table-valued parameter."""
    active = settings.CONTRACT_TABLE
    ex     = excluded_table_name()
    for table in (active, ex):
        _backfill_folder_columns_for(table)


# One-shot per process: run the stale-parser re-derivation exactly once
# on the first /api/contracts hit after startup.  Any parser change
# ships as a code deploy → new process → this fires once → subsequent
# reads skip the O(N) scan.  New rows added while the process is
# running are still caught by the NULL-scan path on every read.
_stale_folder_check_done = False


def backfill_folder_columns_if_needed() -> int:
    """Live-backfill worker for the ACTIVE inventory only.

    Two update paths:

      A. (Every read) Rows with ALL Folder01..20 NULL — fresh inserts
         the upstream pipeline hasn't backfilled yet → parse
         SharePointPath, fill.  Bail-out on the first indexed COUNT
         when nothing matches (near-zero cost in steady state).

      B. (Once per process) Rows where the PERSISTED Folder01 does
         NOT match what the current parser produces — catches parser
         logic changes (e.g. "All Contracts" is now preserved instead
         of collapsed) without requiring a one-shot migration script.
         Runs a single O(N) scan on the first read after startup
         then never again for the life of the process.

    Returns the number of rows updated (0 in the common no-work case)."""
    global _stale_folder_check_done
    table = settings.CONTRACT_TABLE

    # Path A — rows with all folder columns NULL.  Cheap indexed count first.
    null_updated = 0
    if _pending_folder_backfill_count(table) > 0:
        _backfill_folder_columns_for(table)
        null_updated = -1     # exact count logged inside worker

    # Path B — one-shot parser-drift reconciliation.
    stale_updated = 0
    if not _stale_folder_check_done:
        try:
            stale_updated = _rederive_stale_folder_rows(table)
        finally:
            _stale_folder_check_done = True

    return (null_updated if null_updated > 0 else 0) + stale_updated


def _pending_folder_backfill_count(table: str) -> int:
    """Return how many rows in `table` still need Folder01..20 backfilled.

    Zero means the live-backfill hot path can bail out with a single
    indexed COUNT and never open an UPDATE transaction."""
    null_check = " AND ".join(f"[{c}] IS NULL" for c in FOLDER_COLUMNS)
    sql = (f"SELECT COUNT(*) FROM dbo.[{table}] "
           f"WHERE ({null_check}) AND [SharePointPath] IS NOT NULL")
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(sql)
        return int(cur.fetchone()[0] or 0)


def _rederive_stale_folder_rows(table: str) -> int:
    """Re-derive Folder01..Folder20 for rows whose persisted Folder01
    does not match what the current parser produces.

    Only touches rows where the divergence is REAL (persisted first
    segment != freshly-parsed first segment).  Rows where the parser
    can't produce any segment (no SharePointPath, no derivable
    folders) are left alone.

    Cost: one SELECT of (FileID, SharePointPath, Folder01) — light
    payload, indexed on FileID.  UPDATE only fires for the divergent
    subset.  Steady state (parser stable, all rows aligned) → the
    filter finds zero divergent rows and the UPDATE loop is skipped.

    Returns the number of rows re-derived (0 when everything is
    aligned)."""
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT [FileID], [SharePointPath], [Folder01] "
            f"FROM dbo.[{table}] "
            f"WHERE [SharePointPath] IS NOT NULL"
        )
        rows = cur.fetchall()

        divergent: list[tuple] = []
        for fid, path, persisted_f1 in rows:
            fresh = folder_columns_from_path(path)
            fresh_f1 = fresh[0] if fresh else None
            # Compare the first segment — cheapest signal a parser
            # change has occurred.  If they disagree, every other
            # level almost certainly disagrees too (each level is
            # shifted by one), so re-derive the whole 20-column
            # tuple.  If both are None the row is trivially aligned.
            if fresh_f1 != persisted_f1:
                divergent.append((*fresh, str(fid)))

        if not divergent:
            return 0

        set_clause = ", ".join(f"[{c}] = ?" for c in FOLDER_COLUMNS)
        cn.autocommit = False
        try:
            cur.fast_executemany = True
            cur.executemany(
                f"UPDATE dbo.[{table}] SET {set_clause} WHERE [FileID] = ?",
                divergent,
            )
            cn.commit()
            logger.info(
                "_rederive_stale_folder_rows: re-derived Folder01..20 on "
                "%d row(s) in dbo.%s (parser-version drift)",
                len(divergent), table,
            )
            return len(divergent)
        except Exception:
            cn.rollback()
            raise
        finally:
            cn.autocommit = True


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
    # Migration-platform integration fields.  These are the mapping/observation
    # values populated by the /api/migrate submit + /api/migrations/sync
    # poll paths.  All optional — a Pending row will have every field null.
    out["migrationRequestId"]     = _s(r.get("MigrationRequestId"))
    out["migrationFileItemId"]    = _s(r.get("MigrationFileItemId"))
    out["migrationSubmittedAt"]   = _fmt_dt(r.get("MigrationSubmittedAt"))
    out["migrationBackendStatus"] = _s(r.get("MigrationBackendStatus"))
    # retry_count is a plain int on the backend; return as int|null (not str)
    # so the frontend can format "Retry N of M" without re-parsing.
    _rc = r.get("MigrationRetryCount")
    out["migrationRetryCount"]    = int(_rc) if isinstance(_rc, (int,)) or (isinstance(_rc, str) and _rc.isdigit()) else None
    out["migrationErrorCode"]     = _s(r.get("MigrationErrorCode"))
    out["destinationUrl"]         = _s(r.get("DestinationUrl"))
    out["migrationLastSyncedAt"]  = _fmt_dt(r.get("MigrationLastSyncedAt"))
    # Audit-only field: who clicked Migrate for this row.  Exposed on
    # every row so the UI can render a "Submitted By" column globally
    # (rows remain visible to every logged-in user; this is display, not
    # filter).  Null for rows that have never been submitted.
    out["submittedBy"]            = _s(r.get("SubmittedBy"))

    # ── New-schema fields (ContractInventory1) ─────────────────────────
    # Surface every additional column so the UI can display and search
    # over them.  Integer columns pass through as int|null so the client
    # can format them (e.g. numeric sort on PageCount).
    _id = r.get("Id")
    out["id"]                     = int(_id) if isinstance(_id, int) or (isinstance(_id, str) and _id.isdigit()) else None
    out["folderName"]             = _s(r.get("FolderName"))
    out["folderType"]             = _s(r.get("FolderType"))
    out["batchID"]                = _s(r.get("BatchID"))
    _bn = r.get("BatchNumber")
    out["batchNumber"]            = int(_bn) if isinstance(_bn, int) or (isinstance(_bn, str) and _bn.isdigit()) else None
    _pc = r.get("PageCount")
    out["pageCount"]              = int(_pc) if isinstance(_pc, int) or (isinstance(_pc, str) and _pc.isdigit()) else None
    _tc = r.get("TextCharacters")
    out["textCharacters"]         = int(_tc) if isinstance(_tc, int) or (isinstance(_tc, str) and _tc.isdigit()) else None
    out["endCustomerName"]        = _s(r.get("EndCustomerName"))
    out["quoteID"]                = _s(r.get("QuoteID"))
    out["annualFeeIncrease"]      = _s(r.get("AnnualFeeIncrease"))
    out["feeIncreaseDate"]        = _s(r.get("FeeIncreaseDate"))
    # Permanent per-file sequential number.  Assigned once and never
    # renumbered — see _backfill_row_numbers_one_shot() +
    # _assign_row_numbers_to_new_rows().  Null only during the brief
    # window between an upstream INSERT and the next /api/contracts read
    # (the read itself will assign a number before returning).
    _rn = r.get("RowNumber")
    out["rowNumber"]              = int(_rn) if isinstance(_rn, int) or (isinstance(_rn, str) and str(_rn).isdigit()) else None
    return out


# ── Permanent per-file sequential number ("#") ────────────────────────────
# The [RowNumber] column carries a stable, monotonically increasing integer
# assigned exactly once per row and NEVER renumbered.  Two workers keep it
# populated:
#
#   _backfill_row_numbers_one_shot()      — process startup, guarded by
#                                            _row_number_backfill_done.
#                                            Numbers every currently-NULL
#                                            row in Id-ASC order starting
#                                            from MAX(RowNumber)+1 (or 1
#                                            on a brand-new column).
#
#   _assign_row_numbers_to_new_rows()     — every /api/contracts read.
#                                            Cheap COUNT(*) WHERE
#                                            RowNumber IS NULL first;
#                                            on match, allocates the
#                                            next N integers to those
#                                            rows in a SERIALIZABLE tx
#                                            so two concurrent readers
#                                            can't hand out duplicates.
#
# Both are idempotent and fail-soft (any exception is logged and the
# caller proceeds with whatever RowNumber values the row currently has
# — Null renders as an empty cell in the UI, which is acceptable for
# the brief window between an INSERT and the next read).
_row_number_backfill_done = False


def _backfill_row_numbers_one_shot() -> None:
    """One-shot per process: number every row that has RowNumber IS NULL
    in Id-ASC order, continuing from MAX(RowNumber) (or starting at 1
    if the column was just added).

    Uses ROW_NUMBER() in a single UPDATE ... FROM (SELECT ... FROM ... )
    so the whole backfill is one round-trip regardless of row count.
    """
    global _row_number_backfill_done
    if _row_number_backfill_done:
        return
    table = settings.CONTRACT_TABLE
    try:
        with get_connection() as cn:
            cn.autocommit = False
            cur = cn.cursor()
            # Cheap gate — if nothing is NULL, mark done and bail.
            cur.execute(
                f"SELECT COUNT(*) FROM dbo.[{table}] WHERE [RowNumber] IS NULL"
            )
            pending = int(cur.fetchone()[0] or 0)
            if pending == 0:
                cn.rollback()
                _row_number_backfill_done = True
                return

            # Continue from MAX; NULLs (from the new column) come out as 0
            # via ISNULL so the first assigned value is 1 on a fresh column.
            cur.execute(
                f"SELECT ISNULL(MAX([RowNumber]), 0) FROM dbo.[{table}]"
            )
            base = int(cur.fetchone()[0] or 0)

            # One UPDATE that assigns ROW_NUMBER() OVER (ORDER BY Id ASC)
            # to every NULL row, offset by `base`.  Uses a CTE so SQL Server
            # can update through the ranked source.
            cur.execute(
                f"WITH ranked AS ("
                f"    SELECT [RowNumber], "
                f"           ROW_NUMBER() OVER (ORDER BY [Id] ASC) AS rn "
                f"    FROM dbo.[{table}] "
                f"    WHERE [RowNumber] IS NULL"
                f") "
                f"UPDATE ranked SET [RowNumber] = rn + ?",
                [base],
            )
            assigned = cur.rowcount
            cn.commit()
            logger.info(
                "_backfill_row_numbers_one_shot: numbered %d row(s) in dbo.%s "
                "(starting at %d)",
                assigned, table, base + 1,
            )
        _row_number_backfill_done = True
    except Exception as exc:
        logger.warning(
            "_backfill_row_numbers_one_shot: failed (will retry next call): %s",
            exc,
        )


def _assign_row_numbers_to_new_rows() -> int:
    """Per-read worker: hand out sequential numbers to any rows the
    upstream ingest pipeline INSERTed since the last read.

    Cost in steady state: one indexed COUNT(*) that returns 0 → return.
    Only opens a transaction when there is real work to do.

    Concurrency: uses SERIALIZABLE so two concurrent /api/contracts
    reads can't both see the same NULL set and hand out overlapping
    numbers.  The set of NULL rows is small (dozens at most between
    reads), so the range-lock cost is negligible.

    Returns the number of rows numbered (0 in the common no-work case).
    """
    table = settings.CONTRACT_TABLE
    # Cheap gate — separate short connection so we don't hold locks on
    # the common no-work path.
    try:
        with get_connection() as cn:
            cur = cn.cursor()
            cur.execute(
                f"SELECT COUNT(*) FROM dbo.[{table}] WHERE [RowNumber] IS NULL"
            )
            if int(cur.fetchone()[0] or 0) == 0:
                return 0
    except Exception as exc:
        logger.warning("_assign_row_numbers_to_new_rows: gate check failed: %s", exc)
        return 0

    try:
        with get_connection() as cn:
            cn.autocommit = False
            cur = cn.cursor()
            cur.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            cur.execute(
                f"SELECT ISNULL(MAX([RowNumber]), 0) FROM dbo.[{table}]"
            )
            base = int(cur.fetchone()[0] or 0)
            cur.execute(
                f"WITH ranked AS ("
                f"    SELECT [RowNumber], "
                f"           ROW_NUMBER() OVER (ORDER BY [Id] ASC) AS rn "
                f"    FROM dbo.[{table}] "
                f"    WHERE [RowNumber] IS NULL"
                f") "
                f"UPDATE ranked SET [RowNumber] = rn + ?",
                [base],
            )
            assigned = cur.rowcount
            cn.commit()
            if assigned:
                logger.info(
                    "_assign_row_numbers_to_new_rows: numbered %d new row(s) "
                    "in dbo.%s (starting at %d)",
                    assigned, table, base + 1,
                )
            return int(assigned or 0)
    except Exception as exc:
        logger.warning(
            "_assign_row_numbers_to_new_rows: failed (proceeding without): %s",
            exc,
        )
        return 0


# ── Reads ──────────────────────────────────────────────────────────────────
def load_contracts(include_excluded: bool = False) -> List[dict]:
    """Return inventory rows from ContractInventory.

    Parameters
    ----------
    include_excluded : bool, default False
        False (default, used by the Manual Review UI):
            Return only rows currently NOT excluded (`Excluded='No'`).
            This is the review-view working set — Yet-to-be-Migrated +
            In Processing + Migrated + Failed all live here.
        True (used by the "Total Documents" CSV export):
            Return the ENTIRE master inventory, including excluded rows,
            so the export CSV can honour the manager's requirement that
            Total Documents represents the complete inventory.

    The `Excluded`, `ExcludedDate`, `ExcludedBy` fields are ALWAYS returned
    on every row (see `_row_to_dict`) so a caller receiving mixed rows can
    tell them apart without a second lookup.
    """
    _ensure_schema_once()

    # Live folder-column backfill: guarantees rows that landed in the DB
    # AFTER process start (e.g. a background ingest pipeline INSERTs new
    # SharePoint documents) get their Folder01..Folder20 populated before
    # the UI ever sees them.  Cheap in the steady state — a single indexed
    # COUNT that short-circuits to zero once the inventory is caught up.
    # Fails soft: any exception is logged and the read proceeds with
    # whatever folder values the row currently has.
    try:
        n_backfilled = backfill_folder_columns_if_needed()
        if n_backfilled:
            logger.info(
                "load_contracts: live-backfilled Folder01..20 on %d new row(s) in dbo.%s",
                n_backfilled, settings.CONTRACT_TABLE,
            )
    except Exception as exc:
        logger.warning("load_contracts: live folder-backfill failed (proceeding): %s", exc)

    # Live RowNumber assignment for rows the ingest pipeline INSERTed
    # after the last read.  Same fail-soft contract as the folder
    # backfill above: any exception is logged and the read proceeds
    # with whatever RowNumber values are currently persisted.  Also
    # retries the one-shot backfill if it failed earlier (idempotent).
    try:
        _backfill_row_numbers_one_shot()
        _assign_row_numbers_to_new_rows()
    except Exception as exc:
        logger.warning("load_contracts: row-number assignment failed (proceeding): %s", exc)

    cols = ", ".join(f"[{c}]" for c in _COLUMNS)
    # ExcludedDate/ExcludedBy are appended AFTER the fixed _COLUMNS list so
    # _row_to_dict receives its expected column count and the tail-offset
    # slicing below stays deterministic.
    tail = ", [Excluded], [ExcludedDate], [ExcludedBy]"
    where = "" if include_excluded else "WHERE ISNULL([Excluded], 'No') <> 'Yes' "
    sql = (f"SELECT {cols}{tail} "
           f"FROM dbo.[{settings.CONTRACT_TABLE}] "
           f"{where}"
           f"ORDER BY [FileName]")
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
    out = []
    n = len(_COLUMNS)
    for row in rows:
        d = _row_to_dict(row[:n])
        # Same keys the pre-refactor `load_excluded` used, so the client's
        # excluded-view rendering (which reads d.excludedDate / d.excludedBy)
        # keeps working unchanged.
        d["excluded"]     = "Yes" if (_s(row[n]) or "").lower() == "yes" else "No"
        d["excludedDate"] = _fmt_dt(row[n + 1])
        d["excludedBy"]   = _s(row[n + 2])
        out.append(d)
    logger.info(
        "load_contracts: %d rows from dbo.%s (include_excluded=%s)",
        len(out), settings.CONTRACT_TABLE, include_excluded,
    )
    return out


def load_excluded() -> List[dict]:
    """Return every currently-excluded row from ContractInventory,
    newest exclusion first.

    Post-refactor these rows live on the SAME master table as everything
    else — the difference is just `Excluded='Yes'`.  Kept as a separate
    function so the /api/excluded endpoint and its callers don't have to
    change; internally it's a simple filtered SELECT on the master.
    """
    _ensure_schema_once()
    active = settings.CONTRACT_TABLE
    cols = ", ".join(f"[{c}]" for c in _COLUMNS) + ", [ExcludedDate], [ExcludedBy]"
    sql = (f"SELECT {cols} "
           f"FROM dbo.[{active}] "
           f"WHERE ISNULL([Excluded], 'No') = 'Yes' "
           f"ORDER BY [ExcludedDate] DESC, [FileName]")
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
    out = []
    n = len(_COLUMNS)
    for row in rows:
        d = _row_to_dict(row[:n])
        d["excludedDate"] = _fmt_dt(row[n])
        d["excludedBy"]   = _s(row[n + 1])
        out.append(d)
    logger.info("load_excluded: %d rows (Excluded='Yes') from dbo.%s", len(out), active)
    return out


def count_all(user_email: str | None = None) -> dict:
    """Return per-bucket population counts from the unified master inventory.

    Response keys (always present):
        active         — rows with `Excluded='No'` (Manual Review working set)
        excluded       — rows with `Excluded='Yes'`
        total          — active + excluded (COMPLETE inventory)
        pending        — active rows with MigrationStatus = 'Pending'
        in_processing  — active rows with MigrationStatus = 'In Processing'
        migrated       — active rows with MigrationStatus = 'Migrated'
        failed         — active rows with MigrationStatus = 'Failed'

    When `user_email` is provided (non-empty), THREE additional keys are
    returned for the per-user workload-lock feature:
        my_in_processing   — how many of the OVERALL in_processing rows
                             belong to this user (SubmittedBy match,
                             case-insensitive)
        my_active_total    — same, across every ACTIVE_MIGRATION_STATUSES
                             value (currently the same as
                             my_in_processing but future-proofed for
                             adding Queued/Retrying)
        can_migrate        — bool; True iff my_active_total == 0.  UI
                             uses this to gate the Migrate button.

    IMPORTANT (unified-inventory refactor): `total` = active + excluded so
    the UI identity `Total = Yet-to-be-Migrated + In Processing + Migrated
    + Excluded` holds on the server as well as the client.  Per-status
    counts are computed over ACTIVE rows only so an excluded-but-Migrated
    row is counted once (in `excluded`) rather than double-counted.

    Per-user counts intentionally IGNORE the Excluded flag: if a row is
    still in an active migration status it counts against its submitter
    regardless of whether some concurrent action flagged it excluded
    (that shouldn't happen — mark_excluded refuses In Processing rows —
    but the count is a safety net not a correctness contract).
    """
    _ensure_schema_once()
    active = settings.CONTRACT_TABLE
    submitter = _normalise_email(user_email)
    status_list = ", ".join(f"'{s}'" for s in ACTIVE_MIGRATION_STATUSES)
    with get_connection() as cn:
        cur = cn.cursor()
        # Two COUNT(*) with a WHERE — cheap, one table scan each on
        # ContractInventory (~641 rows in current inventory).
        cur.execute(
            f"SELECT COUNT(*) FROM dbo.[{active}] "
            f"WHERE ISNULL([Excluded], 'No') <> 'Yes'"
        )
        a = cur.fetchone()[0] or 0
        cur.execute(
            f"SELECT COUNT(*) FROM dbo.[{active}] "
            f"WHERE ISNULL([Excluded], 'No') = 'Yes'"
        )
        e = cur.fetchone()[0] or 0
        # Per-status counts over ACTIVE rows only.  The `AND Excluded<>'Yes'`
        # guard is critical — without it, an excluded-but-Migrated row would
        # get counted in both `migrated` and `excluded`, breaking the
        # identity Total = Yet + In Processing + Migrated + Excluded.
        cur.execute(
            f"SELECT ISNULL([MigrationStatus], '{STATUS_PENDING}') AS s, COUNT(*) "
            f"FROM dbo.[{active}] "
            f"WHERE ISNULL([Excluded], 'No') <> 'Yes' "
            f"GROUP BY ISNULL([MigrationStatus], '{STATUS_PENDING}')"
        )
        by_status = {str(row[0] or STATUS_PENDING): int(row[1] or 0) for row in cur.fetchall()}

        # Optional per-user slice — one extra seek on the filtered index
        # IX_{table}_SubmittedBy_Active, no impact on the anonymous path.
        my_in_processing = 0
        my_active_total  = 0
        if submitter:
            cur.execute(
                f"SELECT "
                f"  SUM(CASE WHEN [MigrationStatus] = ? THEN 1 ELSE 0 END), "
                f"  SUM(CASE WHEN [MigrationStatus] IN ({status_list}) THEN 1 ELSE 0 END) "
                f"FROM dbo.[{active}] "
                f"WHERE LOWER(ISNULL([SubmittedBy], '')) = ?",
                [STATUS_IN_PROCESSING, submitter],
            )
            r = cur.fetchone()
            my_in_processing = int((r[0] if r else 0) or 0)
            my_active_total  = int((r[1] if r else 0) or 0)

    # Terminal-failure bucket count.  Historically MigrationStatus stored
    # "Failed" only; the rename to "Error" (§Required top buckets) means
    # both spellings may co-exist during rollout.  Sum both so the count
    # is stable regardless of which spelling any given row carries.
    error_count = sum(by_status.get(s, 0) for s in _ERROR_MIGRATION_STATUSES)

    out = {
        "active":        int(a),
        "excluded":      int(e),
        # Total = complete inventory (active + excluded).  Manager spec:
        # "Total Documents must include Excluded".  Error rows are already
        # inside `active` (they carry MigrationStatus='Error' with
        # Excluded='No'), so the identity
        #   total = pending + in_processing + migrated + error + excluded
        # holds without any additional term.
        "total":         int(a) + int(e),
        "pending":       by_status.get(STATUS_PENDING, 0),
        "in_processing": by_status.get(STATUS_IN_PROCESSING, 0),
        "migrated":      by_status.get(STATUS_MIGRATED, 0),
        # NEW canonical bucket for the "Error" tile.
        "error":         error_count,
        # Legacy alias kept for backward compat with any older client build
        # that still reads `pc.failed` (e.g. cached static/js/app.js).  The
        # server-side value is identical; new code should read `error`.
        "failed":        error_count,
    }
    if submitter:
        out["my_in_processing"] = my_in_processing
        out["my_active_total"]  = my_active_total
        out["can_migrate"]      = (my_active_total == 0)
    return out


# ── Per-user workload-lock helpers ────────────────────────────────────────
def _normalise_email(email: str | None) -> str:
    """Canonicalise an email for storage/comparison.

    Session cookies always carry the exact-cased login email, but users
    can log in with either case ("USERA@x.com" vs "usera@x.com").  We
    store and compare in lower-case so a same-user race across two tabs
    that happen to have different casing still resolves to one owner.
    Empty / None → empty string (callers treat as "no user identity").
    """
    return (email or "").strip().lower()


def get_user_active_migration_count(email: str) -> int:
    """Return how many inventory rows this email currently owns in an
    ACTIVE migration status (Pending-with-request-id / In Processing /
    Retrying).  Zero for unknown users or rows never submitted by this
    user.  Case-insensitive on the stored SubmittedBy column.

    Cheap point-lookup: hits IX_{table}_SubmittedBy_Active (filtered
    index on the same status set) — a seek regardless of table size.
    """
    _ensure_schema_once()
    e = _normalise_email(email)
    if not e:
        return 0
    table = settings.CONTRACT_TABLE
    status_list = ", ".join(f"'{s}'" for s in ACTIVE_MIGRATION_STATUSES)
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT COUNT(*) FROM dbo.[{table}] "
            f"WHERE LOWER(ISNULL([SubmittedBy], '')) = ? "
            f"  AND [MigrationStatus] IN ({status_list})",
            [e],
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def can_user_start_migration(email: str) -> tuple[bool, int]:
    """Convenience wrapper around get_user_active_migration_count().

    Returns (allowed, active_count).  `allowed` is True iff the caller
    currently has zero active rows.  The tuple form lets callers avoid
    a second query when they need the count for the response body.
    """
    n = get_user_active_migration_count(email)
    return (n == 0, n)


# ── Writes ─────────────────────────────────────────────────────────────────
def mark_in_processing(file_ids: Iterable[str],
                       submitted_by: str | None = None) -> dict:
    """Transition eligible rows from 'Pending' → 'In Processing' AND stamp
    the submitting user on each row (SubmittedBy).

    Called immediately after the user confirms the Migrate modal, BEFORE
    the Power Automate call.  Persisting this state first ensures:
      - the Pending bucket count drops right away (UI feels responsive),
      - if the Power Automate call crashes or times out we can still see
        which files were in flight,
      - Migrate=Yes and MigratedDate are NOT touched (business rule 4).

    Per-user workload lock (task 2026-09-24)
    ────────────────────────────────────────
    When `submitted_by` is provided (any non-empty email), a single
    SQL transaction performs, in order:

        1. SELECT the caller's current active-status row count with a
           HOLDLOCK / UPDLOCK hint so a concurrent second submission for
           the SAME email is serialised behind us (Azure SQL locks the
           key range under snapshot-isolation-default databases too).
        2. If the count is > 0 → raise ActiveMigrationExistsError and
           roll back.  Nothing on the row set changes.
        3. Otherwise UPDATE the eligible rows to In Processing + stamp
           SubmittedBy in ONE statement (same transaction) so no
           interleaved reader can see rows flipped to In Processing
           without their owner set.

    When `submitted_by` is None / empty (legacy callers), steps 1-2 are
    skipped and step 3 still runs but SubmittedBy is left NULL.  This
    keeps existing tests / call sites that don't care about ownership
    working.

    Only rows currently in 'Pending' or 'Failed' are transitioned —
    already-processing or already-migrated rows are silently skipped
    (business rule 15).  Excluded='Yes' rows are also skipped
    (unified-inventory refactor guard).  Returns which FileIDs actually
    flipped so the caller can send exactly those to the platform.
    """
    _ensure_schema_once()
    ids = [str(i).strip() for i in file_ids if str(i).strip()]
    if not ids:
        return {"succeeded": [], "skipped": [], "rowsAffected": 0}

    submitter = _normalise_email(submitted_by)
    table = settings.CONTRACT_TABLE
    placeholders = ", ".join("?" for _ in ids)
    status_list = ", ".join(f"'{s}'" for s in ACTIVE_MIGRATION_STATUSES)

    with get_connection() as cn:
        cn.autocommit = False
        cur = cn.cursor()
        try:
            # Step 1 — atomic per-user lock check.  Skipped when caller
            # didn't identify itself (back-compat with tests / scripts).
            #
            # UPDLOCK + HOLDLOCK together take a U-lock on any matching
            # rows and hold it to the end of the transaction, which under
            # both READ COMMITTED and SNAPSHOT isolation blocks another
            # concurrent transaction that tries to take the same U-lock
            # for the same email.  Two /api/migrate calls from the same
            # user racing across two tabs therefore serialise: the second
            # one sees the first one's rows already flipped and raises.
            if submitter:
                cur.execute(
                    f"SELECT COUNT(*) FROM dbo.[{table}] "
                    f"WITH (UPDLOCK, HOLDLOCK) "
                    f"WHERE LOWER(ISNULL([SubmittedBy], '')) = ? "
                    f"  AND [MigrationStatus] IN ({status_list})",
                    [submitter],
                )
                active_count = int(cur.fetchone()[0] or 0)
                if active_count > 0:
                    cn.rollback()
                    raise ActiveMigrationExistsError(submitter, active_count)

            # Step 2 — flip eligible rows AND stamp SubmittedBy in the
            # same UPDATE so ownership is atomic with the status change.
            # A NULL submitter parameter leaves the existing SubmittedBy
            # value in place via COALESCE (relevant when a legacy call
            # site retries a Failed row that was previously stamped by
            # its original submitter — we keep the original owner).
            # Accept both 'Error' (canonical, post-rename) and 'Failed'
            # (legacy on-disk value) so /api/migrations/retry works
            # against rows that were written before the rename deploy.
            cur.execute(
                f"UPDATE dbo.[{table}] "
                f"SET   [MigrationStatus] = ?, "
                f"      [SubmittedBy]     = COALESCE(?, [SubmittedBy]) "
                f"WHERE [FileID] IN ({placeholders}) "
                f"  AND ISNULL([MigrationStatus], '{STATUS_PENDING}') IN "
                f"       ('{STATUS_PENDING}', '{STATUS_ERROR}', 'Failed') "
                f"  AND ISNULL([Migrate], 'No') <> 'Yes' "
                f"  AND ISNULL([Excluded], 'No') <> 'Yes'",
                [STATUS_IN_PROCESSING,
                 (submitter or None),
                 *ids],
            )
            affected = cur.rowcount

            # Step 3 — capture which IDs actually made it into In Processing.
            cur.execute(
                f"SELECT [FileID] FROM dbo.[{table}] "
                f"WHERE [FileID] IN ({placeholders}) "
                f"  AND [MigrationStatus] = ?",
                [*ids, STATUS_IN_PROCESSING],
            )
            succeeded = [str(r[0]) for r in cur.fetchall()]
            cn.commit()
        except ActiveMigrationExistsError:
            # Roll back already performed above; re-raise unchanged so
            # /api/migrate can translate to a 409.
            raise
        except Exception:
            cn.rollback()
            raise
        finally:
            cn.autocommit = True

    skipped = [i for i in ids if i not in set(succeeded)]
    logger.info(
        "mark_in_processing: requested=%d affected=%d skipped=%d "
        "submitted_by=%s table=dbo.%s",
        len(ids), affected, len(skipped), submitter or "-", table,
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
        MigrationStatus = 'Error'    (canonical spelling post-rename;
                                      legacy 'Failed' still accepted on read)
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


def classify_retry_eligibility(file_ids: Iterable[str]) -> dict:
    """Given a set of requested FileIDs, split them into three lists based
    on the current MigrationStatus on the master inventory.

    Used by /api/migrations/retry BEFORE calling record_retry_history or
    mark_in_processing so the endpoint can reject the entire request with
    400 if the caller included any FileID that is not currently in Error.

    Returns:
        {
          "eligible":    [FileID, ...],   # MigrationStatus IN ('Error','Failed')
          "ineligible":  [{"fileID": "...", "status": "..."}, ...],
                                          # in an unretriable status (Pending /
                                          # In Processing / Migrated) — the
                                          # request should be rejected 400
          "unknown":     [FileID, ...],   # no row exists (or row is Excluded)
        }

    Excluded rows are treated as `unknown` — you can't retry a row that
    isn't in the active inventory.  The three lists are disjoint and
    together cover every input id (deduped, whitespace-trimmed).
    """
    _ensure_schema_once()
    ids = sorted({str(i).strip() for i in file_ids if str(i).strip()})
    if not ids:
        return {"eligible": [], "ineligible": [], "unknown": []}

    table = settings.CONTRACT_TABLE
    ph = ", ".join("?" for _ in ids)
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT [FileID], ISNULL([MigrationStatus], '{STATUS_PENDING}') "
            f"FROM   dbo.[{table}] "
            f"WHERE  [FileID] IN ({ph}) "
            f"  AND  ISNULL([Excluded], 'No') <> 'Yes'",
            ids,
        )
        found: dict[str, str] = {}
        for r in cur.fetchall():
            found[str(r[0])] = str(r[1])

    eligible:   list[str] = []
    ineligible: list[dict] = []
    unknown:    list[str] = []
    for fid in ids:
        st = found.get(fid)
        if st is None:
            unknown.append(fid)
        elif st in _ERROR_MIGRATION_STATUSES:
            eligible.append(fid)
        else:
            ineligible.append({"fileID": fid, "status": st})
    return {"eligible": eligible, "ineligible": ineligible, "unknown": unknown}


def get_rows_for_submission(file_ids: Iterable[str]) -> List[dict]:
    """Return full inventory rows for the given FileIDs — but ONLY those
    currently 'In Processing' after mark_in_processing() has flipped them.

    Includes the persisted folder columns and (importantly for the
    migration platform integration) the raw SharePointPath so the caller
    can derive site_url / library / source_path via
    services.migration_paths.parse_sharepoint_url.

    This is the input to MigrationPlatformService.group_files_for_submission.
    """
    _ensure_schema_once()
    ids = [str(i).strip() for i in file_ids if str(i).strip()]
    if not ids:
        return []
    table = settings.CONTRACT_TABLE
    ph = ", ".join("?" for _ in ids)
    cols = ", ".join(f"[{c}]" for c in _COLUMNS)
    sql = (
        f"SELECT {cols} FROM dbo.[{table}] "
        f"WHERE [FileID] IN ({ph}) "
        f"  AND [MigrationStatus] = '{STATUS_IN_PROCESSING}'"
    )
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(sql, ids)
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def record_submission(
    file_ids: Iterable[str],
    *,
    migration_request_id: str,
    file_item_id_by_local_id: dict[str, str] | None = None,
) -> dict:
    """Persist the migration-platform reference IDs on each submitted row.

    Called AFTER MigrationPlatformService.create_migration + add_files_batch
    succeeds.  Rows keep their MigrationStatus='In Processing' — the
    backend has now accepted them and the poller will observe transitions.

    Parameters
    ----------
    file_ids
        Local inventory FileIDs that were successfully submitted.
    migration_request_id
        The UUID returned by POST /api/v1/migrations.
    file_item_id_by_local_id
        Optional mapping local FileID → platform file_item UUID as
        returned by the batch-add response.  Rows not present are still
        marked with the migration_request_id so a later /sync can look
        them up by request+file_name.

    Returns { rowsAffected, submittedAt }.
    """
    _ensure_schema_once()
    ids = [str(i).strip() for i in file_ids if str(i).strip()]
    if not ids:
        return {"rowsAffected": 0, "submittedAt": None}

    now = datetime.utcnow()
    table = settings.CONTRACT_TABLE
    mapping = file_item_id_by_local_id or {}

    with get_connection() as cn:
        cn.autocommit = False
        cur = cn.cursor()
        try:
            # Two UPDATEs: one path when we know the file_item_id, one path
            # when we don't (fallback — the sync poll will fill it in).
            with_ids    = [fid for fid in ids if mapping.get(fid)]
            without_ids = [fid for fid in ids if not mapping.get(fid)]

            if with_ids:
                # executemany — one round-trip per row, but the batch is
                # small (<= user's page selection).
                cur.fast_executemany = True
                cur.executemany(
                    f"UPDATE dbo.[{table}] "
                    f"SET [MigrationRequestId]    = ?, "
                    f"    [MigrationFileItemId]   = ?, "
                    f"    [MigrationSubmittedAt]  = ?, "
                    f"    [MigrationBackendStatus]= ?, "
                    f"    [MigrationLastSyncedAt] = ?, "
                    f"    [RunId]                 = ? "
                    f"WHERE [FileID] = ? "
                    f"  AND [MigrationStatus] = '{STATUS_IN_PROCESSING}'",
                    [
                        (migration_request_id, mapping[fid], now, "pending",
                         now, migration_request_id, fid)
                        for fid in with_ids
                    ],
                )

            if without_ids:
                ph = ", ".join("?" for _ in without_ids)
                cur.execute(
                    f"UPDATE dbo.[{table}] "
                    f"SET [MigrationRequestId]    = ?, "
                    f"    [MigrationSubmittedAt]  = ?, "
                    f"    [MigrationBackendStatus]= ?, "
                    f"    [MigrationLastSyncedAt] = ?, "
                    f"    [RunId]                 = ? "
                    f"WHERE [FileID] IN ({ph}) "
                    f"  AND [MigrationStatus] = '{STATUS_IN_PROCESSING}'",
                    [migration_request_id, now, "pending", now,
                     migration_request_id, *without_ids],
                )
            cn.commit()
        except Exception:
            cn.rollback()
            raise
        finally:
            cn.autocommit = True

    logger.info(
        "record_submission: migration_id=%s rows=%d mapped=%d",
        migration_request_id, len(ids), len(mapping),
    )
    return {"rowsAffected": len(ids), "submittedAt": _fmt_dt(now)}


def record_retry_history(
    file_ids: Iterable[str],
    *,
    retried_by: str,
    session_id: str | None = None,
) -> list[dict]:
    """Snapshot the failure evidence for a set of rows about to be retried.

    Called from the /api/migrations/retry route BEFORE mark_in_processing
    flips the rows out of Error — the flip overwrites the existing
    ErrorMessage / MigrationRequestId / MigrationFileItemId columns and
    increments MigrationRetryCount, so we must capture the "previous"
    values first for the audit trail.

    One row is inserted into ContractMigrationRetryHistory per FileID.
    ``new_migration_request_id`` is written later by
    ``stamp_retry_history_new_request_id`` once the platform accepts the
    resubmission and returns the fresh migration UUID.

    Rows whose FileID is not currently in Error/Failed are skipped
    (defensive; the caller already validated eligibility server-side).

    Parameters
    ----------
    file_ids
        Local ContractInventory FileIDs the user requested to retry.
    retried_by
        Session email of the user who clicked Retry.  Required — nothing
        writes to this table without an owner.
    session_id
        Optional session id for finer-grained audit correlation.

    Returns a list of dicts, one per snapshot row, each shaped:
        {
          "fileID":                      "337,514",
          "previousMigrationRequestId":  "<uuid>" | "",
          "previousRetryCount":          0,
          "retryHistoryId":              123    # BIGINT PK of the new row
        }
    """
    _ensure_schema_once()
    email = _normalise_email(retried_by)
    if not email:
        raise ValueError("record_retry_history requires retried_by")
    ids = [str(i).strip() for i in file_ids if str(i).strip()]
    if not ids:
        return []

    table   = settings.CONTRACT_TABLE
    hist    = retry_history_table_name()
    placeholders = ", ".join("?" for _ in ids)

    out: list[dict] = []
    with get_connection() as cn:
        cn.autocommit = False
        cur = cn.cursor()
        try:
            # Snapshot in one round-trip — pull the exact columns we
            # need to persist.  Accept both 'Error' and 'Failed' so the
            # audit works during the rollout window before every row
            # has been rewritten to the new spelling.
            cur.execute(
                f"SELECT [FileID], [FileName], [MigrationRequestId], "
                f"       [MigrationFileItemId], [ErrorMessage], "
                f"       ISNULL([MigrationRetryCount], 0) "
                f"FROM   dbo.[{table}] "
                f"WHERE  [FileID] IN ({placeholders}) "
                f"  AND  [MigrationStatus] IN ('{STATUS_ERROR}', 'Failed')",
                ids,
            )
            snapshots = cur.fetchall()

            for r in snapshots:
                fid       = str(r[0]) if r[0] is not None else ""
                fname     = str(r[1]) if r[1] is not None else None
                prev_mid  = str(r[2]) if r[2] is not None else None
                prev_fiid = str(r[3]) if r[3] is not None else None
                prev_err  = str(r[4]) if r[4] is not None else None
                prev_rc   = int(r[5] or 0)
                cur.execute(
                    f"INSERT INTO dbo.[{hist}] "
                    f"  (file_id, file_name, "
                    f"   previous_migration_request_id, "
                    f"   previous_migration_file_item_id, "
                    f"   previous_error_message, "
                    f"   previous_retry_count, "
                    f"   retried_by, session_id) "
                    f"OUTPUT INSERTED.id "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [fid, fname, prev_mid, prev_fiid, prev_err,
                     prev_rc, email, session_id],
                )
                new_id = int(cur.fetchone()[0])
                out.append({
                    "fileID":                     fid,
                    "previousMigrationRequestId": prev_mid or "",
                    "previousRetryCount":         prev_rc,
                    "retryHistoryId":             new_id,
                })
            cn.commit()
        except Exception:
            cn.rollback()
            raise
        finally:
            cn.autocommit = True

    logger.info(
        "record_retry_history: retried_by=%s snapshots=%d requested=%d",
        email, len(out), len(ids),
    )
    return out


def stamp_retry_history_new_request_id(
    retry_history_ids: Iterable[int],
    *,
    new_migration_request_id: str,
) -> int:
    """Fill in ``new_migration_request_id`` on retry-history rows.

    Called from /api/migrations/retry AFTER the platform's create+add
    calls succeed and record_submission has stamped the new
    MigrationRequestId onto the master inventory rows.  Closes the
    audit loop: "row was retried by X at T; here's the previous batch
    that failed; here's the new batch we submitted".

    A retry-history row without a new_migration_request_id represents a
    retry attempt whose platform submission itself failed — the master
    inventory row will already have been rolled back to Error by the
    normal /api/migrate error-handling path.
    """
    hist_ids = [int(i) for i in retry_history_ids if i]
    if not hist_ids:
        return 0
    hist = retry_history_table_name()
    placeholders = ", ".join("?" for _ in hist_ids)
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"UPDATE dbo.[{hist}] "
            f"SET   new_migration_request_id = ? "
            f"WHERE id IN ({placeholders}) "
            f"  AND new_migration_request_id IS NULL",
            [new_migration_request_id, *hist_ids],
        )
        cn.commit()
        return int(cur.rowcount or 0)


def apply_backend_file_status(
    updates: list[dict],
) -> dict:
    """Persist the outcome of a single /api/migrations/sync poll cycle.

    Each ``update`` is a plain dict:
        {
          "fileID":           "337,514",                 required
          "backendStatus":    "completed" | "failed" | ...,   required
          "uiStatus":         "Migrated" | "In Processing" | "Failed" | "Skipped",
          "migrationFileItemId": "<uuid>" | None,
          "retryCount":       int | None,
          "errorMessage":     str  | None,
          "errorCode":        str  | None,
          "destinationUrl":   str  | None,
        }

    Only rows CURRENTLY in an active state (In Processing or Failed) are
    touched — this protects against late/out-of-order polls trying to
    downgrade a Migrated row.  ``Migrated → Migrated`` and repeated
    ``Failed → Failed`` writes are idempotent.

    Returns per-bucket counts of rows actually updated.
    """
    _ensure_schema_once()
    if not updates:
        return {"migrated": 0, "failed": 0, "skipped": 0,
                "in_processing": 0, "rowsAffected": 0}

    now = datetime.utcnow()
    table = settings.CONTRACT_TABLE
    migrated = failed = skipped = ip = 0

    with get_connection() as cn:
        cn.autocommit = False
        cur = cn.cursor()
        try:
            for u in updates:
                fid = str(u.get("fileID") or "").strip()
                ui  = (u.get("uiStatus") or "").strip()
                if not fid or ui not in (
                    STATUS_IN_PROCESSING, STATUS_MIGRATED, STATUS_FAILED, "Skipped",
                ):
                    continue

                backend_status = u.get("backendStatus")
                file_item_id   = u.get("migrationFileItemId")
                retry_count    = u.get("retryCount")
                error_message  = u.get("errorMessage")
                error_code     = u.get("errorCode")
                destination_url = u.get("destinationUrl")

                # Terminal states set Migrate/Migrated/MigratedDate; non-
                # terminal states only refresh backend observation columns.
                if ui == STATUS_MIGRATED:
                    cur.execute(
                        f"UPDATE dbo.[{table}] "
                        f"SET [MigrationStatus]         = '{STATUS_MIGRATED}', "
                        f"    [Migrate]                 = 'Yes', "
                        f"    [Migrated]                = 'True', "
                        f"    [MigratedDate]            = ?, "
                        f"    [MigrationBackendStatus]  = ?, "
                        f"    [MigrationFileItemId]     = COALESCE(?, [MigrationFileItemId]), "
                        f"    [MigrationRetryCount]     = ?, "
                        f"    [MigrationErrorCode]      = NULL, "
                        f"    [ErrorMessage]            = NULL, "
                        f"    [DestinationUrl]          = COALESCE(?, [DestinationUrl]), "
                        f"    [MigrationLastSyncedAt]   = ? "
                        f"WHERE [FileID] = ? "
            f"  AND [MigrationStatus] IN ('{STATUS_IN_PROCESSING}', '{STATUS_ERROR}', 'Failed')",
                        [now, backend_status, file_item_id, retry_count,
                         destination_url, now, fid],
                    )
                    if cur.rowcount:
                        migrated += cur.rowcount

                elif ui == STATUS_ERROR:
                    # Terminal failure — write "Error" (the new spelling).
                    # WHERE accepts both spellings so a mid-rollout row
                    # already at 'Failed' can transition idempotently to
                    # 'Error' with fresh observation columns.
                    cur.execute(
                        f"UPDATE dbo.[{table}] "
                        f"SET [MigrationStatus]         = '{STATUS_ERROR}', "
                        f"    [MigrationBackendStatus]  = ?, "
                        f"    [MigrationFileItemId]     = COALESCE(?, [MigrationFileItemId]), "
                        f"    [MigrationRetryCount]     = ?, "
                        f"    [MigrationErrorCode]      = ?, "
                        f"    [ErrorMessage]            = ?, "
                        f"    [MigrationLastSyncedAt]   = ? "
                        f"WHERE [FileID] = ? "
                        f"  AND [MigrationStatus] IN ('{STATUS_IN_PROCESSING}', 'Error', 'Failed')",
                        [backend_status, file_item_id, retry_count,
                         error_code, error_message, now, fid],
                    )
                    if cur.rowcount:
                        failed += cur.rowcount

                elif ui == "Skipped":
                    # Backend 'skipped' — treat as a distinct terminal state
                    # (NOT the same as business Excluded per §13).  We fold
                    # into Error for the bucket count so the UI identity
                    # A = B+C+D+E+Error still holds; ErrorMessage explains why.
                    cur.execute(
                        f"UPDATE dbo.[{table}] "
                        f"SET [MigrationStatus]         = '{STATUS_ERROR}', "
                        f"    [MigrationBackendStatus]  = ?, "
                        f"    [MigrationFileItemId]     = COALESCE(?, [MigrationFileItemId]), "
                        f"    [MigrationErrorCode]      = COALESCE(?, 'skipped'), "
                        f"    [ErrorMessage]            = ?, "
                        f"    [MigrationLastSyncedAt]   = ? "
                        f"WHERE [FileID] = ? "
                        f"  AND [MigrationStatus] IN ('{STATUS_IN_PROCESSING}', 'Error', 'Failed')",
                        [backend_status, file_item_id, error_code,
                         error_message or "Skipped by migration backend",
                         now, fid],
                    )
                    if cur.rowcount:
                        skipped += cur.rowcount

                else:  # In Processing — refresh observation columns only
                    cur.execute(
                        f"UPDATE dbo.[{table}] "
                        f"SET [MigrationBackendStatus]  = ?, "
                        f"    [MigrationFileItemId]     = COALESCE(?, [MigrationFileItemId]), "
                        f"    [MigrationRetryCount]     = ?, "
                        f"    [MigrationErrorCode]      = ?, "
                        f"    [ErrorMessage]            = ?, "
                        f"    [MigrationLastSyncedAt]   = ? "
                        f"WHERE [FileID] = ? "
                        f"  AND [MigrationStatus] IN ('{STATUS_IN_PROCESSING}', 'Error', 'Failed')",
                        [backend_status, file_item_id, retry_count,
                         error_code, error_message, now, fid],
                    )
                    if cur.rowcount:
                        ip += cur.rowcount

            cn.commit()
        except Exception:
            cn.rollback()
            raise
        finally:
            cn.autocommit = True

    total = migrated + failed + skipped + ip
    logger.info(
        "apply_backend_file_status: migrated=%d failed=%d skipped=%d in_processing=%d total=%d",
        migrated, failed, skipped, ip, total,
    )
    return {
        "migrated":      migrated,
        "failed":        failed,
        "skipped":       skipped,
        "in_processing": ip,
        "rowsAffected":  total,
    }


def build_match_key_index() -> dict:
    """Build a snapshot ``{match_key -> [FileID, …]}`` from ContractInventory.

    Used by the reconciliation pass to resolve a PostgreSQL file_items
    row (which carries site/library/source_path) to a local FileID.

    Notes
    -----
    * Only rows with a non-empty SharePointPath are indexed — a row
      without a SharePoint URL cannot possibly be a candidate.
    * Rows already ``Excluded='Yes'`` are still indexed: exclusion is a
      business decision and the reconciler must never resurrect them,
      but excluding them from the index would break the ambiguity check
      (two rows sharing a key, one excluded, would look unambiguous
      when they aren't).  The writer's own WHERE clause protects the
      excluded row from being modified — see reconcile_external_file_status().
    * Value is a LIST — a canonical key may legitimately map to multiple
      inventory rows if the same physical file appears in the inventory
      more than once.  The reconciler treats len>1 as ambiguous and
      declines to update rather than guessing.
    """
    _ensure_schema_once()
    # Local import to avoid pulling migration_paths at module import time
    # (keeps the data_service <-> migration_paths edge one-directional).
    from services.migration_paths import match_key_from_sharepoint_url

    table = settings.CONTRACT_TABLE
    index: dict[tuple[str, str, str], list[str]] = {}
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT [FileID], [SharePointPath] FROM dbo.[{table}] "
            f"WHERE [SharePointPath] IS NOT NULL AND LEN([SharePointPath]) > 0"
        )
        for fid, sp in cur.fetchall():
            key = match_key_from_sharepoint_url(sp)
            if key is None:
                continue
            index.setdefault(key, []).append(str(fid))
    return index


def reconcile_external_file_status(
    updates: list[dict],
) -> dict:
    """Reflect externally-triggered migration outcomes into ContractInventory.

    Sibling of :func:`apply_backend_file_status`.  Same status semantics
    (map completed→Migrated, failed→Failed, skipped→Failed with skipped
    tag, active→In Processing) BUT the ``WHERE`` guard is relaxed to
    allow the ``Pending → target`` transition — the whole point of this
    helper is to reconcile files that were migrated by another tool and
    therefore never went through our /api/migrate endpoint (they were
    never marked In Processing here).

    Idempotency & no-downgrade rules (spec §Do not overwrite valid newer
    states):

      * ``Migrated`` rows are NEVER touched — a completed row does not
        get demoted back to In Processing because of an older PostgreSQL
        row that arrived out of order.
      * ``Excluded='Yes'`` rows are NEVER touched — exclusion is a
        business decision independent of migration state.
      * Repeated reconciliation is safe: rows that already match the
        target state are updated with fresh MigrationLastSyncedAt only
        (via cur.rowcount == 0 → we don't bump the "changed" counters).

    Every ``update`` dict:
        {
          "fileID":              "337,514",              required
          "uiStatus":            "Migrated" | "In Processing" | "Failed" | "Skipped",
          "backendStatus":       "completed" | ...,     required
          "migrationRequestId":  "<uuid>" | None,
          "migrationFileItemId": "<uuid>" | None,
          "submittedBy":         str | None,      # from migration_requests.created_by
          "migratedDate":        datetime | None, # from file_items.completed_at
          "destinationUrl":      str | None,      # from audit_logs metadata
          "retryCount":          int | None,
          "errorMessage":        str | None,
          "errorCode":           str | None,
        }

    Returns::
        {"migrated": int, "failed": int, "skipped": int,
         "in_processing": int, "rowsAffected": int}

    where each count is rows that ACTUALLY transitioned (rowcount>0 with
    a state change).  Rows that were already at the target state are not
    counted — that keeps the sync response's ``reconciled`` bucket
    meaningful ("new externally-discovered updates this tick").
    """
    _ensure_schema_once()
    if not updates:
        return {"migrated": 0, "failed": 0, "skipped": 0,
                "in_processing": 0, "rowsAffected": 0}

    now = datetime.utcnow()
    table = settings.CONTRACT_TABLE
    migrated = failed = skipped = ip = 0

    # A single reconciliation tick may target hundreds of rows; do them
    # all inside ONE transaction (one commit at the end) so partial
    # updates never leak.  Matches the apply_backend_file_status pattern.
    with get_connection() as cn:
        cn.autocommit = False
        cur = cn.cursor()
        try:
            for u in updates:
                fid = str(u.get("fileID") or "").strip()
                ui  = (u.get("uiStatus") or "").strip()
                if not fid or ui not in (
                    STATUS_IN_PROCESSING, STATUS_MIGRATED, STATUS_FAILED, "Skipped",
                ):
                    continue

                backend_status  = u.get("backendStatus")
                mig_req_id      = u.get("migrationRequestId")
                file_item_id    = u.get("migrationFileItemId")
                submitted_by    = _normalise_email(u.get("submittedBy") or "") or None
                migrated_date   = u.get("migratedDate")
                destination_url = u.get("destinationUrl")
                retry_count     = u.get("retryCount")
                error_message   = u.get("errorMessage")
                error_code      = u.get("errorCode")

                # Common guard for ALL branches: never touch Migrated
                # (no downgrade) and never touch Excluded='Yes' rows
                # (business decision, orthogonal to migration state).
                # The state whitelist explicitly INCLUDES 'Pending'
                # because external discovery is designed to promote
                # Pending rows into their real state.  Both 'Error' (new)
                # and 'Failed' (legacy) are accepted so the transition
                # completes even on mid-rollout rows.
                allowed_from = (
                    f"[MigrationStatus] IN "
                    f"  ('Pending','{STATUS_IN_PROCESSING}','Error','Failed') "
                    f"AND ISNULL([Excluded],'No') <> 'Yes'"
                )

                if ui == STATUS_MIGRATED:
                    # Terminal success — set every terminal-state column.
                    # MigratedDate prefers the platform's completed_at
                    # (spec §Status mapping: MigratedDate = completed_at)
                    # and falls back to "now" when it's NULL.
                    mig_dt = migrated_date or now
                    cur.execute(
                        f"UPDATE dbo.[{table}] "
                        f"SET [MigrationStatus]         = '{STATUS_MIGRATED}', "
                        f"    [Migrate]                 = 'Yes', "
                        f"    [Migrated]                = 'True', "
                        f"    [MigratedDate]            = ?, "
                        f"    [MigrationBackendStatus]  = ?, "
                        f"    [MigrationRequestId]      = COALESCE(?, [MigrationRequestId]), "
                        f"    [MigrationFileItemId]     = COALESCE(?, [MigrationFileItemId]), "
                        f"    [MigrationRetryCount]     = ?, "
                        f"    [MigrationErrorCode]      = NULL, "
                        f"    [ErrorMessage]            = NULL, "
                        f"    [DestinationUrl]          = COALESCE(?, [DestinationUrl]), "
                        f"    [SubmittedBy]             = COALESCE(?, [SubmittedBy]), "
                        f"    [MigrationLastSyncedAt]   = ? "
                        f"WHERE [FileID] = ? AND {allowed_from} "
                        f"  AND [MigrationStatus] <> '{STATUS_MIGRATED}'",
                        [mig_dt, backend_status, mig_req_id, file_item_id,
                         retry_count, destination_url, submitted_by, now, fid],
                    )
                    if cur.rowcount:
                        migrated += cur.rowcount

                elif ui == STATUS_ERROR:
                    cur.execute(
                        f"UPDATE dbo.[{table}] "
                        f"SET [MigrationStatus]         = '{STATUS_ERROR}', "
                        f"    [MigrationBackendStatus]  = ?, "
                        f"    [MigrationRequestId]      = COALESCE(?, [MigrationRequestId]), "
                        f"    [MigrationFileItemId]     = COALESCE(?, [MigrationFileItemId]), "
                        f"    [MigrationRetryCount]     = ?, "
                        f"    [MigrationErrorCode]      = ?, "
                        f"    [ErrorMessage]            = ?, "
                        f"    [SubmittedBy]             = COALESCE(?, [SubmittedBy]), "
                        f"    [MigrationLastSyncedAt]   = ? "
                        f"WHERE [FileID] = ? AND {allowed_from} "
                        f"  AND ([MigrationStatus] NOT IN ('{STATUS_ERROR}','Failed') "
                        f"       OR ISNULL([MigrationBackendStatus],'') <> ISNULL(?,''))",
                        [backend_status, mig_req_id, file_item_id, retry_count,
                         error_code, error_message, submitted_by, now, fid,
                         backend_status],
                    )
                    if cur.rowcount:
                        failed += cur.rowcount

                elif ui == "Skipped":
                    # Same fold-into-Error policy as apply_backend_file_status.
                    cur.execute(
                        f"UPDATE dbo.[{table}] "
                        f"SET [MigrationStatus]         = '{STATUS_ERROR}', "
                        f"    [MigrationBackendStatus]  = ?, "
                        f"    [MigrationRequestId]      = COALESCE(?, [MigrationRequestId]), "
                        f"    [MigrationFileItemId]     = COALESCE(?, [MigrationFileItemId]), "
                        f"    [MigrationErrorCode]      = COALESCE(?, 'skipped'), "
                        f"    [ErrorMessage]            = ?, "
                        f"    [SubmittedBy]             = COALESCE(?, [SubmittedBy]), "
                        f"    [MigrationLastSyncedAt]   = ? "
                        f"WHERE [FileID] = ? AND {allowed_from} "
                        f"  AND ([MigrationStatus] NOT IN ('{STATUS_ERROR}','Failed') "
                        f"       OR ISNULL([MigrationBackendStatus],'') <> ISNULL(?,''))",
                        [backend_status, mig_req_id, file_item_id, error_code,
                         error_message or "Skipped by migration backend",
                         submitted_by, now, fid, backend_status],
                    )
                    if cur.rowcount:
                        skipped += cur.rowcount

                else:  # STATUS_IN_PROCESSING — active platform state
                    # Promote Pending → In Processing on first observation.
                    # For a row already In Processing this refreshes the
                    # observation columns; the state-change guard below
                    # keeps the counter honest by not counting a pure
                    # observation refresh as "reconciled".
                    cur.execute(
                        f"UPDATE dbo.[{table}] "
                        f"SET [MigrationStatus]         = '{STATUS_IN_PROCESSING}', "
                        f"    [MigrationBackendStatus]  = ?, "
                        f"    [MigrationRequestId]      = COALESCE(?, [MigrationRequestId]), "
                        f"    [MigrationFileItemId]     = COALESCE(?, [MigrationFileItemId]), "
                        f"    [MigrationRetryCount]     = ?, "
                        f"    [MigrationErrorCode]      = ?, "
                        f"    [ErrorMessage]            = ?, "
                        f"    [SubmittedBy]             = COALESCE(?, [SubmittedBy]), "
                        f"    [MigrationLastSyncedAt]   = ? "
                        f"WHERE [FileID] = ? AND {allowed_from} "
                        f"  AND [MigrationStatus] <> '{STATUS_IN_PROCESSING}'",
                        [backend_status, mig_req_id, file_item_id, retry_count,
                         error_code, error_message, submitted_by, now, fid],
                    )
                    if cur.rowcount:
                        ip += cur.rowcount

            cn.commit()
        except Exception:
            cn.rollback()
            raise
        finally:
            cn.autocommit = True

    total = migrated + failed + skipped + ip
    if total:
        logger.info(
            "reconcile_external_file_status: migrated=%d failed=%d "
            "skipped=%d in_processing=%d total=%d",
            migrated, failed, skipped, ip, total,
        )
    return {
        "migrated":      migrated,
        "failed":        failed,
        "skipped":       skipped,
        "in_processing": ip,
        "rowsAffected":  total,
    }


def get_local_ids_for_migration(migration_request_id: str) -> list[dict]:
    """Return the rows we submitted to a given migration so the sync loop
    can correlate platform file records back to local FileIDs.

    The platform does NOT round-trip our ``extra_metadata.local_file_id``
    hint on file records (confirmed live 2026-09-22), so we cannot rely
    on it inside the sync loop.  Instead we use the mapping we already
    persisted at submit time in ``record_submission``:

        FileID (local)  →  MigrationFileItemId (platform)  →  SharePointPath
                                                          →  FileName

    Returns a list of dicts:
        {
          "fileID":              "337,514",
          "migrationFileItemId": "<uuid>" | None,
          "fileName":            "20220128 notice ....pdf",
          "sharePointPath":      "https://itellicloud.sharepoint.com/..."
        }

    Only rows still in an ACTIVE state (In Processing or Failed) are
    returned — Migrated rows are already terminal and should not be
    re-touched by the sync loop.
    """
    _ensure_schema_once()
    mid = (migration_request_id or "").strip()
    if not mid:
        return []
    table = settings.CONTRACT_TABLE
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT [FileID], [MigrationFileItemId], [FileName], [SharePointPath] "
            f"FROM dbo.[{table}] "
            f"WHERE [MigrationRequestId] = ? "
            f"  AND [MigrationStatus] IN ('{STATUS_IN_PROCESSING}', '{STATUS_FAILED}')",
            [mid],
        )
        out: list[dict] = []
        for r in cur.fetchall():
            out.append({
                "fileID":              str(r[0]) if r[0] is not None else "",
                "migrationFileItemId": str(r[1]) if r[1] is not None else "",
                "fileName":            str(r[2]) if r[2] is not None else "",
                "sharePointPath":      str(r[3]) if r[3] is not None else "",
            })
        return out


def active_migration_ids() -> list[str]:
    """Return the distinct MigrationRequestIds for all rows currently
    'In Processing'.  Used by /api/migrations/sync to know which
    migrations to poll.  Deterministic order for logging / tests.
    """
    _ensure_schema_once()
    table = settings.CONTRACT_TABLE
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT DISTINCT [MigrationRequestId] "
            f"FROM dbo.[{table}] "
            f"WHERE [MigrationStatus] = '{STATUS_IN_PROCESSING}' "
            f"  AND [MigrationRequestId] IS NOT NULL "
            f"ORDER BY [MigrationRequestId]"
        )
        return [str(r[0]) for r in cur.fetchall() if r[0]]


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
    """Flag the given FileIDs as `Excluded='Yes'` on the master inventory.

    Post-refactor this is a single UPDATE on the master ContractInventory —
    no more INSERT-into-excluded-table + DELETE-from-active dance.  The row
    stays exactly where it was; only the exclusion flag + metadata columns
    change.  The exclusion_audit sidecar continues to record one row per
    exclusion event so the "who / when / why" history is preserved
    unchanged.

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

    Eligibility rules (unchanged from the pre-refactor behaviour):
      * `Migrate='Yes'`         → NEVER excluded (audit trail preservation).
      * `MigrationStatus = In Processing` → NEVER excluded (must not
                                            disturb an in-flight copy).
      * Already `Excluded='Yes'`→ silently skipped (idempotent).

    Never physically deletes anything.  No SharePoint file touched.
    Recoverable via restore_excluded().
    """
    _ensure_schema_once()
    ids = [str(i).strip() for i in file_ids if str(i).strip()]
    if not ids:
        return {"succeeded": [], "failed": [], "rowsAffected": 0}

    active = settings.CONTRACT_TABLE
    audit  = exclusion_audit_table_name()
    placeholders = ", ".join("?" for _ in ids)

    # A single SYSUTCDATETIME() call for the whole batch keeps ExcludedDate
    # identical across sibling rows (nicer for audit "run" queries).
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
            # Step 1 — flip Excluded='Yes' on eligible rows.  Guarded WHERE
            # ensures already-migrated / in-flight / already-excluded rows
            # are silently skipped (row-count reflects only real transitions).
            cur.execute(
                f"UPDATE dbo.[{active}] "
                f"SET   [Excluded]     = 'Yes', "
                f"      [ExcludedDate] = ?, "
                f"      [ExcludedBy]   = ? "
                f"WHERE [FileID] IN ({placeholders}) "
                f"  AND ISNULL([Excluded], 'No')       <> 'Yes' "
                f"  AND ISNULL([Migrate], 'No')        <> 'Yes' "
                f"  AND ISNULL([MigrationStatus], '{STATUS_PENDING}') <> "
                f"      '{STATUS_IN_PROCESSING}'",
                [now, excluded_by, *ids],
            )
            updated = cur.rowcount

            # Step 2 — capture which IDs actually flipped.  Match on the
            # ExcludedDate stamp we just wrote so we don't accidentally
            # scoop up rows excluded in a different batch on the same day.
            cur.execute(
                f"SELECT [FileID], [FileName] FROM dbo.[{active}] "
                f"WHERE [FileID] IN ({placeholders}) "
                f"  AND [ExcludedDate] = ?",
                [*ids, now],
            )
            succeeded_rows = [(str(r[0]), _s(r[1])) for r in cur.fetchall()]
            succeeded = [fid for fid, _ in succeeded_rows]

            # Step 3 — write one audit row per succeeded exclusion.  Same
            # transaction so the flag and its history are atomic.  Payload
            # shape is unchanged so existing dashboards / reports continue
            # to work.
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
        "mark_excluded: requested=%d flagged=%d failed=%d by=%s reason=%s",
        len(ids), updated, len(failed), excluded_by or "-", reason or "-",
    )
    return {
        "succeeded":    succeeded,
        "failed":       failed,
        "rowsAffected": updated,
    }


def restore_excluded(file_ids: Iterable[str],
                     restored_by: str | None = None) -> dict:
    """Flag the given FileIDs as `Excluded='No'` on the master inventory.

    Post-refactor this is a single UPDATE on the master ContractInventory —
    no INSERT-into-active + DELETE-from-excluded copy.  The row was never
    moved in the first place; we simply clear the exclusion flag and its
    two metadata columns.

    The row's business state (MigrationStatus, Migrate, Migrated,
    MigratedDate, all folder columns, all migration-platform integration
    columns) is preserved intact — so an accidentally-excluded Migrated
    row stays Migrated on restore, exactly as before.

    Audit trail: the most-recent un-restored exclusion_audit row per
    file is stamped with `restored_at` + `restored_by`.  The audit row
    itself is never deleted — history is append-only.
    """
    _ensure_schema_once()
    ids = [str(i).strip() for i in file_ids if str(i).strip()]
    if not ids:
        return {"succeeded": [], "failed": [], "rowsAffected": 0}

    active = settings.CONTRACT_TABLE
    audit  = exclusion_audit_table_name()
    placeholders = ", ".join("?" for _ in ids)
    restored_by = (restored_by or "").strip() or None
    now = datetime.utcnow()

    with get_connection() as cn:
        cn.autocommit = False
        cur = cn.cursor()
        try:
            # Step 1 — clear the Excluded flag + metadata.  Only rows that
            # are currently Excluded='Yes' are touched (idempotent).
            cur.execute(
                f"UPDATE dbo.[{active}] "
                f"SET   [Excluded]     = 'No', "
                f"      [ExcludedDate] = NULL, "
                f"      [ExcludedBy]   = NULL "
                f"WHERE [FileID] IN ({placeholders}) "
                f"  AND ISNULL([Excluded], 'No') = 'Yes'",
                ids,
            )
            updated = cur.rowcount

            # Step 2 — capture which IDs actually flipped (rows that were
            # Excluded='Yes' before this UPDATE ran).  We probe by looking
            # for Excluded='No' rows in the id list.  This is a superset of
            # rows we just updated (unaffected rows were already 'No'),
            # so we intersect with the input ids to get precise membership.
            cur.execute(
                f"SELECT [FileID] FROM dbo.[{active}] "
                f"WHERE [FileID] IN ({placeholders}) "
                f"  AND ISNULL([Excluded], 'No') = 'No'",
                ids,
            )
            now_no = {str(r[0]) for r in cur.fetchall()}
            succeeded = [fid for fid in ids if fid in now_no]

            # Step 3 — stamp `restored_at` + `restored_by` on the newest
            # un-restored audit row per succeeded file (unchanged from
            # pre-refactor).
            if succeeded:
                del_ph = ", ".join("?" for _ in succeeded)
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

    # `succeeded` is the intersection of input ids and rows currently
    # Excluded='No'.  A row that was already 'No' before we ran also lands
    # here — that's fine (it means "the request is satisfied").  `failed`
    # captures anything that stayed excluded despite being requested.
    failed = [i for i in ids if i not in set(succeeded)]
    logger.info(
        "restore_excluded: requested=%d flagged_off=%d failed=%d by=%s",
        len(ids), updated, len(failed), restored_by or "-",
    )
    return {
        "succeeded":    succeeded,
        "failed":       failed,
        "rowsAffected": updated,
    }
