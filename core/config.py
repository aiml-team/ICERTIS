"""Application configuration.

Credentials are loaded from environment variables (or a local `.env` file for
development).  Nothing sensitive is hard-coded.
"""
from pathlib import Path
import os

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent

# Load .env from project root (dev only; in production real env vars win).
load_dotenv(BASE_DIR / ".env")


class Settings:
    # ── Seed source (used ONLY by scripts/seed_database.py) ──────────────
    # Overridable via SEED_CSV_PATH env var so ops can point the seeder at
    # e.g. "Contract Inventory v2.csv" without editing code.  Relative paths
    # are resolved against BASE_DIR (repo root).
    SEED_CSV_PATH: str = (
        os.getenv("SEED_CSV_PATH")
        if os.getenv("SEED_CSV_PATH") and Path(os.getenv("SEED_CSV_PATH")).is_absolute()
        else str(BASE_DIR / (os.getenv("SEED_CSV_PATH") or "Contract Inventory.csv"))
    )

    # ── Azure SQL connection (runtime source of truth) ───────────────────
    AZURE_SQL_SERVER:   str = os.getenv("AZURE_SQL_SERVER", "")
    AZURE_SQL_DATABASE: str = os.getenv("AZURE_SQL_DATABASE", "")
    AZURE_SQL_USERNAME: str = os.getenv("AZURE_SQL_USERNAME", "")
    AZURE_SQL_PASSWORD: str = os.getenv("AZURE_SQL_PASSWORD", "")
    AZURE_SQL_DRIVER:   str = os.getenv("AZURE_SQL_DRIVER", "ODBC Driver 18 for SQL Server")

    # ── Application ──────────────────────────────────────────────────────
    CONTRACT_TABLE: str = os.getenv("CONTRACT_TABLE", "ContractInventory")

    # ── SharePoint File Migration Platform integration ───────────────────
    # This application NO LONGER calls Power Automate directly.  The
    # dedicated migration platform (deployed to Azure Container Apps —
    # see SharePoint_Migration_Platform_Technical_Handover.docx) owns:
    #   orchestration, dispatch, Power Automate call, callbacks,
    #   retry logic, audit trail, idempotency, per-file status.
    #
    # This app only:
    #   1. Creates a migration via POST {base}/api/v1/migrations
    #   2. Adds files via   POST {base}/api/v1/migrations/{id}/files/batch
    #   3. Polls status via GET  {base}/api/v1/migrations/{id}
    #                        + GET {base}/api/v1/migrations/{id}/files/status-counts
    #   4. Fetches audit on demand via
    #        GET {base}/api/v1/migrations/{id}/audit
    #
    # MIGRATION_API_BASE_URL   root of the deployed platform (no trailing /)
    # MIGRATION_API_TIMEOUT    HTTP timeout seconds per call (default 30)
    # MIGRATION_POLL_INTERVAL  browser poll interval in seconds while any
    #                          rows are 'In Processing' (default 15)
    # MIGRATION_DEFAULT_PRIORITY  request priority 0..5 (default 3)
    # MIGRATION_NAME_PREFIX    prepended to auto-generated migration names
    MIGRATION_API_BASE_URL:  str = os.getenv("MIGRATION_API_BASE_URL", "").rstrip("/")
    MIGRATION_API_TIMEOUT:   int = int(os.getenv("MIGRATION_API_TIMEOUT", "30"))
    # Browser polling interval for /api/migrations/sync + /api/contracts.
    # Product spec: 5 seconds by default so multi-user changes appear
    # within one interval on every open page.  Overridable per-env via
    # MIGRATION_POLL_INTERVAL_SECONDS (integer; clamped 2..60 client-side
    # to keep the loop bounded even under a misconfiguration).
    MIGRATION_POLL_INTERVAL: int = int(os.getenv("MIGRATION_POLL_INTERVAL_SECONDS", "5"))
    MIGRATION_DEFAULT_PRIORITY: int = int(os.getenv("MIGRATION_DEFAULT_PRIORITY", "3"))
    MIGRATION_NAME_PREFIX:   str = os.getenv("MIGRATION_NAME_PREFIX", "Wave2")

    # ── Destination SharePoint configuration ─────────────────────────────
    # These end up as destination_site_url / destination_library /
    # destination_folder_path on the migration request payload
    # (handover §5.1).  TODO: set real values in .env before running
    # against the deployed migration platform.
    MIGRATION_DEST_SITE_URL:    str = os.getenv("MIGRATION_DEST_SITE_URL", "")
    MIGRATION_DEST_LIBRARY:     str = os.getenv("MIGRATION_DEST_LIBRARY", "Documents")
    MIGRATION_DEST_FOLDER_PATH: str = os.getenv("MIGRATION_DEST_FOLDER_PATH", "Wave2")

    # ── Migration platform PostgreSQL (direct-read fallback) ─────────────
    # When the platform HTTP API is temporarily unreachable, /api/migrations/sync
    # falls back to reading per-file status directly from the platform's
    # PostgreSQL DB (READ-ONLY — we never write to it).  Set MIGRATION_DB_URL
    # to a valid postgresql:// DSN to enable; leave blank to disable the
    # fallback entirely (sync then works exactly as before).
    #
    # Handover doc: DATABASE_REFERENCE.md §1.  Password comes from
    # .azure-secrets.local (PG_PASSWORD) — never hard-code.
    MIGRATION_DB_URL:              str = os.getenv("MIGRATION_DB_URL", "")
    MIGRATION_DB_CONNECT_TIMEOUT:  int = int(os.getenv("MIGRATION_DB_CONNECT_TIMEOUT", "10"))

    # ── Legacy Power Automate settings — DEPRECATED ──────────────────────
    # Kept only so old .env files do not break process startup.  The
    # POST /api/migrate route no longer reads these; all migration
    # dispatch goes through MigrationPlatformService.  Remove after
    # everyone has migrated their .env files.
    POWER_AUTOMATE_URL:     str  = os.getenv("POWER_AUTOMATE_URL", "")
    POWER_AUTOMATE_TIMEOUT: int  = int(os.getenv("POWER_AUTOMATE_TIMEOUT", "60"))
    POWER_AUTOMATE_SYNC:    bool = os.getenv("POWER_AUTOMATE_SYNC", "0") == "1"
    POWER_AUTOMATE_API_KEY: str  = os.getenv("POWER_AUTOMATE_API_KEY", "")

    # ── Lightweight email-only login ─────────────────────────────────────
    # This is NOT an authentication system — it exists only to identify
    # the current user (by company email) and separate concurrent sessions
    # so future migration actions can record `StartedBy` / `SessionId`.
    #
    # Password-based login was removed at product request — the previous
    # APP_SHARED_PASSWORD variable is no longer read.
    #
    # APP_COMPANY_DOMAIN    the ONLY email domain accepted at login
    #                       (case-insensitive exact match, no subdomains)
    # APP_SESSION_COOKIE    HttpOnly cookie name that carries the session id
    # APP_SESSION_TTL_HOURS session lifetime; expired sessions redirect to
    #                       the login page (default 12h)
    APP_COMPANY_DOMAIN:    str = os.getenv("APP_COMPANY_DOMAIN", "bs.nttdata.com")
    APP_SESSION_COOKIE:    str = os.getenv("APP_SESSION_COOKIE", "cmr_session")
    APP_SESSION_TTL_HOURS: int = int(os.getenv("APP_SESSION_TTL_HOURS", "12"))
    # Secure cookie flag — turn on when deployed over HTTPS.
    APP_SESSION_COOKIE_SECURE: bool = os.getenv("APP_SESSION_COOKIE_SECURE", "0") == "1"

    @property
    def odbc_connection_string(self) -> str:
        return (
            f"Driver={{{self.AZURE_SQL_DRIVER}}};"
            f"Server=tcp:{self.AZURE_SQL_SERVER},1433;"
            f"Database={self.AZURE_SQL_DATABASE};"
            f"Uid={self.AZURE_SQL_USERNAME};"
            f"Pwd={self.AZURE_SQL_PASSWORD};"
            "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
        )


settings = Settings()
