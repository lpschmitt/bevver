"""Phase 2 batch: deterministic tests for the parts that don't need OCR."""
from __future__ import annotations

import threading
import time
import types

from app import batch, patterns
from app.batch import (BatchItem, BatchJob, FAILED, NEEDS_REVIEW, VERIFIED,
                       PENDING)
from app.pipeline import Application


def _fake_result(all_clear: bool = True):
    """Minimal stand-in for a VerificationResult, with the attrs _worker reads."""
    return types.SimpleNamespace(
        summary=lambda: {"all_clear": all_clear, "verified": 1, "total": 1,
                         "needs_review": 0 if all_clear else 1},
        fields=[],
        warning={},
        beverage_class="wine",
        ocr_text="",
        timings=types.SimpleNamespace(to_dict=lambda: {"total_s": 0.01}),
    )


def _job_of(n: int) -> BatchJob:
    items = [BatchItem(f"f{i}.jpg", Application(), b"x") for i in range(n)]
    return BatchJob(job_id="t", items=items)


def test_parse_application_csv_returns_front_back_specs():
    raw = (
        b"front,back,brand_name,class_type,abv,net_contents\n"
        b"a.jpg,,Calvert Brewing Company,Beer,,750 mL\n"
        b"b.png,b_back.png,Otium Cellars,Table Red Wine,14.1,\n"
    )
    specs = batch._parse_application_csv(raw)
    assert [s["front"] for s in specs] == ["a.jpg", "b.png"]
    assert specs[0]["back"] == ""              # optional back omitted
    assert specs[1]["back"] == "b_back.png"    # back paired to the row
    assert specs[0]["application"].brand_name == "Calvert Brewing Company"
    assert specs[1]["application"].abv == "14.1"


def test_parse_application_csv_handles_bom_and_blank_rows():
    raw = "\ufefffront,brand_name\n,skipme\nc.jpg,Brand C\n".encode("utf-8")
    specs = batch._parse_application_csv(raw)
    assert [s["front"] for s in specs] == ["c.jpg"]
    assert specs[0]["application"].brand_name == "Brand C"


def test_job_progress_counts_statuses():
    items = [
        BatchItem("a", batch.Application(), b"", status=VERIFIED,
                  summary={"verified": 5, "total": 5, "needs_review": 0}),
        BatchItem("b", batch.Application(), b"", status=NEEDS_REVIEW,
                  summary={"verified": 4, "total": 5, "needs_review": 1}),
        BatchItem("c", batch.Application(), b"", status=PENDING),
    ]
    job = BatchJob(job_id="x", items=items)
    prog = job.progress()
    assert prog["total"] == 3
    assert prog["finished"] == 2
    assert prog["flagged"] == 1
    assert prog["failed"] == 0
    assert len(prog["items"]) == 3


def test_worker_runs_bounded_concurrency(monkeypatch):
    # Stub the pipeline to record how many verifications overlap at once.
    live = 0
    max_seen = 0
    lock = threading.Lock()

    def stub(data, application, **kwargs):
        nonlocal live, max_seen
        with lock:
            live += 1
            max_seen = max(max_seen, live)
        time.sleep(0.03)                 # hold the slot so workers actually overlap
        with lock:
            live -= 1
        return _fake_result(all_clear=True)

    monkeypatch.setattr(batch, "run_pipeline", stub)
    monkeypatch.setattr(batch, "BATCH_CONCURRENCY", 3)

    job = _job_of(6)
    batch._worker(job)                   # blocks until the pool drains

    assert job.done is True
    assert all(it.status == VERIFIED for it in job.items)
    # The cap is never exceeded, and parallelism actually happened.
    assert max_seen <= 3
    assert max_seen >= 2


def test_worker_failure_does_not_stop_batch(monkeypatch):
    def stub(data, application, *, filename="", **kwargs):
        if filename == "f2.jpg":
            raise RuntimeError("unreadable")
        return _fake_result(all_clear=True)

    monkeypatch.setattr(batch, "run_pipeline", stub)
    monkeypatch.setattr(batch, "BATCH_CONCURRENCY", 3)

    job = _job_of(5)
    batch._worker(job)

    assert job.done is True
    statuses = {it.filename: it.status for it in job.items}
    assert statuses["f2.jpg"] == FAILED
    assert all(statuses[f] == VERIFIED for f in statuses if f != "f2.jpg")


def test_field_patterns_cover_provided_fields_and_skip_blanks():
    app = Application(brand_name="Stone's Throw", class_type="Straight Whiskey",
                      abv="45", net_contents="750 mL", country_of_origin="France")
    pats = {p["field"]: p["pattern"] for p in patterns.field_patterns(app)}
    assert set(pats) == {"Brand name", "Class/type", "Alcohol content (ABV)",
                         "Net contents", "Country of origin"}
    # ABV pattern carries both the percent and proof (2x) forms.
    assert "90" in pats["Alcohol content (ABV)"]

    # Blank fields are omitted; a non-numeric ABV is skipped, not an error.
    sparse = patterns.field_patterns(Application(brand_name="", abv="n/a"))
    assert sparse == []
