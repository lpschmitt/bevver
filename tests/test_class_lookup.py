"""Class/type -> superclass lookup, and its display-only use in matching."""
from __future__ import annotations

from app import matching
from app.class_lookup import superclass_for
from app.matching import MATCH, MISMATCH


def test_superclass_exact_and_varietal():
    assert superclass_for("Pinot Noir") == "Wine"
    assert superclass_for("Cabernet Sauvignon") == "Wine"
    assert superclass_for("Vodka") == "Distilled Spirits"


def test_superclass_freetext_dataset_values():
    # The applications-data dataset's free-text class/types resolve via token match.
    assert superclass_for("STRAIGHT WHISKEY") == "Distilled Spirits"
    assert superclass_for("TABLE RED WINE") == "Wine"
    assert superclass_for("BEER") == "Beer/Malt"
    assert superclass_for("LIQUEUR") == "Distilled Spirits"


def test_superclass_unknown_or_blank_is_none():
    assert superclass_for("") is None
    assert superclass_for("Nonexistent Foo Drink") is None


def test_expected_display_overrides_only_the_shown_value():
    # Expected column shows the superclass; verdict still uses the raw value.
    r = matching.match_class_type("Pinot Noir", "Sonoma Coast Pinot Noir 2021",
                                  expected_display=superclass_for("Pinot Noir"))
    assert r.expected == "Wine"
    # "Found on label" surfaces the matched class designation, not the whole line.
    assert r.found == "Pinot Noir"
    assert r.verdict == MATCH


def test_expected_display_does_not_change_verdict():
    # Superclass in Expected, but a real class/type mismatch is still a mismatch.
    r = matching.match_class_type("Pinot Noir", "Kentucky Straight Bourbon",
                                  expected_display=superclass_for("Pinot Noir"))
    assert r.expected == "Wine"
    assert r.verdict == MISMATCH


def test_expected_display_falls_back_to_form_value_when_unknown():
    r = matching.match_class_type("Foo Drink", "Foo Drink",
                                  expected_display=superclass_for("Foo Drink"))
    assert r.expected == "Foo Drink"   # None -> shows the raw application value
