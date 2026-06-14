"""Deterministic tests for beverage-class inference and the class-aware rules."""
from __future__ import annotations

from app.classes import Bev, PROFILES, infer_class, rules_for
from app.matching import (
    MATCH, MISMATCH, MISSING, NOT_APPLICABLE, check_sulfites, match_abv_classed,
)


def test_infer_class_from_keywords():
    assert infer_class("STRAIGHT WHISKY") == Bev.SPIRITS
    assert infer_class("LIQUEUR") == Bev.SPIRITS          # not mistaken for wine
    assert infer_class("TABLE RED WINE") == Bev.WINE
    assert infer_class("Cabernet Sauvignon") == Bev.WINE
    assert infer_class("BEER") == Bev.MALT
    assert infer_class("India Pale Ale") == Bev.MALT
    assert infer_class("Malt Liquor") == Bev.MALT         # malt beats spirits
    assert infer_class("") == Bev.UNKNOWN


def test_infer_class_abv_fallback():
    assert infer_class("", 40.0) == Bev.SPIRITS
    assert infer_class("", 5.0) == Bev.MALT
    assert infer_class("", 13.0) == Bev.WINE
    assert infer_class("mystery", None) == Bev.UNKNOWN


def test_profiles_match_the_regs():
    assert PROFILES[Bev.SPIRITS].abv_required
    assert not PROFILES[Bev.MALT].abv_required
    assert PROFILES[Bev.WINE].requires_sulfite_decl
    assert PROFILES[Bev.WINE].abv_allows_table_wine
    assert not PROFILES[Bev.UNKNOWN].abv_required        # permissive


def test_abv_spirits_required_missing_is_missing():
    _, rules = rules_for("STRAIGHT WHISKY")
    r = match_abv_classed(rules, "", None, None, "no alcohol statement here")
    assert r.verdict == MISSING


def test_abv_malt_optional_missing_is_not_applicable():
    _, rules = rules_for("BEER")
    r = match_abv_classed(rules, "", None, None, "just a lager")
    assert r.verdict == NOT_APPLICABLE


def test_abv_wine_table_wine_substitution():
    _, rules = rules_for("TABLE RED WINE")
    assert match_abv_classed(rules, "", None, None, "California Table Wine").verdict == MATCH
    # Wine with no number and no "table wine" wording -> required but absent (Missing).
    assert match_abv_classed(rules, "", None, None, "Estate bottled").verdict == MISSING


def test_abv_present_on_label_uses_base_rule():
    _, rules = rules_for("BEER")          # optional class, but a number is shown
    assert match_abv_classed(rules, "5", 5.0, None, "5% ALC/VOL").verdict == MATCH
    _, rules = rules_for("STRAIGHT WHISKY")
    assert match_abv_classed(rules, "45", 50.0, None, "50% ALC/VOL").verdict == MISMATCH


def test_check_sulfites():
    assert check_sulfites("Ingredients … CONTAINS SULFITES").verdict == MATCH
    assert check_sulfites("contains sulfites").verdict == MATCH
    assert check_sulfites("no such declaration").verdict == MISSING
