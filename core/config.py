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
    SEED_CSV_PATH: str = str(BASE_DIR / "Contract Inventory.csv")

    # ── Azure SQL connection (runtime source of truth) ───────────────────
    AZURE_SQL_SERVER:   str = os.getenv("AZURE_SQL_SERVER", "")
    AZURE_SQL_DATABASE: str = os.getenv("AZURE_SQL_DATABASE", "")
    AZURE_SQL_USERNAME: str = os.getenv("AZURE_SQL_USERNAME", "")
    AZURE_SQL_PASSWORD: str = os.getenv("AZURE_SQL_PASSWORD", "")
    AZURE_SQL_DRIVER:   str = os.getenv("AZURE_SQL_DRIVER", "ODBC Driver 18 for SQL Server")

    # ── Application ──────────────────────────────────────────────────────
    CONTRACT_TABLE: str = os.getenv("CONTRACT_TABLE", "ContractInventory")

    # ── Power Automate migration flow ────────────────────────────────────
    # The URL is the HTTP trigger of a Power Automate cloud flow that
    # copies a batch of documents from the source SharePoint site into
    # the destination.  If unset, the /api/migrate endpoint still moves
    # rows to 'In Processing' but does NOT auto-complete them — a later
    # webhook / callback can post per-doc results to /api/migrate/callback.
    #
    # POWER_AUTOMATE_URL      full trigger URL (may include signature/SAS)
    # POWER_AUTOMATE_TIMEOUT  request timeout in seconds (default 60)
    # POWER_AUTOMATE_SYNC     "1" if the flow returns per-doc results in
    #                         the response body (synchronous); "0" if it
    #                         only starts a long-running job (default 0)
    # POWER_AUTOMATE_API_KEY  optional bearer/API key (sent as
    #                         "Authorization: Bearer <key>" when present)
    POWER_AUTOMATE_URL:     str  = os.getenv("POWER_AUTOMATE_URL", "")
    POWER_AUTOMATE_TIMEOUT: int  = int(os.getenv("POWER_AUTOMATE_TIMEOUT", "60"))
    POWER_AUTOMATE_SYNC:    bool = os.getenv("POWER_AUTOMATE_SYNC", "0") == "1"
    POWER_AUTOMATE_API_KEY: str  = os.getenv("POWER_AUTOMATE_API_KEY", "")

    # ── Lightweight shared-password login ────────────────────────────────
    # This is NOT an authentication system — it exists only to identify
    # the current user (by company email) and separate concurrent sessions
    # so future migration actions can record `StartedBy` / `SessionId`.
    #
    # APP_SHARED_PASSWORD   the one password every user types (default
    #                       "AIML@2025"; override in .env for production)
    # APP_COMPANY_DOMAIN    the ONLY email domain accepted at login
    #                       (case-insensitive exact match, no subdomains)
    # APP_SESSION_COOKIE    HttpOnly cookie name that carries the session id
    # APP_SESSION_TTL_HOURS session lifetime; expired sessions redirect to
    #                       the login page (default 12h)
    APP_SHARED_PASSWORD:   str = os.getenv("APP_SHARED_PASSWORD", "AIML@2026")
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
