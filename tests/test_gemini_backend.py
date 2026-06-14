"""
Gemini backend tests.

Two layers, neither of which makes a network call:

  1. Pure parsing of Gemini's JSON response into a GeminiExtraction.
  2. The pipeline's Gemini branch, exercised with an INJECTED fake reader
     (`gemini_fn`) — the same dependency-injection seam used for OCR — so the
     structured-field -> matching and verbatim-text -> warning wiring is covered
     without the SDK, an API key, or the network.
"""
from __future__ import annotations

import pytest

from app.gemini_backend import GeminiExtraction, _parse
from app.pipeline import Application, run_pipeline
from app.warning import STATUTORY_WARNING

# The pipeline's Gemini branch still loads + encodes the image, which needs
# OpenCV. Skip the pipeline-level tests (not the parsing ones) if it's absent.
cv2 = pytest.importorskip("cv2", reason="OpenCV not installed")
import numpy as np  # noqa: E402


def _tiny_png() -> bytes:
    """A small decodable image; content is irrelevant because the reader is
    faked — we only need load_image + imencode to succeed."""
    img = np.full((40, 120, 3), 255, dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def _reading(**overrides) -> GeminiExtraction:
    base = dict(
        brand_name="Stone's Throw",
        class_type="Kentucky Straight Bourbon Whiskey",
        abv=40.0,
        proof=80.0,
        net_contents="750 mL",
        full_text="STONE'S THROW\nKentucky Straight Bourbon Whiskey\n"
                  "40% ALC/VOL (80 PROOF)\n750 mL\n" + STATUTORY_WARNING,
        confidence=0.95,
    )
    base.update(overrides)
    return GeminiExtraction(**base)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def test_parse_full_response():
    raw = (
        '{"brand_name": "Stone\'s Throw", "class_type": "Bourbon Whiskey", '
        '"abv_percent": "40", "proof": "80", "net_contents": "750 mL", '
        '"full_text": "STONE\'S THROW", "confidence": 0.9}'
    )
    r = _parse(raw)
    assert r.brand_name == "Stone's Throw"
    assert r.abv == 40.0
    assert r.proof == 80.0
    assert r.confidence == 0.9


def test_parse_blank_numbers_become_none():
    raw = ('{"brand_name": "X", "class_type": "", "abv_percent": "", '
           '"proof": "", "net_contents": "", "full_text": "X", "confidence": 1}')
    r = _parse(raw)
    assert r.abv is None and r.proof is None


def test_parse_clamps_confidence():
    raw = ('{"brand_name": "X", "class_type": "", "abv_percent": "", '
           '"proof": "", "net_contents": "", "full_text": "X", "confidence": 7}')
    assert _parse(raw).confidence == 1.0


def test_parse_empty_raises():
    with pytest.raises(ValueError):
        _parse("")


# --------------------------------------------------------------------------- #
# Pipeline branch (injected reader, no network)
# --------------------------------------------------------------------------- #

def test_gemini_pipeline_all_match():
    app = Application(
        brand_name="Stone's Throw",
        class_type="Bourbon Whiskey",
        abv="40",
        net_contents="750 mL",
    )
    result = run_pipeline(_tiny_png(), app, gemini_fn=lambda b: _reading())

    verdicts = {f.field: f.verdict for f in result.fields}
    assert verdicts["Brand name"] in ("match", "match_normalized")
    assert verdicts["Class/type"] in ("match", "match_normalized")
    assert verdicts["Alcohol content (ABV)"] == "match"
    assert verdicts["Net contents"] in ("match", "match_normalized")
    assert result.warning["verdict"] == "match"
    # Gemini's self-reported confidence is surfaced as ocr_confidence.
    assert result.ocr_confidence == 0.95
    assert result.ocr_text.startswith("STONE'S THROW")


def test_gemini_pipeline_brand_mismatch():
    app = Application(brand_name="Totally Different Co", class_type="", abv="",
                      net_contents="")
    result = run_pipeline(_tiny_png(), app, gemini_fn=lambda b: _reading())
    verdicts = {f.field: f.verdict for f in result.fields}
    assert verdicts["Brand name"] == "mismatch"


def test_gemini_pipeline_warning_case_violation():
    """Title-case prefix in the transcription must fail the strict case check."""
    bad = _reading(full_text=STATUTORY_WARNING.replace(
        "GOVERNMENT WARNING:", "Government Warning:"))
    app = Application(brand_name="Stone's Throw")
    result = run_pipeline(_tiny_png(), app, gemini_fn=lambda b: bad)
    assert result.warning["verdict"] == "mismatch"
    assert result.warning["case_ok"] is False


def test_gemini_pipeline_derives_abv_from_proof():
    reading = _reading(abv=None, proof=80.0)
    app = Application(brand_name="Stone's Throw", abv="40")
    result = run_pipeline(_tiny_png(), app, gemini_fn=lambda b: reading)
    abv = next(f for f in result.fields if f.field == "Alcohol content (ABV)")
    assert abv.verdict == "match"
