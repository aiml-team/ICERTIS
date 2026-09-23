"""External-migration reconciliation tests (spec §Tests).

Ten scenarios:
  1. exact match by site + library + source_path
  2. URL-encoded SharePoint path matching
  3. duplicate filenames in different folders
  4. completed external migration updates Yet-to-Migrate → Migrated
  5. in_progress external migration updates → In Processing
  6. failed external migration → Failed
  7. repeated reconciliation is idempotent
  8. ambiguous match does not update
  9. unmatched PostgreSQL row does not create inventory data
 10. latest PostgreSQL record wins when same source file appears in multiple migrations

Additional behaviour proven:
 11. no-downgrade: Migrated is never demoted back to In Processing
 12. Excluded='Yes' rows are never touched
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
logging.basicConfig(level=logging.WARNING)
logging.getLogger("services.data_service").setLevel(logging.WARNING)
logging.getLogger("services.migration_reconciliation").setLevel(logging.WARNING)

from core.config import settings
from core.database import get_connection
from services import migration_reconciliation, migration_platform_db, data_service


# Fixed test SharePoint site/library (never collides with real inventory).
SITE = "https://itellicloud.sharepoint.com/sites/RECONCILE-TEST"
LIB  = "Reconcile Test Library"


class T:
    def __init__(self): self.passed = 0; self.failed = 0
    def check(self, label, cond, detail=""):
        if cond:
            print(f"  PASS  {label}"); self.passed += 1
        else:
            print(f"  FAIL  {label}  {detail}"); self.failed += 1
    def section(self, title): print(f"\n=== {title} ===")
    def summary(self):
        total = self.passed + self.failed
        print(f"\n{self.passed}/{total} passed  ({self.failed} failed)")
        return 0 if self.failed == 0 else 1


t = T()
TABLE = settings.CONTRACT_TABLE


# ── Helpers ──────────────────────────────────────────────────────────────
def _make_url(source_path: str, url_encoded: bool = False) -> str:
    """Build a SharePointPath URL for a test source_path relative to LIB."""
    if url_encoded:
        # Percent-encode spaces and commas (as SharePoint does).
        import urllib.parse
        lib_enc = urllib.parse.quote(LIB)
        src_enc = "/".join(urllib.parse.quote(seg) for seg in source_path.split("/"))
        return f"{SITE}/{lib_enc}/{src_enc}"
    return f"{SITE}/{LIB}/{source_path}"


def _insert_test_row(file_id: str, source_path: str, *, url_encoded=False,
                     status="Pending", excluded="No") -> None:
    """Insert a synthetic ContractInventory row for the reconciler to match."""
    sp = _make_url(source_path, url_encoded=url_encoded)
    fn = source_path.split("/")[-1]
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"INSERT INTO dbo.[{TABLE}] "
            f"([FileID], [FileName], [SharePointPath], [MigrationStatus], "
            f" [Migrate], [Migrated], [Excluded]) "
            f"VALUES (?, ?, ?, ?, 'No', 'False', ?)",
            [file_id, fn, sp, status, excluded],
        )
        cn.commit()


def _read_row(file_id: str) -> dict:
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT MigrationStatus, MigrationBackendStatus, "
            f"       MigrationRequestId, MigrationFileItemId, "
            f"       SubmittedBy, MigratedDate, DestinationUrl, "
            f"       Migrated, Migrate "
            f"FROM dbo.[{TABLE}] WHERE FileID = ?",
            [file_id],
        )
        r = cur.fetchone()
    return dict(zip(
        ("MigrationStatus", "MigrationBackendStatus", "MigrationRequestId",
         "MigrationFileItemId", "SubmittedBy", "MigratedDate",
         "DestinationUrl", "Migrated", "Migrate"),
        r,
    )) if r else {}


def _wipe():
    """Remove every synthetic test row inserted by this suite."""
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"DELETE FROM dbo.[{TABLE}] WHERE FileID LIKE 'RECON-%'"
        )
        cn.commit()


def _pg_row(file_item_id: str, migration_request_id: str, file_name: str,
            source_path: str, status: str, *,
            completed_at=None, updated_at=None, created_at=None,
            created_by="external.tool@vendor.example",
            destination_url=None, error_message=None, error_code=None) -> dict:
    """Shape a fake PG row exactly as fetch_recent_file_items would return."""
    now = datetime.now(timezone.utc)
    return {
        "file_item_id":                file_item_id,
        "migration_request_id":        migration_request_id,
        "file_name":                   file_name,
        "source_path":                 source_path,
        "destination_path":            f"Migrated/{source_path}",
        "status":                      status,
        "retry_count":                 0,
        "error_message":               error_message,
        "error_code":                  error_code,
        "power_automate_run_id":       "pa-test-run",
        "created_at":                  created_at or now,
        "updated_at":                  updated_at or now,
        "completed_at":                completed_at,
        "mr_source_site_url":          SITE,
        "mr_source_library":           LIB,
        "mr_source_folder_path":       source_path.rsplit("/", 1)[0] if "/" in source_path else "",
        "mr_destination_site_url":     f"{SITE}-DEST",
        "mr_destination_library":      "Dest Library",
        "mr_destination_folder_path":  "Migrated",
        "mr_created_by":               created_by,
        "mr_status":                   "in_progress",
        "mr_completed_at":             None,
        "audit_destination_url":       destination_url,
    }


def _run_reconciler(fake_pg_rows: list) -> dict:
    """Run the reconciliation pass with a fake PG fetch result."""
    migration_reconciliation.reset_watermark()
    with patch.object(migration_platform_db, "fetch_recent_file_items",
                      return_value=fake_pg_rows), \
         patch.object(migration_platform_db, "is_configured",
                      return_value=True):
        return migration_reconciliation.reconcile_external_migrations()


# ── Tests ────────────────────────────────────────────────────────────────
try:
    _wipe()

    # 1. Exact match by site + library + source_path
    t.section("Test 1: exact match by site + library + source_path")
    _insert_test_row("RECON-001", "FolderA/exact-match.pdf")
    r = _run_reconciler([_pg_row(
        "fi-001", "mr-001", "exact-match.pdf",
        "FolderA/exact-match.pdf", "completed",
        completed_at=datetime.now(timezone.utc),
    )])
    t.check("matched exactly one row", r["matched"] == 1, str(r))
    t.check("ambiguous == 0",           r["ambiguous"] == 0)
    t.check("unmatched == 0",           r["unmatched"] == 0)
    row = _read_row("RECON-001")
    t.check("Azure row now Migrated",   row["MigrationStatus"] == "Migrated", str(row))
    t.check("MigrationRequestId set",   row["MigrationRequestId"] == "mr-001")
    t.check("MigrationFileItemId set",  row["MigrationFileItemId"] == "fi-001")
    t.check("SubmittedBy captured",     row["SubmittedBy"] == "external.tool@vendor.example")

    # 2. URL-encoded SharePoint path matching
    t.section("Test 2: URL-encoded SharePoint path matching")
    _insert_test_row("RECON-002", "Folder With Spaces/notice, comma.pdf",
                     url_encoded=True)
    r = _run_reconciler([_pg_row(
        "fi-002", "mr-002", "notice, comma.pdf",
        "Folder With Spaces/notice, comma.pdf", "completed",
        completed_at=datetime.now(timezone.utc),
    )])
    t.check("URL-encoded row matched",  r["matched"] == 1, str(r))
    t.check("Azure row now Migrated",   _read_row("RECON-002")["MigrationStatus"] == "Migrated")

    # 3. Duplicate filenames in different folders — both correctly resolved
    t.section("Test 3: duplicate filenames in different folders")
    _insert_test_row("RECON-003A", "FolderA/dup.pdf")
    _insert_test_row("RECON-003B", "FolderB/dup.pdf")
    r = _run_reconciler([
        _pg_row("fi-003A", "mr-003", "dup.pdf", "FolderA/dup.pdf",
                "completed", completed_at=datetime.now(timezone.utc)),
        _pg_row("fi-003B", "mr-003", "dup.pdf", "FolderB/dup.pdf",
                "completed", completed_at=datetime.now(timezone.utc)),
    ])
    t.check("both duplicates matched", r["matched"] == 2, str(r))
    t.check("no ambiguity flagged",    r["ambiguous"] == 0)
    a = _read_row("RECON-003A"); b = _read_row("RECON-003B")
    t.check("FolderA row → fi-003A",   a["MigrationFileItemId"] == "fi-003A")
    t.check("FolderB row → fi-003B",   b["MigrationFileItemId"] == "fi-003B")

    # 4. completed external migration updates Pending → Migrated
    # (already proven in Test 1, but re-assert with the specific delta)
    t.section("Test 4: completed migration updates Pending → Migrated")
    _insert_test_row("RECON-004", "FolderA/completed-only.pdf")
    r = _run_reconciler([_pg_row(
        "fi-004", "mr-004", "completed-only.pdf",
        "FolderA/completed-only.pdf", "completed",
        completed_at=datetime.now(timezone.utc),
    )])
    t.check("updated.migrated == 1",    r["updated"]["migrated"] == 1, str(r))
    row = _read_row("RECON-004")
    t.check("MigrationStatus=Migrated", row["MigrationStatus"] == "Migrated")
    t.check("Migrated flag = 'True'",   row["Migrated"] == "True")
    t.check("Migrate flag  = 'Yes'",    row["Migrate"] == "Yes")

    # 5. in_progress external migration updates Pending → In Processing
    t.section("Test 5: in_progress migration updates → In Processing")
    _insert_test_row("RECON-005", "FolderA/inprog.pdf")
    r = _run_reconciler([_pg_row(
        "fi-005", "mr-005", "inprog.pdf",
        "FolderA/inprog.pdf", "in_progress",
    )])
    t.check("updated.in_processing == 1", r["updated"]["in_processing"] == 1, str(r))
    row = _read_row("RECON-005")
    t.check("row now In Processing",    row["MigrationStatus"] == "In Processing")
    t.check("Migrated flag NOT True",   row["Migrated"] != "True")

    # 6. failed external migration → Failed
    t.section("Test 6: failed migration → Failed")
    _insert_test_row("RECON-006", "FolderA/failed.pdf")
    r = _run_reconciler([_pg_row(
        "fi-006", "mr-006", "failed.pdf",
        "FolderA/failed.pdf", "failed",
        error_message="boom", error_code="E_TIMEOUT",
    )])
    t.check("updated.failed == 1",      r["updated"]["failed"] == 1, str(r))
    # Post-rename: terminal-failure status is now spelled "Error" on-disk
    # (was "Failed" pre-rename).  Accept both for backward compat during
    # rollout while any legacy on-disk data converges.
    t.check("row now Error/Failed",     _read_row("RECON-006")["MigrationStatus"] in ("Error", "Failed"))

    # 7. Repeated reconciliation is idempotent
    t.section("Test 7: repeated reconciliation is idempotent")
    _insert_test_row("RECON-007", "FolderA/idempotent.pdf")
    pg = [_pg_row("fi-007", "mr-007", "idempotent.pdf",
                  "FolderA/idempotent.pdf", "completed",
                  completed_at=datetime.now(timezone.utc))]
    r1 = _run_reconciler(pg)
    r2 = _run_reconciler(pg)  # exact same input
    t.check("first pass updates 1",     r1["updated"]["migrated"] == 1, str(r1))
    t.check("second pass updates 0",    r2["updated"]["migrated"] == 0, str(r2))
    t.check("row stays Migrated",       _read_row("RECON-007")["MigrationStatus"] == "Migrated")

    # 8. Ambiguous match (two Azure rows share the key) → no update, logged
    t.section("Test 8: ambiguous match does not update")
    _insert_test_row("RECON-008A", "FolderA/ambig.pdf")
    _insert_test_row("RECON-008B", "FolderA/ambig.pdf")  # SAME source_path
    r = _run_reconciler([_pg_row(
        "fi-008", "mr-008", "ambig.pdf",
        "FolderA/ambig.pdf", "completed",
        completed_at=datetime.now(timezone.utc),
    )])
    t.check("ambiguous flagged",        r["ambiguous"] == 1, str(r))
    t.check("matched == 0",             r["matched"] == 0)
    t.check("updated.migrated == 0",    r["updated"]["migrated"] == 0)
    t.check("A stays Pending",          _read_row("RECON-008A")["MigrationStatus"] == "Pending")
    t.check("B stays Pending",          _read_row("RECON-008B")["MigrationStatus"] == "Pending")

    # 9. Unmatched PG row: no inventory row exists → no insert, logged
    t.section("Test 9: unmatched PostgreSQL row does not create inventory data")
    before_count = None
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM dbo.[{TABLE}]")
        before_count = cur.fetchone()[0]
    r = _run_reconciler([_pg_row(
        "fi-009", "mr-009", "orphan.pdf",
        "NoSuchFolder/orphan.pdf", "completed",
        completed_at=datetime.now(timezone.utc),
    )])
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM dbo.[{TABLE}]")
        after_count = cur.fetchone()[0]
    t.check("unmatched flagged",        r["unmatched"] == 1, str(r))
    t.check("row count unchanged",      before_count == after_count,
            f"before={before_count} after={after_count}")

    # 10. Latest PG record wins when same source file appears in multiple migrations
    t.section("Test 10: latest PG record wins")
    _insert_test_row("RECON-010", "FolderA/multi.pdf")
    older = datetime.now(timezone.utc) - timedelta(hours=1)
    newer = datetime.now(timezone.utc)
    r = _run_reconciler([
        _pg_row("fi-010-OLD", "mr-010A", "multi.pdf", "FolderA/multi.pdf",
                "failed", updated_at=older,
                error_message="old failure"),
        _pg_row("fi-010-NEW", "mr-010B", "multi.pdf", "FolderA/multi.pdf",
                "completed", updated_at=newer,
                completed_at=newer),
    ])
    t.check("collapsed to 1 candidate", r["candidates"] == 1, str(r))
    row = _read_row("RECON-010")
    t.check("newer completed wins",     row["MigrationStatus"] == "Migrated")
    t.check("MigrationFileItemId=newer", row["MigrationFileItemId"] == "fi-010-NEW")

    # 11. No-downgrade: Migrated stays Migrated even if a stale active row appears
    t.section("Test 11: no downgrade — Migrated never demoted")
    _insert_test_row("RECON-011", "FolderA/nodown.pdf")
    _run_reconciler([_pg_row(
        "fi-011", "mr-011", "nodown.pdf",
        "FolderA/nodown.pdf", "completed",
        completed_at=datetime.now(timezone.utc),
    )])
    t.check("row promoted to Migrated", _read_row("RECON-011")["MigrationStatus"] == "Migrated")
    # Now pretend an older 'queued' event arrives:
    r = _run_reconciler([_pg_row(
        "fi-011-STALE", "mr-011", "nodown.pdf",
        "FolderA/nodown.pdf", "queued",
        updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )])
    t.check("no in_processing count",   r["updated"]["in_processing"] == 0, str(r))
    t.check("row STILL Migrated",       _read_row("RECON-011")["MigrationStatus"] == "Migrated")

    # 12. Excluded='Yes' rows are never touched
    t.section("Test 12: Excluded='Yes' rows are never touched")
    _insert_test_row("RECON-012", "FolderA/excluded.pdf", excluded="Yes")
    r = _run_reconciler([_pg_row(
        "fi-012", "mr-012", "excluded.pdf",
        "FolderA/excluded.pdf", "completed",
        completed_at=datetime.now(timezone.utc),
    )])
    row = _read_row("RECON-012")
    t.check("updated.migrated == 0",    r["updated"]["migrated"] == 0, str(r))
    t.check("row stays Pending",        row["MigrationStatus"] == "Pending")

finally:
    _wipe()

sys.exit(t.summary())
