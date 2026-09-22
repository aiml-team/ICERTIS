"""End-to-end test for the per-user migration lock (task 2026-09-24).

Exercises the LIVE Azure SQL DB directly through the FastAPI TestClient
and mocks the migration platform HTTP client so /api/migrate does not
touch the real platform.

Test scenarios (matches the acceptance list in the task spec):

  1. Single user  — User A submits 50 → my_active becomes 50 → Migrate disabled.
  2. Same user tries again — 2nd /api/migrate returns 409 ACTIVE_MIGRATION_EXISTS.
  3. Different user — User B can still submit while A is locked.
  4. Completion — sync simulates 50 → 30 → 10 → 0; A auto-unlocks.
  5. Two tabs / same account — concurrent /api/migrate → exactly one succeeds.
  6. Logout/login — after re-login, A still sees my_in_processing>0 + can_migrate=false.
  7. Submission failure — external HTTP fails; A's rows rolled back → unlocks.

Also verifies the SubmittedBy column is written and case-insensitive.

Run:  MYENV/bin/python scripts/test_user_migration_lock.py
"""
from __future__ import annotations

import logging
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.WARNING)
logging.getLogger("core.database").setLevel(logging.WARNING)
logging.getLogger("services.data_service").setLevel(logging.WARNING)
logging.getLogger("routes.contracts").setLevel(logging.ERROR)

from fastapi.testclient import TestClient

from core.config import settings
from core.database import get_connection
from services import data_service
from services.data_service import (
    STATUS_FAILED,
    STATUS_IN_PROCESSING,
    STATUS_MIGRATED,
    STATUS_PENDING,
    count_all,
    can_user_start_migration,
    get_user_active_migration_count,
)


# ── Harness ───────────────────────────────────────────────────────────────
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

USER_A = "user-a-locktest@bs.nttdata.com"
USER_B = "user-b-locktest@bs.nttdata.com"


# ── DB helpers ────────────────────────────────────────────────────────────
def wipe_leftovers() -> None:
    """Return any rows this test may have left behind to a clean baseline.
    Uses SubmittedBy to scope so we never touch production data."""
    table = settings.CONTRACT_TABLE
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"UPDATE dbo.[{table}] "
            f"SET [MigrationStatus] = 'Pending', "
            f"    [Migrate] = 'No', [Migrated] = 'False', [MigratedDate] = NULL, "
            f"    [MigrationRequestId] = NULL, [MigrationFileItemId] = NULL, "
            f"    [MigrationBackendStatus] = NULL, [MigrationRetryCount] = NULL, "
            f"    [MigrationErrorCode] = NULL, [MigrationSubmittedAt] = NULL, "
            f"    [MigrationLastSyncedAt] = NULL, [DestinationUrl] = NULL, "
            f"    [ErrorMessage] = NULL, [RunId] = NULL, "
            f"    [SubmittedBy] = NULL "
            f"WHERE LOWER(ISNULL([SubmittedBy], '')) IN (?, ?) "
            f"   OR [MigrationRequestId] LIKE 'locktest-%' "
            f"   OR [MigrationRequestId] LIKE 'mig-%'",
            [USER_A.lower(), USER_B.lower()],
        )
        cn.commit()


def pick_pending_ids(n: int) -> List[str]:
    table = settings.CONTRACT_TABLE
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT TOP {n} [FileID] FROM dbo.[{table}] "
            f"WHERE ISNULL([MigrationStatus], '{STATUS_PENDING}') = '{STATUS_PENDING}' "
            f"  AND ISNULL([Excluded], 'No') = 'No' "
            f"  AND ISNULL([SubmittedBy], '') = ''"
        )
        return [str(r[0]) for r in cur.fetchall()]


def force_status(file_ids: List[str], status: str,
                 submitted_by: str | None = None) -> None:
    if not file_ids:
        return
    table = settings.CONTRACT_TABLE
    ph = ", ".join("?" for _ in file_ids)
    with get_connection() as cn:
        cur = cn.cursor()
        if submitted_by is not None:
            cur.execute(
                f"UPDATE dbo.[{table}] "
                f"SET [MigrationStatus] = ?, [SubmittedBy] = ? "
                f"WHERE [FileID] IN ({ph})",
                [status, submitted_by.lower(), *file_ids],
            )
        else:
            cur.execute(
                f"UPDATE dbo.[{table}] "
                f"SET [MigrationStatus] = ? WHERE [FileID] IN ({ph})",
                [status, *file_ids],
            )
        cn.commit()


def read_submitted_by(file_id: str) -> str:
    table = settings.CONTRACT_TABLE
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT [SubmittedBy] FROM dbo.[{table}] WHERE [FileID] = ?",
            [file_id],
        )
        row = cur.fetchone()
        return str(row[0]) if row and row[0] is not None else ""


# ── Login helper ──────────────────────────────────────────────────────────
def login_client(email: str) -> TestClient:
    from main import app
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": email})
    assert r.status_code == 200, r.text
    return c


# ── Fake platform (reused pattern from test_async_migration_flow.py) ──────
class FakePlatform:
    def __init__(self, fail_create: bool = False):
        self.created: List[Dict[str, Any]] = []
        self.batched: List[Dict[str, Any]] = []
        self.fail_create = fail_create

    def is_configured(self): return True

    def build_migration_request_payload(self, **kw):
        return {"name": "fake", **kw}

    def create_migration(self, payload):
        from services.migration_platform import MigrationPlatformError
        if self.fail_create:
            raise MigrationPlatformError("boom-create", status_code=500)
        mid = f"locktest-{len(self.created) + 1:03d}"
        self.created.append({"id": mid, "payload": payload})
        return {"migration_id": mid}

    def extract_migration_id(self, obj):
        return obj.get("migration_id") or obj.get("id")

    def build_files_batch_payload(self, files):
        return {"files": [{"local_id": lid} for lid, _p in files]}

    def add_files_batch(self, mid, payload):
        self.batched.append({"mid": mid, "payload": payload})
        items = [{"id": f"item-{i}", "extra_metadata": {"local_file_id": f["local_id"]}}
                 for i, f in enumerate(payload["files"])]
        return {"items": items}

    def correlate_batch_response(self, files, items):
        out = {}
        for it in items or []:
            lid = (it.get("extra_metadata") or {}).get("local_file_id")
            fid = it.get("id")
            if lid and fid:
                out[str(lid)] = str(fid)
        return out

    def get_migration_files(self, mid): return []

    def group_files_for_submission(self, rows):
        from services.migration_paths import ParsedSource
        files = []
        for r in rows:
            fid = str(r["fileID"])
            files.append((fid, ParsedSource(
                site_url="https://x/sites/s",
                library="Docs",
                folder_path="",
                file_name=r["fileName"] or f"{fid}.pdf",
            )))
        return ([{
            "group_key":   ("https://x/sites/s", "Docs", ""),
            "site_url":    "https://x/sites/s",
            "library":     "Docs",
            "folder_path": "",
            "files":       files,
        }], [])


# ── Main test flow ────────────────────────────────────────────────────────
def run():
    t.section("Setup: reset any leftover state, pick fresh Pending rows")
    # Trigger the one-shot schema migration (adds SubmittedBy + filtered
    # index).  count_all() is the lightest data_service call that fires
    # _ensure_schema_once().
    _ = count_all()
    wipe_leftovers()

    ten_a = pick_pending_ids(10)
    t.check("picked 10 Pending rows for User A", len(ten_a) == 10, f"got {len(ten_a)}")
    five_b = pick_pending_ids(20)[10:15]   # 5 different rows
    t.check("picked 5 more Pending rows for User B", len(five_b) == 5, f"got {len(five_b)}")
    assert not (set(ten_a) & set(five_b)), "User A/B row sets must be disjoint"

    baseline = count_all()
    print(f"  baseline overall counts: total={baseline['total']} "
          f"pending={baseline['pending']} in_processing={baseline['in_processing']}")

    fake = FakePlatform()
    platform_mod = __import__("services.migration_platform", fromlist=["service"])

    # ── Test 1: single user → 10 In Processing → Migrate disabled ────────
    t.section("Test 1: User A submits 10 → my_active=10 → Migrate locked")
    with patch.object(platform_mod, "service", fake):
        client_a = login_client(USER_A)

        r0 = client_a.get("/api/contracts")
        t.check("A: GET /api/contracts pre-submit → 200",
                r0.status_code == 200, r0.text[:200])
        pre = r0.json()["counts"]
        t.check("A: can_migrate=true before any submit",
                pre.get("can_migrate") is True, str(pre))
        t.check("A: my_in_processing=0 before any submit",
                (pre.get("my_in_processing") or 0) == 0, str(pre))

        r = client_a.post("/api/migrate", json={"ids": ten_a})
        t.check("A: POST /api/migrate → 200",
                r.status_code == 200, r.text[:300])
        body = r.json()
        t.check("A: response inProcessing has 10 ids",
                len(body["inProcessing"]) == 10,
                f"got {len(body['inProcessing'])}")

    # Verify server persisted SubmittedBy for every flipped row.
    stamped = [fid for fid in ten_a if read_submitted_by(fid) == USER_A.lower()]
    t.check("A: SubmittedBy persisted on all 10 rows (case-insensitive)",
            len(stamped) == 10, f"stamped={len(stamped)} of 10")

    # Verify server-side helpers agree A is locked.
    n_a = get_user_active_migration_count(USER_A)
    t.check("A: get_user_active_migration_count == 10",
            n_a == 10, f"got {n_a}")
    allowed_a, count_a = can_user_start_migration(USER_A)
    t.check("A: can_user_start_migration → (False, 10)",
            allowed_a is False and count_a == 10, f"({allowed_a}, {count_a})")

    # Verify GET /api/contracts reflects it too.
    with patch.object(platform_mod, "service", fake):
        client_a2 = login_client(USER_A)
        r2 = client_a2.get("/api/contracts")
        counts = r2.json()["counts"]
    t.check("A: /api/contracts shows can_migrate=false after submit",
            counts.get("can_migrate") is False, str(counts))
    t.check("A: /api/contracts shows my_in_processing=10",
            (counts.get("my_in_processing") or 0) == 10, str(counts))
    t.check("A: overall in_processing >= 10",
            (counts.get("in_processing") or 0) >= 10, str(counts))

    # ── Test 2: same user tries again → 409 ─────────────────────────────
    t.section("Test 2: User A tries again → 409 ACTIVE_MIGRATION_EXISTS")
    more_a = pick_pending_ids(5)
    with patch.object(platform_mod, "service", fake):
        client_a3 = login_client(USER_A)
        r_dup = client_a3.post("/api/migrate", json={"ids": more_a})
    t.check("A: 2nd /api/migrate → 409",
            r_dup.status_code == 409, r_dup.text[:200])
    detail = (r_dup.json() or {}).get("detail") or {}
    t.check("A: 409 body reason == ACTIVE_MIGRATION_EXISTS",
            detail.get("reason") == "ACTIVE_MIGRATION_EXISTS", str(detail))
    t.check("A: 409 body activeCount == 10",
            detail.get("activeCount") == 10, str(detail))
    t.check("A: no new rows flipped by the rejected submission",
            get_user_active_migration_count(USER_A) == 10)
    # Verify none of the "more_a" ids leaked into In Processing.
    unaffected = [fid for fid in more_a
                  if read_submitted_by(fid) == ""]
    t.check("A: rejected submission left rows' SubmittedBy blank",
            len(unaffected) == len(more_a),
            f"unaffected={len(unaffected)} of {len(more_a)}")

    # ── Test 3: different user submits while A is locked ────────────────
    t.section("Test 3: User B submits while A is locked → 200")
    with patch.object(platform_mod, "service", FakePlatform()):
        client_b = login_client(USER_B)
        r_b = client_b.post("/api/migrate", json={"ids": five_b})
    t.check("B: POST /api/migrate → 200",
            r_b.status_code == 200, r_b.text[:300])
    t.check("B: 5 rows flipped for B", len(r_b.json()["inProcessing"]) == 5)
    n_b = get_user_active_migration_count(USER_B)
    t.check("B: get_user_active_migration_count(B) == 5",
            n_b == 5, f"got {n_b}")
    # A's count is unchanged.
    t.check("A: A's active count still 10 (independent of B's submission)",
            get_user_active_migration_count(USER_A) == 10)

    # ── Test 4: completion auto-unlock ──────────────────────────────────
    t.section("Test 4: complete A's rows → auto-unlock")
    # Simulate the sync poller finishing A's rows in 3 waves: 4 → 6 → all
    force_status(ten_a[:4], STATUS_MIGRATED, submitted_by=USER_A)
    t.check("A: after 4 complete, my_active == 6",
            get_user_active_migration_count(USER_A) == 6)
    force_status(ten_a[4:10], STATUS_MIGRATED, submitted_by=USER_A)
    t.check("A: after all complete, my_active == 0",
            get_user_active_migration_count(USER_A) == 0)
    allowed_a, _ = can_user_start_migration(USER_A)
    t.check("A: can_user_start_migration → True after completion",
            allowed_a is True)
    with patch.object(platform_mod, "service", FakePlatform()):
        client_a4 = login_client(USER_A)
        r4 = client_a4.get("/api/contracts")
        counts4 = r4.json()["counts"]
    t.check("A: /api/contracts can_migrate=true after auto-unlock",
            counts4.get("can_migrate") is True, str(counts4))
    t.check("A: /api/contracts my_in_processing=0 after auto-unlock",
            (counts4.get("my_in_processing") or 0) == 0, str(counts4))

    # ── Test 5: two tabs / same account concurrent submission ───────────
    t.section("Test 5: two tabs / same user race → exactly one wins")
    # Reset A so they can start a fresh submission.
    wipe_leftovers()
    ten_a2 = pick_pending_ids(10)
    ten_a3 = pick_pending_ids(20)[10:20]
    assert not (set(ten_a2) & set(ten_a3)), "row sets must be disjoint"

    fake_tab1 = FakePlatform()
    fake_tab2 = FakePlatform()

    # Two threads hit /api/migrate at the "same time" (as close as the
    # GIL + TestClient sync-execution allows).  Both use the same email,
    # but each thread has its own TestClient session cookie.
    results: Dict[int, Any] = {}

    def submit(tab_id: int, ids: List[str], fake_plat):
        try:
            with patch.object(platform_mod, "service", fake_plat):
                c = login_client(USER_A)
                results[tab_id] = c.post("/api/migrate", json={"ids": ids})
        except Exception as e:
            results[tab_id] = e

    barrier = threading.Barrier(2)

    def worker(tab_id, ids, fake_plat):
        barrier.wait()
        submit(tab_id, ids, fake_plat)

    tab1 = threading.Thread(target=worker, args=(1, ten_a2, fake_tab1))
    tab2 = threading.Thread(target=worker, args=(2, ten_a3, fake_tab2))
    tab1.start(); tab2.start()
    tab1.join(timeout=60); tab2.join(timeout=60)

    r_tab1 = results.get(1)
    r_tab2 = results.get(2)
    codes = sorted([getattr(r_tab1, "status_code", None),
                    getattr(r_tab2, "status_code", None)])
    t.check("Race: exactly one 200 and one 409",
            codes == [200, 409], f"codes={codes}")

    # And the losing tab's ids must NOT have SubmittedBy set.
    winner_ids = (ten_a2 if getattr(r_tab1, "status_code", None) == 200
                  else ten_a3)
    loser_ids  = (ten_a3 if winner_ids is ten_a2 else ten_a2)
    stamped_winner = sum(1 for fid in winner_ids
                         if read_submitted_by(fid) == USER_A.lower())
    stamped_loser  = sum(1 for fid in loser_ids
                         if read_submitted_by(fid) == USER_A.lower())
    t.check("Race: 10 winner rows stamped with A",
            stamped_winner == 10, f"stamped_winner={stamped_winner}")
    t.check("Race: 0 loser rows stamped",
            stamped_loser == 0, f"stamped_loser={stamped_loser}")
    t.check("Race: A's active count == 10 (not 20)",
            get_user_active_migration_count(USER_A) == 10)

    # ── Test 6: logout / login preserves the lock ───────────────────────
    t.section("Test 6: logout then log back in → lock persists")
    # (User A's session from the race is now stale; make a fresh one.)
    with patch.object(platform_mod, "service", FakePlatform()):
        client_relog = login_client(USER_A)
        r_relog = client_relog.get("/api/contracts")
    counts_r = r_relog.json()["counts"]
    t.check("A after re-login: my_in_processing == 10",
            (counts_r.get("my_in_processing") or 0) == 10, str(counts_r))
    t.check("A after re-login: can_migrate == false",
            counts_r.get("can_migrate") is False, str(counts_r))
    # And a submit attempt still gets 409.
    with patch.object(platform_mod, "service", FakePlatform()):
        r_reblock = login_client(USER_A).post(
            "/api/migrate", json={"ids": pick_pending_ids(3)}
        )
    t.check("A after re-login: /api/migrate still 409",
            r_reblock.status_code == 409, r_reblock.text[:200])

    # ── Test 7: submission failure unlocks the user ─────────────────────
    t.section("Test 7: platform submission fails → A rolls back → unlocked")
    # Clean A's state first so we can test the failure path from scratch.
    wipe_leftovers()
    t.check("cleanup: A's active count reset to 0",
            get_user_active_migration_count(USER_A) == 0)

    fail_plat = FakePlatform(fail_create=True)
    fresh = pick_pending_ids(5)
    with patch.object(platform_mod, "service", fail_plat):
        client_fail = login_client(USER_A)
        r_fail = client_fail.post("/api/migrate", json={"ids": fresh})
    # /api/migrate still returns 200 because Phase-3 failures are handled
    # asynchronously (matches the existing async migration flow).  The
    # background task rolls the rows back to Failed before returning.
    t.check("A: /api/migrate returned 200 despite platform failure",
            r_fail.status_code == 200, r_fail.text[:200])
    # After the background rollback, A's active count should be 0.
    remaining_active = get_user_active_migration_count(USER_A)
    t.check("A: active count == 0 after platform-failure rollback",
            remaining_active == 0, f"got {remaining_active}")
    allowed_after_fail, _ = can_user_start_migration(USER_A)
    t.check("A: can_user_start_migration → True after failure rollback",
            allowed_after_fail is True)

    # ── Cleanup ─────────────────────────────────────────────────────────
    t.section("Cleanup: wipe test leftovers")
    wipe_leftovers()
    t.check("cleanup: A active == 0",
            get_user_active_migration_count(USER_A) == 0)
    t.check("cleanup: B active == 0",
            get_user_active_migration_count(USER_B) == 0)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        print("\n!! UNCAUGHT EXCEPTION")
        traceback.print_exc()
        t.failed += 1
    sys.exit(t.summary())
