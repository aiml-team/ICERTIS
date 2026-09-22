/* ── Column definitions ─────────────────────────────────────────────────── */
// Every column carries an optional `group` tag used ONLY by the Columns
// picker to render section headings.  Untagged columns fall under "General".
const COL_CFG = [
  { f: 'fileName',               h: 'File Name',           ft: 'text', w: 320 },
  { f: 'customerName',           h: 'Customer Name',       ft: 'text', w: 170 },
  { f: 'agreementName',          h: 'Agreement Name',      ft: 'text', w: 210 },
  { f: 'contractType',           h: 'Contract Type',       ft: 'text', w: 120 },
  { f: 'contractClassification', h: 'Classification',      ft: 'text', w: 130 },
  { f: 'ae',                     h: 'AE',                  ft: 'text', w: 120 },
  { f: 'legalEntity',            h: 'Legal Entity',        ft: 'text', w: 180 },
  { f: 'effectiveDate',          h: 'Effective Date',      ft: 'date', w: 105 },
  { f: 'endDate',                h: 'End Date',            ft: 'date', w: 100 },
  { f: 'extractionStatus',       h: 'Extraction Status',   ft: 'text', w: 120 },
  { f: 'reviewRequired',         h: 'Review Required',     ft: 'bool', w: 110 },
  { f: 'missingFields',          h: 'Missing Fields',      ft: 'text', w: 190 },
  { f: 'migrate',                h: 'Migrate',             ft: 'text', w: 80  },
  { f: 'migratedDate',           h: 'Migrated Date',       ft: 'date', w: 180 },
  // Migration Status — rich cell: In Processing (Retrying N/M) · Destination
  // link when Migrated · Failed with error tooltip.  Sourced entirely from
  // columns the sync poller keeps fresh (migrationStatus + retryCount +
  // destinationUrl + errorMessage + migrationRequestId).
  { f: 'migrationStatus',        h: 'Migration Status',    ft: 'text', w: 200 },
  { f: 'fileID',                 h: 'File ID',             ft: 'text', w: 80  },
  // hidden by default
  { f: 'opportunityID',          h: 'Opportunity ID',      ft: 'text', w: 120, hide: true },
  { f: 'orderNumber',            h: 'Order Number',        ft: 'text', w: 110, hide: true },
  { f: 'autoRenewalStatus',      h: 'Auto-Renewal',        ft: 'text', w: 100, hide: true },
  { f: 'startDate',              h: 'Start Date',          ft: 'date', w: 100, hide: true },
  { f: 'expiryDate',             h: 'Expiry Date',         ft: 'date', w: 100, hide: true },
  { f: 'associatedMSAFileName',  h: 'Associated MSA',      ft: 'text', w: 200, hide: true },
  { f: 'associatedNDAFileName',  h: 'Associated NDA',      ft: 'text', w: 200, hide: true },
  { f: 'voidExclusionIndicator', h: 'Void/Exclusion',      ft: 'text', w: 110, hide: true },
  { f: 'processedDate',          h: 'Processed Date',      ft: 'date', w: 105, hide: true },
  { f: 'errorMessage',           h: 'Error Message',       ft: 'text', w: 200, hide: true },
  { f: 'migrated',               h: 'Migrated (source)',   ft: 'bool', w: 110, hide: true },
  { f: 'runId',                  h: 'Run ID',              ft: 'text', w: 200, hide: true },
  { f: 'sharePointPath',         h: 'SharePoint Path',     ft: 'text', w: 300, hide: true },
];

// ── SharePoint hierarchy columns (Folder 1..20) ─────────────────────────
// Persisted server-side (dbo.ContractInventory.Folder01..Folder20) and
// exposed under a dedicated group so the Columns picker can render them
// as a separate section.  Only Folder 1-4 are visible by default —
// SharePoint paths rarely go deeper for the common workflow, and
// showing all 20 would make the table unusably wide.
const FOLDER_LEVEL_MAX = 20;
const FOLDER_DEFAULT_VISIBLE = 4;   // Folders 1..4 visible by default
for (let i = 1; i <= FOLDER_LEVEL_MAX; i++) {
  COL_CFG.push({
    f:      `folder${i}`,
    h:      `Folder ${i}`,
    ft:     'text',
    w:      140,
    hide:   i > FOLDER_DEFAULT_VISIBLE,
    group:  'SharePoint Hierarchy',
  });
}
// Fields (in addition to SEARCHABLE) the folder filter scans.  Order is
// preserved so the "Matched: Folder N = value" tooltip picks the shallowest
// matching level first (matches user intuition — top of the tree wins).
const FOLDER_FIELDS = Array.from({length: FOLDER_LEVEL_MAX}, (_, i) => `folder${i + 1}`);

const SEARCHABLE = ['fileName','customerName','agreementName','opportunityID','ae','contractType','contractClassification','legalEntity'];

/* ── State ──────────────────────────────────────────────────────────────── */
const state = {
  allData:          [],
  filteredData:     [],
  sortCol:          null,
  sortDir:          null,
  columnFilters:    {},
  globalSearch:     '',
  hiddenCols:       new Set(COL_CFG.filter(c => c.hide).map(c => c.f)),
  colWidths:        {},
  selectedIds:      new Set(),
  pageSize:         100,   // fixed — pagination shows exactly 100 rows/page
  page:             1,
  activeFilter:     null,
  showColPanel:     false,
  // migration-status bucket filter: 'all' | 'migrated' | 'pending'
  //                                | 'in_processing'
  // Note: 'excluded' is NOT a filter — clicking that bucket switches to a
  // separate view (state.currentView = 'excluded').
  // 'selected' bucket was removed from the KPI row; the value 'selected'
  // is retained here only as legacy no-op to keep older bookmarks safe.
  bucketFilter:     'all',
  // ── Global folder filter (Philippe requirement) ───────────────────────
  // Searches across folder1..folder20 for ALL rows regardless of the
  // folder navigator's current scope.  When active, applyFiltersAndSort()
  // scans every level and annotates each returned row with __folderMatch
  // = { level: 'Folder 4', value: 'Services' } so the UI can render a
  // tooltip / chip explaining why the row is present.  Cleared by the
  // Clear Filters button along with all other filters.
  folderFilter: {
    text: '',                      // user's raw search text (trimmed)
    mode: 'contains',              // 'contains' | 'starts_with' | 'exact'
  },
  // records fetched from the database via /api/contracts on Start
  sourceData:       [],
  sourceCount:      0,

  /* ── Excluded documents (recoverable soft-delete) ─────────────────────
   * Excluded rows live on the SAME master ContractInventory table with
   * Excluded='Yes' as a status flag (no physical row move).  The UI fetches
   * them on demand via /api/excluded and lets the user Restore them
   * (UPDATE Excluded='No') back into the active list.
   * ─────────────────────────────────────────────────────────────────── */
  currentView:        'review',   // 'review' | 'excluded'
  excludedData:       [],
  excludedSelectedIds: new Set(),
  excludedPage:       1,
  excludedLoaded:     false,      // set true after first /api/excluded fetch
  // Server-authoritative population counts (from /api/contracts.counts).
  // `total` = active + excluded (matches the Total Documents bucket which
  // shows every row on the master table).  `active` excludes rows with
  // Excluded='Yes'.  Kept in sync with state.allData by exclude/restore
  // flows so a reload without a refetch stays correct.
  //
  // Per-user workload-lock fields (populated when the session is
  // authenticated — always true in production):
  //   my_in_processing — how many of the overall in_processing rows
  //                      were submitted by the logged-in user
  //   my_active_total  — same, across every ACTIVE_MIGRATION_STATUSES
  //                      value (identical today; future-proofed)
  //   can_migrate      — bool; false → Migrate button MUST be disabled
  //                      regardless of the user's row selection.
  //                      Server enforces this too (409 on /api/migrate).
  populationCounts:   { active: 0, excluded: 0, total: 0,
                        my_in_processing: 0, my_active_total: 0,
                        can_migrate: true },

  // Migration destination configuration echoed by GET /api/contracts.
  // Used ONLY by the Migrate-confirm modal to preview the real
  // destination path (no secrets — just site/library/folder from the
  // MIGRATION_DEST_* env vars).  Populated on Start; may be empty
  // strings if the server hasn't been configured yet, in which case
  // the modal falls back to a neutral "(destination not configured)"
  // string instead of the old hardcoded "Wave2 destination/…".
  migrationConfig:    { destSiteUrl: '', destLibrary: '', destFolderPath: '' },

  /* ── Folder navigator ──────────────────────────────────────────────────
   * Derived at load-time from row.sharePointPath and rebuilt whenever
   * state.allData changes (exclude / restore).  folderPath is the array
   * of segment names representing the currently-selected scope; empty
   * array = root ("All Contracts") = show everything.
   *
   * folderTree is a nested { name, children:Map<name,node>, count } map;
   * folderIndex is a per-row Set of ancestor-path keys so recursive
   * filtering is O(1) per row.  Both are built by rebuildFolderTree().
   * ─────────────────────────────────────────────────────────────────── */
  folderPath:  [],
  folderTree:  null,
  folderIndex: new WeakMap(),  // row → Set<pathKey>
};

// Restore hidden cols from localStorage; ensure migration columns are always visible
try {
  const s = localStorage.getItem('cmr-hidden');
  if (s) state.hiddenCols = new Set(JSON.parse(s));
} catch {}
state.hiddenCols.delete('migrate');
state.hiddenCols.delete('migratedDate');

/* ── Utility ────────────────────────────────────────────────────────────── */
function cellStr(row, field) {
  const v = row[field];
  if (v === null || v === undefined) return '';
  if (typeof v === 'boolean') return v ? 'True' : 'False';
  return String(v);
}

function parseDate(s) {
  if (!s) return null;
  // Date-only: M/D/YYYY or MM/DD/YYYY (US)
  if (/^\d{1,2}\/\d{1,2}\/\d{4}$/.test(s)) {
    const [m, d, y] = s.split('/').map(Number);
    return new Date(y, m - 1, d);
  }
  // ISO date-only: YYYY-MM-DD
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) {
    const [y, m, d] = s.split('-').map(Number);
    return new Date(y, m - 1, d);
  }
  // MigratedDate format: M/D/YYYY H:MM[:SS] AM/PM  (also handled without seconds)
  const m1 = /^(\d{1,2})\/(\d{1,2})\/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)$/i.exec(s);
  if (m1) {
    let [_, mo, da, yr, hh, mm, ss, ap] = m1;
    mo = +mo; da = +da; yr = +yr; hh = +hh; mm = +mm; ss = ss ? +ss : 0;
    if (/PM/i.test(ap) && hh !== 12) hh += 12;
    if (/AM/i.test(ap) && hh === 12) hh = 0;
    return new Date(yr, mo - 1, da, hh, mm, ss);
  }
  // 24-hour variant: M/D/YYYY HH:MM[:SS]   (e.g. ProcessedDate "9/18/2026 11:13")
  const m2 = /^(\d{1,2})\/(\d{1,2})\/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec(s);
  if (m2) {
    let [_, mo, da, yr, hh, mm, ss] = m2;
    return new Date(+yr, +mo - 1, +da, +hh, +mm, ss ? +ss : 0);
  }
  // ISO datetime: YYYY-MM-DD[ T]HH:MM[:SS[.fff]][Z]
  const m3 = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?/.exec(s);
  if (m3) {
    let [_, yr, mo, da, hh, mm, ss] = m3;
    return new Date(+yr, +mo - 1, +da, +hh, +mm, ss ? +ss : 0);
  }
  return null;
}

/* ── US date formatting (UI display only) ────────────────────────────────
 * All date columns render as MM/DD/YYYY.  MigratedDate additionally shows
 * hh:mm:ss AM/PM so migration-run timestamps stay auditable.
 *
 * These functions normalise the raw string from the API into a canonical
 * displayable string ONCE at ingest (nextBtn handler).  The rest of the
 * pipeline (filter/sort/search/export) then keeps working because
 * parseDate() accepts both padded and non-padded forms.
 * ──────────────────────────────────────────────────────────────────────── */
const _pad2 = n => String(n).padStart(2, '0');

function _toDateObj(raw) {
  if (raw === null || raw === undefined || raw === '') return null;
  if (raw instanceof Date) return isNaN(raw.getTime()) ? null : raw;
  const d = parseDate(String(raw));
  return (d && !isNaN(d.getTime())) ? d : null;
}

function toUsDate(raw) {
  const d = _toDateObj(raw);
  if (!d) return '';
  return `${_pad2(d.getMonth() + 1)}/${_pad2(d.getDate())}/${d.getFullYear()}`;
}

function toUsDateTime(raw) {
  const d = _toDateObj(raw);
  if (!d) return '';
  const h = d.getHours();
  const ampm = h >= 12 ? 'PM' : 'AM';
  const h12 = h % 12 || 12;
  return `${_pad2(d.getMonth() + 1)}/${_pad2(d.getDate())}/${d.getFullYear()} `
       + `${_pad2(h12)}:${_pad2(d.getMinutes())}:${_pad2(d.getSeconds())} ${ampm}`;
}

// Field-level normaliser — mutates the incoming row in place.
// Extend DATE_ONLY_FIELDS / DATE_TIME_FIELDS if new date columns are added.
const DATE_ONLY_FIELDS = ['effectiveDate','startDate','endDate','expiryDate','processedDate'];
const DATE_TIME_FIELDS = ['migratedDate'];
function normaliseDatesInRow(row) {
  for (const f of DATE_ONLY_FIELDS) if (f in row) row[f] = toUsDate(row[f]);
  for (const f of DATE_TIME_FIELDS) if (f in row) row[f] = toUsDateTime(row[f]);
  return row;
}

function passesFilter(row, field, filter) {
  if (!filter) return true;
  const str = cellStr(row, field);
  if (filter.type === 'set') {
    return filter.values.length === 0 || filter.values.includes(str);
  }
  if (filter.type === 'text') {
    const lower = str.toLowerCase();
    const val = (filter.value || '').toLowerCase();
    switch (filter.op) {
      case 'contains':    return lower.includes(val);
      case 'notContains': return !lower.includes(val);
      case 'equals':      return lower === val;
      case 'notEquals':   return lower !== val;
      case 'startsWith':  return lower.startsWith(val);
      case 'endsWith':    return lower.endsWith(val);
    }
  }
  // Composite filter — used by the File Name column so the user can combine
  // an Excel-style value checkbox filter with an advanced text-condition
  // filter in the SAME popup.  Either side may be null (no condition).
  // A row must satisfy both active sides (AND) to be included.  All string
  // comparisons remain case-insensitive via the existing 'set'/'text' paths.
  if (filter.type === 'composite') {
    if (filter.set) {
      if (!passesFilter(row, field, { type: 'set', values: filter.set })) return false;
    }
    if (filter.text && filter.text.value) {
      if (!passesFilter(row, field, { type: 'text', op: filter.text.op, value: filter.text.value })) return false;
    }
    return true;
  }
  if (filter.type === 'date') {
    const d = parseDate(str);
    if (!d) return false;
    const from = parseDate(filter.from);
    const to   = parseDate(filter.to);
    switch (filter.op) {
      case 'equals':  return from ? d.toDateString() === from.toDateString() : true;
      case 'before':  return from ? d < from : true;
      case 'after':   return from ? d > from : true;
      case 'between': return (!from || d >= from) && (!to || d <= to);
    }
  }
  return true;
}

/* ── Migration status ─────────────────────────────────────────────────────
 * Canonical values (must match services.data_service constants):
 *   'Pending'        eligible for selection + migration
 *   'In Processing'  user confirmed Migrate; Power Automate call in flight
 *   'Migrated'       Power Automate confirmed success + MigratedDate set
 *   'Failed'         Power Automate confirmed failure (Migrate stays No)
 *
 * The DB is the source of truth for `row.migrationStatus`.  The legacy
 * `row.migrate === 'Yes'` flag is derived from Migrated and is kept in
 * sync so any older reader keeps working.
 * ─────────────────────────────────────────────────────────────────────── */
const STATUS = Object.freeze({
  PENDING:       'Pending',
  IN_PROCESSING: 'In Processing',
  MIGRATED:      'Migrated',
  FAILED:        'Failed',
});

function rowStatus(row) {
  // Prefer the explicit status field; fall back to Migrate=Yes for
  // rows that arrived from a very old response shape.
  if (row && row.migrationStatus) return row.migrationStatus;
  if (row && row.migrate === 'Yes') return STATUS.MIGRATED;
  return STATUS.PENDING;
}

function isMigrated(row)     { return rowStatus(row) === STATUS.MIGRATED; }
function isInProcessing(row) { return rowStatus(row) === STATUS.IN_PROCESSING; }
function isFailed(row)       { return rowStatus(row) === STATUS.FAILED; }
// "Pending" here means "eligible to be selected/migrated" — i.e. NOT in
// any of the three terminal/in-flight states.
function isPending(row)      { const s = rowStatus(row); return s !== STATUS.MIGRATED && s !== STATUS.IN_PROCESSING; }
// Everything except Migrated + In Processing is selectable (Pending + Failed).
// Failed rows CAN be retried by the user — spec §10 leaves this open, and
// keeping them selectable matches the "return to pending or failed" wording.
function isSelectable(row)   { const s = rowStatus(row); return s !== STATUS.MIGRATED && s !== STATUS.IN_PROCESSING; }

/* ── Folder navigation ────────────────────────────────────────────────────
 * Derive a business-folder hierarchy from row.sharePointPath.  The virtual
 * root "All Contracts" is always the top; below it we surface the ACTUAL
 * folder segments from the data, dynamically.  No DB columns are created.
 *
 * Technical URL parts (scheme, host, /sites/<site-name>/, and any literal
 * "Shared Documents" segment) are stripped.  If a path already contains an
 * "All Contracts" segment, it's collapsed so we never nest the virtual root
 * inside itself.  The trailing filename is always removed.
 * ────────────────────────────────────────────────────────────────────────── */
const FOLDER_ROOT_LABEL = 'All Contracts';

// Segments that appear in SharePoint URLs but have no business meaning.
// Matched case-insensitively.  If the raw path contains them we skip them
// while walking segments so the browser starts at the real business root.
const _TECHNICAL_SEG_RE = /^(shared\s*documents|documents|forms|allitems\.aspx?)$/i;

function _folderSegmentsFor(row) {
  const raw = row && row.sharePointPath;
  if (!raw || typeof raw !== 'string') return [];
  // Strip query/fragment
  let s = raw.split('#')[0].split('?')[0];

  let segs;
  if (/^https?:\/\//i.test(s)) {
    // Full URL: parse and drop scheme+host+/sites/<site>/ prefix.
    let url;
    try { url = new URL(s); } catch { return []; }
    const parts = url.pathname.split('/').filter(Boolean);
    // Drop leading /sites/<site-name>/ pair when present.
    if (parts[0] && parts[0].toLowerCase() === 'sites' && parts.length >= 2) {
      parts.splice(0, 2);
    }
    segs = parts;
  } else {
    // Non-URL: treat as a POSIX/Windows-style path.
    segs = s.replace(/\\/g, '/').split('/').filter(Boolean);
  }

  // Decode + drop technical noise + drop the trailing filename.
  const decoded = segs.map(seg => {
    try { return decodeURIComponent(seg); } catch { return seg; }
  }).filter(seg => seg && !_TECHNICAL_SEG_RE.test(seg));

  if (decoded.length === 0) return [];
  // Drop trailing filename (anything with an extension after the last dot).
  // Files often have . in the name too — only drop if the LAST segment has
  // a short (1-6 char) alphanumeric extension.
  const last = decoded[decoded.length - 1];
  if (/\.[A-Za-z0-9]{1,6}$/.test(last)) decoded.pop();

  // Collapse a literal "All Contracts" segment anywhere in the chain —
  // we render the virtual root separately so nesting it would duplicate.
  return decoded.filter(seg => seg.toLowerCase() !== FOLDER_ROOT_LABEL.toLowerCase());
}

function _makeFolderNode(name) {
  return { name, children: new Map(), count: 0 };
}

/**
 * Rebuild the folder tree + per-row ancestor-key index from state.allData.
 * Called on initial load and after exclude/restore mutations so the browser
 * always reflects the current active dataset.  O(rows × max depth).
 */
function rebuildFolderTree() {
  const root  = _makeFolderNode(FOLDER_ROOT_LABEL);
  const index = new WeakMap();

  for (const row of state.allData) {
    const segs = _folderSegmentsFor(row);
    // Every row is under the virtual root (empty ancestor key set = "").
    const keys = new Set(['']);
    let node = root;
    root.count++;
    let acc = '';
    for (const seg of segs) {
      let child = node.children.get(seg);
      if (!child) {
        child = _makeFolderNode(seg);
        node.children.set(seg, child);
      }
      child.count++;
      acc = acc ? `${acc}/${seg}` : seg;
      keys.add(acc);
      node = child;
    }
    index.set(row, keys);
  }
  state.folderTree  = root;
  state.folderIndex = index;
}

/** Return the child map of the node at the given path (empty path = root). */
function _folderNodeAt(pathArr) {
  let node = state.folderTree;
  if (!node) return null;
  for (const seg of pathArr) {
    const nxt = node.children.get(seg);
    if (!nxt) return null;
    node = nxt;
  }
  return node;
}

/** True when the row lives under state.folderPath (recursive match). */
function _rowInSelectedFolder(row) {
  if (!state.folderPath.length) return true;                    // root scope
  const keys = state.folderIndex.get(row);
  if (!keys) return false;
  return keys.has(state.folderPath.join('/'));
}

/* ── Global folder filter helpers ─────────────────────────────────────────
 * The folder filter scans folder1..folder20 for a match under the chosen
 * mode.  When it fires for a row, we stash __folderMatch on that row so
 * the UI can (a) render a tooltip / info chip explaining the match and
 * (b) build a per-file matches map when the user bulk-excludes the
 * filtered set (audit persistence in dbo.exclusion_audit).
 * ──────────────────────────────────────────────────────────────────────── */
function _folderFilterMatcher(text, mode) {
  const needle = (text || '').trim().toLowerCase();
  if (!needle) return null;
  switch (mode) {
    case 'starts_with':
      return (v) => v.toLowerCase().startsWith(needle);
    case 'exact':
      return (v) => v.toLowerCase() === needle;
    case 'contains':
    default:
      return (v) => v.toLowerCase().includes(needle);
  }
}

/** Return {level, value} of the SHALLOWEST folder that matches, or null. */
function _matchFolderInRow(row, match) {
  for (let i = 0; i < FOLDER_FIELDS.length; i++) {
    const v = row[FOLDER_FIELDS[i]];
    if (v && match(String(v))) {
      return { level: `Folder ${i + 1}`, value: String(v) };
    }
  }
  return null;
}

function isFolderFilterActive() {
  return !!(state.folderFilter && state.folderFilter.text && state.folderFilter.text.trim());
}

function applyFiltersAndSort() {
  let d = state.allData;

  // Clear last pass's per-row folder-match annotation.  We re-annotate
  // below only for rows that the folder filter actually returns.
  for (const r of state.allData) { if (r.__folderMatch) delete r.__folderMatch; }

  // Folder scope: applied FIRST so column-filter dropdowns show values that
  // are consistent with the currently-visible subtree.  Empty path = no-op.
  if (state.folderPath.length) {
    d = d.filter(_rowInSelectedFolder);
  }

  // Global folder filter (Philippe): applied AFTER the navigator scope so
  // narrowing the tree can further constrain results, but the filter
  // itself still scans across ALL folder levels.  Rows that match get a
  // __folderMatch annotation for tooltip + audit.
  if (isFolderFilterActive()) {
    const match = _folderFilterMatcher(state.folderFilter.text, state.folderFilter.mode);
    if (match) {
      const survivors = [];
      for (const r of d) {
        const m = _matchFolderInRow(r, match);
        if (m) {
          r.__folderMatch = m;
          survivors.push(r);
        }
      }
      d = survivors;
    }
  }

  // Bucket filter (migration status): applied first so column filters still narrow further
  if (state.bucketFilter === 'migrated')            d = d.filter(isMigrated);
  else if (state.bucketFilter === 'in_processing')  d = d.filter(isInProcessing);
  // "Pending" bucket shows only rows eligible for migration selection —
  // Failed rows are kept out so the user isn't misled about retry status.
  else if (state.bucketFilter === 'pending')        d = d.filter(r => rowStatus(r) === STATUS.PENDING);
  else if (state.bucketFilter === 'selected')       d = d.filter(r => state.selectedIds.has(r.fileID));

  if (state.globalSearch.trim()) {
    const q = state.globalSearch.toLowerCase();
    d = d.filter(r => SEARCHABLE.some(f => cellStr(r, f).toLowerCase().includes(q)));
  }
  for (const [field, filter] of Object.entries(state.columnFilters)) {
    if (filter) d = d.filter(r => passesFilter(r, field, filter));
  }
  if (state.sortCol) {
    const col = COL_CFG.find(c => c.f === state.sortCol);
    const ft = col ? col.ft : 'text';
    d = [...d].sort((a, b) => {
      const va = cellStr(a, state.sortCol);
      const vb = cellStr(b, state.sortCol);
      let cmp = 0;
      if (ft === 'date') {
        const da = parseDate(va), db = parseDate(vb);
        cmp = (da || 0) > (db || 0) ? 1 : (da || 0) < (db || 0) ? -1 : 0;
      } else {
        cmp = va.localeCompare(vb, undefined, { sensitivity: 'base', numeric: true });
      }
      return state.sortDir === 'desc' ? -cmp : cmp;
    });
  }
  state.filteredData = d;
  state.page = 1;
}

/* ── DOM references ─────────────────────────────────────────────────────── */
const $ = id => document.getElementById(id);

/* ── Render helpers ─────────────────────────────────────────────────────── */
function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function renderStatusBadge(val, field, row) {
  if (field === 'migrate') {
    // Show the canonical MigrationStatus so the user can see 'In Processing'
    // and 'Failed' without needing a separate column.  Row's migrate flag
    // (Yes/No) is only set for the terminal Migrated state.
    const status = rowStatus(row);
    if (status === STATUS.MIGRATED)       return '<span class="status-badge status-migrate-yes">Yes</span>';
    if (status === STATUS.IN_PROCESSING)  return '<span class="status-badge status-in-processing">In Processing</span>';
    if (status === STATUS.FAILED)         return '<span class="status-badge status-failed">Failed</span>';
    return '<span class="status-no">No</span>';
  }

  if (val === null || val === undefined || val === '') return '<span class="cell-empty">—</span>';

  if (field === 'extractionStatus') {
    const cls = { Success: 'status-success', Failed: 'status-failed', Partial: 'status-partial' }[val] || '';
    return `<span class="status-badge ${cls}">${esc(val)}</span>`;
  }
  if (field === 'reviewRequired') {
    if (val === true || val === 'True') return '<span class="status-badge status-yes">Yes</span>';
    return '<span class="status-no">No</span>';
  }
  if (field === 'migrated') {
    if (val === true || val === 'True') return '<span class="status-badge status-migrated">Yes</span>';
    return '<span class="status-no">No</span>';
  }
  return esc(String(val));
}

function renderCell(row, col) {
  const val = row[col.f];
  const str = cellStr(row, col.f);

  if (col.f === 'fileName') {
    const path = row.sharePointPath;
    const display = esc(str) || '—';
    // Full name + matched-folder info kept in the tooltip so the reason
    // is discoverable even when the dedicated "Matched Folder" column is
    // scrolled off-screen.  Visible matched-folder chip is rendered in
    // its own column (see the virtual "__matchedFolder" col below) so the
    // File Name cell can stay a clean single-line ellipsis.
    const fm = row.__folderMatch;
    const tipBase = str || '';
    const tip = fm ? `${tipBase}\nMatched: ${fm.level} = ${fm.value}` : tipBase;
    if (path && path.startsWith('http')) {
      return `<a class="cell-link cell-clip" href="${esc(path)}" target="_blank" rel="noopener noreferrer" title="${esc(tip)}">${display}<span class="cell-link-icon">&#8599;</span></a>`;
    }
    return `<span class="cell-clip" title="${esc(tip)}">${display}</span>`;
  }

  // Virtual "Matched Folder" column — injected by getVisibleCols() only
  // while the global folder filter is active.  Not in COL_CFG, so it is
  // never exported, never sortable, never filterable, never picker-listed.
  if (col.f === '__matchedFolder') {
    const fm = row.__folderMatch;
    if (!fm) return '<span class="cell-empty">—</span>';
    const label = `${fm.level}: ${fm.value}`;
    return `<span class="cell-folder-match cell-clip" title="${esc(label)}">${esc(label)}</span>`;
  }

  // Folder columns — mild highlight when this specific level is the one
  // the folder filter matched, so the user can see immediately which
  // cell caused the row to be present.
  if (col.group === 'SharePoint Hierarchy') {
    if (!str) return '<span class="cell-empty">—</span>';
    const fm = row.__folderMatch;
    const isMatchCol = fm && fm.level.toLowerCase() === col.h.toLowerCase();
    if (isMatchCol) {
      return `<span class="cell-folder-hit cell-clip" title="Matched by folder filter: ${esc(str)}">${esc(str)}</span>`;
    }
    return `<span class="cell-clip" title="${esc(str)}">${esc(str)}</span>`;
  }

  // ── Migration Status cell (rich) ─────────────────────────────────────
  // Combines the UI bucket, the retry counter and the destination link
  // into a single glanceable cell.  Everything is data-driven — no
  // additional API calls from render time.
  if (col.f === 'migrationStatus') {
    const st        = rowStatus(row);        // Pending | In Processing | Migrated | Failed
    const retryN    = Number(row.migrationRetryCount || 0);
    const destUrl   = row.destinationUrl || '';
    const errMsg    = row.errorMessage || '';
    const migId     = row.migrationRequestId || '';
    const backend   = row.migrationBackendStatus || '';

    if (st === STATUS.MIGRATED) {
      const link = destUrl
        ? `<a class="cell-link migstatus-dest" href="${esc(destUrl)}" target="_blank" rel="noopener noreferrer" title="Open copied file: ${esc(destUrl)}">Open destination<span class="cell-link-icon">&#8599;</span></a>`
        : '<span class="migstatus-note" title="Destination URL not yet reported by the platform">destination pending</span>';
      return `<span class="migstatus-cell"><span class="status-badge status-migrate-yes">Migrated</span>${link}</span>`;
    }
    if (st === STATUS.IN_PROCESSING) {
      const isRetrying = backend === 'retrying' || retryN > 0;
      const badge = isRetrying
        ? `<span class="status-badge status-in-processing" title="Backend is retrying — attempt ${retryN + 1}">Retrying${retryN ? ` (${retryN})` : ''}</span>`
        : `<span class="status-badge status-in-processing">In Processing</span>`;
      const tip = migId ? `Migration ID: ${migId}` : 'Awaiting platform';
      return `<span class="migstatus-cell" title="${esc(tip)}">${badge}</span>`;
    }
    if (st === STATUS.FAILED) {
      const badge = `<span class="status-badge status-failed">Failed</span>`;
      const note = errMsg
        ? `<span class="migstatus-note" title="${esc(errMsg)}">${esc(errMsg.length > 60 ? errMsg.slice(0, 60) + '…' : errMsg)}</span>`
        : '';
      return `<span class="migstatus-cell">${badge}${note}</span>`;
    }
    // Pending (default) — quiet cell to keep the column visually calm.
    return '<span class="cell-empty">—</span>';
  }

  if (col.f === 'missingFields' && str) {
    const parts = str.split(',').map(s => s.trim()).filter(Boolean);
    if (!parts.length) return '<span class="cell-empty">—</span>';
    const chips = parts.slice(0, 3).map(p => `<span class="mf-chip">${esc(p)}</span>`).join('');
    const extra = parts.length > 3 ? `<span class="mf-chip" title="${esc(str)}">+${parts.length - 3}</span>` : '';
    return `<span title="${esc(str)}">${chips}${extra}</span>`;
  }

  if (col.ft === 'bool' || col.f === 'reviewRequired' || col.f === 'migrated' || col.f === 'migrate') {
    return renderStatusBadge(val, col.f, row);
  }

  if (col.f === 'extractionStatus') {
    return renderStatusBadge(val, col.f, row);
  }

  if (!str) return '<span class="cell-empty">—</span>';

  // SharePoint path — render as a real link that opens the full URL in a new tab.
  // Truncation/ellipsis stays the same; the underlying href is the untruncated URL.
  if (col.f === 'sharePointPath') {
    const isHttpUrl = /^https?:\/\//i.test(str);
    if (isHttpUrl) {
      // SharePoint URLs in the source are already percent-encoded (spaces as %20).
      // Only encode raw spaces if any slipped through; DO NOT re-encode existing % sequences.
      const href = str.replace(/ /g, '%20');
      return `<a class="cell-link cell-clip" href="${esc(href)}" target="_blank" rel="noopener noreferrer" title="${esc(str)}">${esc(str)}</a>`;
    }
    // Fall through for non-URL values so they render as plain truncated text.
  }

  const longFields = ['sharePointPath','errorMessage','agreementName','associatedMSAFileName','associatedNDAFileName','runId'];
  if (longFields.includes(col.f)) {
    return `<span class="cell-clip" title="${esc(str)}">${esc(str)}</span>`;
  }

  return `<span class="cell-clip" title="${esc(str)}">${esc(str)}</span>`;
}

/* ── Table render ───────────────────────────────────────────────────────── */
// Virtual "Matched Folder" pseudo-column definition.  Injected into the
// visible-columns list ONLY while the global folder filter is active so the
// per-row matched level+value is visible without crowding the File Name
// cell.  Not in COL_CFG — that means it is automatically absent from the
// Columns picker, sort/filter menus, Export CSV, resize memory, etc.
const MATCHED_FOLDER_COL = {
  f:        '__matchedFolder',
  h:        'Matched Folder',
  ft:       'text',
  w:        170,
  virtual:  true,   // never persisted, never exported, never filterable
};

function getVisibleCols() {
  const base = COL_CFG.filter(c => !state.hiddenCols.has(c.f));
  // Inject the virtual "Matched Folder" column immediately after File Name
  // when the global folder filter is active.  If File Name is hidden for
  // some reason, prepend it so the info is still visible.
  if (isFolderFilterActive()) {
    const fnIdx = base.findIndex(c => c.f === 'fileName');
    if (fnIdx >= 0) base.splice(fnIdx + 1, 0, MATCHED_FOLDER_COL);
    else            base.unshift(MATCHED_FOLDER_COL);
  }
  return base;
}

function renderTable() {
  const visibleCols = getVisibleCols();
  const start = (state.page - 1) * state.pageSize;
  const pageData = state.filteredData.slice(start, start + state.pageSize);

  let thead = '<thead><tr>';
  thead += `<th class="col-rn col-frozen col-frozen-0 cell-rn" style="width:40px;min-width:40px;max-width:40px">#</th>`;
  thead += `<th class="col-cb col-frozen col-frozen-1 th-cb-hdr" style="width:44px;min-width:44px;max-width:44px;" title="Select all visible rows"><input type="checkbox" id="hdr-check"></th>`;

  visibleCols.forEach((col, idx) => {
    const w = state.colWidths[col.f] || col.w;
    const isFirst = idx === 0;
    const frozen = isFirst ? 'col-frozen col-frozen-2' : '';
    const frozenStyle = isFirst ? 'position:sticky;left:84px;z-index:15;background:#eaeff5;' : '';
    // Virtual columns (e.g. "Matched Folder") get a stripped-down header:
    // no filter button, no resize handle, no sort click — they are not
    // backed by a real data field.
    if (col.virtual) {
      thead += `<th class="th-virtual" style="${frozenStyle}width:${w}px;min-width:${w}px;max-width:${w}px" data-field="${col.f}" title="Automatically shown while the Folder Filter is active">
        <div class="th-inner">
          <span class="th-label">${esc(col.h)}</span>
        </div>
      </th>`;
      return;
    }
    const hasFilter = !!state.columnFilters[col.f];
    const filterActive = hasFilter ? 'active' : '';
    const sortIcon = state.sortCol === col.f
      ? (state.sortDir === 'asc' ? ' <span class="th-sort-icon">&#9650;</span>' : ' <span class="th-sort-icon">&#9660;</span>')
      : '';
    const filterTitle = hasFilter ? `Filter active — ${esc(col.h)}` : `Filter ${esc(col.h)}`;
    // Funnel SVG icon for stronger visual weight
    const filterIcon = `<svg class="th-filter-svg" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="M2 3h12a.5.5 0 0 1 .4.8L10 9.6V13a.5.5 0 0 1-.28.45l-2 1A.5.5 0 0 1 7 14V9.6L1.6 3.8A.5.5 0 0 1 2 3z" fill="currentColor"/></svg>`;
    thead += `<th class="${frozen}" style="${frozenStyle}width:${w}px;min-width:${w}px;max-width:${w}px" data-field="${col.f}">
      <div class="th-inner">
        <span class="th-label">${esc(col.h)}${sortIcon}</span>
        <button class="th-filter-btn ${filterActive}" data-field="${col.f}" title="${filterTitle}" aria-label="${filterTitle}">${filterIcon}</button>
      </div>
      <div class="th-resize" data-field="${col.f}"></div>
    </th>`;
  });
  thead += '</tr></thead>';

  let tbody = '<tbody>';
  if (pageData.length === 0) {
    const totalCols = visibleCols.length + 2;
    tbody += `<tr><td colspan="${totalCols}" style="text-align:center;padding:32px;color:#9ca3af;">No records match the current filters.</td></tr>`;
  } else {
    pageData.forEach((row, i) => {
      const globalIdx = start + i;
      const isSelected  = state.selectedIds.has(row.fileID);
      const status      = rowStatus(row);
      const isMigratedR = status === STATUS.MIGRATED;
      const isInProc    = status === STATUS.IN_PROCESSING;
      // Selection is blocked for BOTH terminal Migrated rows and in-flight
      // In-Processing rows (business rule §15).
      const lockedForSelection = isMigratedR || isInProc;

      const rowClasses = [
        isSelected      ? 'row-selected'       : '',
        isMigratedR     ? 'row-migrated'       : '',
        isInProc        ? 'row-in-processing'  : '',
        // Faint tint when the folder filter surfaced this row.
        row.__folderMatch ? 'row-folder-match' : '',
      ].filter(Boolean).join(' ');

      const cbTitle = isMigratedR ? 'Already migrated'
                    : isInProc    ? 'Currently being migrated'
                                  : '';
      const cbDisabled = lockedForSelection ? `disabled title="${cbTitle}"` : '';
      // data-migrated stays set only for terminal Migrated rows so the
      // existing row-click guard keeps its exact semantics; a new
      // data-locked flag covers the In-Processing case.
      const dataFlags = (isMigratedR ? ' data-migrated="1"' : '')
                      + (lockedForSelection ? ' data-locked="1"' : '');

      tbody += `<tr class="${rowClasses}" data-id="${esc(row.fileID)}"${dataFlags}>`;
      tbody += `<td class="col-rn col-frozen col-frozen-0 cell-rn" style="width:40px;min-width:40px;max-width:40px">${globalIdx + 1}</td>`;
      tbody += `<td class="col-cb col-frozen col-frozen-1 cell-cb" style="width:44px;min-width:44px;max-width:44px"${cbTitle ? ` title="${cbTitle}"` : ''}><input type="checkbox" class="row-check" data-id="${esc(row.fileID)}" ${isSelected ? 'checked' : ''} ${cbDisabled}></td>`;

      visibleCols.forEach((col, idx) => {
        const w = state.colWidths[col.f] || col.w;
        const isFirst = idx === 0;
        // NOTE: no inline background here — the .col-frozen CSS class provides
        // an opaque background-color per row-state variant so sticky cells
        // properly occlude horizontally-scrolled cells behind them. Setting
        // `background: inherit` inline used to resolve to transparent and
        // caused the visible "row overlap" when the table was scrolled right.
        const frozenStyle = isFirst ? 'position:sticky;left:84px;z-index:10;' : '';
        const frozen = isFirst ? 'col-frozen col-frozen-2' : '';
        tbody += `<td class="${frozen}" style="${frozenStyle}width:${w}px;min-width:${w}px;max-width:${w}px;overflow:hidden;">${renderCell(row, col)}</td>`;
      });
      tbody += '</tr>';
    });
  }
  tbody += '</tbody>';

  $('table-root').innerHTML = thead + tbody;
  attachTableEvents();
  updateHdrCheckbox();
}

function updateHdrCheckbox() {
  const hdr = document.getElementById('hdr-check');
  if (!hdr) return;
  const eligibleIds = getEligiblePageIds();
  const allSelected = eligibleIds.length > 0 && eligibleIds.every(id => state.selectedIds.has(id));
  const someSelected = eligibleIds.some(id => state.selectedIds.has(id));
  hdr.checked = allSelected;
  hdr.indeterminate = !allSelected && someSelected;
  hdr.disabled = eligibleIds.length === 0;

  const th = hdr.closest('th');
  if (th) {
    if (eligibleIds.length === 0) {
      th.title = 'No eligible (pending) rows to select on this page';
    } else {
      th.title = allSelected
        ? 'Deselect all pending rows on this page'
        : 'Select all pending rows on this page';
    }
  }
}

function getPageIds() {
  const start = (state.page - 1) * state.pageSize;
  return state.filteredData.slice(start, start + state.pageSize).map(r => r.fileID);
}

// Only selectable rows (Pending or Failed — NOT Migrated or In Processing)
// on the current page are eligible for the master checkbox / row-click.
function getEligiblePageIds() {
  const start = (state.page - 1) * state.pageSize;
  return state.filteredData
    .slice(start, start + state.pageSize)
    .filter(isSelectable)
    .map(r => r.fileID);
}

function attachTableEvents() {
  const hdrCheck = document.getElementById('hdr-check');
  if (hdrCheck) {
    hdrCheck.addEventListener('change', () => {
      // Master checkbox only toggles ELIGIBLE (pending) visible rows.
      // Migrated rows are never added/removed here.
      const ids = getEligiblePageIds();
      if (hdrCheck.checked) ids.forEach(id => state.selectedIds.add(id));
      else ids.forEach(id => state.selectedIds.delete(id));
      renderAll();
    });
  }

  document.querySelectorAll('.row-check').forEach(cb => {
    cb.addEventListener('change', () => {
      const id = cb.dataset.id;
      if (cb.checked) state.selectedIds.add(id);
      else state.selectedIds.delete(id);
      updateHdrCheckbox();
      updateBuckets();
      updateToolbar();
      // Re-render only if we're on the 'selected' bucket so filtered set updates
      if (state.bucketFilter === 'selected') {
        applyFiltersAndSort();
        renderTable();
        updateFooter();
      }
    });
  });

  document.querySelectorAll('#table-root tbody tr[data-id]').forEach(tr => {
    tr.addEventListener('click', e => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'A') return;
      // Don't allow row-click to toggle selection on rows locked for
      // selection (Migrated OR In Processing).  Legacy data-migrated
      // guard is kept below for maximum backwards compatibility.
      if (tr.dataset.locked === '1' || tr.dataset.migrated === '1') return;
      const id = tr.dataset.id;
      if (state.selectedIds.has(id)) state.selectedIds.delete(id);
      else state.selectedIds.add(id);
      renderAll();
    });
  });

  document.querySelectorAll('.th-filter-btn').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const field = btn.dataset.field;
      const rect = btn.getBoundingClientRect();
      openFilterPanel(field, rect);
    });
  });

  document.querySelectorAll('.th-resize').forEach(handle => {
    handle.addEventListener('mousedown', startResize);
  });
}

/* ── Toolbar & footer update ────────────────────────────────────────────── */
function updateToolbar() {
  // Count only selectable (Pending / Failed) documents among the currently
  // selected set.  Anything already-migrated or in-flight is defensively
  // ignored — those states cannot be re-migrated (§15).
  let sel = 0;
  state.selectedIds.forEach(id => {
    const row = state.allData.find(r => r.fileID === id);
    if (row && isSelectable(row)) sel++;
  });

  // Per-user workload lock: server-authoritative flag from /api/contracts
  // and /api/migrations/sync.  When false, this user already has one or
  // more rows in an active migration status they submitted, so we MUST
  // disable Migrate regardless of selection.  Server enforces the same
  // rule (returns 409 on /api/migrate) — this is a UX + latency
  // optimisation, not the enforcement point.
  const pc         = state.populationCounts || {};
  const canMigrate = (pc.can_migrate !== false);   // default true if unknown
  const myActive   = Number(pc.my_in_processing) || 0;

  const btn = $('migrate-btn');
  if (!canMigrate) {
    btn.textContent = myActive > 0
      ? `Migrate — ${myActive} of yours in processing`
      : 'Migrate — batch in progress';
    btn.disabled = true;
    btn.className = 'action-migrate-btn locked';
    btn.title = (
      `You currently have ${myActive} file${myActive !== 1 ? 's' : ''} `
      + `in processing.  New migrations can be submitted after your `
      + `current batch completes.`
    );
  } else {
    btn.textContent = sel > 0 ? `Migrate (${sel})` : 'Migrate';
    btn.disabled = sel === 0;
    btn.className = `action-migrate-btn${sel > 0 ? ' active' : ''}`;
    btn.title = '';
  }

  // "My Active" indicator next to the selection counter.  Always
  // reflects the same server-authoritative number the lock uses so
  // users can see why their button is disabled at a glance.
  const myInd = $('my-active-indicator');
  if (myInd) {
    if (myActive > 0) {
      myInd.textContent = `My Active: ${myActive}`;
      myInd.style.display = '';
      myInd.classList.toggle('locked', !canMigrate);
    } else {
      myInd.textContent = '';
      myInd.style.display = 'none';
      myInd.classList.remove('locked');
    }
  }

  // Exclude button — enabled only when the selection includes at least one
  // Pending row.  Business rule §19: already-migrated documents AND
  // in-flight (In Processing) documents cannot be excluded.  Failed rows
  // ARE excludable — they didn't move successfully and re-classifying them
  // as excluded is a valid recovery workflow.
  const excludeBtn = $('exclude-btn');
  if (excludeBtn) {
    let excludable = 0;
    state.selectedIds.forEach(id => {
      const row = state.allData.find(r => r.fileID === id);
      // Exclude excludes Pending + Failed but not Migrated/In-Processing.
      if (row && rowStatus(row) !== STATUS.MIGRATED && rowStatus(row) !== STATUS.IN_PROCESSING) excludable++;
    });
    excludeBtn.textContent = excludable > 0 ? `Exclude (${excludable})` : 'Exclude';
    excludeBtn.disabled = excludable === 0;
  }

  const sc = $('sel-count');
  if (sel === 0) {
    sc.textContent = 'No files selected';
    sc.classList.remove('has-selection');
  } else {
    sc.textContent = `${sel} file${sel !== 1 ? 's' : ''} selected`;
    sc.classList.add('has-selection');
  }

  const hasFilters = Object.values(state.columnFilters).some(Boolean)
                     || state.globalSearch
                     || state.bucketFilter !== 'all'
                     || state.folderPath.length > 0
                     || isFolderFilterActive();
  $('clear-filters-btn').disabled = !hasFilters;
}

function updateFooter() {
  const total = state.filteredData.length;
  const totalPages = Math.max(1, Math.ceil(total / state.pageSize));
  renderPager(totalPages);
}

function renderPager(totalPages) {
  const container = $('pager');
  const p = state.page;
  let html = `<button class="pg-btn" id="pg-prev" ${p <= 1 ? 'disabled' : ''}>&#8249;</button>`;

  const pages = pagesToShow(p, totalPages);
  let prev = null;
  pages.forEach(n => {
    if (prev !== null && n - prev > 1) html += `<span style="padding:0 4px;color:#9ca3af;">&#8230;</span>`;
    html += `<button class="pg-btn${n === p ? ' active' : ''}" data-page="${n}">${n}</button>`;
    prev = n;
  });

  html += `<button class="pg-btn" id="pg-next" ${p >= totalPages ? 'disabled' : ''}>&#8250;</button>`;
  container.innerHTML = html;

  container.querySelectorAll('[data-page]').forEach(btn => {
    btn.addEventListener('click', () => { state.page = parseInt(btn.dataset.page); renderAll(); });
  });
  const prev_btn = $('pg-prev');
  const next_btn = $('pg-next');
  if (prev_btn) prev_btn.addEventListener('click', () => { if (state.page > 1) { state.page--; renderAll(); } });
  if (next_btn) next_btn.addEventListener('click', () => { if (state.page < totalPages) { state.page++; renderAll(); } });
}

function pagesToShow(current, total) {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const set = new Set([1, total, current]);
  if (current > 1) set.add(current - 1);
  if (current < total) set.add(current + 1);
  if (current > 2) set.add(current - 2);
  if (current < total - 1) set.add(current + 2);
  return [...set].sort((a, b) => a - b);
}

function renderActiveFilters() {
  const bar = $('af-bar');
  const chips = Object.entries(state.columnFilters)
    .filter(([, f]) => f)
    .map(([field, f]) => {
      const col = COL_CFG.find(c => c.f === field);
      const label = col ? col.h : field;
      let val = '';
      if (f.type === 'set') val = f.values.length === 1 ? f.values[0] : `${f.values.length} values`;
      if (f.type === 'text') val = `${f.op}: "${f.value}"`;
      if (f.type === 'date') val = f.op === 'between' ? `${f.from} – ${f.to}` : `${f.op} ${f.from}`;
      if (f.type === 'composite') {
        const parts = [];
        if (f.set)  parts.push(f.set.length === 1 ? f.set[0] : `${f.set.length} values`);
        if (f.text && f.text.value) parts.push(`${f.text.op}: "${f.text.value}"`);
        val = parts.join(' + ');
      }
      return `<span class="af-chip"><span class="af-col">${esc(label)}:</span> ${esc(val)} <button class="af-x" data-field="${field}">✕</button></span>`;
    });

  // Prepend a chip for the global folder filter when active — same visual
  // language as the column-filter chips, but with a distinct class so the
  // user recognises it as a cross-cutting filter rather than per-column.
  if (isFolderFilterActive()) {
    const modeLabel = { contains: 'contains', starts_with: 'starts with', exact: 'exact' }[state.folderFilter.mode] || 'contains';
    chips.unshift(
      `<span class="af-chip af-chip-folder">`
      + `<span class="af-col">Folder ${esc(modeLabel)}:</span> `
      + `${esc(state.folderFilter.text)} `
      + `<button class="af-x" data-af="folder-filter">✕</button></span>`
    );
  }

  if (!chips.length) {
    bar.innerHTML = '';
    bar.style.display = 'none';
    return;
  }
  bar.style.display = 'flex';
  bar.innerHTML = `<span class="af-label">Filters:</span>${chips.join('')}<button class="af-clear-all" id="af-clear-all">Clear All</button>`;

  bar.querySelectorAll('.af-x').forEach(btn => {
    btn.addEventListener('click', () => {
      // Folder-filter chip clears the global folder filter; per-column
      // chips clear their specific state.columnFilters entry.
      if (btn.dataset.af === 'folder-filter') {
        state.folderFilter = { text: '', mode: 'contains' };
        _syncFolderFilterButton();
      } else if (btn.dataset.field) {
        delete state.columnFilters[btn.dataset.field];
      }
      applyFiltersAndSort();
      renderAll();
    });
  });
  $('af-clear-all').addEventListener('click', () => {
    state.columnFilters = {};
    state.globalSearch = '';
    $('search-input').value = '';
    state.bucketFilter = 'all';
    // Also drop the global folder filter so "Clear All" behaves as advertised.
    state.folderFilter = { text: '', mode: 'contains' };
    _syncFolderFilterButton();
    applyFiltersAndSort();
    renderAll();
  });
}

/** Toggle .has-selection on the Folder Filter toolbar button and update
 *  its label to include the filter value (e.g. "Folder Filter: Services"). */
function _syncFolderFilterButton() {
  const btn = document.getElementById('folder-filter-btn');
  const lbl = document.getElementById('folder-filter-btn-label');
  if (!btn || !lbl) return;
  if (isFolderFilterActive()) {
    btn.classList.add('has-selection');
    const modeLabel = { contains: 'contains', starts_with: 'starts', exact: 'exact' }[state.folderFilter.mode] || '';
    lbl.textContent = `Folder ${modeLabel}: ${state.folderFilter.text}`;
  } else {
    btn.classList.remove('has-selection');
    lbl.textContent = 'Folder Filter';
  }
}

/* ── Summary buckets ────────────────────────────────────────────────────── */
function updateBuckets() {
  // KPI identity per manager spec:
  //   A (Total) = B (Yet to be Migrated) + C (In Processing) + D (Migrated) + E (Excluded)
  //
  // Failed rows are still eligible for retry, so they roll up into
  // "Yet to be Migrated" (B) — this preserves the A = B + C + D + E
  // identity and keeps every document in exactly one KPI bucket.
  //
  // Source-of-truth rule (live-refresh, added for task §17 follow-up):
  //   When the sync poller has just written fresh counts from Azure SQL
  //   into state.populationCounts, USE THOSE — they reflect the platform's
  //   authoritative per-file result within the last 8 s.  Fall back to
  //   the in-memory row scan only when server counts aren't available
  //   yet (first paint before /api/contracts returns, or a legacy code
  //   path that doesn't set populationCounts).  This makes the top-row
  //   tiles decrement in lockstep with the row-status updates instead
  //   of lagging behind until the next full table refresh.
  const pc = state.populationCounts || {};
  const hasServerCounts =
        typeof pc.in_processing === 'number' &&
        typeof pc.migrated      === 'number' &&
        typeof pc.pending       === 'number';

  let migrated, inProcessing, yetToBeMigrated;
  if (hasServerCounts) {
    migrated        = pc.migrated;
    inProcessing    = pc.in_processing;
    // "Yet to be Migrated" bundles Pending + Failed (both retry-eligible).
    yetToBeMigrated = (pc.pending || 0) + (pc.failed || 0);
  } else {
    migrated = 0; inProcessing = 0; yetToBeMigrated = 0;
    state.allData.forEach(r => {
      switch (rowStatus(r)) {
        case STATUS.MIGRATED:       migrated++;         break;
        case STATUS.IN_PROCESSING:  inProcessing++;     break;
        // Pending + Failed → eligible for migration → "Yet to be Migrated".
        default:                    yetToBeMigrated++;  break;
      }
    });
  }
  const excluded  = (typeof pc.excluded === 'number')
                      ? pc.excluded
                      : (state.excludedData.length || 0);
  // A = B + C + D + E  (Total Documents formula).
  const total = yetToBeMigrated + inProcessing + migrated + excluded;

  const setVal = (id, v) => { const el = $(id); if (el) el.textContent = v.toLocaleString(); };
  setVal('bucket-total',           total);
  setVal('bucket-migrated',        migrated);
  setVal('bucket-pending',         yetToBeMigrated);
  setVal('bucket-in-processing',   inProcessing);
  setVal('bucket-excluded',        excluded);

  // Live formula tooltip on the Total bucket:
  //   "B + C + D + E   <hover>  500 + 20 + 110 + 10 = 640"
  // Values are dynamic — nothing is hardcoded.
  const formulaEl = $('bucket-total-formula');
  if (formulaEl) {
    const parts = `${yetToBeMigrated.toLocaleString()} + ${inProcessing.toLocaleString()} + `
                + `${migrated.toLocaleString()} + ${excluded.toLocaleString()}`;
    formulaEl.setAttribute(
      'title',
      `B + C + D + E\n${parts} = ${total.toLocaleString()}`
    );
    formulaEl.setAttribute(
      'aria-label',
      `Total Documents formula: B (${yetToBeMigrated}) + C (${inProcessing}) + `
      + `D (${migrated}) + E (${excluded}) = ${total}`
    );
  }

  // Active state — the Excluded bucket is "active" when the excluded view
  // is showing.  Everything else is driven by state.bucketFilter.
  document.querySelectorAll('.bucket').forEach(b => {
    let isActive;
    if (b.dataset.bucket === 'excluded') {
      isActive = state.currentView === 'excluded';
    } else {
      isActive = state.currentView === 'review' && b.dataset.bucket === state.bucketFilter;
    }
    b.classList.toggle('active', isActive);
    b.setAttribute('aria-selected', isActive ? 'true' : 'false');
  });

  // NOTE: A previous version had a 'Selected' bucket in this KPI row.
  // Per manager spec the KPI row is now exactly A/B/C/D/E; the selected
  // count lives only in the bottom action bar next to the Migrate button.
}

/* ── Main render ────────────────────────────────────────────────────────── */
function renderAll() {
  renderTable();
  updateToolbar();
  updateFooter();
  renderActiveFilters();
  updateBuckets();
  // renderFolderControl() removed with the "All Contracts" navigator.
  // The remaining Folder Filter control renders itself via
  // _syncFolderFilterButton() from its own handlers.
}

/* ── Folder control render (button label + breadcrumb bar) ────────────── */
function renderFolderControl() {
  const label = $('folder-btn-label');
  const crumb = $('folder-crumb');
  const btn   = $('folder-btn');
  if (!label || !crumb || !btn) return;

  if (state.folderPath.length === 0) {
    label.textContent = FOLDER_ROOT_LABEL;
    btn.classList.remove('has-selection');
    crumb.style.display = 'none';
    crumb.innerHTML = '';
    return;
  }
  // Button label = current (deepest) folder name for compactness.
  label.textContent = state.folderPath[state.folderPath.length - 1];
  btn.classList.add('has-selection');

  // Breadcrumb: virtual root + every segment; last one is non-clickable.
  const parts = [FOLDER_ROOT_LABEL, ...state.folderPath];
  const segsHtml = parts.map((name, i) => {
    const isLast = i === parts.length - 1;
    const cls = isLast ? 'folder-crumb-segment current' : 'folder-crumb-segment';
    const html = `<button type="button" class="${cls}" data-depth="${i}" title="${esc(name)}">${esc(name)}</button>`;
    return i === 0 ? html : `<span class="folder-crumb-sep">›</span>${html}`;
  }).join('');
  crumb.innerHTML = `<span class="folder-crumb-label">Folder:</span>${segsHtml}` +
                    `<button type="button" class="folder-crumb-clear" id="folder-crumb-clear" title="Reset to All Contracts">Show All</button>`;
  crumb.style.display = 'flex';

  crumb.querySelectorAll('.folder-crumb-segment').forEach(b => {
    b.addEventListener('click', () => {
      const depth = parseInt(b.dataset.depth, 10);
      // depth 0 = virtual root → empty path; depth N = keep first N segments.
      state.folderPath = state.folderPath.slice(0, depth);
      state.page = 1;
      applyFiltersAndSort();
      renderAll();
    });
  });
  const clearBtn = $('folder-crumb-clear');
  if (clearBtn) clearBtn.addEventListener('click', () => {
    state.folderPath = [];
    state.page = 1;
    applyFiltersAndSort();
    renderAll();
  });
}

/* ── Folder dropdown (compact browser) ────────────────────────────────── */
function openFolderPanel() {
  closeFolderPanel();
  const wrap = $('folder-btn-wrap');
  if (!wrap || !state.folderTree) return;

  const node = _folderNodeAt(state.folderPath);
  const children = node ? [...node.children.values()].sort((a, b) =>
    a.name.localeCompare(b.name, undefined, { sensitivity: 'base' })
  ) : [];

  const crumbTxt = [FOLDER_ROOT_LABEL, ...state.folderPath].join(' › ');
  const backHtml = state.folderPath.length > 0
    ? `<button type="button" class="folder-panel-back" id="folder-back">↑ Up to ${esc(state.folderPath.length === 1 ? FOLDER_ROOT_LABEL : state.folderPath[state.folderPath.length - 2])}</button>`
    : '';

  const listHtml = children.length === 0
    ? '<div class="folder-panel-empty">No subfolders</div>'
    : children.map(c => `
        <div class="folder-item" data-name="${esc(c.name)}" title="${esc(c.name)}">
          <span class="folder-item-icon">📁</span>
          <span class="folder-item-name">${esc(c.name)}</span>
          <span class="folder-item-count">${c.count.toLocaleString()}</span>
          <span class="folder-item-arrow">›</span>
        </div>`).join('');

  const html = `
    <div class="folder-panel" id="folder-panel" role="menu">
      <div class="folder-panel-header">
        <span class="folder-btn-icon">📁</span>
        <span>Folders</span>
        <button type="button" class="fp-close" id="folder-panel-close" aria-label="Close">✕</button>
      </div>
      <div class="folder-panel-current">
        Current: <strong>${esc(crumbTxt)}</strong>
      </div>
      ${backHtml}
      <div class="folder-panel-list" id="folder-panel-list">${listHtml}</div>
      <div class="folder-panel-footer">
        <button type="button" class="folder-showall" id="folder-panel-showall">Show All</button>
        <span style="color:#9ca3af;font-size:11px">${node ? node.count.toLocaleString() : 0} in scope</span>
      </div>
    </div>`;
  wrap.insertAdjacentHTML('beforeend', html);

  const panel = $('folder-panel');
  panel.querySelectorAll('.folder-item').forEach(el => {
    el.addEventListener('click', () => {
      state.folderPath = [...state.folderPath, el.dataset.name];
      state.page = 1;
      applyFiltersAndSort();
      renderAll();
      // Reopen so user can drill deeper without closing.
      openFolderPanel();
    });
  });
  const back = $('folder-back');
  if (back) back.addEventListener('click', () => {
    state.folderPath = state.folderPath.slice(0, -1);
    state.page = 1;
    applyFiltersAndSort();
    renderAll();
    openFolderPanel();
  });
  $('folder-panel-close').addEventListener('click', closeFolderPanel);
  $('folder-panel-showall').addEventListener('click', () => {
    state.folderPath = [];
    state.page = 1;
    applyFiltersAndSort();
    renderAll();
    closeFolderPanel();
  });

  // Click-outside to close.  Registered async so the opening click doesn't
  // immediately close the just-opened panel.
  setTimeout(() => {
    document.addEventListener('mousedown', _folderPanelOutside);
    document.addEventListener('keydown', _folderPanelEsc);
  }, 0);
}

function _folderPanelOutside(e) {
  const panel = document.getElementById('folder-panel');
  const btn = document.getElementById('folder-btn');
  if (!panel) return;
  if (panel.contains(e.target) || (btn && btn.contains(e.target))) return;
  closeFolderPanel();
}
function _folderPanelEsc(e) { if (e.key === 'Escape') closeFolderPanel(); }

function closeFolderPanel() {
  const p = document.getElementById('folder-panel');
  if (p) p.remove();
  document.removeEventListener('mousedown', _folderPanelOutside);
  document.removeEventListener('keydown', _folderPanelEsc);
}

/* ── Column resize ──────────────────────────────────────────────────────── */
let resizing = null;

function startResize(e) {
  e.preventDefault();
  const field = e.target.dataset.field;
  const th = e.target.closest('th');
  const startX = e.clientX;
  const startW = th.offsetWidth;

  resizing = { field, startX, startW };
  e.target.classList.add('dragging');
  document.body.style.cursor = 'col-resize';
  document.body.style.userSelect = 'none';

  function onMove(ev) {
    const delta = ev.clientX - resizing.startX;
    const newW = Math.max(50, resizing.startW + delta);
    state.colWidths[resizing.field] = newW;
    const idx = getVisibleCols().findIndex(c => c.f === resizing.field);
    if (idx >= 0) {
      const colIdx = idx + 2;
      document.querySelectorAll(`#table-root tr`).forEach(tr => {
        const cell = tr.children[colIdx + 1];
        if (cell) {
          cell.style.width = newW + 'px';
          cell.style.minWidth = newW + 'px';
          cell.style.maxWidth = newW + 'px';
        }
      });
    }
  }

  function onUp() {
    if (resizing) {
      e.target.classList.remove('dragging');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      resizing = null;
    }
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
  }

  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

/* ── Filter panel ───────────────────────────────────────────────────────── */
function openFilterPanel(field, rect) {
  closeFilterPanel();
  state.activeFilter = field;

  // File Name uses an enhanced composite panel that shows the value-list
  // AND advanced text-condition filter together in one popup.
  if (field === 'fileName') {
    return openFileNameFilterPanel(field, rect);
  }

  const col = COL_CFG.find(c => c.f === field);
  const filter = state.columnFilters[field] || null;

  const dataForValues = state.allData.filter(row => {
    for (const [f, flt] of Object.entries(state.columnFilters)) {
      if (f === field || !flt) continue;
      if (!passesFilter(row, f, flt)) return false;
    }
    if (state.globalSearch.trim()) {
      const q = state.globalSearch.toLowerCase();
      if (!SEARCHABLE.some(sf => cellStr(row, sf).toLowerCase().includes(q))) return false;
    }
    return true;
  });

  const uniqueVals = [...new Set(dataForValues.map(r => cellStr(r, field)))]
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));

  const currentSet = filter?.type === 'set' ? new Set(filter.values) : new Set();
  const currentTab = filter?.type === 'text' ? 'text' : filter?.type === 'date' ? 'date' : 'values';

  let left = Math.max(0, Math.min(rect.left, window.innerWidth - 330));
  let top = rect.bottom + 4;
  if (top + 480 > window.innerHeight) top = rect.top - 480;
  if (top < 0) top = 4;

  const sortLabel = col?.ft === 'date' ? ['Oldest → Newest', 'Newest → Oldest'] : ['A → Z', 'Z → A'];

  const tabsHtml = col?.ft === 'text' ? `
    <div class="fp-subtabs">
      <button class="fp-subtab${currentTab==='values'?' active':''}" data-tab="values">Values</button>
      <button class="fp-subtab${currentTab==='text'?' active':''}" data-tab="text">Text Filter</button>
    </div>` : col?.ft === 'date' ? `
    <div class="fp-subtabs">
      <button class="fp-subtab${currentTab==='values'?' active':''}" data-tab="values">Values</button>
      <button class="fp-subtab${currentTab==='date'?' active':''}" data-tab="date">Date Filter</button>
    </div>` : '';

  const valListHtml = `
    <div class="fp-search"><input type="text" id="fp-search" placeholder="Search…" autocomplete="off"></div>
    <div class="fp-select-all"><label id="fp-sel-all-label">
      <input type="checkbox" id="fp-sel-all"> <span>(Select All) <span class="fp-count" id="fp-val-count">${uniqueVals.length}</span></span>
    </label></div>
    <div class="fp-values" id="fp-values-list"></div>`;

  const textFilterHtml = `
    <div class="fp-adv-filter">
      <label>Condition</label>
      <select id="fp-text-op">
        <option value="contains"${filter?.op==='contains'?' selected':''}>Contains</option>
        <option value="notContains"${filter?.op==='notContains'?' selected':''}>Does Not Contain</option>
        <option value="equals"${filter?.op==='equals'?' selected':''}>Equals</option>
        <option value="notEquals"${filter?.op==='notEquals'?' selected':''}>Does Not Equal</option>
        <option value="startsWith"${filter?.op==='startsWith'?' selected':''}>Begins With</option>
        <option value="endsWith"${filter?.op==='endsWith'?' selected':''}>Ends With</option>
      </select>
      <label style="margin-top:6px">Value</label>
      <input type="text" id="fp-text-val" value="${esc(filter?.value||'')}" placeholder="Type value…">
    </div>`;

  const dateFilterHtml = `
    <div class="fp-adv-filter">
      <label>Condition</label>
      <select id="fp-date-op">
        <option value="equals"${filter?.op==='equals'?' selected':''}>Equals</option>
        <option value="before"${filter?.op==='before'?' selected':''}>Before</option>
        <option value="after"${filter?.op==='after'?' selected':''}>After</option>
        <option value="between"${filter?.op==='between'?' selected':''}>Between</option>
      </select>
      <label id="fp-date-from-label" style="margin-top:6px">${filter?.op==='between'?'From':'Date'}</label>
      <input type="date" id="fp-date-from" value="${esc(filter?.from||'')}">
      <div id="fp-date-to-wrap" style="display:${filter?.op==='between'?'block':'none'}">
        <label style="margin-top:6px">To</label>
        <input type="date" id="fp-date-to" value="${esc(filter?.to||'')}">
      </div>
    </div>`;

  const html = `
    <div class="fp-backdrop" id="fp-backdrop"></div>
    <div class="fp-panel" id="fp-panel" style="left:${left}px;top:${top}px;">
      <div class="fp-header">
        <span class="fp-title">${esc(col?.h || field)}</span>
        <button class="fp-close" id="fp-close">✕</button>
      </div>
      <div class="fp-sort-row">
        <button class="fp-sort-btn" data-sort="asc">${sortLabel[0]}</button>
        <button class="fp-sort-btn" data-sort="desc">${sortLabel[1]}</button>
      </div>
      ${tabsHtml}
      <div id="fp-tab-content">
        ${currentTab === 'text' ? textFilterHtml : currentTab === 'date' ? dateFilterHtml : valListHtml}
      </div>
      <div class="fp-footer">
        <button class="fp-btn-clear" id="fp-clear">Clear Filter</button>
        <div style="display:flex;gap:6px">
          <button class="fp-btn-cancel" id="fp-cancel">Cancel</button>
          <button class="fp-btn-apply" id="fp-apply">Apply</button>
        </div>
      </div>
    </div>`;

  const mount = document.createElement('div');
  mount.id = 'fp-mount';
  document.body.appendChild(mount);
  mount.innerHTML = html;

  let selSet = new Set(currentSet);
  let tab = currentTab;

  function renderValueList(search = '') {
    const list = document.getElementById('fp-values-list');
    if (!list) return;
    const displayed = uniqueVals.filter(v => v.toLowerCase().includes(search.toLowerCase()));
    document.getElementById('fp-val-count').textContent = displayed.length;

    const allChk = document.getElementById('fp-sel-all');
    const allChecked = displayed.length > 0 && displayed.every(v => selSet.has(v));
    const someChecked = displayed.some(v => selSet.has(v));
    allChk.checked = allChecked;
    allChk.indeterminate = !allChecked && someChecked;

    list.innerHTML = displayed.length === 0
      ? '<div class="fp-empty-msg">No values match</div>'
      : displayed.map(v => `
          <label>
            <input type="checkbox" class="fp-val-chk" data-val="${esc(v)}" ${selSet.has(v)?'checked':''}>
            <span class="${v===''?'fp-val-blank':''}">${v === '' ? '(Blanks)' : esc(v)}</span>
          </label>`).join('');

    list.querySelectorAll('.fp-val-chk').forEach(cb => {
      cb.addEventListener('change', () => {
        if (cb.checked) selSet.add(cb.dataset.val);
        else selSet.delete(cb.dataset.val);
        const d = uniqueVals.filter(v => v.toLowerCase().includes((document.getElementById('fp-search')?.value||'').toLowerCase()));
        const allC = d.length > 0 && d.every(v => selSet.has(v));
        const someC = d.some(v => selSet.has(v));
        allChk.checked = allC;
        allChk.indeterminate = !allC && someC;
      });
    });

    allChk.removeEventListener('change', allChk._handler);
    allChk._handler = () => {
      const d = uniqueVals.filter(v => v.toLowerCase().includes((document.getElementById('fp-search')?.value||'').toLowerCase()));
      if (allChk.checked) d.forEach(v => selSet.add(v));
      else d.forEach(v => selSet.delete(v));
      renderValueList(document.getElementById('fp-search')?.value || '');
    };
    allChk.addEventListener('change', allChk._handler);
  }

  function switchTab(newTab) {
    tab = newTab;
    const content = document.getElementById('fp-tab-content');
    if (!content) return;
    if (tab === 'values') content.innerHTML = valListHtml;
    else if (tab === 'text') content.innerHTML = textFilterHtml;
    else if (tab === 'date') content.innerHTML = dateFilterHtml;
    if (tab === 'values') {
      renderValueList('');
      const searchEl = document.getElementById('fp-search');
      if (searchEl) searchEl.addEventListener('input', () => renderValueList(searchEl.value));
    }
    if (tab === 'text') {
      document.getElementById('fp-text-val')?.focus();
      document.getElementById('fp-text-val')?.addEventListener('keydown', e => {
        if (e.key === 'Enter') doApply();
      });
    }
    if (tab === 'date') {
      const opSel = document.getElementById('fp-date-op');
      if (opSel) opSel.addEventListener('change', () => {
        const wrap = document.getElementById('fp-date-to-wrap');
        const lbl = document.getElementById('fp-date-from-label');
        if (wrap) wrap.style.display = opSel.value === 'between' ? 'block' : 'none';
        if (lbl) lbl.textContent = opSel.value === 'between' ? 'From' : 'Date';
      });
    }
    document.querySelectorAll('.fp-subtab').forEach(btn => {
      btn.className = 'fp-subtab' + (btn.dataset.tab === tab ? ' active' : '');
      btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });
  }

  function doApply() {
    if (tab === 'values') {
      state.columnFilters[field] = selSet.size > 0 ? { type: 'set', values: [...selSet] } : null;
    } else if (tab === 'text') {
      const op = document.getElementById('fp-text-op')?.value || 'contains';
      const val = document.getElementById('fp-text-val')?.value?.trim() || '';
      state.columnFilters[field] = val ? { type: 'text', op, value: val } : null;
    } else if (tab === 'date') {
      const op = document.getElementById('fp-date-op')?.value || 'equals';
      const from = document.getElementById('fp-date-from')?.value || '';
      const to = document.getElementById('fp-date-to')?.value || '';
      state.columnFilters[field] = from ? { type: 'date', op, from, to } : null;
    }
    applyFiltersAndSort();
    renderAll();
    closeFilterPanel();
  }

  if (tab === 'values') {
    renderValueList('');
    const searchEl = document.getElementById('fp-search');
    if (searchEl) searchEl.addEventListener('input', () => renderValueList(searchEl.value));
  }
  if (tab === 'text') {
    document.getElementById('fp-text-val')?.focus();
    document.getElementById('fp-text-val')?.addEventListener('keydown', e => {
      if (e.key === 'Enter') doApply();
    });
  }
  if (tab === 'date') {
    const opSel = document.getElementById('fp-date-op');
    if (opSel) opSel.addEventListener('change', () => {
      const wrap = document.getElementById('fp-date-to-wrap');
      const lbl = document.getElementById('fp-date-from-label');
      if (wrap) wrap.style.display = opSel.value === 'between' ? 'block' : 'none';
      if (lbl) lbl.textContent = opSel.value === 'between' ? 'From' : 'Date';
    });
  }

  document.querySelectorAll('.fp-subtab').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  document.querySelectorAll('.fp-sort-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const dir = btn.dataset.sort;
      state.sortCol = field;
      state.sortDir = dir;
      applyFiltersAndSort();
      renderAll();
      closeFilterPanel();
    });
  });

  document.getElementById('fp-close').addEventListener('click', closeFilterPanel);
  document.getElementById('fp-cancel').addEventListener('click', closeFilterPanel);
  document.getElementById('fp-backdrop').addEventListener('click', closeFilterPanel);
  document.getElementById('fp-clear').addEventListener('click', () => {
    state.columnFilters[field] = null;
    applyFiltersAndSort();
    renderAll();
    closeFilterPanel();
  });
  document.getElementById('fp-apply').addEventListener('click', doApply);

  function onEsc(e) { if (e.key === 'Escape') closeFilterPanel(); }
  document.addEventListener('keydown', onEsc);
  mount._onEsc = onEsc;
}

/* ── Enhanced File Name filter panel ─────────────────────────────────────
 * Same popup as other columns but shows BOTH the value-list section and
 * an advanced Text Filter section together (no tabs).  Apply produces a
 * composite filter combining the two conditions with AND.
 *
 * All string comparisons remain case-insensitive (delegated to the shared
 * passesFilter() 'set'/'text' handlers).  The displayed file names are
 * never modified — case normalisation is comparison-only.
 * ────────────────────────────────────────────────────────────────────── */
function openFileNameFilterPanel(field, rect) {
  const col = COL_CFG.find(c => c.f === field);
  const filter = state.columnFilters[field] || null;

  // Compute unique values the same way the generic panel does — narrowed
  // by every OTHER column filter + global search so the value list stays
  // consistent with what's currently visible.
  const dataForValues = state.allData.filter(row => {
    for (const [f, flt] of Object.entries(state.columnFilters)) {
      if (f === field || !flt) continue;
      if (!passesFilter(row, f, flt)) return false;
    }
    if (state.globalSearch.trim()) {
      const q = state.globalSearch.toLowerCase();
      if (!SEARCHABLE.some(sf => cellStr(row, sf).toLowerCase().includes(q))) return false;
    }
    return true;
  });
  const uniqueVals = [...new Set(dataForValues.map(r => cellStr(r, field)))]
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));

  // Extract pre-existing conditions from either a legacy 'set'/'text' filter
  // or the new 'composite' filter so re-opening the panel restores state.
  let currentSetVals = [];
  let currentTextOp  = 'contains';
  let currentTextVal = '';
  if (filter?.type === 'composite') {
    currentSetVals = Array.isArray(filter.set) ? filter.set : [];
    currentTextOp  = filter.text?.op    || 'contains';
    currentTextVal = filter.text?.value || '';
  } else if (filter?.type === 'set') {
    currentSetVals = filter.values || [];
  } else if (filter?.type === 'text') {
    currentTextOp  = filter.op    || 'contains';
    currentTextVal = filter.value || '';
  }
  // File Name filter no longer offers "Equals" — coerce any stale op that
  // came from a previous session back to the default so the dropdown always
  // reflects a valid option.
  const ALLOWED_OPS = new Set(['contains', 'notContains', 'startsWith', 'endsWith']);
  if (!ALLOWED_OPS.has(currentTextOp)) currentTextOp = 'contains';
  const selSet = new Set(currentSetVals);

  // Position: same clamping logic as the generic panel.  Slightly taller
  // due to the extra section — cap the height and let the value list scroll.
  const panelWidth  = 320;
  const panelHeight = 560;
  let left = Math.max(0, Math.min(rect.left, window.innerWidth - panelWidth - 8));
  let top  = rect.bottom + 4;
  if (top + panelHeight > window.innerHeight) top = Math.max(4, rect.top - panelHeight);

  const html = `
    <div class="fp-backdrop" id="fp-backdrop"></div>
    <div class="fp-panel fp-panel-fn" id="fp-panel" style="left:${left}px;top:${top}px;">
      <div class="fp-header">
        <span class="fp-title">${esc(col?.h || field)}</span>
        <button class="fp-close" id="fp-close">✕</button>
      </div>
      <div class="fp-sort-row">
        <button class="fp-sort-btn" data-sort="asc">A → Z</button>
        <button class="fp-sort-btn" data-sort="desc">Z → A</button>
      </div>

      <div class="fp-section">
        <div class="fp-search"><input type="text" id="fp-search" placeholder="Search file names…" autocomplete="off"></div>
        <div class="fp-select-all"><label id="fp-sel-all-label">
          <input type="checkbox" id="fp-sel-all"> <span>(Select All) <span class="fp-count" id="fp-val-count">${uniqueVals.length}</span></span>
        </label></div>
        <div class="fp-values" id="fp-values-list"></div>
      </div>

      <div class="fp-section fp-section-text">
        <div class="fp-section-title">Text Filter</div>
        <div class="fp-adv-filter">
          <select id="fp-text-op">
            <option value="contains"${currentTextOp==='contains'?' selected':''}>Contains</option>
            <option value="notContains"${currentTextOp==='notContains'?' selected':''}>Does Not Contain</option>
            <option value="startsWith"${currentTextOp==='startsWith'?' selected':''}>Starts With</option>
            <option value="endsWith"${currentTextOp==='endsWith'?' selected':''}>Ends With</option>
          </select>
          <input type="text" id="fp-text-val" value="${esc(currentTextVal)}" placeholder="Enter value…" autocomplete="off">
        </div>
      </div>

      <div class="fp-footer">
        <button class="fp-btn-clear" id="fp-clear">Clear Filter</button>
        <div style="display:flex;gap:6px">
          <button class="fp-btn-cancel" id="fp-cancel">Cancel</button>
          <button class="fp-btn-apply" id="fp-apply">Apply</button>
        </div>
      </div>
    </div>`;

  const mount = document.createElement('div');
  mount.id = 'fp-mount';
  document.body.appendChild(mount);
  mount.innerHTML = html;

  function renderValueList(search = '') {
    const list = document.getElementById('fp-values-list');
    if (!list) return;
    // Case-insensitive search of the checkbox list (existing behaviour).
    const q = search.toLowerCase();
    const displayed = uniqueVals.filter(v => v.toLowerCase().includes(q));
    document.getElementById('fp-val-count').textContent = displayed.length;

    const allChk = document.getElementById('fp-sel-all');
    const allChecked  = displayed.length > 0 && displayed.every(v => selSet.has(v));
    const someChecked = displayed.some(v => selSet.has(v));
    allChk.checked = allChecked;
    allChk.indeterminate = !allChecked && someChecked;

    list.innerHTML = displayed.length === 0
      ? '<div class="fp-empty-msg">No values match</div>'
      : displayed.map(v => `
          <label>
            <input type="checkbox" class="fp-val-chk" data-val="${esc(v)}" ${selSet.has(v)?'checked':''}>
            <span class="${v===''?'fp-val-blank':''}">${v === '' ? '(Blanks)' : esc(v)}</span>
          </label>`).join('');

    list.querySelectorAll('.fp-val-chk').forEach(cb => {
      cb.addEventListener('change', () => {
        if (cb.checked) selSet.add(cb.dataset.val);
        else selSet.delete(cb.dataset.val);
        const searchEl = document.getElementById('fp-search');
        const d = uniqueVals.filter(v => v.toLowerCase().includes((searchEl?.value||'').toLowerCase()));
        const allC = d.length > 0 && d.every(v => selSet.has(v));
        const someC = d.some(v => selSet.has(v));
        allChk.checked = allC;
        allChk.indeterminate = !allC && someC;
      });
    });

    allChk.removeEventListener('change', allChk._handler);
    allChk._handler = () => {
      const searchEl = document.getElementById('fp-search');
      const d = uniqueVals.filter(v => v.toLowerCase().includes((searchEl?.value||'').toLowerCase()));
      if (allChk.checked) d.forEach(v => selSet.add(v));
      else                d.forEach(v => selSet.delete(v));
      renderValueList(searchEl?.value || '');
    };
    allChk.addEventListener('change', allChk._handler);
  }

  function doApply() {
    const op  = document.getElementById('fp-text-op')?.value || 'contains';
    // Trim leading/trailing whitespace; empty → no text condition.
    const raw = document.getElementById('fp-text-val')?.value || '';
    const val = raw.trim();

    const setSide  = selSet.size > 0 ? [...selSet] : null;
    const textSide = val ? { op, value: val } : null;

    if (!setSide && !textSide) {
      state.columnFilters[field] = null;
    } else {
      state.columnFilters[field] = { type: 'composite', set: setSide, text: textSide };
    }
    applyFiltersAndSort();
    // Filter change may shrink the total pages — reset to first page like
    // the other filter flows would (they render before pagination guards).
    state.page = 1;
    renderAll();
    closeFilterPanel();
  }

  // Initial render + wire the search box, text-value Enter-to-apply, sort
  // buttons, and Cancel/Close/Clear.
  renderValueList('');
  document.getElementById('fp-search').addEventListener('input', e => renderValueList(e.target.value));
  document.getElementById('fp-text-val').addEventListener('keydown', e => {
    if (e.key === 'Enter') doApply();
  });

  document.querySelectorAll('.fp-sort-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      state.sortCol = field;
      state.sortDir = btn.dataset.sort;
      applyFiltersAndSort();
      renderAll();
      closeFilterPanel();
    });
  });

  document.getElementById('fp-close').addEventListener('click', closeFilterPanel);
  document.getElementById('fp-cancel').addEventListener('click', closeFilterPanel);
  document.getElementById('fp-backdrop').addEventListener('click', closeFilterPanel);
  document.getElementById('fp-clear').addEventListener('click', () => {
    state.columnFilters[field] = null;
    applyFiltersAndSort();
    renderAll();
    closeFilterPanel();
  });
  document.getElementById('fp-apply').addEventListener('click', doApply);

  function onEsc(e) { if (e.key === 'Escape') closeFilterPanel(); }
  document.addEventListener('keydown', onEsc);
  mount._onEsc = onEsc;
}

function closeFilterPanel() {
  const mount = document.getElementById('fp-mount');
  if (mount) {
    if (mount._onEsc) document.removeEventListener('keydown', mount._onEsc);
    mount.remove();
  }
  state.activeFilter = null;
}

/* ── Column visibility panel ────────────────────────────────────────────── */
function openColPanel() {
  const wrap = $('col-btn-wrap');
  if (document.getElementById('colpanel')) {
    closeColPanel();
    return;
  }

  // Bucket columns by their `group` tag so the picker can render section
  // headings (§17 spec: "General" + "SharePoint Hierarchy").  Untagged
  // columns fall into the leading "General" bucket in original order.
  const groups = new Map();
  groups.set('General', []);
  for (const c of COL_CFG) {
    const g = c.group || 'General';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(c);
  }

  const renderGroup = (title, cols) => {
    if (!cols.length) return '';
    const rows = cols.map(c => `
        <label class="colpanel-row">
          <input type="checkbox" class="col-toggle" data-field="${c.f}" ${!state.hiddenCols.has(c.f)?'checked':''}>
          ${esc(c.h)}
        </label>`).join('');
    // Each group also gets a compact "Show all / hide all" affordance so
    // toggling Folder 5..20 en masse doesn't require 16 clicks.
    return `
      <div class="colpanel-group">
        <div class="colpanel-group-head">
          <span>${esc(title)}</span>
          <span class="colpanel-group-actions">
            <button type="button" class="colpanel-group-btn" data-group="${esc(title)}" data-action="show">Show all</button>
            <button type="button" class="colpanel-group-btn" data-group="${esc(title)}" data-action="hide">Hide all</button>
          </span>
        </div>
        ${rows}
      </div>`;
  };

  const html = `
    <div class="colpanel-backdrop" id="colpanel-backdrop"></div>
    <div class="colpanel" id="colpanel">
      <div class="colpanel-head">Columns</div>
      ${[...groups.entries()].map(([g, cols]) => renderGroup(g, cols)).join('')}
    </div>`;

  const mount = document.createElement('div');
  mount.id = 'colpanel-mount';
  mount.style.cssText = 'position:relative;';
  wrap.appendChild(mount);
  mount.innerHTML = html;

  mount.querySelectorAll('.col-toggle').forEach(cb => {
    cb.addEventListener('change', () => {
      const f = cb.dataset.field;
      if (cb.checked) state.hiddenCols.delete(f);
      else state.hiddenCols.add(f);
      localStorage.setItem('cmr-hidden', JSON.stringify([...state.hiddenCols]));
      renderAll();
    });
  });

  // Group-level bulk toggles — keep group headers responsive.
  mount.querySelectorAll('.colpanel-group-btn').forEach(btn => {
    btn.addEventListener('click', (ev) => {
      ev.preventDefault();
      const g = btn.dataset.group;
      const action = btn.dataset.action;                // 'show' | 'hide'
      const cols = groups.get(g) || [];
      for (const c of cols) {
        if (action === 'show') state.hiddenCols.delete(c.f);
        else                   state.hiddenCols.add(c.f);
      }
      localStorage.setItem('cmr-hidden', JSON.stringify([...state.hiddenCols]));
      // Reflect the new checked state without tearing down the panel.
      mount.querySelectorAll('.col-toggle').forEach(cb => {
        cb.checked = !state.hiddenCols.has(cb.dataset.field);
      });
      renderAll();
    });
  });

  document.getElementById('colpanel-backdrop').addEventListener('click', closeColPanel);
}

function closeColPanel() {
  document.getElementById('colpanel-mount')?.remove();
}

/* ── Global folder-filter popover (Philippe requirement) ──────────────────
 * A compact 3-field form (text + match-mode + Apply/Clear).  Persists into
 * state.folderFilter, then re-runs applyFiltersAndSort() + renderAll().
 * Rendered inside #folder-filter-btn-wrap using the same absolute-under-
 * button positioning pattern as the Columns and Folder-navigator panels. */
function openFolderFilterPanel() {
  const wrap = document.getElementById('folder-filter-btn-wrap');
  if (!wrap) return;
  if (document.getElementById('folder-filter-panel')) {
    closeFolderFilterPanel();
    return;
  }

  const curText = state.folderFilter.text || '';
  const curMode = state.folderFilter.mode || 'contains';
  const modeOpt = (v, l) => `<option value="${v}"${v === curMode ? ' selected' : ''}>${l}</option>`;

  const html = `
    <div class="colpanel-backdrop" id="folder-filter-backdrop"></div>
    <div class="folder-filter-panel" id="folder-filter-panel" role="dialog" aria-label="Folder filter">
      <div class="folder-filter-head">Folder Filter</div>
      <div class="folder-filter-row">
        <label for="ff-text">Folder name</label>
        <input type="text" id="ff-text" class="folder-filter-input"
               placeholder="e.g. Services, Don't Use"
               value="${esc(curText)}" autocomplete="off">
      </div>
      <div class="folder-filter-row">
        <label for="ff-mode">Match</label>
        <select id="ff-mode" class="folder-filter-select">
          ${modeOpt('contains',    'Contains')}
          ${modeOpt('starts_with', 'Starts With')}
          ${modeOpt('exact',       'Exact Match')}
        </select>
      </div>
      <div class="folder-filter-hint">
        Searches across <b>Folder 1 – Folder 20</b> for every document in the
        entire inventory. Case-insensitive. Combines with the current bucket,
        general search and folder navigation.
      </div>
      <div class="folder-filter-actions">
        <button type="button" class="folder-filter-clear" id="ff-clear">Clear</button>
        <button type="button" class="folder-filter-apply" id="ff-apply">Apply Filter</button>
      </div>
    </div>`;

  const mount = document.createElement('div');
  mount.id = 'folder-filter-mount';
  mount.style.cssText = 'position:relative;';
  wrap.appendChild(mount);
  mount.innerHTML = html;

  const textEl = document.getElementById('ff-text');
  const modeEl = document.getElementById('ff-mode');
  const apply = () => {
    state.folderFilter = {
      text: (textEl.value || '').trim(),
      mode: modeEl.value || 'contains',
    };
    _syncFolderFilterButton();
    state.page = 1;                        // §16: reset pagination for filter
    applyFiltersAndSort();
    renderAll();
    closeFolderFilterPanel();
  };
  const clearAndClose = () => {
    state.folderFilter = { text: '', mode: 'contains' };
    _syncFolderFilterButton();
    state.page = 1;
    applyFiltersAndSort();
    renderAll();
    closeFolderFilterPanel();
  };

  document.getElementById('ff-apply').addEventListener('click', apply);
  document.getElementById('ff-clear').addEventListener('click', clearAndClose);
  document.getElementById('folder-filter-backdrop').addEventListener('click', closeFolderFilterPanel);
  textEl.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter')  { ev.preventDefault(); apply(); }
    if (ev.key === 'Escape') { ev.preventDefault(); closeFolderFilterPanel(); }
  });

  // Autofocus for a fast keyboard workflow.
  setTimeout(() => { try { textEl.focus(); textEl.select(); } catch (_) {} }, 0);
}

function closeFolderFilterPanel() {
  document.getElementById('folder-filter-mount')?.remove();
}

/* ── Migration modal ────────────────────────────────────────────────────── */
function openMigrateModal() {
  // Per-user workload lock — belt-and-braces guard so a keyboard shortcut
  // or programmatic click still can't open the modal while the user
  // already has an active batch.  updateToolbar() disables #migrate-btn
  // in this state, but the click handler runs regardless of button
  // state on some browsers when triggered from code, and the server
  // will 409 anyway.  This just short-circuits before the modal opens.
  const pc = state.populationCounts || {};
  if (pc.can_migrate === false) {
    const n = Number(pc.my_in_processing) || 0;
    showToast(
      `You currently have ${n} file${n !== 1 ? 's' : ''} in processing. `
      + `New migrations can be submitted after your current batch completes.`
    );
    return;
  }

  // Only Pending / Failed (selectable) docs may actually be migrated.
  const eligible = [...state.selectedIds].filter(id => {
    const row = state.allData.find(r => r.fileID === id);
    return row && isSelectable(row);
  });
  const n = eligible.length;
  if (n === 0) return;

  // Group the selection by (source site, library, root-folder) tuple so
  // the user sees exactly how many migration requests will be created
  // and where every file is going.  Grouping mirrors the server's own
  // logic in services.migration_platform.group_files_for_submission.
  const groups = _summariseMigrateSelection(eligible);
  const groupsHtml = groups.length
    ? `<div class="migrate-groups">
         ${groups.map(g => `
           <div class="migrate-group">
             <div class="migrate-group-row">
               <span class="migrate-group-label">From</span>
               <span class="migrate-group-value" title="${_escapeHtml(g.sourceDisplay)}">${_escapeHtml(g.sourceDisplay)}</span>
             </div>
             <div class="migrate-group-row">
               <span class="migrate-group-label">To</span>
               <span class="migrate-group-value" title="${_escapeHtml(g.destinationDisplay)}">${_escapeHtml(g.destinationDisplay)}</span>
             </div>
             <div class="migrate-group-row">
               <span class="migrate-group-label">Files</span>
               <span class="migrate-group-value">${g.count}</span>
             </div>
           </div>`).join('')}
       </div>`
    : `<div class="migrate-groups migrate-groups-empty">
         Unable to determine source folders for the selection. The server will
         validate each file before submission.
       </div>`;

  const migrationCount = groups.length;
  const migrationCountLabel = migrationCount === 1
    ? '1 migration request'
    : `${migrationCount} migration requests`;

  const html = `
    <div class="modal-overlay" id="modal-overlay">
      <div class="modal modal-wide">
        <div class="modal-header">
          <span class="modal-title">Confirm Copy to Destination</span>
        </div>
        <div class="modal-body">
          <p class="modal-lead">
            <strong>${n}</strong> document${n !== 1 ? 's' : ''} will be <b>copied</b>
            to the destination SharePoint via the Migration Platform
            (${migrationCountLabel}).
          </p>
          ${groupsHtml}
          <div class="modal-note">
            <b>What happens:</b> each selected document is submitted to the
            Migration Platform, which copies it to the destination shown above.
            Row status becomes <b>In Processing</b> immediately and updates to
            <b>Migrated</b> once the copy is confirmed. Files that fail become
            <b>Failed</b> — you can retry them later.
            <br><br>
            <b>Your source files are not modified or deleted</b> — this is a
            one-way copy. Nothing is touched in the source SharePoint site.
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn-cancel-modal" id="modal-cancel">Cancel</button>
          <button class="btn-confirm-modal" id="modal-confirm">Copy to Destination</button>
        </div>
      </div>
    </div>`;

  const mount = document.createElement('div');
  mount.id = 'modal-mount';
  document.body.appendChild(mount);
  mount.innerHTML = html;

  $('modal-cancel').addEventListener('click', () => mount.remove());
  $('modal-overlay').addEventListener('click', e => { if (e.target === $('modal-overlay')) mount.remove(); });
  $('modal-confirm').addEventListener('click', async () => {
    mount.remove();
    await performMigration(new Set(eligible));
  });
}

/**
 * Group a set of selected FileIDs by their (site, library, root-folder)
 * so the confirmation modal can preview exactly what will be submitted.
 * Returns [{sourceDisplay, destinationDisplay, count}, ...].
 * Rows whose SharePointPath cannot be parsed are lumped under a single
 * "Unknown source" group so the user still sees them and can decide.
 */
function _summariseMigrateSelection(fileIds) {
  const cfg = state.migrationConfig || {};
  const destLibrary    = (cfg.destLibrary    || '').trim();
  const destFolderPath = (cfg.destFolderPath || '').trim();
  // Base destination prefix uses the real configured library + folder,
  // mirroring services.migration_paths.build_destination_path().  When
  // the server hasn't been configured, show a neutral placeholder so
  // the user knows the preview isn't authoritative rather than seeing
  // a stale "Wave2 destination/…" hardcoded string.
  const destPrefix = destLibrary
    ? `${destLibrary}${destFolderPath ? '/' + destFolderPath : ''}`
    : '(destination not configured)';

  const byGroup = new Map();
  for (const fid of fileIds) {
    const row = state.allData.find(r => r.fileID === fid);
    if (!row) continue;
    const parsed = _parseSharePointPathForPreview(row.sharePointPath);
    const key = parsed
      ? `${parsed.siteUrl}|${parsed.library}|${parsed.folderPath}`
      : '__invalid__';
    const g = byGroup.get(key) || {
      sourceDisplay:      parsed
        ? `${parsed.library}${parsed.folderPath ? '/' + parsed.folderPath : ''}`
        : 'Unknown source (SharePoint path not recognised)',
      destinationDisplay: parsed
        ? (parsed.folderPath
            ? `${destPrefix}/${parsed.folderPath}`
            : `${destPrefix} (library root)`)
        : '—',
      count: 0,
    };
    g.count += 1;
    byGroup.set(key, g);
  }
  return [...byGroup.values()];
}

/**
 * Client-side approximation of services.migration_paths.parse_sharepoint_url.
 * Used only for modal preview — the server re-parses authoritatively.
 * Returns {siteUrl, library, folderPath} or null.
 */
function _parseSharePointPathForPreview(url) {
  if (!url || typeof url !== 'string') return null;
  const stripped = url.split('#')[0].split('?')[0].trim();
  if (!/^https?:\/\//i.test(stripped)) return null;
  let u;
  try { u = new URL(stripped); } catch { return null; }
  const segs = u.pathname.split('/').filter(Boolean).map(decodeURIComponent);
  if (segs.length < 4 || segs[0].toLowerCase() !== 'sites') return null;
  // Drop technical segments (Forms, AllItems.aspx) but keep the library name.
  const cleaned = segs.slice(2).filter(s => !/^(forms|allitems\.aspx?)$/i.test(s));
  if (cleaned.length < 2) return null;
  return {
    siteUrl:    `${u.protocol}//${u.host}/sites/${segs[1]}`,
    library:    cleaned[0],
    folderPath: cleaned.slice(1, -1).join('/'),
  };
}

function _escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* ── Migration service ──────────────────────────────────────────────────────
 * Talks to POST /api/migrate which (post-platform-integration):
 *   1. Persists MigrationStatus='In Processing' for eligible FileIDs.
 *   2. Splits the eligible batch by (site, library, root-folder) tuple.
 *   3. Per group: POST to the migration platform's /api/v1/migrations then
 *      /api/v1/migrations/{id}/files/batch and persists the returned
 *      MigrationRequestId + file_item IDs on our rows.
 *   4. Groups whose platform call fails are rolled back to 'Failed';
 *      successful groups stay In Processing until /api/migrations/sync
 *      observes the terminal outcome.
 *
 * Response contract (all fields always present):
 *   {
 *     runId:               string | null,           // first migrationRequestId, back-compat
 *     migrationRequestIds: string[],                // NEW canonical — one per group
 *     inProcessing:        string[],                // ids persisted to In Processing
 *     submitted:           { [migrationId]: string[] },  // per-migration file IDs
 *     failed:              Array<{fileID, error}>,  // rolled back to Failed
 *     skipped:             string[],                // not eligible for phase 1
 *     invalid:             Array<{fileID, error}>,  // unparseable SharePointPath
 *     groups:              Array<{migrationId, sourceSite, sourceLibrary,
 *                                sourceFolder, fileCount, status, error}>,
 *     error:               string | null
 *   }
 * ────────────────────────────────────────────────────────────────────────── */
// Thrown by migrationService.migrate() when the server enforces the
// per-user workload lock (409 with detail.reason='ACTIVE_MIGRATION_EXISTS').
// performMigration() catches this specifically so we can show a friendly
// message + refresh state.populationCounts from the response instead of
// treating it as a generic network failure.
class ActiveMigrationLockError extends Error {
  constructor(activeCount, message) {
    super(message || 'Active migration already in progress for your account.');
    this.name          = 'ActiveMigrationLockError';
    this.activeCount   = Number(activeCount) || 0;
    this.reason        = 'ACTIVE_MIGRATION_EXISTS';
  }
}

const migrationService = {
  async migrate(ids) {
    const res = await fetch('/api/migrate', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ ids: [...ids] }),
    });
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      let detail = null;
      try { const j = await res.json(); detail = j.detail; if (detail) msg = (typeof detail === 'string') ? detail : (detail.message || msg); } catch {}
      // 409 + ACTIVE_MIGRATION_EXISTS → per-user workload lock hit.
      // Server is the source of truth here: even if the client thought
      // canMigrate was true (stale populationCounts), the server just
      // told us otherwise.  Surface a typed error so the caller can
      // rehydrate state + show the correct message.
      if (res.status === 409 && detail && typeof detail === 'object'
          && detail.reason === 'ACTIVE_MIGRATION_EXISTS') {
        throw new ActiveMigrationLockError(detail.activeCount, detail.message);
      }
      throw new Error(msg);
    }
    const json = await res.json();
    return {
      runId:               json.runId || null,
      migrationRequestIds: Array.isArray(json.migrationRequestIds) ? json.migrationRequestIds : [],
      inProcessing:        Array.isArray(json.inProcessing) ? json.inProcessing : [],
      submitted:           (json.submitted && typeof json.submitted === 'object') ? json.submitted : {},
      failed:              Array.isArray(json.failed)  ? json.failed  : [],
      skipped:             Array.isArray(json.skipped) ? json.skipped : [],
      invalid:             Array.isArray(json.invalid) ? json.invalid : [],
      groups:              Array.isArray(json.groups)  ? json.groups  : [],
      error:               json.error || null,
    };
  },
  /** Poll every migration with any In Processing row and apply per-file
   *  status deltas.  Returns:
   *   { polled, updated, counts, errors }
   *  where counts is the same shape /api/contracts returns. */
  async sync() {
    const res = await fetch('/api/migrations/sync', {
      method:      'POST',
      credentials: 'same-origin',
      headers:     { 'content-type': 'application/json' },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  },
};

// "09/18/2026 03:45:12 PM" — zero-padded US format, matches parseDate() rules.
// Kept as a thin wrapper around toUsDateTime() so the display format stays
// consistent everywhere (ingest + fresh-migration path).
function formatMigrationTimestamp(d) {
  return toUsDateTime(d);
}

async function performMigration(ids) {
  // Guard: only Pending/Failed rows may be sent.
  const eligibleIds = [...ids].filter(id => {
    const row = state.allData.find(r => r.fileID === id);
    return row && isSelectable(row);
  });
  if (!eligibleIds.length) {
    showToast('No eligible documents to migrate.');
    return;
  }

  // Disable the button while the call is in flight
  const btn = $('migrate-btn');
  const oldTxt = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Submitting…';

  // Show the live progress badge immediately so the click feels responsive
  // (the /api/migrate round-trip can take 2–4s while groups are created).
  migProgress.startSubmitting(eligibleIds.length);

  let result;
  try {
    result = await migrationService.migrate(eligibleIds);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = oldTxt;
    migProgress.hide();
    // Per-user workload lock hit on the server.  Sync local state to
    // match what the server just told us, re-render the button as
    // locked, and show the exact message the server sent (which
    // includes the accurate active count).  No files were flipped.
    if (err && err.name === 'ActiveMigrationLockError') {
      state.populationCounts = {
        ...state.populationCounts,
        my_in_processing: err.activeCount,
        my_active_total:  err.activeCount,
        can_migrate:      false,
      };
      applyFiltersAndSort();
      renderAll();
      showToast(err.message);
      return;
    }
    showToast(`Could not submit to migration platform: ${err.message || err}`);
    return;
  } finally {
    // Always release the button — polling handles the rest of the lifecycle.
    // updateToolbar() (called by renderAll below) will re-lock it if
    // can_migrate came back false from /api/migrate's response.
    btn.disabled = false;
    btn.textContent = oldTxt;
  }

  // ── Mirror server state locally so the UI is honest without a refetch ──
  // 1. Submitted rows → In Processing (server already persisted this).
  const inProcessingSet = new Set(result.inProcessing);
  // 2. Failed-at-submit rows (platform 5xx, invalid path, group rollback)
  //    → Failed.  Same set the server rolled back to Failed via
  //    apply_migration_result().
  const failedMap = new Map(result.failed.map(f => [f.fileID, f.error]));
  const submittedByMigrationId = result.submitted || {};

  state.allData.forEach(r => {
    if (inProcessingSet.has(r.fileID)) {
      r.migrationStatus = STATUS.IN_PROCESSING;
      // Stamp the migration_request_id on the row so the Status column
      // can link to the migration detail.  Server will echo this back
      // on the next /api/contracts refresh anyway.
      for (const [mid, fids] of Object.entries(submittedByMigrationId)) {
        if (fids.includes(r.fileID)) { r.migrationRequestId = mid; break; }
      }
    } else if (failedMap.has(r.fileID)) {
      r.migrationStatus = STATUS.FAILED;
      r.errorMessage    = failedMap.get(r.fileID) || r.errorMessage || '';
    }
  });

  // Submitted rows leave the selection immediately (they're no longer Pending).
  inProcessingSet.forEach(id => state.selectedIds.delete(id));
  failedMap.forEach((_e, id) => state.selectedIds.delete(id));

  applyFiltersAndSort();
  renderAll();

  // ── User feedback ──
  // Primary channel: the live progress badge (top-right).  It tracks the
  // current cohort from Submitting → Migrating → Success / Partial / Failed
  // and updates on every /api/migrations/sync tick.  This replaces the
  // old "N files submitted (M migrations)" toast which users read as
  // "M migrated" — see fix history 2026-09-22.
  migProgress.onSubmitted(result);

  // Secondary channel: short-lived toast for edge cases the badge does
  // NOT surface (invalid SharePoint paths, already-processed rows).
  const nInv  = result.invalid.length;
  const nSkip = result.skipped.length;
  const extraParts = [];
  if (nInv  > 0) extraParts.push(`${nInv} rejected (bad SharePoint path)`);
  if (nSkip > 0) extraParts.push(`${nSkip} skipped (already processed)`);
  if (extraParts.length > 0) showToast(extraParts.join(' · '));

  // ── Kick the poller — it will refresh counts + row statuses every
  // MIG_POLL_INTERVAL_MS (see below) until in_processing hits 0.  Safe
  // to call while already running (idempotent via a timer-not-null
  // guard inside migrationPoller.start).
  //
  // We start the poller whenever ANY row was flipped to In Processing
  // by /api/migrate — the server has now committed those rows and the
  // background submission is running.  The very first poll tick fires
  // immediately (see start()) so counts refresh within ~200ms of the
  // click, without waiting for the full interval.
  if (inProcessingSet.size > 0) {
    migrationPoller.start();
  }
}

/* ── Migration status poller ───────────────────────────────────────────────
 * When any row is In Processing, poll /api/migrations/sync every 15s so
 * the UI reflects the platform's authoritative state without the user
 * having to reload.  The poller:
 *   * self-throttles (idempotent — repeated .start() calls are no-ops),
 *   * hides itself when in_processing hits 0,
 *   * refetches /api/contracts opportunistically when the sync reports
 *     any row-level updates so the actual row.migrationStatus values,
 *     destination URLs, retry counts etc. reflect the latest DB state,
 *   * survives a browser refresh via start-on-load in enterReviewScreen(),
 *   * bails out and stops on repeated errors (max 5 in a row) so a broken
 *     platform doesn't spam the network forever.
 * ────────────────────────────────────────────────────────────────────────── */
const migrationPoller = (function () {
  // 8s — inside the 5–10s band the product team specified.  Faster than
  // the previous 15s so live "Remaining N/M" counts feel responsive
  // without hammering the platform or Azure SQL.  Adjustable at the
  // module level; the browser never reads a server-provided interval.
  const INTERVAL_MS = 8_000;
  const MAX_CONSECUTIVE_ERRORS = 5;
  // Timer handle — non-null means a poll cycle is scheduled.  The
  // start() guard `if (timer) return` prevents two concurrent timers
  // even if start() is called from multiple code paths (Migrate click,
  // enterReviewScreen resume, page-refresh recovery).
  let timer = null;
  let inFlight = false;
  let consecutiveErrors = 0;

  async function tick() {
    if (inFlight) return;               // avoid overlap on slow networks
    inFlight = true;
    try {
      const res = await migrationService.sync();
      consecutiveErrors = 0;

      // Update the KPI-bar counts immediately (cheap, no refetch needed).
      if (res.counts && typeof res.counts === 'object') {
        state.populationCounts = {
          ...state.populationCounts,
          active:        Number(res.counts.active)        || state.populationCounts.active,
          excluded:      Number(res.counts.excluded)      || state.populationCounts.excluded,
          total:         Number(res.counts.total)         || state.populationCounts.total,
          pending:       Number(res.counts.pending)       || 0,
          in_processing: Number(res.counts.in_processing) || 0,
          migrated:      Number(res.counts.migrated)      || 0,
          failed:        Number(res.counts.failed)        || 0,
          // Per-user workload-lock fields — server includes these
          // whenever the session is authenticated.  When they arrive we
          // trust them absolutely: this is the mechanism that
          // auto-unlocks the Migrate button as the user's rows complete
          // (my_in_processing goes 50 → 42 → 30 → … → 0 → can_migrate
          // flips true → next renderAll enables the button).
          my_in_processing: Number(res.counts.my_in_processing) || 0,
          my_active_total:  Number(res.counts.my_active_total)  || 0,
          can_migrate:      (res.counts.can_migrate !== false),
        };
      }

      // Always refresh the actual row data on every tick — the user
      // asked for live per-file decrement (one file completes → tile
      // drops from 10 to 9 to 8 …).  Refreshing only on transitions
      // meant the table could lag the tiles by one tick when the
      // server's counts already showed the drop.  /api/contracts is
      // cheap (single indexed read of ~641 rows) and the poller only
      // runs while in_processing > 0, so this is bounded work.
      const anyUpdated = res.updated && (
        (res.updated.migrated || 0) +
        (res.updated.failed || 0) +
        (res.updated.skipped || 0) +
        (res.updated.in_processing || 0)
      ) > 0;
      if (anyUpdated) {
        // Row-level changes happened — pull fresh rows so Status column,
        // destination URL, retry count, error message all reflect DB.
        await refreshContractRows();
      } else {
        // Nothing changed row-wise but counts (and now Failed bucket)
        // may still have — re-render tiles from state.populationCounts
        // which the block above just refreshed from the sync response.
        renderAll();
      }

      // Update the live progress badge (top-right) so the user sees
      // cohort-level progress even between full-table refreshes.  Runs
      // AFTER refreshContractRows() so state.allData holds the freshest
      // per-row migrationStatus.
      migProgress.tick();

      // Stop when nothing is in flight anymore.
      if ((state.populationCounts.in_processing || 0) === 0) {
        stop();
      }
    } catch (err) {
      consecutiveErrors += 1;
      if (consecutiveErrors >= MAX_CONSECUTIVE_ERRORS) {
        showToast('Migration status updates paused — the platform is not reachable. Reload the page to retry.');
        stop();
      }
    } finally {
      inFlight = false;
    }
  }

  function start() {
    if (timer) return;                  // idempotent
    // Fire once immediately so the user sees updates without waiting.
    tick();
    timer = setInterval(tick, INTERVAL_MS);
  }

  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
    consecutiveErrors = 0;
  }

  function isRunning() { return timer != null; }

  return { start, stop, isRunning, _tick: tick };
})();

/** Re-fetch /api/contracts and merge into state.allData.  Used by the
 *  poller when the sync reports row-level updates so the UI reflects the
 *  DB (destination URL, retry count, error message, etc.) without the
 *  user having to reload the page. */
async function refreshContractRows() {
  const res = await fetch('/api/contracts',
                          { cache: 'no-store', credentials: 'same-origin' });
  if (res.status === 401) { window.location.assign('/login'); return; }
  if (!res.ok) return;
  const json = await res.json();
  const rows = Array.isArray(json.data) ? json.data : [];
  // Merge by fileID so we don't lose any client-side ephemeral flags.
  const byId = new Map(state.allData.map(r => [r.fileID, r]));
  rows.forEach(r => {
    const merged = {
      ...byId.get(r.fileID),
      ...r,
      migrate:         (r.migrate === 'Yes' || r.migrate === true) ? 'Yes' : 'No',
      migratedDate:    r.migratedDate || '',
      migrationStatus: r.migrationStatus
                       || (r.migrate === 'Yes' ? STATUS.MIGRATED : STATUS.PENDING),
    };
    byId.set(r.fileID, normaliseDatesInRow(merged));
  });
  state.allData = [...byId.values()];
  if (json.counts) {
    state.populationCounts = {
      ...state.populationCounts,
      ...json.counts,
    };
  }
  applyFiltersAndSort();
  renderAll();
}

/* ── Exclusion (soft-delete) ───────────────────────────────────────────────
 * Marks selected FileIDs as Excluded='Yes' in the database.  Excluded rows
 * are filtered out of /api/contracts on subsequent reads, so the Manual
 * Review UI never shows them again as active migration candidates.
 *
 * IMPORTANT — this is a SOFT delete.  No SharePoint file is touched.
 * The DB row is retained (audit history) with Excluded='Yes'.
 * ────────────────────────────────────────────────────────────────────────── */
const exclusionService = {
  /**
   * POST selected FileIDs to /api/exclude.
   *
   * @param {Iterable<string>} ids                — file IDs to exclude
   * @param {object}          [audit]             — optional audit metadata
   * @param {string}          [audit.reason]      — e.g. "Matched folder filter"
   * @param {string}          [audit.folderText]  — the folder filter text
   * @param {string}          [audit.folderMode]  — "contains"|"starts_with"|"exact"
   * @param {Object<string,{level:string,value:string}>} [audit.matches]
   *          per-file matched folder level/value (fileID -> {level, value}).
   *
   * When `audit` is omitted (row-selection Exclude), the payload contains
   * only `ids` and the server writes NULLs for reason/level/value — full
   * back-compat with the old behaviour.
   */
  async exclude(ids, audit) {
    const body = { ids: [...ids] };
    if (audit && audit.reason) {
      body.reason           = audit.reason;
      body.folderFilterText = audit.folderText || null;
      body.folderFilterMode = audit.folderMode || null;
      if (audit.matches && Object.keys(audit.matches).length) {
        body.matches = audit.matches;
      }
    }
    const res = await fetch('/api/exclude', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { const j = await res.json(); if (j.detail) msg = j.detail; } catch {}
      throw new Error(msg);
    }
    const json = await res.json();
    return {
      succeeded: Array.isArray(json.succeeded) ? json.succeeded : [...ids],
      failed:    Array.isArray(json.failed)    ? json.failed    : [],
    };
  },
};

function openExcludeModal() {
  // Only PENDING rows are eligible for exclusion — already-migrated rows are
  // silently skipped so the audit trail stays intact.  The modal shows the
  // count that will actually be moved.
  const eligible = [...state.selectedIds].filter(id => {
    const row = state.allData.find(r => r.fileID === id);
    return row && isPending(row);
  });
  const n = eligible.length;
  if (n === 0) return;

  // Compute audit payload if the global folder filter is active.  We
  // capture the level+value that CAUSED each row to be present so the
  // exclusion_audit table records exactly why each file was excluded
  // (§10 spec).  Rows without a match (e.g. selected before the filter
  // was applied) are omitted from the map — server persists NULL for them.
  let auditPayload = null;
  let filterNoteHtml = '';
  if (isFolderFilterActive()) {
    const matches = {};
    let distinctCustomers = new Set();
    for (const id of eligible) {
      const row = state.allData.find(r => r.fileID === id);
      if (row && row.__folderMatch) {
        matches[id] = { level: row.__folderMatch.level, value: row.__folderMatch.value };
      }
      if (row && row.customerName) distinctCustomers.add(row.customerName);
    }
    auditPayload = {
      reason:     'Matched folder filter',
      folderText: state.folderFilter.text,
      folderMode: state.folderFilter.mode,
      matches,
    };
    const modeLabel = { contains: 'contains', starts_with: 'starts with', exact: 'exact' }[state.folderFilter.mode] || 'contains';
    filterNoteHtml = `
      <div class="modal-note" style="background:#eef4ff;border:1px solid #c7d7f5;color:#1e3a8a;">
        Folder filter active — <b>Folder ${esc(modeLabel)}: “${esc(state.folderFilter.text)}”</b><br>
        <span style="opacity:.8">${n} file${n !== 1 ? 's' : ''} across ${distinctCustomers.size} customer${distinctCustomers.size !== 1 ? 's' : ''}. The matched folder level for each file will be recorded in the exclusion audit.</span>
      </div>`;
  }

  const html = `
    <div class="modal-overlay" id="modal-overlay">
      <div class="modal">
        <div class="modal-header">
          <span class="modal-title">Exclude Selected Files</span>
        </div>
        <div class="modal-body">
          You are about to exclude <strong>${n}</strong> selected file${n !== 1 ? 's' : ''} from the migration review list.
          ${filterNoteHtml}
          <div class="modal-note">
            These files will be moved to the <b>Excluded</b> list and will no
            longer be considered for migration. They are <b>not</b> deleted —
            you can restore them from the Excluded view at any time. The
            source documents in SharePoint are not affected.
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn-cancel-modal" id="modal-cancel">Cancel</button>
          <button class="btn-confirm-modal btn-danger" id="modal-confirm">Exclude ${n} File${n !== 1 ? 's' : ''}</button>
        </div>
      </div>
    </div>`;

  const mount = document.createElement('div');
  mount.id = 'modal-mount';
  document.body.appendChild(mount);
  mount.innerHTML = html;

  const close = () => mount.remove();
  $('modal-cancel').addEventListener('click', close);
  $('modal-overlay').addEventListener('click', e => { if (e.target === $('modal-overlay')) close(); });
  $('modal-confirm').addEventListener('click', async () => {
    close();
    await performExclusion(eligible, auditPayload);
  });
}

async function performExclusion(ids, audit) {
  const idSet = new Set(ids);
  if (idSet.size === 0) return;

  const btn = $('exclude-btn');
  const oldTxt = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Excluding…';

  let result;
  try {
    // `audit` is passed through only when the user is bulk-excluding a
    // folder-filter result — plain row-selection excludes send only ids.
    result = await exclusionService.exclude(idSet, audit);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = oldTxt;
    showToast('Exclusion failed. Please try again.');
    return;
  }

  const succeededSet = new Set(result.succeeded);

  // Snapshot the excluded rows BEFORE removing them from state.allData so we
  // can prepend them to state.excludedData with a client-side excludedDate.
  // The Excluded view will still refetch from the server the next time it's
  // opened, keeping the DB as the source of truth.
  const movedRows = state.allData
    .filter(r => succeededSet.has(r.fileID))
    .map(r => ({ ...r, excludedDate: toUsDateTime(new Date()) }));

  // Remove from active dataset so they disappear from the review list.
  state.allData = state.allData.filter(r => !succeededSet.has(r.fileID));

  // Drop from selection so Select-All etc. cannot re-target them.
  succeededSet.forEach(id => state.selectedIds.delete(id));

  // Update population counts client-side (server is already updated).
  // Total = ACTIVE count, so it decreases along with active on exclusion.
  state.populationCounts.active   = Math.max(0, state.populationCounts.active   - succeededSet.size);
  state.populationCounts.excluded = state.populationCounts.excluded + succeededSet.size;
  state.populationCounts.total    = state.populationCounts.active;

  // Prepend to in-memory excluded cache so the Excluded view reflects the
  // change immediately without a network round-trip.
  state.excludedData = movedRows.concat(state.excludedData);

  // Rebuild the folder tree so folder counts reflect the new active set.
  rebuildFolderTree();

  applyFiltersAndSort();
  renderAll();

  const n = result.succeeded.length;
  const f = result.failed.length;
  if (f > 0) {
    showToast(`${n} excluded successfully, ${f} failed.`);
  } else {
    showToast(`${n} file${n !== 1 ? 's' : ''} excluded successfully.`);
  }
}

/* ── Restoration (recover excluded rows) ─────────────────────────────────
 * Moves the given FileIDs from the ContractInventory_Excluded table back to
 * the active ContractInventory table.  Symmetric with exclusionService.
 * ────────────────────────────────────────────────────────────────────────── */
const restorationService = {
  async restore(ids) {
    const res = await fetch('/api/restore', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ ids: [...ids] }),
    });
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { const j = await res.json(); if (j.detail) msg = j.detail; } catch {}
      throw new Error(msg);
    }
    const json = await res.json();
    return {
      succeeded: Array.isArray(json.succeeded) ? json.succeeded : [...ids],
      failed:    Array.isArray(json.failed)    ? json.failed    : [],
    };
  },
};

/* ── Excluded view ─────────────────────────────────────────────────────────
 * Renders state.excludedData in its own table with a Restore action bar.
 * Reuses COL_CFG for column definitions but omits filter/sort UI — the
 * excluded list is small and chronologically ordered by ExcludedDate DESC.
 * ────────────────────────────────────────────────────────────────────────── */

// Columns shown in the excluded table.  Deliberately a smaller, fixed subset
// so the recovery UI stays uncluttered; the DB row is intact and fully
// restored on Restore regardless of what's displayed here.
const EXCLUDED_COLS = [
  { f: 'fileName',      h: 'File Name',      w: 260 },
  { f: 'customerName',  h: 'Customer Name',  w: 170 },
  { f: 'agreementName', h: 'Agreement Name', w: 210 },
  { f: 'contractType',  h: 'Contract Type',  w: 120 },
  { f: 'migrate',       h: 'Migrate',        w: 80  },
  { f: 'excludedDate',  h: 'Excluded Date',  w: 180 },
  { f: 'fileID',        h: 'File ID',        w: 90  },
];

function renderExcludedTable() {
  const container = document.querySelector('#excluded-view .table-container');
  const empty     = $('excluded-empty');
  const rows      = state.excludedData || [];

  if (rows.length === 0) {
    if (container) container.style.display = 'none';
    if (empty) empty.style.display = '';
    // Clear header selection state
    return;
  }
  if (container) container.style.display = '';
  if (empty) empty.style.display = 'none';

  let thead = '<thead><tr>';
  thead += `<th class="col-rn col-frozen col-frozen-0 cell-rn" style="width:40px;min-width:40px;max-width:40px">#</th>`;
  thead += `<th class="col-cb col-frozen col-frozen-1 th-cb-hdr" style="width:44px;min-width:44px;max-width:44px;" title="Select all rows"><input type="checkbox" id="excluded-hdr-check"></th>`;

  EXCLUDED_COLS.forEach((col, idx) => {
    const isFirst = idx === 0;
    const frozen = isFirst ? 'col-frozen col-frozen-2' : '';
    const frozenStyle = isFirst ? 'position:sticky;left:84px;z-index:15;background:#eaeff5;' : '';
    thead += `<th class="${frozen}" style="${frozenStyle}width:${col.w}px;min-width:${col.w}px;max-width:${col.w}px">
      <div class="th-inner"><span class="th-label">${esc(col.h)}</span></div>
    </th>`;
  });
  thead += '</tr></thead>';

  let tbody = '<tbody>';
  rows.forEach((row, i) => {
    const isSelected = state.excludedSelectedIds.has(row.fileID);
    const classes = isSelected ? 'row-selected' : '';
    tbody += `<tr class="${classes}" data-id="${esc(row.fileID)}">`;
    tbody += `<td class="col-rn col-frozen col-frozen-0 cell-rn" style="width:40px;min-width:40px;max-width:40px">${i + 1}</td>`;
    tbody += `<td class="col-cb col-frozen col-frozen-1 cell-cb" style="width:44px;min-width:44px;max-width:44px"><input type="checkbox" class="excluded-row-check" data-id="${esc(row.fileID)}" ${isSelected ? 'checked' : ''}></td>`;

    EXCLUDED_COLS.forEach((col, idx) => {
      const isFirst = idx === 0;
      // See note in renderTable() — no inline background; .col-frozen CSS
      // supplies opaque per-state background-color so sticky cells occlude
      // scrolled content instead of letting it bleed through.
      const frozenStyle = isFirst ? 'position:sticky;left:84px;z-index:10;' : '';
      const frozen = isFirst ? 'col-frozen col-frozen-2' : '';
      // Reuse renderCell for consistency — it handles SharePoint links, dates,
      // status badges, etc. For fields not in COL_CFG (excludedDate) fall back
      // to raw value.
      const cfg = COL_CFG.find(c => c.f === col.f);
      const cellHtml = cfg
        ? renderCell(row, cfg)
        : esc(row[col.f] == null ? '' : String(row[col.f]));
      tbody += `<td class="${frozen}" style="${frozenStyle}width:${col.w}px;min-width:${col.w}px;max-width:${col.w}px;overflow:hidden;">${cellHtml}</td>`;
    });
    tbody += '</tr>';
  });
  tbody += '</tbody>';

  $('excluded-table-root').innerHTML = thead + tbody;
  attachExcludedTableEvents();
  updateExcludedHdrCheckbox();
}

function attachExcludedTableEvents() {
  const hdr = document.getElementById('excluded-hdr-check');
  if (hdr) {
    hdr.addEventListener('change', () => {
      const ids = state.excludedData.map(r => r.fileID);
      if (hdr.checked) ids.forEach(id => state.excludedSelectedIds.add(id));
      else ids.forEach(id => state.excludedSelectedIds.delete(id));
      renderExcludedTable();
      updateExcludedToolbar();
      updateBuckets();
    });
  }

  document.querySelectorAll('.excluded-row-check').forEach(cb => {
    cb.addEventListener('change', () => {
      const id = cb.dataset.id;
      if (cb.checked) state.excludedSelectedIds.add(id);
      else state.excludedSelectedIds.delete(id);
      updateExcludedHdrCheckbox();
      updateExcludedToolbar();
      updateBuckets();
    });
  });

  document.querySelectorAll('#excluded-table-root tbody tr[data-id]').forEach(tr => {
    tr.addEventListener('click', e => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'A') return;
      const id = tr.dataset.id;
      if (state.excludedSelectedIds.has(id)) state.excludedSelectedIds.delete(id);
      else state.excludedSelectedIds.add(id);
      renderExcludedTable();
      updateExcludedToolbar();
      updateBuckets();
    });
  });
}

function updateExcludedHdrCheckbox() {
  const hdr = document.getElementById('excluded-hdr-check');
  if (!hdr) return;
  const ids = state.excludedData.map(r => r.fileID);
  const allSelected = ids.length > 0 && ids.every(id => state.excludedSelectedIds.has(id));
  const someSelected = ids.some(id => state.excludedSelectedIds.has(id));
  hdr.checked = allSelected;
  hdr.indeterminate = !allSelected && someSelected;
  hdr.disabled = ids.length === 0;
}

function updateExcludedToolbar() {
  const sel = state.excludedSelectedIds.size;
  const sc = $('excluded-sel-count');
  if (sc) {
    if (sel === 0) {
      sc.textContent = 'No files selected';
      sc.classList.remove('has-selection');
    } else {
      sc.textContent = `${sel} file${sel !== 1 ? 's' : ''} selected`;
      sc.classList.add('has-selection');
    }
  }
  const btn = $('restore-btn');
  if (btn) {
    btn.textContent = sel > 0 ? `Restore (${sel})` : 'Restore';
    btn.disabled = sel === 0;
    btn.className = `action-restore-btn${sel > 0 ? ' active' : ''}`;
  }
}

/* ── View switching ────────────────────────────────────────────────────── */
async function showExcludedView() {
  state.currentView = 'excluded';

  // Hide the active review pieces (table + toolbar + action bar + filter chips)
  document.querySelector('#screen-review .toolbar').style.display     = 'none';
  document.querySelector('#screen-review .grid-wrap').style.display   = 'none';
  document.querySelector('#screen-review .action-bar#action-bar').style.display = 'none';
  const afBar = $('af-bar');
  if (afBar) afBar._prevDisplay = afBar.style.display, afBar.style.display = 'none';

  // Show the excluded view
  $('excluded-view').style.display = '';

  // Fetch on first open (or refresh if the cache is empty but server says
  // there are excluded rows — e.g. after a hard reload).
  if (!state.excludedLoaded || (state.excludedData.length === 0 && state.populationCounts.excluded > 0)) {
    try {
      const res = await fetch('/api/excluded', { cache: 'no-store' });
      if (res.ok) {
        const json = await res.json();
        state.excludedData = Array.isArray(json.data) ? json.data : [];
        state.excludedLoaded = true;
      }
    } catch (err) {
      showToast('Unable to load excluded documents.');
    }
  }

  renderExcludedTable();
  updateExcludedToolbar();
  updateBuckets();
}

function showReviewView() {
  state.currentView = 'review';
  $('excluded-view').style.display = 'none';

  document.querySelector('#screen-review .toolbar').style.display     = '';
  document.querySelector('#screen-review .grid-wrap').style.display   = '';
  document.querySelector('#screen-review .action-bar#action-bar').style.display = '';
  const afBar = $('af-bar');
  if (afBar && afBar._prevDisplay !== undefined) {
    afBar.style.display = afBar._prevDisplay;
    delete afBar._prevDisplay;
  }

  renderAll();
}

/* ── Restore modal + action ───────────────────────────────────────────── */
function openRestoreModal() {
  const ids = [...state.excludedSelectedIds];
  const n = ids.length;
  if (n === 0) return;

  const html = `
    <div class="modal-overlay" id="modal-overlay">
      <div class="modal">
        <div class="modal-header">
          <span class="modal-title">Restore Selected Files</span>
        </div>
        <div class="modal-body">
          You are about to restore <strong>${n}</strong> file${n !== 1 ? 's' : ''} to the active Manual Review list.
          <div class="modal-note">
            These files will be moved back to the active list and become
            eligible for migration again. Their original data is preserved.
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn-cancel-modal" id="modal-cancel">Cancel</button>
          <button class="btn-confirm-modal btn-restore" id="modal-confirm">Restore ${n} File${n !== 1 ? 's' : ''}</button>
        </div>
      </div>
    </div>`;

  const mount = document.createElement('div');
  mount.id = 'modal-mount';
  document.body.appendChild(mount);
  mount.innerHTML = html;

  const close = () => mount.remove();
  $('modal-cancel').addEventListener('click', close);
  $('modal-overlay').addEventListener('click', e => { if (e.target === $('modal-overlay')) close(); });
  $('modal-confirm').addEventListener('click', async () => {
    close();
    await performRestoration(ids);
  });
}

async function performRestoration(ids) {
  const idSet = new Set(ids);
  if (idSet.size === 0) return;

  const btn = $('restore-btn');
  const oldTxt = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Restoring…'; }

  let result;
  try {
    result = await restorationService.restore(idSet);
  } catch (err) {
    if (btn) { btn.disabled = false; btn.textContent = oldTxt; }
    showToast('Restore failed. Please try again.');
    return;
  }

  try {
    const succeededSet = new Set(result.succeeded);

    // Move restored rows back into state.allData so they show in the active
    // review immediately without a full re-fetch.
    const restoredRows = state.excludedData
      .filter(r => succeededSet.has(r.fileID))
      .map(r => {
        const { excludedDate, excludedBy, ...rest } = r;
        return normaliseDatesInRow({
          ...rest,
          migrate:         (rest.migrate === 'Yes' || rest.migrate === true) ? 'Yes' : 'No',
          migratedDate:    rest.migratedDate || '',
          migrationStatus: rest.migrationStatus || (rest.migrate === 'Yes' ? STATUS.MIGRATED : STATUS.PENDING),
        });
      });

    // Remove from excluded cache + selection
    state.excludedData = state.excludedData.filter(r => !succeededSet.has(r.fileID));
    succeededSet.forEach(id => state.excludedSelectedIds.delete(id));

    // Prepend to active dataset so the user sees them without hunting
    state.allData = restoredRows.concat(state.allData);

    // Update population counts (server already updated).
    // Total = ACTIVE count, so it grows along with active on restore.
    state.populationCounts.active   = state.populationCounts.active   + succeededSet.size;
    state.populationCounts.excluded = Math.max(0, state.populationCounts.excluded - succeededSet.size);
    state.populationCounts.total    = state.populationCounts.active;

    // Rebuild folder tree so restored rows appear in the folder counts.
    rebuildFolderTree();

    applyFiltersAndSort();
    renderExcludedTable();
    updateExcludedToolbar();
    updateBuckets();

    const n = result.succeeded.length;
    const f = result.failed.length;
    if (f > 0) {
      showToast(`${n} restored successfully, ${f} failed.`);
    } else {
      showToast(`${n} file${n !== 1 ? 's' : ''} restored successfully.`);
    }
  } catch (err) {
    console.error('[restore] post-processing failed', err);
    if (btn) { btn.disabled = false; btn.textContent = oldTxt; }
    showToast('Restore succeeded but UI update failed. Please reload.');
  }
}

/* ── Toast ──────────────────────────────────────────────────────────────── */
function showToast(msg) {
  const t = document.createElement('div');
  t.className = 'toast';
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

/* ── Live migration progress badge ─────────────────────────────────────────
 *
 * Sticky pill top-right that follows a single submission cohort from
 * click → completion.  Replaces the misleading "N files submitted
 * (M migrations)" toast which users read as "M migrated".
 *
 * Cohort = the set of FileIDs returned in the /api/migrate `inProcessing`
 * array PLUS any that failed at submit.  We do NOT track background
 * migrations kicked off by other users / earlier sessions — those still
 * flow through the KPI counters normally.
 *
 * States (single-line title, styled left border):
 *   submitting  ⟳  "Submitting N files…"
 *   progress    ⟳  "Migrating N files"    sub: "X done · Y in progress · Z failed"
 *   success     ✓  "N of N files migrated"
 *   partial     ⚠  "X done · Y failed of N"
 *   failed      ✕  "All N files failed to migrate"
 *
 * Auto-hide: success → fades after 8s; partial/failed stay sticky with ×.
 * A brand-new click while a badge is showing resets it to the new cohort.
 * ─────────────────────────────────────────────────────────────────────── */
const migProgress = (() => {
  // Live migration-progress pill was removed at user request.  All public
  // methods are retained as no-ops so existing call sites (Migrate button,
  // /api/migrations/sync poller) continue to work without any changes.
  //
  // Row-level statuses in the main table (Pending → In Processing → Migrated
  // / Failed) are still updated by the sync poller as usual — only the
  // top-right floating pill has been suppressed.
  //
  // Belt-and-braces: also force-hide the pill element in the DOM in case a
  // previous session left the `is-visible` class on it.
  document.addEventListener('DOMContentLoaded', () => {
    const e = document.getElementById('mig-progress');
    if (e) e.classList.remove('is-visible');
  });

  function startSubmitting(_nFiles) { /* no-op */ }
  function onSubmitted(_result)     { /* no-op */ }
  function tick()                   { /* no-op */ }
  function hide()                   { /* no-op */ }

  return { startSubmitting, onSubmitted, tick, hide };
})();

/* ── Screen management ─────────────────────────────────────────────────── */
function showScreen(name) {
  $('screen-source').style.display = name === 'source' ? 'flex' : 'none';
  $('screen-review').style.display = name === 'review' ? 'flex' : 'none';
}

/* ── Source Data screen ────────────────────────────────────────────────────
 * Single action button.  Click Start → fetch documents → on success go
 * directly to the Manual Review page (no intermediate "N documents found"
 * step, no Next button).  All existing DB fetch logic is preserved; only
 * the navigation gate between fetch success and Manual Review is removed.
 * ─────────────────────────────────────────────────────────────────────── */
function enterReviewScreen() {
  // Use the data already retrieved by Start — do NOT re-fetch.
  state.allData = state.sourceData.map(r => {
    const row = {
      ...r,
      migrate:         (r.migrate === 'Yes' || r.migrate === true) ? 'Yes' : 'No',
      migratedDate:    r.migratedDate || '',
      // migrationStatus is authoritative for row state.  Fall back to
      // Pending if the server didn't populate it (e.g. very old row).
      migrationStatus: r.migrationStatus || (r.migrate === 'Yes' ? STATUS.MIGRATED : STATUS.PENDING),
    };
    // Normalise every date column to zero-padded US format (MM/DD/YYYY)
    // for consistent display.  Filter/sort still work because parseDate()
    // accepts both padded and non-padded variants.
    return normaliseDatesInRow(row);
  });

  state.columnFilters = {};
  state.globalSearch  = '';
  state.sortCol       = null;
  state.sortDir       = null;
  state.selectedIds.clear();
  state.bucketFilter  = 'all';
  state.folderPath    = [];
  state.page = 1;

  // Build the folder tree ONCE from state.allData; exclude/restore keep
  // it in sync by calling rebuildFolderTree() after mutating allData.
  rebuildFolderTree();

  applyFiltersAndSort();
  showScreen('review');
  $('table-root').closest('.table-container').style.display = '';
  renderAll();

  // Resume polling if we landed on a page where files are already In
  // Processing (browser refresh mid-migration, or another user submitted
  // some rows earlier).  Poller is idempotent so double-calling is safe.
  if ((state.populationCounts.in_processing || 0) > 0) {
    migrationPoller.start();
  }
}

function initSourceScreen() {
  const startBtn  = $('source-start-btn');
  const labelEl   = startBtn.querySelector('.source-start-label');
  const errorEl   = $('source-error');

  startBtn.addEventListener('click', async () => {
    // Guard against duplicate requests
    if (startBtn.disabled) return;
    startBtn.disabled = true;
    startBtn.classList.add('is-loading');
    labelEl.textContent = 'Fetching documents…';
    errorEl.style.display = 'none';

    try {
      const res = await fetch('/api/contracts', { cache: 'no-store', credentials: 'same-origin' });
      if (res.status === 401) {
        // Session expired between load and Start — bounce to login.
        window.location.assign('/login');
        return;
      }
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const json = await res.json();

      state.sourceData  = Array.isArray(json.data) ? json.data : [];
      state.sourceCount = typeof json.total === 'number' ? json.total : state.sourceData.length;

      // Capture destination config for the Migrate-confirm modal preview.
      // Server echoes MIGRATION_DEST_* env vars — no secrets included.
      if (json.config && typeof json.config === 'object') {
        state.migrationConfig = {
          destSiteUrl:    String(json.config.destSiteUrl    || ''),
          destLibrary:    String(json.config.destLibrary    || ''),
          destFolderPath: String(json.config.destFolderPath || ''),
        };
      }

      // Capture server-authoritative population counts so the Manual Review
      // KPI buckets reflect the *entire* population (active + excluded).
      if (json.counts && typeof json.counts === 'object') {
        state.populationCounts = {
          active:        Number(json.counts.active)        || state.sourceData.length,
          excluded:      Number(json.counts.excluded)      || 0,
          total:         Number(json.counts.total)         || state.sourceData.length,
          pending:       Number(json.counts.pending)       || 0,
          in_processing: Number(json.counts.in_processing) || 0,
          migrated:      Number(json.counts.migrated)      || 0,
          failed:        Number(json.counts.failed)        || 0,
          // Per-user workload-lock fields (task 2026-09-24).  Present
          // on every authenticated /api/contracts response — used to
          // paint the correct Migrate button state on first render
          // WITHOUT waiting for the sync poller's first tick.
          my_in_processing: Number(json.counts.my_in_processing) || 0,
          my_active_total:  Number(json.counts.my_active_total)  || 0,
          can_migrate:      (json.counts.can_migrate !== false),
        };
      } else {
        state.populationCounts = {
          active:        state.sourceData.length,
          excluded:      0,
          total:         state.sourceData.length,
          pending:       0,
          in_processing: 0,
          migrated:      0,
          failed:        0,
          my_in_processing: 0,
          my_active_total:  0,
          can_migrate:      true,
        };
      }

      // Success → go straight to Manual Review.  The button label is reset
      // by the finally block below so a future re-entry (via reload) starts
      // clean.  On failure we stay on this page and let the user retry.
      enterReviewScreen();
    } catch (err) {
      errorEl.textContent = `Unable to load source documents: ${err.message}`;
      errorEl.style.display = '';
    } finally {
      startBtn.disabled = false;
      startBtn.classList.remove('is-loading');
      labelEl.textContent = 'Start';
    }
  });
}

/* ── CSV Export ─────────────────────────────────────────────────────────
 * Exports whichever dataset the user is currently looking at:
 *
 *   currentView = 'review'   → state.filteredData
 *                               = state.allData
 *                                 ↓ folder scope + folder filter
 *                                 ↓ bucket filter (all / pending /
 *                                                   in_processing /
 *                                                   migrated / selected)
 *                                 ↓ global search
 *                                 ↓ column filters
 *                                 ↓ current sort
 *   currentView = 'excluded' → state.excludedData   (the excluded bucket)
 *
 * This is the SAME dataset applyFiltersAndSort() feeds the table, so what
 * the user sees on-screen and what lands in the CSV always match.  It is
 * NOT the current page — the whole matching dataset is exported.
 *
 * Excel-safe escaping (RFC 4180): fields containing ", commas or newlines
 * are wrapped in "…" with embedded quotes doubled.
 * ─────────────────────────────────────────────────────────────────────── */
function csvEscape(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'boolean') return v ? 'True' : 'False';
  const s = String(v);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

// Human-readable slug for the CSV filename, derived from whichever bucket
// the user has selected.  Kept in sync with the tile labels in index.html.
function _bucketSlugForExport() {
  if (state.currentView === 'excluded') return 'excluded';
  switch (state.bucketFilter) {
    case 'migrated':      return 'migrated';
    case 'in_processing': return 'in_processing';
    case 'pending':       return 'yet_to_be_migrated';
    case 'selected':      return 'selected';
    case 'all':
    default:              return 'all';
  }
}

async function exportCsv() {
  // Pick the dataset the user is currently looking at.  Three cases:
  //
  //   1. Excluded view          → state.excludedData (all excluded rows).
  //   2. Review, bucket=all     → server union of active + excluded
  //                               (fetched on demand with
  //                               include_excluded=1) so the Total
  //                               Documents export actually contains
  //                               every row on the master inventory,
  //                               not just the active subset the review
  //                               grid renders.  Current search + folder
  //                               scope + column filters are re-applied
  //                               to the union so the exported set still
  //                               reflects the user's UI state.
  //   3. Review, other buckets  → state.filteredData (already correct —
  //                               Pending / In Processing / Migrated
  //                               are all subsets of the active set).
  let rows;
  if (state.currentView === 'excluded') {
    rows = state.excludedData || [];
  } else if (state.bucketFilter === 'all') {
    try {
      const res = await fetch('/api/contracts?include_excluded=1',
                              { cache: 'no-store', credentials: 'same-origin' });
      if (res.status === 401) { window.location.assign('/login'); return; }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      const fetched = Array.isArray(json.data) ? json.data : [];
      // Re-apply the user's current filters (folder scope, folder filter,
      // global search, column filters, sort) to the union by temporarily
      // swapping state.allData through applyFiltersAndSort().  We restore
      // it (and state.filteredData / state.page) immediately so no render
      // is triggered against the swapped set.
      const savedAll      = state.allData;
      const savedFiltered = state.filteredData;
      const savedPage     = state.page;
      try {
        state.allData = fetched.map(r => normaliseDatesInRow({ ...r }));
        applyFiltersAndSort();
        rows = state.filteredData || [];
      } finally {
        state.allData      = savedAll;
        state.filteredData = savedFiltered;
        state.page         = savedPage;
      }
    } catch (err) {
      console.error('Total export fetch failed:', err);
      showToast('Export failed — could not fetch full inventory.');
      return;
    }
  } else {
    rows = state.filteredData || [];
  }

  // Use ALL configured data columns (not just currently visible) so the export
  // is a complete record, and always in a stable order.  Skips UI-only fields
  // like row-number and checkbox (those aren't in COL_CFG).
  const cols = COL_CFG.slice();

  const header = cols.map(c => csvEscape(c.h)).join(',');
  const body   = rows.map(r =>
    cols.map(c => csvEscape(r[c.f])).join(',')
  ).join('\r\n');

  // Prepend UTF-8 BOM so Excel opens accented / non-ASCII characters correctly.
  const csv  = '\uFEFF' + header + '\r\n' + body;
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });

  // Filename reflects the currently-selected bucket per manager spec:
  //   contract_inventory_all.csv
  //   contract_inventory_yet_to_be_migrated.csv
  //   contract_inventory_in_processing.csv
  //   contract_inventory_migrated.csv
  //   contract_inventory_excluded.csv
  // Timestamp appended (existing behaviour) so multiple exports don't clash.
  const d = new Date();
  const stamp = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  const filename = `contract_inventory_${_bucketSlugForExport()}_${stamp}.csv`;

  const url  = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  setTimeout(() => URL.revokeObjectURL(url), 1000);

  showToast(`Exported ${rows.length} record${rows.length !== 1 ? 's' : ''}.`);
}

/* ── Review screen event listeners ─────────────────────────────────────── */
function initReviewEventListeners() {
  $('search-input').addEventListener('input', () => {
    state.globalSearch = $('search-input').value;
    state.page = 1;
    applyFiltersAndSort();
    renderAll();
  });

  $('search-clear').addEventListener('click', () => {
    state.globalSearch = '';
    $('search-input').value = '';
    applyFiltersAndSort();
    renderAll();
  });

  $('clear-filters-btn').addEventListener('click', () => {
    state.columnFilters = {};
    state.globalSearch = '';
    $('search-input').value = '';
    state.sortCol = null;
    state.sortDir = null;
    state.bucketFilter = 'all';
    state.folderPath = [];                 // reset folder scope to All Contracts
    // Also drop the global folder filter (§15 spec — Clear Filters must
    // reset the new folder filter along with everything else).
    state.folderFilter = { text: '', mode: 'contains' };
    _syncFolderFilterButton();
    applyFiltersAndSort();
    renderAll();
  });

  $('col-btn').addEventListener('click', openColPanel);
  $('migrate-btn').addEventListener('click', openMigrateModal);
  $('exclude-btn').addEventListener('click', openExcludeModal);
  $('export-csv-btn').addEventListener('click', exportCsv);
  // Same handler for the Export CSV button in the Excluded view header.
  // exportCsv() inspects state.currentView and exports state.excludedData
  // when the user is looking at Excluded — so "Excluded selected → export
  // only excluded records" works without a second export implementation.
  const exclExportBtn = $('excluded-export-csv-btn');
  if (exclExportBtn) exclExportBtn.addEventListener('click', exportCsv);
  // (Removed) "All Contracts" folder navigator button + dropdown wiring —
  // the hierarchical folder browser was retired per manager request.
  // Global folder filter (Philippe requirement) — searches Folder 1..20
  // across the entire inventory.
  const folderFilterBtn = $('folder-filter-btn');
  if (folderFilterBtn) folderFilterBtn.addEventListener('click', e => {
    e.stopPropagation();
    if (document.getElementById('folder-filter-panel')) closeFolderFilterPanel();
    else openFolderFilterPanel();
  });

  // Restore button (Excluded view).  Users return to the active list by
  // clicking any non-Excluded bucket (Total / Pending / In Processing /
  // Migrated) in the header — no dedicated back button.
  const restoreBtn = $('restore-btn');
  if (restoreBtn) restoreBtn.addEventListener('click', openRestoreModal);

  // Summary bucket clicks — apply migration-status filter over the dataset.
  // The Excluded bucket is special: it switches to a separate view instead
  // of applying a filter to the active table.
  document.querySelectorAll('.bucket').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.disabled) return;
      const next = btn.dataset.bucket;

      if (next === 'excluded') {
        // Toggle excluded view on/off
        if (state.currentView === 'excluded') {
          showReviewView();
        } else {
          showExcludedView();
        }
        return;
      }

      // Any other bucket click switches back to review first
      if (state.currentView === 'excluded') {
        state.bucketFilter = next;
        state.page = 1;
        applyFiltersAndSort();
        showReviewView();
        return;
      }

      state.bucketFilter = (state.bucketFilter === next && next !== 'all') ? 'all' : next;
      state.page = 1;
      applyFiltersAndSort();
      renderAll();
    });
  });

  // Page size is fixed at 100; no rows-per-page dropdown.
}

/* ── Session badge + logout ────────────────────────────────────────────────
 * On load, ask /api/auth/me who's logged in and paint the email into the
 * header.  If the server says 401 (session expired), redirect to /login.
 * Logout POSTs to /api/auth/logout and then redirects to /login. */
async function initSessionBadge() {
  const wrap    = document.getElementById('app-user');
  const emailEl = document.getElementById('app-user-email');
  const btn     = document.getElementById('app-user-logout');
  if (!wrap || !emailEl || !btn) return;

  try {
    const res = await fetch('/api/auth/me', { credentials: 'same-origin', cache: 'no-store' });
    if (res.status === 401) {
      window.location.assign('/login');
      return;
    }
    if (!res.ok) return;
    const body = await res.json();
    if (body && typeof body.email === 'string' && body.email) {
      emailEl.textContent = body.email;
      emailEl.title       = body.email;
      wrap.style.display  = '';
    }
  } catch (_) {
    /* leave the badge hidden — non-fatal */
  }

  btn.addEventListener('click', async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      await fetch('/api/auth/logout', {
        method: 'POST',
        credentials: 'same-origin',
      });
    } catch (_) { /* proceed to redirect anyway */ }
    window.location.assign('/login');
  });
}

/* ── Bootstrap ──────────────────────────────────────────────────────────── */
function init() {
  initSessionBadge();
  initSourceScreen();
  initReviewEventListeners();
  showScreen('source');
}

document.addEventListener('DOMContentLoaded', init);
