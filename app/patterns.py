"""
Per-field pattern generation.

The matcher's model: for each application field we *generate a regex from the
record's value* that matches every valid way that value can legitimately appear
on a label, then search it across the label's OCR text. A field is verified when
its generated pattern is found; otherwise it is flagged.

Every pattern is case-insensitive (TTB allows any casing for most mandatory
information, and OCR casing is unreliable). Two field types need more than a
literal-with-variations regex and are handled here as helpers the matcher calls:

  net contents   -> quantities are *canonicalized to millilitres* so 750 mL,
                    0.75 L and 75 cL compare equal, and a compound value
                    ("16 fl oz (1 pint)") is any-one-matches.
  country origin -> a domestic value (USA / a US state) is satisfied by *any*
                    US state name, its uppercase postal abbreviation, or a
                    spelling of USA appearing on the label.

The gazetteers (US states, abbreviations, countries) are reused from
``app.extraction`` so there is a single source of truth.
"""
from __future__ import annotations

import re

from app import extraction

# --------------------------------------------------------------------------- #
# Loose phrase patterns (brand, generic text)
# --------------------------------------------------------------------------- #


def loose_pattern(value: str, gap: str = r"\W*") -> str | None:
    """A regex body matching ``value`` tolerant of punctuation, spacing and case.

    The value is reduced to its word tokens; consecutive tokens are joined by
    ``gap`` (default: any run of non-word characters, including OCR line breaks),
    so ``STONE'S THROW`` matches "Stone's Throw", "STONES  THROW" and a copy
    split across two OCR lines. A standalone ``and`` token also matches ``&``.
    Returns ``None`` when the value has no word characters.
    """
    tokens = re.findall(r"\w+", (value or "").lower())
    if not tokens:
        return None
    parts = [r"(?:and|&)" if t == "and" else re.escape(t) for t in tokens]
    return gap.join(parts)


def brand_regex(value: str) -> re.Pattern | None:
    body = loose_pattern(value)
    return re.compile(body, re.IGNORECASE) if body else None


# --------------------------------------------------------------------------- #
# Class / type
# --------------------------------------------------------------------------- #

# Spelling variants the TTB designation list itself carries (ClassLookUp.csv has
# both "Whisky" and "Whiskey"; wine uses "rosé"/"rose"). Applied per token so the
# generated pattern matches either spelling regardless of which the record used.
_TOKEN_VARIANTS = {
    "whisky": r"whisk(?:e)?y",
    "whiskey": r"whisk(?:e)?y",
    "rose": r"ros(?:e|é)",
    "rosé": r"ros(?:e|é)",
    "colour": r"colou?r",
    "color": r"colou?r",
}


def _class_token(tok: str) -> str:
    return _TOKEN_VARIANTS.get(tok.lower(), re.escape(tok))


def class_regex(value: str) -> re.Pattern | None:
    """Pattern for a class/type designation.

    Matches either the full designation (spelling-variant aware) or its head word
    alone — the commodity noun a label is most likely to print. So an application
    "STRAIGHT WHISKEY" is satisfied by a label reading "Whisky",
    "Kentucky Straight Bourbon Whiskey" or the full phrase; "TABLE RED WINE" by a
    label simply stating "Red Wine".
    """
    tokens = re.findall(r"\w+", (value or "").lower())
    if not tokens:
        return None
    full = r"\W*".join(_class_token(t) for t in tokens)
    head = _class_token(tokens[-1])
    body = full if head == full else f"(?:{full}|{head})"
    return re.compile(body, re.IGNORECASE)


# --------------------------------------------------------------------------- #
# ABV (numeric, with proof equivalent)
# --------------------------------------------------------------------------- #


def _num_body(value: float) -> str:
    """Regex for a number allowing insignificant trailing zeros (5 -> 5, 5.0)."""
    if value == int(value):
        return rf"{int(value)}(?:\.0+)?"
    return re.escape(f"{value:g}") + r"0*"


# The ABV form field accepts however an operator reads the strength off a label —
# "40", "40%", "40 %", "40 pct", "40% abv" — so we pull out the first number and
# ignore any percent/unit decoration. Returns None when there is no number.
_ABV_NUM_RE = re.compile(r"[-+]?\d*\.?\d+")


def parse_abv_value(value) -> float | None:
    """Extract the numeric ABV from a free-text entry, or None if there is none."""
    if value is None:
        return None
    m = _ABV_NUM_RE.search(str(value))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def abv_regex(value: float) -> re.Pattern:
    """Pattern matching the ABV figure on a label, in percent or as proof.

    ``45`` matches "45%", "45% alc/vol", "45 % ABV", and the proof equivalent
    "90 proof" (proof = 2 × ABV) — proof being the unit variation/canonical form
    for alcohol strength.
    """
    pct = rf"{_num_body(value)}\s*%\s*(?:alc|abv|alcohol|vol)?"
    proof = rf"{_num_body(value * 2)}\s*proof"
    return re.compile(rf"(?:{pct}|{proof})", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Net contents (canonical millilitres + unit-variation regex)
# --------------------------------------------------------------------------- #

# Every supported unit -> millilitres. Beer in the dataset is stated in gallons
# (keg sizes) and pints, so those join the wine/spirits mL/cL/L/fl-oz set.
_ML_PER_UNIT = {
    "ml": 1.0, "milliliter": 1.0, "milliliters": 1.0,
    "millilitre": 1.0, "millilitres": 1.0,
    "cl": 10.0, "centiliter": 10.0, "centilitre": 10.0,
    "l": 1000.0, "liter": 1000.0, "liters": 1000.0,
    "litre": 1000.0, "litres": 1000.0,
    "floz": 29.5735, "fluidounce": 29.5735, "fluidounces": 29.5735,
    "pt": 473.176, "pint": 473.176, "pints": 473.176,
    "qt": 946.353, "quart": 946.353, "quarts": 946.353,
    "gal": 3785.41, "gallon": 3785.41, "gallons": 3785.41,
}

# One number + one unit. Units are ordered longest-first inside each group so the
# alternation is greedy ("milliliters" before "ml"). Multi-word units ("fl oz",
# "fluid ounce") allow internal whitespace/periods.
_UNIT_ALT = (
    r"milliliters?|millilitres?|ml|"
    r"centiliters?|centilitres?|cl|"
    r"liters?|litres?|l|"
    r"fl\.?\s*oz\.?|fluid\s+ounces?|"
    r"gallons?|gal\.?|"
    r"pints?|pt\.?|"
    r"quarts?|qt\.?"
)
# An optional system qualifier between the number and the unit — kegs and other
# US measures are often printed "5.17 US Gallon" / "5.17 U.S. Gal" (and imports as
# "Imp. gallon"). It's matched non-capturing so the captured unit stays clean for
# _unit_key; US and imperial gallons differ in volume, but the dataset's gallon is
# the US gallon (already _ML_PER_UNIT["gallon"]), so we treat the qualifier as a
# label affordance rather than a separate unit.
_SYS_QUALIFIER = r"(?:u\.?\s*s\.?|imp(?:erial)?\.?)\s*"
_QTY_RE = re.compile(
    rf"(\d+(?:\.\d+)?)\s*(?:{_SYS_QUALIFIER})?({_UNIT_ALT})\b", re.IGNORECASE)


def _unit_key(raw: str) -> str:
    """Normalize a matched unit string to a key in ``_ML_PER_UNIT``."""
    return re.sub(r"[.\s]+", "", raw.lower())


def parse_volumes_ml(text: str) -> list[float]:
    """Every volume occurrence in ``text``, each converted to millilitres."""
    out: list[float] = []
    for m in _QTY_RE.finditer(text or ""):
        factor = _ML_PER_UNIT.get(_unit_key(m.group(2)))
        if factor is not None:
            out.append(float(m.group(1)) * factor)
    return out


def parse_volume_ml(text: str) -> float | None:
    """The first volume in ``text`` in millilitres (back-compat helper)."""
    vols = parse_volumes_ml(text)
    return vols[0] if vols else None


# Characters OCR commonly substitutes for digits in a faint/thin printed volume
# ("750 ML" read as "75O ML"). Applied ONLY to the numeric run before a unit —
# never to the unit itself — and only via parse_volumes_ml_lenient, which callers
# use as a rescue when the strict parse finds nothing. The single-letter litre
# unit "l" is deliberately excluded from the number class so it can't be eaten.
_OCR_DIGIT_FIX = str.maketrans({
    "O": "0", "o": "0", "Q": "0", "q": "0", "D": "0", "d": "0",
    "I": "1", "i": "1", "S": "5", "s": "5", "B": "8", "b": "8",
})
_LENIENT_QTY_RE = re.compile(
    rf"([0-9OoQqDdIiSsBb]+(?:[.,][0-9OoQqDdIiSsBb]+)?)\s*"
    rf"(?:{_SYS_QUALIFIER})?({_UNIT_ALT})\b",
    re.IGNORECASE)


def parse_volumes_ml_lenient(text: str) -> list[float]:
    """Like :func:`parse_volumes_ml`, but tolerating common OCR digit confusions
    (O→0, I→1, S→5, B→8) in the numeric part. Intended only as a fallback when
    the strict parse finds nothing, and callers should keep only a result that
    equals an expected volume — so a misread can rescue a match without inventing
    one."""
    out: list[float] = []
    for m in _LENIENT_QTY_RE.finditer(text or ""):
        num = m.group(1).translate(_OCR_DIGIT_FIX).replace(",", ".")
        try:
            val = float(num)
        except ValueError:
            continue
        factor = _ML_PER_UNIT.get(_unit_key(m.group(2)))
        if factor is not None:
            out.append(val * factor)
    return out


def volumes_equal(a: float, b: float) -> bool:
    """Equal within rounding tolerance for unit conversion (≥0.5 mL or 0.5%)."""
    return abs(a - b) <= max(0.5, max(a, b) * 0.005)


# --------------------------------------------------------------------------- #
# Country of origin
#
# A domestic product states its origin as a US locality: an address ending in a
# state ("...Waterford, Virginia" / "...Newberg, OR 97132") or an explicit
# "Product of USA". So a USA / US-state application value is verified by ANY US
# state name, its UPPERCASE postal abbreviation, or a spelling of USA on the
# label. A foreign value is matched by its country name (loose, OCR-tolerant).
# --------------------------------------------------------------------------- #

# Full state names (case-insensitive) + USA wordings, with word boundaries so
# "america" doesn't fire on "American" and "usa" doesn't fire inside a word.
_STATE_NAME_ALT = "|".join(re.escape(s) for s in sorted(extraction._US_STATES,
                                                         key=len, reverse=True))
_USA_FORM_ALT = r"u\.?\s*s\.?\s*a\.?|united\s+states(?:\s+of\s+america)?|america"
_US_ORIGIN_RE = re.compile(rf"\b(?:{_STATE_NAME_ALT}|{_USA_FORM_ALT})\b",
                           re.IGNORECASE)

# Postal abbreviations are only trusted when UPPERCASE in the source, so the
# English words "in", "or", "me", "hi"… don't masquerade as states. Hence this
# pattern is case-SENSITIVE (no re.IGNORECASE).
_STATE_ABBR_RE = re.compile(
    r"\b(?:" + "|".join(sorted((a.upper() for a in extraction._STATE_ABBR),
                               key=len, reverse=True)) + r")\b")


def is_domestic_value(value: str) -> bool:
    """True if the application origin is the USA or a US state."""
    core = re.sub(r"[^\w\s]", " ", (value or "").lower())
    if re.search(r"\b(usa|us|united states|america)\b", core):
        return True
    tokens = core.split()
    for n in (3, 2, 1):
        for i in range(len(tokens) - n + 1):
            if " ".join(tokens[i:i + n]) in extraction._US_STATES:
                return True
    return False


def us_origin_on_label(text: str) -> str | None:
    """Return the US origin token found on the label (state/abbr/USA form), or None."""
    if not text:
        return None
    m = _US_ORIGIN_RE.search(text)
    if m:
        return m.group(0)
    m = _STATE_ABBR_RE.search(text)
    return m.group(0) if m else None


def country_regex(value: str) -> re.Pattern | None:
    """Loose pattern for a foreign country-of-origin name."""
    body = loose_pattern(value)
    return re.compile(body, re.IGNORECASE) if body else None


# --------------------------------------------------------------------------- #
# Introspection: the generated pattern per field (for the UI's "show pattern")
# --------------------------------------------------------------------------- #


def field_patterns(application) -> list[dict]:
    """The regex generated from each application value, for display.

    Mirrors what the matcher searches the label text for (see module docstring).
    A few fields aren't a single value-derived literal regex — net contents is
    canonicalized to millilitres, and a *domestic* origin is satisfied by any US
    locality — so for those the closest equivalent matcher is shown. ``application``
    is duck-typed (any object exposing the application-field attributes), so this
    module stays free of an ``app.pipeline`` import.
    """
    out: list[dict] = []

    def add(field: str, value: str, pattern: str) -> None:
        out.append({"field": field, "value": value, "pattern": pattern})

    if getattr(application, "brand_name", ""):
        rx = brand_regex(application.brand_name)
        add("Brand name", application.brand_name, rx.pattern if rx else "—")

    if getattr(application, "class_type", ""):
        rx = class_regex(application.class_type)
        add("Class/type", application.class_type, rx.pattern if rx else "—")

    if getattr(application, "abv", ""):
        abv_val = parse_abv_value(application.abv)
        if abv_val is not None:
            add("Alcohol content (ABV)", application.abv, abv_regex(abv_val).pattern)

    if getattr(application, "net_contents", ""):
        vols = parse_volumes_ml(application.net_contents)
        ml = ", ".join(f"{v:g} mL" for v in vols) or "(could not parse a volume)"
        add("Net contents", application.net_contents,
            f"{_QTY_RE.pattern}  →  canonical: {ml}")

    if getattr(application, "country_of_origin", ""):
        if is_domestic_value(application.country_of_origin):
            add("Country of origin", application.country_of_origin,
                f"{_US_ORIGIN_RE.pattern}  (or an UPPERCASE state abbreviation)")
        else:
            rx = country_regex(application.country_of_origin)
            add("Country of origin", application.country_of_origin,
                rx.pattern if rx else "—")

    return out


# --------------------------------------------------------------------------- #
# "Found on label": the exact substring of the OCR text a field's pattern hit
#
# For display only. Reuses the same generated patterns the matcher searches with,
# so the "Found on label" column shows the literal text on the label that
# satisfied each field — rather than a separately-extracted/normalized value.
# Returns None when the field's value is blank or nothing matches (the caller
# then leaves the existing display untouched).
# --------------------------------------------------------------------------- #


def _tidy(s: str) -> str:
    """Collapse OCR line breaks / runs of whitespace for a one-line display."""
    return re.sub(r"\s+", " ", s or "").strip()


def _matched_volume_text(value: str, text: str) -> str | None:
    """The label volume token (e.g. "750 ML") that equals an expected volume;
    failing that, the first volume token on the label."""
    exp = parse_volumes_ml(value) if value else []
    first = None
    for m in _QTY_RE.finditer(text):
        factor = _ML_PER_UNIT.get(_unit_key(m.group(2)))
        if factor is None:
            continue
        if first is None:
            first = m.group(0)
        ml = float(m.group(1)) * factor
        if exp and any(volumes_equal(ml, e) for e in exp):
            return _tidy(m.group(0))
    return _tidy(first) if first else None


def _matched_origin_text(value: str, text: str) -> str | None:
    """The foreign country name as printed on the label. Domestic values return
    None: a US origin is satisfied by *any* state/abbreviation/USA wording, which
    collides with business suffixes ("…Whiskey Co." → "CO"), so the existing
    "USA" display stays clearer than a raw token."""
    if is_domestic_value(value):
        return None
    rx = country_regex(value)
    m = rx.search(text) if rx else None
    return _tidy(m.group(0)) if m else None


def matched_on_label(field: str, value: str, label_text: str) -> str | None:
    """The exact substring of ``label_text`` that ``field``'s generated pattern
    matches, for the "Found on label" column. ``field`` is the FieldResult label
    (e.g. "Brand name"); ``value`` is the application value."""
    text = label_text or ""
    if not text or not value:
        return None
    f = (field or "").lower()

    if f.startswith("net"):
        return _matched_volume_text(value, text)
    if f.startswith("country"):
        return _matched_origin_text(value, text)

    rx = None
    if f.startswith("brand"):
        rx = brand_regex(value)
    elif f.startswith("class"):
        rx = class_regex(value)
    elif f.startswith("alcohol") or "abv" in f:
        abv_val = parse_abv_value(value)
        rx = abv_regex(abv_val) if abv_val is not None else None
    if rx is None:
        return None
    m = rx.search(text)
    return _tidy(m.group(0)) if m else None
