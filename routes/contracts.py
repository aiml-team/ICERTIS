"""Contract endpoints — database-backed.

    GET  /api/contracts         → active contract rows + population counts
    GET  /api/excluded          → excluded contract rows (recoverable)
    POST /api/migrate           → mark selected FileIDs as Migrate='Yes'
    POST /api/exclude           → MOVE selected FileIDs into the excluded table
    POST /api/restore           → MOVE selected FileIDs back to the active table

Exclusion is a **recoverable soft-delete**: the row is moved into
`ContractInventory_Excluded` (transactional) and can be restored later.
No SharePoint file is ever deleted.

Future SharePoint integration
─────────────────────────────
When the real SharePoint migration endpoint becomes available:
  1. Call it from `migrate_contracts()` BEFORE `mark_migrated()` and only
     forward the succeeded IDs to the database update, OR
  2. Keep `mark_migrated()` as the persistence step and add a SharePoint
     call in a new service; the JS `migrationService.migrate()` client
     already treats the response as authoritative.
"""
from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.data_service import (
    count_all,
    load_contracts,
    load_excluded,
    mark_excluded,
    mark_migrated,
    restore_excluded,
)

router = APIRouter(tags=["Contracts"])


class MigrateRequest(BaseModel):
    ids: List[str] = Field(..., min_length=1, description="FileIDs to mark as migrated")


class ExcludeRequest(BaseModel):
    ids: List[str] = Field(..., min_length=1, description="FileIDs to move into the excluded table")


class RestoreRequest(BaseModel):
    ids: List[str] = Field(..., min_length=1, description="FileIDs to move back to the active table")


@router.get("/contracts")
def get_contracts():
    """Return every ACTIVE contract row from the database, plus population counts.

    Response shape:
        {
          "data":     [ ...active rows... ],
          "total":    <active count>,           # length of data (backwards-compat)
          "counts":   {"active": N, "excluded": M, "total": N+M}
        }
    """
    try:
        records = load_contracts()
        counts = count_all()
        return {
            "data": records,
            "total": len(records),
            "counts": counts,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load contracts: {exc}")


@router.get("/excluded")
def get_excluded():
    """Return every row from the excluded table, newest exclusion first."""
    try:
        records = load_excluded()
        return {"data": records, "total": len(records)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load excluded records: {exc}")


@router.post("/migrate")
def migrate_contracts(payload: MigrateRequest):
    """Mark ONLY the given FileIDs as migrated (`Migrate='Yes'`, timestamped).

    The database is the source of truth: this endpoint returns the same
    timestamp value it wrote, so the UI stays consistent with what persisted.
    """
    try:
        result = mark_migrated(payload.ids)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Migration update failed: {exc}")


@router.post("/exclude")
def exclude_contracts(payload: ExcludeRequest):
    """MOVE the given FileIDs into the excluded table (transactional).

    Already-migrated rows are skipped by the service layer — they cannot be
    excluded.  No SharePoint file is affected.
    """
    try:
        result = mark_excluded(payload.ids)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Exclusion update failed: {exc}")


@router.post("/restore")
def restore_excluded_contracts(payload: RestoreRequest):
    """MOVE the given FileIDs back into the active table (transactional)."""
    try:
        result = restore_excluded(payload.ids)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Restore failed: {exc}")
