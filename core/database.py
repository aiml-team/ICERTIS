"""Azure SQL database access helpers.

All SQL is parameterised — no user-controlled string concatenation.

Public API
──────────
    get_connection()  → context-manager yielding a pyodbc.Connection
    ensure_schema()   → creates the ContractInventory table if missing
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

import pyodbc

from core.config import settings

logger = logging.getLogger(__name__)


@contextmanager
def get_connection() -> Iterator[pyodbc.Connection]:
    """Yield a live pyodbc connection. Caller commits/rolls back as needed."""
    if not settings.AZURE_SQL_SERVER or not settings.AZURE_SQL_PASSWORD:
        raise RuntimeError(
            "Azure SQL configuration missing. Set AZURE_SQL_SERVER, "
            "AZURE_SQL_DATABASE, AZURE_SQL_USERNAME, AZURE_SQL_PASSWORD "
            "(via environment variables or .env file)."
        )
    conn = pyodbc.connect(settings.odbc_connection_string, timeout=30)
    try:
        yield conn
    finally:
        conn.close()


# ── Schema DDL ──────────────────────────────────────────────────────────────
# Column names mirror the source CSV so the existing Manual Review UI keeps
# working without changes.  Sizes are generous (NVARCHAR(MAX) for free-form
# fields) because URLs and error messages can be long.
_TABLE_DDL_TEMPLATE = """
IF NOT EXISTS (
    SELECT 1 FROM sys.tables WHERE name = '{table}' AND schema_id = SCHEMA_ID('dbo')
)
BEGIN
    CREATE TABLE dbo.[{table}] (
        FileID                 NVARCHAR(64)  NOT NULL PRIMARY KEY,
        FileName               NVARCHAR(512) NOT NULL,
        SharePointPath         NVARCHAR(MAX) NULL,
        LastModified           NVARCHAR(64)  NULL,
        ModifiedBy             NVARCHAR(256) NULL,
        ItemType               NVARCHAR(64)  NULL,
        OpportunityID          NVARCHAR(64)  NULL,
        AE                     NVARCHAR(128) NULL,
        LegalEntity            NVARCHAR(256) NULL,
        CustomerName           NVARCHAR(256) NULL,
        AgreementName          NVARCHAR(512) NULL,
        AgreementFileName      NVARCHAR(512) NULL,
        OrderNumber            NVARCHAR(128) NULL,
        AutoRenewalStatus      NVARCHAR(64)  NULL,
        EffectiveDate          NVARCHAR(64)  NULL,
        StartDate              NVARCHAR(64)  NULL,
        EndDate                NVARCHAR(64)  NULL,
        ExpiryDate             NVARCHAR(64)  NULL,
        ContractType           NVARCHAR(128) NULL,
        TypeOfContract         NVARCHAR(128) NULL,
        AssociatedMSAFileName  NVARCHAR(512) NULL,
        AssociatedNDAFileName  NVARCHAR(512) NULL,
        VoidExclusionIndicator NVARCHAR(64)  NULL,
        ExtractionStatus       NVARCHAR(64)  NULL,
        ReviewRequired         NVARCHAR(16)  NULL,
        MissingFields          NVARCHAR(MAX) NULL,
        ProcessedDate          NVARCHAR(64)  NULL,
        ErrorMessage           NVARCHAR(MAX) NULL,
        RunId                  NVARCHAR(128) NULL,
        Migrate                NVARCHAR(8)   NOT NULL CONSTRAINT DF_{table}_Migrate DEFAULT ('No'),
        Migrated               NVARCHAR(16)  NULL,
        MigratedDate           DATETIME2     NULL,
        Excluded               NVARCHAR(8)   NOT NULL CONSTRAINT DF_{table}_Excluded DEFAULT ('No')
    );
END
"""

# Idempotent ALTER for pre-existing deployments that don't yet have the
# Excluded column.  Kept separate from the CREATE TABLE DDL so it can be run
# lazily on the first read/write without erroring on already-migrated schemas.
_ADD_EXCLUDED_COLUMN_DDL = """
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE Name = N'Excluded' AND Object_ID = Object_ID(N'dbo.[{table}]')
)
BEGIN
    ALTER TABLE dbo.[{table}]
      ADD Excluded NVARCHAR(8) NOT NULL
      CONSTRAINT DF_{table}_Excluded DEFAULT ('No') WITH VALUES;
END
"""

# Excluded (soft-deleted) documents live in a separate table.  Same column
# shape as ContractInventory (so restore is a straight column-for-column
# copy) plus an ExcludedDate timestamp.  We do NOT carry the [Excluded]
# flag column here — presence in this table IS the excluded state.
_EXCLUDED_TABLE_DDL_TEMPLATE = """
IF NOT EXISTS (
    SELECT 1 FROM sys.tables WHERE name = '{ex_table}' AND schema_id = SCHEMA_ID('dbo')
)
BEGIN
    CREATE TABLE dbo.[{ex_table}] (
        FileID                 NVARCHAR(64)  NOT NULL PRIMARY KEY,
        FileName               NVARCHAR(512) NOT NULL,
        SharePointPath         NVARCHAR(MAX) NULL,
        LastModified           NVARCHAR(64)  NULL,
        ModifiedBy             NVARCHAR(256) NULL,
        ItemType               NVARCHAR(64)  NULL,
        OpportunityID          NVARCHAR(64)  NULL,
        AE                     NVARCHAR(128) NULL,
        LegalEntity            NVARCHAR(256) NULL,
        CustomerName           NVARCHAR(256) NULL,
        AgreementName          NVARCHAR(512) NULL,
        AgreementFileName      NVARCHAR(512) NULL,
        OrderNumber            NVARCHAR(128) NULL,
        AutoRenewalStatus      NVARCHAR(64)  NULL,
        EffectiveDate          NVARCHAR(64)  NULL,
        StartDate              NVARCHAR(64)  NULL,
        EndDate                NVARCHAR(64)  NULL,
        ExpiryDate             NVARCHAR(64)  NULL,
        ContractType           NVARCHAR(128) NULL,
        TypeOfContract         NVARCHAR(128) NULL,
        AssociatedMSAFileName  NVARCHAR(512) NULL,
        AssociatedNDAFileName  NVARCHAR(512) NULL,
        VoidExclusionIndicator NVARCHAR(64)  NULL,
        ExtractionStatus       NVARCHAR(64)  NULL,
        ReviewRequired         NVARCHAR(16)  NULL,
        MissingFields          NVARCHAR(MAX) NULL,
        ProcessedDate          NVARCHAR(64)  NULL,
        ErrorMessage           NVARCHAR(MAX) NULL,
        RunId                  NVARCHAR(128) NULL,
        Migrate                NVARCHAR(8)   NOT NULL CONSTRAINT DF_{ex_table}_Migrate DEFAULT ('No'),
        Migrated               NVARCHAR(16)  NULL,
        MigratedDate           DATETIME2     NULL,
        ExcludedDate           DATETIME2     NOT NULL CONSTRAINT DF_{ex_table}_ExcludedDate DEFAULT SYSUTCDATETIME(),
        ExcludedBy             NVARCHAR(256) NULL
    );
END
"""


def ensure_schema() -> None:
    """Create the contract tables if they do not yet exist. Safe to run repeatedly.

    Creates BOTH the active table (ContractInventory) and the recoverable
    Excluded table (ContractInventory_Excluded).  Also runs any additive
    ALTERs needed to bring older schemas up to date.
    """
    ddl_active   = _TABLE_DDL_TEMPLATE.format(table=settings.CONTRACT_TABLE)
    ddl_excluded = _EXCLUDED_TABLE_DDL_TEMPLATE.format(ex_table=excluded_table_name())
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(ddl_active)
        # Additive migration for legacy schemas.
        cur.execute(_ADD_EXCLUDED_COLUMN_DDL.format(table=settings.CONTRACT_TABLE))
        cur.execute(ddl_excluded)
        cn.commit()
    logger.info("Schema ensured for tables dbo.%s + dbo.%s",
                settings.CONTRACT_TABLE, excluded_table_name())


def ensure_excluded_column() -> None:
    """Idempotent: adds the Excluded column if the table pre-dates the feature.

    Safe to call repeatedly; cheap (single metadata check).  Called lazily
    from services.data_service so we don't force startup-time DDL.
    """
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(_ADD_EXCLUDED_COLUMN_DDL.format(table=settings.CONTRACT_TABLE))
        cn.commit()


def excluded_table_name() -> str:
    """Convention: <ActiveTable>_Excluded.  Kept as a helper so callers never
    hand-concatenate the name."""
    return f"{settings.CONTRACT_TABLE}_Excluded"


def ensure_excluded_table() -> None:
    """Idempotent create of the excluded-documents table.  Also runs a one-time
    data migration: any legacy rows in the active table with Excluded='Yes'
    are moved into the excluded table so state is consistent."""
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(_EXCLUDED_TABLE_DDL_TEMPLATE.format(ex_table=excluded_table_name()))
        cn.commit()
