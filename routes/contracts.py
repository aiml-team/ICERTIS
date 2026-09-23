"""Contract endpoints — database-backed.

    GET  /api/contracts                         → active rows + population counts
                                                   (accepts ?include_excluded=1 to
                                                   also return Excluded='Yes' rows;
                                                   used by the Total Documents CSV
                                                   export)
    GET  /api/excluded                          → excluded rows (recoverable)
    POST /api/migrate                           → submit selection to migration platform
    POST /api/migrations/sync                   → poll platform, apply per-file outcomes
    GET  /api/migrations/{id}                   → platform migration record passthrough
    GET  /api/migrations/{id}/audit             → platform audit log passthrough
    POST /api/migrate/callback                  → LEGACY (Power Automate no longer calls us)
    POST /api/exclude                           → flag selected FileIDs as Excluded='Yes'
    POST /api/restore                           → flag selected FileIDs as Excluded='No'

MIGRATION FLOW (post-integration with SharePoint File Migration Platform)
────────────────────────────────────────────────────────────────────────
The current app is a control-plane UI on top of the deployed migration
platform (see SharePoint_Migration_Platform_Technical_Handover.docx).
Power Automate, dispatch, retry, idempotency and callbacks are ALL owned
by the platform; we only submit and observe.

On /api/migrate:
    1. Flip selected rows Pending|Failed → 'In Processing' in our DB
       (Migrate=No, MigratedDate=NULL — nothing is "migrated" yet).
    2. Load the eligible rows and split by (site, library, root-folder)
       tuple — one migration request per group (handover §6 answer).
    3. For each group: POST /api/v1/migrations then POST .../files/batch
       against the platform.  Persist the returned MigrationRequestId +
       file_item IDs on our inventory rows via record_submission().
    4. Groups that FAILED at either step are rolled back to Failed via
       apply_migration_result() so nothing stays stuck In Processing.
       Groups that SUCCEEDED stay In Processing and the browser polls
       /api/migrations/sync every 15s until in_processing == 0.

On /api/migrations/sync (browser poller):
    1. active_migration_ids() → distinct MigrationRequestIds with any
       In Processing row.
    2. Per ID: platform.get_migration_files → map backend status →
       apply_backend_file_status() → returns per-bucket delta.
    3. Response includes updated counts so the UI can refresh buckets.

Exclusion is a **recoverable soft-delete**: the row stays on the master
`ContractInventory` table and is flagged with Excluded='Yes' (an atomic
UPDATE, no physical row move).  Restore flips the flag back to 'No'.
The legacy `ContractInventory_Excluded` table is kept for audit history
only; back-migration on startup folds any rows there into the master
with Excluded='Yes' + preserved ExcludedDate/ExcludedBy metadata.
No SharePoint file is ever deleted at any point.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from routes.auth import require_session
from services import migration_platform, migration_platform_db
from services.data_service import (
    ActiveMigrationExistsError,
    active_migration_ids,
    apply_backend_file_status,
    apply_migration_result,
    can_user_start_migration,
    classify_retry_eligibility,
    count_all,
    get_local_ids_for_migration,
    get_rows_for_submission,
    get_user_active_migration_count,
    load_contracts,
    load_excluded,
    mark_excluded,
    mark_in_processing,
    record_retry_history,
    record_submission,
    restore_excluded,
    stamp_retry_history_new_request_id,
)
from services.migration_platform import (
    MigrationPlatformError,
    map_backend_status,
)

router = APIRouter(tags=["Contracts"])


class MigrateRequest(BaseModel):
    ids: List[str] = Field(..., min_length=1,
                           description="FileIDs selected for migration")


class RetryRequest(BaseModel):
    """Payload for POST /api/migrations/retry.  Same shape as
    MigrateRequest — kept as a separate model so its docs read
    "retry" instead of "migrate" in the OpenAPI schema and so the
    contract stays explicit if the two flows diverge later."""
    ids: List[str] = Field(..., min_length=1,
                           description="FileIDs currently in the Error bucket "
                                       "that the user wants to resubmit")


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
    """LEGACY webhook payload.  Power Automate no longer calls this route
    directly — it is owned by the migration platform now.  The route is
    kept inert so any stale flow that still posts here gets a well-formed
    2xx response instead of a 404 (which would trigger PA-side retries).
    """
    runId:     Optional[str]                        = None
    results:   Optional[List[MigrateCallbackResultItem]] = None
    succeeded: Optional[List[str]]                       = None
    failed:    Optional[List[Any]]                       = None


class MigrationSyncResponse(BaseModel):
    """Response shape for POST /api/migrations/sync."""
    polled:   int                = Field(..., description="How many migration IDs were polled")
    updated:  Dict[str, int]     = Field(..., description="Per-bucket rowcounts actually updated this cycle")
    # counts is Dict[str, Any] rather than Dict[str, int] because the
    # per-user extension adds a bool key (can_migrate) alongside the
    # existing int bucket counts.  See services.data_service.count_all().
    counts:   Dict[str, Any]     = Field(..., description="Full up-to-date population counts (same shape as /api/contracts); when the session is authenticated also includes my_in_processing / my_active_total / can_migrate")
    errors:   List[Dict[str, str]] = Field(default_factory=list,
                                           description="Per-migration errors — polling continues on the rest")
    # Externally-triggered migration reconciliation (spec §External
    # discovery).  Optional so older clients that only inspect the four
    # legacy fields keep working unchanged.
    reconciled: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Summary of the external-migration reconciliation pass this tick: "
                    "polled/candidates/matched/ambiguous/unmatched + updated bucket counts.",
    )


@router.get("/contracts")
def get_contracts(include_excluded: bool = False,
                  session: dict = Depends(require_session)):
    """Return contract inventory rows plus population counts.

    Query parameters
    ----------------
    include_excluded : bool, default False
        - False (default): return only rows currently NOT excluded
          (`Excluded='No'`).  This is what the Manual Review table renders.
        - True: return the COMPLETE inventory including excluded rows.
          Used by the "Total Documents → Export CSV" path so the export
          contains every document in the inventory (per manager spec).

    Response shape:
        {
          "data":     [ ...inventory rows... ],
          "total":    <returned count>,
          "counts":   {
              "active":           N,   # rows currently Excluded='No'
              "excluded":         E,   # rows currently Excluded='Yes'
              "total":            N+E, # complete inventory
              "pending":          P,   # active + MigrationStatus='Pending'
              "in_processing":    IP,  # active + MigrationStatus='In Processing'
              "migrated":         Mig, # active + MigrationStatus='Migrated'
              "error":            E,   # active + MigrationStatus IN ('Error','Failed')
              "failed":           E,   # legacy alias for `error` (kept for
                                       # cached JS during rollout); identical value
              # Per-user workload-lock fields (present iff session
              # carries an email — the /api/contracts route always
              # requires a session, so these are always present):
              "my_in_processing": My,  # SubmittedBy=session.email & In Processing
              "my_active_total":  My,  # same, across all active statuses
              "can_migrate":      bool # True iff my_active_total == 0
          },
          "config": {                  # migration destination echo
              "destSiteUrl":    "<https://...>",
              "destLibrary":    "<library-name>",
              "destFolderPath": "<folder/path>"
          }
        }
    """
    try:
        records = load_contracts(include_excluded=bool(include_excluded))
        # Pass the session email so the response carries per-user counts
        # the UI needs to render the Migrate button state on first paint
        # (no separate round-trip required).
        counts = count_all(user_email=session.get("email"))
        from core.config import settings as _settings
        return {
            "data":   records,
            "total":  len(records),
            "counts": counts,
            "config": {
                "destSiteUrl":    _settings.MIGRATION_DEST_SITE_URL or "",
                "destLibrary":    _settings.MIGRATION_DEST_LIBRARY or "",
                "destFolderPath": _settings.MIGRATION_DEST_FOLDER_PATH or "",
                # Browser auto-refresh cadence for buckets + table rows.
                # Read once on page hydrate; the client clamps to
                # 2..60 seconds so a misconfigured env value can never
                # produce a runaway loop or a stalled UI.
                "pollIntervalSeconds": int(_settings.MIGRATION_POLL_INTERVAL or 5),
            },
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


def _submit_groups_background(groups: List[Dict[str, Any]],
                              created_by: str,
                              retry_history_by_local_id: Optional[Dict[str, int]] = None) -> None:
    """Phase 3 of the migrate flow, executed AFTER the /api/migrate HTTP
    response has been sent to the client.

    Per task §1/§2 the UI must not wait on the platform HTTP round-trip.
    /api/migrate marks eligible rows In Processing synchronously, then
    schedules this function as a FastAPI BackgroundTask.  Successful
    groups leave their rows In Processing (browser's /api/migrations/sync
    picks up the eventual completion via PostgreSQL — task §3).  Failed
    groups are rolled back to Failed by the same helpers /api/migrate
    used to use inline (apply_migration_result), so nothing gets stuck
    In Processing that was never accepted by the platform (task §9).

    ``retry_history_by_local_id`` (optional, retry flow only): mapping
    FileID → retry_history.id captured by /api/migrations/retry before
    the row was flipped out of Error.  When present, after the platform
    accepts the resubmission and record_submission stamps the new
    MigrationRequestId onto the master row, we close the audit loop by
    updating retry_history.new_migration_request_id to the same value.
    For non-retry submissions (the classic Migrate flow) this parameter
    is None and the behaviour is unchanged.

    This helper is deliberately extracted verbatim from the original
    inline Phase 3 logic plus the retry-audit hook — no other behaviour
    change, only execution timing.
    """
    import logging as _lg
    log = _lg.getLogger(__name__)
    service = migration_platform.service

    for g in groups:
        group_local_ids = [fid for fid, _p in g["files"]]

        # ── 3a: create the migration (retry up to 3× on 409 name collision).
        migration_id: Optional[str] = None
        last_create_error: Optional[MigrationPlatformError] = None
        for attempt in range(3):
            try:
                mig_payload = service.build_migration_request_payload(
                    group_key=g["group_key"],
                    file_count=len(g["files"]),
                    created_by=created_by,
                )
                created = service.create_migration(mig_payload)
                migration_id = service.extract_migration_id(created)
                if not migration_id:
                    raise MigrationPlatformError(
                        "create_migration succeeded but response had no "
                        "'migration_id' (or legacy 'id') field",
                    )
                break
            except MigrationPlatformError as e:
                last_create_error = e
                if e.status_code == 409 and attempt < 2:
                    continue
                break

        if not migration_id:
            # Roll this group back — nothing was submitted for it (task §9).
            apply_migration_result(succeeded_ids=[], failed_ids=group_local_ids)
            e = last_create_error
            raw = (f"{e} (status={e.status_code})"
                   if e and e.status_code else str(e) if e else "unknown error")
            log.warning(
                "background submit: create_migration failed for group %s: %s",
                g.get("group_key"), raw,
            )
            continue

        # ── 3b: attach files.  On failure, roll back this group only.
        try:
            files_payload = service.build_files_batch_payload(g["files"])
            batch_resp = service.add_files_batch(migration_id, files_payload)
        except MigrationPlatformError as e:
            apply_migration_result(succeeded_ids=[], failed_ids=group_local_ids)
            raw = (f"add_files_batch failed for migration_id={migration_id}: "
                   f"{e} (status={e.status_code})" if e.status_code
                   else f"add_files_batch failed for migration_id={migration_id}: {e}")
            log.warning("background submit: %s", raw)
            continue

        # ── 3c: correlate platform file_item IDs back to local FileIDs.
        batch_items = (batch_resp.get("files") if isinstance(batch_resp, dict)
                       else batch_resp) or []
        if isinstance(batch_resp, dict) and not batch_items:
            batch_items = batch_resp.get("items") or []
        file_item_id_by_local_id = service.correlate_batch_response(
            g["files"], batch_items,
        )
        missing = [lid for lid, _ in g["files"]
                   if lid not in file_item_id_by_local_id]
        if missing:
            try:
                server_items = service.get_migration_files(migration_id)
                file_item_id_by_local_id.update(
                    service.correlate_batch_response(g["files"], server_items)
                )
            except MigrationPlatformError:
                # Non-fatal — the sync poll picks up mappings later.
                pass

        record_submission(
            group_local_ids,
            migration_request_id=migration_id,
            file_item_id_by_local_id=file_item_id_by_local_id,
        )

        # ── 3d (retry flow only): close the audit loop by writing
        # `new_migration_request_id` on every retry-history row whose
        # FileID we just resubmitted successfully.  A retry-history row
        # left with NULL new_migration_request_id represents a retry
        # whose platform submission itself failed — the master row was
        # already rolled back to Error by apply_migration_result above.
        if retry_history_by_local_id:
            hist_ids = [
                retry_history_by_local_id[fid]
                for fid in group_local_ids
                if fid in retry_history_by_local_id
            ]
            if hist_ids:
                try:
                    stamp_retry_history_new_request_id(
                        hist_ids,
                        new_migration_request_id=migration_id,
                    )
                except Exception as exc:
                    # Non-fatal — the master row was already stamped by
                    # record_submission.  Failing to close the audit
                    # loop shouldn't fail the whole retry.
                    log.warning(
                        "background submit: retry-history stamp failed for "
                        "migration_id=%s hist_ids=%s: %s",
                        migration_id, hist_ids, exc,
                    )

        log.info(
            "background submit: migration_id=%s files=%d created_by=%s",
            migration_id, len(group_local_ids), created_by,
        )


@router.post("/migrate")
def migrate_contracts(payload: MigrateRequest,
                      background_tasks: BackgroundTasks,
                      session: dict = Depends(require_session)):
    """Submit the selected FileIDs to the SharePoint File Migration Platform.

    Asynchronous per task §1/§2:

    Phase 1 (SYNCHRONOUS, before response is sent):
        * ``mark_in_processing`` flips eligible rows Pending|Failed → In
          Processing in Azure SQL — the UI's bucket counts change
          immediately on the next /api/contracts call the browser makes.
        * ``get_rows_for_submission`` + ``group_files_for_submission``
          shape the batch and identify rows with unparseable SharePoint
          paths.  Invalid rows are rolled back to Failed SYNCHRONOUSLY
          so the response body can accurately list them.

    Phase 2 (BACKGROUND, after response is sent — see _submit_groups_background):
        * For each (site, library, folder) group: POST /api/v1/migrations
          then POST .../files/batch, then persist migration_request_id +
          file_item_ids on the inventory rows via ``record_submission``.
        * Groups whose platform call fails are rolled back to Failed
          (task §9) — the browser's /api/migrations/sync will observe
          that transition on the next tick.

    Response shape (unchanged — clients that don't need the async detail
    can ignore the new ``submitted`` semantics: it lists rows *sent for*
    background submission, not rows the platform has accepted yet):
        {
          "runId":                "<first-id-or-null>",       # legacy field
          "migrationRequestIds":  [],                         # populated by sync (async submit)
          "inProcessing":         ["FileID", ...],            # rows flipped In Processing
          "submitted":            {},                         # populated by sync (async submit)
          "failed":               [{"fileID": "...", "error": "..."}],
                                                              # invalid-path rows only (sync)
          "skipped":              ["FileID", ...],            # ineligible in Phase 1
          "invalid":              [{"fileID": "...", "error": "..."}],
          "groups":               [ ... group descriptors ... ],
          "error":                "..." | null
        }
    """
    service = migration_platform.service

    # Fail fast if the platform is not configured.  Do NOT flip any rows
    # to In Processing when we cannot possibly submit them — this keeps
    # the DB honest and avoids ghost In-Processing rows.
    if not service.is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Migration platform is not configured "
                "(MIGRATION_API_BASE_URL is empty).  Cannot submit."
            ),
        )

    # Validate destination config per-field so ops see exactly which
    # environment variable is missing rather than a generic error.  The
    # platform requires destination_site_url + destination_library on
    # every create-migration call (contract Step 1) and we build
    # destination_path from destination_folder_path — all three MUST be
    # non-empty before we flip any row to In Processing.
    from core.config import settings as _settings
    _dest_missing = []
    if not (_settings.MIGRATION_DEST_SITE_URL or "").strip():
        _dest_missing.append("MIGRATION_DEST_SITE_URL")
    if not (_settings.MIGRATION_DEST_LIBRARY or "").strip():
        _dest_missing.append("MIGRATION_DEST_LIBRARY")
    if not (_settings.MIGRATION_DEST_FOLDER_PATH or "").strip():
        _dest_missing.append("MIGRATION_DEST_FOLDER_PATH")
    if _dest_missing:
        raise HTTPException(
            status_code=503,
            detail=(
                "Migration configuration incomplete: "
                + ", ".join(f"{v} is missing." for v in _dest_missing)
            ),
        )

    # created_by is required on every create-migration call (contract
    # §CREATED_BY).  Use the authenticated user's email; fall back to
    # "system" only when the session has no email — never send empty.
    session_email = (session.get("email") or "").strip()
    created_by    = session_email or "system"

    # ── Per-user workload lock (fast pre-check) ──────────────────────────
    # Reject early if this user already has active rows so we don't do
    # any of the expensive downstream work (row loads, grouping, HTTP).
    # The AUTHORITATIVE check happens inside mark_in_processing() below
    # under a UPDLOCK/HOLDLOCK — this pre-check just improves latency
    # for the common "user clicks Migrate while their previous batch is
    # still running" case, and is what returns the friendly 409 body
    # with the accurate current count.
    #
    # Uses the *session* email (never falls back to "system") because
    # the whole point is per-user isolation; a session with no email
    # would incorrectly share a lock with every other identity-less
    # caller.  Anonymous / no-email sessions are treated as
    # "no lock enforced" and proceed.
    if session_email:
        allowed, active_count = can_user_start_migration(session_email)
        if not allowed:
            raise HTTPException(
                status_code=409,
                detail={
                    "allowed":     False,
                    "reason":      "ACTIVE_MIGRATION_EXISTS",
                    "activeCount": active_count,
                    "message":     (
                        f"You already have {active_count} file"
                        f"{'s' if active_count != 1 else ''} in processing. "
                        f"Wait until your current migration completes "
                        f"before submitting another batch."
                    ),
                },
            )

    try:
        # ── Phase 1 ── flip eligible rows to In Processing AND stamp
        # SubmittedBy in one atomic transaction.  If a concurrent tab
        # for the same user squeaks past the pre-check above and races
        # us to here, mark_in_processing()'s UPDLOCK+HOLDLOCK re-check
        # will lose the race and raise ActiveMigrationExistsError,
        # which we translate to the same 409 below.
        try:
            phase1 = mark_in_processing(payload.ids, submitted_by=session_email)
        except ActiveMigrationExistsError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "allowed":     False,
                    "reason":      "ACTIVE_MIGRATION_EXISTS",
                    "activeCount": exc.active_count,
                    "message":     (
                        f"You already have {exc.active_count} file"
                        f"{'s' if exc.active_count != 1 else ''} in processing. "
                        f"Wait until your current migration completes "
                        f"before submitting another batch."
                    ),
                },
            )
        in_processing_ids = phase1["succeeded"]
        skipped_ids       = phase1["skipped"]

        if not in_processing_ids:
            return {
                "runId":               None,
                "migrationRequestIds": [],
                "inProcessing":        [],
                "submitted":           {},
                "failed":              [],
                "skipped":             skipped_ids,
                "invalid":             [],
                "groups":              [],
                "error":               None,
            }

        # ── Phase 2 ── load full rows + split by (site, library, folder).
        rows = get_rows_for_submission(in_processing_ids)
        groups, invalid_rows = service.group_files_for_submission(rows)

        # Roll back invalid rows so they don't stay stuck In Processing.
        invalid_ids = [str(r.get("fileID")) for r in invalid_rows if r.get("fileID")]
        invalid_items: List[Dict[str, str]] = []
        if invalid_ids:
            apply_migration_result(
                succeeded_ids=[], failed_ids=invalid_ids,
            )
            invalid_items = [
                {"fileID": fid,
                 "error": "SharePointPath is missing or not a recognisable "
                          "SharePoint URL — cannot build source_path."}
                for fid in invalid_ids
            ]

        # ── Phase 2 done ── schedule the platform-HTTP submission to run
        # AFTER the response is sent.  See _submit_groups_background for
        # the exact logic (unchanged from the original inline Phase 3 —
        # only the execution timing moved).
        #
        # `still_in_processing` reports every FileID whose row is currently
        # In Processing after Phase 1.  Rows that later fail during Phase 3
        # (platform 5xx, batch-add failure) will be rolled back to Failed
        # by the background task and picked up on the next sync tick.
        still_in_processing: List[str] = []
        groups_report: List[Dict[str, Any]] = []
        for g in groups:
            group_local_ids = [fid for fid, _p in g["files"]]
            still_in_processing.extend(group_local_ids)
            groups_report.append({
                # migrationId is unknown at this point — assigned when the
                # background task creates the migration.  Clients that need
                # it should read state via /api/migrations/sync.
                "migrationId":   None,
                "sourceSite":    g["site_url"],
                "sourceLibrary": g["library"],
                "sourceFolder":  g["folder_path"],
                "fileCount":     len(group_local_ids),
                # "queued" — created + files-batch will run in background.
                "status":        "queued",
                "error":         None,
            })

        if groups:
            background_tasks.add_task(
                _submit_groups_background, groups, created_by,
            )

        # The `submitted` map + `migrationRequestIds` are populated
        # asynchronously (background task calls record_submission) — they
        # start empty on the response.  Clients read the eventual state
        # via /api/migrations/sync + /api/contracts as usual.
        return {
            "runId":               None,
            "migrationRequestIds": [],
            "inProcessing":        still_in_processing,
            "submitted":           {},
            # `failed` on the immediate response covers ONLY invalid-path
            # rows we already rolled back synchronously.  Platform-side
            # submission failures move rows Failed asynchronously; the
            # browser observes them via the next sync tick.
            "failed":              list(invalid_items),
            "skipped":             skipped_ids,
            "invalid":             invalid_items,
            "groups":              groups_report,
            "error":               None,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Migration failed: {exc}")


@router.post("/migrations/retry")
def retry_migrations(payload: RetryRequest,
                     background_tasks: BackgroundTasks,
                     session: dict = Depends(require_session)):
    """Resubmit a set of Error rows to the migration platform.

    Mirrors POST /api/migrate exactly with three additions specific to
    the retry flow:

      1. Server-side eligibility gate: every requested FileID MUST
         currently be in MigrationStatus IN ('Error', 'Failed').  Any
         row in a non-terminal or already-migrated state causes the
         whole request to be rejected with 400 (partial retries are not
         allowed — the user needs to know exactly which rows they're
         acting on).

      2. Per-user workload lock: uses the SAME my_active_total gate
         that /api/migrate uses.  Only the caller's active count blocks
         them — other users' activity does not.  The gate is enforced
         both by an early can_user_start_migration() check and re-checked
         atomically inside mark_in_processing()'s UPDLOCK+HOLDLOCK
         transaction so two tabs from the same user can't both submit.

      3. Failure evidence is snapshotted into
         ContractMigrationRetryHistory before mark_in_processing flips
         the rows out of Error.  The snapshot captures the previous
         MigrationRequestId, MigrationFileItemId, ErrorMessage, and
         MigrationRetryCount so a later audit can reconstruct every
         attempt for a FileID.  After record_submission stamps the NEW
         MigrationRequestId onto the master row (in the background
         task), the same UUID is written back onto the retry-history
         row so both sides of the audit trail are linked.

    Response shape is identical to /api/migrate — same fields, same
    semantics — so the browser's existing post-submit rendering path
    can be reused unchanged.
    """
    service = migration_platform.service

    # Fail fast if the platform is not configured (same gate /api/migrate uses).
    if not service.is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Migration platform is not configured "
                "(MIGRATION_API_BASE_URL is empty).  Cannot submit retry."
            ),
        )

    from core.config import settings as _settings
    _dest_missing = []
    if not (_settings.MIGRATION_DEST_SITE_URL or "").strip():
        _dest_missing.append("MIGRATION_DEST_SITE_URL")
    if not (_settings.MIGRATION_DEST_LIBRARY or "").strip():
        _dest_missing.append("MIGRATION_DEST_LIBRARY")
    if not (_settings.MIGRATION_DEST_FOLDER_PATH or "").strip():
        _dest_missing.append("MIGRATION_DEST_FOLDER_PATH")
    if _dest_missing:
        raise HTTPException(
            status_code=503,
            detail=(
                "Migration configuration incomplete: "
                + ", ".join(f"{v} is missing." for v in _dest_missing)
            ),
        )

    session_email = (session.get("email") or "").strip()
    created_by    = session_email or "system"
    session_id    = session.get("session_id") or session.get("id")

    # ── Eligibility gate — every requested FileID must be Error/Failed. ──
    # Enforced server-side so a stale client (Migrated row hanging in a
    # cached selection, JS bug that includes Pending rows, etc.) can
    # never accidentally retry the wrong document.
    classification = classify_retry_eligibility(payload.ids)
    if classification["ineligible"] or classification["unknown"]:
        raise HTTPException(
            status_code=400,
            detail={
                "reason":     "INELIGIBLE_FOR_RETRY",
                "message":    (
                    "One or more requested FileIDs are not currently in "
                    "the Error bucket.  Retry only accepts rows whose "
                    "MigrationStatus is 'Error' (or the legacy 'Failed')."
                ),
                "ineligible": classification["ineligible"],
                "unknown":    classification["unknown"],
                "eligible":   classification["eligible"],
            },
        )
    if not classification["eligible"]:
        # Empty payload after dedupe — nothing to do.
        return {
            "runId":               None,
            "migrationRequestIds": [],
            "inProcessing":        [],
            "submitted":           {},
            "failed":              [],
            "skipped":             [],
            "invalid":             [],
            "groups":              [],
            "retryHistory":        [],
            "error":               None,
        }

    # ── Per-user workload lock (fast pre-check).  See /api/migrate for
    # the full rationale — same lock, same 409 shape. ──────────────────
    if session_email:
        allowed, active_count = can_user_start_migration(session_email)
        if not allowed:
            raise HTTPException(
                status_code=409,
                detail={
                    "allowed":     False,
                    "reason":      "ACTIVE_MIGRATION_EXISTS",
                    "activeCount": active_count,
                    "message":     (
                        f"You already have {active_count} file"
                        f"{'s' if active_count != 1 else ''} in processing. "
                        f"Wait until your current migration completes "
                        f"before submitting a retry."
                    ),
                },
            )

    try:
        # ── Retry §3: snapshot failure evidence BEFORE the flip.  If
        # this fails we abort — better to lose the retry than lose the
        # audit trail.  Note: record_retry_history is idempotent per
        # FileID in the sense that it always writes a NEW row, so
        # replaying a retry that partially succeeded is safe.
        eligible_ids = classification["eligible"]
        try:
            history_rows = record_retry_history(
                eligible_ids,
                retried_by=session_email or "system",
                session_id=str(session_id) if session_id else None,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Retry audit snapshot failed: {exc}",
            )
        retry_history_by_local_id = {
            r["fileID"]: r["retryHistoryId"] for r in history_rows
        }

        # ── Phase 1 — flip Error → In Processing under the per-user
        # lock.  mark_in_processing accepts both 'Error' and legacy
        # 'Failed' on the source side, so no changes are needed there.
        try:
            phase1 = mark_in_processing(eligible_ids, submitted_by=session_email)
        except ActiveMigrationExistsError as exc:
            # Race with a concurrent tab.  The retry-history rows we
            # just wrote will be left with NULL new_migration_request_id
            # — that's a valid audit signal ("retry attempted but
            # blocked").  Return the same 409 shape as /api/migrate.
            raise HTTPException(
                status_code=409,
                detail={
                    "allowed":     False,
                    "reason":      "ACTIVE_MIGRATION_EXISTS",
                    "activeCount": exc.active_count,
                    "message":     (
                        f"You already have {exc.active_count} file"
                        f"{'s' if exc.active_count != 1 else ''} in processing. "
                        f"Wait until your current migration completes "
                        f"before submitting a retry."
                    ),
                },
            )
        in_processing_ids = phase1["succeeded"]
        skipped_ids       = phase1["skipped"]

        if not in_processing_ids:
            # Race lost between classify + flip (row was picked up by
            # another mark_in_processing call in the meantime, or the
            # Excluded flag was flipped).  Well-formed empty response.
            return {
                "runId":               None,
                "migrationRequestIds": [],
                "inProcessing":        [],
                "submitted":           {},
                "failed":              [],
                "skipped":             skipped_ids,
                "invalid":             [],
                "groups":              [],
                "retryHistory":        history_rows,
                "error":               None,
            }

        # ── Phase 2 — load + group + roll back invalid-path rows.
        rows = get_rows_for_submission(in_processing_ids)
        groups, invalid_rows = service.group_files_for_submission(rows)

        invalid_ids = [str(r.get("fileID")) for r in invalid_rows if r.get("fileID")]
        invalid_items: List[Dict[str, str]] = []
        if invalid_ids:
            apply_migration_result(succeeded_ids=[], failed_ids=invalid_ids)
            invalid_items = [
                {"fileID": fid,
                 "error": "SharePointPath is missing or not a recognisable "
                          "SharePoint URL — cannot build source_path."}
                for fid in invalid_ids
            ]

        # ── Schedule Phase 3 in the background, WITH the retry-history
        # map so record_submission's new MigrationRequestId can be
        # mirrored back onto the audit rows.
        still_in_processing: List[str] = []
        groups_report: List[Dict[str, Any]] = []
        for g in groups:
            group_local_ids = [fid for fid, _p in g["files"]]
            still_in_processing.extend(group_local_ids)
            groups_report.append({
                "migrationId":   None,
                "sourceSite":    g["site_url"],
                "sourceLibrary": g["library"],
                "sourceFolder":  g["folder_path"],
                "fileCount":     len(group_local_ids),
                "status":        "queued",
                "error":         None,
            })

        if groups:
            background_tasks.add_task(
                _submit_groups_background, groups, created_by,
                retry_history_by_local_id,
            )

        return {
            "runId":               None,
            "migrationRequestIds": [],
            "inProcessing":        still_in_processing,
            "submitted":           {},
            "failed":              list(invalid_items),
            "skipped":             skipped_ids,
            "invalid":             invalid_items,
            "groups":              groups_report,
            "retryHistory":        history_rows,
            "error":               None,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Retry failed: {exc}")


@router.post("/migrations/sync", response_model=MigrationSyncResponse)
def sync_migrations(session: dict = Depends(require_session)):
    """Sync Azure SQL row statuses from the migration platform.

    Per task §12 the platform's PostgreSQL is the AUTHORITATIVE per-file
    execution result: only ``file_items.status = 'completed'`` marks a
    file Migrated in our Azure SQL inventory (task §4).  The platform
    HTTP API stays available as a fallback.

    Source-selection strategy (task §13):
      1. Batch-read all active migrations' file_items from PostgreSQL in
         ONE connection + ONE query (services.migration_platform_db.
         fetch_files_for_migrations).  This is the primary path.
      2. For migrations the DB call did not cover (DB disabled, unreachable,
         or the migration_id has no file rows yet), fall back to the HTTP
         API per migration.
      3. Both paths funnel into the SAME status mapper and the SAME
         persister — no duplicate translation logic (task §16).

    Called by the browser every 8 s while counts.in_processing > 0 (and
    once on page load to resume tracking after a browser refresh — task §8).

    Errors on individual migrations do NOT abort the whole cycle — they
    are collected into ``errors`` and the caller retries next tick.
    Idempotent: repeated calls converge to the same DB state.
    """
    service   = migration_platform.service
    ids       = active_migration_ids()
    aggregate = {"migrated": 0, "failed": 0, "skipped": 0,
                 "in_processing": 0, "rowsAffected": 0}
    errors: List[Dict[str, str]] = []

    # ── Primary: one batch read from PostgreSQL for ALL active IDs. ──
    # Returns { migration_id: [items...] }.  Empty dict when PG is
    # disabled/unreachable — we transparently fall back to HTTP per id.
    grouped_from_db: Dict[str, List[Dict[str, Any]]] = (
        migration_platform_db.fetch_files_for_migrations(ids) if ids else {}
    )

    # Guardrails when neither source is available.  Nothing to do — but
    # return a well-formed body so the poller can keep working.
    # Per-user counts (my_in_processing / can_migrate) travel back on
    # every sync response so the UI can decrement the "My Active" tile
    # and auto-unlock the Migrate button as the user's rows complete —
    # without a separate round-trip.
    _user_email = session.get("email")
    if not ids:
        return MigrationSyncResponse(
            polled=0, updated=aggregate,
            counts=count_all(user_email=_user_email), errors=errors,
        )
    if not grouped_from_db and not service.is_configured():
        return MigrationSyncResponse(
            polled=0, updated=aggregate,
            counts=count_all(user_email=_user_email),
            errors=[{"migrationId": "",
                     "error": "Neither MIGRATION_DB_URL nor "
                              "MIGRATION_API_BASE_URL is configured"}],
        )

    for mid in ids:
        # PostgreSQL is authoritative — use its rows when present.
        items: List[Dict[str, Any]] = list(grouped_from_db.get(mid) or [])
        http_err: Optional[MigrationPlatformError] = None

        # If PG returned nothing for this id (disabled, unreachable, or
        # simply no file rows yet), fall back to the HTTP API.
        if not items and service.is_configured():
            try:
                items = service.get_migration_files(mid)
            except MigrationPlatformError as e:
                http_err = e

        if not items:
            if http_err is not None:
                errors.append({
                    "migrationId": mid,
                    "error": (f"{http_err} (status={http_err.status_code})"
                              if http_err.status_code else str(http_err)),
                })
            # No items from either path — leave rows alone this tick.
            continue

        # ── Correlate platform file records back to local FileIDs. ──
        # The platform does NOT round-trip extra_metadata on file records
        # (confirmed live 2026-09-22), so we cannot use the local_file_id
        # hint we sent at submit time.  Instead, build indexes from what
        # we persisted locally in record_submission() and match by:
        #   1. MigrationFileItemId  (populated for rows in the "with_ids"
        #      branch of record_submission — most reliable)
        #   2. file_name            (unique within a migration for our
        #      workload — one file per row from the inventory)
        #   3. source_path suffix   (last-resort tiebreaker)
        local_rows = get_local_ids_for_migration(mid)
        by_item_id: Dict[str, str] = {}
        by_file_name: Dict[str, str] = {}
        by_src_path: Dict[str, str] = {}
        for lr in local_rows:
            fid = lr.get("fileID") or ""
            if not fid:
                continue
            item_id = (lr.get("migrationFileItemId") or "").strip()
            if item_id:
                by_item_id[item_id] = fid
            name = (lr.get("fileName") or "").strip()
            if name and name not in by_file_name:
                by_file_name[name] = fid
            sp = (lr.get("sharePointPath") or "").strip()
            if sp and sp not in by_src_path:
                by_src_path[sp] = fid

        updates: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue

            # Strategy 1: platform file_item id → local FileID
            local_id = by_item_id.get(str(item.get("id") or "").strip(), "")

            # Strategy 2: file_name → local FileID
            if not local_id:
                fn = str(item.get("file_name") or "").strip()
                if fn and fn in by_file_name:
                    local_id = by_file_name[fn]

            # Strategy 3: source_path suffix — the platform stores
            # source_path WITHOUT the library prefix and WITHOUT the
            # scheme/site, so match by "path ends with the platform's
            # source_path" against the local SharePointPath URL.
            if not local_id:
                sp = str(item.get("source_path") or "").strip()
                if sp:
                    # URL-decode a stored local path for tail comparison
                    from urllib.parse import unquote
                    for local_url, fid_candidate in by_src_path.items():
                        if unquote(local_url).endswith(sp):
                            local_id = fid_candidate
                            break

            if not local_id:
                # Couldn't correlate — skip rather than risk a cross-write.
                continue

            backend_status = item.get("status")
            ui = map_backend_status(backend_status)
            if ui is None:
                continue
            updates.append({
                "fileID":               local_id,
                "backendStatus":        backend_status,
                "uiStatus":             ui,
                "migrationFileItemId":  item.get("id"),
                "retryCount":           item.get("retry_count"),
                "errorMessage":         item.get("error_message"),
                "errorCode":            item.get("error_code"),
                "destinationUrl":       item.get("destination_url"),
            })

        if updates:
            delta = apply_backend_file_status(updates)
            for k in aggregate:
                aggregate[k] += int(delta.get(k, 0) or 0)

    # ── External-migration reconciliation (spec §External discovery) ──
    # Runs AFTER the known-migration branch so any race between "user
    # submitted here" and "external tool touched the same file" is
    # resolved in favour of the more-recent PostgreSQL row.  Failures
    # here never break the sync response — the reconciler is fail-soft.
    reconciled_summary: Optional[Dict[str, Any]] = None
    try:
        from services import migration_reconciliation
        rec = migration_reconciliation.reconcile_external_migrations()
        # Fold the reconciled bucket counts into the same aggregate the
        # frontend already renders — a completed external migration is
        # observationally identical to a completed local one from the
        # UI's perspective.
        rec_updated = rec.get("updated") or {}
        for k in aggregate:
            aggregate[k] += int(rec_updated.get(k, 0) or 0)
        reconciled_summary = rec
    except Exception as exc:  # pragma: no cover — defensive
        import logging as _lg2
        _lg2.getLogger(__name__).exception(
            "reconcile_external_migrations failed: %s", exc,
        )

    return MigrationSyncResponse(
        polled=len(ids),
        updated=aggregate,
        counts=count_all(user_email=_user_email),
        errors=errors,
        reconciled=reconciled_summary,
    )


@router.get("/migrations/{migration_id}")
def get_migration_detail(migration_id: str):
    """Thin passthrough to GET /api/v1/migrations/{id} on the platform.
    Used by the future detail drawer / debug view.  The response body is
    the platform's record verbatim (no field renaming) so anyone reading
    the handover doc can match the shape without translation."""
    service = migration_platform.service
    if not service.is_configured():
        raise HTTPException(status_code=503,
                            detail="Migration platform is not configured.")
    try:
        return service.get_migration(migration_id)
    except MigrationPlatformError as e:
        raise HTTPException(
            status_code=e.status_code or 502,
            detail=str(e),
        )


@router.get("/migrations/{migration_id}/audit")
def get_migration_audit(migration_id: str):
    """Thin passthrough to GET /api/v1/migrations/{id}/audit on the platform."""
    service = migration_platform.service
    if not service.is_configured():
        raise HTTPException(status_code=503,
                            detail="Migration platform is not configured.")
    try:
        return {"items": service.get_audit(migration_id)}
    except MigrationPlatformError as e:
        raise HTTPException(
            status_code=e.status_code or 502,
            detail=str(e),
        )


@router.post("/migrate/callback")
def migrate_callback(payload: MigrateCallbackRequest):
    """LEGACY endpoint.

    Under the pre-integration architecture, Power Automate posted per-file
    outcomes here.  The current architecture routes ALL Power Automate
    callbacks to the migration platform's own endpoint
    (POST {platform}/api/v1/callbacks/power-automate — see handover §10.1);
    this app observes state via POST /api/migrations/sync instead.

    We keep the route so any stale flow that still calls it receives a
    2xx and does not trigger PA-side retry storms, but we no longer apply
    the payload to our DB — doing so would race with /api/migrations/sync
    and could clobber platform-authoritative state.  Payload is logged
    and discarded.
    """
    import logging
    logging.getLogger(__name__).info(
        "migrate/callback (legacy): runId=%s results=%d succeeded=%d failed=%d — ignored",
        payload.runId,
        len(payload.results or []),
        len(payload.succeeded or []),
        len(payload.failed or []),
    )
    return {
        "runId":      payload.runId,
        "migrated":   [],
        "failed":     [],
        "migratedAt": None,
        "note":       ("This endpoint is a legacy no-op. State is now sourced "
                       "from the migration platform via /api/migrations/sync."),
    }


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
