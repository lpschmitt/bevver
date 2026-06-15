"""
FastAPI application: single-label verification (Phase 1).

One container, no database, no auth. Serves a single static page and a /verify
endpoint. All processing is local (see app.ocr). Errors are translated to plain
English for low-tech-comfort users — no stack traces, no OCR jargon.

Batch verification (Phase 2) lives in app.batch and is mounted below.
"""
from __future__ import annotations

import csv
import logging
import random
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app import gemini_backend, patterns
from app.classes import infer_class
from app.pipeline import Application, run_pipeline

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
APPLICATIONS_DATA = BASE_DIR.parent / "test_images" / "ApplicationsData.csv"

# Uploads larger than this are rejected before OCR (keeps latency bounded).
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".pdf", ".gif", ".bmp", ".tif", ".tiff"}

FRIENDLY_READ_ERROR = (
    "We couldn't read this image clearly. A straight-on photo with even "
    "lighting works best. Please try another image or a PDF of the label."
)

app = FastAPI(title="TTB Label Verifier", version="1.0")

# Start each run with a clean Gemini result cache (it's also empty on a fresh
# import; this makes the "reset at program start" explicit).
gemini_backend.reset_cache()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


async def _read_label(upload: UploadFile) -> tuple[bytes | None, JSONResponse | None]:
    """Validate one uploaded label and return (bytes, None) or (None, error)."""
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix and suffix not in ALLOWED_SUFFIXES:
        return None, _friendly_error(
            "That file type isn't supported. Please upload a JPG, PNG or PDF of "
            "the label.", status=415)

    data = await upload.read()
    if not data:
        return None, _friendly_error("The uploaded file was empty. Please choose "
                                     "a label image or PDF.", status=400)
    if len(data) > MAX_UPLOAD_BYTES:
        return None, _friendly_error("That file is too large. Please upload a "
                                     "label image under 20 MB.", status=413)
    return data, None


@app.post("/verify")
async def verify(
    file: UploadFile = File(...),
    back_file: Optional[UploadFile] = File(None),
    brand_name: str = Form(""),
    class_type: str = Form(""),
    abv: str = Form(""),
    net_contents: str = Form(""),
    country_of_origin: str = Form(""),
) -> JSONResponse:
    data, err = await _read_label(file)
    if err is not None:
        return err

    # Back label is optional. An empty file part (no file chosen) arrives with a
    # blank filename — treat that as "front only".
    back_data = None
    if back_file is not None and (back_file.filename or "").strip():
        back_data, err = await _read_label(back_file)
        if err is not None:
            return err

    application = Application(
        brand_name=brand_name.strip(),
        class_type=class_type.strip(),
        abv=abv.strip(),
        net_contents=net_contents.strip(),
        country_of_origin=country_of_origin.strip(),
    )

    try:
        result = run_pipeline(
            data, application,
            content_type=file.content_type or "",
            filename=file.filename or "",
            back_data=back_data,
            back_content_type=(back_file.content_type or "") if back_data else "",
            back_filename=(back_file.filename or "") if back_data else "",
        )
    except ValueError:
        # Decoding / unreadable image — expected, user-fixable.
        return _friendly_error(FRIENDLY_READ_ERROR, status=422)
    except Exception:
        log.exception("Unexpected pipeline error")
        return _friendly_error(
            "Something went wrong while reading this label. Please try again, "
            "or use a clearer image.", status=500)

    payload = result.to_dict()
    # The regex generated from each application value, for the "Patterns & text"
    # panel in the results view (the OCR text is already in result.to_dict()).
    payload["patterns"] = patterns.field_patterns(application)
    return JSONResponse(payload)


# Map the UI's sample-menu values to inferred beverage classes.
_SAMPLE_TYPE_ALIAS = {"spirits": "spirits", "wine": "wine", "beer": "malt", "malt": "malt"}

# Curated sample per class (by TTB ID); falls back to the richest matching row.
_SAMPLE_FEATURED = {"malt": "18017001000321"}   # 851 HELLES


@app.get("/sample")
def sample(type: str = "", ttb_id: str = "") -> JSONResponse:
    """
    Return one application row from the applications-data dataset so the UI's
    "Load sample" menu can pre-fill the form. With `ttb_id`, return that exact
    record (e.g. ?ttb_id=10210001000026, used to deep-link a specific test). With
    `type` (spirits/wine/beer), pick a representative row of that class; otherwise
    the first row with a back. The richest matching row (back label + country) is
    preferred so the demo exercises as many fields as possible.
    """
    if not APPLICATIONS_DATA.exists():
        return JSONResponse(
            {"error": "No sample data available. Run scripts/fetch_test_images.py."},
            status_code=404,
        )
    with APPLICATIONS_DATA.open(newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("front")]
    if not rows:
        return JSONResponse({"error": "No sample rows with images."}, status_code=404)

    # Specific record by TTB ID (a direct deep link to one test).
    wanted = (ttb_id or "").strip()
    if wanted:
        row = next((r for r in rows if (r.get("ttb_id") or "").strip() == wanted), None)
        if row is None:
            return JSONResponse({"error": f"No test found for TTB ID {wanted}."},
                                status_code=404)
        return _sample_response(row)

    type_l = (type or "").strip().lower()

    # "random" (any) or "random-<class>" (random within spirits/wine/beer).
    if type_l.startswith("random"):
        sub = type_l.partition("-")[2]
        pool = rows
        if sub:
            sub_target = _SAMPLE_TYPE_ALIAS.get(sub)
            pool = [r for r in rows
                    if sub_target and infer_class(r.get("class_type", "")).value == sub_target]
            if not pool:
                return JSONResponse({"error": f"No sample available for {sub}."},
                                    status_code=404)
        return _sample_response(random.choice(pool))

    target = _SAMPLE_TYPE_ALIAS.get(type_l)
    if target:
        typed = [r for r in rows
                 if infer_class(r.get("class_type", "")).value == target]
        if not typed:
            return JSONResponse({"error": f"No sample available for {type}."},
                                status_code=404)
        # Prefer the curated row for this class; else the richest matching row
        # (has back, then country of origin).
        featured_id = _SAMPLE_FEATURED.get(target)
        row = next((r for r in typed if r.get("ttb_id") == featured_id), None) \
            if featured_id else None
        if row is None:
            row = max(typed, key=lambda r: (bool(r.get("back")),
                                            bool(r.get("country_of_origin"))))
    else:
        # No type given: first row with a back label, else the first row.
        row = next((r for r in rows if r.get("back")), rows[0])
    return _sample_response(row)


def _sample_response(row: dict) -> JSONResponse:
    return JSONResponse({
        "ttb_id": row.get("ttb_id", ""),
        "front": row["front"],
        "back": row.get("back", ""),
        "brand_name": row.get("brand_name", ""),
        "class_type": row.get("class_type", ""),
        "abv": row.get("abv", ""),
        "net_contents": row.get("net_contents", ""),
        "country_of_origin": row.get("country_of_origin", ""),
    })


@app.get("/sample-csv")
def sample_csv() -> Response:
    """Serve the bundled ApplicationsData.csv as a downloadable sample template."""
    if not APPLICATIONS_DATA.exists():
        return JSONResponse({"error": "Sample CSV not found."}, status_code=404)
    return FileResponse(
        APPLICATIONS_DATA,
        media_type="text/csv",
        filename="Application-Sample.csv",
    )


@app.get("/sample-image/{name}")
def sample_image(name: str) -> Response:
    """Serve a bundled test label by filename so the demo runs in one click."""
    safe = Path(name).name   # strip any path components (no traversal)
    if Path(safe).suffix.lower() not in ALLOWED_SUFFIXES:
        return JSONResponse({"error": "Unsupported image name."}, status_code=400)
    path = APPLICATIONS_DATA.parent / safe
    if path.exists():
        return FileResponse(path)
    return JSONResponse({"error": "Sample image not found."}, status_code=404)


def _friendly_error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


# --- Phase 2: batch verification (mounted; single-label flow stays primary) ---
try:
    from app.batch import router as batch_router
    app.include_router(batch_router)
except Exception:  # batch is optional; never break the core app if it's absent
    log.info("Batch router not mounted.")

# Static UI is mounted last so API routes take precedence.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
