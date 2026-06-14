---
title: TTB Label Verifier
emoji: 🧾
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# TTB Alcohol Label Verification Prototype

A prototype of the core daily task of a TTB compliance agent: **verify a beverage
label against its application data**. Upload a label (image or PDF), enter the
application values, and the tool reads the label locally with OCR and reports a
per-field verdict — including the strict, exact check on the Government Health
Warning. All processing runs **inside the container**; no image or text ever
leaves the app, and there are no external ML/OCR calls.

> **Key design choice:** OCR runs locally (PaddleOCR, CPU) rather than via a
> cloud API. The agency firewall blocks external ML endpoints and their latency
> is unpredictable; a prior vendor failed exactly on the 5-second budget. Keeping
> everything local makes latency bounded and the tool usable behind the firewall.

---

## Stakeholder constraint → feature mapping

| Interview constraint | What was built |
|---|---|
| **~5 s latency or agents bypass the tool** | Fully local pipeline (PaddleOCR CPU). Per-stage timing is instrumented and the **total time is shown in the UI and the API response**. The integration test asserts < 5 s per label. |
| **No cloud OCR (firewall, unpredictable latency)** | All OCR/processing runs in-container; no external calls anywhere in the request path. |
| **Nuanced matching, not bare pass/fail** | Each field returns `{expected, found, verdict, confidence, note}`. Normalization is explained in plain language ("Match after normalizing case, punctuation and spacing"). |
| **"STONE'S THROW" == "Stone's Throw"** | Brand matching is case/punctuation/whitespace-insensitive and reports `match_normalized` with the reason. |
| **Government Warning must be exact, incl. ALL CAPS** | Dedicated strict check (`app/warning.py`): content is matched with OCR-noise tolerance, **and the `GOVERNMENT WARNING:` prefix case is checked separately** — title-case is reported as a mismatch/rejection. |
| **Different commodities have different mandatory fields** | Class-aware rules (`app/classes.py`): ABV is required for spirits, optional (N/A) for beer, and "Table Wine" is accepted for 7–14% wine; wine also requires a sulfite declaration. The applied class shows as a "Treated as: …" chip. |
| **Low tech-comfort users; simple UI** | Single screen, three explicit states, large labelled buttons (no bare icons), plain-English errors, no modals, no hidden state. |
| **Batch processing** | Phase 2: multi-file upload + CSV, sequential queue, live status, flagged-only review pane, CSV export. The single-label flow stays primary and untouched. |

---

## Architecture / pipeline

```
upload (image or PDF)
   │
   ├─ load            decode image, or render PDF first page to image (PyMuPDF)
   ├─ preprocess      grayscale → CLAHE contrast normalize → deskew (OpenCV)
   ├─ OCR             PaddleOCR (CPU; PP-OCRv3 detection + PP-OCRv4 recognition) → lines with text/confidence/bbox
   ├─ field extract   per-field strategies (not generic text search)
   ├─ field match     per-field rules + the strict warning check
   └─ results JSON    per-field verdicts + warning detail + per-stage timings
```

- `app/ocr.py` — image loading, preprocessing, and the OCR backend (PaddleOCR
  primary; Tesseract fallback via `OCR_BACKEND=tesseract`). The model is a cached
  singleton so it loads once per process.
- `app/gemini_backend.py` — **optional** cloud vision backend
  (`OCR_BACKEND=gemini`). A Gemini model reads the fields directly and returns a
  verbatim transcription; both flow through the same `matching` / `warning`
  rules, so verdicts are unchanged. Off by default — it sends the image off-box,
  which breaks the local-only constraint, so it is an explicit opt-in. Needs
  `GEMINI_API_KEY` (optional `GEMINI_MODEL`, `GEMINI_TIMEOUT_S`, `GEMINI_CACHE_DIR`).
  Readings are cached by image fingerprint (SHA-256), so an identical image isn't
  re-sent to the API — a two-tier cache (in-memory + an on-disk JSON store) that
  survives across requests/workers/restarts and is **wiped at program start**.
- `app/extraction.py` — field extraction strategies (still feed the verdicts).
- `app/patterns.py` — generates a regex per field from the record value, and the
  `matched_on_label()` helper that drives the exact "Found on label" display.
- `app/matching.py` — per-field matching rules + the verdict vocabulary.
- `app/classes.py` — beverage-class inference (spirits / wine / malt) and the
  per-class rule profiles that make ABV/sulfite checks class-aware.
- `app/warning.py` — the dedicated Government Health Warning verifier.
- `app/pipeline.py` — orchestration + timing. OCR is **injectable**, so the test
  suite exercises extraction/matching without loading any ML model.
- `app/main.py` — FastAPI app + static UI. `app/batch.py` — Phase 2 batch router.

### Field extraction strategies

| Field | Strategy |
|---|---|
| Brand name | Largest text block(s) by bounding-box height; the candidate closest to the application value is chosen so normalization can be explained. |
| ABV | Two regexes covering the common label phrasings in either order — number-first (`13.5% Alc/Vol`, `13.5% ABV`, `13.5% alcohol by volume`, `13.5% by volume`, `13.5 % vol`) and cue-first (`Alcohol 13.5%`, `ALC. 13.5% BY VOL.`, `Alc. by Vol. 5.2%`, `Alcohol content: 13.5%`). A bare `100%` with no alcohol cue is rejected. Plus a `\d{2,3}\s*proof` pattern. |
| Net contents | Volume regex canonicalized to millilitres (`mL`/`cL`/`L`/`fl oz`/pints/quarts/gallons), tolerating `US`/`U.S.`/`Imp.` gallon qualifiers. A faint volume the OCR garbles (`75O ML`) is rescued by tolerating digit confusions (`O→0`, `I→1`, `S→5`) — but **only** if the corrected value equals an expected one, so it can't invent a match. |
| Class/type | Fuzzy substring across all OCR text (may span lines). |
| Country of origin | An explicit cue first (`Product of France`, `Made in Scotland`); otherwise a US state or country name at the **end of a sentence/phrase** (e.g. `…Bottled by Otium Cellars, Waterford, Virginia`). A trailing ZIP is ignored, postal abbreviations count only when UPPERCASE (`VA`, not the word "or"), and a place name inside the brand name is skipped. US states resolve to the USA — results display just `USA` (the specific state is kept only internally, for matching). |
| Government warning | Located via the `GOVERNMENT WARNING` anchor, then verified. |

### Field matching rules

| Field | Rule |
|---|---|
| Brand name | A regex generated from the record value is searched across the whole label text (case/punctuation/whitespace-insensitive), so a brand inside `…Company LLC` or split across OCR lines still verifies; reports `match_normalized`. Multi-word brands match even when the OCR runs them together (`PullmanPILSNER`); single-word brands keep word boundaries so a short name can't fire inside a larger word. |
| Class/type | Fuzzy ratio ≥ 0.85 (rapidfuzz). |
| ABV | **Class-aware** (see below): numeric equality after extraction (`45 == 45.0`) with a proof cross-check when present (`proof = 2 × ABV`); strict for spirits, `Table Wine` accepted for 7–14% wine, optional (N/A) for malt. |
| Net contents | Unit-aware equality canonicalized to mL (`750 mL == 750ml == 0.75 L`; `5.17 US Gallon == 5.17 gal`). Trusts a structured reading (Gemini's `net_contents`) alongside the transcription. |
| Sulfites | Wine only: a `Contains Sulfites` declaration must be present. |
| Country of origin | The application value is compared on its bare place name (`USA (Oregon)` ≡ `Oregon`) against what was read off the label, with a verbatim-text fallback. Any US state is shown as `USA` in the results (display only — the state is still used for matching). |
| Government warning | (a) **content**: each statutory unit (the prefix + the two numbered sentences) must be present with high fuzzy similarity — checked *order-independently*, because multi-column labels make OCR return the sentences scrambled/interleaved; a removed or reworded sentence still drops below threshold and is caught; (b) **case**: the `GOVERNMENT WARNING:` prefix must be ALL CAPS exactly as OCR'd — title-case is flagged as a rejection. Reported as two separate checks. The anchor tolerates OCR merging the two words (`GOVERNMENTWARNING`). |

### Class-aware rules (`app/classes.py`)

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
`✓ Verified` / `✓ Verified*` (green), `✗ Mismatch` (red — the label states a
*different* value), `✗ Missing` (red — a required value the application gives is
*absent* from the label), `⚠ Not found` (yellow — blank on the form **and** absent
on the label, so there is nothing to verify), `? Partial Match` (yellow — names
overlap but aren't equal), `— N/A` (not required for this class), and `≈ Assumed`
(yellow — read off the label but left blank on the form). Missing and Mismatch are
kept distinct so the reviewer sees *why* a field failed; N/A and Assumed are
excluded from the verified / needs-review counts.

The **"Found on label"** column shows the *exact text on the label* that each
field's pattern matched (not a separately-normalized value), so a reviewer can see
precisely what was read.

---

## Test data provenance

29 **real** labels from TTB's [Public COLA Registry](https://ttbonline.gov/colasonline/)
(public data, no auth), fetched by `scripts/fetch_test_images.py`:

1. For each TTB ID, fetch the detail page (mints a session cookie) and parse the
   structured metadata.
2. Fetch the "Printable Version" page, whose HTML embeds the label artwork as
   `publicViewAttachment.do?filename=…&filetype=l` (the URL pattern was
   discovered by inspecting the page, not guessed).
3. Download each label with the session cookie + referer, and write
   `test_images/ApplicationsData.csv`.

The script is deliberately polite (sequential, 2 s delay, identifiable
User-Agent, 3 retries with backoff) and falls back to a manual-download
checklist if the site ever blocks automation — the app never depends on the
fetch succeeding.

**Applications data notes (honest):**
- The public structured record carries **no ABV and no net-contents field** —
  both live only on the label artwork. `abv`, `net_contents`, and
  `country_of_origin` have each been filled to ~**90%** by **reading the label
  images** (e.g. David James 18061001000436 is a 375 mL half-bottle; the keg
  collars list dual sizes). The remaining ~10% are left **blank on purpose** to
  exercise the blank-form paths (blank on the form but present on the label →
  `≈ Assumed`; blank on the form **and** absent on the label → `⚠ Not found`).
- `country_of_origin` follows a fixed convention: a US state is written
  `USA (State)`, a country is written as-is (`Argentina`, `France`).
- `warning_expected` is `TRUE` for all rows (all alcohol labels require it).
- Each row names a `front` image and an optional `back` image. Many COLA records
  attach more than one label (front + back/neck); `scripts/fetch_back_labels.py`
  revisits each record by `ttb_id` and downloads the back when present (preferring
  the attachment whose filename says "back"). **17 of the 30 rows have a back**
  (16 from the registry + the hand-added Cointreau pair); single-label records
  (most beers/keg-collars) have an empty `back`. When a `back` is present,
  front+back OCR is merged before matching — both in the test suite and the app.

---

## Tests

```bash
pytest                                              # in the container / x86 Linux
pytest --ignore=tests/test_pipeline_integration.py  # on an Apple Silicon dev box
```

(The integration test drives real PaddleOCR, which hangs on Apple Silicon —
exclude it locally; it runs in the container/CI.)

- **Deterministic suite (always runs, no ML model):** unit tests for every
  matching rule, every extraction regex, the class-aware ABV/sulfite rules, the
  country-of-origin and mandatory-field checks, and the warning case logic.
- **Adversarial suite:** for 5 real records, the pipeline core is fed a correct
  synthetic label, then the metadata is mutated (wrong ABV, altered brand, wrong
  net contents, tampered/missing warning) — and the tool is asserted to report
  the mismatch. *A verifier that can't fail is worthless.*
- **Fixture-driven integration (`tests/test_pipeline_integration.py`):** for each
  applications-data row with a downloaded image, the **full** pipeline runs with real
  OCR and asserts extraction + matching, **and asserts < 5 s per label**. It is
  skipped automatically when no OCR backend/OpenCV is installed (e.g. a bare
  laptop), and runs in the container/CI where PaddleOCR is present.

**Honest reporting:** old scanned labels (2005–2012 IDs) are known to be hard for
OCR. They are marked `xfail(strict=False)` — they pass if OCR reads them and do
**not** fail the suite if it doesn't. Thresholds were **not** loosened to force
green.

### Verified locally vs. measured on the deploy target

- **Deterministic + adversarial suites: 126 passed, 0 failed** locally
  (`pytest --ignore=tests/test_pipeline_integration.py`), with no ML model
  required. These prove all matching/extraction/warning logic — class-aware ABV,
  country-of-origin extraction, the mandatory-field checks — including that the
  verifier **catches** wrong ABV, altered brand, wrong net contents, and
  tampered/missing warnings.
- **Real-OCR pipeline:** verified to run end-to-end (image decode → preprocess →
  PaddleOCR → extract → match); the model loads in ~0.5–2.8 s.
- **Latency / full-dataset pass rate:** these must be measured on the **Linux
  deploy target**, not this repo's dev box. `paddlepaddle`'s CPU wheels are
  known to be pathologically slow on Apple Silicon (a single image's text
  detection did not complete in minutes here), which is unrepresentative of the
  x86 Linux container where PP-OCR runs in well under a second per image.
  `scripts/measure_pass_rate.py` prints per-image timing + verdicts and a
  summary; run it (or `pytest tests/test_pipeline_integration.py`) in the
  container to fill in the numbers below.

> **Deploy-target results (fill in from a container run):**
> `images: __  ·  brand verified: __/29  ·  latency avg/max: __s/__s  ·  within 5 s: __/29`

When a label has a **front and a back image, the two are OCR'd in parallel**
(threads), so a two-sided label costs roughly the slower side rather than the
sum. The Gemini and Tesseract backends parallelize freely (independent HTTP
calls / subprocesses); the PaddleOCR singleton is not safe for concurrent
inference, so its recognition call is serialized by a lock (the load/preprocess
of both sides still overlap).

#### Latency tuning knobs (env vars)
- `MAX_OCR_SIDE` (default `1280`) — long-side cap before OCR; the single biggest
  latency lever for high-res registry artwork.
- `OCR_USE_ANGLE_CLS` (default `true`) — set `false` to skip rotation correction
  and roughly halve OCR time for upright scans.
- `OCR_BACKEND` (`paddle` | `tesseract` | `gemini`). `gemini` is a cloud vision
  backend (off by default; needs `GEMINI_API_KEY`, optional `GEMINI_MODEL`,
  `GEMINI_TIMEOUT_S`) — sends the image off-box, so use only where that's allowed.

---

## Setup & run

### Local (Docker — recommended)

```bash
docker compose up --build
# open http://localhost:8000
```

Use **Load sample…** (a menu — pick Spirits / Wine / Beer, or a random one of
each) to pre-fill a real application row and its label image, then **Verify
Label**. You can also deep-link a specific test with **`/?ttb_id=<id>`** (e.g.
`/?ttb_id=10210001000026`), which loads that record's fields and image(s) on
load.

Each result carries small operator links (top-right): **TTB: {id}** opens the
record in the public COLA registry, and **PATTERN** reveals the generated
matching pattern per field plus the raw OCR label text. In the batch view each
row adds **SINGLE**, which re-opens that record in the single-label tab for
focused re-testing.

### Local (without Docker)

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload              # open http://localhost:8000
```

The 30 test labels are bundled in `test_images/`, so no fetch step is required
(`scripts/fetch_test_images.py` can re-pull them from the registry if needed).

> **Apple Silicon / local dev:** the default PaddleOCR CPU wheel is pathologically
> slow on arm64 Macs (a single image's text detection may not complete). Run with
> `OCR_BACKEND=tesseract` (install the `tesseract` binary) or `OCR_BACKEND=gemini`
> (cloud; set `GEMINI_API_KEY`) locally. PaddleOCR is fast on the x86 Linux
> deploy container.

### Tests

```bash
pytest
```

### Deployed

- **HuggingFace Spaces (Docker SDK).** The frontmatter at the top of this README
  is the Space config (`sdk: docker`, `app_port: 7860`). The free tier RAM fits
  PaddleOCR.
- **Deployed URL:** _add your Space URL here._
- Note: free Spaces sleep after ~48 h idle — open the link once to wake it
  before sharing.

#### Deploy steps
1. Create a new Space → SDK: **Docker**.
2. Push this repository to the Space (it contains the `Dockerfile`, app code, and
   the bundled `test_images/` so the demo works out of the box).
3. The Space builds the container and serves on port 7860.

---

## Phase 2 — Batch verification

Open **"Verify many labels at once (batch)"** from the main page (`/batch.html`).

- Upload up to 50 label files + a CSV of application rows. Each row carries a
  `ttb_id` (the per-item designator shown in the results and the COLA link — it
  need not match the image filename), a `front` image, and an optional `back`
  image (for labels whose Government Warning or ABV is on the back); front+back
  OCR is merged before matching, the same as the single-label flow. (The CSV
  shape lives in `CSV_TEMPLATE_COLUMNS`; `/batch/template.csv` still serves it.)
- A single server-side worker processes the queue **sequentially** (OCR is
  CPU/memory-bound; parallelism would blow the latency/RAM budget). The client
  polls for live per-item status: `pending → processing → verified / needs review
  / failed`.
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
  the request; avoids data-handling/compliance surface for a prototype.
- **Image storage of uploads** — same reason; uploads are processed in memory and
  discarded.
- **Multi-language labels** — the dataset and statutory warning are English; PP-OCR
  is configured for English to keep the model small and fast.
- **Exhaustive per-type rule coverage** — the class-aware engine (`app/classes.py`)
  already varies the ABV and sulfite requirements across spirits / wine / malt;
  the long tail of commodity-specific rules (e.g. vintage/appellation for wine,
  age statements for spirits) is a follow-on.
- **OCR fallback (Tesseract)** is wired (`OCR_BACKEND=tesseract`) in case
  container size/RAM ever blocks PaddleOCR on the deploy target; PaddleOCR is the
  default for accuracy.
- **Warning-block layout extraction** — on some real labels the Government Warning
  is printed in a narrow multi-column block, or interleaved line-by-line with an
  adjacent column (e.g. keg-collar safety text). CPU OCR then returns the
  statutory sentences fragmented and out of order. The verifier still *locates*
  the warning (the anchor tolerates merged/scrambled casing) and checks each
  statutory sentence order-independently, but when the wording can't be confirmed
  from scrambled OCR it deliberately reports **"needs review"** rather than
  auto-verifying. **This is intentional:** loosening the content check enough to
  pass those labels would also let a *tampered* warning through (the adversarial
  suite asserts a reworded/missing warning is caught). De-interleaving the warning
  column into its own OCR pass is a worthwhile follow-on; it is not done here so
  the verifier never rubber-stamps a warning it could not actually read.
