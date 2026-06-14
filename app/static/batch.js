"use strict";

const csvInput = document.getElementById("csv-input");
const filesInput = document.getElementById("batch-files");
const dropzone = document.getElementById("batch-dropzone");
const chooseBtn = document.getElementById("batch-choose-btn");
const fileCountEl = document.getElementById("batch-file-count");
const startBtn = document.getElementById("batch-start-btn");
const sampleSelect = document.getElementById("batch-sample-select");
const errorEl = document.getElementById("batch-error");

const progressSection = document.getElementById("batch-progress");
const summaryEl = document.getElementById("batch-summary");
const bodyEl = document.getElementById("batch-body");

let selectedFiles = [];
let pollTimer = null;
let currentJobId = null;

// Drill-down: which row indices are expanded, and a cache of fetched per-item
// detail (so re-renders during polling don't re-fetch). patternsOpen tracks which
// expanded rows additionally show the "Patterns & text" panel (kept separate so the
// state survives a re-render).
const expanded = new Set();
const patternsOpen = new Set();
const detailCache = {};

// The files submitted for the current job (snapshot of selectedFiles at start),
// so the drill-down can show each label's image — the server frees the bytes after
// processing, but the browser still holds the originals. A batch row maps to its
// images by filename (front = item.filename, back = detail.back_filename), since
// rows are keyed per-product, not per-uploaded-file. detailImgUrls caches one
// object URL per filename.
let submittedFiles = [];
let filesByName = {};
const detailImgUrls = {};

// cls maps to a .verdict-* colour ("assumed" = yellow caution); label is the
// Title-Case text shown in the Status pill. (processing renders as a bar, below.)
const STATUS_META = {
  "pending":      { symbol: "\u2026", cls: "assumed",  label: "Pending" },
  "processing":   { symbol: "\u2026", cls: "assumed",  label: "Processing" },
  "verified":     { symbol: "\u2713", cls: "match",    label: "Verified" },
  "needs review": { symbol: "\u26A0", cls: "assumed",  label: "Needs Review" },
  "failed":       { symbol: "\u2717", cls: "mismatch", label: "Failed" },
};

// Per-field verdict rendering (word + symbol), mirrored from the single-label view.
const VERDICT_META = {
  match:            { label: "Verified",  symbol: "\u2713" },
  match_normalized: { label: "Verified*", symbol: "\u2713" },
  mismatch:         { label: "Mismatch",  symbol: "\u2717" },
  missing:          { label: "Missing",   symbol: "\u2717" },
  partial_match:    { label: "Partial Match", symbol: "?" },
  not_found:        { label: "Not found", symbol: "\u26A0" },
  not_applicable:   { label: "N/A",       symbol: "\u2014" },
  assumed:          { label: "Assumed",   symbol: "\u2248" },
};

const BEV_LABEL = { spirits: "Spirits", wine: "Wine", malt: "Beer / Malt beverage", unknown: "Unclassified" };

// Statuses for which detail exists (item has finished processing).
const FINISHED = new Set(["verified", "needs review", "failed"]);

function showError(msg) { errorEl.textContent = msg; errorEl.hidden = false; }
function clearError() { errorEl.hidden = true; errorEl.textContent = ""; }

function setFiles(files) {
  selectedFiles = Array.from(files).slice(0, 50);
  fileCountEl.textContent = selectedFiles.length
    ? `${selectedFiles.length} file(s) selected`
    : "";
  clearError();
}

chooseBtn.addEventListener("click", () => filesInput.click());
dropzone.addEventListener("click", (e) => { if (e.target !== chooseBtn) filesInput.click(); });
dropzone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); filesInput.click(); }
});
filesInput.addEventListener("change", () => setFiles(filesInput.files));

["dragenter", "dragover"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); }));
["dragleave", "drop"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); }));
dropzone.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) setFiles(e.dataTransfer.files); });

// Start a batch from a CSV file + label files (used by both the manual
// "Verify all" button and the "Load sample" flow).
async function submitBatch(csvFile, files) {
  startBtn.disabled = true;
  // New batch: clear any drill-down state from a previous run.
  expanded.clear();
  patternsOpen.clear();
  for (const k of Object.keys(detailCache)) delete detailCache[k];
  for (const k of Object.keys(detailImgUrls)) {
    if (detailImgUrls[k]) URL.revokeObjectURL(detailImgUrls[k]);
    delete detailImgUrls[k];
  }
  // Snapshot the files for this job so drill-down images survive later re-selection.
  submittedFiles = files.slice();
  filesByName = {};
  for (const f of submittedFiles) filesByName[f.name] = f;

  const body = new FormData();
  body.append("csv_file", csvFile);
  for (const f of files) body.append("files", f);
  try {
    const res = await fetch("/batch", { method: "POST", body });
    const data = await res.json();
    if (!res.ok) { showError(data.error || "Could not start the batch."); startBtn.disabled = false; return; }
    progressSection.hidden = false;
    poll(data.job_id);
  } catch (err) {
    showError("We couldn't reach the verifier. Please try again.");
    startBtn.disabled = false;
  }
}

startBtn.addEventListener("click", () => {
  clearError();
  if (!csvInput.files.length) { showError("Please choose the application CSV file."); return; }
  if (!selectedFiles.length) { showError("Please choose at least one label file."); return; }
  submitBatch(csvInput.files[0], selectedFiles);
});

// --- Load sample batch ----------------------------------------------------- //
// The server returns a manifest (Fixed or Random set of bundled rows); we fetch
// each referenced image, build a CSV in memory, and run it as a normal batch —
// so the drill-down thumbnails work just like a user-uploaded batch.
const BATCH_CSV_COLUMNS = ["ttb_id", "front", "back", "brand_name", "class_type",
                           "abv", "net_contents", "country_of_origin"];

function rowsToCsv(rows) {
  const esc = (v) => {
    const s = (v ?? "").toString();
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [BATCH_CSV_COLUMNS.join(",")];
  for (const r of rows) lines.push(BATCH_CSV_COLUMNS.map((c) => esc(r[c])).join(","));
  return lines.join("\n");
}

async function fetchSampleImage(name) {
  const res = await fetch(`/sample-image/${encodeURIComponent(name)}`);
  if (!res.ok) return null;
  const blob = await res.blob();
  const ext = name.split(".").pop() || "jpg";
  return new File([blob], name, { type: blob.type || `image/${ext}` });
}

async function loadSampleBatch(kind) {
  clearError();
  startBtn.disabled = true;
  try {
    const res = await fetch(`/batch/sample?set=${encodeURIComponent(kind)}`);
    const data = await res.json();
    if (!res.ok) { showError(data.error || "Sample data isn't available."); startBtn.disabled = false; return; }
    const rows = data.rows || [];
    if (!rows.length) { showError("No sample labels available."); startBtn.disabled = false; return; }

    // Fetch each referenced image once (front + optional back).
    const names = new Set();
    for (const r of rows) { if (r.front) names.add(r.front); if (r.back) names.add(r.back); }
    const files = [];
    for (const name of names) { const f = await fetchSampleImage(name); if (f) files.push(f); }
    if (!files.length) { showError("Sample images couldn't be loaded."); startBtn.disabled = false; return; }

    // Reflect the loaded sample in the pickers so a follow-up "Verify all" works too.
    csvInput.value = "";          // the in-memory sample CSV supersedes any picked file
    setFiles(files);
    fileCountEl.textContent = `${files.length} sample file(s) loaded`;

    const csvFile = new File([rowsToCsv(rows)], "sample_batch.csv", { type: "text/csv" });
    await submitBatch(csvFile, files);
  } catch (err) {
    showError("We couldn't load the sample. Please try again.");
    startBtn.disabled = false;
  }
}

sampleSelect.addEventListener("change", async () => {
  const kind = sampleSelect.value;
  if (!kind) return;
  await loadSampleBatch(kind);
  sampleSelect.value = "";       // reset to the "Load sample…" placeholder
});

function poll(jobId) {
  currentJobId = jobId;
  const tick = async () => {
    const res = await fetch(`/batch/${jobId}`);
    if (!res.ok) return;
    const data = await res.json();
    render(data);
    if (data.done) {
      clearInterval(pollTimer);
      startBtn.disabled = false;
    }
  };
  tick();
  pollTimer = setInterval(tick, 1000);
}

function render(data) {
  const headline = `${data.finished} of ${data.total} processed` +
    (data.flagged ? ` — ${data.flagged} need review` : "") +
    (data.failed ? ` — ${data.failed} failed` : "");
  summaryEl.textContent = data.done ? `Done. ${headline}.` : `${headline}…`;
  summaryEl.className = "summary " +
    (data.done && !data.flagged && !data.failed ? "all-clear" : "needs-review");

  bodyEl.innerHTML = "";
  data.items.forEach((item, index) => {
    const finished = FINISHED.has(item.status);
    const tr = document.createElement("tr");

    // ID cell (the TTB ID from the application data) carries a caret affordance
    // when the row can be drilled into.
    const fileTd = cell(designator(item), "ID");
    if (finished) {
      const caret = document.createElement("span");
      caret.className = "drill-caret";
      caret.textContent = expanded.has(index) ? "▾ " : "▸ ";
      fileTd.prepend(caret);
    }
    tr.appendChild(fileTd);

    const statusTd = document.createElement("td");
    statusTd.setAttribute("data-label", "Status");
    if (item.status === "processing") {
      // In-progress: an animated light-blue bar that fills up, not a red pill.
      const bar = document.createElement("div");
      bar.className = "proc-bar";
      bar.setAttribute("role", "progressbar");
      bar.setAttribute("aria-label", "Processing");
      bar.appendChild(document.createElement("div")).className = "proc-bar-fill";
      statusTd.appendChild(bar);
    } else {
      const meta = STATUS_META[item.status] || { symbol: "", cls: "not_found", label: item.status };
      const pill = document.createElement("span");
      pill.className = `verdict verdict-${meta.cls}`;
      pill.textContent = `${meta.symbol} ${meta.label || item.status}`;
      statusTd.appendChild(pill);
    }
    tr.appendChild(statusTd);

    const verified = item.summary ? `${item.summary.verified}/${item.summary.total}` : "—";
    tr.appendChild(cell(verified, "Verified"));
    tr.appendChild(cell(item.timing_s != null ? `${item.timing_s}s` : "—", "Time"));

    if (finished) {
      tr.classList.add("row-expandable");
      tr.tabIndex = 0;
      tr.setAttribute("role", "button");
      tr.setAttribute("aria-expanded", expanded.has(index) ? "true" : "false");
      tr.addEventListener("click", () => toggleDetail(index));
      tr.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleDetail(index); }
      });
    }
    bodyEl.appendChild(tr);

    if (finished && expanded.has(index)) bodyEl.appendChild(detailRow(index, item));
  });
}

// Expand/collapse one row, lazily fetching its field-level detail on first open.
async function toggleDetail(index) {
  if (expanded.has(index)) {
    expanded.delete(index);
    refresh();
    return;
  }
  expanded.add(index);
  refresh();  // show the loading row immediately
  if (detailCache[index] === undefined && currentJobId) {
    try {
      const res = await fetch(`/batch/${currentJobId}/item/${index}`);
      detailCache[index] = res.ok ? await res.json() : null;
    } catch (err) {
      detailCache[index] = null;
    }
    if (expanded.has(index)) refresh();
  }
}

// The expanded detail row: the label image alongside a nested per-field table
// (or a loading/error/empty state).
function detailRow(index, item) {
  const tr = document.createElement("tr");
  tr.className = "detail-row";
  const td = document.createElement("td");
  td.colSpan = 4;

  const detail = detailCache[index];

  // Top-right action bar: TTB registry lookup, re-run in the single-label tab,
  // and toggle the generated-pattern / OCR-text panel.
  td.appendChild(detailActions(index, item, detail));

  const wrap = document.createElement("div");
  wrap.className = "detail-wrap";

  // The label image(s), from the files the browser still holds (the server frees
  // the bytes after processing). Front comes from the row's filename; the back
  // (if any) is named in the fetched detail. PDFs can't render in an <img>, so
  // they're omitted. Captions are only shown when there's a back, to distinguish.
  // Front/back stack vertically (back below front) to conserve horizontal space.
  const hasBack = !!(detail && detail.back_filename);
  const thumbs = document.createElement("div");
  thumbs.className = "detail-thumbs";
  appendThumb(thumbs, item.filename, hasBack ? "Front" : "");
  if (hasBack) appendThumb(thumbs, detail.back_filename, "Back");
  if (thumbs.children.length) wrap.appendChild(thumbs);

  const content = document.createElement("div");
  content.className = "detail-content";
  // Intake note (e.g. a back named in the CSV but not uploaded) — shown first so
  // a missing warning/ABV is explained rather than looking like a tool error.
  if (detail && detail.note) content.appendChild(note(detail.note, true));
  // Which class ruleset was applied (class-aware ABV / sulfite rules).
  if (detail && detail.beverage_class) {
    const chip = document.createElement("p");
    chip.className = "bev-chip";
    chip.textContent = `Ruleset: ${BEV_LABEL[detail.beverage_class] || detail.beverage_class}`;
    content.appendChild(chip);
  }
  if (detail === undefined) {
    content.appendChild(note("Loading details…"));
  } else if (detail === null) {
    content.appendChild(note("Could not load details for this label."));
  } else if (item.status === "failed") {
    content.appendChild(note(detail.error || "This label could not be read."));
  } else if (!detail.fields || !detail.fields.length) {
    content.appendChild(note("No field details available."));
  } else {
    content.appendChild(detailTable(detail));
  }
  wrap.appendChild(content);

  td.appendChild(wrap);

  // The generated-pattern / OCR-text panel, shown beneath the wrap when toggled.
  if (patternsOpen.has(index)) td.appendChild(patternPanel(detail));

  tr.appendChild(td);
  return tr;
}

// The three per-entry debugging links (top-right of the expanded detail).
function detailActions(index, item, detail) {
  const bar = document.createElement("div");
  bar.className = "entry-links";

  // TTB — look this label up in the public TTB COLA registry, by its TTB ID
  // (same designator shown in the ID column).
  const id = designator(item);
  const ttb = document.createElement("a");
  ttb.className = "entry-link";
  ttb.href = "https://ttbonline.gov/colasonline/viewColaDetails.do" +
    `?action=publicFormDisplay&ttbid=${encodeURIComponent(id)}`;
  ttb.target = "_blank";
  ttb.rel = "noopener noreferrer";
  ttb.textContent = `TTB: ${id}`;
  ttb.title = `Open TTB ID ${id} in the public COLA registry (new tab)`;
  ttb.addEventListener("click", (e) => e.stopPropagation());
  bar.appendChild(ttb);

  // SINGLE — re-open this record's values + images in the single-label tab.
  const single = document.createElement("button");
  single.type = "button";
  single.className = "entry-link";
  single.textContent = "SINGLE";
  single.title = "Open this label's images and application values in the single-label tab";
  single.addEventListener("click", (e) => {
    e.stopPropagation();
    openInSingleLabel(item, detail);
  });
  bar.appendChild(single);

  // PATTERN — show the regex generated from each field and the OCR label text.
  const open = patternsOpen.has(index);
  const pat = document.createElement("button");
  pat.type = "button";
  pat.className = "entry-link" + (open ? " is-open" : "");
  pat.setAttribute("aria-pressed", open ? "true" : "false");
  pat.textContent = "PATTERN";
  pat.title = "Show the generated field patterns and OCR label text";
  pat.addEventListener("click", (e) => {
    e.stopPropagation();
    if (open) patternsOpen.delete(index); else patternsOpen.add(index);
    refresh();
  });
  bar.appendChild(pat);

  return bar;
}

// The generated-pattern + OCR-text panel for one label.
function patternPanel(detail) {
  const box = document.createElement("div");
  box.className = "pattern-panel";
  if (detail === undefined) { box.appendChild(note("Loading details…")); return box; }
  if (detail === null) { box.appendChild(note("Could not load details for this label.")); return box; }

  const h1 = document.createElement("h4");
  h1.className = "pattern-h";
  h1.textContent = "Generated field patterns";
  box.appendChild(h1);

  if (detail.patterns && detail.patterns.length) {
    const dl = document.createElement("dl");
    dl.className = "pattern-list";
    for (const p of detail.patterns) {
      const dt = document.createElement("dt");
      dt.textContent = `${p.field} — “${p.value}”`;
      const dd = document.createElement("dd");
      const code = document.createElement("code");
      code.textContent = p.pattern;
      dd.appendChild(code);
      dl.append(dt, dd);
    }
    box.appendChild(dl);
  } else {
    box.appendChild(note("No application values were provided to build patterns from."));
  }

  const h2 = document.createElement("h4");
  h2.className = "pattern-h";
  h2.textContent = "Label text (OCR)";
  box.appendChild(h2);
  const pre = document.createElement("pre");
  pre.className = "pattern-text";
  pre.textContent = detail.ocr_text || "(no text was read from the label)";
  box.appendChild(pre);

  return box;
}

// --- Hand off one record to the single-label tab --------------------------- //
// Stash this label's application values + image bytes in sessionStorage, then
// navigate to "/". The single-label page picks the handoff up on load (see
// app.js), reconstructs the File objects and pre-fills the form, so the operator
// can re-run and inspect one label on its own.
function readAsDataUrl(file) {
  return new Promise((resolve) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => resolve(null);
    r.readAsDataURL(file);
  });
}

async function handoffPart(name) {
  if (!name) return null;
  const f = filesByName[name];
  if (!f) return { name };                      // bytes gone; page may re-fetch a sample
  const dataUrl = await readAsDataUrl(f);
  return dataUrl ? { name, type: f.type || "", dataUrl } : { name, type: f.type || "" };
}

function storeHandoff(h) {
  try {
    sessionStorage.setItem("singleLabelHandoff", JSON.stringify(h));
    return true;
  } catch (e) {
    return false;   // typically QuotaExceededError for large embedded images
  }
}

async function openInSingleLabel(item, detail) {
  const a = (detail && detail.application) || {};
  const handoff = {
    app: {
      brand_name: a.brand_name || "",
      class_type: a.class_type || "",
      abv: a.abv || "",
      net_contents: a.net_contents || "",
      country_of_origin: a.country_of_origin || "",
    },
    front: await handoffPart(item.filename),
    back: detail && detail.back_filename ? await handoffPart(detail.back_filename) : null,
  };
  if (!storeHandoff(handoff)) {
    // Too large to embed: keep filenames only. Sample images can be re-fetched
    // by name on the other side; a user upload falls back to the values alone.
    const strip = (p) => (p ? { name: p.name, type: p.type } : null);
    handoff.front = strip(handoff.front);
    handoff.back = strip(handoff.back);
    storeHandoff(handoff);
  }
  window.location.href = "/";
}

// Append one label thumbnail (with an optional caption) if the named file is a
// renderable image the browser still holds.
function appendThumb(wrap, name, caption) {
  const url = detailImgUrl(name);
  if (!url) return;
  const fig = document.createElement("figure");
  fig.className = "detail-thumb";
  const img = document.createElement("img");
  img.src = url;
  img.alt = `Label ${name}`;
  img.loading = "lazy";
  fig.appendChild(img);
  if (caption) {
    const cap = document.createElement("figcaption");
    cap.textContent = caption;
    fig.appendChild(cap);
  }
  wrap.appendChild(fig);
}

// One cached object URL per filename (null for PDFs / non-images / missing files).
function detailImgUrl(name) {
  if (name in detailImgUrls) return detailImgUrls[name];
  const f = filesByName[name];
  let url = null;
  if (f && f.type && f.type.startsWith("image/")) url = URL.createObjectURL(f);
  detailImgUrls[name] = url;
  return url;
}

function detailTable(detail) {
  const table = document.createElement("table");
  table.className = "results-table detail-table";

  const thead = document.createElement("thead");
  const hrow = document.createElement("tr");
  for (const h of ["Field", "Expected (application)", "Found on label", "Result", "Note"]) {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = h;
    hrow.appendChild(th);
  }
  thead.appendChild(hrow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  const rows = [...detail.fields];
  // Append the warning as its own row (carries the case/content detail), mirroring
  // the single-label view.
  if (detail.warning) {
    rows.push({
      field: detail.warning.field,
      expected: "Statutory text, ALL-CAPS prefix",
      found: detail.warning.found_prefix ||
        (detail.warning.found_text ? "(warning text)" : "(none)"),
      verdict: detail.warning.verdict,
      note: detail.warning.note,
    });
  }

  for (const f of rows) {
    const tr = document.createElement("tr");
    tr.appendChild(cell(f.field, "Field"));
    tr.appendChild(cell(f.expected || "—", "Expected"));
    tr.appendChild(cell(f.found || "—", "Found on label"));
    tr.appendChild(verdictCell(f.verdict));
    tr.appendChild(cell(f.note || "", "Note"));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  return table;
}

function verdictCell(verdict) {
  const td = document.createElement("td");
  td.setAttribute("data-label", "Result");
  const meta = VERDICT_META[verdict] || { label: verdict, symbol: "" };
  const pill = document.createElement("span");
  pill.className = `verdict verdict-${verdict}`;
  pill.textContent = `${meta.symbol} ${meta.label}`;
  td.appendChild(pill);
  return td;
}

function note(text, warn) {
  const p = document.createElement("p");
  p.className = warn ? "detail-note detail-note-warn" : "detail-note";
  p.textContent = text;
  return p;
}

function cell(text, label) {
  const td = document.createElement("td");
  td.textContent = text;
  td.setAttribute("data-label", label);
  return td;
}

// The TTB ID is the front image filename without its extension
// ("15141001000396.jpg" -> "15141001000396").
function idFromFilename(name) {
  return (name || "").replace(/\.[^.]+$/, "");
}

// The per-item designator: the TTB ID from the application data when present,
// else a best-effort fallback to the front filename (for CSVs without a ttb_id).
function designator(item) {
  return (item && item.ttb_id) || idFromFilename(item ? item.filename : "");
}

async function refresh() {
  if (!currentJobId) return;
  const res = await fetch(`/batch/${currentJobId}`);
  if (res.ok) render(await res.json());
}
