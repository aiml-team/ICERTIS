"""Power Automate migration integration.

The Power Automate flow is expected to:
  1. Receive a JSON payload of the documents to migrate.
  2. Copy each file from the source SharePoint site to the destination.
  3. Either
       (a) SYNCHRONOUS: return a JSON body describing per-document success/
           failure BEFORE responding to the HTTP call; or
       (b) ASYNCHRONOUS: return 200/202 immediately and post per-document
           results later via a callback endpoint (/api/migrate/callback).

This module isolates the HTTP call so the rest of the app treats migration
as a black box.  It uses only the Python stdlib (urllib) — no new deps.

Configuration (env vars, wired in core.config.Settings):
    POWER_AUTOMATE_URL      trigger URL (secret; never hard-coded)
    POWER_AUTOMATE_TIMEOUT  request timeout in seconds
    POWER_AUTOMATE_SYNC     "1" if the flow returns per-doc results in-line
    POWER_AUTOMATE_API_KEY  optional bearer token

Payload shape (contract with the flow):
    {
      "runId":     "<uuid>",                 # for correlation / logging
      "documents": [
        {
          "fileID":         "337,514",       # unique doc identifier
          "fileName":       "Contract.pdf",
          "sharePointPath": "https://…",     # source URL
          "customerName":   "Acme",
          "agreementName":  "MSA 2026",
          "contractType":   "MSA"
        }, …
      ]
    }

Response shape (only for SYNC mode):
    {
      "results": [
        { "fileID": "…", "success": true },
        { "fileID": "…", "success": false, "error": "…" }
      ]
    }
  OR (looser flavour accepted for compatibility):
    { "succeeded": ["…"], "failed": [{"fileID": "…", "error": "…"}] }

If the URL is not configured, `trigger_migration()` returns
`{"mode": "unconfigured"}` — callers should keep the documents in
'In Processing' and wait for a manual callback.
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
import uuid
from typing import Iterable, List, Tuple

from core.config import settings

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    """True when Power Automate URL is set.  When False, the /api/migrate
    endpoint still moves rows to 'In Processing' and callers should rely
    on a later callback (or manual retry) to complete the flow."""
    return bool(settings.POWER_AUTOMATE_URL)


def trigger_migration(documents: List[dict], run_id: str | None = None) -> dict:
    """POST the batch to the Power Automate flow.

    Returns a dict with one of these shapes:
      • {"mode": "unconfigured"}                    — URL not set
      • {"mode": "async", "runId": "…"}             — flow accepted the job
      • {"mode": "sync", "runId": "…",
         "succeeded": [...], "failed": [{fileID, error}, ...]}
      • {"mode": "error", "error": "…"}             — network / HTTP failure

    Never raises — errors are captured in the returned dict so the caller
    can persist an appropriate row status and surface a user message.
    """
    rid = run_id or str(uuid.uuid4())
    if not is_configured():
        logger.info("trigger_migration: Power Automate URL not configured; runId=%s docs=%d",
                    rid, len(documents))
        return {"mode": "unconfigured", "runId": rid}

    body = json.dumps({"runId": rid, "documents": list(documents)}).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if settings.POWER_AUTOMATE_API_KEY:
        headers["Authorization"] = f"Bearer {settings.POWER_AUTOMATE_API_KEY}"

    req = urllib.request.Request(
        settings.POWER_AUTOMATE_URL,
        data=body,
        headers=headers,
        method="POST",
    )
    ctx = ssl.create_default_context()

    try:
        with urllib.request.urlopen(req, timeout=settings.POWER_AUTOMATE_TIMEOUT, context=ctx) as resp:
            raw = resp.read()
            status = resp.status
            content_type = (resp.headers.get("Content-Type") or "").lower()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        logger.error("trigger_migration: HTTPError %s runId=%s detail=%s", e.code, rid, detail)
        return {"mode": "error", "runId": rid,
                "error": f"Power Automate HTTP {e.code}: {detail or e.reason}"}
    except urllib.error.URLError as e:
        logger.error("trigger_migration: URLError runId=%s reason=%s", rid, e.reason)
        return {"mode": "error", "runId": rid, "error": f"Power Automate unreachable: {e.reason}"}
    except Exception as e:  # timeout, ssl, etc.
        logger.error("trigger_migration: unexpected %s runId=%s", type(e).__name__, rid)
        return {"mode": "error", "runId": rid, "error": f"Power Automate error: {e}"}

    logger.info("trigger_migration: status=%s runId=%s bytes=%d ct=%s",
                status, rid, len(raw), content_type)

    # If we're configured for async, the response body is generally empty
    # or a status envelope; results come later via /api/migrate/callback.
    if not settings.POWER_AUTOMATE_SYNC:
        return {"mode": "async", "runId": rid}

    # SYNC mode — try to parse per-document outcome.
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        logger.error("trigger_migration: unparseable body runId=%s", rid)
        return {"mode": "error", "runId": rid, "error": "Power Automate returned non-JSON body"}

    succeeded, failed = _extract_results(parsed)
    return {
        "mode":      "sync",
        "runId":     rid,
        "succeeded": succeeded,
        "failed":    failed,
    }


def _extract_results(payload: dict) -> Tuple[List[str], List[dict]]:
    """Accept either of the documented response shapes and return
    (succeeded_fileIDs, failed[{fileID, error}])."""
    if not isinstance(payload, dict):
        return [], []

    # Shape A: {"results": [{fileID, success, error?}, …]}
    if isinstance(payload.get("results"), list):
        succ, fail = [], []
        for r in payload["results"]:
            if not isinstance(r, dict):
                continue
            fid = str(r.get("fileID") or r.get("fileId") or "").strip()
            if not fid:
                continue
            if bool(r.get("success")):
                succ.append(fid)
            else:
                fail.append({"fileID": fid, "error": str(r.get("error") or "")})
        return succ, fail

    # Shape B: {"succeeded": [...], "failed": [...]}
    succ_raw = payload.get("succeeded") or []
    fail_raw = payload.get("failed") or []
    succ = [str(x).strip() for x in succ_raw if str(x).strip()]
    fail = []
    for f in fail_raw:
        if isinstance(f, dict):
            fid = str(f.get("fileID") or f.get("fileId") or "").strip()
            if fid:
                fail.append({"fileID": fid, "error": str(f.get("error") or "")})
        else:
            fid = str(f).strip()
            if fid:
                fail.append({"fileID": fid, "error": ""})
    return succ, fail
