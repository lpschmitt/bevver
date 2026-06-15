"""
Verification pipeline.

    upload bytes
      -> load (pdf->image if needed)
      -> preprocess (grayscale, contrast, deskew)
      -> OCR
      -> field extraction
      -> field matching
      -> results (per-field verdicts + warning check + timings)

Every stage is timed and the timings are returned in the response, because the
5-second budget is a headline requirement we have to *prove*, not assert.

OCR is injected (`ocr_fn`) so the same pipeline runs under pytest with canned
OCR output and in production with PaddleOCR — no ML model needed to test the
extraction/matching logic. The optional Gemini backend (`OCR_BACKEND=gemini`) is
injected the same way via `gemini_fn`.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from app import class_lookup, extraction, gemini_backend, matching, patterns
from app.classes import PROFILES, infer_class
from app.extraction import OcrResult
from app.gemini_backend import GeminiExtraction
from app.warning import verify_warning


@dataclass
class Application:
    """The application data an agent is verifying the label against."""
    brand_name: str = ""
    class_type: str = ""
    abv: str = ""
    net_contents: str = ""
    country_of_origin: str = ""


@dataclass
class Timings:
    load_ms: float = 0.0
    preprocess_ms: float = 0.0
    ocr_ms: float = 0.0
    extract_match_ms: float = 0.0

    @property
    def total_ms(self) -> float:
        return self.load_ms + self.preprocess_ms + self.ocr_ms + self.extract_match_ms

    def to_dict(self) -> dict:
        return {
            "load_ms": round(self.load_ms, 1),
            "preprocess_ms": round(self.preprocess_ms, 1),
            "ocr_ms": round(self.ocr_ms, 1),
            "extract_match_ms": round(self.extract_match_ms, 1),
            "total_ms": round(self.total_ms, 1),
            "total_s": round(self.total_ms / 1000.0, 2),
        }


@dataclass
class VerificationResult:
    fields: list = field(default_factory=list)        # list[FieldResult]
    warning: dict = field(default_factory=dict)
    timings: Timings = field(default_factory=Timings)
    ocr_text: str = ""
    ocr_confidence: float = 0.0
    beverage_class: str = "unknown"                   # which ruleset was applied

    def summary(self) -> dict:
        verdicts = [f.verdict for f in self.fields]
        warning_verdict = self.warning.get("verdict", "not_found")
        all_verdicts = verdicts + [warning_verdict]
        # "Not applicable" (not required for the class) and "assumed" (read off
        # the label but not given in the form to verify against) are informational
        # — they don't count for or against the score.
        applicable = [v for v in all_verdicts if v not in ("not_applicable", "assumed")]
        verified = sum(1 for v in applicable if v in ("match", "match_normalized"))
        total = len(applicable)
        needs_review = total - verified
        return {
            "verified": verified,
            "total": total,
            "needs_review": needs_review,
            "headline": (
                f"{verified} of {total} fields verified"
                + (f" — {needs_review} needs review" if needs_review else "")
            ),
            "all_clear": needs_review == 0,
        }

    def to_dict(self) -> dict:
        return {
            "summary": self.summary(),
            "fields": [f.to_dict() for f in self.fields],
            "warning": self.warning,
            "timings": self.timings.to_dict(),
            "ocr_confidence": round(self.ocr_confidence, 3),
            "beverage_class": self.beverage_class,
            "ocr_text": self.ocr_text,
        }


def _ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def run_pipeline(
    data: bytes,
    application: Application,
    content_type: str = "",
    filename: str = "",
    ocr_fn: Callable[..., OcrResult] | None = None,
    gemini_fn: Callable[..., GeminiExtraction] | None = None,
    back_data: bytes | None = None,
    back_content_type: str = "",
    back_filename: str = "",
) -> VerificationResult:
    """Run the full verification pipeline over raw uploaded bytes.

    A label is often split across two sides — the government health warning and
    the ABV/net-contents statements typically live on the *back*. When
    `back_data` is supplied, both images are read and their text is MERGED before
    extraction/matching, so a field printed on either side is found.
    """
    # Imported lazily so the extraction/matching core (and its tests) don't pull
    # in OpenCV / the OCR model unless an actual image is being processed.
    from app import ocr as ocr_module

    images = [(data, content_type, filename)]
    if back_data is not None:
        images.append((back_data, back_content_type, back_filename))

    # Gemini backend: a vision model reads the fields directly, so we skip the
    # local OCR + regex-extraction path entirely (see app.gemini_backend).
    if gemini_fn is not None or gemini_backend.is_selected():
        return _run_pipeline_gemini(images, application, gemini_fn, ocr_module)

    timings = Timings()

    # Read each side (load -> preprocess -> OCR), then merge the lines. With a
    # front+back pair the two sides are read concurrently to cut latency.
    merged = _ocr_images(ocr_module, images, ocr_fn, timings)

    # 4. Extraction + 5. Matching (over the merged front+back text).
    t = time.perf_counter()
    result = _extract_and_match(application, merged)
    timings.extract_match_ms = _ms(t)

    result.timings = timings
    result.ocr_text = merged.full_text
    result.ocr_confidence = merged.mean_confidence
    return result


def _ocr_one(ocr_module, data, content_type, filename, ocr_fn) -> tuple[OcrResult, float, float, float]:
    """Load -> preprocess -> OCR one image. Returns the result + stage durations (ms)."""
    t = time.perf_counter()
    img = ocr_module.load_image(data, content_type=content_type, filename=filename)
    load_ms = _ms(t)

    t = time.perf_counter()
    prepped = ocr_module.preprocess(img)
    prep_ms = _ms(t)

    t = time.perf_counter()
    runner = ocr_fn or ocr_module.run_ocr
    result: OcrResult = runner(prepped)
    ocr_ms = _ms(t)
    return result, load_ms, prep_ms, ocr_ms


def _ocr_images(ocr_module, images: list, ocr_fn, timings: Timings) -> OcrResult:
    """OCR every image and merge the lines (front first). One image keeps the
    per-stage timing breakdown; multiple images are read in parallel threads and
    we record the real wall-clock of that concurrent region as ocr_ms (load and
    preprocess fold into it), so the reported latency matches what was waited."""
    if len(images) == 1:
        data, ctype, fname = images[0]
        result, timings.load_ms, timings.preprocess_ms, timings.ocr_ms = \
            _ocr_one(ocr_module, data, ctype, fname, ocr_fn)
        return OcrResult(lines=list(result.lines))

    t = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(images)) as pool:
        # map preserves input order, so front lines stay ahead of back lines.
        results = list(pool.map(
            lambda im: _ocr_one(ocr_module, im[0], im[1], im[2], ocr_fn), images))
    timings.ocr_ms = _ms(t)   # concurrent region wall-clock (load+preprocess+OCR)

    lines: list = []
    for result, *_ in results:
        lines.extend(result.lines)
    return OcrResult(lines=lines)


def _run_pipeline_gemini(
    images: list,
    application: Application,
    gemini_fn: Callable[..., GeminiExtraction] | None,
    ocr_module,
) -> VerificationResult:
    """Gemini path: load -> (downscale + encode) -> Gemini read -> match.

    Reuses the local loader (so PDFs still render to their first page and the
    same size cap applies) but skips contrast/deskew preprocessing — a vision
    model reads the original colour image better than a binarized one. With a
    back image, each side is read separately and the two readings are merged.
    """
    import cv2

    timings = Timings()
    runner = gemini_fn or gemini_backend.extract_fields

    def _read_one(img_tuple) -> GeminiExtraction:
        # Load + downscale + encode to JPEG, then the Gemini read (injectable).
        img_data, ctype, fname = img_tuple
        img = ocr_module.load_image(img_data, content_type=ctype, filename=fname)
        img = ocr_module._downscale(img)
        ok, buf = cv2.imencode(".jpg", img)
        if not ok:
            raise ValueError("Could not encode image for Gemini.")
        return runner(buf.tobytes())

    # Front+back are independent network calls, so read them concurrently; the
    # ocr_ms is the real wall-clock of that region.
    t = time.perf_counter()
    if len(images) == 1:
        readings = [_read_one(images[0])]
    else:
        with ThreadPoolExecutor(max_workers=len(images)) as pool:
            readings = list(pool.map(_read_one, images))   # order preserved (front first)
    timings.ocr_ms = _ms(t)

    reading = readings[0] if len(readings) == 1 else _merge_readings(readings)

    # 3. Matching (reuses the same per-field rules as the local path).
    t = time.perf_counter()
    result = _extract_and_match_gemini(application, reading)
    timings.extract_match_ms = _ms(t)

    result.timings = timings
    result.ocr_text = reading.full_text
    result.ocr_confidence = reading.confidence
    return result


def _merge_readings(readings: list) -> GeminiExtraction:
    """Combine per-side Gemini readings: take the first non-empty value for each
    field (front is listed first, so it wins ties), concatenate the verbatim
    transcriptions, and keep the lowest confidence as the conservative estimate."""
    def first(attr):
        for r in readings:
            if getattr(r, attr):
                return getattr(r, attr)
        return getattr(readings[0], attr)

    def first_num(attr):
        for r in readings:
            if getattr(r, attr) is not None:
                return getattr(r, attr)
        return None

    return GeminiExtraction(
        brand_name=first("brand_name"),
        class_type=first("class_type"),
        abv=first_num("abv"),
        proof=first_num("proof"),
        net_contents=first("net_contents"),
        full_text="\n".join(r.full_text for r in readings if r.full_text).strip(),
        confidence=min(r.confidence for r in readings),
    )


def _show_matched_label_text(fields: list, app: Application, full_text: str) -> None:
    """Override each field's ``found`` with the exact text on the label that its
    generated pattern matched — so "Found on label" shows the literal matching
    string rather than a separately-extracted/normalized value. Display only:
    verdicts and notes are untouched, and the prior extraction machinery still
    runs (its output is just no longer what we show here). Fields whose pattern
    finds nothing (e.g. a mismatch, or a class matched via its superclass) keep
    their existing display.

    Class/type is intentionally excluded: ``match_class_type`` already surfaces
    the precise matched designation (e.g. "Whiskey", resolved via the lookup
    table), which is more specific than what the application's broad class regex
    would re-match here (its head word could pick a generic "Spirits" off the
    label instead).
    """
    values = {
        "Brand name": app.brand_name,
        "Alcohol content (ABV)": app.abv,
        "Net contents": app.net_contents,
        "Country of origin": app.country_of_origin,
    }
    for fr in fields:
        value = values.get(fr.field)
        if not value:
            continue
        hit = patterns.matched_on_label(fr.field, value, full_text)
        if hit:
            fr.found = hit


def _extract_and_match_gemini(
    app: Application, reading: GeminiExtraction
) -> VerificationResult:
    """Feed Gemini's structured reading through the existing matching rules, and
    its verbatim transcription through the warning check."""
    bev = infer_class(app.class_type, reading.abv)
    rules = PROFILES[bev]

    fields = [
        matching.match_brand(app.brand_name, reading.brand_name, full_text=reading.full_text),
        matching.match_class_type(app.class_type, reading.class_type,
                                  expected_display=class_lookup.superclass_for(app.class_type),
                                  full_text=reading.full_text),
        matching.match_abv_classed(rules, app.abv, reading.abv, reading.proof,
                                   reading.full_text),
        matching.match_net_contents(app.net_contents, reading.net_contents,
                                    full_text=reading.full_text),
    ]
    if rules.requires_sulfite_decl:
        fields.append(matching.check_sulfites(reading.full_text))
    # Mandatory text field, matched against the verbatim transcription.
    fields.append(matching.match_country_of_origin(app.country_of_origin, reading.full_text,
                                                   app.brand_name))

    # "Found on label" shows the exact matching text from the transcription.
    _show_matched_label_text(fields, app, reading.full_text)

    warning = verify_warning(reading.full_text)
    warning_dict = {
        "field": "Government health warning",
        "verdict": warning.verdict,
        "content_ok": warning.content_ok,
        "case_ok": warning.case_ok,
        "content_distance": warning.content_distance,
        "found_prefix": warning.found_prefix,
        "found_text": warning.found_text,
        "note": warning.note,
    }

    return VerificationResult(fields=fields, warning=warning_dict,
                              beverage_class=bev.value)


def _extract_and_match(app: Application, ocr_result: OcrResult) -> VerificationResult:
    full_text = ocr_result.full_text
    fields = []

    # ABV is extracted up front so the beverage class can use it as a fallback.
    found_abv = extraction.extract_abv(full_text)
    found_proof = extraction.extract_proof(full_text)
    bev = infer_class(app.class_type, found_abv)
    rules = PROFILES[bev]

    # Brand name (largest text block, then explain normalization).
    found_brand = extraction.best_brand_for(app.brand_name, ocr_result)
    fields.append(matching.match_brand(app.brand_name, found_brand, full_text=full_text))

    # Class/type (fuzzy substring across all OCR text). The "Expected" column
    # shows the superclass looked up for the application value (display only).
    # No separately-extracted class field here, so the full transcription is the
    # search target; "Found on label" still shows the concise matched designation.
    fields.append(matching.match_class_type(
        app.class_type, "",
        expected_display=class_lookup.superclass_for(app.class_type),
        full_text=full_text))

    # ABV (class-aware: strict for spirits, optional for malt, table-wine for wine).
    fields.append(matching.match_abv_classed(rules, app.abv, found_abv, found_proof,
                                             full_text))

    # Net contents (unit-aware).
    found_net = extraction.extract_net_contents(full_text)
    fields.append(matching.match_net_contents(app.net_contents, found_net,
                                              full_text=full_text))

    # Sulfite declaration (wine only).
    if rules.requires_sulfite_decl:
        fields.append(matching.check_sulfites(full_text))

    # Mandatory text field (country of origin).
    fields.append(matching.match_country_of_origin(app.country_of_origin, full_text,
                                                   app.brand_name))

    # "Found on label" shows the exact matching text from the OCR label text.
    _show_matched_label_text(fields, app, full_text)

    # Government health warning (dedicated strict check).
    warning = verify_warning(full_text)
    warning_dict = {
        "field": "Government health warning",
        "verdict": warning.verdict,
        "content_ok": warning.content_ok,
        "case_ok": warning.case_ok,
        "content_distance": warning.content_distance,
        "found_prefix": warning.found_prefix,
        "found_text": warning.found_text,
        "note": warning.note,
    }

    return VerificationResult(fields=fields, warning=warning_dict,
                              beverage_class=bev.value)
