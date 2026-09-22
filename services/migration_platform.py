"""SharePoint File Migration Platform HTTP client.

This is the ONLY place in the application that makes HTTP calls to the
migration platform (deployed at MIGRATION_API_BASE_URL — see
SharePoint_Migration_Platform_Technical_Handover.docx §7 / §10.1).

Contract summary
────────────────
    POST {base}/api/v1/migrations                          create migration
    POST {base}/api/v1/migrations/{id}/files/batch         add files (batched)
    GET  {base}/api/v1/migrations/{id}                     migration + counts
    GET  {base}/api/v1/migrations/{id}/files               per-file status
    GET  {base}/api/v1/migrations/{id}/files/status-counts quick counters
    GET  {base}/api/v1/migrations/{id}/audit               event log

The application MUST NOT call Power Automate directly.  Power Automate is
driven by the migration platform's own worker (handover §2.2, §8).

Status mapping (single source of truth — do NOT duplicate elsewhere)
────────────────────────────────────────────────────────────────────
Backend file-status enum  →  local UI bucket (services/data_service.STATUS_*)

    pending      ─┐
    queued        ├─► In Processing   (row shows "In Processing" or
    in_progress   │                     "Retrying (N of M)" while retrying)
    retrying     ─┘
    completed        Migrated
    failed           Failed            (UI bucket = "Yet to Be Migrated" —
                                        keeps the A = B+C+D+E identity)
    skipped          Skipped           (NOT the same as business Excluded)

Idempotency, retry logic, callbacks, and Power Automate details are ALL
owned by the platform.  We only observe.
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Iterable

from core.config import settings
from services.migration_paths import (
    ParsedSource,
    build_destination_path,
    parse_sharepoint_url,
    suggest_migration_name,
)

logger = logging.getLogger(__name__)


# ── Status-mapping constants ──────────────────────────────────────────────
# Kept as frozen sets so callers cannot accidentally mutate them.
#
# Confirmed migration statuses (Step 4):
#     pending, in_progress, completed, failed, cancelled
# May also expose intermediate states — per contract "do not break if
# those appear":  dispatching, paused
#
# Confirmed file statuses (Step 5):
#     pending → queued → in_progress → completed
#     failed → retrying → queued
#
# 'cancelled' is a terminal non-success state — mapped to Failed so it
# leaves the In Processing bucket.
# 'paused' is not terminal — user/admin can resume, so keep it In Processing.
_BACKEND_IN_PROCESSING = frozenset({
    "pending", "queued", "in_progress", "retrying",
    "dispatching", "paused",
})
_BACKEND_COMPLETED     = frozenset({"completed"})
_BACKEND_FAILED        = frozenset({"failed", "cancelled"})
_BACKEND_SKIPPED       = frozenset({"skipped"})

# UI-facing local statuses (aligned with services.data_service constants
# but declared here as strings to avoid a circular import).
UI_STATUS_IN_PROCESSING = "In Processing"
UI_STATUS_MIGRATED      = "Migrated"
UI_STATUS_FAILED        = "Failed"
UI_STATUS_SKIPPED       = "Skipped"  # not mapped to Excluded per §13


def map_backend_status(backend_status: str | None) -> str | None:
    """Translate the platform's raw file-status enum into the local UI
    bucket string persisted in ContractInventory.MigrationStatus.

    Returns None when the input is unknown/empty so callers can decide
    whether to leave the current status untouched (safer than guessing).
    """
    if not backend_status:
        return None
    b = str(backend_status).strip().lower()
    if b in _BACKEND_IN_PROCESSING:
        return UI_STATUS_IN_PROCESSING
    if b in _BACKEND_COMPLETED:
        return UI_STATUS_MIGRATED
    if b in _BACKEND_FAILED:
        return UI_STATUS_FAILED
    if b in _BACKEND_SKIPPED:
        return UI_STATUS_SKIPPED
    logger.warning("map_backend_status: unknown backend status %r", backend_status)
    return None


def _extract_id(obj: dict | None, *field_names: str) -> str:
    """Return the first non-empty string value found under any of the
    given key names in ``obj``.  Handles the confirmed contract where
    the create-migration response carries ``migration_id`` while some
    implementations carry ``id`` — accept either without guessing."""
    if not isinstance(obj, dict):
        return ""
    for k in field_names:
        v = obj.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


class MigrationPlatformError(RuntimeError):
    """Raised when the platform returns a non-2xx or an unparseable body.
    Contains an optional ``status_code`` and ``detail`` for the caller."""

    def __init__(self, message: str, status_code: int | None = None,
                 detail: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class MigrationPlatformService:
    """Reusable HTTP client wrapper.

    Design goals:
      * one class instance shared across the process (thread-safe: stdlib
        urllib is stateless),
      * no direct HTTP calls anywhere else in the app,
      * timeouts + structured logging on every call,
      * NEVER logs the base URL query string / secret headers (there
        aren't any today, but this keeps the code safe if that changes),
      * no external dependencies (uses stdlib urllib — same choice as
        services/power_automate.py, matches project's zero-new-deps rule).
    """

    def __init__(self,
                 base_url:    str | None = None,
                 timeout:     int | None = None,
                 default_priority: int | None = None):
        self.base_url = (base_url or settings.MIGRATION_API_BASE_URL or "").rstrip("/")
        self.timeout  = int(timeout if timeout is not None else settings.MIGRATION_API_TIMEOUT)
        self.default_priority = int(default_priority
                                    if default_priority is not None
                                    else settings.MIGRATION_DEFAULT_PRIORITY)

    # ── Config / health ──────────────────────────────────────────────────
    def is_configured(self) -> bool:
        """True when MIGRATION_API_BASE_URL is set.  When False, callers
        MUST treat the platform as unreachable and NOT flip any rows to
        'In Processing' — the /api/migrate route surfaces a clear error."""
        return bool(self.base_url)

    # ── Payload builders (public — reused by /api/migrate and by tests) ──
    def build_migration_request_payload(
        self,
        *,
        group_key: tuple[str, str, str],
        file_count: int,
        created_by: str,
        now: datetime | None = None,
    ) -> dict:
        """Build the JSON body for POST /api/v1/migrations.

        Contract (Step 1 – CREATE MIGRATION) — required fields:
            name, created_by, source_site_url, source_library,
            destination_site_url, destination_library
        Optional fields:
            source_folder_path, destination_folder_path, priority

        ``created_by`` MUST be populated (contract §CREATED_BY).  Caller
        passes the current authenticated user's email; if unavailable,
        the caller should pass a well-defined fallback such as "system".
        """
        site_url, library, folder_path = group_key
        ts = now or datetime.utcnow()
        yyyymmdd = ts.strftime("%Y%m%d")
        # Per-submit unique token: HHMMSS + 4-char random.  The platform
        # enforces UNIQUE(name) at DB level (returns 409 on collision —
        # see live-tested behaviour 2026-09-22).  Two submits of the
        # same (library, folder, file_count) on the same day therefore
        # need distinct names.  HHMMSS handles same-day-different-second
        # cases; the 4-char random covers same-second races (rapid
        # retries, parallel groups) and gives us ~1.7M-name headroom
        # before a birthday-collision risk.
        import secrets
        unique_suffix = f"{ts.strftime('%H%M%S')}-{secrets.token_hex(2)}"
        # extra_metadata is NOT in the confirmed contract but is a
        # commonly-supported field on similar platforms.  Keep it small
        # and safe to drop server-side (never rely on the platform to
        # echo it back — correlation of file items → local FileID is
        # done via per-file extra_metadata.local_file_id instead).
        payload: dict = {
            "name":                    suggest_migration_name(
                settings.MIGRATION_NAME_PREFIX, yyyymmdd, group_key, file_count,
                unique_suffix=unique_suffix),
            "created_by":              created_by,
            "priority":                self.default_priority,
            "source_site_url":         site_url,
            "source_library":          library,
            "source_folder_path":      folder_path,
            "destination_site_url":    settings.MIGRATION_DEST_SITE_URL,
            "destination_library":     settings.MIGRATION_DEST_LIBRARY,
            "destination_folder_path": settings.MIGRATION_DEST_FOLDER_PATH,
        }
        return payload

    def build_files_batch_payload(
        self,
        files: Iterable[tuple[str, ParsedSource]],
    ) -> dict:
        """Build the JSON body for POST /api/v1/migrations/{id}/files/batch.

        Contract (Step 2 – ADD FILES) — required fields per item:
            file_name, source_path, destination_path

        Notes:
          * ``source_path`` must be library-RELATIVE (see
            ParsedSource.source_path docstring).  The platform prepends
            the library itself.
          * ``local_file_id`` is included in extra_metadata (an optional
            free-form field common to REST platforms).  If the platform
            drops unknown fields, the caller falls back to correlating
            by (source_path, file_name) via GET /files — see
            correlate_batch_response().
        """
        items = []
        for local_id, parsed in files:
            items.append({
                "file_name":        parsed.file_name,
                "source_path":      parsed.source_path,       # library-relative
                "destination_path": build_destination_path(
                    parsed, settings.MIGRATION_DEST_FOLDER_PATH),
                # Best-effort correlation aid; contract doesn't require it.
                # correlate_batch_response() has a source_path fallback.
                "extra_metadata": {
                    "local_file_id": local_id,
                },
            })
        return {"files": items}

    @staticmethod
    def correlate_batch_response(
        submitted_files: list[tuple[str, ParsedSource]],
        response_items: list[dict],
    ) -> dict[str, str]:
        """Map local FileID → platform file_item_id from a batch response.

        Tries three strategies in order (first hit wins):
          1. ``extra_metadata.local_file_id`` echoed on the response item.
          2. ``source_path`` match (per contract Step 2 — source_path is
             unique within a migration by construction).
          3. ``file_name`` match (fallback — only unambiguous when file
             names are unique within the migration).

        Returns a dict of local_id → file_item_id.  Missing correlations
        are simply absent from the dict; the caller is expected to fill
        them in via a follow-up GET /files call (see get_migration_files).
        """
        result: dict[str, str] = {}
        by_source: dict[str, str] = {}
        by_name:   dict[str, str] = {}
        used_ids: set[str] = set()

        for item in response_items:
            if not isinstance(item, dict):
                continue
            item_id = _extract_id(item, "file_item_id", "id")
            if not item_id:
                continue
            extra = item.get("extra_metadata") or {}
            lid = str(extra.get("local_file_id") or "").strip()
            if lid and lid not in used_ids:
                result[lid] = item_id
                used_ids.add(item_id)
                continue
            sp = str(item.get("source_path") or "").strip()
            if sp and sp not in by_source:
                by_source[sp] = item_id
            fn = str(item.get("file_name") or "").strip()
            if fn and fn not in by_name:
                by_name[fn] = item_id

        for local_id, parsed in submitted_files:
            if local_id in result:
                continue
            if parsed.source_path in by_source:
                result[local_id] = by_source[parsed.source_path]
                continue
            if parsed.file_name in by_name:
                result[local_id] = by_name[parsed.file_name]
        return result

    def group_files_for_submission(
        self,
        rows: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        """Split a mixed selection into per-group submissions.

        Returns (groups, invalid_rows) where each ``group`` is:
            {
              "group_key":     (site, library, folder),
              "site_url":      str,
              "library":       str,
              "folder_path":   str,
              "files":         [(local_file_id, ParsedSource), ...],
            }
        and ``invalid_rows`` contains rows whose SharePoint path could not
        be parsed (missing / malformed).  Caller decides how to surface
        them to the user.
        """
        by_group: dict[tuple[str, str, str], dict] = {}
        invalid: list[dict] = []
        for row in rows:
            path = (row.get("sharePointPath") or "").strip()
            parsed = parse_sharepoint_url(path) if path else None
            if parsed is None:
                invalid.append(row)
                continue
            g = by_group.setdefault(parsed.group_key, {
                "group_key":   parsed.group_key,
                "site_url":    parsed.site_url,
                "library":     parsed.library,
                "folder_path": parsed.folder_path,
                "files":       [],
            })
            g["files"].append((row["fileID"], parsed))
        return list(by_group.values()), invalid

    # ── HTTP wrappers ────────────────────────────────────────────────────
    def create_migration(self, payload: dict) -> dict:
        """POST /api/v1/migrations → returns the created record (dict).

        The response is expected to carry the migration ID under one of
        ``migration_id`` (confirmed contract) or ``id`` (some builds).
        Callers should use ``extract_migration_id()`` — never hard-code
        one key name."""
        return self._request("POST", "/api/v1/migrations", body=payload)

    @staticmethod
    def extract_migration_id(create_response: dict) -> str:
        """Read the migration ID from a create_migration response,
        accepting either the ``migration_id`` (confirmed contract) or
        ``id`` field.  Returns "" when neither is present so the caller
        can raise a clear error."""
        return _extract_id(create_response, "migration_id", "id")

    def add_file(self, migration_id: str, payload: dict) -> dict:
        """POST /api/v1/migrations/{id}/files (single file).

        Contract Step 2 exposes this endpoint alongside the batch endpoint.
        Prefer ``add_files_batch`` for multi-file selections to avoid N
        HTTP round-trips.  Included here for completeness and to satisfy
        the "use the batch endpoint whenever multiple files belong to the
        same migration request" rule (single file → single endpoint)."""
        return self._request(
            "POST", f"/api/v1/migrations/{migration_id}/files",
            body=payload,
        )

    def add_files_batch(self, migration_id: str, payload: dict) -> dict:
        """POST /api/v1/migrations/{id}/files/batch → returns the created
        file items.  Prefer over per-file POST to avoid N HTTP round-trips.

        Response shape may be a bare list, a ``{"files": [...]}`` envelope,
        or ``{"items": [...]}``. Callers should use
        ``correlate_batch_response()`` rather than assume a shape."""
        return self._request(
            "POST", f"/api/v1/migrations/{migration_id}/files/batch",
            body=payload,
        )

    def get_migration(self, migration_id: str) -> dict:
        """GET /api/v1/migrations/{id} → full migration record (status,
        counters, timestamps).  Used by /api/migrations/sync."""
        return self._request("GET", f"/api/v1/migrations/{migration_id}")

    def get_migration_files(self, migration_id: str) -> list[dict]:
        """GET /api/v1/migrations/{id}/files → list of file items with
        per-file status, retry_count, error_message, destination_url."""
        resp = self._request("GET", f"/api/v1/migrations/{migration_id}/files")
        # The platform's list endpoints return either a bare list or a
        # {"items": [...], "total": N} envelope depending on pagination.
        # Handle both defensively.
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict) and isinstance(resp.get("items"), list):
            return resp["items"]
        logger.warning("get_migration_files: unexpected shape %r", type(resp))
        return []

    def get_status_counts(self, migration_id: str) -> dict:
        """GET /api/v1/migrations/{id}/files/status-counts → shortcut for
        the file-status counter tally without pulling every file item."""
        return self._request(
            "GET", f"/api/v1/migrations/{migration_id}/files/status-counts",
        )

    def get_audit(self, migration_id: str) -> list[dict]:
        """GET /api/v1/migrations/{id}/audit → event log for the detail view."""
        resp = self._request("GET", f"/api/v1/migrations/{migration_id}/audit")
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict) and isinstance(resp.get("items"), list):
            return resp["items"]
        return []

    # ── HTTP core ────────────────────────────────────────────────────────
    def _request(self, method: str, path: str,
                 *, body: dict | None = None) -> Any:
        """Perform ONE HTTP call.  Raises MigrationPlatformError on any
        network / non-2xx / non-JSON failure so the caller can surface a
        user-friendly message and leave DB state untouched.
        """
        if not self.is_configured():
            raise MigrationPlatformError(
                "MIGRATION_API_BASE_URL is not configured. "
                "Set it in the environment before submitting migrations.",
                status_code=503,
            )
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        # SSL context — prefer certifi's CA bundle so the platform's
        # Azure Container Apps cert chain verifies cleanly on macOS
        # (where Python's default trust store often can't reach the
        # system keychain and raises CERTIFICATE_VERIFY_FAILED).
        # Falls back to the default context if certifi is unavailable
        # (e.g. locked-down prod image where the OS CA bundle is used).
        try:
            import certifi  # noqa: WPS433 — deliberately local import
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ctx = ssl.create_default_context()

        # Structured log — path only, no query string, no body.  If the
        # platform ever requires an Authorization header we still won't
        # leak it here.
        logger.info("migration_platform: %s %s (timeout=%ss)", method, path, self.timeout)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:1000]
            except Exception:
                pass
            logger.error("migration_platform: HTTP %s on %s %s — %s",
                         e.code, method, path, detail[:200])
            raise MigrationPlatformError(
                f"Migration platform returned HTTP {e.code}",
                status_code=e.code,
                detail=detail,
            ) from e
        except urllib.error.URLError as e:
            logger.error("migration_platform: URL error on %s %s — %s",
                         method, path, e.reason)
            raise MigrationPlatformError(
                f"Migration platform unreachable: {e.reason}",
                status_code=None,
            ) from e
        except Exception as e:  # timeout, SSL, etc.
            logger.error("migration_platform: %s on %s %s",
                         type(e).__name__, method, path)
            raise MigrationPlatformError(
                f"Migration platform call failed: {e}",
                status_code=None,
            ) from e

        if status < 200 or status >= 300:
            raise MigrationPlatformError(
                f"Migration platform returned HTTP {status}",
                status_code=status,
                detail=raw[:1000].decode("utf-8", "replace"),
            )

        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            logger.error("migration_platform: unparseable JSON on %s %s", method, path)
            raise MigrationPlatformError(
                "Migration platform returned non-JSON body",
                status_code=status,
                detail=raw[:200].decode("utf-8", "replace"),
            ) from e


# Shared singleton — routes call this instead of instantiating.  Kept as a
# module-level attribute so tests can monkey-patch it cleanly.
service = MigrationPlatformService()
