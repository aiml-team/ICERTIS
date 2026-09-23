"""Verifies the auth-service email normalization fix.

Bug root-cause guard: without full lowercasing, the same person logging
in as "Naveen@bs.nttdata.com" versus "naveen@bs.nttdata.com" would end
up with TWO distinct session emails.  The per-user migration lock uses
LOWER(SubmittedBy) on the DB side, so the SECOND session (different
case) would query with the mixed-case string, which SQL Server's
LOWER() would coerce — coincidentally correct.  But the persisted
SubmittedBy column would carry whichever case the winning submitter
used, which could confuse audit trails and worst-case let a user
bypass their own lock by re-logging with different casing.

This test proves:
  1. validate_email_domain() always returns fully lowercase.
  2. Logging in with mixed case produces a session with lowercase email.
  3. Two sessions for the same person (different case) share the same
     my_in_processing count.
"""
from __future__ import annotations
import logging, sys, traceback
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
logging.basicConfig(level=logging.WARNING)
logging.getLogger("routes.contracts").setLevel(logging.ERROR)

from fastapi.testclient import TestClient
from core.config import settings
from core.database import get_connection
from services import auth_service
from services.data_service import STATUS_PENDING, count_all


CANONICAL = "casetest-user@bs.nttdata.com"
MIXED     = "CaseTest-USER@BS.NTTdata.COM"


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
            f"WHERE LOWER(ISNULL([SubmittedBy], '')) = ?",
            [CANONICAL.lower()])
        cn.commit()


def pick(n):
    table = settings.CONTRACT_TABLE
    with get_connection() as cn:
        cur = cn.cursor()
        cur.execute(
            f"SELECT TOP {n} [FileID] FROM dbo.[{table}] "
            f"WHERE ISNULL([MigrationStatus], '{STATUS_PENDING}') = '{STATUS_PENDING}' "
            f"  AND ISNULL([Excluded], 'No') = 'No' "
            f"  AND ISNULL([SubmittedBy], '') = ''")
        return [str(r[0]) for r in cur.fetchall()]


class FakePlatform:
    def is_configured(self): return True
    def build_migration_request_payload(self, **kw): return {"name": "fake"}
    def create_migration(self, p): return {"migration_id": "casetest-001"}
    def extract_migration_id(self, o): return o.get("migration_id")
    def build_files_batch_payload(self, files):
        return {"files": [{"local_id": lid} for lid, _ in files]}
    def add_files_batch(self, mid, p):
        return {"items": [{"id": f"it-{i}", "extra_metadata": {"local_file_id": f["local_id"]}}
                          for i, f in enumerate(p["files"])]}
    def correlate_batch_response(self, files, items):
        return {(it.get("extra_metadata") or {}).get("local_file_id"): it.get("id")
                for it in items or [] if it.get("id")}
    def get_migration_files(self, mid): return []
    def group_files_for_submission(self, rows):
        from services.migration_paths import ParsedSource
        files = [(str(r["fileID"]), ParsedSource("https://x/s", "D", "", r["fileName"] or "x.pdf"))
                 for r in rows]
        return ([{"group_key":("https://x/s","D",""),"site_url":"https://x/s",
                  "library":"D","folder_path":"","files":files}], [])


def run():
    _ = count_all()
    wipe()

    # 1. validate_email_domain returns fully lowercase for any input case.
    t.section("§1: validate_email_domain() always returns fully lowercase")
    ok, norm = auth_service.validate_email_domain(MIXED)
    t.check(f"mixed-case '{MIXED}' → valid",  ok, f"got {norm!r}")
    t.check(f"result == canonical lowercase '{CANONICAL}' (got {norm!r})",
            norm == CANONICAL)
    ok2, norm2 = auth_service.validate_email_domain(CANONICAL)
    t.check(f"lower-case '{CANONICAL}' → normalises to same value (got {norm2!r})",
            ok2 and norm2 == CANONICAL)
    ok3, norm3 = auth_service.validate_email_domain("  " + MIXED.upper() + "  ")
    t.check(f"stripped + upper-cased → same canonical (got {norm3!r})",
            ok3 and norm3 == CANONICAL)

    # 2. Session created with mixed-case login → stored email is lowercase
    t.section("§2: login with mixed case → session carries canonical lowercase email")
    from main import app
    c_mixed = TestClient(app)
    r = c_mixed.post("/api/auth/login", json={"email": MIXED})
    t.check("mixed-case login → 200", r.status_code == 200, r.text[:200])
    session_email = r.json().get("email")
    t.check(f"login response email == canonical (got {session_email!r})",
            session_email == CANONICAL)

    # 3. Two sessions for the same person via different case share the
    #    same my_in_processing bucket.
    t.section("§3: mixed-case and lowercase sessions share my_in_processing")
    ids = pick(6)
    t.check(f"picked 6 rows for the user (got {len(ids)})", len(ids) == 6)

    pm = __import__("services.migration_platform", fromlist=["service"])
    fake = FakePlatform()

    # User submits via the MIXED-case login session
    with patch.object(pm, "service", fake):
        rsub = c_mixed.post("/api/migrate", json={"ids": ids})
        t.check("mixed-case session /api/migrate → 200",
                rsub.status_code == 200, rsub.text[:300])

        # New session with LOWER-case email string — must see same
        # my_in_processing because SubmittedBy is stored / compared
        # consistently in lowercase.
        c_lower = TestClient(app)
        r_low = c_lower.post("/api/auth/login", json={"email": CANONICAL})
        t.check("lower-case login → 200", r_low.status_code == 200)

        cm = c_mixed.get("/api/contracts").json()["counts"]
        cl = c_lower.get("/api/contracts").json()["counts"]

    t.check(f"mixed-case session sees my_in_processing == 6 (got {cm.get('my_in_processing')})",
            (cm.get("my_in_processing") or 0) == 6)
    t.check(f"lower-case session sees my_in_processing == 6 (got {cl.get('my_in_processing')})",
            (cl.get("my_in_processing") or 0) == 6)
    t.check("both sessions see the same can_migrate value",
            cm.get("can_migrate") == cl.get("can_migrate") == False)

    # 4. A completely DIFFERENT user must not see this user's count.
    t.section("§4: unrelated user sees my_in_processing == 0")
    OTHER = "casetest-other@bs.nttdata.com"
    c_other = TestClient(app)
    r_other = c_other.post("/api/auth/login", json={"email": OTHER})
    t.check("other-user login → 200", r_other.status_code == 200)
    with patch.object(pm, "service", fake):
        co = c_other.get("/api/contracts").json()["counts"]
    t.check(f"other user: my_in_processing == 0 (got {co.get('my_in_processing')})",
            (co.get("my_in_processing") or 0) == 0)
    t.check(f"other user: can_migrate == True (got {co.get('can_migrate')!r})",
            co.get("can_migrate") is True)
    t.check(f"other user sees SAME global in_processing "
            f"(other={co['in_processing']} mixed={cm['in_processing']})",
            co["in_processing"] == cm["in_processing"])


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
