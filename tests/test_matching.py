"""Unit tests for the per-field matching rules."""
from __future__ import annotations

import pytest

from app import matching, patterns
from app.matching import (MATCH, MATCH_NORMALIZED, MISMATCH, MISSING,
                          NOT_FOUND, PARTIAL_MATCH)


# --- Blank-in-form but read off the label -> "Mismatch" (the application is
#     missing a value the label states), not "missing" -----------------------#
def test_blank_form_fields_found_on_label_are_mismatch():
    # ABV / net contents / brand left blank in the application but read by OCR.
    assert matching.match_abv("", 14.0, None).verdict == MISMATCH
    assert matching.match_net_contents("", "750 mL").verdict == MISMATCH
    assert matching.match_brand("", "PINOTOPIA").verdict == MISMATCH


def test_blank_form_fields_absent_on_label_stay_not_found():
    assert matching.match_abv("", None, None).verdict == NOT_FOUND
    assert matching.match_net_contents("", "no volume here").verdict == NOT_FOUND
    assert matching.match_brand("", "").verdict == NOT_FOUND


# --- Brand name ------------------------------------------------------------ #

def test_brand_exact_match():
    r = matching.match_brand("Stone's Throw", "Stone's Throw")
    assert r.verdict == MATCH


def test_brand_case_and_punctuation_normalized():
    # The stakeholder example: label all-caps vs application title-case.
    r = matching.match_brand("Stone's Throw", "STONE'S THROW")
    assert r.verdict == MATCH_NORMALIZED
    assert "normaliz" in r.note.lower()


def test_brand_whitespace_normalized():
    r = matching.match_brand("8 Chains North", "8  CHAINS   NORTH")
    assert r.verdict == MATCH_NORMALIZED


def test_brand_ocr_noise_still_matches():
    r = matching.match_brand("CALVERT BREWING COMPANY", "CALVERT BREWlNG COMPANY")
    assert r.verdict in (MATCH, MATCH_NORMALIZED)


def test_brand_real_mismatch():
    r = matching.match_brand("CALVERT BREWING COMPANY", "TOTALLY DIFFERENT BRAND")
    assert r.verdict == MISMATCH


def test_brand_partial_match():
    # Application name appears within a longer label brand -> partial match.
    r = matching.match_brand("Cointreau", "Cointreau Liqueur")
    assert r.verdict == PARTIAL_MATCH
    assert "partial" in r.note.lower()


def test_brand_missing_when_expected_but_absent():
    # The application names a brand but none is on the label -> Missing, not Mismatch.
    assert matching.match_brand("Anything", "").verdict == MISSING


# --- Class / type ---------------------------------------------------------- #

def test_class_type_substring_match():
    r = matching.match_class_type("BEER", "PROUDLY BREWED — CRAFT BEER — MARYLAND")
    assert r.verdict == MATCH


def test_class_type_fuzzy_match():
    r = matching.match_class_type("STRAIGHT WHISKY", "STRAIGHT WHISKEY")
    assert r.verdict in (MATCH, MATCH_NORMALIZED)


def test_class_type_mismatch():
    # A different TTB superclass on the label (spirits) must still flag a wine app.
    r = matching.match_class_type("TABLE RED WINE", "VODKA DISTILLED FROM GRAIN")
    assert r.verdict == MISMATCH


def test_class_type_superclass_sibling_matches():
    # Lookup-driven: a varietal on the label is consistent with a wine class.
    r = matching.match_class_type("TABLE RED WINE", "Malbec")
    assert r.verdict == MATCH
    assert r.found == "Malbec"


# --- ABV ------------------------------------------------------------------- #

def test_abv_numeric_equality():
    assert matching.match_abv("45", 45.0).verdict == MATCH


def test_abv_integer_vs_float():
    assert matching.match_abv("45.0", 45.0).verdict == MATCH


def test_abv_mismatch():
    assert matching.match_abv("40", 45.0).verdict == MISMATCH


def test_abv_proof_cross_check_consistent():
    r = matching.match_abv("45", 45.0, found_proof=90.0)
    assert r.verdict == MATCH
    assert "consistent" in r.note.lower()


def test_abv_proof_inconsistent_flagged():
    r = matching.match_abv("45", 45.0, found_proof=100.0)
    assert r.verdict == MATCH       # ABV itself matches
    assert "not" in r.note.lower()  # but proof mismatch is flagged


def test_abv_derived_from_proof():
    r = matching.match_abv("50", None, found_proof=100.0)
    assert r.verdict == MATCH


def test_abv_missing_when_expected_but_absent():
    # Application gives an ABV but the label shows none -> Missing, not Mismatch.
    assert matching.match_abv("45", None, None).verdict == MISSING


# --- Net contents ---------------------------------------------------------- #

def test_net_contents_identical():
    assert matching.match_net_contents("750 mL", "750 mL").verdict == MATCH


def test_net_contents_unit_normalized():
    r = matching.match_net_contents("750 mL", "0.75 L")
    assert r.verdict == MATCH_NORMALIZED


def test_net_contents_spacing_variation():
    assert matching.match_net_contents("750 mL", "750ml").verdict in (MATCH, MATCH_NORMALIZED)


def test_net_contents_mismatch():
    assert matching.match_net_contents("750 mL", "500 mL").verdict == MISMATCH


def test_net_contents_ocr_digit_confusion_is_rescued():
    # A faint "750 ML" misread as "75O ML" (letter O) still verifies, because the
    # rescue only accepts a corrected reading that equals an expected volume.
    label = "OLD LOUISVILLE WHISKEY CO.\n56% ALC/VOL (112 PROOF)\n75O ML\nUNCUT"
    r = matching.match_net_contents("750 mL", "", full_text=label)
    assert r.verdict == MATCH_NORMALIZED
    assert "OCR" in r.note


def test_net_contents_rescue_never_invents_a_match():
    # A different real volume must still flag, and a label with no volume stays
    # Missing — the rescue cannot conjure the expected value.
    assert matching.match_net_contents("750 mL", "", full_text="500 ML").verdict == MISMATCH
    assert matching.match_net_contents(
        "750 mL", "", full_text="OLD LOUISVILLE WHISKEY").verdict == "missing"


def test_net_contents_trusts_structured_reading():
    # Gemini-style: the verbatim transcription garbled the volume, but the model's
    # structured net-contents reading is clean -> verified.
    r = matching.match_net_contents("750 mL", "750 mL", full_text="...75O ML...")
    assert r.verdict in (MATCH, MATCH_NORMALIZED)


def test_parse_volume_ml():
    assert matching.parse_volume_ml("750 mL") == pytest.approx(750.0)
    assert matching.parse_volume_ml("0.75 L") == pytest.approx(750.0)
    assert matching.parse_volume_ml("1 liter") == pytest.approx(1000.0)


def test_net_contents_gallons_and_pints():
    # Beer in the dataset is stated in gallons (keg sizes) and pints.
    assert matching.parse_volume_ml("5.16 gal") == pytest.approx(19532.7, rel=1e-3)
    assert matching.match_net_contents("5.16 gal", "5.16 GAL").verdict == MATCH
    assert matching.match_net_contents("16 fl oz (1 pint)", "1 PINT").verdict in (
        MATCH, MATCH_NORMALIZED)


def test_net_contents_us_gallon_qualifier():
    # Keg labels often print "US Gallon" / "U.S. Gallon"; the system qualifier
    # must parse to the same volume as a bare gallon (the dataset's gallon is US).
    for label in ("5.17 US Gallon", "5.17 U.S. Gallon", "5.17 US Gal"):
        assert matching.parse_volume_ml(label) == pytest.approx(
            matching.parse_volume_ml("5.17 gal"), rel=1e-6), label
    # Application states "gal"; the label prints "U.S. Gallon" -> still verified.
    r = matching.match_net_contents("5.17 gal / 15.5 gal", "",
                                    full_text="Net Contents 5.17 U.S. Gallon")
    assert r.verdict in (MATCH, MATCH_NORMALIZED)


def test_net_contents_compound_any_one_matches():
    # A keg pair verifies if the label shows EITHER size.
    r = matching.match_net_contents("15.5 gal / 5.2 gal", "label says 5.2 GAL keg",
                                    full_text="label says 5.2 GAL keg")
    assert r.verdict in (MATCH, MATCH_NORMALIZED)


# --- ABV proof equivalent (pattern form) ----------------------------------- #

def test_abv_regex_matches_percent_and_proof():
    rx = patterns.abv_regex(45)
    assert rx.search("45% ALC/VOL")
    assert rx.search("90 PROOF")
    assert not rx.search("80 PROOF")


# --- Country of origin: USA = any state / abbreviation / USA form ----------- #

def test_country_usa_matches_any_state_name():
    r = matching.match_country_of_origin("USA (Oregon)",
                                         "Bottled by Matello, Gaston, Oregon")
    assert r.verdict == MATCH
    assert r.found == "USA"


def test_country_usa_matches_uppercase_abbreviation():
    r = matching.match_country_of_origin("USA (Kentucky)", "Louisville, KY 40202")
    assert r.verdict == MATCH


def test_country_usa_lowercase_word_is_not_a_state():
    # "or" the English word must not satisfy a domestic origin requirement; with no
    # origin statement at all on the label, the mandatory field is Missing.
    r = matching.match_country_of_origin("USA (Texas)", "aged in oak or steel")
    assert r.verdict == MISSING


def test_country_foreign_matches_name():
    r = matching.match_country_of_origin("France", "Product of France")
    assert r.verdict == MATCH
