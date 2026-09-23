"""End-to-end test for the always-on 5 s auto-refresh poller.

Verifies (against the LIVE Azure SQL DB via the FastAPI TestClient):

  1. GET /api/contracts exposes config.pollIntervalSeconds (default 5).
  2. GET /api/migrations/sync returns the same global + per-user count
     shape as /api/contracts.counts so the browser can update KPIs
     without a full row refetch when nothing changed.
  3. Multi-user propagation: User A submits N rows → User B's very
     next /api/contracts + /api/migrations/sync tick show
     in_processing bumped by N and pending down by N (proof that the
     poller's 5 s cycle will surface A's change to B).
  4. Personal fields remain per-user (my_in_processing) even though
     global buckets are shared.
  5. Migrate-button lock reflects can_migrate on the sync-tick payload.

Does NOT test the actual browser setInterval — the poller is JS and
tested by inspection.  This test verifies the SERVER contract the
poller depends on, plus the multi-user visibility requirement.

Run:  MYENV/bin/python scripts/test_auto_refresh_poller.py
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
from services.data_service import STATUS_PENDING, count_all


USER_A = "user-a-refreshtest@bs.nttdata.com"
USER_B = "user-b-refreshtest@bs.nttdata.com"


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
            f"   OR [MigrationRequestId] LIKE 'refreshtest-%'",
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
        mid = f"refreshtest-{len(self.created)+1:03d}"
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
    _ = count_all()
    wipe_leftovers()

    fake = FakePlatform()
    platform_mod = __import__("services.migration_platform", fromlist=["service"])

    # ── §1: /api/contracts exposes pollIntervalSeconds ──────────────────
    t.section("§1: /api/contracts.config.pollIntervalSeconds is exposed")
    client_a = login_client(USER_A)
    r = client_a.get("/api/contracts")
    t.check("GET /api/contracts → 200", r.status_code == 200, r.text[:200])
    cfg = r.json().get("config") or {}
    poll_s = cfg.get("pollIntervalSeconds")
    t.check(f"config.pollIntervalSeconds present (got {poll_s!r})",
            poll_s is not None)
    t.check(f"pollIntervalSeconds is a positive integer (got {poll_s!r})",
            isinstance(poll_s, int) and poll_s > 0)
    t.check(f"pollIntervalSeconds == 5 (default) or overridden by env "
            f"(got {poll_s!r}, env={settings.MIGRATION_POLL_INTERVAL})",
            poll_s == int(settings.MIGRATION_POLL_INTERVAL))

    # ── §2: sync payload has the same count shape as contracts.counts ─
    t.section("§2: /api/migrations/sync count shape matches /api/contracts.counts")
    # /api/migrations/sync is a POST (see routes/contracts.py) — the JS
    # migrationService.sync() uses POST too.  A GET would be intercepted
    # by /api/migrations/{migration_id}.
    with patch.object(platform_mod, "service", fake):
        sync = client_a.post("/api/migrations/sync")
    t.check("POST /api/migrations/sync → 200",
            sync.status_code == 200, sync.text[:200])
    sc = sync.json().get("counts") or {}
    contract_c = r.json().get("counts") or {}
    for key in ("total", "active", "excluded", "pending", "in_processing",
                "migrated", "failed", "my_in_processing", "can_migrate"):
        t.check(f"sync.counts.{key} present (got {sc.get(key)!r})",
                key in sc, f"sync counts keys={list(sc.keys())}")
    for key in ("total", "in_processing", "migrated", "pending", "excluded"):
        t.check(f"sync.counts.{key} == contracts.counts.{key}",
                sc.get(key) == contract_c.get(key),
                f"sync={sc.get(key)} contracts={contract_c.get(key)}")

    # ── §3: Multi-user propagation via poller (poll = re-GET) ──────────
    t.section("§3: A submits 8 rows → next poll tick surfaces to B")
    a_ids = pick_pending_ids(8)
    t.check(f"picked 8 rows for A (got {len(a_ids)})", len(a_ids) == 8)

    baseline_b = login_client(USER_B).get("/api/contracts").json()["counts"]
    print(f"  B baseline: in_processing={baseline_b['in_processing']} "
          f"pending={baseline_b['pending']} "
          f"my_in_processing={baseline_b.get('my_in_processing')}")

    with patch.object(platform_mod, "service", fake):
        client_a2 = login_client(USER_A)
        rsub = client_a2.post("/api/migrate", json={"ids": a_ids})
        t.check("A: /api/migrate → 200", rsub.status_code == 200, rsub.text[:200])

    # Simulate B's next 5 s poll tick — this is exactly what the browser
    # does: POST /api/migrations/sync (cheap) then GET /api/contracts (row set).
    with patch.object(platform_mod, "service", fake):
        client_b2 = login_client(USER_B)
        b_sync    = client_b2.post("/api/migrations/sync").json()
        b_contracts = client_b2.get("/api/contracts").json()

    bs = b_sync["counts"]
    bc = b_contracts["counts"]
    t.check(f"B sync tick: in_processing bumped by 8 "
            f"({baseline_b['in_processing']} → {bs['in_processing']})",
            bs["in_processing"] - baseline_b["in_processing"] == 8)
    t.check(f"B sync tick: pending decreased by 8 "
            f"({baseline_b['pending']} → {bs['pending']})",
            baseline_b["pending"] - bs["pending"] == 8)
    t.check(f"B contracts tick: in_processing == {bs['in_processing']} (matches sync)",
            bc["in_processing"] == bs["in_processing"])
    t.check("B contracts tick: A's rows are in the returned list",
            len([r for r in b_contracts["data"] if r["fileID"] in set(a_ids)]) == 8)
    t.check("B contracts tick: A's rows carry migrationStatus='In Processing'",
            all(r["migrationStatus"] == "In Processing"
                for r in b_contracts["data"] if r["fileID"] in set(a_ids)))
    t.check("B contracts tick: A's rows carry submittedBy=User A",
            all((r.get("submittedBy") or "").lower() == USER_A.lower()
                for r in b_contracts["data"] if r["fileID"] in set(a_ids)))

    # ── §4: personal fields are per-user, buckets are global ───────────
    t.section("§4: personal counts diverge, global counts identical")
    with patch.object(platform_mod, "service", fake):
        a_sync = login_client(USER_A).post("/api/migrations/sync").json()["counts"]
    t.check(f"A: my_in_processing == 8 (got {a_sync.get('my_in_processing')})",
            (a_sync.get("my_in_processing") or 0) == 8)
    t.check(f"B: my_in_processing == 0 (got {bs.get('my_in_processing')})",
            (bs.get("my_in_processing") or 0) == 0)
    t.check("global in_processing identical for A and B",
            a_sync["in_processing"] == bs["in_processing"])

    # ── §5: can_migrate flag reflects personal lock ────────────────────
    t.section("§5: can_migrate on sync-tick payload gates Migrate button")
    t.check(f"A: can_migrate == False (locked, got {a_sync.get('can_migrate')!r})",
            a_sync.get("can_migrate") is False)
    t.check(f"B: can_migrate == True (free, got {bs.get('can_migrate')!r})",
            bs.get("can_migrate") is True)

    # ── §6: sync tick after row completion shows the drop ─────────────
    t.section("§6: 3 of A's rows complete → next tick shows the drop for both users")
    complete = a_ids[:3]
    table = settings.CONTRACT_TABLE
    with get_connection() as cn:
        cur = cn.cursor()
        ph = ", ".join("?" for _ in complete)
        cur.execute(
            f"UPDATE dbo.[{table}] "
            f"SET [MigrationStatus] = 'Migrated', [Migrated] = 'True', "
            f"    [MigratedDate] = SYSUTCDATETIME() "
            f"WHERE [FileID] IN ({ph})",
            list(complete),
        )
        cn.commit()

    with patch.object(platform_mod, "service", fake):
        a_after = login_client(USER_A).post("/api/migrations/sync").json()["counts"]
        b_after = login_client(USER_B).post("/api/migrations/sync").json()["counts"]

    t.check(f"A next tick: in_processing dropped by 3 "
            f"({a_sync['in_processing']} → {a_after['in_processing']})",
            a_sync["in_processing"] - a_after["in_processing"] == 3)
    t.check(f"B next tick: in_processing dropped by 3 (same global figure)",
            bs["in_processing"] - b_after["in_processing"] == 3)
    t.check(f"A next tick: migrated grew by 3",
            a_after["migrated"] - a_sync["migrated"] == 3)
    t.check(f"B next tick: migrated grew by 3 (same for both)",
            b_after["migrated"] - bs["migrated"] == 3)
    t.check(f"A next tick: my_in_processing == 5 (was 8 → -3)",
            (a_after.get("my_in_processing") or 0) == 5)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        import traceback
        print(traceback.format_exc())
        t.failed += 1
    finally:
        try: wipe_leftovers()
        except Exception: pass
        sys.exit(t.summary())
