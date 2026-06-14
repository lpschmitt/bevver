"""Deterministic tests for the mandatory country-of-origin check."""
from __future__ import annotations

from app.matching import match_country_of_origin

LABEL = (
    "STONE'S THROW DISTILLERY\n"
    "KENTUCKY STRAIGHT BOURBON WHISKEY\n"
    "BOTTLED BY ACME DISTILLING CO., LOUISVILLE, KY\n"
    "PRODUCT OF FRANCE\n"
    "GOVERNMENT WARNING: ...\n"
)


def test_country_of_origin_found():
    r = match_country_of_origin("France", LABEL)
    assert r.verdict == "match"
    assert "france" in r.found.lower()


def test_country_of_origin_mismatch():
    r = match_country_of_origin("Mexico", LABEL)
    assert r.verdict == "mismatch"
    assert "france" in r.found.lower()                # surfaces what the label actually shows


def test_country_of_origin_blank_but_on_label_is_assumed():
    # Not in the form but read off the label -> "assumed" (yellow), not "missing".
    r = match_country_of_origin("", LABEL)
    assert r.verdict == "assumed"
    assert "france" in r.found.lower()


def test_country_of_origin_blank_and_absent_is_not_found():
    r = match_country_of_origin("", "no origin statement here")
    assert r.verdict == "not_found"


# --- trailing state/country at the end of a phrase --------------------------- #

def test_trailing_state_normalized_to_usa():
    label = "Produced and bottled by Otium Cellars, Waterford, Virginia\nContains sulfites\n"
    r = match_country_of_origin("USA (Virginia)", label)
    assert r.verdict == "match"
    assert r.found == "USA"            # state dropped from the display
    assert r.expected == "USA"


def test_trailing_state_abbreviation_uppercase():
    label = "Distilled, aged and bottled by Old Dominick Distillery, Memphis, TN\n"
    r = match_country_of_origin("USA (Tennessee)", label)
    assert r.verdict == "match"


def test_trailing_state_blank_form_is_assumed():
    label = "Bottled by Matello, Gaston, Oregon\n"
    r = match_country_of_origin("", label)
    assert r.verdict == "assumed"
    assert r.found == "USA"            # state dropped from the display


def test_zip_after_state_still_detected():
    label = "Cellared & bottled by 8 Chains North, Waterford, VA 20197\n"
    r = match_country_of_origin("USA (Virginia)", label, brand_name="8 Chains North")
    assert r.verdict == "match"


def test_state_in_brand_name_is_ignored():
    # "Texas" here is the brand, not an origin statement -> nothing extracted.
    label = "TEXAS\nStraight Bourbon Whiskey\n"
    r = match_country_of_origin("", label, brand_name="Texas")
    assert r.verdict == "not_found"


def test_lowercase_two_letter_word_is_not_a_state():
    # "or" as the English word must not be read as Oregon.
    label = "Aged in oak or steel\n"
    r = match_country_of_origin("", label)
    assert r.verdict == "not_found"
