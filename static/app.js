// ─── State ────────────────────────────────────────────────────────────────────
const state = {
  page: 1,
  ticker: "",
  dateFrom: "",
  dateTo: "",
  source: "all",
  signal: "all",
  totalRows: 0,
  unprocessed: 0,
  stopProcessing: false,
};

// ─── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
  await loadSettings();
  setupDragDrop();
  // Try loading data if a file was previously uploaded
  const res = await fetchJSON("/api/data?page=1");
  if (res && res.total > 0) {
    showDataUI(res);
    await refreshTable();
  }
});

// ─── Settings ─────────────────────────────────────────────────────────────────
async function loadSettings() {
  const s = await fetchJSON("/api/settings");
  if (!s) return;
  document.getElementById("apiKeyInput").value = "";
  document.getElementById("modelSelect").value = s.model || "claude-haiku-4-5";
  if (s.file_path) {
    showFileInfo(s.file_path);
  }
}

function openSettings() {
  new bootstrap.Modal(document.getElementById("settingsModal")).show();
}

async function saveSettings() {
  const key = document.getElementById("apiKeyInput").value.trim();
  const model = document.getElementById("modelSelect").value;
  const body = { model };
  if (key) body.api_key = key;
  await postJSON("/api/settings", body);
  bootstrap.Modal.getInstance(document.getElementById("settingsModal")).hide();
  showToast("Settings saved.", "success");
}

function toggleApiKeyVisibility() {
  const input = document.getElementById("apiKeyInput");
  const icon = document.getElementById("eyeIcon");
  if (input.type === "password") {
    input.type = "text";
    icon.className = "bi bi-eye-slash";
  } else {
    input.type = "password";
    icon.className = "bi bi-eye";
  }
}

// ─── File upload ──────────────────────────────────────────────────────────────
function setupDragDrop() {
  const zone = document.getElementById("uploadZone");
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drag-over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("drag-over");
    const file = e.dataTransfer.files[0];
    if (file) uploadFile(file);
  });
}

async function handleFileUpload(event) {
  const file = event.target.files[0];
  if (file) await uploadFile(file);
  event.target.value = ""; // reset so same file can be re-selected
}

async function uploadFile(file) {
  document.getElementById("uploadStatus").textContent = `Uploading ${file.name}…`;
  const form = new FormData();
  form.append("file", file);

  const res = await fetch("/api/upload", { method: "POST", body: form });
  const data = await res.json();

  if (data.error) {
    document.getElementById("uploadStatus").textContent = "Error: " + data.error;
    return;
  }

  document.getElementById("uploadStatus").textContent = "";
  showFileInfo(data.filename);
  openMappingModal(data.columns);
}

function showFileInfo(filename) {
  document.getElementById("fileInfoName").textContent = filename;
  document.getElementById("fileInfo").classList.remove("d-none");
}

// ─── Column mapping ───────────────────────────────────────────────────────────
let _uploadedColumns = [];

function openMappingModal(columns) {
  _uploadedColumns = columns;
  const FIELDS = ["mapHeadline", "mapTicker", "mapDate", "mapSource"];
  const HINTS = ["headline", "ticker", "date", "source"];

  FIELDS.forEach((id, i) => {
    const sel = document.getElementById(id);
    sel.innerHTML = `<option value="">(none)</option>` +
      columns.map(c => {
        const lc = c.toLowerCase();
        const selected = HINTS[i] && lc.includes(HINTS[i]) ? "selected" : "";
        return `<option value="${escHtml(c)}" ${selected}>${escHtml(c)}</option>`;
      }).join("");
  });

  new bootstrap.Modal(document.getElementById("mappingModal")).show();
}

async function saveMapping() {
  const mapping = {
    headline: document.getElementById("mapHeadline").value,
    ticker:   document.getElementById("mapTicker").value,
    date:     document.getElementById("mapDate").value,
    source:   document.getElementById("mapSource").value,
  };

  if (!mapping.headline || !mapping.ticker) {
    alert("Please map at least the Headline and Ticker columns.");
    return;
  }

  await postJSON("/api/settings", { column_mapping: mapping });
  bootstrap.Modal.getInstance(document.getElementById("mappingModal")).hide();
  state.page = 1;
  await refreshTable();
}

// ─── Filters & table ──────────────────────────────────────────────────────────
function applyFilters() {
  state.ticker   = document.getElementById("fTicker").value.trim();
  state.dateFrom = document.getElementById("fDateFrom").value;
  state.dateTo   = document.getElementById("fDateTo").value;
  state.source   = document.getElementById("fSource").value;
  state.signal   = document.getElementById("fSignal").value;
  state.page = 1;
  refreshTable();
}

async function refreshTable() {
  const params = new URLSearchParams({
    page:      state.page,
    ticker:    state.ticker,
    date_from: state.dateFrom,
    date_to:   state.dateTo,
    source:    state.source,
    signal:    state.signal,
  });
  const data = await fetchJSON("/api/data?" + params);
  if (!data) return;
  showDataUI(data);
  renderTable(data.rows);
  renderPagination(data.total, data.per_page);
  updateStats(data.total, data.unprocessed);
  populateSourceDropdown(data.sources);
}

function showDataUI(data) {
  document.getElementById("filterSection").style.removeProperty("display");
  document.getElementById("tableSection").style.removeProperty("display");
  document.getElementById("actionBar").classList.remove("d-none");
}

function renderTable(rows) {
  const tbody = document.getElementById("tableBody");
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="text-center text-muted py-4">No headlines match the current filters.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(r => {
    const badge = signalBadge(r.signal);
    const tip = r.reasoning ? `<span class="reasoning-tip ms-1" title="${escHtml(r.reasoning)}"><i class="bi bi-info-circle text-muted"></i></span>` : "";
    return `<tr>
      <td class="headline-cell">${escHtml(r.headline)}</td>
      <td><span class="badge bg-secondary">${escHtml(r.ticker)}</span></td>
      <td>${escHtml(r.date)}</td>
      <td>${escHtml(r.source)}</td>
      <td>${badge}${tip}</td>
    </tr>`;
  }).join("");
}

function signalBadge(signal) {
  if (!signal) return `<span class="signal-badge badge-unprocessed">—</span>`;
  const labels = { buy: "Buy", sell: "Sell", neutral: "Neutral", error: "Error" };
  const cls = `badge-${signal}`;
  return `<span class="signal-badge ${cls}">${labels[signal] || signal}</span>`;
}

function renderPagination(total, perPage) {
  const totalPages = Math.ceil(total / perPage);
  const ul = document.getElementById("pagination");
  const info = document.getElementById("pageInfo");
  const start = (state.page - 1) * perPage + 1;
  const end = Math.min(state.page * perPage, total);
  info.textContent = total ? `Showing ${start}–${end} of ${total}` : "No results";

  if (totalPages <= 1) { ul.innerHTML = ""; return; }

  let pages = [];
  pages.push(1);
  if (state.page > 3) pages.push("…");
  for (let p = Math.max(2, state.page - 1); p <= Math.min(totalPages - 1, state.page + 1); p++) pages.push(p);
  if (state.page < totalPages - 2) pages.push("…");
  if (totalPages > 1) pages.push(totalPages);

  ul.innerHTML =
    `<li class="page-item ${state.page === 1 ? "disabled" : ""}">
       <a class="page-link" href="#" onclick="goPage(${state.page - 1})">‹</a></li>` +
    pages.map(p =>
      p === "…"
        ? `<li class="page-item disabled"><span class="page-link">…</span></li>`
        : `<li class="page-item ${p === state.page ? "active" : ""}">
             <a class="page-link" href="#" onclick="goPage(${p})">${p}</a></li>`
    ).join("") +
    `<li class="page-item ${state.page === totalPages ? "disabled" : ""}">
       <a class="page-link" href="#" onclick="goPage(${state.page + 1})">›</a></li>`;
}

function goPage(p) {
  event.preventDefault();
  state.page = p;
  refreshTable();
}

function updateStats(total, unprocessed) {
  state.totalRows = total;
  state.unprocessed = unprocessed;
  document.getElementById("statsText").textContent =
    `${total.toLocaleString()} headlines · ${unprocessed.toLocaleString()} unprocessed`;
  document.getElementById("btnProcessUnprocessed").disabled = unprocessed === 0;
}

function populateSourceDropdown(sources) {
  const sel = document.getElementById("fSource");
  const current = sel.value;
  sel.innerHTML = `<option value="all">All sources</option>` +
    sources.map(s => `<option value="${escHtml(s)}" ${s === current ? "selected" : ""}>${escHtml(s)}</option>`).join("");
}

// ─── AI processing ────────────────────────────────────────────────────────────
let _processingActive = false;

async function startProcessing(mode) {
  // Collect row_ids to process
  let rowIds;
  if (mode === "unprocessed") {
    // Fetch all unprocessed row_ids across all pages
    rowIds = await fetchAllRowIds("unprocessed");
  } else {
    // Fetch row_ids for current filtered view
    rowIds = await fetchAllRowIds(state.signal, true);
  }

  if (!rowIds.length) {
    showToast("No headlines to process.", "info");
    return;
  }

  _processingActive = true;
  state.stopProcessing = false;
  showProgress(0, rowIds.length);

  const chunkSize = 50;
  let done = 0;

  for (let i = 0; i < rowIds.length; i += chunkSize) {
    if (state.stopProcessing) break;

    const chunk = rowIds.slice(i, i + chunkSize);
    const result = await postJSON("/api/process", { row_ids: chunk });

    if (result && result.error) {
      hideProgress();
      showToast("Error: " + result.error, "danger");
      _processingActive = false;
      return;
    }

    done += chunk.length;
    showProgress(done, rowIds.length);
  }

  hideProgress();
  _processingActive = false;
  showToast(
    state.stopProcessing ? "Processing stopped." : `Done! Processed ${done} headlines.`,
    "success"
  );
  await refreshTable();
}

async function fetchAllRowIds(signalFilter, useCurrentFilters = false) {
  const params = new URLSearchParams({
    page: 1,
    per_page: 99999,
    ticker:    useCurrentFilters ? state.ticker : "",
    date_from: useCurrentFilters ? state.dateFrom : "",
    date_to:   useCurrentFilters ? state.dateTo : "",
    source:    useCurrentFilters ? state.source : "all",
    signal:    signalFilter,
  });
  const data = await fetchJSON("/api/data?" + params);
  if (!data) return [];
  // Only return rows that actually have no signal (for unprocessed mode)
  if (signalFilter === "unprocessed") {
    return data.rows.filter(r => !r.signal).map(r => r.row_id);
  }
  return data.rows.map(r => r.row_id);
}

function stopProcessing() {
  state.stopProcessing = true;
}

function showProgress(done, total) {
  const wrap = document.getElementById("progressWrap");
  wrap.style.display = "block";
  const pct = total ? Math.round((done / total) * 100) : 0;
  document.getElementById("progressBar").style.width = pct + "%";
  document.getElementById("progressText").textContent = `${done} / ${total}`;
}

function hideProgress() {
  document.getElementById("progressWrap").style.display = "none";
}

// ─── Export ───────────────────────────────────────────────────────────────────
function exportCSV() {
  const params = new URLSearchParams({
    ticker:    state.ticker,
    date_from: state.dateFrom,
    date_to:   state.dateTo,
    source:    state.source,
    signal:    state.signal,
  });
  window.location = "/api/export?" + params;
}

// ─── Utilities ────────────────────────────────────────────────────────────────
async function fetchJSON(url) {
  try {
    const res = await fetch(url);
    return await res.json();
  } catch (e) {
    console.error("fetchJSON error", e);
    return null;
  }
}

async function postJSON(url, body) {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return await res.json();
  } catch (e) {
    console.error("postJSON error", e);
    return null;
  }
}

function escHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function showToast(msg, type = "info") {
  const existing = document.getElementById("appToast");
  if (existing) existing.remove();

  const toast = document.createElement("div");
  toast.id = "appToast";
  toast.className = `alert alert-${type} alert-dismissible position-fixed bottom-0 end-0 m-3`;
  toast.style.zIndex = 9999;
  toast.innerHTML = `${escHtml(msg)}<button type="button" class="btn-close" data-bs-dismiss="alert"></button>`;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}
