# AGENT.md

Orientation for an AI agent working in this repo. Read this before making changes.
For the product/stakeholder story, see [README.md](README.md).

## What this is

**Bevver** — a TTB alcohol-label verification prototype. FastAPI backend +
vanilla-JS frontend. A user uploads a beverage label (front, optional back),
enters the application data, and the tool OCRs the label and reports a
**per-field verdict** — including a strict check of the Government Health Warning.

## Run it locally (the one gotcha that matters)

```bash
pip install -r requirements.txt
OCR_BACKEND=gemini GEMINI_API_KEY=... uvicorn app.main:app --reload   # http://localhost:8000
```

⚠️ **PaddleOCR (the default backend) hangs on Apple Silicon** — its CPU wheel
spins at 100% in `ZeroCopyRun()` and never returns. On a Mac dev box you **must**
set `OCR_BACKEND=gemini` (cloud, needs `GEMINI_API_KEY`) or `OCR_BACKEND=tesseract`
(local, needs the `tesseract` binary). Paddle is correct and fast only on the
x86 Linux deploy container. The user runs the **gemini** backend.

- `GEMINI_API_KEY` lives in the user's shell env. **Never** put it in code, tests,
  or commits, and don't ask for it.
- Run the server **from the repo root** (`cd ~/devl/Bevver`), or imports fail
  with `ModuleNotFoundError: No module named 'app'`.

## Test it

```bash
python3 -m pytest --ignore=tests/test_pipeline_integration.py   # 126 deterministic tests, no ML model, <1s
```

- The deterministic suite never loads an OCR model — OCR is **injectable** in
  `pipeline.py`, so extraction/matching/warning logic is tested directly.
- **`tests/test_pipeline_integration.py` needs real PaddleOCR and hangs on Apple
  Silicon — always exclude it locally.** It runs in the container/CI.
- `python3` on this box (not `python`).

## Architecture

```
upload → load → preprocess → OCR → field extract → field match → results JSON
```

| File | Responsibility |
|---|---|
| `app/main.py` | FastAPI app, single-label `/verify`, `/sample`, static UI |
| `app/batch.py` | Phase-2 batch router (CSV-driven queue, in-memory, ephemeral) |
| `app/pipeline.py` | Orchestration + timing; **OCR is injectable**; parallel front/back OCR; class inference |
| `app/ocr.py` | Image load, preprocess (grayscale/CLAHE/deskew), PaddleOCR/Tesseract backend (singleton + lock) |
| `app/gemini_backend.py` | Optional cloud vision backend (`OCR_BACKEND=gemini`) — structured read + verbatim text; **two-tier (memory+disk) result cache keyed by image SHA-256** |
| `app/extraction.py` | Field extraction strategies (brand, ABV, proof, net contents, **country of origin**) — still used for verdicts |
| `app/patterns.py` | **Per-field regex generation** from the record value (brand/class/ABV/net/country), `field_patterns()` (introspection), `matched_on_label()` (exact "Found on label" text) |
| `app/matching.py` | Per-field matching rules + verdict vocabulary + `normalize_text`. Model: **generate a regex from the record value (`patterns.py`) and search it across the label text** |
| `app/classes.py` | Beverage-class inference (`Bev`, `infer_class`) + per-class `ClassRules`/`PROFILES` |
| `app/warning.py` | Dedicated strict Government Health Warning verifier |
| `app/static/` | `index.html` (single), `batch.html`, `app.js`, `batch.js`, `style.css` |

Both the Paddle/Tesseract path (`_extract_and_match`) and the Gemini path
(`_extract_and_match_gemini`) feed the **same** `matching`/`warning` rules, so
verdicts are backend-independent. If you add a field, wire it into **both**.

## Domain model you must know

**Verdict vocabulary** (`app/matching.py`) — rendered as **word + symbol + colour**,
never colour alone. Frontend `VERDICT_META` in `app.js` and `.verdict-*` classes
in `style.css` must stay in sync with these:

| Constant | UI | Meaning |
|---|---|---|
| `match` / `match_normalized` | ✓ Verified / Verified* (green) | Exact / matched after normalizing case-punctuation-spacing |
| `mismatch` | ✗ Mismatch (red) | The label **states a value, but it differs** from the application |
| `missing` | ✗ Missing (red) | A required value the application gives is **absent from the label** |
| `not_found` | ⚠ Not found (**yellow**, keeps the ⚠ triangle) | Nothing to verify: blank in the application **and** absent on the label |
| `partial_match` | ? Partial Match (yellow) | Names overlap but aren't the same (extra/missing words) |
| `not_applicable` | — N/A (grey) | Field not required for this beverage class |
| `assumed` | ≈ Assumed (yellow) | Read off the label, but blank in the form — nothing to verify against |

**`missing` vs `mismatch`** is deliberate: `missing` = the label lacks the value;
`mismatch` = the label has it but it disagrees (class/type and country can be a
mismatch only when the label states a *different recognized* value). `not_found`
became **yellow** (was red) once `missing` took over the red "required but absent"
case. `summary()` in `pipeline.py` **excludes `not_applicable` and `assumed`** from
the verified/needs-review counts; `missing` counts as needs-review.

**Beverage classes** (`app/classes.py`): `spirits` / `wine` / `malt` / `unknown`.
ABV scoring is class-aware — required for spirits, "Table Wine" allowed for 7–14%
wine, optional (N/A) for malt. Wine additionally requires a sulfite declaration.

**Front/back merge**: when a label has a back image, both are OCR'd (in parallel)
and the lines merged front-then-back before extraction/matching.

## Matching & display details

- **"Found on label" shows the exact matched text.** `pipeline._show_matched_label_text`
  overrides each field's `found` (display only — verdicts/notes untouched) with the
  literal label substring its generated pattern hit, via `patterns.matched_on_label`.
  The old extraction machinery still runs for the verdict; it's just no longer the
  display. Domestic country is left on its `USA` display (a bare state/abbr token
  collides with business suffixes like "…Co.").
- **Brand** is searched as a generated regex across the whole label text (not just
  the tallest block), so a brand inside `…Company LLC` or split across lines still
  verifies. **Multi-word** brands are searched **unguarded** (the inter-word `\W*`
  prevents false hits and keeps the verdict consistent with the display, e.g.
  `PullmanPILSNER`); **single-word** brands keep word boundaries so a short name
  can't fire inside a larger word (`Cain` in `Cocaine`).
- **Net contents** canonicalizes to mL. It also: parses `US`/`U.S.`/`Imp.` gallon
  qualifiers; trusts a structured reading (Gemini's `net_contents`) alongside the
  transcription; and, only when strict parsing finds nothing, runs an **OCR-digit
  rescue** (`O→0`, `I→1`, `S→5`) that counts **only if** the corrected value equals
  an expected volume — so a faint `75O ML` verifies without inventing a match.

## Operator affordances (UI)

- **Per-entry debugging links** (top-right of a single-label result and each
  expanded batch row): **TTB: {id}** (opens the public COLA registry), **SINGLE**
  (batch only — re-opens that record's values + images in the single-label tab via
  a sessionStorage handoff), **PATTERN** (toggles a panel showing each field's
  generated regex + the OCR label text). Keep `VERDICT_META` (`app.js`/`batch.js`)
  and `.verdict-*` (`style.css`) — including **`missing`** — in sync with the
  backend vocabulary.
- **`/?ttb_id=<id>`** deep-links a specific test: the single-label page loads that
  record's fields + image(s) on startup (served by `/sample?ttb_id=`).

## Gemini result cache

`gemini_backend` caches readings keyed by **SHA-256 of the image bytes** (+ mime +
model): an in-memory dict in front of an on-disk JSON store (`GEMINI_CACHE_DIR`,
default `<tmp>/ttb_gemini_cache`), so identical images aren't re-sent across
requests/workers/restarts. `reset_cache()` clears **both** tiers and is called at
app startup, so each run starts empty. Note: this writes label readings to a temp
dir, a deliberate, startup-wiped exception to "nothing persists".

## Data / applications data

`test_images/ApplicationsData.csv` — 30 real COLA rows + bundled label images.
Columns: `ttb_id, front, back, brand_name, fanciful_name, class_type, origin,
net_contents, approval_date, abv, warning_expected, country_of_origin`.

- `front`/`back` name the bundled image files (`back` empty for single-label rows).
- `abv`, `net_contents`, `country_of_origin` are each ~**90% filled** (the rest left
  blank on purpose, to exercise the blank-form → "assumed"/"not found" paths).
  Values that aren't in the public COLA record were **read off the label artwork**.
- `country_of_origin` convention: a US state → `USA (State)`; a country → as-is.
- The batch CSV (`CSV_TEMPLATE_COLUMNS` in `batch.py`) is the application subset:
  `ttb_id, front, back, brand_name, class_type, abv, net_contents,
  country_of_origin`. **`ttb_id` is the per-item designator** shown in the batch
  results and the TTB-registry link — it need not equal the image filename (e.g.
  Cointreau is `X4119001000041` / `IMG_7458.jpg`); it falls back to the filename
  when a row omits it. (`/batch/template.csv` still exists but its UI download link
  was removed.)

The COLA registry (`ttbonline.gov`) is fetched via **system `curl`** in the
`scripts/`, not Python `requests` — the box's LibreSSL + urllib3 v2 combo resets
the TLS connection. The public record has **no** ABV or net-contents field; those
live only on the label image.

## Conventions & guardrails

- **Local-only is the headline constraint.** Paddle/Tesseract keep the image
  in-box. `gemini` sends it off-box and is an explicit, documented opt-in — never
  make a cloud backend the default.
- **Nothing persists** — no DB, uploads processed in memory and discarded. Batch
  jobs are in-memory and die with the process.
- **Plain-English UI for low-tech-comfort users** — labelled buttons, no bare
  icons, no modals, no hidden state.
- **Match the surrounding code.** Files carry dense explanatory comments about
  *why*; keep that density. Frontend is dependency-free vanilla JS.
- **Bump the asset version** (`?v=N` on `app.js`/`style.css` in `index.html` and
  `batch.html`) whenever you change a static asset, or the browser serves stale.
- The single-label flow is the **primary** path — keep it working; batch is
  secondary.

## Verifying UI changes

Use the `Claude_Preview` MCP tools (not Chrome/computer-use) against the user's
running Gemini server on `:8000`. Typical loop: `preview_start` → navigate →
`preview_eval`/`preview_snapshot` to inspect → `preview_screenshot` for proof.
Stop the preview server when done.

## Workflow

- **Commit/push only when the user asks.** If on `main`, branch first.
- Commit-message trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- `NOTES.md` is the user's running to-do/assumptions list — append, don't restructure.
