# TTB Alcohol Label Verification Prototype

### ▶ Live Demo: **https://bevver-production.up.railway.app**

<sub>Lauren Schmitt · June 14, 2026</sub>

---

A prototype of the core daily task of a TTB compliance agent: **verify a beverage
label against its application data**. Upload a label (image or PDF), enter the
application values, and the tool reads the label with a vision model and reports a
per-field verdict — including the strict, exact check on the Government Health
Warning.

> **Key design choice:** label reading uses a hosted **vision model (Gemini)**,
> which handles the messy, real-world artwork in the COLA registry far more
> robustly than template OCR and returns both the structured fields and a verbatim
> transcription in a single call. Every reading — whichever backend produced it —
> flows through the *same* deterministic matching/warning rules, so the verdicts
> are identical and explainable. For environments that can't reach an external API
> (the agency firewall), a fully **local OCR pipeline is wired as a drop-in
> backup** — see [OCR backend](#ocr-backend).

---

## How this addresses the evaluation criteria

> Time was limited, so I focused on a solid, working core with clean code rather
> than half-finished extras. Wherever I made a trade-off or left something out,
> I've said so — here and under [Out of scope & trade-offs](#out-of-scope--trade-offs).

**Correctness and completeness.** The whole job works end to end: upload a label,
enter the application values, and every field comes back with a verdict — including
the Government Health Warning, which is checked exactly (wording *and* ALL-CAPS)
and stays robust even when a multi-column label makes the reader return the
sentences out of order. Just as important, the verifier is proven to *fail* when it
should: an adversarial test suite tampers with the ABV, brand, net contents, and
warning, and confirms each one is caught.

**Code quality and organization.** Each step lives in its own small module —
extraction, pattern-building, matching, class rules, the warning check, and the
pipeline that ties them together. The label reader is passed in rather than
hard-wired, so the entire test suite runs without loading any model. Every result
has the same simple shape: what was expected, what was found, the verdict, a
confidence, and a plain-English note.

**Right-sized technical choices.** It's a FastAPI backend with a plain
HTML/CSS/JS front end — no build step, no framework, no database, nothing to
persist. Matching is deterministic (rules, regexes, and fuzzy ratios) rather than a
black box, so every verdict can be explained and tested.

**User experience and error handling.** The interface is one screen with three
clear alcoholic beverage catagories, large ALL-CAPS field labels, and errors written 
in plain language. Every verdict shows a word and a symbol alongside its
color. Small things help too: the ABV field accepts whatever an operator types 
(`40`, `40%`, `40 pct`), a Clear-Fields
button wipes the slate, and the processing time is shown so the time budget
stays visible.

**Attention to requirements.** Everything the stakeholders asked for maps to
something built — verification takes place in seconds and averages out close to 
the 5 seconds suggested time budget, "STONE'S THROW" matching
"Stone's Throw", the exact ALL-CAPS warning, different mandatory fields per
commodity, batch processing, and an offline fallback for when the API can't be
reached. The [next section](#stakeholder-constraint--feature-mapping) lists them
one by one.

**Creative problem-solving.** A few cases needed more than a straight check: the
warning is matched sentence by sentence in any order, so a scrambled multi-column
read doesn't defeat it; a garbled net-contents reading is only repaired when the
fix matches an expected value, never to invent a match; a lookup table lets
"Whiskey" buried in body copy still satisfy a "Distilled Spirits" application; and
batch mode runs several labels — and each label's front and back — in parallel to
keep things fast.

---


## Architecture / pipeline

```
upload (image or PDF)
   │
   ├─ load            decode image, or render PDF first page to image (PyMuPDF)
   ├─ read label      vision model (Gemini) → structured fields + verbatim text
   │                  (backup: local preprocess → PaddleOCR/Tesseract, CPU)
   ├─ field extract   per-field strategies (not generic text search)
   ├─ field match     per-field rules + the strict warning check
   └─ results JSON    per-field verdicts + warning detail + per-stage timings
```

- `app/gemini_backend.py` — the **primary** reader. A hosted vision model reads
  the fields directly and returns a verbatim transcription; both flow through the
  same `matching` / `warning` rules. Readings are cached by image fingerprint
  (SHA-256), so an identical image isn't re-sent to the API — a two-tier cache
  (in-memory + an on-disk JSON store) that survives across requests/workers/
  restarts and is **wiped at program start**. See [OCR backend](#ocr-backend) for
  configuration.
- `app/ocr.py` — the **backup** reader: local image loading, preprocessing
  (grayscale → CLAHE → deskew), and on-device OCR (PaddleOCR; Tesseract via
  `OCR_BACKEND=tesseract`). Used only where the vision API isn't reachable.
- `app/extraction.py` — field extraction strategies (still feed the verdicts).
- `app/patterns.py` — generates a regex per field from the record value, and the
  `matched_on_label()` helper that drives the exact "Found on label" display.
- `app/matching.py` — per-field matching rules + the verdict vocabulary.
- `app/classes.py` — beverage-class inference (spirits / wine / malt) and the
  per-class rule profiles that make ABV/sulfite checks class-aware.
- `app/warning.py` — the dedicated Government Health Warning verifier.
- `app/pipeline.py` — orchestration + timing. The reader is **injectable**, so the
  test suite exercises extraction/matching without any model or OCR engine.
- `app/main.py` — FastAPI app + static UI. `app/batch.py` — Phase 2 batch router.



### Field matching rules

| Field | Rule |
|---|---|
| Brand name | A regex generated from the record value is searched across the whole label text (case/punctuation/whitespace-insensitive), so a brand inside `…Company LLC` or split across lines still verifies; reports `match_normalized`. Multi-word brands match even when the text runs them together (`PullmanPILSNER`); single-word brands keep word boundaries so a short name can't fire inside a larger word. |
| Class/type | The application's designation (or its head word) is searched on the label; if absent, the label's stated designation is resolved to a superclass via the lookup table and compared (a "Malbec" label is consistent with a "Wine" application), with a fuzzy fallback (ratio ≥ 0.85, rapidfuzz). |
| ABV | **Class-aware** (see below): numeric equality after extraction (`45 == 45.0`) with a proof cross-check when present (`proof = 2 × ABV`); strict for spirits, `Table Wine` accepted for 7–14% wine, optional (N/A) for malt. The form accepts free-text strengths (`40`, `40%`, `40 %`, `40 pct`, `40% abv`). |
| Net contents | Unit-aware equality canonicalized to mL (`750 mL == 750ml == 0.75 L`; `5.17 US Gallon == 5.17 gal`). Trusts a structured reading alongside the transcription. |
| Sulfites | Wine only: a `Contains Sulfites` declaration must be present. |
| Country of origin | The application value is compared on its bare place name (`USA (Oregon)` ≡ `Oregon`) against what was read off the label, with a verbatim-text fallback. Any US state is shown as `USA` in the results (display only — the state is still used for matching). |
| Government warning | (a) **content**: each statutory unit (the prefix + the two numbered sentences) must be present with high fuzzy similarity — checked *order-independently*, because multi-column labels make readers return the sentences scrambled/interleaved; a removed or reworded sentence still drops below threshold and is caught; (b) **case**: the `GOVERNMENT WARNING:` prefix must be ALL CAPS exactly as read — title-case is flagged as a rejection. Reported as two separate checks. The anchor tolerates the two words being merged (`GOVERNMENTWARNING`). |

### Class-aware rules (`app/classes.py`)

Beyond verification the ALV system also performs some correctness checks.
The beverage class is inferred from the application's `class/type` text (ABV as a
fallback) and selects a rule profile, because TTB regulates the three commodities
differently (27 CFR Parts 5 / 4 / 7):

| Class | ABV on label | Sulfite declaration |
|---|---|---|
| **Spirits** | required (a missing numeric ABV is a mismatch) | — |
| **Wine** | required, but 7–14% may say **"Table Wine"** in lieu of a number | required |
| **Malt / beer** | optional → a missing ABV is **N/A**, not a failure | — |

The applied class is surfaced in the UI as a "Treated as: …" chip.

Verdicts render with **words + symbols + colour** (never colour alone):
`✓ Verified` / `✓ Verified*` (green), `✗ Mismatch` (red — the label states a value
the application doesn't, either *different* from or *absent* from the form),
`✗ Missing` (red — a required value the application gives is *absent* from the
label), `⚠ Not found` (yellow — blank on the form **and** absent on the label, so
there is nothing to verify), `? Partial Match` (yellow — names overlap but aren't
equal), and `— N/A` (not required for this class). Missing and Mismatch are kept
distinct so the reviewer sees *why* a field failed; only N/A is excluded from the
verified / needs-review counts. (A value present on the label but left **blank on
the form** is treated as a `✗ Mismatch` — the application is incomplete relative to
the label — rather than silently passing.)

The **"Found on label"** column shows the *exact text on the label* that each
field's pattern matched (not a separately-normalized value), so a reviewer can see
precisely what was read.

---

## OCR backend

The label reader is selected by the `OCR_BACKEND` env var; all backends feed the
same matching/warning rules, so verdicts don't change with the reader.

| `OCR_BACKEND` | Reader | Use |
|---|---|---|
| `gemini` | Hosted vision model | **Primary.** Needs `GEMINI_API_KEY` (optional `GEMINI_MODEL`, default `gemini-2.5-flash`; `GEMINI_TIMEOUT_S`; `GEMINI_CACHE_DIR`). Sends the image to the API. |
| `paddle` | PaddleOCR (CPU, on-device) | **Backup** for firewalled/offline deploys — nothing leaves the box. |
| `tesseract` | Tesseract (CPU, on-device) | Lighter backup where container size/RAM blocks PaddleOCR (needs the `tesseract` binary). |

Readings (text, not images) are cached by image SHA-256 — in memory plus an
on-disk JSON store under `GEMINI_CACHE_DIR` (default `<tmp>/ttb_gemini_cache`) —
and the cache is **wiped at program start**.

**Local-OCR backup notes:** PaddleOCR's CPU wheels are pathologically slow on
Apple Silicon (a single image's detection may not finish), so on an arm64 Mac use
`OCR_BACKEND=gemini` or `OCR_BACKEND=tesseract`; PaddleOCR is fast on the x86 Linux
container. Latency knobs for the local path: `MAX_OCR_SIDE` (default `1280`,
long-side cap — the biggest lever) and `OCR_USE_ANGLE_CLS` (default `true`; set
`false` to skip rotation correction). **Parallelism caveat:** front/back sides and
concurrent batch workers overlap on load/preprocess, but the PaddleOCR singleton
serializes the recognition call behind a lock — so the local backup sees less
speedup from parallelism than the network-bound Gemini default.

---

## Test data provenance

29 **real** labels from TTB's [Public COLA Registry](https://ttbonline.gov/colasonline/)
(public data, no auth), fetched by `scripts/fetch_test_images.py` as well as some home 
scanned labels:

The script is deliberately polite (sequential, 2 s delay, identifiable
User-Agent, 3 retries with backoff) and falls back to a manual-download
checklist if the site ever blocks automation — the app never depends on the
fetch succeeding.

**Applications data notes (honest):**
- The public structured record carries **no ABV and no net-contents field** —
  both live only on the label artwork. `abv`, `net_contents`, and
  `country_of_origin` have each been filled to ~**90%** by **reading the label
  images** (the keg collars list dual sizes). The remaining ~10% are left **blank
  on purpose** to exercise the blank-form paths (blank on the form but present on
  the label → `✗ Mismatch`; blank on the form **and** absent on the label →
  `⚠ Not found`).
- `country_of_origin` follows a fixed convention: a US state is written
  `USA (State)`, a country is written as-is (`Argentina`, `France`).
- `warning_expected` is `TRUE` for all rows (all alcohol labels require it).
- Each row names a `front` image and an optional `back` image. Many COLA records
  attach more than one label (front + back/neck); `scripts/fetch_back_labels.py`
  revisits each record by `ttb_id` and downloads the back when present. **17 of
  the 30 rows have a back**; single-label records (most beers/keg-collars) have an
  empty `back`. When a `back` is present, front+back text is merged before
  matching — both in the test suite and the app.

---

## Tests

```bash
pytest                                              # in the container / x86 Linux
pytest --ignore=tests/test_pipeline_integration.py  # on an Apple Silicon dev box
```

- **Deterministic suite (always runs, no model/OCR engine):** unit tests for every
  matching rule, every extraction regex, the class-aware ABV/sulfite rules, the
  country-of-origin and mandatory-field checks, and the warning case logic.
- **Adversarial suite:** for 5 real records, the pipeline core is fed a correct
  synthetic label, then the metadata is mutated (wrong ABV, altered brand, wrong
  net contents, tampered/missing warning) — and the tool is asserted to report
  the mismatch. *A verifier that can't fail is worthless.*
- **Fixture-driven integration (`tests/test_pipeline_integration.py`):** for each
  applications-data row with a downloaded image, the **full** pipeline runs with a
  real reader and asserts extraction + matching.


## Setup & run

### Local

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload              # open http://localhost:8000
```

The Gemini vision reader is the default — set `GEMINI_API_KEY` in the environment
(`docker compose` reads it from the host). To run fully offline instead, set
`OCR_BACKEND=paddle` (or `tesseract`); see [OCR backend](#ocr-backend).

Upload the front label (add the back if the warning/ABV is there), then fill the
**application fields** and click **Verify Label**:

- **Class / type** is a dropdown (Distilled Spirits / Wine / Beer-Malt) — the
  three TTB superclasses; the verdict still resolves the specific designation on
  the label (e.g. "Whiskey") via the lookup table.
- **Alcohol content (ABV)** accepts however an operator reads it — `40`, `40%`,
  `40 %`, `40 pct`, `40% abv` all parse to the number.
- **Load sample…** (pick Spirits / Wine / Beer, or a random one of each) pre-fills
  a real application row and its label image. **Clear Fields** resets the form and
  the uploaded images for a clean slate.

You can deep-link a specific test with **`/?ttb_id=<id>`**, which loads that
record's fields and image(s). Each result shows **TTB: {id}** (opens the record in
the public COLA registry); the batch view adds **SINGLE** (re-opens the record in
the single-label tab). The **PATTERN** debug toggle (generated matching pattern
per field + raw label text) is hidden unless the page is opened with **`?debug`**.

The 30 test labels are bundled in `test_images/`, so no fetch step is required
(`scripts/fetch_test_images.py` can re-pull them from the registry if needed).

### Tests

```bash
pytest
```

### Deployed (Railway)

[Railway](https://railway.app) builds the `Dockerfile` and runs the container,
injecting a `$PORT` the app already binds to (`uvicorn … --port ${PORT}`), so no
extra config file is needed.

- **Deployed URL:** _add your Railway URL here._

#### Deploy steps
1. Create a new Railway project → **Deploy from GitHub repo** (or run `railway up`
   from the repo). Railway auto-detects the `Dockerfile`.
2. The image bundles the app code and the sample `test_images/`, so the demo works
   out of the box.
3. Add a `GEMINI_API_KEY` service variable (the Gemini reader is the default; or
   set `OCR_BACKEND=paddle` for a fully self-contained, offline build).
4. Railway builds the container, injects `PORT`, and serves the app on its
   generated domain.

---

## Phase 2 — Batch verification

Open the **Batch** tab (`/batch.html`).

- **Step 1** takes the application CSV (data first), **Step 2** the label images.
  **Download Sample CSV** (`/sample-csv`) serves the bundled `ApplicationsData.csv`
  as `Application-Sample.csv` to edit and re-upload. Each row carries a `ttb_id`
  (the per-item designator shown in the results and COLA link — it need not match
  the image filename), a `front` image, and an optional `back` image (warning/ABV
  on the back); front+back text is merged before matching, like the single-label
  flow. (The CSV shape also lives in `CSV_TEMPLATE_COLUMNS`; `/batch/template.csv`
  serves an empty template.) **Load sample…** populates the files but no longer
  auto-runs — click **Verify all labels** to start.
- **Parallelized for time reduction (two levels).** A bounded pool verifies
  **up to `BATCH_CONCURRENCY` labels at once** (default **3**; set `1` for
  strictly sequential). The default Gemini reader is network-bound, so overlapping
  ~3 requests cuts a batch's wall-clock toward **a third** — without straining the
  server or the API, and with the cap also bounding how many images are held in
  memory at once. On top of that, **each label's front and back sides are read in
  parallel**, so a two-sided label costs roughly the *slower* side rather than the
  sum. The browser is untouched — one upload, then light polling — so it's never
  overwhelmed. Live per-item status: `pending → processing → verified / needs
  review / failed`.
- **Review pane:** "Show flagged only" lists just the items needing review.
- **CSV export** of all results.
- The single-label flow is unchanged and remains the primary path.

Batch state is in-memory and ephemeral (no database), consistent with the
no-persistence constraint.

---

## Out of scope & trade-offs

These are intentionally **not** built, each for a reason:

- **Authentication** — a prototype for evaluation; auth would add friction without
  exercising the core verification task.
- **COLA system integration** — the brief is offline verification; live
  integration is a separate, larger effort.
- **Persistence / database** — stakeholders asked for nothing to persist beyond
  the request; avoids data-handling/compliance surface for a prototype. (The only
  exception is the reading cache, keyed by image hash and wiped at program start.)
- **Image storage of uploads** — same reason; uploads are processed in memory and
  discarded.
- **Multi-language labels** — the dataset and statutory warning are English.
- **Exhaustive per-type rule coverage** — the class-aware engine
  (`app/classes.py`) already varies the ABV and sulfite requirements across
  spirits / wine / malt; the long tail of commodity-specific rules (vintage/
  appellation for wine, age statements for spirits) is a follow-on.
- **Warning-block layout extraction** — on some real labels the Government Warning
  is printed in a narrow multi-column block, or interleaved line-by-line with an
  adjacent column. The reader then returns the statutory sentences fragmented and
  out of order. The verifier still *locates* the warning (the anchor tolerates
  merged/scrambled casing) and checks each statutory sentence order-independently,
  but when the wording can't be confirmed it deliberately reports **"needs
  review"** rather than auto-verifying. **This is intentional:** loosening the
  content check enough to pass those labels would also let a *tampered* warning
  through (the adversarial suite asserts a reworded/missing warning is caught).
