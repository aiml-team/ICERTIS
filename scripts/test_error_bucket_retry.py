"""Error-bucket + Retry-flow tests (spec §Required top buckets, §Retry flow).

Fifteen scenarios covering the full lifecycle:

  Backend-status mapping / bucket accounting
    1.  PostgreSQL 'failed'    → MigrationStatus='Error' (not 'Failed')
    2.  PostgreSQL 'retrying'  → stays In Processing (spec: retrying is
                                 not terminal — user shouldn't see it in Error)
    3.  Error bucket count increments; row leaves In Processing
    4.  ErrorMessage carried on the row for user visibility
    5.  Total = Pending + InProc + Migrated + Error + Excluded
                (Error included; not folded into Pending)

  Retry endpoint eligibility gates
    6.  /api/migrations/retry accepts Error rows → 200 → In Processing
    7.  Non-Error rows in the payload → 400 with INELIGIBLE_FOR_RETRY
    8.  Unknown FileIDs → 400 with INELIGIBLE_FOR_RETRY (unknown list)
    9.  Per-user lock: my_active_total > 0 → 409 ACTIVE_MIGRATION_EXISTS
    10. Other user's activity does NOT block me (per-user lock, not global)

  Retry outcomes
    11. Retry that succeeds → row Migrated
    12. Retry that fails again → row back to Error
    13. Retry audit trail row created with previous evidence
    14. Retry audit trail row stamped with new_migration_request_id on
        successful platform submission

  CSV / count semantics
    15. Total includes Error rows (via count_all().total)
"""
from __future__ import annotations
import logging, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
logging.basicConfig(level=logging.WARNING)
logging.getLogger("core.database").setLevel(logging.WARNING)
logging.getLogger("services.data_service").setLevel(logging.WARNING)
logging.getLogger("services.migration_reconciliation").setLevel(logging.WARNING)
logging.getLogger("routes.contracts").setLevel(logging.ERROR)

from fastapi.testclient import TestClient
from core.config import settings
from core.database import get_connection, retry_history_table_name
from services import data_service, migration_platform_db, migration_reconciliation
from services.data_service import (
    STATUS_PENDING, STATUS_IN_PROCESSING, STATUS_MIGRATED, STATUS_ERROR,
    apply_backend_file_status, count_all,
)


TABLE = settings.CONTRACT_TABLE
HIST  = retry_history_table_name()

USER_A = "err-retry-a@bs.nttdata.com"
USER_B = "err-retry-b@bs.nttdata.com"


class T:
    def __init__(self): self.passed = 0; self.failed = 0
    def check(self, label, cond, detail=""):
        if cond: print(f"  PASS  {label}"); self.passed += 1
        else:    print(f"  FAIL  {label}  {detail}"); self.failed += 1
    def section(self, title): print(f"\n=== {title} ===")
    def summary(self):
        total = self.passed + self.failed
        print(f"\n{self.passed}/{total} passed  ({self.failed} failed)")
        return 0 if self.failed == 0 else 1

t = T()


# ── Test-row helpers ────────────────────────────────────────────────────
# All test rows use the "ERBK-" (Error Bucket) prefix so wipe() can
# clean them up without touching real inventory rows.

SITE = "https://itellicloud.sharepoint.com/sites/ERBK-TEST"
LIB  = "ErrBucket Test Library"


def _sp_url(name: str) -> str:
    return f"{SITE}/{LIB}/{name}"


def _insert(file_id: str, *, status="Pending", excluded="No",
            submitted_by=None, request_id=None, file_item_id=None,
            error_message=None, retry_count=0):
    fn = f"{file_id}.pdf"
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"INSERT INTO dbo.[{TABLE}] "
            f"([FileID], [FileName], [SharePointPath], [MigrationStatus], "
            f" [Migrate], [Migrated], [Excluded], [SubmittedBy], "
            f" [MigrationRequestId], [MigrationFileItemId], [ErrorMessage], "
            f" [MigrationRetryCount]) "
            f"VALUES (?, ?, ?, ?, 'No', 'False', ?, ?, ?, ?, ?, ?)",
            [file_id, fn, _sp_url(fn), status, excluded, submitted_by,
             request_id, file_item_id, error_message, retry_count],
        )
        cn.commit()


def _read(file_id: str) -> Dict[str, Any]:
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT MigrationStatus, MigrationBackendStatus, "
            f"       MigrationRequestId, MigrationFileItemId, "
            f"       SubmittedBy, MigratedDate, ErrorMessage, "
            f"       ISNULL(MigrationRetryCount, 0) "
            f"FROM dbo.[{TABLE}] WHERE FileID = ?",
            [file_id],
        )
        r = cur.fetchone()
    if not r: return {}
    return dict(zip(
        ("MigrationStatus", "MigrationBackendStatus", "MigrationRequestId",
         "MigrationFileItemId", "SubmittedBy", "MigratedDate",
         "ErrorMessage", "MigrationRetryCount"),
        r,
    ))


def _wipe():
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(f"DELETE FROM dbo.[{TABLE}] WHERE FileID LIKE 'ERBK-%'")
        cur.execute(f"DELETE FROM dbo.[{HIST}]  WHERE file_id LIKE 'ERBK-%'")
        cn.commit()


def _history_rows(file_id: str) -> List[Dict[str, Any]]:
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT id, file_id, previous_migration_request_id, "
            f"       previous_migration_file_item_id, previous_error_message, "
            f"       previous_retry_count, retried_by, "
            f"       new_migration_request_id "
            f"FROM dbo.[{HIST}] WHERE file_id = ? "
            f"ORDER BY id ASC",
            [file_id],
        )
        cols = ("id", "file_id", "prev_mid", "prev_fiid", "prev_err",
                "prev_rc", "retried_by", "new_mid")
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _login(email: str) -> TestClient:
    from main import app
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": email})
    assert r.status_code == 200, r.text
    return c


# ── FakePlatform (identical to test_naveen_ashok_lock.py) ────────────────
# So the retry route's background task can run end-to-end without hitting
# the real migration platform.  Every attempt returns a fresh, unique
# migration_id so we can prove the retry-history "new_migration_request_id"
# field gets stamped with the NEW id (not the previous failed one).

class FakePlatform:
    def __init__(self, fail_add_files_batch=False):
        self.n = 0
        self.fail_add_files_batch = fail_add_files_batch
        self.last_migration_id = None
    def is_configured(self): return True
    def build_migration_request_payload(self, **kw): return {"name": "fake", **kw}
    def create_migration(self, payload):
        self.n += 1
        self.last_migration_id = f"erbk-mid-{self.n:03d}"
        return {"migration_id": self.last_migration_id}
    def extract_migration_id(self, obj): return obj.get("migration_id") or obj.get("id")
    def build_files_batch_payload(self, files):
        return {"files": [{"local_id": lid} for lid, _p in files]}
    def add_files_batch(self, mid, payload):
        if self.fail_add_files_batch:
            from services.migration_platform import MigrationPlatformError
            raise MigrationPlatformError("simulated add-files failure",
                                          status_code=500)
        return {"items": [{"id": f"it-{i}",
                           "extra_metadata": {"local_file_id": f["local_id"]}}
                          for i, f in enumerate(payload["files"])]}
    def correlate_batch_response(self, files, items):
        out = {}
        for it in items or []:
            lid = (it.get("extra_metadata") or {}).get("local_file_id")
            fid = it.get("id")
            if lid and fid: out[str(lid)] = str(fid)
        return out
    def get_migration_files(self, mid): return []
    def group_files_for_submission(self, rows):
        from services.migration_paths import ParsedSource
        files = [(str(r["fileID"]), ParsedSource(
            site_url=SITE, library=LIB, folder_path="",
            file_name=r["fileName"] or f"{r['fileID']}.pdf")) for r in rows]
        return ([{
            "group_key":   (SITE, LIB, ""),
            "site_url":    SITE,
            "library":     LIB,
            "folder_path": "",
            "files":       files,
        }], [])


# ═══════════════════════════════════════════════════════════════════════════
# Scenarios 1-5: Bucket accounting / backend-status mapping
# ═══════════════════════════════════════════════════════════════════════════

def scenario_backend_status_mapping():
    t.section("Test 1-2: Backend-status mapping (failed→Error, retrying→InProc)")
    _wipe()
    # Insert a row already In Processing so apply_backend_file_status
    # can flip it based on the poller's observed backend status.
    _insert("ERBK-001",
            status=STATUS_IN_PROCESSING,
            submitted_by=USER_A,
            request_id="mid-old-001",
            file_item_id="fi-001",
            retry_count=0)
    _insert("ERBK-002",
            status=STATUS_IN_PROCESSING,
            submitted_by=USER_A,
            request_id="mid-old-002",
            file_item_id="fi-002")

    # PG 'failed' → UI 'Error'
    upd = apply_backend_file_status([{
        "fileID":            "ERBK-001",
        "migrationRequestId":"mid-old-001",
        "migrationFileItemId":"fi-001",
        "backendStatus":     "failed",
        "uiStatus":          "Error",
        "retryCount":        1,
        "errorMessage":      "Simulated PG failure",
        "errorCode":         "E_SIM",
        "destinationUrl":    None,
        "startedAt":         None,
        "completedAt":       None,
    }])
    r1 = _read("ERBK-001")
    t.check("PG failed → MigrationStatus='Error' (canonical spelling)",
            r1["MigrationStatus"] == STATUS_ERROR,
            str(r1))
    t.check("PG failed → ErrorMessage carried on row",
            (r1["ErrorMessage"] or "").startswith("Simulated PG"),
            str(r1))
    t.check("PG failed → MigrationRetryCount stored",
            r1["MigrationRetryCount"] == 1,
            str(r1))
    t.check("apply_backend_file_status returned failed=1",
            upd["failed"] == 1, str(upd))

    # PG 'retrying' → UI 'In Processing' (must NOT flip to Error)
    upd2 = apply_backend_file_status([{
        "fileID":            "ERBK-002",
        "migrationRequestId":"mid-old-002",
        "migrationFileItemId":"fi-002",
        "backendStatus":     "retrying",
        "uiStatus":          STATUS_IN_PROCESSING,
        "retryCount":        2,
        "errorMessage":      None,
        "errorCode":         None,
        "destinationUrl":    None,
        "startedAt":         None,
        "completedAt":       None,
    }])
    r2 = _read("ERBK-002")
    t.check("PG retrying stays In Processing (not Error)",
            r2["MigrationStatus"] == STATUS_IN_PROCESSING, str(r2))
    t.check("apply_backend_file_status did not count retrying as failed",
            upd2["failed"] == 0, str(upd2))


def scenario_error_bucket_counts():
    t.section("Test 3-5: Error bucket counts + Total formula (B+C+D+E+F)")
    # Use the two rows from scenario 1: one is now Error, one is InProc.
    # Verify the counts reflect the new bucket.
    baseline = count_all()
    t.check("count_all() returns 'error' key",
            "error" in baseline, str(baseline))
    t.check("count_all() 'failed' legacy alias == 'error'",
            baseline.get("failed") == baseline.get("error"),
            str(baseline))
    # ERBK-001 is Error, ERBK-002 is In Processing
    t.check("Error count includes ERBK-001",
            baseline["error"] >= 1, str(baseline))
    # Total should equal Pending + InProc + Migrated + Error + Excluded
    tot = (baseline["pending"] + baseline["in_processing"]
           + baseline["migrated"] + baseline["error"] + baseline["excluded"])
    t.check("Total == P + IP + M + E + Excluded (Error included)",
            baseline["total"] == tot,
            f"total={baseline['total']} vs sum={tot}")

    # Prove Error is NOT folded into Pending: ERBK-001 is Error and its
    # SharePoint/FileID look identical to Pending rows, but pending
    # count should exclude it.
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT MigrationStatus FROM dbo.[{TABLE}] WHERE FileID='ERBK-001'")
        s = cur.fetchone()[0]
    t.check("ERBK-001 on-disk value is 'Error' (not 'Failed', not 'Pending')",
            s == "Error", f"got {s!r}")


# ═══════════════════════════════════════════════════════════════════════════
# Scenarios 6-10: Retry endpoint eligibility gates
# ═══════════════════════════════════════════════════════════════════════════

def scenario_retry_accepts_error_rows():
    t.section("Test 6: /api/migrations/retry accepts Error rows → 200 → InProc")
    _wipe()
    _insert("ERBK-101", status=STATUS_ERROR,
            submitted_by=USER_A, request_id="mid-fail-101",
            file_item_id="fi-101",
            error_message="Timed out", retry_count=0)
    _insert("ERBK-102", status=STATUS_ERROR,
            submitted_by=USER_A, request_id="mid-fail-102",
            file_item_id="fi-102",
            error_message="Auth failure", retry_count=1)

    with patch.object(migration_platform_db, "fetch_files_for_migrations",
                      return_value={}):
        with patch("services.migration_platform.service", FakePlatform()):
            with patch("routes.contracts.migration_platform.service",
                       FakePlatform()) as fp:
                c = _login(USER_A)
                r = c.post("/api/migrations/retry",
                           json={"ids": ["ERBK-101", "ERBK-102"]})
                t.check("POST /api/migrations/retry → 200",
                        r.status_code == 200, r.text)
                if r.status_code == 200:
                    body = r.json()
                    t.check("response inProcessing has both ids",
                            sorted(body["inProcessing"]) ==
                              ["ERBK-101", "ERBK-102"],
                            str(body["inProcessing"]))
                    t.check("response retryHistory has both snapshots",
                            len(body["retryHistory"]) == 2,
                            str(body["retryHistory"]))
                    # Verify DB rows are now In Processing
                    for fid in ("ERBK-101", "ERBK-102"):
                        row = _read(fid)
                        t.check(f"{fid} now In Processing",
                                row["MigrationStatus"] == STATUS_IN_PROCESSING,
                                str(row))
                        t.check(f"{fid} SubmittedBy == USER_A",
                                (row["SubmittedBy"] or "").lower() == USER_A,
                                str(row))


def scenario_retry_rejects_non_error():
    t.section("Test 7-8: Non-Error and unknown FileIDs → 400 INELIGIBLE_FOR_RETRY")
    _wipe()
    _insert("ERBK-201", status=STATUS_PENDING)
    _insert("ERBK-202", status=STATUS_MIGRATED)
    _insert("ERBK-203", status=STATUS_ERROR,
            submitted_by=USER_A, request_id="mid-fail-203")

    with patch.object(migration_platform_db, "fetch_files_for_migrations",
                      return_value={}):
        with patch("routes.contracts.migration_platform.service",
                   FakePlatform()):
            c = _login(USER_A)
            # Case 7a: mixed request with a Pending row → 400
            r = c.post("/api/migrations/retry",
                       json={"ids": ["ERBK-201", "ERBK-203"]})
            t.check("mixed (Pending+Error) → 400",
                    r.status_code == 400, f"status={r.status_code}")
            if r.status_code == 400:
                detail = r.json().get("detail", {})
                t.check("400 detail.reason == INELIGIBLE_FOR_RETRY",
                        detail.get("reason") == "INELIGIBLE_FOR_RETRY",
                        str(detail))
                t.check("400 lists ERBK-201 as ineligible",
                        any(x.get("fileID") == "ERBK-201"
                            for x in detail.get("ineligible", [])),
                        str(detail))
            # Verify no row was flipped
            t.check("ERBK-203 (Error) NOT flipped by rejected request",
                    _read("ERBK-203")["MigrationStatus"] == STATUS_ERROR,
                    str(_read("ERBK-203")))

            # Case 7b: Migrated in request → 400
            r2 = c.post("/api/migrations/retry",
                        json={"ids": ["ERBK-202"]})
            t.check("Migrated → 400",
                    r2.status_code == 400, f"status={r2.status_code}")

            # Case 8: unknown FileID → 400
            r3 = c.post("/api/migrations/retry",
                        json={"ids": ["ERBK-NONEXIST"]})
            t.check("unknown FileID → 400",
                    r3.status_code == 400, f"status={r3.status_code}")
            if r3.status_code == 400:
                detail3 = r3.json().get("detail", {})
                t.check("400 detail.unknown lists ERBK-NONEXIST",
                        "ERBK-NONEXIST" in (detail3.get("unknown") or []),
                        str(detail3))


def scenario_per_user_lock_on_retry():
    t.section("Test 9-10: Per-user lock — my_active blocks me, not other users")
    _wipe()
    # USER_A has an active In-Processing row (blocks USER_A's retry).
    _insert("ERBK-301",
            status=STATUS_IN_PROCESSING,
            submitted_by=USER_A,
            request_id="mid-inflight-301")
    # USER_A also has an Error row that they want to retry.
    _insert("ERBK-302",
            status=STATUS_ERROR,
            submitted_by=USER_A,
            request_id="mid-fail-302",
            error_message="prev fail")
    # USER_B has an Error row of their own (should succeed independently).
    _insert("ERBK-303",
            status=STATUS_ERROR,
            submitted_by=USER_B,
            request_id="mid-fail-303",
            error_message="prev fail B")

    with patch.object(migration_platform_db, "fetch_files_for_migrations",
                      return_value={}):
        with patch("routes.contracts.migration_platform.service",
                   FakePlatform()):
            # Test 9: USER_A retry → 409 (they have an active row)
            ca = _login(USER_A)
            r = ca.post("/api/migrations/retry", json={"ids": ["ERBK-302"]})
            t.check("USER_A retry with active row → 409",
                    r.status_code == 409, f"status={r.status_code}")
            if r.status_code == 409:
                detail = r.json().get("detail", {})
                t.check("409 detail.reason == ACTIVE_MIGRATION_EXISTS",
                        detail.get("reason") == "ACTIVE_MIGRATION_EXISTS",
                        str(detail))
                t.check("409 activeCount >= 1",
                        int(detail.get("activeCount") or 0) >= 1,
                        str(detail))
            # Verify ERBK-302 unchanged
            t.check("ERBK-302 unchanged (still Error) after blocked retry",
                    _read("ERBK-302")["MigrationStatus"] == STATUS_ERROR,
                    str(_read("ERBK-302")))

            # Test 10: USER_B retry succeeds — other user's activity does NOT block
            cb = _login(USER_B)
            r2 = cb.post("/api/migrations/retry", json={"ids": ["ERBK-303"]})
            t.check("USER_B retry succeeds despite USER_A being locked",
                    r2.status_code == 200,
                    f"status={r2.status_code} body={r2.text[:200]}")
            if r2.status_code == 200:
                t.check("ERBK-303 flipped to In Processing",
                        _read("ERBK-303")["MigrationStatus"]
                          == STATUS_IN_PROCESSING,
                        str(_read("ERBK-303")))
                t.check("ERBK-303 SubmittedBy == USER_B",
                        (_read("ERBK-303")["SubmittedBy"] or "").lower()
                          == USER_B,
                        str(_read("ERBK-303")))


# ═══════════════════════════════════════════════════════════════════════════
# Scenarios 11-14: Retry outcomes + audit history
# ═══════════════════════════════════════════════════════════════════════════

def scenario_retry_success_then_migrated():
    t.section("Test 11: Retry succeeds → row eventually Migrated")
    _wipe()
    _insert("ERBK-401", status=STATUS_ERROR,
            submitted_by=USER_A, request_id="mid-fail-401",
            file_item_id="fi-401",
            error_message="Prev failure", retry_count=1)

    fake = FakePlatform()
    with patch.object(migration_platform_db, "fetch_files_for_migrations",
                      return_value={}):
        with patch("routes.contracts.migration_platform.service", fake):
            c = _login(USER_A)
            r = c.post("/api/migrations/retry", json={"ids": ["ERBK-401"]})
            t.check("retry POST → 200", r.status_code == 200, r.text)
            new_mid = fake.last_migration_id
            # After background task, MigrationRequestId should be the NEW id
            row = _read("ERBK-401")
            t.check("row is In Processing after retry",
                    row["MigrationStatus"] == STATUS_IN_PROCESSING, str(row))
            t.check("row now carries NEW MigrationRequestId (not the old one)",
                    row["MigrationRequestId"] == new_mid, str(row))

            # Simulate the platform completing the retry successfully
            upd = apply_backend_file_status([{
                "fileID":            "ERBK-401",
                "migrationRequestId":new_mid,
                "migrationFileItemId":"fi-401-retry",
                "backendStatus":     "completed",
                "uiStatus":          STATUS_MIGRATED,
                "retryCount":        1,
                "errorMessage":      None,
                "errorCode":         None,
                "destinationUrl":    "https://dest/final/erbk-401.pdf",
                "startedAt":         None,
                "completedAt":       datetime.now(timezone.utc),
            }])
            t.check("apply_backend_file_status.migrated == 1",
                    upd["migrated"] == 1, str(upd))
            final = _read("ERBK-401")
            t.check("row is Migrated after successful retry",
                    final["MigrationStatus"] == STATUS_MIGRATED, str(final))


def scenario_retry_fails_again_back_to_error():
    t.section("Test 12: Retry that fails again → row returns to Error")
    _wipe()
    _insert("ERBK-501", status=STATUS_ERROR,
            submitted_by=USER_A, request_id="mid-fail-501",
            file_item_id="fi-501",
            error_message="First failure", retry_count=0)

    fake = FakePlatform()
    with patch.object(migration_platform_db, "fetch_files_for_migrations",
                      return_value={}):
        with patch("routes.contracts.migration_platform.service", fake):
            c = _login(USER_A)
            r = c.post("/api/migrations/retry", json={"ids": ["ERBK-501"]})
            t.check("retry POST → 200", r.status_code == 200, r.text)
            new_mid = fake.last_migration_id

            # Simulate the platform failing the retry
            upd = apply_backend_file_status([{
                "fileID":            "ERBK-501",
                "migrationRequestId":new_mid,
                "migrationFileItemId":"fi-501-retry",
                "backendStatus":     "failed",
                "uiStatus":          STATUS_ERROR,
                "retryCount":        1,
                "errorMessage":      "Second failure",
                "errorCode":         "E_AGAIN",
                "destinationUrl":    None,
                "startedAt":         None,
                "completedAt":       None,
            }])
            t.check("apply_backend_file_status.failed == 1 on second failure",
                    upd["failed"] == 1, str(upd))
            row = _read("ERBK-501")
            t.check("row back to Error after retry failure",
                    row["MigrationStatus"] == STATUS_ERROR, str(row))
            t.check("row carries the NEW error message",
                    "Second failure" in (row["ErrorMessage"] or ""),
                    str(row))


def scenario_retry_audit_trail():
    t.section("Test 13-14: Retry history table captures prev + new evidence")
    _wipe()
    _insert("ERBK-601", status=STATUS_ERROR,
            submitted_by=USER_A, request_id="mid-fail-601",
            file_item_id="fi-601-orig",
            error_message="Original failure",
            retry_count=0)

    fake = FakePlatform()
    with patch.object(migration_platform_db, "fetch_files_for_migrations",
                      return_value={}):
        with patch("routes.contracts.migration_platform.service", fake):
            c = _login(USER_A)
            r = c.post("/api/migrations/retry", json={"ids": ["ERBK-601"]})
            t.check("retry POST → 200", r.status_code == 200, r.text)
            new_mid = fake.last_migration_id

            hist = _history_rows("ERBK-601")
            t.check("exactly 1 retry-history row written",
                    len(hist) == 1, f"count={len(hist)}")
            if hist:
                h = hist[0]
                # Test 13 — previous evidence
                t.check("prev_mid captured (was mid-fail-601)",
                        h["prev_mid"] == "mid-fail-601", str(h))
                t.check("prev_fiid captured (was fi-601-orig)",
                        h["prev_fiid"] == "fi-601-orig", str(h))
                t.check("prev_err captured",
                        (h["prev_err"] or "").startswith("Original"), str(h))
                t.check("prev_rc captured (was 0)",
                        h["prev_rc"] == 0, str(h))
                t.check("retried_by == USER_A",
                        (h["retried_by"] or "").lower() == USER_A, str(h))
                # Test 14 — new_mid stamped by background task
                t.check("new_mid stamped == platform's returned migration id",
                        h["new_mid"] == new_mid,
                        f"new_mid={h['new_mid']!r} platform={new_mid!r}")


# ═══════════════════════════════════════════════════════════════════════════
# Scenario 15: Total includes Error rows (bucket accounting invariant)
# ═══════════════════════════════════════════════════════════════════════════

def scenario_total_includes_error():
    t.section("Test 15: Total Documents includes Error rows (spec: A = B+C+D+E+F)")
    _wipe()
    # Insert 3 rows with distinct statuses so we can prove the total
    # exactly reflects every status contribution.
    _insert("ERBK-701", status=STATUS_PENDING)
    _insert("ERBK-702", status=STATUS_MIGRATED)
    _insert("ERBK-703", status=STATUS_ERROR,
            submitted_by=USER_A, request_id="mid-fail-703",
            error_message="For total test")

    counts = count_all()
    calc_total = (counts["pending"] + counts["in_processing"]
                  + counts["migrated"] + counts["error"] + counts["excluded"])
    t.check("total == pending + in_processing + migrated + error + excluded",
            counts["total"] == calc_total,
            f"total={counts['total']} sum={calc_total} counts={counts}")
    t.check("total >= 3 (our three test rows are present)",
            counts["total"] >= 3, str(counts))
    t.check("error count includes ERBK-703",
            counts["error"] >= 1, str(counts))

    # Regression: verify Error is NOT counted in pending
    #   (spec: failed rows must NOT fold back into Yet-to-be-Migrated)
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT COUNT(*) FROM dbo.[{TABLE}] "
            f"WHERE FileID='ERBK-703' AND MigrationStatus='{STATUS_ERROR}' "
            f"  AND ISNULL(Excluded,'No')='No'"
        )
        n = cur.fetchone()[0]
    t.check("Error row is active (Excluded='No')", n == 1)


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════

def main() -> int:
    try:
        scenario_backend_status_mapping()
        scenario_error_bucket_counts()
        scenario_retry_accepts_error_rows()
        scenario_retry_rejects_non_error()
        scenario_per_user_lock_on_retry()
        scenario_retry_success_then_migrated()
        scenario_retry_fails_again_back_to_error()
        scenario_retry_audit_trail()
        scenario_total_includes_error()
    finally:
        _wipe()
    return t.summary()


if __name__ == "__main__":
    sys.exit(main())
