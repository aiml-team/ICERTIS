"""Pure path-building helpers for the migration platform.

Split from services/migration_platform.py so these functions can be unit
tested without any HTTP / DB dependencies.  Every function here is:
  * pure (no I/O, no globals mutated),
  * deterministic,
  * safe against percent-encoded input.

Source of truth for the contract:
  SharePoint_Migration_Platform_Technical_Handover.docx §5.1, §8.1, §17.2

Key rules encoded here:

  1. source_site_url         = protocol + host + "/sites/<site>"
  2. source_library          = first path segment after "/sites/<site>"
                               URL-decoded (e.g. "Shared Documents")
  3. source_folder_path      = folder segments between the library and the
                               file name, joined with "/" (empty string when
                               the file is at the library root)
  4. per-file source_path    = "<folder>/<file_name>"    (library NOT prefixed;
                               the migration platform adds the library
                               prefix itself before calling Power Automate —
                               confirmed API contract, Step 2 ADD FILES)
  5. per-file destination_path
                             = "<DEST_FOLDER_PATH>/<original-subfolders>/<file_name>"
                               (Wave 2 rule: preserve source subfolder
                               structure under the configured destination root)

The parser accepts either a fully-qualified URL or a POSIX-ish path,
matches services/data_service.py:folder_segments_for so what we send
to the platform is consistent with what the folder-hierarchy filter
already understands.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote, urlsplit


# Technical SharePoint segments that never appear in the business hierarchy.
# Case-insensitive.  Kept in sync with services.data_service._TECHNICAL_SEG_RE.
_TECHNICAL_SEG_RE = re.compile(r"^(forms|allitems\.aspx?)$", re.I)
_HTTP_RE          = re.compile(r"^https?://", re.I)

# SharePoint "Web Access Point" prefixes that appear on share/preview URLs
# such as   https://<tenant>.sharepoint.com/:b:/r/sites/<Site>/<Library>/…
# The single letter identifies the intended viewer (b=browser/PDF,
# w=Word, x=Excel, p=PowerPoint, o=OneNote, f=folder, u=universal, v=video,
# t=text, i=image).  The optional  "/r"  segment tells SharePoint to
# redirect to the raw file instead of showing the WebViewer chrome.
# These segments are UI-only and must be stripped before we look for
# the business "sites/<name>/<library>/…" shape.
_WEB_ACCESS_PREFIX_RE = re.compile(r"^:[a-z]:$", re.I)


@dataclass(frozen=True)
class ParsedSource:
    """Everything the migration platform needs to describe the source of
    ONE file.  Used to (a) group files into migration requests and
    (b) build the per-file ``source_path`` value."""

    site_url:    str           # "https://tenant.sharepoint.com/sites/<site>"
    library:     str           # decoded library name, e.g. "Shared Documents"
    folder_path: str           # slash-joined subfolders under the library, "" if root
    file_name:   str           # decoded final filename

    @property
    def source_path(self) -> str:
        """Library-RELATIVE path the migration platform expects.

        Per the CONFIRMED API contract (Step 2 – ADD FILES):

            "source_path must NOT include the SharePoint document library
             name.  The migration platform adds the library prefix itself
             before calling Power Automate."

        Example: for URL
            https://itellicloud.sharepoint.com/sites/Legal/Contracts/
                CustomerA/SAP/Agreement.pdf
        with source_library="Contracts", the returned source_path is
        "CustomerA/SAP/Agreement.pdf" — NOT "Contracts/CustomerA/…".
        Files at library root return just the file name.
        """
        parts = []
        if self.folder_path:
            parts.append(self.folder_path)
        parts.append(self.file_name)
        return "/".join(p.strip("/") for p in parts if p)

    @property
    def group_key(self) -> tuple[str, str, str]:
        """Files with the same (site, library, folder_path) triple can be
        added to the same migration request.  See §6 answer:
        'Split into one migration per (site, library, root-folder) tuple'."""
        return (self.site_url, self.library, self.folder_path)


def _decode(s: str) -> str:
    """URL-decode a segment; percent sequences that are already text are left."""
    try:
        return unquote(s)
    except Exception:
        return s


def parse_sharepoint_url(url: str) -> Optional[ParsedSource]:
    """Split an inventory SharePointPath into its four platform-relevant
    pieces.  Returns None if the input isn't a usable SharePoint URL.

    Examples
    --------
    >>> p = parse_sharepoint_url(
    ...   "https://x.sharepoint.com/sites/Legal/Shared%20Documents/A/B/c.pdf")
    >>> p.site_url
    'https://x.sharepoint.com/sites/Legal'
    >>> p.library
    'Shared Documents'
    >>> p.folder_path
    'A/B'
    >>> p.file_name
    'c.pdf'
    >>> p.source_path
    'A/B/c.pdf'

    Files at library root have an empty folder_path:

    >>> parse_sharepoint_url(
    ...   "https://x.sharepoint.com/sites/Legal/Documents/root.pdf").folder_path
    ''
    """
    if not url or not isinstance(url, str):
        return None
    stripped = url.split("#", 1)[0].split("?", 1)[0].strip()
    if not stripped or not _HTTP_RE.match(stripped):
        return None

    try:
        parts = urlsplit(stripped)
    except Exception:
        return None
    if not parts.netloc or not parts.path:
        return None

    segs = [p for p in parts.path.split("/") if p]

    # Strip SharePoint "share URL" chrome that appears in front of the
    # business path — e.g. ":b:/r/sites/Site/Library/…" becomes
    # "sites/Site/Library/…".  We drop:
    #   * the ":x:" web-access-point marker (any single letter),
    #   * an optional single-letter "r" (redirect) segment right after it,
    #   * an optional "sites" grouping that some tenants add before "/sites/".
    while segs and _WEB_ACCESS_PREFIX_RE.match(segs[0]):
        segs.pop(0)
        # After the ":b:" marker SharePoint puts "r" (redirect) or
        # occasionally another single-char segment before the real path.
        # Consume any single-letter segments up to but NOT including
        # "sites".
        while segs and len(segs[0]) == 1 and segs[0].lower() != "sites":
            segs.pop(0)

    if len(segs) < 3:
        # Need at least: sites/<sitename>/<library>/<file>
        return None
    if segs[0].lower() != "sites":
        return None
    site_name = segs[1]
    library_and_rest = segs[2:]
    if len(library_and_rest) < 2:
        # Need library + at least one filename-shaped segment
        return None

    # Drop technical segments (Forms, AllItems.aspx) but keep the library name
    # ("Shared Documents" IS the library, not a technical segment).
    cleaned = [_decode(s) for s in library_and_rest
               if not _TECHNICAL_SEG_RE.match(s)]
    if len(cleaned) < 2:
        return None

    library    = cleaned[0]
    file_name  = cleaned[-1]
    folder_segs = cleaned[1:-1]
    folder_path = "/".join(folder_segs)

    site_url = f"{parts.scheme}://{parts.netloc}/sites/{_decode(site_name)}"
    return ParsedSource(
        site_url    = site_url,
        library     = library,
        folder_path = folder_path,
        file_name   = file_name,
    )


def build_destination_path(
    parsed: ParsedSource,
    dest_root_folder: str,
) -> str:
    """Build ``destination_path`` for one file.

    Rule (Wave 2, confirmed with product): preserve the source subfolder
    structure under the configured destination root, keeping the file name
    unchanged so links keep working.

        source_folder=""            → "<root>/<file_name>"
        source_folder="A/B"         → "<root>/A/B/<file_name>"

    ``dest_root_folder`` may be empty (files land at the destination
    library root); a leading/trailing slash on the root is normalised.
    """
    root = (dest_root_folder or "").strip("/")
    subs = (parsed.folder_path or "").strip("/")
    name = (parsed.file_name  or "").strip("/")
    parts = [p for p in (root, subs, name) if p]
    return "/".join(parts)


def suggest_migration_name(
    prefix: str,
    now_yyyymmdd: str,
    group_key: tuple[str, str, str],
    file_count: int,
    *,
    unique_suffix: str | None = None,
) -> str:
    """Return a compact, human-readable migration name.

    Format:  "<prefix>-<yyyymmdd>-<library>-<folder_slug>-<n>-<unique>"

    ``unique_suffix`` guarantees the name is different across submits
    (the platform enforces UNIQUE(name) — see 409 handling in the
    integration tests).  Callers should pass a per-submit token such as
    ``HHMMSS + short random``.  When omitted (unit tests / backfill) the
    caller is responsible for uniqueness.

    Folder segments are slugified (spaces → dashes) and truncated so the
    name stays well under the platform's VARCHAR(255) column limit.
    """
    _site, library, folder = group_key
    lib_slug = _slug(library, 24)
    fld_slug = _slug(folder,  40) or "root"
    parts = [prefix, now_yyyymmdd, lib_slug, fld_slug, str(file_count)]
    if unique_suffix:
        parts.append(unique_suffix.strip("-"))
    return "-".join(p for p in parts if p)[:200]


def _slug(s: str, max_len: int) -> str:
    """Very small slugifier for names (not paths).  Keeps unicode."""
    if not s:
        return ""
    out = re.sub(r"[\s/\\]+", "-", s.strip())
    out = re.sub(r"-{2,}", "-", out).strip("-")
    return out[:max_len]
