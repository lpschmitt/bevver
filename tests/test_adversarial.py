"""
Adversarial tests — a verifier that cannot fail is worthless.

These feed the extraction+matching core (the heart of run_pipeline) a *correct*
synthetic label, then mutate the application metadata (wrong ABV, altered brand,
wrong net contents, tampered warning) and assert the tool reports the mismatch.
Deterministic: no OCR model required, so they always run under `pytest`.
"""
from __future__ import annotations

import pytest

from app.classes import Bev, infer_class
from app.pipeline import Application, _extract_and_match
from app.warning import STATUTORY_WARNING
from tests.conftest import make_ocr, synthetic_label_text

# Five real records from the dataset, with plausible label content.
LABELS = [
    # (brand, class_type, abv, net)
    ("CALVERT BREWING COMPANY", "BEER", "5.5", "750 mL"),
    ("8 CHAINS NORTH", "TABLE RED WINE", "13.5", "750 mL"),
    ("OTIUM CELLARS", "TABLE RED WINE", "14.1", "750 mL"),
    ("DAVID JAMES STRAIGHT AMERICAN WHISKEY", "STRAIGHT WHISKY", "45", "750 mL"),
    ("TOMMYROTTER", "STRAIGHT WHISKY", "51.5", "750 mL"),
]


def _ocr_for(label):
    brand, cls, abv, net = label
    # Wine labels carry a sulfite declaration so a "correct" wine fully verifies.
    is_wine = infer_class(cls) == Bev.WINE
    return make_ocr(synthetic_label_text(brand, cls, abv, net, include_sulfites=is_wine))


@pytest.mark.parametrize("label", LABELS)
def test_correct_metadata_all_verify(label):
    brand, cls, abv, net = label
    app = Application(brand_name=brand, class_type=cls, abv=abv, net_contents=net)
    result = _extract_and_match(app, _ocr_for(label))
    verdicts = {f.field: f.verdict for f in result.fields}
    assert verdicts["Brand name"] in ("match", "match_normalized")
    assert verdicts["Alcohol content (ABV)"] == "match"
    assert verdicts["Net contents"] in ("match", "match_normalized")
    assert result.warning["verdict"] == "match"


@pytest.mark.parametrize("label", LABELS)
def test_wrong_abv_is_caught(label):
    brand, cls, abv, net = label
    wrong_abv = str(float(abv) + 5.0)
    app = Application(brand_name=brand, class_type=cls, abv=wrong_abv, net_contents=net)
    result = _extract_and_match(app, _ocr_for(label))
    abv_result = next(f for f in result.fields if f.field.startswith("Alcohol"))
    assert abv_result.verdict == "mismatch"


@pytest.mark.parametrize("label", LABELS)
def test_altered_brand_is_caught(label):
    brand, cls, abv, net = label
    app = Application(brand_name="ACME COUNTERFEIT DISTILLERS",
                     class_type=cls, abv=abv, net_contents=net)
    result = _extract_and_match(app, _ocr_for(label))
    brand_result = next(f for f in result.fields if f.field == "Brand name")
    assert brand_result.verdict == "mismatch"


@pytest.mark.parametrize("label", LABELS)
def test_wrong_net_contents_is_caught(label):
    brand, cls, abv, net = label
    app = Application(brand_name=brand, class_type=cls, abv=abv, net_contents="375 mL")
    result = _extract_and_match(app, _ocr_for(label))
    net_result = next(f for f in result.fields if f.field == "Net contents")
    assert net_result.verdict == "mismatch"


def test_tampered_warning_case_is_caught():
    label = LABELS[0]
    brand, cls, abv, net = label
    tampered = synthetic_label_text(
        brand, cls, abv, net,
        warning_text=STATUTORY_WARNING.replace("GOVERNMENT WARNING:", "Government Warning:"),
    )
    app = Application(brand_name=brand, class_type=cls, abv=abv, net_contents=net)
    result = _extract_and_match(app, make_ocr(tampered))
    assert result.warning["verdict"] == "mismatch"
    assert not result.warning["case_ok"]


def test_missing_warning_is_caught():
    label = LABELS[0]
    brand, cls, abv, net = label
    no_warning = synthetic_label_text(brand, cls, abv, net, include_warning=False)
    app = Application(brand_name=brand, class_type=cls, abv=abv, net_contents=net)
    result = _extract_and_match(app, make_ocr(no_warning))
    assert result.warning["verdict"] == "missing"


def test_missing_sulfite_on_wine_is_caught():
    brand, cls, abv, net = LABELS[1]  # 8 CHAINS NORTH, TABLE RED WINE
    no_sulfite = synthetic_label_text(brand, cls, abv, net, include_sulfites=False)
    app = Application(brand_name=brand, class_type=cls, abv=abv, net_contents=net)
    result = _extract_and_match(app, make_ocr(no_sulfite))
    sulfite = next(f for f in result.fields if f.field == "Sulfite declaration")
    assert sulfite.verdict == "missing"


def test_sulfite_field_only_on_wine():
    # Wine gets a sulfite field; a beer does not.
    wine = LABELS[1]
    rw = _extract_and_match(
        Application(brand_name=wine[0], class_type=wine[1], abv=wine[2], net_contents=wine[3]),
        _ocr_for(wine))
    assert any(f.field == "Sulfite declaration" for f in rw.fields)
    assert rw.beverage_class == "wine"

    beer = LABELS[0]
    rb = _extract_and_match(
        Application(brand_name=beer[0], class_type=beer[1], abv=beer[2], net_contents=beer[3]),
        _ocr_for(beer))
    assert not any(f.field == "Sulfite declaration" for f in rb.fields)
    assert rb.beverage_class == "malt"
