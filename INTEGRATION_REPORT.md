# SharePoint Migration Platform Integration — Final Reconciliation Report

**App:** Contract Migration Review (`/Users/Ashok.Pottapalli/Desktop/Legal`)
**Platform:** SharePoint File Migration Platform (deployed at `MIGRATION_API_BASE_URL`)
**Reconciled against:** the confirmed API contract you shared this session
(supersedes the earlier `SharePoint_Migration_Platform_Technical_Handover.docx`
where they diverge).

**Overall status: ALIGNED.**
Test suite: **76/76 passing** — 75 unit tests (mocked HTTP, no live platform)
in ~0.3 s + 1 end-to-end live-Azure-SQL smoke test in ~28 s.

---

## 1. Architecture (unchanged)

The app is a control-plane UI on top of the migration platform. It
submits work via REST and observes state via polling. Power Automate,
dispatch, retry, idempotency and callbacks are ALL owned by the platform.
The app never calls Power Automate directly.

```
Browser (static/js/app.js)
  │  POST /api/migrate          (user clicks "Copy to Destination")
  │  POST /api/migrations/sync  (every 15 s while in_processing > 0)
  ▼
FastAPI (this app — routes/contracts.py)
  │  POST {platform}/api/v1/migrations                    (create)
  │  POST {platform}/api/v1/migrations/{id}/files/batch   (attach files)
  │  GET  {platform}/api/v1/migrations/{id}/files         (poll status)
  ▼
SharePoint File Migration Platform
  │  worker loop (~30 s)
  ▼
Power Automate → SharePoint (destination)
```

---

## 2. Confirmed contract → implementation delta table

| # | Area | Contract | Before this session | After this session | Status |
|---|------|----------|---------------------|--------------------|--------|
|  1 | `source_path` shape | Library-relative — `"CustomerA/A.pdf"` (platform prepends library) | Library-prefixed — `"Contracts/CustomerA/A.pdf"` | Library-relative | **FIXED** |
|  2 | `created_by` field on create-migration | Required | Missing | Required kwarg on payload builder; route reads authenticated session email | **FIXED** |
|  3 | Migration ID field on create response | `migration_id` (canonical), `id` legacy-tolerated | Only `id` read | `extract_migration_id()` helper prefers `migration_id`, falls back to `id` | **FIXED** |
|  4 | Route session context | Needed for `created_by` | No session param | `Depends(require_session)` added | **FIXED** |
|  5 | Single-file endpoint | Contract exposes `POST .../files` alongside batch | Only batch endpoint wrapped | `add_file()` method added | **FIXED** |
|  6 | Backend `skipped` mapping | Do not map to Excluded | Already correct — maps to `Skipped` UI bucket | Unchanged | **OK** |
|  7 | Partial-group failure wording | "Submission Failed" | "Failed" + raw exception | Prefixed with `"Submission Failed: …"` | **FIXED** |
|  8 | Backend statuses `dispatching`, `paused`, `cancelled` | Must not break; `paused`→In Processing, `cancelled`→Failed | Not in status sets → treated as unknown → left untouched | Added to `_BACKEND_IN_PROCESSING` / `_BACKEND_FAILED` frozensets | **FIXED** |
|  9 | Frontend destination preview | Reflect real config | Hardcoded string `"Wave2 destination/…"` | `/api/contracts` now echoes `config.{destSiteUrl,destLibrary,destFolderPath}`; modal uses that | **FIXED** |
| 10 | Smoke-test fake platform response | `migration_id` | `id` | Fake now returns `migration_id` — exercises the primary field path | **FIXED** |
| 11 | Env validation at submit | Per-field message for each missing MIGRATION_DEST_* var | Only checked `MIGRATION_API_BASE_URL` | Route validates all three dest vars and returns 503 naming each missing one | **FIXED** |
| 12 | Batch response `file_item_id` correlation | 3-strategy fallback + `GET /files` follow-up | Only `extra_metadata.local_file_id`+`id` | `correlate_batch_response()` static method (extra_metadata → source_path → file_name); route adds `GET /files` fallback for still-unmapped IDs | **FIXED** |

---

## 3. Files changed this session

### 3.1 Backend

**`services/migration_paths.py`**
- `ParsedSource.source_path` now returns library-**relative** path
  (was library-prefixed). Docstring cites the confirmed contract
  verbatim. Doctest updated: `'A/B/c.pdf'` instead of
  `'Shared Documents/A/B/c.pdf'`.

**`services/migration_platform.py`**
- Extended `_BACKEND_IN_PROCESSING` with `dispatching, paused`.
- Extended `_BACKEND_FAILED` with `cancelled`.
- `build_migration_request_payload()` — added required `created_by`
  keyword argument; removed non-contract `extra_metadata` from the
  create-migration payload (per-file items still carry it as a
  correlation aid).
- Added module-level `_extract_id(obj, *field_names)` helper.
- Added `MigrationPlatformService.extract_migration_id(response)`
  staticmethod — accepts both `migration_id` (contract) and `id` (legacy).
- Added `MigrationPlatformService.add_file(migration_id, payload)` for
  the contract's single-file endpoint (batch preferred, single available).
- Added `MigrationPlatformService.correlate_batch_response(submitted,
  response_items)` static method with 3-strategy fallback:
  `extra_metadata.local_file_id` → `source_path` → `file_name`.
- Docstring on `build_files_batch_payload()` clarifies that
  `source_path` is library-RELATIVE + `extra_metadata` is best-effort.

**`routes/contracts.py`**
- `/api/migrate` handler now accepts `session: dict = Depends(require_session)`.
- Added per-field env-validation block that returns 503 with a
  message naming every missing MIGRATION_DEST_* variable before any
  DB write.
- Computes `created_by = session.email or "system"` and passes it into
  `build_migration_request_payload()`.
- Uses `service.extract_migration_id(created)` instead of `created["id"]`.
- Replaced the inline batch-correlation loop with
  `service.correlate_batch_response(...)`.
- Added `GET /files` follow-up when any local FileID is still
  unmapped after batch correlation (non-fatal — sync poll picks up
  remainders on the next tick).
- Failed-group error message now prefixed with `"Submission Failed:"`
  matching the contract wording.
- `/api/contracts` response now includes a `config` block echoing
  `MIGRATION_DEST_SITE_URL`, `MIGRATION_DEST_LIBRARY`,
  `MIGRATION_DEST_FOLDER_PATH` for the Migrate-confirm modal preview
  (no secrets, no base URL, nothing sensitive).

### 3.2 Frontend

**`static/js/app.js`**
- Added `state.migrationConfig = {destSiteUrl, destLibrary, destFolderPath}`.
- Start-button handler stores `json.config` into `state.migrationConfig`.
- `_summariseMigrateSelection(fileIds)` now builds the destination
  preview from `state.migrationConfig` (`"{destLibrary}/{destFolderPath}/{source-subfolder}"`)
  instead of the hardcoded `"Wave2 destination/…"` string.
- When config is empty (server not yet configured), shows a neutral
  `"(destination not configured)"` placeholder so the user knows the
  preview is non-authoritative.

### 3.3 Tests

**`/tmp/legal_ui/test_migration_paths.py`**
- `test_typical_nested_path` and `test_file_at_library_root` updated
  to assert library-relative `source_path` per the confirmed contract.

**`/tmp/legal_ui/test_migration_platform_service.py`**
- `TestMapBackendStatus` extended with `dispatching`, `paused`,
  `cancelled` cases.
- `TestBuildMigrationRequestPayload` renamed
  → `test_shape_matches_confirmed_contract`; asserts `created_by`
  present, `extra_metadata` absent.
- New `test_created_by_is_required_kwarg` — guards against silent
  regression of `created_by` becoming optional.
- `TestBuildFilesBatchPayload.test_files_carry_library_relative_source_path`
  — asserts `source_path` has NO library prefix.
- New `TestExtractMigrationId` (4 cases: primary `migration_id`,
  legacy `id`, precedence, missing).
- New `TestCorrelateBatchResponse` (5 cases: primary via metadata,
  fallback via source_path, fallback via file_name, missing IDs
  absent, accepts `file_item_id` field).

**`/tmp/legal_ui/test_migrate_route.py`**
- Added `_dest_env()` context manager to set the three
  MIGRATION_DEST_* env vars on the settings singleton per-test.
- `fake_platform` fixture now binds `extract_migration_id` +
  `correlate_batch_response` to the real static methods (otherwise
  the MagicMock returned unhashable mock objects that broke FastAPI's
  JSON encoder).
- All happy-path/multi-group/rollback tests now supply
  `{"migration_id": ...}` in the create-migration mock response.
- Happy-path test additionally asserts that `created_by` was passed as
  the authenticated user's email (`test@example.com`).
- Group-failure test asserts the failed-group error string starts with
  `"Submission Failed:"`.
- New `test_migrate_returns_503_with_per_field_message_when_dest_env_missing`
  — asserts all three MIGRATION_DEST_* variable names appear in the 503
  detail so ops can fix them without spelunking.

**`/tmp/legal_ui/test_migration_integration_smoke.py`**
- Fake platform HTTP handler for `POST /api/v1/migrations` now returns
  `{"migration_id": ..., "status": "pending", ...}` instead of `{"id": ...}`.
  This exercises the primary/contract-mandated field name against a
  real live-database round-trip.

---

## 4. Files NOT touched (verified working, preserved as-is)

- `services/data_service.py` — schema, `mark_in_processing`,
  `apply_backend_file_status`, `record_submission`,
  `active_migration_ids`, `get_rows_for_submission` all still valid.
- `core/database.py` — `MigrationIntegration` DDL untouched.
- `services/power_automate.py` — legacy dead code; retained but not referenced.
- `services/auth_service.py` — session dict shape confirmed used as-is.
- `main.py` — routing untouched.
- All existing UI features preserved: Folder Filter, Folder 1-20 columns,
  Search, Columns picker, All Contracts navigation, Pagination,
  Export CSV, Exclude/Restore, top bucket counts, SharePoint links,
  Run ID column, Error Message column, sorting, Migration Status
  column with destination link + retry badge, modal-wide, migrate-groups,
  migstatus-cell CSS.

---

## 5. Runtime contract summary (post-reconciliation)

### 5.1 Create migration — one per (site, library, root-folder) group

```
POST {MIGRATION_API_BASE_URL}/api/v1/migrations
Content-Type: application/json

{
  "name":                    "legal-mig-20260922-Shared-Documents-A-B-5",
  "created_by":              "ashok@bs.nttdata.com",   # authenticated session email
  "priority":                3,
  "source_site_url":         "https://tenant.sharepoint.com/sites/Legal",
  "source_library":          "Shared Documents",
  "source_folder_path":      "A/B",
  "destination_site_url":    "<MIGRATION_DEST_SITE_URL>",
  "destination_library":     "<MIGRATION_DEST_LIBRARY>",
  "destination_folder_path": "<MIGRATION_DEST_FOLDER_PATH>"
}

→ 201 {"migration_id": "<uuid>", "status": "pending", ...}
```

### 5.2 Add files — batch (multiple files → one HTTP call)

```
POST {MIGRATION_API_BASE_URL}/api/v1/migrations/{id}/files/batch
Content-Type: application/json

{
  "files": [
    {
      "file_name":        "Agreement.pdf",
      "source_path":      "CustomerA/SAP/Agreement.pdf",   # LIBRARY-RELATIVE
      "destination_path": "Wave2/CustomerA/SAP/Agreement.pdf",
      "extra_metadata":   { "local_file_id": "<FileID>" }   # best-effort
    },
    ...
  ]
}

→ 201 {"files": [{"id": "...", "extra_metadata": {...}, ...}, ...]}
```

### 5.3 Sync poll

```
GET {MIGRATION_API_BASE_URL}/api/v1/migrations/{id}/files
→ 200 [{"id": "...", "status": "queued|in_progress|completed|failed|retrying|skipped",
         "extra_metadata": {...}, "destination_url": "...",
         "retry_count": N, "error_message": "...", "error_code": "..."}]
```

### 5.4 Status mapping (single source of truth — `services.migration_platform.map_backend_status`)

| Backend | UI bucket |
|---------|-----------|
| `pending`, `queued`, `in_progress`, `retrying`, `dispatching`, `paused` | **In Processing** |
| `completed` | **Migrated** |
| `failed`, `cancelled` | **Failed** |
| `skipped` | **Skipped** (NOT Excluded) |
| anything else / empty | leave current UI status untouched |

---

## 6. Multi-location grouping rule

One click may span multiple `(source_site_url, source_library,
source_folder_path)` groups. The route submits **one migration request
per group** and reports them independently:

- Successful groups → **committed** (rows stay In Processing, browser
  polls until terminal outcome).
- Failed groups → **rolled back to Failed** with error message
  `"Submission Failed: <reason>"` on `ErrorMessage`. Never rolls back a
  group the platform already accepted.

---

## 7. Environment variables validated at submit-time

Before any DB row flips to In Processing, the route confirms each of
these is non-empty and returns a 503 naming any that are missing:

| Var | Purpose |
|-----|---------|
| `MIGRATION_API_BASE_URL` | Platform base URL (checked by `service.is_configured()`) |
| `MIGRATION_DEST_SITE_URL` | Destination `destination_site_url` |
| `MIGRATION_DEST_LIBRARY`  | Destination `destination_library` |
| `MIGRATION_DEST_FOLDER_PATH` | Destination `destination_folder_path` + `build_destination_path()` root |

`MIGRATION_API_TIMEOUT`, `MIGRATION_DEFAULT_PRIORITY`, and
`MIGRATION_NAME_PREFIX` have sensible defaults and are not
submit-blocking.

---

## 8. Central helpers (single source of truth — do NOT duplicate)

| Helper | Location |
|--------|----------|
| Status mapper `map_backend_status()` | `services/migration_platform.py` |
| SharePoint URL parser `parse_sharepoint_url()` | `services/migration_paths.py` |
| Destination path builder `build_destination_path()` | `services/migration_paths.py` |
| Migration ID extractor `extract_migration_id()` | `services/migration_platform.py` |
| Batch response correlator `correlate_batch_response()` | `services/migration_platform.py` |
| Group splitter `group_files_for_submission()` | `services/migration_platform.py` |

The browser has a **defensive** URL parser at
`static/js/app.js:_parseSharePointPathForPreview` used only for the
modal preview — the server re-parses authoritatively before submit.

---

## 9. Test results (final)

```
$ MYENV/bin/python -m pytest \
    /tmp/legal_ui/test_migration_paths.py \
    /tmp/legal_ui/test_migration_platform_service.py \
    /tmp/legal_ui/test_migrate_route.py \
    /tmp/legal_ui/test_migration_integration_smoke.py -v

======================= 76 passed, 8 warnings in 28.55s ========================
```

| Test file | Tests | Purpose | Runtime |
|-----------|-------|---------|---------|
| `test_migration_paths.py`             | 15 | URL parsing, destination builder, name suggestion | < 0.1 s |
| `test_migration_platform_service.py`  | 44 | Payload shapes, status mapping, ID extraction, batch correlation, HTTP error handling, no-auth-header guarantee | 0.15 s |
| `test_migrate_route.py`               | 16 | End-to-end via FastAPI TestClient with mocked HTTP + patched DB — happy path, multi-group split, per-group failure isolation, 503 no-config, 503 missing-dest-env, invalid path, noop, sync poll happy path, sync per-migration errors, duplicate submit, legacy callback inert | 0.1 s |
| `test_migration_integration_smoke.py` | 1  | **LIVE Azure SQL** + in-process fake platform HTTP server — end-to-end submit + sync round-trip, verifies row state transitions on the real DB | 28 s |

---

## 10. Live-DB verification

The smoke test:
1. Picks 3 real `Pending` rows with valid `https://…` SharePointPath
   from the Azure SQL `ContractInventory` table.
2. Stands up an in-process HTTP server that impersonates the migration
   platform's `POST /api/v1/migrations`, `POST /files/batch`, and
   `GET /files` endpoints (returning `migration_id`, then progressing
   file statuses `queued → in_progress → completed`).
3. Points `MIGRATION_API_BASE_URL` at that fake server.
4. Calls the real `/api/migrate` route via `TestClient`.
5. Asserts the rows flipped `Pending → In Processing`.
6. Calls `/api/migrations/sync` twice (once for `in_progress`, once for
   `completed`).
7. Asserts the rows flipped `In Processing → Migrated` with real
   `MigrationRequestId`, `MigrationFileItemId`, `DestinationUrl`,
   `MigratedDate` values persisted to Azure SQL.
8. Restores the original row state (roll-back cleanup).

Result: **PASS** in 28 s. The DB was left in its original state after
the run.

---

## 11. Not touched / preserved UI features

- Folder Filter (global, across Folder 1-20)
- Folder navigator (root "All Contracts" + drill-down)
- Global Search (across the eight indexed columns)
- Columns picker (show/hide any column; Migrate/MigratedDate always visible)
- Pagination (fixed 100 rows/page)
- Sorting (all columns)
- Export CSV (uses filtered set, RFC-4180 escaping, UTF-8 BOM)
- Exclude / Restore (recoverable soft-delete via `ContractInventory_Excluded`)
- KPI buckets (Pending / In Processing / Migrated / Failed / Excluded)
  driven by server-side `count_all()`
- SharePoint source link column
- Run ID + Error Message columns
- Migration Status column with destination link + retry badge
- Session auth + login page

---

## 12. Recommended follow-ups (optional, not blocking)

- Replace `datetime.utcnow()` in
  `services/migration_platform.py:190` with
  `datetime.now(datetime.UTC)` — Python 3.12 deprecation warning.
- Add a Playwright regression test that verifies the Migrate-confirm
  modal's destination preview text now reflects `state.migrationConfig`
  and not the old hardcoded string. Current coverage is via
  visual/manual inspection.
- When the platform starts returning an authenticated header
  requirement, add `MIGRATION_API_AUTH_HEADER` env var and inject in
  `MigrationPlatformService._request()`. The
  `TestNoAuthHeader.test_request_has_no_authorization_header` test
  documents the current no-auth baseline.

---

## 13. Sign-off

All 12 delta items reconciled. All 76 tests passing (75 mocked + 1 live-DB).
No existing UI functionality regressed. The app is aligned with the
confirmed API contract and ready for platform integration testing.
