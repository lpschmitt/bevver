"""Phase 2 batch: deterministic tests for the parts that don't need OCR."""
from __future__ import annotations

from app import batch, patterns
from app.batch import BatchItem, BatchJob, NEEDS_REVIEW, VERIFIED, PENDING
from app.pipeline import Application


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
