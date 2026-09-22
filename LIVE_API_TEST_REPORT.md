# SharePoint Migration Platform — Live API Test Report

**Date:** 2026-09-22
**Platform base URL:** `https://ca-spmig-api.wittywater-29faa4f6.westeurope.azurecontainerapps.io`
**Test method:** `curl` only (no scripts)
**Test source file:** `20170403 notice of AE assignment change.pdf` (3M Company / Contact Data)
**Reference:** platform team's "Complete Integration Guide" (2026-09-22)

---

## 1. Executive Summary

Every non-internal endpoint in the reference was exercised live. All endpoints are reachable and return sensible HTTP codes. **Platform is functioning correctly** — files ARE being copied by real Azure Logic Apps runs (confirmed via `extra_metadata.power_automate.flow_response` run IDs of format `08584115xxxxx`). Six behavioural deltas vs. the original reference doc are acknowledged by the platform team and logged.

**Endpoint coverage:** 17 of 18 public endpoints tested (callback endpoint is internal — requires secret header, correctly skipped).

---

## 2. Endpoint Test Matrix

| # | Method | Endpoint | HTTP | Result |
|---|---|---|---|---|
| 1 | GET | `/api/v1/health` | 200 | ✓ Returns `{status, app, version, environment, timestamp}` |
| 2 | GET | `/api/v1/health/db` | 200 | ✓ Returns `{status: ok, database: ok, timestamp}` |
| 3 | POST | `/api/v1/migrations` (valid) | 201 | ✓ Returns full migration object with `id` |
| 4 | POST | `/api/v1/migrations` (invalid) | 422 | ✓ Returns detailed Pydantic validation errors |
| 5 | GET | `/api/v1/migrations` | 200 | ✓ Envelope `{items, total, page, page_size, pages}` |
| 6 | GET | `/api/v1/migrations?status=pending&size=5` | 200 | ✓ `status` filter works; `size` param ignored (known) |
| 7 | GET | `/api/v1/migrations/stats` | 200 | ✓ Shape confirmed matches platform team's guide |
| 8 | GET | `/api/v1/migrations/{id}` | 200 | ✓ Full record; includes `extra_metadata.power_automate.flow_response` when in-flight |
| 9 | GET | `/api/v1/migrations/{bad-id}` | 404 | ✓ Clean `{"detail": "Migration request '...' not found."}` |
| 10 | PATCH | `/api/v1/migrations/{id}` | 200 | ⚠ `name` silently dropped (known); `priority`/`description` update correctly |
| 11 | POST | `/api/v1/migrations/{id}/files` (single) | 201 | ✓ Returns full file item with `id` |
| 12 | POST | `/api/v1/migrations/{id}/files/batch` | 201 | ⚠ Summary only, no per-file IDs (known); must GET `/files` after |
| 13 | GET | `/api/v1/migrations/{id}/files` | 200 | ✓ Envelope `{items, total, page, page_size, pages}` |
| 14 | GET | `/api/v1/migrations/{id}/files?status=X` | 200 | ✓ `status` filter works |
| 15 | GET | `/api/v1/migrations/{id}/files/status-counts` | 200 | ✓ Returns `{pending, queued, completed, failed, retrying, skipped}` |
| 16 | GET | `/api/v1/migrations/{id}/files/{file_id}` | 200 | ✓ Full file item |
| 17 | GET | `/api/v1/migrations/{id}/audit` | 200 | ⚠ Envelope `{items:[...]}` (known — docs say bare list) |
| 18 | POST | `/api/v1/migrations/{id}/status` (→ pending) | 422 | ✓ Intentionally blocked by state-machine guard |
| 19 | POST | `/api/v1/migrations/{id}/status` (→ cancelled) | 200 | ✓ Transition allowed; returns full updated record |
| 20 | POST | `/api/v1/migrations/{bad-id}/status` | 404 | ✓ Clean error |
| 21 | DELETE | `/api/v1/migrations/{id}` (pending) | 204 | ✓ Silent success; GET returns 404 |
| 22 | DELETE | `/api/v1/migrations/{id}` (cancelled) | 204 | ✓ Silent success; GET returns 404 |
| 23 | DELETE | `/api/v1/migrations/{bad-id}` | 404 | ✓ Clean error |
| — | POST | `/api/v1/callbacks/power-automate` | — | Internal — requires `X-Callback-Secret`. Correctly not called from client. |

---

## 3. Confirmed Behavioural Deltas (all acknowledged by platform team)

| # | Behaviour | Status |
|---|---|---|
| 1 | `?size=N` param ignored on list endpoints | Logged |
| 2 | `PATCH` silently drops `name` field | Logged |
| 3 | `/stats` response shape differs from original doc | Doc will be updated |
| 4 | Default `priority` = 5, not 3 | Doc will be updated |
| 5 | Audit endpoint returns `{items:[...]}` envelope, not bare list | Doc will be updated |
| 6 | Batch endpoint returns no per-file IDs | By design; call `GET /files` after |

---

## 4. State Transition Rules (confirmed live)

**Migration status transitions via `POST /status`:**

| From | To | Result |
|---|---|---|
| `in_progress` | `cancelled` | 200 OK |
| `pending` | `cancelled` | 200 OK |
| `in_progress` | `pending` | **422 blocked (by design)** |

**File status flow (automatic, driven by worker + PA callbacks):**
`pending → queued → completed / failed / skipped`
On failure: `failed → retrying → queued` (up to 3 attempts)

---

## 5. Corrections to Previous Report

Earlier drafts of this report contained three incorrect conclusions. These have been retracted:

1. **"Stub PA callback" defect — RETRACTED.** The `"test-run-id"` string is a developer artifact hardcoded inside the Power Automate flow, not a platform defect. Real Azure Logic Apps run IDs (`08584115xxxxx` format) in `extra_metadata.power_automate.flow_response` prove files are being copied by live PA executions.
2. **"33.33% stuck migration" defect — RETRACTED.** The stuck files (`batch-nonexistent-1.pdf`, `batch-nonexistent-2.pdf`) did not exist in SharePoint. PA correctly failed to locate them and entered the retry cycle. Platform behaved as designed.
3. **"`in_progress → pending` should be allowed" — RETRACTED.** The 422 rejection is an intentional state-machine guard. Correct recovery is: force to `cancelled`, then create a new migration.

**Rule going forward:** never add a file to a migration unless it physically exists in the SharePoint source library at the constructed source path (`<library>/<source_path>`).

---

## 6. Client-Side Guidance (for the Contract Migration Review app)

The app's `services/migration_platform.py` should:
- ✓ Read `id` from create-response.
- ✓ Use post-batch `GET /files` correlation (batch endpoint returns no per-file IDs — by design).
- ⚠ Not rely on `PATCH` to rename migrations (server bug — logged).
- ✓ Trust `status: completed` as proof of successful copy — the PA integration is real and working.
- ✓ When surfacing `power_automate_run_id`, be aware `"test-run-id"` is a developer artifact from the PA flow and does not indicate any platform issue.
- ⚠ Before batch-adding files, verify each `source_path` exists in the source library. `source_path` must NOT include the library name (system prepends it).

---

## 7. `source_path` Construction Rule (critical)

Given a SharePoint share URL like:
```
https://itellicloud.sharepoint.com/:b:/r/sites/US-Contracts_Management/Contracts/All%20Contracts/Contracts/Client/3M%20Company/Contact%20Data/20170403%20notice.pdf
```
Strip the `:b:/r/` prefix, then:
- **Site URL:** `https://itellicloud.sharepoint.com/sites/US-Contracts_Management`
- **Library:** `Contracts` (first segment after site)
- **`source_path`:** `All Contracts/Contracts/Client/3M Company/Contact Data/20170403 notice.pdf` (everything after the library, URL-decoded — do NOT include `Contracts/` prefix)

Platform will construct the full PA path as `<library>/<source_path>` internally.

---

*End of report.*
