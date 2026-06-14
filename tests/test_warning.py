"""
Government Health Warning tests.

The case check is the whole point of this field: title-case is a real rejection,
so it must be reported separately from the (OCR-tolerant) content check.
"""
from __future__ import annotations

from app.warning import STATUTORY_WARNING, verify_warning


def test_exact_statutory_warning_passes():
    r = verify_warning(STATUTORY_WARNING)
    assert r.found and r.content_ok and r.case_ok
    assert r.verdict == "match"


def test_title_case_prefix_is_rejected():
    text = STATUTORY_WARNING.replace("GOVERNMENT WARNING:", "Government Warning:")
    r = verify_warning(text)
    assert r.found
    assert r.content_ok          # wording is the same
    assert not r.case_ok         # but the casing is wrong
    assert r.verdict == "mismatch"
    assert "case" in r.note.lower()


def test_lowercase_prefix_is_rejected():
    text = STATUTORY_WARNING.replace("GOVERNMENT WARNING:", "government warning:")
    r = verify_warning(text)
    assert r.found and not r.case_ok
    assert r.verdict == "mismatch"


def test_missing_warning_is_missing():
    r = verify_warning("CALVERT BREWING COMPANY\nBEER\n5.5% ALC/VOL\n750 mL")
    assert not r.found
    assert r.verdict == "missing"


def test_ocr_noise_within_tolerance_still_matches():
    # A couple of single-character OCR slips (l<->I, missing period).
    noisy = STATUTORY_WARNING.replace("birth defects", "birth defects")  # 1 edit
    r = verify_warning(noisy)
    assert r.content_ok
    assert r.case_ok
    assert r.verdict == "match"


def test_large_wording_change_exceeds_tolerance():
    bad = STATUTORY_WARNING.replace(
        "may cause health problems",
        "will definitely cause many serious health problems and more",
    )
    r = verify_warning(bad)
    assert r.found
    assert not r.content_ok
    assert r.verdict == "mismatch"


def test_warning_located_within_surrounding_text():
    surrounded = (
        "SOME BRAND\nIMPORTED\n"
        + STATUTORY_WARNING
        + "\nBOTTLED BY ACME"
    )
    r = verify_warning(surrounded)
    assert r.found and r.case_ok and r.content_ok
