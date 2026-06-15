"""
Phase 2 — batch verification.

Multiple label files paired with a CSV of application rows keyed by filename.
Items are processed by a bounded pool of `BATCH_CONCURRENCY` workers (default 3):
the default Gemini reader is network-bound, so a few in-flight requests overlap
without straining the server, while the cap bounds both API pressure and the
number of images held in memory at once. (The local-OCR backup serializes its
recognition behind a lock, so it stays safe at any cap — just with less speedup.)
Set `BATCH_CONCURRENCY=1` to restore strict sequential processing. The client
polls for live per-item status. Everything is in-memory and ephemeral —
consistent with the "no database, nothing persists" constraint; a job is
forgotten when the process restarts.

The single-label flow (app.main) is untouched and remains primary.
"""
from __future__ import annotations

import csv
import io
import os
import random
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import JSONResponse, Response

from app import patterns
from app.classes import infer_class
from app.pipeline import Application, run_pipeline

router = APIRouter(prefix="/batch")

MAX_FILES = 50

# How many labels are verified concurrently. The default Gemini reader is
# network-bound, so a small overlap shortens a batch without straining the server
# or the API; 1 restores strict sequential processing.
BATCH_CONCURRENCY = max(1, int(os.environ.get("BATCH_CONCURRENCY", "3")))

# Bundled applications-data dataset, used to assemble the "Load sample" batches.
APPLICATIONS_DATA = Path(__file__).resolve().parent.parent / "test_images" / "ApplicationsData.csv"
SAMPLE_SET_SIZE = 6  # how many labels a sample batch contains

# Per-item lifecycle states surfaced to the UI.
PENDING = "pending"
PROCESSING = "processing"
VERIFIED = "verified"
NEEDS_REVIEW = "needs review"
FAILED = "failed"

CSV_TEMPLATE_COLUMNS = ["ttb_id", "front", "back", "brand_name", "class_type",
                        "abv", "net_contents", "country_of_origin"]


@dataclass
class BatchItem:
    filename: str                       # the front image filename (the upload key)
    application: Application
    data: bytes
    ttb_id: str = ""                    # COLA serial — the per-item designator
    content_type: str = ""
    back_data: bytes | None = None      # optional back label (e.g. warning side)
    back_content_type: str = ""
    back_filename: str = ""
    note: str = ""                      # intake note, e.g. a named back not uploaded
    status: str = PENDING
    summary: dict | None = None
    fields: list | None = None
    warning: dict | None = None
    beverage_class: str = ""
    ocr_text: str = ""                  # merged front+back label text (drill-down)
    timing_s: float | None = None
    error: str | None = None

    def public(self) -> dict:
        return {
            "filename": self.filename,
            "ttb_id": self.ttb_id,
            "status": self.status,
            "summary": self.summary,
            "timing_s": self.timing_s,
            "error": self.error,
            "note": self.note,
        }


@dataclass
class BatchJob:
    job_id: str
    items: list[BatchItem]
    done: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def progress(self) -> dict:
        total = len(self.items)
        finished = sum(1 for it in self.items
                       if it.status in (VERIFIED, NEEDS_REVIEW, FAILED))
        flagged = sum(1 for it in self.items if it.status == NEEDS_REVIEW)
        failed = sum(1 for it in self.items if it.status == FAILED)
        return {
            "job_id": self.job_id,
            "total": total,
            "finished": finished,
            "flagged": flagged,
            "failed": failed,
            "done": self.done,
            "items": [it.public() for it in self.items],
        }


# In-memory job registry. Ephemeral by design.
_JOBS: dict[str, BatchJob] = {}
_JOBS_LOCK = threading.Lock()


def _parse_application_csv(raw: bytes) -> list[dict]:
    """Parse the application CSV into ordered per-product specs.

    Each usable row becomes ``{"front", "back", "application"}`` — one product,
    keyed (by the caller) on its front image filename, with an optional back
    image filename. Rows without a ``front`` are skipped.
    """
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    specs: list[dict] = []
    for row in reader:
        front = (row.get("front") or "").strip()
        if not front:
            continue
        specs.append({
            "front": front,
            "back": (row.get("back") or "").strip(),
            "ttb_id": (row.get("ttb_id") or "").strip(),
            "application": Application(
                brand_name=(row.get("brand_name") or "").strip(),
                class_type=(row.get("class_type") or "").strip(),
                abv=(row.get("abv") or "").strip(),
                net_contents=(row.get("net_contents") or "").strip(),
                country_of_origin=(row.get("country_of_origin") or "").strip(),
            ),
        })
    return specs


def _process_item(job: BatchJob, item: BatchItem) -> None:
    """Verify a single label and record its result on the item (thread-safe)."""
    with job.lock:
        item.status = PROCESSING
    try:
        result = run_pipeline(
            item.data, item.application,
            content_type=item.content_type, filename=item.filename,
            back_data=item.back_data,
            back_content_type=item.back_content_type,
            back_filename=item.back_filename,
        )
        summary = result.summary()
        with job.lock:
            item.summary = summary
            item.fields = [f.to_dict() for f in result.fields]
            item.warning = result.warning
            item.beverage_class = result.beverage_class
            item.ocr_text = result.ocr_text
            item.timing_s = result.timings.to_dict()["total_s"]
            item.status = VERIFIED if summary["all_clear"] else NEEDS_REVIEW
    except Exception:  # keep going; one bad file shouldn't stop the batch
        with job.lock:
            item.status = FAILED
            item.error = "Could not read this label clearly."
            item.timing_s = None
    finally:
        # Free the image bytes once processed to bound memory.
        item.data = b""
        item.back_data = None


def _worker(job: BatchJob) -> None:
    """Process a job's items with up to BATCH_CONCURRENCY in flight at once.

    Items are submitted in list order (so earlier rows tend to start first) but
    complete out of order; the UI re-renders the full list each poll, keyed per
    row, so ordering of completion doesn't matter.
    """
    workers = min(BATCH_CONCURRENCY, len(job.items)) or 1
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix=f"batch-{job.job_id}") as pool:
        # `map` blocks until every item is done; exceptions are already handled
        # inside _process_item, so none escape here.
        list(pool.map(lambda it: _process_item(job, it), job.items))
    job.done = True


@router.get("/template.csv")
def template_csv() -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_TEMPLATE_COLUMNS)
    # front-only product, then a front+back product (warning on the back). The
    # ttb_id is the per-item designator; it need not match the image filename.
    writer.writerow(["15141001000396", "15141001000396.jpg", "",
                     "Calvert Brewing Company", "Beer", "", "750 mL", ""])
    writer.writerow(["X4119001000041", "IMG_7458.jpg", "IMG_7459.jpg",
                     "Cointreau", "Liqueur", "40", "750 mL", "France"])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=batch_template.csv"},
    )


def _sample_row(row: dict) -> dict:
    """One applications-data row reduced to the batch CSV's application columns."""
    return {
        "ttb_id": row.get("ttb_id", ""),
        "front": row.get("front", ""),
        "back": row.get("back", ""),
        "brand_name": row.get("brand_name", ""),
        "class_type": row.get("class_type", ""),
        "abv": row.get("abv", ""),
        "net_contents": row.get("net_contents", ""),
        "country_of_origin": row.get("country_of_origin", ""),
    }


def _fixed_sample(rows: list[dict]) -> list[dict]:
    """A deterministic, class-balanced selection (so the demo always matches).

    Rows are bucketed by inferred beverage class and drawn round-robin —
    richest first (those with a back label, then by ttb_id) — so a Fixed set
    spans spirits/wine/beer and exercises the front+back path.
    """
    order = ["spirits", "wine", "malt", "unknown"]
    buckets: dict[str, list[dict]] = {c: [] for c in order}
    for r in sorted(rows, key=lambda x: (not x.get("back"), x.get("ttb_id", ""))):
        buckets.setdefault(infer_class(r.get("class_type", "")).value, []).append(r)
    chosen: list[dict] = []
    while len(chosen) < SAMPLE_SET_SIZE:
        progressed = False
        for c in order:
            if buckets.get(c):
                chosen.append(buckets[c].pop(0))
                progressed = True
                if len(chosen) >= SAMPLE_SET_SIZE:
                    break
        if not progressed:
            break
    return chosen


@router.get("/sample")
def batch_sample(set: str = "fixed") -> JSONResponse:
    """Manifest for a sample batch: a Fixed (curated) set, a Random set, or All
    rows in the dataset.

    The client turns this into a CSV + image uploads and posts it back through
    the normal `/batch` flow, so a sample runs exactly like a user's own batch.
    """
    if not APPLICATIONS_DATA.exists():
        return JSONResponse(
            {"error": "No sample data available. Run scripts/fetch_test_images.py."},
            status_code=404)
    with APPLICATIONS_DATA.open(newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("front")]
    if not rows:
        return JSONResponse({"error": "No sample rows with images."}, status_code=404)

    kind = (set or "fixed").strip().lower()
    if kind == "all":
        chosen = rows
    elif kind == "random":
        chosen = random.sample(rows, min(SAMPLE_SET_SIZE, len(rows)))
    else:
        kind = "fixed"
        chosen = _fixed_sample(rows)
    return JSONResponse({"set": kind, "rows": [_sample_row(r) for r in chosen]})


@router.post("")
async def start_batch(
    files: list[UploadFile] = File(...),
    csv_file: UploadFile = File(...),
) -> JSONResponse:
    if not files:
        return JSONResponse({"error": "Please choose at least one label file."},
                            status_code=400)
    if len(files) > MAX_FILES:
        return JSONResponse(
            {"error": f"Please upload at most {MAX_FILES} files at a time."},
            status_code=413)

    specs = _parse_application_csv(await csv_file.read())
    if not specs:
        return JSONResponse(
            {"error": "The application CSV had no usable rows. Use the template "
                      "and include a 'front' column."},
            status_code=400)

    # Index the uploaded files by name so each CSV row can pull its front (and
    # optional back) image. Only the labels the user actually selected are
    # processed: a CSV row whose front image wasn't uploaded is skipped, not
    # listed — the upload set, intersected with the CSV, drives the work list.
    uploads: dict[str, tuple[bytes, str]] = {}
    for up in files:
        uploads[up.filename or ""] = (await up.read(), up.content_type or "")

    items: list[BatchItem] = []
    for spec in specs:
        front = uploads.get(spec["front"])
        if front is None:
            # Front image for this row wasn't selected — skip it entirely.
            continue
        back = uploads.get(spec["back"]) if spec["back"] else None
        # A back named in the CSV but absent from the upload would otherwise be
        # dropped silently — flag it so a missing warning/ABV is explained.
        note = ""
        if spec["back"] and back is None:
            note = (f"Back image '{spec['back']}' is named in the CSV but was not "
                    f"uploaded — verified the front only.")
        items.append(BatchItem(
            filename=spec["front"],
            ttb_id=spec["ttb_id"],
            application=spec["application"],
            data=front[0],
            content_type=front[1],
            back_data=back[0] if back else None,
            back_content_type=back[1] if back else "",
            back_filename=spec["back"] if back else "",
            note=note,
        ))

    if not items:
        return JSONResponse(
            {"error": "None of the selected files matched a row in the "
                      "application CSV. Match is by the 'front' filename."},
            status_code=400)

    job = BatchJob(job_id=uuid.uuid4().hex[:12], items=items)
    with _JOBS_LOCK:
        _JOBS[job.job_id] = job

    threading.Thread(target=_worker, args=(job,), daemon=True).start()
    return JSONResponse({"job_id": job.job_id, "total": len(items)})


@router.get("/{job_id}")
def batch_status(job_id: str) -> JSONResponse:
    job = _JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "Unknown or expired job."}, status_code=404)
    with job.lock:
        return JSONResponse(job.progress())


@router.get("/{job_id}/item/{index}")
def batch_item(job_id: str, index: int) -> JSONResponse:
    """Full per-label detail (field verdicts + warning) for the drill-down view.

    Kept out of the polling payload (`public()`) so live status stays light; the
    UI fetches this only when a row is expanded.
    """
    job = _JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "Unknown or expired job."}, status_code=404)
    with job.lock:
        if not 0 <= index < len(job.items):
            return JSONResponse({"error": "No such label in this job."}, status_code=404)
        it = job.items[index]
        return JSONResponse({
            "filename": it.filename,
            "ttb_id": it.ttb_id,
            "back_filename": it.back_filename,
            "note": it.note,
            "status": it.status,
            "summary": it.summary,
            "fields": it.fields,
            "warning": it.warning,
            "beverage_class": it.beverage_class,
            "timing_s": it.timing_s,
            "error": it.error,
            # Generated regex per field + the OCR label text, for the
            # "Patterns & text" drill-down panel.
            "ocr_text": it.ocr_text,
            "patterns": patterns.field_patterns(it.application),
            "application": {
                "brand_name": it.application.brand_name,
                "class_type": it.application.class_type,
                "abv": it.application.abv,
                "net_contents": it.application.net_contents,
                "country_of_origin": it.application.country_of_origin,
            },
        })


@router.get("/{job_id}/export.csv")
def batch_export(job_id: str) -> Response:
    job = _JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "Unknown or expired job."}, status_code=404)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "ttb_id", "front", "back", "status", "verified_fields", "total_fields",
        "needs_review", "processing_time_s", "error",
    ])
    with job.lock:
        for it in job.items:
            s = it.summary or {}
            writer.writerow([
                it.ttb_id, it.filename, it.back_filename, it.status,
                s.get("verified", ""), s.get("total", ""),
                s.get("needs_review", ""), it.timing_s or "", it.error or "",
            ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=batch_{job_id}.csv"},
    )
