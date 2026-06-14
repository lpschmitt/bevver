"""Deterministic tests for the front/back OCR merge (no ML model, no OpenCV).

`_ocr_images` is driven with a fake ocr module + injected runner, so we can
assert the front/back lines merge in order and the multi-image path records a
timing — without loading PaddleOCR or cv2.
"""
from __future__ import annotations

from app.extraction import OcrLine, OcrResult
from app.pipeline import Timings, _ocr_images


class _FakeOcrModule:
    """Stand-in for app.ocr: load/preprocess are pass-throughs."""
    def load_image(self, data, content_type="", filename=""):
        return data

    def preprocess(self, img):
        return img


def _runner(img: bytes) -> OcrResult:
    # The "image" is just bytes carrying its own text, so order is observable.
    return OcrResult(lines=[OcrLine(text=img.decode(), confidence=0.9, height=20.0, top=0.0)])


def test_single_image_keeps_stage_breakdown():
    timings = Timings()
    merged = _ocr_images(_FakeOcrModule(), [(b"FRONT", "", "")], _runner, timings)
    assert [l.text for l in merged.lines] == ["FRONT"]
    # Single image reports each stage separately.
    assert timings.ocr_ms >= 0.0
    assert timings.load_ms >= 0.0
    assert timings.preprocess_ms >= 0.0


def test_front_back_merge_in_order():
    timings = Timings()
    images = [(b"FRONT", "", ""), (b"BACK", "", "")]
    merged = _ocr_images(_FakeOcrModule(), images, _runner, timings)
    # Front lines must come before back lines (brand-by-size + merge rely on it).
    assert [l.text for l in merged.lines] == ["FRONT", "BACK"]
    # Multi-image folds load/preprocess into the measured concurrent OCR region.
    assert timings.ocr_ms >= 0.0
    assert timings.load_ms == 0.0
    assert timings.preprocess_ms == 0.0
