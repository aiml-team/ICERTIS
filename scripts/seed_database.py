"""One-time database initialisation & seeding script.

Reads the sample CSV (`Contract Inventory.csv`) and inserts each row into the
Azure SQL `ContractInventory` table.

Design goals
────────────
* **Idempotent.** Uses SQL MERGE on the natural key `FileID`; running the
  script again does NOT create duplicates.
* **Non-destructive.** Existing rows keep their `Migrate`, `Migrated`, and
  `MigratedDate` values (so re-seeding never overwrites migration state
  captured through the UI).
* **Schema-safe.** Calls `ensure_schema()` first so the table is created if
  it doesn't yet exist.

Usage
─────
    MYENV/bin/python -m scripts.seed_database          # normal seed
    MYENV/bin/python -m scripts.seed_database --reset  # wipe + reseed

`--reset` truncates the table first (development convenience only).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

# Allow `python scripts/seed_database.py` from any CWD
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import settings  # noqa: E402
from core.database import ensure_schema, get_connection  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("seed")


# Map DB column -> CSV column (exact header). Order matches _COLUMNS in
# services/data_service.py so the SELECT/INSERT stay aligned.
_COL_MAP: list[tuple[str, str]] = [
    ("FileID",                 "FileID"),
    ("FileName",               "File Name"),
    ("SharePointPath",         "SharePointPath"),
    ("LastModified",           "LastModified"),
    ("ModifiedBy",             "ModifiedBy"),
    ("ItemType",               "ItemType"),
    ("OpportunityID",          "OpportunityID"),
    ("AE",                     "AE"),
    ("LegalEntity",            "LegalEntity"),
    ("CustomerName",           "CustomerName"),
    ("AgreementName",          "AgreementName"),
    ("AgreementFileName",      "AgreementFileName"),
    ("OrderNumber",            "OrderNumber"),
    ("AutoRenewalStatus",      "AutoRenewalStatus"),
    ("EffectiveDate",          "EffectiveDate"),
    ("StartDate",              "StartDate"),
    ("EndDate",                "EndDate"),
    ("ExpiryDate",             "ExpiryDate"),
    ("ContractType",           "ContractType"),
    ("TypeOfContract",         "TypeOfContract"),
    ("AssociatedMSAFileName",  "AssociatedMSAFileName"),
    ("AssociatedNDAFileName",  "AssociatedNDAFileName"),
    ("VoidExclusionIndicator", "VoidExclusionIndicator"),
    ("ExtractionStatus",       "ExtractionStatus"),
    ("ReviewRequired",         "ReviewRequired"),
    ("MissingFields",          "MissingFields"),
    ("ProcessedDate",          "ProcessedDate"),
    ("ErrorMessage",           "ErrorMessage"),
    ("RunId",                  "RunId"),
    ("Migrate",                "Migrate"),
    ("Migrated",               "Migrated"),
    # MigratedDate is NOT seeded from the CSV — the CSV field is a text
    # column whereas the DB uses DATETIME2.  Rows start with NULL and are
    # populated by the UI migrate action.
]

# All columns to write, in stable order.
DB_COLS = [c[0] for c in _COL_MAP] + ["MigratedDate"]


def _clean(v) -> str | None:
    """Strip whitespace; return None for empty."""
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _row_values(row) -> list:
    """Extract values in DB_COLS order from a CSV row (pandas Series)."""
    vals = [_clean(row.get(csv_col, "")) for _db_col, csv_col in _COL_MAP]
    vals.append(None)  # MigratedDate — NULL for fresh rows
    return vals


def _iter_rows(csv_path: Path):
    logger.info("Reading CSV: %s", csv_path)
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df.columns = [c.strip().strip('"') for c in df.columns]
    total = len(df)
    skipped_no_id = 0
    for _, row in df.iterrows():
        fid = _clean(row.get("FileID", ""))
        if not fid:
            skipped_no_id += 1
            continue
        yield _row_values(row)
    if skipped_no_id:
        logger.warning("Skipped %d row(s) with empty FileID", skipped_no_id)
    logger.info("CSV rows: %d (usable: %d)", total, total - skipped_no_id)


def _build_merge_sql(table: str) -> str:
    """Build a MERGE that inserts new rows and updates the *non-migration*
    columns of existing rows, leaving Migrate/Migrated/MigratedDate untouched
    when the row already exists (so we never blow away UI-recorded migrations).
    """
    all_cols = DB_COLS
    src_cols_sql = ", ".join(f"[{c}]" for c in all_cols)
    params_sql   = ", ".join("?" for _ in all_cols)

    # Preserved on update (target keeps its own values):
    preserve = {"FileID", "Migrate", "Migrated", "MigratedDate"}
    update_cols = [c for c in all_cols if c not in preserve]
    update_sql  = ", ".join(f"[{c}] = src.[{c}]" for c in update_cols)

    insert_cols_sql = ", ".join(f"[{c}]" for c in all_cols)
    insert_vals_sql = ", ".join(f"src.[{c}]" for c in all_cols)

    return (
        f"MERGE dbo.[{table}] AS tgt "
        f"USING (SELECT {src_cols_sql} FROM (VALUES ({params_sql})) v({src_cols_sql})) AS src "
        f"ON tgt.[FileID] = src.[FileID] "
        f"WHEN MATCHED THEN UPDATE SET {update_sql} "
        f"WHEN NOT MATCHED THEN INSERT ({insert_cols_sql}) VALUES ({insert_vals_sql});"
    )


def seed(reset: bool = False) -> None:
    csv_path = Path(settings.SEED_CSV_PATH)
    if not csv_path.exists():
        raise FileNotFoundError(f"Seed CSV not found: {csv_path}")

    logger.info("Ensuring schema (table: dbo.%s)…", settings.CONTRACT_TABLE)
    ensure_schema()

    table = settings.CONTRACT_TABLE
    merge_sql = _build_merge_sql(table)

    with get_connection() as cn:
        cur = cn.cursor()

        if reset:
            logger.warning("--reset: TRUNCATE dbo.%s", table)
            cur.execute(f"TRUNCATE TABLE dbo.[{table}]")
            cn.commit()

        # Batched execute for speed
        cur.fast_executemany = True
        batch: list[list] = []
        BATCH_SIZE = 200
        total = 0
        for vals in _iter_rows(csv_path):
            batch.append(vals)
            if len(batch) >= BATCH_SIZE:
                cur.executemany(merge_sql, batch)
                total += len(batch)
                logger.info("  merged %d rows (running total: %d)", len(batch), total)
                batch.clear()
        if batch:
            cur.executemany(merge_sql, batch)
            total += len(batch)
            logger.info("  merged %d rows (running total: %d)", len(batch), total)
        cn.commit()

        cur.execute(f"SELECT COUNT(*) FROM dbo.[{table}]")
        db_count = cur.fetchone()[0]

    logger.info("─" * 60)
    logger.info("Seed complete.")
    logger.info("  CSV rows processed : %d", total)
    logger.info("  Rows in database   : %d (table: dbo.%s)", db_count, table)
    logger.info("─" * 60)


def main():
    ap = argparse.ArgumentParser(description="Seed the ContractInventory table from the sample CSV.")
    ap.add_argument("--reset", action="store_true", help="TRUNCATE the table before seeding (dev only).")
    args = ap.parse_args()
    seed(reset=args.reset)


if __name__ == "__main__":
    main()
