"""End-to-end test for the unified-inventory Excluded refactor
(2026-09-22).

Exercises the LIVE Azure SQL DB directly through the FastAPI TestClient
+ the data_service module.  No migration platform mocking is needed
because these paths never call it.

Covers the five cases the manager specified:

    1. Exclude a Pending file:
       Pending -1, Excluded +1, Total unchanged.
    2. Export bucket=Total (via /api/contracts?include_excluded=1):
       response contains the excluded row.
    3. Export bucket=Excluded (via /api/excluded):
       response contains only Excluded='Yes' rows.
    4. Restore the same file:
       Excluded -1, Pending +1, Total unchanged.
    5. Exclude → Restore → Exclude:
       no duplicate inventory row (single row on the master table).

Also verifies:
    * The row physically stays on ContractInventory across the Exclude
      cycle (no INSERT/DELETE dance).
    * `count_all()` reports total = active + excluded.
    * `mark_in_processing` refuses excluded rows (regression guard).

Run:  MYENV/bin/python scripts/test_excluded_unified_inventory.py
"""
from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path
from typing import List

# Make repo root importable.
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.WARNING)
logging.getLogger("core.database").setLevel(logging.WARNING)
logging.getLogger("services.data_service").setLevel(logging.WARNING)

from fastapi.testclient import TestClient

from core.config import settings
from core.database import get_connection
from services import data_service
from services.data_service import (
    STATUS_IN_PROCESSING,
    STATUS_PENDING,
    count_all,
    mark_excluded,
    mark_in_processing,
    restore_excluded,
)


# ── Test harness ──────────────────────────────────────────────────────────
class T:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, label: str, cond: bool, detail: str = ""):
        if cond:
            print(f"  PASS  {label}")
            self.passed += 1
        else:
            print(f"  FAIL  {label}  {detail}")
            self.failed += 1

    def section(self, title: str):
        print(f"\n=== {title} ===")

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{self.passed}/{total} passed  ({self.failed} failed)")
        return 0 if self.failed == 0 else 1


t = T()


# ── DB helpers ────────────────────────────────────────────────────────────
def pick_pending_id() -> str:
    """Return a single FileID currently in Pending state and not excluded."""
    table = settings.CONTRACT_TABLE
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT TOP 1 [FileID] FROM dbo.[{table}] "
            f"WHERE ISNULL([MigrationStatus], '{STATUS_PENDING}') = '{STATUS_PENDING}' "
            f"  AND ISNULL([Excluded], 'No') = 'No'"
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("No Pending / non-excluded rows available for test.")
        return str(row[0])


def force_pending(file_id: str) -> None:
    """Return a specific row to a clean Pending / Excluded='No' baseline."""
    table = settings.CONTRACT_TABLE
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"UPDATE dbo.[{table}] "
            f"SET [MigrationStatus] = ?, "
            f"    [Migrate] = 'No', [Migrated] = 'False', [MigratedDate] = NULL, "
            f"    [MigrationRequestId] = NULL, [MigrationFileItemId] = NULL, "
            f"    [MigrationBackendStatus] = NULL, [MigrationRetryCount] = NULL, "
            f"    [MigrationErrorCode] = NULL, [MigrationSubmittedAt] = NULL, "
            f"    [MigrationLastSyncedAt] = NULL, [DestinationUrl] = NULL, "
            f"    [ErrorMessage] = NULL, [RunId] = NULL, "
            f"    [Excluded] = 'No', [ExcludedDate] = NULL, [ExcludedBy] = NULL "
            f"WHERE [FileID] = ?",
            [STATUS_PENDING, file_id],
        )
        cn.commit()


def inventory_row_count(file_id: str) -> int:
    """Physical row-count for a FileID on the master table (should be 1)."""
    table = settings.CONTRACT_TABLE
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM dbo.[{table}] WHERE [FileID] = ?", [file_id])
        return int(cur.fetchone()[0])


def excluded_flag(file_id: str) -> str:
    """Return the Excluded column value for a FileID ('Yes' / 'No')."""
    table = settings.CONTRACT_TABLE
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT ISNULL([Excluded], 'No') FROM dbo.[{table}] WHERE [FileID] = ?",
            [file_id],
        )
        row = cur.fetchone()
        return str(row[0]) if row else ""


# ── Login helper ──────────────────────────────────────────────────────────
def login_client() -> TestClient:
    from main import app
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": "test@bs.nttdata.com"})
    assert r.status_code == 200, r.text
    return c


# ── Main test flow ────────────────────────────────────────────────────────
def run():
    t.section("Setup: pick a Pending row + baseline counts")
    # Trigger the one-shot schema migration (adds ExcludedDate / ExcludedBy
    # + back-migrates any legacy excluded rows).  count_all() is the
    # lightest data_service call that fires _ensure_schema_once().
    _ = count_all()
    fid = pick_pending_id()
    # Guarantee a clean baseline in case a prior aborted run left this row
    # in some other state.
    force_pending(fid)
    print(f"  → using FileID: {fid}")

    c0 = count_all()
    total_0    = int(c0.get("total", 0))
    pending_0  = int(c0.get("pending", 0))
    excluded_0 = int(c0.get("excluded", 0))
    active_0   = int(c0.get("active", 0))
    print(f"  baseline: total={total_0}  active={active_0}  "
          f"pending={pending_0}  excluded={excluded_0}")

    t.check("baseline: total == active + excluded",
            total_0 == active_0 + excluded_0,
            f"total={total_0} active={active_0} excluded={excluded_0}")
    t.check("baseline: row physically present on master (count=1)",
            inventory_row_count(fid) == 1)
    t.check("baseline: Excluded flag is 'No'", excluded_flag(fid) == "No")

    # ── Case 1: Exclude a Pending file ───────────────────────────────────
    t.section("Case 1: Exclude the Pending file")
    res = mark_excluded([fid], excluded_by="test-runner", reason="unit-test")
    t.check("mark_excluded returned rowsAffected=1",
            res.get("rowsAffected") == 1, str(res))
    t.check("mark_excluded returned FileID in succeeded",
            fid in res.get("succeeded", []), str(res))

    c1 = count_all()
    total_1    = int(c1.get("total", 0))
    pending_1  = int(c1.get("pending", 0))
    excluded_1 = int(c1.get("excluded", 0))

    t.check("after exclude: Total unchanged",
            total_1 == total_0, f"{total_1} vs {total_0}")
    t.check("after exclude: Pending decremented by 1",
            pending_1 == pending_0 - 1, f"{pending_1} vs {pending_0}")
    t.check("after exclude: Excluded incremented by 1",
            excluded_1 == excluded_0 + 1, f"{excluded_1} vs {excluded_0}")
    t.check("after exclude: total = active + excluded",
            total_1 == int(c1.get("active", 0)) + excluded_1)
    t.check("after exclude: row STILL physically on master (no INSERT/DELETE)",
            inventory_row_count(fid) == 1)
    t.check("after exclude: Excluded flag = 'Yes'",
            excluded_flag(fid) == "Yes")

    # Regression guard: mark_in_processing must refuse excluded rows.
    res_ip = mark_in_processing([fid])
    t.check("mark_in_processing refuses excluded row (0 affected)",
            (res_ip.get("rowsAffected") or 0) == 0, str(res_ip))
    t.check("mark_in_processing did not flip status",
            excluded_flag(fid) == "Yes")

    # ── Case 2: /api/contracts?include_excluded=1 contains the excluded row ─
    t.section("Case 2: Total Documents export includes excluded")
    client = login_client()

    r_all = client.get("/api/contracts?include_excluded=1")
    t.check("GET /api/contracts?include_excluded=1 → 200",
            r_all.status_code == 200, r_all.text[:200])
    data_all = r_all.json().get("data") or []
    ids_all  = {str(r.get("fileID")) for r in data_all}
    t.check("union response contains the excluded file id",
            fid in ids_all)
    excl_row = next((r for r in data_all if str(r.get("fileID")) == fid), None)
    t.check("union row carries excluded='Yes'",
            (excl_row or {}).get("excluded") == "Yes",
            str(excl_row))

    # Default (no query param) must still hide excluded rows.
    r_active = client.get("/api/contracts")
    t.check("GET /api/contracts (default) → 200",
            r_active.status_code == 200, r_active.text[:200])
    ids_active = {str(r.get("fileID")) for r in (r_active.json().get("data") or [])}
    t.check("default /api/contracts does NOT contain excluded id",
            fid not in ids_active)

    # ── Case 3: /api/excluded contains only excluded rows ────────────────
    t.section("Case 3: Excluded export contains only Excluded='Yes'")
    r_ex = client.get("/api/excluded")
    t.check("GET /api/excluded → 200",
            r_ex.status_code == 200, r_ex.text[:200])
    data_ex = r_ex.json().get("data") or []
    ids_ex  = {str(r.get("fileID")) for r in data_ex}
    t.check("/api/excluded contains the excluded id", fid in ids_ex)
    # Verify no active-side FileID leaked in.
    leaked = ids_ex & ids_active
    t.check("/api/excluded has no overlap with active list",
            not leaked, f"leaked={leaked}")

    # ── Case 4: Restore the file ─────────────────────────────────────────
    t.section("Case 4: Restore the file")
    res_r = restore_excluded([fid], restored_by="test-runner")
    t.check("restore_excluded rowsAffected=1",
            res_r.get("rowsAffected") == 1, str(res_r))

    c2 = count_all()
    total_2    = int(c2.get("total", 0))
    pending_2  = int(c2.get("pending", 0))
    excluded_2 = int(c2.get("excluded", 0))

    t.check("after restore: Total unchanged",
            total_2 == total_0, f"{total_2} vs {total_0}")
    t.check("after restore: Pending back to baseline",
            pending_2 == pending_0, f"{pending_2} vs {pending_0}")
    t.check("after restore: Excluded back to baseline",
            excluded_2 == excluded_0, f"{excluded_2} vs {excluded_0}")
    t.check("after restore: Excluded flag = 'No'",
            excluded_flag(fid) == "No")
    t.check("after restore: row STILL physically on master (count=1)",
            inventory_row_count(fid) == 1)

    # ── Case 5: Exclude → Restore → Exclude leaves ONE physical row ──────
    t.section("Case 5: Exclude → Restore → Exclude — no duplicates")
    mark_excluded([fid], excluded_by="test-runner", reason="cycle-1")
    t.check("cycle: excluded first time (row count=1)",
            inventory_row_count(fid) == 1)
    restore_excluded([fid], restored_by="test-runner")
    t.check("cycle: restored (row count=1)",
            inventory_row_count(fid) == 1)
    mark_excluded([fid], excluded_by="test-runner", reason="cycle-2")
    t.check("cycle: excluded second time (row count=1, no duplicates)",
            inventory_row_count(fid) == 1)
    t.check("cycle: Excluded flag = 'Yes' at end",
            excluded_flag(fid) == "Yes")

    # ── Cleanup: restore the row so the test is idempotent ───────────────
    t.section("Cleanup")
    restore_excluded([fid], restored_by="test-runner")
    force_pending(fid)
    t.check("cleanup: row back to Pending / Excluded='No'",
            excluded_flag(fid) == "No" and inventory_row_count(fid) == 1)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        print("\n!! UNCAUGHT EXCEPTION")
        traceback.print_exc()
        t.failed += 1
    sys.exit(t.summary())
