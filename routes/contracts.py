"""Contract endpoints — database-backed.

    GET  /api/contracts             → active contract rows + population counts
    GET  /api/excluded              → excluded contract rows (recoverable)
    POST /api/migrate               → move to 'In Processing' → trigger Power Automate
    POST /api/migrate/callback      → apply per-document outcome (webhook)
    POST /api/exclude               → MOVE selected FileIDs into the excluded table
    POST /api/restore               → MOVE selected FileIDs back to the active table

MIGRATION FLOW
──────────────
The correct sequence on /api/migrate is:
    1. Persist selected rows as MigrationStatus='In Processing'
       (Migrate=NO, MigratedDate=NULL — nothing is "migrated" yet).
    2. Trigger the Power Automate flow with the minimal per-doc payload.
    3. If the flow is configured SYNCHRONOUSLY: apply per-doc results
       (Migrated / Failed) inside the same request and return them.
    4. If the flow is ASYNC (default): return runId + inProcessing set;
       the flow later POSTs to /api/migrate/callback with results.

Exclusion is a **recoverable soft-delete**: the row is moved into
`ContractInventory_Excluded` (transactional) and can be restored later.
No SharePoint file is ever deleted.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from routes.auth import require_session
from services import power_automate
from services.data_service import (
    apply_migration_result,
    count_all,
    get_documents_for_migration,
    load_contracts,
    load_excluded,
    mark_excluded,
    mark_in_processing,
    restore_excluded,
)

router = APIRouter(tags=["Contracts"])


class MigrateRequest(BaseModel):
    ids: List[str] = Field(..., min_length=1,
                           description="FileIDs selected for migration")


class FolderMatch(BaseModel):
    """Per-file audit detail describing which folder level matched the
    global folder filter that triggered a bulk exclusion."""
    level: str = Field(..., description="Human label, e.g. 'Folder 4'")
    value: str = Field(..., description="The segment value that matched, e.g. 'Services'")


class ExcludeRequest(BaseModel):
    ids: List[str] = Field(..., min_length=1,
                           description="FileIDs to move into the excluded table")
    # ── Optional folder-filter audit metadata (all default to None so the
    # existing row-selection Exclude button keeps working unchanged). ──
    reason:           Optional[str]                       = Field(
        None, description="Short label, e.g. 'Matched folder filter'")
    folderFilterText: Optional[str]                       = Field(
        None, description="The search text the user typed in the folder filter")
    folderFilterMode: Optional[str]                       = Field(
        None, description="'contains' | 'starts_with' | 'exact'")
    matches:          Optional[Dict[str, FolderMatch]]    = Field(
        None, description="fileID -> matched folder level/value (per-file)")


class RestoreRequest(BaseModel):
    ids: List[str] = Field(..., min_length=1,
                           description="FileIDs to move back to the active table")


class MigrateCallbackResultItem(BaseModel):
    fileID:  str
    success: bool
    error:   Optional[str] = None


class MigrateCallbackRequest(BaseModel):
    """Webhook payload from Power Automate.  Accepts either a `results`
    array (preferred) or explicit `succeeded`/`failed` lists."""
    runId:     Optional[str]                        = None
    results:   Optional[List[MigrateCallbackResultItem]] = None
    succeeded: Optional[List[str]]                       = None
    failed:    Optional[List[Any]]                       = None


@router.get("/contracts")
def get_contracts():
    """Return every ACTIVE contract row from the database, plus population counts.

    Response shape:
        {
          "data":     [ ...active rows... ],           # each row has migrationStatus
          "total":    <active count>,                   # length of data
          "counts":   {
              "active": N, "excluded": M, "total": N,
              "pending": P, "in_processing": IP,
              "migrated": Mig, "failed": F
          }
        }
    """
    try:
        records = load_contracts()
        counts = count_all()
        return {
            "data":   records,
            "total":  len(records),
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
    """Two-phase migration for the given FileIDs.

    Phase 1 — persist 'In Processing' for eligible rows only (Pending →
    In Processing).  Migrate flag and MigratedDate are NOT touched.

    Phase 2 — trigger the Power Automate flow with the resulting batch.
      • SYNC flow → apply per-doc results immediately, return them.
      • ASYNC flow → return runId + inProcessing IDs; the flow later
        POSTs results to /api/migrate/callback.
      • Unconfigured → same as ASYNC (rows stay In Processing).
      • Trigger error → apply failure to the batch so the UI reflects it
        and the rows return to a non-in-flight state (Failed).

    Response shape (all fields always present):
        {
          "runId":         "<uuid>",
          "mode":          "sync"|"async"|"unconfigured"|"error",
          "inProcessing": ["…"],  # rows persisted to In Processing (Phase 1)
          "migrated":     ["…"],  # empty unless mode=sync
          "failed":       [{"fileID": "…", "error": "…"}],  # empty unless sync/error
          "migratedAt":   "…" | null,
          "skipped":      ["…"],  # rows that were NOT eligible for Phase 1
          "error":        "…" | null
        }
    """
    try:
        # Phase 1 — DB: Pending → In Processing (persist BEFORE calling PA).
        phase1 = mark_in_processing(payload.ids)
        in_processing_ids = phase1["succeeded"]
        skipped_ids       = phase1["skipped"]

        if not in_processing_ids:
            # Nothing eligible — return early without touching Power Automate.
            return {
                "runId":         None,
                "mode":          "noop",
                "inProcessing":  [],
                "migrated":      [],
                "failed":        [],
                "migratedAt":    None,
                "skipped":       skipped_ids,
                "error":         None,
            }

        # Phase 2 — hand the eligible batch to Power Automate.
        docs = get_documents_for_migration(in_processing_ids)
        pa   = power_automate.trigger_migration(docs)

        migrated_ids: List[str] = []
        failed_items: List[dict] = []
        migrated_at:  Optional[str] = None
        error_msg:    Optional[str] = None

        if pa.get("mode") == "sync":
            # Trust per-doc outcome; anything the flow didn't mention stays
            # In Processing (defensive — should not normally happen).
            result = apply_migration_result(
                succeeded_ids=pa.get("succeeded", []),
                failed_ids   =[f["fileID"] for f in pa.get("failed", [])],
            )
            migrated_ids = result["migrated"]
            failed_items = [
                {"fileID": f["fileID"], "error": f.get("error", "")}
                for f in pa.get("failed", [])
                if f.get("fileID") in set(result["failed"])
            ]
            migrated_at = result["migratedAt"]
        elif pa.get("mode") == "error":
            # Trigger itself failed — mark the whole batch as Failed so the
            # UI is honest (no rows stuck as "In Processing" forever).
            error_msg = pa.get("error")
            result = apply_migration_result(
                succeeded_ids=[], failed_ids=in_processing_ids,
            )
            failed_items = [{"fileID": fid, "error": error_msg or ""}
                            for fid in result["failed"]]
        # 'async' and 'unconfigured' both leave rows In Processing; results
        # will arrive via /api/migrate/callback.

        return {
            "runId":         pa.get("runId"),
            "mode":          pa.get("mode", "async"),
            "inProcessing":  in_processing_ids,
            "migrated":      migrated_ids,
            "failed":        failed_items,
            "migratedAt":    migrated_at,
            "skipped":       skipped_ids,
            "error":         error_msg,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Migration failed: {exc}")


@router.post("/migrate/callback")
def migrate_callback(payload: MigrateCallbackRequest):
    """Webhook invoked by Power Automate when an ASYNC migration completes.

    Applies per-document success/failure to the DB.  Only rows currently
    'In Processing' are updated (guards against duplicate deliveries).
    """
    try:
        succeeded_ids: List[str] = []
        failed_ids:    List[str] = []

        if payload.results:
            for r in payload.results:
                (succeeded_ids if r.success else failed_ids).append(r.fileID)
        else:
            succeeded_ids = [str(x) for x in (payload.succeeded or []) if str(x).strip()]
            for f in (payload.failed or []):
                if isinstance(f, dict):
                    fid = str(f.get("fileID") or f.get("fileId") or "").strip()
                    if fid:
                        failed_ids.append(fid)
                elif f is not None:
                    fid = str(f).strip()
                    if fid:
                        failed_ids.append(fid)

        result = apply_migration_result(succeeded_ids, failed_ids)
        return {
            "runId":      payload.runId,
            "migrated":   result["migrated"],
            "failed":     result["failed"],
            "migratedAt": result["migratedAt"],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Migration callback failed: {exc}")


@router.post("/exclude")
def exclude_contracts(payload: ExcludeRequest,
                      session: dict = Depends(require_session)):
    """MOVE the given FileIDs into the excluded table (transactional).

    Already-migrated rows and rows currently In Processing are skipped by
    the service layer — they cannot be excluded.  No SharePoint file is
    affected.

    Audit: `excluded_by` and `session_id` are pulled from the current
    login session so the caller never has to (and cannot) spoof them.
    The optional folder-filter metadata (`reason`, `folderFilterText`,
    `folderFilterMode`, `matches`) is persisted verbatim into the
    exclusion_audit table.
    """
    try:
        matches = None
        if payload.matches:
            # Pydantic v2 returns FolderMatch models; convert to plain dicts
            # the service layer can index by fileID.
            matches = {
                fid: {"level": m.level, "value": m.value}
                for fid, m in payload.matches.items()
            }
        result = mark_excluded(
            payload.ids,
            excluded_by        = session.get("email"),
            session_id         = session.get("session_id"),
            reason             = payload.reason,
            folder_filter_text = payload.folderFilterText,
            folder_filter_mode = payload.folderFilterMode,
            matches_by_id      = matches,
        )
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Exclusion update failed: {exc}")


@router.post("/restore")
def restore_excluded_contracts(payload: RestoreRequest,
                               session: dict = Depends(require_session)):
    """MOVE the given FileIDs back into the active table (transactional).

    Also stamps `restored_at`/`restored_by` on the most-recent unrestored
    exclusion_audit row per file (append-only history)."""
    try:
        result = restore_excluded(
            payload.ids,
            restored_by=session.get("email"),
        )
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Restore failed: {exc}")
