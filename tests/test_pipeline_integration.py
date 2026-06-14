"""
Fixture-driven integration suite over the real Test_Images dataset.

For each applications-data row that has a downloaded label, run the FULL pipeline
(load -> preprocess -> OCR -> extract -> match) and assert extraction + matching.
This requires an OCR backend (PaddleOCR/Tesseract) and OpenCV, so the whole
module is skipped when those aren't installed — the deterministic unit and
adversarial suites still cover the logic without a model.

Honesty policy (per the brief): old scanned labels (2005–2012 IDs) are known to
be hard for OCR. They are marked xfail(strict=False) rather than having
thresholds loosened to force green — they pass if OCR happens to read them, and
don't fail the suite if it doesn't. The README reports the real pass rate.
"""
from __future__ import annotations

import pytest

from tests.conftest import image_path_for, back_image_path_for

# Skip the whole module unless the heavy deps are importable.
pytest.importorskip("cv2", reason="OpenCV not installed")
_ocr_backend = None
for _name in ("paddleocr", "pytesseract"):
    try:
        __import__(_name)
        _ocr_backend = _name
        break
    except Exception:
        continue
if _ocr_backend is None:
    pytest.skip("No OCR backend (paddleocr/pytesseract) installed.",
                allow_module_level=True)

from app.pipeline import Application, run_pipeline  # noqa: E402


def _is_old_scan(ttb_id: str) -> bool:
    """First two digits of a TTB ID are the filing year (e.g. 05.., 12..)."""
    try:
        year = 2000 + int(ttb_id[:2])
    except ValueError:
        return False
    return year <= 2012


def _load_cases():
    from tests.conftest import APPLICATIONS_DATA
    import csv
    if not APPLICATIONS_DATA.exists():
        return []
    with APPLICATIONS_DATA.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    cases = []
    for row in rows:
        if image_path_for(row) is None:
            continue
        # Own-photo rows (e.g. Cointreau) have no TTB id; key them by front file.
        label = row.get("ttb_id") or row.get("front")
        marks = [pytest.mark.xfail(reason="old scan, OCR may fail", strict=False)] \
            if _is_old_scan(row.get("ttb_id", "")) else []
        cases.append(pytest.param(row, marks=marks, id=label))
    return cases


CASES = _load_cases()


@pytest.mark.skipif(not CASES, reason="No downloaded test images.")
@pytest.mark.parametrize("row", CASES)
def test_real_label_pipeline(row):
    label = row.get("ttb_id") or row.get("front")
    path = image_path_for(row)
    data = path.read_bytes()
    app = Application(
        brand_name=row.get("brand_name", ""),
        class_type=row.get("class_type", ""),
        abv=row.get("abv", ""),          # often blank -> assertion skipped
        net_contents=row.get("net_contents", ""),
        country_of_origin=row.get("country_of_origin", ""),
    )

    # Read the back label too when present (e.g. the warning lives on the back);
    # the pipeline merges front+back OCR before extraction/matching.
    back_path = back_image_path_for(row)
    back_data = back_path.read_bytes() if back_path else None
    result = run_pipeline(
        data, app, filename=path.name,
        back_data=back_data,
        back_filename=back_path.name if back_path else "",
    )

    # Latency budget is a headline requirement.
    assert result.timings.total_ms < 5000, (
        f"{label} took {result.timings.total_s:.2f}s (>5s budget)")

    verdicts = {f.field: f.verdict for f in result.fields}

    # Brand must be located and match (the core verification).
    assert verdicts["Brand name"] in ("match", "match_normalized"), (
        f"brand verdict was {verdicts['Brand name']} for {label}")

    # Class/type when present in the application.
    if row.get("class_type"):
        assert verdicts["Class/type"] in ("match", "match_normalized"), (
            f"class/type verdict {verdicts['Class/type']} for {label}")

    # ABV only when hand-filled (skip blanks rather than fail).
    if row.get("abv", "").strip():
        assert verdicts["Alcohol content (ABV)"] == "match"

    # Net contents only when present (registry leaves it blank for many).
    if row.get("net_contents", "").strip():
        assert verdicts["Net contents"] in ("match", "match_normalized")
