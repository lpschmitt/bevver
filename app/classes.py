"""
Beverage-class inference and per-class rule profiles.

TTB regulates the three commodity classes under different parts of 27 CFR
(spirits = Part 5, wine = Part 4, malt beverages = Part 7), and the mandatory
label information differs — most notably the alcohol-content rule:

  - spirits: a numeric ABV is always required;
  - wine:    required, but 7–14% "table wine" may say "Table Wine" in lieu of a
             number, and a sulfite declaration is required (≥10 ppm);
  - malt:    a numeric ABV is optional (state-dependent), so its absence is N/A.

We infer the class from the application's free-text `class_type` (the dataset's
values — "STRAIGHT WHISKY", "TABLE RED WINE", "BEER" — classify cleanly), with
the label/expected ABV as a fallback. The matching pipeline reads the resulting
`ClassRules` to decide how to score ABV and whether to add the sulfite check.
Unknown stays permissive so an unclassifiable label is never wrongly penalized.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Bev(str, Enum):
    SPIRITS = "spirits"
    WINE = "wine"
    MALT = "malt"        # beer / malt beverage
    UNKNOWN = "unknown"


# Keyword tables. Order of the checks below matters: malt is tested before
# spirits ("malt liquor" -> malt), and spirits before wine ("liqueur" -> spirits,
# not "...eur"/"wine").
_MALT = ("beer", "ale", "lager", "ipa", "pilsner", "pilsener", "stout",
         "porter", "saison", "malt", "hefeweizen", "kolsch", "bock")
_SPIRITS = ("whisky", "whiskey", "bourbon", "rye", "vodka", "gin", "rum",
            "tequila", "brandy", "cognac", "liqueur", "cordial", "mezcal",
            "schnapps", "spirit")
_WINE = ("wine", "cabernet", "merlot", "pinot", "chardonnay", "sauvignon",
         "riesling", "zinfandel", "syrah", "shiraz", "malbec", "port",
         "sherry", "vermouth", "rosé", "rose", "sparkling", "champagne")


def infer_class(class_type: str, abv: float | None = None) -> Bev:
    """Classify a beverage from its class/type text, with ABV as a fallback."""
    t = (class_type or "").lower()
    if any(k in t for k in _MALT):
        return Bev.MALT
    if any(k in t for k in _SPIRITS):
        return Bev.SPIRITS
    if any(k in t for k in _WINE):
        return Bev.WINE
    if abv is not None:
        if abv >= 20:
            return Bev.SPIRITS
        if abv <= 8:
            return Bev.MALT
        return Bev.WINE
    return Bev.UNKNOWN


@dataclass(frozen=True)
class ClassRules:
    abv_required: bool            # is a numeric ABV mandatory on the label?
    abv_allows_table_wine: bool   # may "Table Wine"/"Light Wine" stand in for a number?
    requires_sulfite_decl: bool   # wine ≥10 ppm -> "Contains Sulfites"


PROFILES: dict[Bev, ClassRules] = {
    Bev.SPIRITS: ClassRules(abv_required=True,  abv_allows_table_wine=False, requires_sulfite_decl=False),
    Bev.WINE:    ClassRules(abv_required=True,  abv_allows_table_wine=True,  requires_sulfite_decl=True),
    Bev.MALT:    ClassRules(abv_required=False, abv_allows_table_wine=False, requires_sulfite_decl=False),
    Bev.UNKNOWN: ClassRules(abv_required=False, abv_allows_table_wine=False, requires_sulfite_decl=False),
}


def rules_for(class_type: str, abv: float | None = None) -> tuple[Bev, ClassRules]:
    """Convenience: infer the class and return (Bev, its ClassRules)."""
    bev = infer_class(class_type, abv)
    return bev, PROFILES[bev]
