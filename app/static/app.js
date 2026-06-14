"use strict";

// Three explicit states, no hidden state machine: upload -> processing -> results.
const states = {
  upload: document.getElementById("upload-state"),
  processing: document.getElementById("processing-state"),
  results: document.getElementById("results-state"),
};

const form = document.getElementById("application-form");
const uploadError = document.getElementById("upload-error");
const loadSampleSelect = document.getElementById("load-sample-select");
const resetBtn = document.getElementById("reset-btn");

// A label can be uploaded as two sides; the back is optional.
let selectedFront = null;
let selectedBack = null;

// Object URLs created for the results-page previews; revoked on reset to avoid
// leaking blobs.
let previewUrls = [];

// --- Verdict rendering: word + symbol + colour ----------------------------- //
const VERDICT_META = {
  match:            { label: "Verified",    symbol: "✓" },  // check
  match_normalized: { label: "Verified*",   symbol: "✓" },
  mismatch:         { label: "Mismatch",    symbol: "✗" },  // present on label, but differs
  missing:          { label: "Missing",     symbol: "✗" },  // required value absent from the label
  partial_match:    { label: "Partial Match", symbol: "?" }, // only part of the name matches
  not_found:        { label: "Not found",   symbol: "⚠" },  // nothing to verify (blank + absent)
  not_applicable:   { label: "N/A",         symbol: "—" },  // not required for class
  assumed:          { label: "Assumed",     symbol: "≈" },  // on label, not in the form
};

// Human label for the inferred beverage class shown in the "Treated as" chip.
const BEV_LABEL = { spirits: "Spirits", wine: "Wine", malt: "Beer / Malt beverage", unknown: "Unclassified" };

function showState(name) {
  for (const [key, el] of Object.entries(states)) {
    el.hidden = key !== name;
  }
}

function showUploadError(message) {
  uploadError.textContent = message;
  uploadError.hidden = false;
}

function clearUploadError() {
  uploadError.hidden = true;
  uploadError.textContent = "";
}

// --- File selection: wire one dropzone to a setter ------------------------- //
function wireDropzone(dropzoneId, onFile) {
  const dropzone = document.getElementById(dropzoneId);
  const input = dropzone.querySelector(".dz-input");
  const chooseBtn = dropzone.querySelector(".dz-choose");
  const nameEl = dropzone.querySelector(".dz-name");
  const previewEl = dropzone.querySelector(".dz-preview");

  const setName = (file) => {
    nameEl.textContent = file ? `Selected: ${file.name}` : "";

    // Render a thumbnail right inside the box. Images only — PDFs and other
    // non-image types can't display in an <img>, so they keep the text label.
    // The previous object URL is revoked first so swapped/cleared files don't leak.
    if (dropzone._previewUrl) {
      URL.revokeObjectURL(dropzone._previewUrl);
      dropzone._previewUrl = null;
    }
    if (file && file.type && file.type.startsWith("image/")) {
      const url = URL.createObjectURL(file);
      dropzone._previewUrl = url;
      previewEl.src = url;
      previewEl.alt = `Preview of the selected label, ${file.name}`;
      previewEl.hidden = false;
      dropzone.classList.add("has-preview");
    } else {
      previewEl.removeAttribute("src");
      previewEl.alt = "";
      previewEl.hidden = true;
      dropzone.classList.remove("has-preview");
    }
  };
  // Expose the name-clearer so reset() can blank it out (also clears the preview).
  dropzone._clearName = () => setName(null);

  chooseBtn.addEventListener("click", () => input.click());
  dropzone.addEventListener("click", (e) => {
    if (e.target === chooseBtn) return;
    input.click();
  });
  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
  });
  input.addEventListener("change", () => {
    if (input.files.length) { onFile(input.files[0]); setName(input.files[0]); clearUploadError(); }
  });

  ["dragenter", "dragover"].forEach((ev) =>
    dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); })
  );
  ["dragleave", "drop"].forEach((ev) =>
    dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); })
  );
  dropzone.addEventListener("drop", (e) => {
    if (e.dataTransfer.files.length) {
      onFile(e.dataTransfer.files[0]);
      setName(e.dataTransfer.files[0]);
      clearUploadError();
    }
  });

  return { input, setName };
}

const frontDz = wireDropzone("dropzone-front", (f) => { selectedFront = f; });
const backDz = wireDropzone("dropzone-back", (f) => { selectedBack = f; });

// --- Load sample ----------------------------------------------------------- //
// Fetch one bundled sample image by filename and drop it into a dropzone.
async function loadSampleImage(name, setSelected, dz) {
  const imgRes = await fetch(`/sample-image/${encodeURIComponent(name)}`);
  if (!imgRes.ok) return;
  const blob = await imgRes.blob();
  const ext = (name.split(".").pop() || "jpg");
  const file = new File([blob], name, { type: blob.type || `image/${ext}` });
  setSelected(file);
  dz.setName(file);
}

// Fetch one application record (`/sample?<query>`) and fill the form + image(s),
// so the demo runs before Verify. Shared by the type menu and the ?ttb_id=
// deep-link. `errMsg` is shown if the record can't be loaded.
async function loadSampleByQuery(query, errMsg) {
  clearUploadError();
  // Clear any previously-loaded sample so a record with no back doesn't keep the old one.
  selectedFront = null;
  selectedBack = null;
  document.getElementById("dropzone-front")._clearName();
  document.getElementById("dropzone-back")._clearName();
  try {
    const res = await fetch(`/sample?${query}`);
    if (!res.ok) throw new Error("no sample");
    const s = await res.json();
    document.getElementById("brand_name").value = s.brand_name || "";
    document.getElementById("class_type").value = s.class_type || "";
    document.getElementById("abv").value = s.abv || "";
    document.getElementById("net_contents").value = s.net_contents || "";
    document.getElementById("country_of_origin").value = s.country_of_origin || "";

    // Some samples have a back label too (e.g. the warning is on the back).
    if (s.front) await loadSampleImage(s.front, (f) => { selectedFront = f; }, frontDz);
    if (s.back)  await loadSampleImage(s.back,  (f) => { selectedBack = f; },  backDz);
  } catch (err) {
    showUploadError(errMsg || "Sample data isn't available. You can still upload a label manually.");
  }
}

// Load a representative sample for one alcohol type (spirits / wine / beer).
function loadSample(type) {
  return loadSampleByQuery(`type=${encodeURIComponent(type)}`);
}

loadSampleSelect.addEventListener("change", async () => {
  const type = loadSampleSelect.value;
  if (!type) return;
  await loadSample(type);
  loadSampleSelect.value = "";   // reset back to the "Load sample…" placeholder
});

// --- Fill the form from a CSV file (single-label; not batch) ---------------- //
// A minimal RFC-4180-ish parser so quoted fields with commas (e.g. a fanciful
// name) survive. Returns an array of row objects keyed by the header.
function parseCsv(text) {
  const rows = [];
  let field = "", row = [], inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (c === '"') { inQuotes = false; }
      else { field += c; }
    } else if (c === '"') { inQuotes = true; }
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\n") { row.push(field); rows.push(row); row = []; field = ""; }
    else if (c !== "\r") { field += c; }
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  const nonEmpty = rows.filter((r) => r.some((cell) => cell.trim() !== ""));
  if (!nonEmpty.length) return [];
  const header = nonEmpty[0].map((h) => h.trim());
  return nonEmpty.slice(1).map((r) =>
    Object.fromEntries(header.map((h, i) => [h, (r[i] ?? "").trim()])));
}

function fillFormFromRow(r) {
  document.getElementById("brand_name").value = r.brand_name || "";
  document.getElementById("class_type").value = r.class_type || "";
  document.getElementById("abv").value = r.abv || "";
  document.getElementById("net_contents").value = r.net_contents || "";
  document.getElementById("country_of_origin").value = r.country_of_origin || "";
}
// NOTE: the "Fill from CSV…" UI was removed; parseCsv/fillFormFromRow are kept
// (uncalled) so the flow can be restored without rebuilding the parser.

// --- Submit ---------------------------------------------------------------- //
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  clearUploadError();
  if (!selectedFront) {
    showUploadError("Please choose a front label image or PDF first.");
    return;
  }

  const body = new FormData();
  body.append("file", selectedFront);
  if (selectedBack) body.append("back_file", selectedBack);
  body.append("brand_name", document.getElementById("brand_name").value);
  body.append("class_type", document.getElementById("class_type").value);
  body.append("abv", document.getElementById("abv").value);
  body.append("net_contents", document.getElementById("net_contents").value);
  body.append("country_of_origin", document.getElementById("country_of_origin").value);

  showState("processing");
  try {
    const res = await fetch("/verify", { method: "POST", body });
    const data = await res.json();
    if (!res.ok) {
      showState("upload");
      showUploadError(data.error || "We couldn't verify this label. Please try again.");
      return;
    }
    renderResults(data);
    showState("results");
  } catch (err) {
    showState("upload");
    showUploadError("We couldn't reach the verifier. Please check your connection and try again.");
  }
});

// The most recent verification payload (for the pattern/text toggle), and
// whether that panel is currently open.
let lastResult = null;
let resultPatternsOpen = false;

// --- Results rendering ----------------------------------------------------- //
function renderResults(data) {
  lastResult = data;
  resultPatternsOpen = false;
  renderResultActions(data);
  renderResultPatternPanel();

  const summaryEl = document.getElementById("overall-summary");
  const s = data.summary;
  summaryEl.textContent = s.headline;
  summaryEl.className = "summary " + (s.all_clear ? "all-clear" : "needs-review");

  // Which class ruleset was applied (drives the class-aware ABV / sulfite rules).
  const bevEl = document.getElementById("beverage-class");
  if (data.beverage_class) {
    bevEl.textContent = `Ruleset: ${BEV_LABEL[data.beverage_class] || data.beverage_class}`;
    bevEl.hidden = false;
  } else {
    bevEl.hidden = true;
  }

  // Timing breakdown, with OCR (the label-reading step) called out explicitly.
  const t = data.timings;
  const timingEl = document.getElementById("timing");
  timingEl.innerHTML = "";
  timingEl.append(
    timingStat("OCR processing time", `${Math.round(t.ocr_ms)} ms`),
    timingStat("Total processing time", `${t.total_s} s`),
  );

  renderImages();

  const body = document.getElementById("results-body");
  body.innerHTML = "";

  const rows = [...data.fields];
  // Append the warning as its own row (it carries extra case/content detail).
  rows.push({
    field: data.warning.field,
    expected: "Statutory text, ALL-CAPS prefix",
    found: data.warning.found_prefix || (data.warning.found_text ? "(warning text)" : "(none)"),
    verdict: data.warning.verdict,
    note: data.warning.note,
  });

  for (const f of rows) {
    const tr = document.createElement("tr");
    tr.appendChild(td(f.field, "Field"));
    tr.appendChild(td(f.expected || "—", "Expected"));
    tr.appendChild(td(f.found || "—", "Found on label"));
    tr.appendChild(verdictCell(f.verdict));
    tr.appendChild(td(f.note || "", "Note"));
    body.appendChild(tr);
  }
}

// The TTB ID is the front image filename without its extension
// ("15141001000396.jpg" -> "15141001000396").
function idFromFilename(name) {
  return (name || "").replace(/\.[^.]+$/, "");
}

// Top-right debugging links on the results view: TTB COLA registry lookup and a
// PATTERN toggle. (No SINGLE link here — this IS the single-label view.)
function renderResultActions(data) {
  const bar = document.getElementById("result-actions");
  bar.innerHTML = "";

  // TTB — COLA registry lookup, keyed on the front filename (the TTB ID), shown
  // only when a front file is present to derive an ID from.
  const id = selectedFront ? idFromFilename(selectedFront.name) : "";
  if (id) {
    const ttb = document.createElement("a");
    ttb.className = "entry-link";
    ttb.href = "https://ttbonline.gov/colasonline/viewColaDetails.do" +
      `?action=publicFormDisplay&ttbid=${encodeURIComponent(id)}`;
    ttb.target = "_blank";
    ttb.rel = "noopener noreferrer";
    ttb.textContent = `TTB: ${id}`;
    ttb.title = `Open TTB ID ${id} in the public COLA registry (new tab)`;
    bar.appendChild(ttb);
  }

  // PATTERN — toggle the generated field patterns + OCR label text.
  const pat = document.createElement("button");
  pat.type = "button";
  pat.className = "entry-link" + (resultPatternsOpen ? " is-open" : "");
  pat.id = "result-pattern-toggle";
  pat.setAttribute("aria-pressed", resultPatternsOpen ? "true" : "false");
  pat.textContent = "PATTERN";
  pat.title = "Show the generated field patterns and OCR label text";
  pat.addEventListener("click", () => {
    resultPatternsOpen = !resultPatternsOpen;
    pat.classList.toggle("is-open", resultPatternsOpen);
    pat.setAttribute("aria-pressed", resultPatternsOpen ? "true" : "false");
    renderResultPatternPanel();
  });
  bar.appendChild(pat);
}

// Build (or hide) the generated-pattern / OCR-text panel for the current result.
function renderResultPatternPanel() {
  const box = document.getElementById("result-pattern-panel");
  box.innerHTML = "";
  if (!resultPatternsOpen || !lastResult) { box.hidden = true; return; }
  box.hidden = false;
  box.className = "pattern-panel";

  const h1 = document.createElement("h4");
  h1.className = "pattern-h";
  h1.textContent = "Generated field patterns";
  box.appendChild(h1);

  const pats = lastResult.patterns || [];
  if (pats.length) {
    const dl = document.createElement("dl");
    dl.className = "pattern-list";
    for (const p of pats) {
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
    const p = document.createElement("p");
    p.className = "detail-note";
    p.textContent = "No application values were provided to build patterns from.";
    box.appendChild(p);
  }

  const h2 = document.createElement("h4");
  h2.className = "pattern-h";
  h2.textContent = "Label text (OCR)";
  box.appendChild(h2);
  const pre = document.createElement("pre");
  pre.className = "pattern-text";
  pre.textContent = lastResult.ocr_text || "(no text was read from the label)";
  box.appendChild(pre);
}

// Show the uploaded front (and back, if present) on the results page. Images are
// rendered straight from the local File objects — nothing round-trips the server.
function renderImages() {
  previewUrls.forEach(URL.revokeObjectURL);
  previewUrls = [];

  const wrap = document.getElementById("label-images");
  setPreview("front-preview", null, selectedFront);
  const backFig = document.getElementById("back-fig");
  if (selectedBack) {
    setPreview("back-preview", backFig, selectedBack);
    backFig.hidden = false;
  } else {
    backFig.hidden = true;
  }
  wrap.hidden = false;
}

function setPreview(imgId, figEl, file) {
  const img = document.getElementById(imgId);
  // PDFs and other non-image types can't render in an <img>; show a label instead.
  if (file && file.type && file.type.startsWith("image/")) {
    const url = URL.createObjectURL(file);
    previewUrls.push(url);
    img.src = url;
    img.hidden = false;
  } else {
    img.removeAttribute("src");
    img.hidden = true;
  }
}

function timingStat(label, value) {
  const span = document.createElement("span");
  span.className = "timing-stat";
  const lab = document.createElement("span");
  lab.className = "timing-label";
  lab.textContent = label + ": ";
  const val = document.createElement("strong");
  val.textContent = value;
  span.append(lab, val);
  return span;
}

function td(text, label) {
  const cell = document.createElement("td");
  cell.textContent = text;
  if (label) cell.setAttribute("data-label", label);
  return cell;
}

function verdictCell(verdict) {
  const cell = document.createElement("td");
  cell.setAttribute("data-label", "Result");
  const meta = VERDICT_META[verdict] || { label: verdict, symbol: "" };
  const pill = document.createElement("span");
  pill.className = `verdict verdict-${verdict}`;
  pill.textContent = `${meta.symbol} ${meta.label}`;
  cell.appendChild(pill);
  return cell;
}

// --- Handoff from the batch tab -------------------------------------------- //
// The batch view's "Test individually" button stashes one record's application
// values + label image bytes in sessionStorage and navigates here. Pick that up
// on load: rebuild the File objects, fill the form, and show the previews so the
// operator can re-run a single label on its own. The handoff is consumed once.
async function fileFromHandoffPart(part) {
  if (!part || !part.name) return null;
  if (part.dataUrl) {
    try {
      const blob = await (await fetch(part.dataUrl)).blob();
      return new File([blob], part.name, { type: part.type || blob.type || "" });
    } catch (e) {
      return null;
    }
  }
  // No embedded bytes (the batch dropped them, e.g. too large): try to re-fetch a
  // bundled sample image by name. A user-uploaded file won't be found — the form
  // values still transfer and the operator re-picks the image.
  try {
    const res = await fetch(`/sample-image/${encodeURIComponent(part.name)}`);
    if (!res.ok) return null;
    const blob = await res.blob();
    return new File([blob], part.name, { type: blob.type || part.type || "" });
  } catch (e) {
    return null;
  }
}

async function consumeSingleLabelHandoff() {
  let raw;
  try { raw = sessionStorage.getItem("singleLabelHandoff"); } catch (e) { return; }
  if (!raw) return;
  try { sessionStorage.removeItem("singleLabelHandoff"); } catch (e) { /* ignore */ }

  let h;
  try { h = JSON.parse(raw); } catch (e) { return; }
  const a = h.app || {};
  document.getElementById("brand_name").value = a.brand_name || "";
  document.getElementById("class_type").value = a.class_type || "";
  document.getElementById("abv").value = a.abv || "";
  document.getElementById("net_contents").value = a.net_contents || "";
  document.getElementById("country_of_origin").value = a.country_of_origin || "";

  const front = await fileFromHandoffPart(h.front);
  if (front) { selectedFront = front; frontDz.setName(front); }
  const back = await fileFromHandoffPart(h.back);
  if (back) { selectedBack = back; backDz.setName(back); }
  clearUploadError();
}

// On load: a ?ttb_id= deep link loads that specific test; otherwise pick up a
// handoff from the batch tab's "SINGLE" link.
const _ttbId = new URLSearchParams(window.location.search).get("ttb_id");
if (_ttbId && _ttbId.trim()) {
  loadSampleByQuery(`ttb_id=${encodeURIComponent(_ttbId.trim())}`,
                    `No test found for TTB ID “${_ttbId.trim()}”.`);
} else {
  consumeSingleLabelHandoff();
}

// --- Reset ----------------------------------------------------------------- //
resetBtn.addEventListener("click", () => {
  form.reset();
  selectedFront = null;
  selectedBack = null;
  frontDz.input.value = "";
  backDz.input.value = "";
  document.getElementById("dropzone-front")._clearName();
  document.getElementById("dropzone-back")._clearName();
  previewUrls.forEach(URL.revokeObjectURL);
  previewUrls = [];
  // Drop the results-view extras (pattern panel + its toggle state).
  lastResult = null;
  resultPatternsOpen = false;
  document.getElementById("result-actions").innerHTML = "";
  const panel = document.getElementById("result-pattern-panel");
  panel.innerHTML = "";
  panel.hidden = true;
  clearUploadError();
  showState("upload");
});
