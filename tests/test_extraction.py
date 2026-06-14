"""Unit tests for field extraction from OCR text."""
from __future__ import annotations

import pytest

from app import extraction
from tests.conftest import make_ocr


def test_extract_abv_alc_vol():
    assert extraction.extract_abv("Brand\n13.5% ALC/VOL\n750 mL") == 13.5


def test_extract_abv_abbreviation():
    assert extraction.extract_abv("ALCOHOL 40% BY VOLUME") == 40.0


@pytest.mark.parametrize("text", [
    "Alcohol 13.5% by volume",
    "13.5% Alc/Vol",
    "ALC. 13.5% BY VOL.",
    "13.5% alcohol by volume",
    "13.5% by volume",            # 'vol' cue without the word 'alcohol'
    "13.5 % vol",                 # space before %, bare 'vol'
    "Alc. by Vol. 13.5%",         # cue before the number
    "Alcohol content: 13.5%",
    "13.5% ABV",
])
def test_extract_abv_variant_formats(text):
    assert extraction.extract_abv(text) == 13.5


@pytest.mark.parametrize("text", [
    "100% natural flavors",       # not an alcohol statement (and 3 digits)
    "made with 100% agave",
    "JUST A BRAND NAME\n750 mL",
])
def test_extract_abv_none_when_not_an_abv(text):
    assert extraction.extract_abv(text) is None


def test_extract_proof():
    assert extraction.extract_proof("BOTTLED AT 90 PROOF") == 90.0


def test_extract_net_contents_ml():
    assert extraction.extract_net_contents("Foo 750 mL Bar") == "750 mL"


def test_extract_net_contents_liters():
    assert extraction.extract_net_contents("1.5 L red wine") == "1.5 L"


def test_extract_net_contents_none():
    assert extraction.extract_net_contents("no volume here") == ""


def test_brand_candidates_orders_by_size():
    ocr = make_ocr([("small print", 10.0), ("BIG BRAND", 60.0), ("medium", 25.0)])
    cands = extraction.brand_candidates(ocr)
    assert cands[0] == "BIG BRAND"


def test_brand_candidates_skip_stopwords():
    ocr = make_ocr([("GOVERNMENT WARNING:", 70.0), ("REAL BRAND", 50.0)])
    cands = extraction.brand_candidates(ocr)
    assert "REAL BRAND" in cands
    assert all("GOVERNMENT WARNING" not in c for c in cands)


def test_best_brand_for_picks_closest():
    ocr = make_ocr([("OTHER WORDS", 55.0), ("OTIUM CELLARS", 50.0)])
    assert extraction.best_brand_for("Otium Cellars", ocr) == "OTIUM CELLARS"
