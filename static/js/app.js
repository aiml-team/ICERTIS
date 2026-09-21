/* ── Column definitions ─────────────────────────────────────────────────── */
const COL_CFG = [
  { f: 'fileName',               h: 'File Name',           ft: 'text', w: 260 },
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
  // migration-status bucket filter: 'all' | 'migrated' | 'pending' | 'selected'
  // Note: 'excluded' is NOT a filter — clicking that bucket switches to a
  // separate view (state.currentView = 'excluded').
  bucketFilter:     'all',
  // records fetched from the database via /api/contracts on Start
  sourceData:       [],
  sourceCount:      0,

  /* ── Excluded documents (recoverable soft-delete) ─────────────────────
   * Rows live in the ContractInventory_Excluded table server-side; the UI
   * fetches them on demand via /api/excluded and lets the user Restore
   * them back into the active list.
   * ─────────────────────────────────────────────────────────────────── */
  currentView:        'review',   // 'review' | 'excluded'
  excludedData:       [],
  excludedSelectedIds: new Set(),
  excludedPage:       1,
  excludedLoaded:     false,      // set true after first /api/excluded fetch
  // Server-authoritative population counts (from /api/contracts.counts).
  // Total Documents bucket shows the ACTIVE count only (excluded rows are
  // reported separately via `excluded`).  Kept in sync with state.allData
  // by exclude/restore flows so a reload without a refetch stays correct.
  populationCounts:   { active: 0, excluded: 0, total: 0 },
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

function isMigrated(row) { return row.migrate === 'Yes'; }
function isPending(row)  { return !isMigrated(row); }

function applyFiltersAndSort() {
  let d = state.allData;

  // Bucket filter (migration status): applied first so column filters still narrow further
  if (state.bucketFilter === 'migrated')      d = d.filter(isMigrated);
  else if (state.bucketFilter === 'pending')  d = d.filter(isPending);
  else if (state.bucketFilter === 'selected') d = d.filter(r => state.selectedIds.has(r.fileID));

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

function renderStatusBadge(val, field) {
  if (val === null || val === undefined || val === '') return '<span class="cell-empty">—</span>';

  if (field === 'extractionStatus') {
    const cls = { Success: 'status-success', Failed: 'status-failed', Partial: 'status-partial' }[val] || '';
    return `<span class="status-badge ${cls}">${esc(val)}</span>`;
  }
  if (field === 'reviewRequired') {
    if (val === true || val === 'True') return '<span class="status-badge status-yes">Yes</span>';
    return '<span class="status-no">No</span>';
  }
  if (field === 'migrate') {
    if (val === true || val === 'True' || val === 'Yes') return '<span class="status-badge status-migrate-yes">Yes</span>';
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
    if (path && path.startsWith('http')) {
      return `<a class="cell-link" href="${esc(path)}" target="_blank" rel="noopener noreferrer" title="${esc(str)}">${display} <span class="cell-link-icon">&#8599;</span></a>`;
    }
    return `<span title="${esc(str)}">${display}</span>`;
  }

  if (col.f === 'missingFields' && str) {
    const parts = str.split(',').map(s => s.trim()).filter(Boolean);
    if (!parts.length) return '<span class="cell-empty">—</span>';
    const chips = parts.slice(0, 3).map(p => `<span class="mf-chip">${esc(p)}</span>`).join('');
    const extra = parts.length > 3 ? `<span class="mf-chip" title="${esc(str)}">+${parts.length - 3}</span>` : '';
    return `<span title="${esc(str)}">${chips}${extra}</span>`;
  }

  if (col.ft === 'bool' || col.f === 'reviewRequired' || col.f === 'migrated' || col.f === 'migrate') {
    return renderStatusBadge(val, col.f);
  }

  if (col.f === 'extractionStatus') {
    return renderStatusBadge(val, col.f);
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
      return `<a class="cell-link" href="${esc(href)}" target="_blank" rel="noopener noreferrer" title="${esc(str)}" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block">${esc(str)}</a>`;
    }
    // Fall through for non-URL values so they render as plain truncated text.
  }

  const longFields = ['sharePointPath','errorMessage','agreementName','associatedMSAFileName','associatedNDAFileName','runId'];
  if (longFields.includes(col.f)) {
    return `<span title="${esc(str)}" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block">${esc(str)}</span>`;
  }

  return `<span title="${esc(str)}">${esc(str)}</span>`;
}

/* ── Table render ───────────────────────────────────────────────────────── */
function getVisibleCols() {
  return COL_CFG.filter(c => !state.hiddenCols.has(c.f));
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
      const isSelected = state.selectedIds.has(row.fileID);
      const isMigrated = row.migrate === 'Yes';
      const classes = [isSelected ? 'row-selected' : '', isMigrated ? 'row-migrated' : ''].filter(Boolean).join(' ');

      tbody += `<tr class="${classes}" data-id="${esc(row.fileID)}" ${isMigrated ? 'data-migrated="1"' : ''}>`;
      tbody += `<td class="col-rn col-frozen col-frozen-0 cell-rn" style="width:40px;min-width:40px;max-width:40px">${globalIdx + 1}</td>`;
      const cbDisabled = isMigrated ? 'disabled title="Already migrated"' : '';
      tbody += `<td class="col-cb col-frozen col-frozen-1 cell-cb" style="width:44px;min-width:44px;max-width:44px"${isMigrated ? ' title="Already migrated"' : ''}><input type="checkbox" class="row-check" data-id="${esc(row.fileID)}" ${isSelected ? 'checked' : ''} ${cbDisabled}></td>`;

      visibleCols.forEach((col, idx) => {
        const w = state.colWidths[col.f] || col.w;
        const isFirst = idx === 0;
        const frozenStyle = isFirst ? 'position:sticky;left:84px;z-index:10;background:inherit;' : '';
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

// Only pending (not-yet-migrated) rows on the current page are eligible for
// the master checkbox / row-click selection.
function getEligiblePageIds() {
  const start = (state.page - 1) * state.pageSize;
  return state.filteredData
    .slice(start, start + state.pageSize)
    .filter(isPending)
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
      // Don't allow row-click to toggle selection on already-migrated rows
      if (tr.dataset.migrated === '1') return;
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
  // Count only eligible (pending) documents among the currently selected set.
  // Anything already-migrated slipping through is defensively ignored.
  let sel = 0;
  state.selectedIds.forEach(id => {
    const row = state.allData.find(r => r.fileID === id);
    if (row && isPending(row)) sel++;
  });

  const btn = $('migrate-btn');
  btn.textContent = sel > 0 ? `Migrate (${sel})` : 'Migrate';
  btn.disabled = sel === 0;
  btn.className = `action-migrate-btn${sel > 0 ? ' active' : ''}`;

  // Exclude button — enabled only when the selection includes at least one
  // *pending* (not-yet-migrated) row.  Business rule: already-migrated
  // documents cannot be excluded (protects the migration audit trail).
  // The label shows the count that will actually be excluded on click.
  const excludeBtn = $('exclude-btn');
  if (excludeBtn) {
    let excludable = 0;
    state.selectedIds.forEach(id => {
      const row = state.allData.find(r => r.fileID === id);
      if (row && isPending(row)) excludable++;
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
                     || state.bucketFilter !== 'all';
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

  if (!chips.length) {
    bar.innerHTML = '';
    bar.style.display = 'none';
    return;
  }
  bar.style.display = 'flex';
  bar.innerHTML = `<span class="af-label">Filters:</span>${chips.join('')}<button class="af-clear-all" id="af-clear-all">Clear All</button>`;

  bar.querySelectorAll('.af-x').forEach(btn => {
    btn.addEventListener('click', () => {
      delete state.columnFilters[btn.dataset.field];
      applyFiltersAndSort();
      renderAll();
    });
  });
  $('af-clear-all').addEventListener('click', () => {
    state.columnFilters = {};
    state.globalSearch = '';
    $('search-input').value = '';
    state.bucketFilter = 'all';
    applyFiltersAndSort();
    renderAll();
  });
}

/* ── Summary buckets ────────────────────────────────────────────────────── */
function updateBuckets() {
  // Total Documents = ACTIVE documents currently available for review.
  // Excluded rows live in a separate table and are counted separately —
  // they are NOT added into Total.  (Pending + Migrated == Total always.)
  const active    = state.allData.length;
  const migrated  = state.allData.reduce((n, r) => n + (isMigrated(r) ? 1 : 0), 0);
  const pending   = active - migrated;
  const excluded  = (typeof state.populationCounts.excluded === 'number')
                      ? state.populationCounts.excluded
                      : (state.excludedData.length || 0);
  const total     = active;
  // Selected count depends on which view the user is looking at.
  const selected  = state.currentView === 'excluded'
                      ? state.excludedSelectedIds.size
                      : state.selectedIds.size;

  const setVal = (id, v) => { const el = $(id); if (el) el.textContent = v.toLocaleString(); };
  setVal('bucket-total',     total);
  setVal('bucket-migrated',  migrated);
  setVal('bucket-pending',   pending);
  setVal('bucket-excluded',  excluded);
  setVal('bucket-selected',  selected);

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

  // 'Selected' bucket only makes sense when something is selected
  const selBtn = document.querySelector('.bucket-selected');
  if (selBtn) {
    selBtn.disabled = selected === 0 && state.bucketFilter !== 'selected';
    // If user had 'selected' active but cleared all selections, fall back to 'all'
    if (selected === 0 && state.bucketFilter === 'selected') {
      state.bucketFilter = 'all';
      selBtn.disabled = true;
      applyFiltersAndSort();
    }
  }
}

/* ── Main render ────────────────────────────────────────────────────────── */
function renderAll() {
  renderTable();
  updateToolbar();
  updateFooter();
  renderActiveFilters();
  updateBuckets();
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

  const html = `
    <div class="colpanel-backdrop" id="colpanel-backdrop"></div>
    <div class="colpanel" id="colpanel">
      <div class="colpanel-head">Columns</div>
      ${COL_CFG.map(c => `
        <label class="colpanel-row">
          <input type="checkbox" class="col-toggle" data-field="${c.f}" ${!state.hiddenCols.has(c.f)?'checked':''}>
          ${esc(c.h)}
        </label>`).join('')}
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

  document.getElementById('colpanel-backdrop').addEventListener('click', closeColPanel);
}

function closeColPanel() {
  document.getElementById('colpanel-mount')?.remove();
}

/* ── Migration modal ────────────────────────────────────────────────────── */
function openMigrateModal() {
  // Only pending, currently-selected docs are actually going to be migrated
  const eligible = [...state.selectedIds].filter(id => {
    const row = state.allData.find(r => r.fileID === id);
    return row && isPending(row);
  });
  const n = eligible.length;
  if (n === 0) return;

  const html = `
    <div class="modal-overlay" id="modal-overlay">
      <div class="modal">
        <div class="modal-header">
          <span class="modal-title">Confirm Migration</span>
        </div>
        <div class="modal-body">
          <strong>${n}</strong> document${n !== 1 ? 's' : ''} will be migrated.
          <div class="modal-note">
            On success, each document's <b>Migrate</b> status will be set to <b>Yes</b>
            and <b>MigratedDate</b> will be stamped with the completion time. Already-migrated
            documents are skipped automatically.
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn-cancel-modal" id="modal-cancel">Cancel</button>
          <button class="btn-confirm-modal" id="modal-confirm">Confirm Migration</button>
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

/* ── Migration service ──────────────────────────────────────────────────────
 * Talks to POST /api/migrate which:
 *   1. Persists Migrate='Yes' + MigratedDate for the given FileIDs in Azure SQL
 *   2. Returns the authoritative timestamp actually written to the database
 *
 * Contract:
 *   input:  Array<fileID>  — ids to migrate
 *   output: Promise<{ succeeded: string[], failed: Array<{id, error}>, migratedAt: string }>
 *
 * Future SharePoint hook:
 *   Add a SharePoint call here BEFORE the /api/migrate POST (or replace
 *   the endpoint with one that fans out to SharePoint then updates the DB).
 * ────────────────────────────────────────────────────────────────────────── */
const migrationService = {
  async migrate(ids) {
    const res = await fetch('/api/migrate', {
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
      succeeded:  Array.isArray(json.succeeded) ? json.succeeded : [...ids],
      failed:     Array.isArray(json.failed)    ? json.failed    : [],
      migratedAt: json.migratedAt || formatMigrationTimestamp(new Date()),
    };
  },
};

// "09/18/2026 03:45:12 PM" — zero-padded US format, matches parseDate() rules.
// Kept as a thin wrapper around toUsDateTime() so the display format stays
// consistent everywhere (ingest + fresh-migration path).
function formatMigrationTimestamp(d) {
  return toUsDateTime(d);
}

async function performMigration(ids) {
  // Guard: never migrate already-migrated documents even if somehow selected.
  const eligibleIds = [...ids].filter(id => {
    const row = state.allData.find(r => r.fileID === id);
    return row && isPending(row);
  });
  if (!eligibleIds.length) {
    showToast('No eligible documents to migrate.');
    return;
  }

  // Disable the button while the async call is in flight
  const btn = $('migrate-btn');
  const oldTxt = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Migrating…';

  let result;
  try {
    result = await migrationService.migrate(eligibleIds);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = oldTxt;
    showToast('Migration failed. Please try again.');
    return;
  }

  const succeededSet = new Set(result.succeeded);
  // Ensure server-provided timestamp is displayed in the same zero-padded
  // US format as ingested rows.  Falls back to a locally-formatted "now"
  // if the server didn't include a value.
  const migratedAt   = toUsDateTime(result.migratedAt) || formatMigrationTimestamp(new Date());

  // Update ONLY the successful documents (fully isolated to the selected set)
  state.allData.forEach(r => {
    if (succeededSet.has(r.fileID)) {
      r.migrate = 'Yes';
      r.migratedDate = migratedAt;
    }
    // Failed ones: keep migrate='No' and migratedDate=''
    // (Contract already ensures this — no-op here.)
  });

  // Clear selection for migrated docs (leave any failed ones selected so the
  // user can retry). For the mock, failed is always empty → clears all.
  succeededSet.forEach(id => state.selectedIds.delete(id));

  applyFiltersAndSort();
  renderAll();

  const n = result.succeeded.length;
  const f = result.failed.length;
  if (f > 0) {
    showToast(`${n} migrated successfully, ${f} failed.`);
  } else {
    showToast(`${n} document${n !== 1 ? 's' : ''} migrated successfully.`);
  }
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
  async exclude(ids) {
    const res = await fetch('/api/exclude', {
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

  const html = `
    <div class="modal-overlay" id="modal-overlay">
      <div class="modal">
        <div class="modal-header">
          <span class="modal-title">Exclude Selected Files</span>
        </div>
        <div class="modal-body">
          You are about to exclude <strong>${n}</strong> selected file${n !== 1 ? 's' : ''} from the migration review list.
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
    await performExclusion(eligible);
  });
}

async function performExclusion(ids) {
  const idSet = new Set(ids);
  if (idSet.size === 0) return;

  const btn = $('exclude-btn');
  const oldTxt = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Excluding…';

  let result;
  try {
    result = await exclusionService.exclude(idSet);
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
      const frozenStyle = isFirst ? 'position:sticky;left:84px;z-index:10;background:inherit;' : '';
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
          migrate:      (rest.migrate === 'Yes' || rest.migrate === true) ? 'Yes' : 'No',
          migratedDate: rest.migratedDate || '',
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

/* ── Screen management ─────────────────────────────────────────────────── */
function showScreen(name) {
  $('screen-source').style.display = name === 'source' ? 'flex' : 'none';
  $('screen-review').style.display = name === 'review' ? 'flex' : 'none';
}

/* ── Source Data screen ────────────────────────────────────────────────────
 * One page, three inline actions:
 *   [ Start ]      ✓ N documents found      [ Next → ]
 * Start fetches from /api/contracts; Next uses the already-fetched dataset
 * (no re-fetch) and enters the Manual Review page.
 * ─────────────────────────────────────────────────────────────────────── */
function initSourceScreen() {
  const startBtn  = $('source-start-btn');
  const labelEl   = startBtn.querySelector('.source-start-label');
  const errorEl   = $('source-error');
  const resultEl  = $('source-result');
  const countEl   = $('source-result-count');
  const rLabelEl  = $('source-result-label');
  const nextBtn   = $('source-next-btn');

  startBtn.addEventListener('click', async () => {
    // Guard against duplicate requests
    if (startBtn.disabled) return;
    startBtn.disabled = true;
    startBtn.classList.add('is-loading');
    labelEl.textContent = 'Fetching documents…';
    errorEl.style.display = 'none';
    // Hide any prior result while a new fetch is running
    resultEl.hidden = true;
    nextBtn.hidden = true;

    try {
      const res = await fetch('/api/contracts', { cache: 'no-store' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const json = await res.json();

      state.sourceData  = Array.isArray(json.data) ? json.data : [];
      state.sourceCount = typeof json.total === 'number' ? json.total : state.sourceData.length;

      // Capture server-authoritative population counts so the Total and
      // Excluded buckets reflect the *entire* population (active + excluded)
      // even before the user opens the Excluded view.
      if (json.counts && typeof json.counts === 'object') {
        state.populationCounts = {
          active:   Number(json.counts.active)   || state.sourceData.length,
          excluded: Number(json.counts.excluded) || 0,
          total:    Number(json.counts.total)    || state.sourceData.length,
        };
      } else {
        state.populationCounts = {
          active:   state.sourceData.length,
          excluded: 0,
          total:    state.sourceData.length,
        };
      }

      countEl.textContent = state.sourceCount.toLocaleString();
      rLabelEl.textContent =
        state.sourceCount === 1 ? 'document found' : 'documents found';
      resultEl.hidden = false;
      nextBtn.hidden = false;
    } catch (err) {
      errorEl.textContent = `Unable to load source documents: ${err.message}`;
      errorEl.style.display = '';
    } finally {
      startBtn.disabled = false;
      startBtn.classList.remove('is-loading');
      labelEl.textContent = 'Start';
    }
  });

  nextBtn.addEventListener('click', () => {
    // Use the data already retrieved by Start — do NOT re-fetch.
    state.allData = state.sourceData.map(r => {
      const row = {
        ...r,
        migrate:      (r.migrate === 'Yes' || r.migrate === true) ? 'Yes' : 'No',
        migratedDate: r.migratedDate || '',
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
    state.page = 1;

    applyFiltersAndSort();
    showScreen('review');
    $('table-root').closest('.table-container').style.display = '';
    renderAll();
  });
}

/* ── CSV Export ─────────────────────────────────────────────────────────
 * Exports state.filteredData (i.e. the current search/filter/bucket result
 * set — NOT the current page) as a real .csv file.  Excel-safe escaping:
 * fields containing ", commas, or newlines are wrapped in "…" with any
 * embedded quotes doubled per RFC 4180.
 * ─────────────────────────────────────────────────────────────────────── */
function csvEscape(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'boolean') return v ? 'True' : 'False';
  const s = String(v);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function exportCsv() {
  const rows = state.filteredData || [];
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

  const d = new Date();
  const stamp = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  const filename = `contract_migration_review_${stamp}.csv`;

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
    applyFiltersAndSort();
    renderAll();
  });

  $('col-btn').addEventListener('click', openColPanel);
  $('migrate-btn').addEventListener('click', openMigrateModal);
  $('exclude-btn').addEventListener('click', openExcludeModal);
  $('export-csv-btn').addEventListener('click', exportCsv);

  // Restore + back-to-review buttons (Excluded view)
  const restoreBtn = $('restore-btn');
  if (restoreBtn) restoreBtn.addEventListener('click', openRestoreModal);
  const backBtn = $('excluded-back-btn');
  if (backBtn) backBtn.addEventListener('click', () => {
    // Return to review with the current bucket filter untouched.
    showReviewView();
  });

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

/* ── Bootstrap ──────────────────────────────────────────────────────────── */
function init() {
  initSourceScreen();
  initReviewEventListeners();
  showScreen('source');
}

document.addEventListener('DOMContentLoaded', init);
