"""End-to-end test: GLOBAL inventory buckets vs PERSONAL migration state.

Verifies the architectural rule requested in the multi-user spec:

    MAIN TOP BUCKETS         = GLOBAL inventory state (same for every user)
    PERSONAL MIGRATION       = current user's SubmittedBy only
    MIGRATE BUTTON LOCK      = current user's SubmittedBy only

Concretely: after User A submits N rows, User B logging in must see:
    - Global 'in_processing' bucket includes A's N rows.
    - Global 'yet_to_be_migrated' bucket has decreased by N.
    - B's `my_in_processing` = 0.
    - B's `can_migrate` = true.
    - A's rows appear in the GET /api/contracts payload B receives.
    - A's rows carry submittedBy = user-a@... (audit column).

Run:  MYENV/bin/python scripts/test_global_bucket_visibility.py
"""
from __future__ import annotations

import logging
import sys
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
from services.data_service import (
    STATUS_PENDING,
    count_all,
)


USER_A = "user-a-globaltest@bs.nttdata.com"
USER_B = "user-b-globaltest@bs.nttdata.com"


class T:
    def __init__(self): self.passed = 0; self.failed = 0
    def check(self, label, cond, detail=""):
        if cond:
            print(f"  PASS  {label}")
            self.passed += 1
        else:
            print(f"  FAIL  {label}  {detail}")
            self.failed += 1
    def section(self, title): print(f"\n=== {title} ===")
    def summary(self):
        total = self.passed + self.failed
        print(f"\n{self.passed}/{total} passed  ({self.failed} failed)")
        return 0 if self.failed == 0 else 1


t = T()


def wipe_leftovers() -> None:
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
            f"   OR [MigrationRequestId] LIKE 'globaltest-%'",
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


def login_client(email: str) -> TestClient:
    from main import app
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": email})
    assert r.status_code == 200, r.text
    return c


class FakePlatform:
    def __init__(self): self.created = []; self.batched = []
    def is_configured(self): return True
    def build_migration_request_payload(self, **kw): return {"name": "fake", **kw}
    def create_migration(self, payload):
        mid = f"globaltest-{len(self.created)+1:03d}"
        self.created.append({"id": mid, "payload": payload})
        return {"migration_id": mid}
    def extract_migration_id(self, obj): return obj.get("migration_id") or obj.get("id")
    def build_files_batch_payload(self, files):
        return {"files": [{"local_id": lid} for lid, _p in files]}
    def add_files_batch(self, mid, payload):
        self.batched.append({"mid": mid, "payload": payload})
        return {"items": [{"id": f"item-{i}", "extra_metadata": {"local_file_id": f["local_id"]}}
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
            site_url="https://x/sites/s", library="Docs", folder_path="",
            file_name=r["fileName"] or f"{r['fileID']}.pdf")) for r in rows]
        return ([{
            "group_key":   ("https://x/sites/s", "Docs", ""),
            "site_url":    "https://x/sites/s",
            "library":     "Docs",
            "folder_path": "",
            "files":       files,
        }], [])


def run():
    t.section("Setup")
    _ = count_all()   # trigger _ensure_schema_once
    wipe_leftovers()

    a_ids = pick_pending_ids(20)
    t.check("picked 20 Pending rows for User A", len(a_ids) == 20, f"got {len(a_ids)}")
    baseline = count_all()
    print(f"  baseline: total={baseline['total']} pending={baseline['pending']} "
          f"in_processing={baseline['in_processing']} migrated={baseline['migrated']}")

    fake = FakePlatform()
    platform_mod = __import__("services.migration_platform", fromlist=["service"])

    # ── User A submits 20 ────────────────────────────────────────────────
    t.section("User A submits 20 rows")
    with patch.object(platform_mod, "service", fake):
        client_a = login_client(USER_A)
        r = client_a.post("/api/migrate", json={"ids": a_ids})
        t.check("A: /api/migrate → 200", r.status_code == 200, r.text[:300])
        t.check("A: response inProcessing count = 20",
                len(r.json().get("inProcessing", [])) == 20)

        # A's view immediately after submit
        r_a = client_a.get("/api/contracts")
        counts_a: Dict[str, Any] = r_a.json()["counts"]
        rows_a: List[Dict[str, Any]] = r_a.json()["data"]

    # ── User B logs in FRESH — separate TestClient, separate session ────
    t.section("User B logs in (fresh session) — must see A's 20 globally")
    with patch.object(platform_mod, "service", fake):
        client_b = login_client(USER_B)
        r_b = client_b.get("/api/contracts")
        counts_b: Dict[str, Any] = r_b.json()["counts"]
        rows_b: List[Dict[str, Any]] = r_b.json()["data"]

    # ── PROOFS ───────────────────────────────────────────────────────────
    t.section("Proof 1: Global bucket counts are IDENTICAL for A and B")
    for key in ("total", "in_processing", "migrated", "pending", "excluded", "failed"):
        t.check(f"counts.{key} matches (A={counts_a.get(key)!r}, B={counts_b.get(key)!r})",
                counts_a.get(key) == counts_b.get(key),
                f"A={counts_a.get(key)} B={counts_b.get(key)}")

    t.section("Proof 2: Global in_processing INCLUDES A's submission for both users")
    t.check(f"A sees in_processing >= 20 (got {counts_a.get('in_processing')})",
            (counts_a.get("in_processing") or 0) >= 20)
    t.check(f"B sees in_processing >= 20 (got {counts_b.get('in_processing')})",
            (counts_b.get("in_processing") or 0) >= 20)
    t.check("A's in_processing delta over baseline == 20",
            (counts_a.get("in_processing") or 0) - (baseline["in_processing"] or 0) == 20,
            f"delta={counts_a.get('in_processing',0) - baseline['in_processing']}")
    t.check("B's in_processing delta over baseline == 20",
            (counts_b.get("in_processing") or 0) - (baseline["in_processing"] or 0) == 20)

    t.section("Proof 3: Global pending DECREASED by 20 for both users")
    t.check("A: pending decreased by 20",
            baseline["pending"] - (counts_a.get("pending") or 0) == 20,
            f"delta={baseline['pending'] - counts_a.get('pending',0)}")
    t.check("B: pending decreased by 20",
            baseline["pending"] - (counts_b.get("pending") or 0) == 20,
            f"delta={baseline['pending'] - counts_b.get('pending',0)}")

    t.section("Proof 4: Personal my_in_processing is per-USER, not global")
    t.check(f"A: my_in_processing == 20 (got {counts_a.get('my_in_processing')})",
            (counts_a.get("my_in_processing") or 0) == 20)
    t.check(f"B: my_in_processing == 0 (got {counts_b.get('my_in_processing')})",
            (counts_b.get("my_in_processing") or 0) == 0)

    t.section("Proof 5: Migrate button lock uses PERSONAL count only")
    t.check(f"A: can_migrate == False (got {counts_a.get('can_migrate')})",
            counts_a.get("can_migrate") is False)
    t.check(f"B: can_migrate == True (got {counts_b.get('can_migrate')})",
            counts_b.get("can_migrate") is True)

    t.section("Proof 6: A's rows APPEAR in B's /api/contracts payload")
    a_id_set = set(a_ids)
    rows_b_for_a = [r for r in rows_b if r.get("fileID") in a_id_set]
    t.check(f"B receives all 20 of A's rows (got {len(rows_b_for_a)})",
            len(rows_b_for_a) == 20)
    b_in_proc = [r for r in rows_b_for_a if r.get("migrationStatus") == "In Processing"]
    t.check(f"B sees all 20 of A's rows as 'In Processing' (got {len(b_in_proc)})",
            len(b_in_proc) == 20)

    t.section("Proof 7: A's rows carry submittedBy audit field visible to B")
    b_with_submitter = [r for r in b_in_proc
                        if (r.get("submittedBy") or "").lower() == USER_A.lower()]
    t.check(f"B sees submittedBy=User A on all 20 rows (got {len(b_with_submitter)})",
            len(b_with_submitter) == 20,
            f"missing/empty on {len(b_in_proc) - len(b_with_submitter)} rows; "
            f"sample submittedBy values: "
            f"{[r.get('submittedBy') for r in b_in_proc[:3]]}")

    t.section("Proof 8: Rows disappear from 'yet-to-be-migrated' pool for B")
    # Ensure NONE of A's ids appear with a Pending/Failed status in B's data
    b_pending_of_a = [r for r in rows_b_for_a
                      if r.get("migrationStatus") in ("Pending", "Failed", None)]
    t.check(f"None of A's 20 rows appear Pending/Failed for B (got {len(b_pending_of_a)})",
            len(b_pending_of_a) == 0)

    # ── Completion visible to both users ────────────────────────────────
    t.section("Proof 9: Completion updates propagate globally")
    # Simulate 5 of A's rows completing → Migrated
    complete_5 = a_ids[:5]
    table = settings.CONTRACT_TABLE
    with get_connection() as cn:
        cur = cn.cursor()
        ph = ", ".join("?" for _ in complete_5)
        cur.execute(
            f"UPDATE dbo.[{table}] "
            f"SET [MigrationStatus] = 'Migrated', [Migrated] = 'True', "
            f"    [MigratedDate] = SYSUTCDATETIME() "
            f"WHERE [FileID] IN ({ph})",
            list(complete_5),
        )
        cn.commit()

    with patch.object(platform_mod, "service", fake):
        client_a3 = login_client(USER_A)
        client_b3 = login_client(USER_B)
        counts_a3 = client_a3.get("/api/contracts").json()["counts"]
        counts_b3 = client_b3.get("/api/contracts").json()["counts"]

    t.check(f"A: in_processing dropped by 5 vs post-submit "
            f"({counts_a.get('in_processing')} → {counts_a3.get('in_processing')})",
            (counts_a.get("in_processing") or 0) - (counts_a3.get("in_processing") or 0) == 5)
    t.check(f"B: in_processing dropped by 5 vs post-submit "
            f"({counts_b.get('in_processing')} → {counts_b3.get('in_processing')})",
            (counts_b.get("in_processing") or 0) - (counts_b3.get("in_processing") or 0) == 5)
    t.check(f"A: migrated grew by 5 "
            f"({counts_a.get('migrated')} → {counts_a3.get('migrated')})",
            (counts_a3.get("migrated") or 0) - (counts_a.get("migrated") or 0) == 5)
    t.check(f"B: migrated grew by 5 "
            f"({counts_b.get('migrated')} → {counts_b3.get('migrated')})",
            (counts_b3.get("migrated") or 0) - (counts_b.get("migrated") or 0) == 5)
    t.check("A: my_in_processing dropped by 5 (was 20 → now 15)",
            (counts_a3.get("my_in_processing") or 0) == 15)
    t.check("B: my_in_processing still 0",
            (counts_b3.get("my_in_processing") or 0) == 0)

    # ── User B independently submits 5 rows while A is still locked ─────
    t.section("Proof 10: B can migrate independently while A is locked")
    b_ids = pick_pending_ids(5)
    t.check(f"picked 5 fresh Pending rows for User B (got {len(b_ids)})",
            len(b_ids) == 5)
    with patch.object(platform_mod, "service", fake):
        client_b4 = login_client(USER_B)
        r_bsubmit = client_b4.post("/api/migrate", json={"ids": b_ids})
        t.check("B: /api/migrate → 200 (independent of A's lock)",
                r_bsubmit.status_code == 200, r_bsubmit.text[:200])

        # Both users must now see a further +5 in the global bucket
        counts_a5 = login_client(USER_A).get("/api/contracts").json()["counts"]
        counts_b5 = client_b4.get("/api/contracts").json()["counts"]
    t.check(f"A: in_processing bumped by 5 more "
            f"({counts_a3.get('in_processing')} → {counts_a5.get('in_processing')})",
            (counts_a5.get("in_processing") or 0) - (counts_a3.get("in_processing") or 0) == 5)
    t.check(f"B: in_processing bumped by 5 more (same global figure)",
            (counts_b5.get("in_processing") or 0) - (counts_b3.get("in_processing") or 0) == 5)
    t.check("A: my_in_processing still 15 (unaffected by B's submit)",
            (counts_a5.get("my_in_processing") or 0) == 15)
    t.check("B: my_in_processing now 5",
            (counts_b5.get("my_in_processing") or 0) == 5)
    t.check("A: can_migrate still False",
            counts_a5.get("can_migrate") is False)
    t.check("B: can_migrate now False (has active batch)",
            counts_b5.get("can_migrate") is False)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        traceback_text = __import__("traceback").format_exc()
        print(traceback_text)
        t.failed += 1
    finally:
        try:
            wipe_leftovers()
        except Exception:
            pass
        sys.exit(t.summary())
