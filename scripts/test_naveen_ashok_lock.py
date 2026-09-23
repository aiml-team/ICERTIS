"""Reproduces the reported bug: two logged-in users seeing the SAME
My Active count.

Scenario from the spec:
   Naveen submits 10 → Naveen My Active = 10, Ashok My Active = 0
   Ashok  submits 15 → Naveen My Active = 10, Ashok My Active = 15
   Naveen's 10 complete → Naveen unlocks; Ashok still locked
   Ashok's 15 complete → both unlocked

Also verifies:
   - global in_processing is identical for both users (shared count)
   - can_migrate is per-user
   - two tabs same user → one 200 + one 409 (race protection preserved)
   - two DIFFERENT users at the same instant → both allowed
"""
from __future__ import annotations
import logging, sys, threading, traceback
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

NAVEEN = "naveen-locktest@bs.nttdata.com"
ASHOK  = "ashok-locktest@bs.nttdata.com"


class T:
    def __init__(self): self.passed = 0; self.failed = 0
    def check(self, label, cond, detail=""):
        (self.passed if cond else self.failed).__add__  # noqa
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


def wipe():
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
            f"   OR [MigrationRequestId] LIKE 'naveen-%' "
            f"   OR [MigrationRequestId] LIKE 'ashok-%'",
            [NAVEEN.lower(), ASHOK.lower()],
        )
        cn.commit()


def pick(n: int) -> List[str]:
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


def force_migrated(ids):
    if not ids: return
    table = settings.CONTRACT_TABLE
    ph = ", ".join("?" for _ in ids)
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"UPDATE dbo.[{table}] SET [MigrationStatus]='Migrated', "
            f"[Migrated]='True', [MigratedDate]=SYSUTCDATETIME() "
            f"WHERE [FileID] IN ({ph})", list(ids))
        cn.commit()


def login(email: str) -> TestClient:
    from main import app
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": email})
    assert r.status_code == 200, r.text
    return c


class FakePlatform:
    def __init__(self): self.n = 0
    def is_configured(self): return True
    def build_migration_request_payload(self, **kw): return {"name": "fake", **kw}
    def create_migration(self, payload):
        self.n += 1
        prefix = "naveen" if "naveen" in (payload.get("created_by") or "").lower() else "ashok"
        return {"migration_id": f"{prefix}-{self.n:03d}"}
    def extract_migration_id(self, obj): return obj.get("migration_id") or obj.get("id")
    def build_files_batch_payload(self, files):
        return {"files": [{"local_id": lid} for lid, _p in files]}
    def add_files_batch(self, mid, payload):
        return {"items": [{"id": f"it-{i}", "extra_metadata": {"local_file_id": f["local_id"]}}
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


def counts_for(client: TestClient) -> Dict[str, Any]:
    r = client.get("/api/contracts")
    assert r.status_code == 200, r.text
    return r.json()["counts"]


def run():
    _ = count_all()
    wipe()

    fake = FakePlatform()
    pm = __import__("services.migration_platform", fromlist=["service"])

    baseline = count_all()
    n_ids = pick(10)
    a_ids = pick(30)[10:25]   # 15 disjoint from Naveen's 10
    t.check("picked 10 rows for Naveen", len(n_ids) == 10)
    t.check("picked 15 rows for Ashok",  len(a_ids) == 15)
    assert not (set(n_ids) & set(a_ids)), "disjoint sets"

    # ── Test 1: Naveen submits 10 ────────────────────────────────────
    t.section("Test 1: Naveen submits 10 → Naveen locked, Ashok free")
    with patch.object(pm, "service", fake):
        naveen = login(NAVEEN)
        ashok  = login(ASHOK)

        r = naveen.post("/api/migrate", json={"ids": n_ids})
        t.check("Naveen: /api/migrate → 200", r.status_code == 200, r.text[:200])

        c_n = counts_for(naveen)
        c_a = counts_for(ashok)

    t.check(f"GLOBAL in_processing bumped by 10 (Naveen sees {c_n['in_processing']})",
            c_n["in_processing"] - baseline["in_processing"] == 10)
    t.check(f"GLOBAL in_processing identical for Ashok (sees {c_a['in_processing']})",
            c_a["in_processing"] == c_n["in_processing"])
    t.check(f"Naveen: my_in_processing == 10 (got {c_n.get('my_in_processing')})",
            (c_n.get("my_in_processing") or 0) == 10)
    t.check(f"Ashok:  my_in_processing == 0  (got {c_a.get('my_in_processing')})",
            (c_a.get("my_in_processing") or 0) == 0)
    t.check(f"Naveen: can_migrate == False (got {c_n.get('can_migrate')!r})",
            c_n.get("can_migrate") is False)
    t.check(f"Ashok:  can_migrate == True  (got {c_a.get('can_migrate')!r})",
            c_a.get("can_migrate") is True)

    # ── Test 2: Ashok submits 15 while Naveen still locked ──────────
    t.section("Test 2: Ashok submits 15 → both locked on own batches, global=25")
    with patch.object(pm, "service", fake):
        r = ashok.post("/api/migrate", json={"ids": a_ids})
        t.check("Ashok: /api/migrate → 200 (independent of Naveen's lock)",
                r.status_code == 200, r.text[:300])
        c_n2 = counts_for(naveen)
        c_a2 = counts_for(ashok)

    t.check(f"GLOBAL in_processing == baseline + 25 (Naveen sees {c_n2['in_processing']})",
            c_n2["in_processing"] - baseline["in_processing"] == 25)
    t.check(f"GLOBAL identical for Ashok (sees {c_a2['in_processing']})",
            c_a2["in_processing"] == c_n2["in_processing"])
    t.check(f"Naveen: my_in_processing == 10 (got {c_n2.get('my_in_processing')})",
            (c_n2.get("my_in_processing") or 0) == 10)
    t.check(f"Ashok:  my_in_processing == 15 (got {c_a2.get('my_in_processing')})",
            (c_a2.get("my_in_processing") or 0) == 15)
    t.check("Naveen: can_migrate == False", c_n2.get("can_migrate") is False)
    t.check("Ashok:  can_migrate == False", c_a2.get("can_migrate") is False)

    # ── Test 3: Naveen's 10 complete → Naveen unlocks, Ashok stays locked
    t.section("Test 3: Naveen's 10 complete → Naveen unlocks, Ashok stays locked")
    force_migrated(n_ids)
    with patch.object(pm, "service", fake):
        c_n3 = counts_for(naveen)
        c_a3 = counts_for(ashok)

    t.check(f"GLOBAL in_processing == baseline + 15 (Naveen sees {c_n3['in_processing']})",
            c_n3["in_processing"] - baseline["in_processing"] == 15)
    t.check(f"GLOBAL identical for Ashok (sees {c_a3['in_processing']})",
            c_a3["in_processing"] == c_n3["in_processing"])
    t.check(f"Naveen: my_in_processing == 0 (got {c_n3.get('my_in_processing')})",
            (c_n3.get("my_in_processing") or 0) == 0)
    t.check(f"Ashok:  my_in_processing == 15 (got {c_a3.get('my_in_processing')})",
            (c_a3.get("my_in_processing") or 0) == 15)
    t.check(f"Naveen: can_migrate flips back to True (got {c_n3.get('can_migrate')!r})",
            c_n3.get("can_migrate") is True)
    t.check(f"Ashok:  can_migrate still False (got {c_a3.get('can_migrate')!r})",
            c_a3.get("can_migrate") is False)

    # ── Test 4: Ashok's 15 complete → both unlocked ────────────────
    t.section("Test 4: Ashok's 15 complete → both unlocked, global back to baseline")
    force_migrated(a_ids)
    with patch.object(pm, "service", fake):
        c_n4 = counts_for(naveen)
        c_a4 = counts_for(ashok)

    t.check(f"GLOBAL back to baseline (Naveen sees {c_n4['in_processing']})",
            c_n4["in_processing"] == baseline["in_processing"])
    t.check(f"GLOBAL identical for Ashok (sees {c_a4['in_processing']})",
            c_a4["in_processing"] == c_n4["in_processing"])
    t.check("Naveen: my_in_processing == 0",
            (c_n4.get("my_in_processing") or 0) == 0)
    t.check("Ashok:  my_in_processing == 0",
            (c_a4.get("my_in_processing") or 0) == 0)
    t.check("Naveen: can_migrate == True", c_n4.get("can_migrate") is True)
    t.check("Ashok:  can_migrate == True", c_a4.get("can_migrate") is True)

    # ── Test 5: same email in two tabs — only one allowed ──────────
    t.section("Test 5: Naveen from two tabs at once → one 200 + one 409")
    fresh = pick(20)
    naveen_a_ids = fresh[:5]
    naveen_b_ids = fresh[5:10]
    barrier = threading.Barrier(2)
    results = {}

    def tab(name, ids):
        with patch.object(pm, "service", fake):
            c = login(NAVEEN)
            barrier.wait()
            r = c.post("/api/migrate", json={"ids": ids})
            results[name] = r.status_code

    th_a = threading.Thread(target=tab, args=("tab_a", naveen_a_ids))
    th_b = threading.Thread(target=tab, args=("tab_b", naveen_b_ids))
    th_a.start(); th_b.start(); th_a.join(); th_b.join()
    ok = sorted(results.values()) == [200, 409]
    t.check(f"one 200 + one 409 (got {results})", ok)

    # Cleanup Naveen's Test 5 rows (whichever tab won stamped 5 rows).
    with patch.object(pm, "service", fake):
        # unlock Naveen by completing everything he owns
        table = settings.CONTRACT_TABLE
        with get_connection() as cn:
            cur = cn.cursor()
            cur.execute(
                f"UPDATE dbo.[{table}] SET [MigrationStatus]='Migrated', "
                f"[Migrated]='True', [MigratedDate]=SYSUTCDATETIME() "
                f"WHERE LOWER(ISNULL([SubmittedBy], '')) = ?", [NAVEEN.lower()])
            cn.commit()

    # ── Test 6: different users simultaneous — both allowed ────────
    t.section("Test 6: Naveen + Ashok submit simultaneously → both 200")
    fresh2 = pick(20)
    n_ids2 = fresh2[:5]
    a_ids2 = fresh2[5:10]
    barrier2 = threading.Barrier(2)
    results2 = {}

    def tab_diff(email, ids):
        with patch.object(pm, "service", fake):
            c = login(email)
            barrier2.wait()
            r = c.post("/api/migrate", json={"ids": ids})
            results2[email] = r.status_code

    th_n = threading.Thread(target=tab_diff, args=(NAVEEN, n_ids2))
    th_a = threading.Thread(target=tab_diff, args=(ASHOK,  a_ids2))
    th_n.start(); th_a.start(); th_n.join(); th_a.join()
    t.check(f"Naveen 200 (got {results2.get(NAVEEN)})",
            results2.get(NAVEEN) == 200)
    t.check(f"Ashok  200 (got {results2.get(ASHOK)})",
            results2.get(ASHOK) == 200)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        print(traceback.format_exc())
        t.failed += 1
    finally:
        try: wipe()
        except Exception: pass
        sys.exit(t.summary())
