"""
Per-field matching rules.

The model: for each application field, generate a regex from the *record's* value
that matches every valid way that value can appear on a label (see
``app.patterns``), then search it across the label's OCR text. A field is verified
when its pattern is found; otherwise it is flagged. Because OCR substitutes the
odd character ("BREWlNG" for "BREWING"), the generated pattern is tried first and
a fuzzy similarity check backs it up before anything is declared a mismatch — the
government health warning is the one exception (it is checked exactly, in
``app.warning``).

Comparisons don't return a bare pass/fail. Each returns a FieldResult carrying
the verdict, a confidence and a human-readable note, and surfaces normalization
("STONE'S THROW" vs "Stone's Throw" is obviously the same brand).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from rapidfuzz import fuzz

from app import patterns
from app.patterns import parse_volume_ml, parse_volumes_ml  # re-exported

# Verdict vocabulary (rendered with word + symbol + colour, never colour alone).
MATCH = "match"
MATCH_NORMALIZED = "match_normalized"
MISMATCH = "mismatch"               # label states a value, but it disagrees with the application
MISSING = "missing"                 # a required value the application gives is absent from the label
PARTIAL_MATCH = "partial_match"     # only part of the name matches (extra/missing words)
NOT_FOUND = "not_found"             # nothing to verify: blank in the application AND absent on the label
NOT_APPLICABLE = "not_applicable"   # field not required for this beverage class
ASSUMED = "assumed"                 # read off the label, but not given in the form to verify against

# Default similarity floor for the fuzzy fallback (OCR-noise tolerance).
_FUZZY_THRESHOLD = 0.85


@dataclass
class FieldResult:
    field: str
    expected: str
    found: str
    verdict: str
    confidence: float
    note: str

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Normalization helpers
# --------------------------------------------------------------------------- #

def normalize_text(value: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    value = value.lower()
    value = re.sub(r"[^\w\s]", " ", value)       # punctuation -> space
    value = re.sub(r"\s+", " ", value)
    return value.strip()


# --------------------------------------------------------------------------- #
# Brand name: pattern from the record value, fuzzy fallback for OCR noise
# --------------------------------------------------------------------------- #

def match_brand(expected: str, found: str, full_text: str | None = None) -> FieldResult:
    field = "Brand name"
    if not expected:
        # Blank in the form: read off the label -> assumed; nothing read -> not found.
        if not found:
            return FieldResult(field, "", found, NOT_FOUND, 0.0,
                               "Could not locate a brand name on the label.")
        return FieldResult(field, "", found, ASSUMED, 1.0,
                           "Read from the label; not given in the application to verify against.")

    if expected == found:
        return FieldResult(field, expected, found, MATCH, 1.0, "Exact match.")

    body = patterns.loose_pattern(expected)

    # The generated pattern matches the whole candidate after normalizing case,
    # punctuation and spacing ("STONE'S THROW" vs "Stone's Throw").
    if body and found and re.fullmatch(body, found, re.IGNORECASE):
        return FieldResult(
            field, expected, found, MATCH_NORMALIZED, 0.98,
            "Match after normalizing case, punctuation and spacing "
            f'("{found}" vs "{expected}").',
        )

    # The brand appears *within* the label text — e.g. "Calvert Brewing Company"
    # inside "Calvert Brewing Company LLC", or split across OCR lines so the single
    # tallest block was only part of it. This is the core model (generate a regex
    # from the record's value, search it across the label text) and catches a brand
    # the block-by-block candidate missed.
    #
    # A MULTI-WORD brand present in order is a strong signal even when it abuts
    # other text ("PullmanPILSNER…"): the inter-token separators already guard
    # against accidental hits, so it's searched unguarded (and stays consistent
    # with the "Found on label" display, which is also unguarded). A SINGLE-token
    # brand keeps word boundaries so a short name can't fire inside a larger word
    # ("Cain" in "Cocaine").
    if body and full_text:
        multi_word = len(re.findall(r"\w+", expected)) >= 2
        pattern = body if multi_word else rf"(?<!\w)(?:{body})(?!\w)"
        m = re.search(pattern, full_text, re.IGNORECASE)
        if m:
            hit = re.sub(r"\s+", " ", m.group(0)).strip()
            return FieldResult(field, expected, found or hit, MATCH_NORMALIZED, 0.97,
                               f'Brand found on the label ("{hit}").')

    if not found:
        # The application names a brand, but none was located on the label.
        return FieldResult(field, expected, found, MISSING, 0.0,
                           "Brand name not found on the label.")

    norm_exp, norm_found = normalize_text(expected), normalize_text(found)
    # Tolerate OCR noise / minor wording with a fuzzy score on normalized text.
    score = fuzz.ratio(norm_exp, norm_found) / 100.0
    if score >= _FUZZY_THRESHOLD:
        return FieldResult(
            field, expected, found, MATCH_NORMALIZED, score,
            f"Close match after normalization (similarity {score:.0%}); "
            "likely OCR noise.",
        )
    # Partial match: one name appears within the other (extra or missing words),
    # e.g. "Cointreau" vs "Cointreau Liqueur" — they share text but aren't equal.
    partial = fuzz.partial_ratio(norm_exp, norm_found) / 100.0
    if partial >= _FUZZY_THRESHOLD:
        return FieldResult(
            field, expected, found, PARTIAL_MATCH, partial,
            f'Partial match: the label brand ("{found}") and the application '
            f'("{expected}") overlap but are not the same '
            f"(overlap {partial:.0%}, overall {score:.0%}). Please review.",
        )
    return FieldResult(
        field, expected, found, MISMATCH, score,
        f'Brand on label ("{found}") does not match the application '
        f'("{expected}"); similarity {score:.0%}.',
    )


# --------------------------------------------------------------------------- #
# Class/type: pattern (full designation or its head word) across the label text
# --------------------------------------------------------------------------- #

def match_class_type(expected: str, found: str, threshold: float = _FUZZY_THRESHOLD,
                     expected_display: str | None = None) -> FieldResult:
    """Verify the application's class/type against the label text.

    The generated pattern matches either the full designation or its commodity
    head word (so "STRAIGHT WHISKEY" is satisfied by a label reading "Whisky" or
    "Kentucky Straight Bourbon Whiskey"). ``expected_display`` overrides only the
    "Expected" column (e.g. the looked-up superclass); the verdict is still
    computed from the raw ``expected`` vs the label.
    """
    field = "Class/type"
    shown = expected_display if expected_display is not None else expected
    if not expected:
        return FieldResult(field, shown, found, NOT_FOUND, 0.0,
                           "No class/type provided in the application.")
    if not found:
        return FieldResult(field, shown, found, MISSING, 0.0,
                           "Could not find the class/type text on the label.")

    # 1. The application's own designation (or its head word) appears on the label.
    rx = patterns.class_regex(expected)
    m = rx.search(found) if rx else None
    if m:
        return FieldResult(field, shown, m.group(0), MATCH, 0.97,
                           "Class/type text found on the label.")

    # 2. Superclass-aware: the label states *some* designation in the same TTB
    #    category as the application's class (a "Malbec" label is consistent with
    #    a "TABLE RED WINE" application — both are Wine).
    from app import class_lookup
    app_cat = class_lookup.superclass_for(expected)
    label = class_lookup.label_designation(found)
    # label_designation gives the matched designation for display; superclass_for
    # is the bidirectional fallback so a partial read ("Kentucky Straight Bourbon"
    # without "Whiskey") still resolves to its category.
    label_cat = label[1] if label else class_lookup.superclass_for(found)
    if app_cat and label_cat and label_cat == app_cat:
        des = label[0] if label else found
        return FieldResult(field, shown, des, MATCH, 0.95,
                           f'Label states "{des}", a {app_cat} class consistent '
                           "with the application.")

    # 3. Fuzzy fallback: the designation appears with OCR noise / across lines.
    norm_exp, norm_found = normalize_text(expected), normalize_text(found)
    score = fuzz.partial_ratio(norm_exp, norm_found) / 100.0
    if score >= threshold:
        return FieldResult(field, shown, found, MATCH_NORMALIZED, score,
                           f"Fuzzy match (ratio {score:.0%} ≥ {threshold:.0%}).")
    # The label states a recognized designation in a *different* TTB category ->
    # a genuine mismatch; no recognized class designation at all -> missing.
    if label_cat:
        return FieldResult(field, shown, found, MISMATCH, score,
                           f"Label states a {label_cat} designation, which "
                           f"differs from the application (best ratio {score:.0%}).")
    return FieldResult(field, shown, found, MISSING, score,
                       f"Class/type designation not found on the label "
                       f"(best ratio {score:.0%}).")


# --------------------------------------------------------------------------- #
# ABV: numeric equality, with proof cross-check
#
# Alcohol strength is matched numerically rather than by raw substring: proof is
# the unit variation (proof = 2 × ABV), so canonicalizing to ABV lets "90 proof"
# verify a 45% application value. See app.patterns.abv_regex for the equivalent
# regex form.
# --------------------------------------------------------------------------- #

def _to_float(value: str) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def match_abv(expected: str, found_abv: float | None,
              found_proof: float | None = None) -> FieldResult:
    field = "Alcohol content (ABV)"
    exp = _to_float(expected)
    found_display = (
        f"{found_abv:g}% ABV" if found_abv is not None
        else (f"{found_proof:g} proof" if found_proof is not None else "")
    )

    if exp is None:
        # No application value to compare against. If the OCR still read an ABV
        # off the label, treat the field as verified (present on the label);
        # otherwise it's genuinely absent.
        if found_abv is not None or found_proof is not None:
            return FieldResult(field, "", found_display, ASSUMED, 1.0,
                               "Read from the label; not given in the application to verify against.")
        return FieldResult(field, "", "", NOT_FOUND, 0.0,
                           "No application value provided and none found on the label.")
    if found_abv is None and found_proof is None:
        return FieldResult(field, f"{exp:g}%", "", MISSING, 0.0,
                           "No alcohol content found on the label.")

    # Prefer a directly-read ABV; otherwise derive from proof (proof = 2 x ABV).
    derived_from_proof = None
    if found_abv is None and found_proof is not None:
        derived_from_proof = found_proof / 2.0

    candidate = found_abv if found_abv is not None else derived_from_proof
    matched = abs(candidate - exp) < 0.05    # 45 == 45.0

    # Proof cross-check when both ABV and proof are present on the label.
    proof_note = ""
    if found_abv is not None and found_proof is not None:
        if abs(found_proof - 2 * found_abv) < 0.6:
            proof_note = f" Proof {found_proof:g} is consistent with {found_abv:g}% ABV."
        else:
            proof_note = (
                f" Warning: proof {found_proof:g} is NOT 2× the ABV {found_abv:g}%."
            )

    if matched:
        src = "" if found_abv is not None else f" (derived from {found_proof:g} proof)"
        return FieldResult(field, f"{exp:g}%", found_display, MATCH, 1.0,
                           f"Alcohol content matches{src}.{proof_note}")
    return FieldResult(field, f"{exp:g}%", found_display, MISMATCH, 0.0,
                       f"Label reads {candidate:g}% but application says "
                       f"{exp:g}%.{proof_note}")


# --------------------------------------------------------------------------- #
# Class-aware ABV: applies the per-class profile on top of the base ABV rule
# --------------------------------------------------------------------------- #

_TABLE_WINE_RE = re.compile(r"\b(table|light)\s+wine\b", re.IGNORECASE)


def match_abv_classed(rules, expected: str, found_abv: float | None,
                      found_proof: float | None, full_text: str) -> FieldResult:
    """ABV verdict adjusted for the beverage class (see app.classes.ClassRules).

    When the label *shows* an alcohol figure, the base numeric rule stands. Only
    the label-shows-nothing case is class-dependent:
      - wine stating "Table Wine"/"Light Wine"  -> match (number not required);
      - class where ABV is optional (malt)      -> N/A (doesn't count);
      - class where ABV is required (spirits, wine without table-wine wording)
        -> mismatch, regardless of whether the application supplied a value.
    """
    base = match_abv(expected, found_abv, found_proof)
    if found_abv is not None or found_proof is not None:
        return base  # the label shows an alcohol figure; the base verdict holds

    field = base.field
    expected_display = base.expected
    if rules.abv_allows_table_wine and _TABLE_WINE_RE.search(full_text or ""):
        return FieldResult(field, expected_display, "Table Wine", MATCH, 1.0,
                           "Stated as 'Table Wine' in lieu of a numeric ABV "
                           "(permitted for 7–14% wine).")
    if rules.abv_required:
        return FieldResult(field, expected_display, "", MISSING, 0.0,
                           "Alcohol content is required for this beverage class "
                           "but none was found on the label.")
    return FieldResult(field, expected_display, "", NOT_APPLICABLE, 1.0,
                       "Alcohol content not shown — not required for this class "
                       "(e.g. malt beverages).")


# --------------------------------------------------------------------------- #
# Sulfite declaration (wine): "Contains Sulfites" required at ≥10 ppm
# --------------------------------------------------------------------------- #

_SULFITE_RE = re.compile(r"contains?\s+sulfites?", re.IGNORECASE)


def check_sulfites(full_text: str) -> FieldResult:
    field = "Sulfite declaration"
    if _SULFITE_RE.search(full_text or ""):
        return FieldResult(field, "Contains Sulfites", "Contains Sulfites",
                           MATCH, 1.0, "Sulfite declaration present.")
    return FieldResult(field, "Contains Sulfites", "", MISSING, 0.0,
                       "No 'Contains Sulfites' declaration found "
                       "(required for wine at ≥10 ppm).")


# --------------------------------------------------------------------------- #
# Net contents: quantities canonicalized to millilitres, any-one-matches
#
# The application value may list several quantities — a keg pair
# ("15.5 gal / 5.2 gal") or a dual statement ("16 fl oz (1 pint)"). Each is
# converted to millilitres; the field is verified when ANY of them equals (within
# unit-conversion tolerance) ANY volume printed on the label. ``full_text``, when
# given, is parsed for label volumes so a compound size printed only on the back
# is still found; otherwise ``found`` is used as both the label text and display.
# --------------------------------------------------------------------------- #

def match_net_contents(expected: str, found: str,
                       full_text: str | None = None) -> FieldResult:
    field = "Net contents"
    label_text = full_text if full_text is not None else found
    label_vols = parse_volumes_ml(label_text)
    # When the verbatim text is supplied separately, also trust a structured /
    # extracted reading passed as `found` (e.g. a vision model's normalized
    # net-contents) so a clean reading still verifies when the transcription
    # garbled the printed volume.
    if full_text is not None and found:
        for v in parse_volumes_ml(found):
            if v not in label_vols:
                label_vols.append(v)
    display_found = found or (label_text if full_text is None else "")

    if not expected:
        # Blank in the form: verified if the OCR read a volume, else absent.
        if label_vols:
            return FieldResult(field, "", display_found, ASSUMED, 1.0,
                               "Read from the label; not given in the application to verify against.")
        return FieldResult(field, "", display_found, NOT_FOUND, 0.0,
                           "No application value provided and none found on the label.")

    exp_vols = parse_volumes_ml(expected)
    # OCR rescue: nothing parsed strictly, but the application gives a volume.
    # Retry tolerating common digit confusions (O→0, I→1, S→5) and keep ONLY a
    # reading that equals an expected volume — so a faint "750 ML" misread as
    # "75O ML" still verifies, without ever inventing a match.
    rescued = False
    if not label_vols and exp_vols:
        cand = patterns.parse_volumes_ml_lenient(label_text)
        if full_text is not None and found:
            cand += patterns.parse_volumes_ml_lenient(found)
        hits = [v for v in cand if any(patterns.volumes_equal(v, e) for e in exp_vols)]
        if hits:
            label_vols, rescued = hits, True

    if not label_vols:
        return FieldResult(field, expected, display_found, MISSING, 0.0,
                           "Could not read a net-contents volume on the label.")
    if not exp_vols:
        return FieldResult(field, expected, display_found, NOT_FOUND, 0.0,
                           f'Could not interpret the application value "{expected}".')

    for e in exp_vols:
        for f in label_vols:
            if patterns.volumes_equal(e, f):
                if rescued:
                    return FieldResult(field, expected, display_found, MATCH_NORMALIZED, 0.9,
                                       "Matched after correcting a likely OCR misread "
                                       f'of the printed volume ("{expected}").')
                same_units = (expected.strip().lower().replace(" ", "")
                              == (found or "").strip().lower().replace(" ", ""))
                if same_units:
                    return FieldResult(field, expected, display_found, MATCH, 1.0,
                                       "Exact match.")
                return FieldResult(field, expected, display_found, MATCH_NORMALIZED, 1.0,
                                   f"Equal after unit conversion ({e:g} mL on the label "
                                   f'matches "{expected}").')

    return FieldResult(field, expected, display_found, MISMATCH, 0.0,
                       f"Volumes differ: label {label_vols[0]:g} mL vs application "
                       f"{exp_vols[0]:g} mL.")


# --------------------------------------------------------------------------- #
# Country of origin (mandatory)
#
# A domestic application value (USA / a US state) is verified by ANY US state
# name, its uppercase postal abbreviation, or a spelling of USA on the label —
# that is how a domestic product states origin (an address ending in a state, or
# "Product of USA"). A foreign value is matched by its country name. A blank form
# value is surfaced as "assumed" when the label carries an origin statement, and
# "not found" otherwise (read via app.extraction so a place name inside the brand
# is ignored).
# --------------------------------------------------------------------------- #

def _display_origin(value: str) -> str:
    """Display form: drop the parenthetical state, so "USA (Oregon)" -> "USA"."""
    if not value:
        return value
    return re.sub(r"\s*\([^)]*\)", "", value).strip()


def match_country_of_origin(expected: str, full_text: str,
                            brand_name: str = "") -> FieldResult:
    field = "Country of origin"
    from app import extraction
    found_on_label = extraction.extract_country_of_origin(full_text or "", brand_name)
    found_disp = _display_origin(found_on_label)

    if not expected:
        # Blank in the form: assumed if the OCR read an origin statement, else absent.
        if found_on_label:
            return FieldResult(field, "", found_disp, ASSUMED, 1.0,
                               "Read from the label; not given in the application to verify against.")
        return FieldResult(field, "", "", NOT_FOUND, 0.0,
                           "No application value provided and no origin statement found "
                           "on the label.")

    exp_disp = _display_origin(expected)

    # Domestic: any US locality (state name / uppercase abbr) or USA wording.
    if patterns.is_domestic_value(expected):
        hit = patterns.us_origin_on_label(full_text or "")
        if hit:
            return FieldResult(field, exp_disp, "USA", MATCH, 0.97,
                               f'Domestic origin confirmed on the label ("{hit}").')
        # A different (non-US) origin printed on the label is a mismatch; no origin
        # statement at all is a missing mandatory field.
        if found_disp:
            return FieldResult(field, exp_disp, found_disp, MISMATCH, 0.0,
                               f'Application says "{exp_disp}" but the label shows '
                               f'"{found_disp}".')
        return FieldResult(field, exp_disp, found_disp, MISSING, 0.0,
                           f'Application country "{exp_disp}" was not confirmed on the '
                           "label (no US state, abbreviation or USA statement found).")

    # Foreign: the country name appears on the label (loose, OCR-tolerant).
    rx = patterns.country_regex(expected)
    if rx and rx.search(full_text or ""):
        return FieldResult(field, exp_disp, found_disp or exp_disp, MATCH, 0.97,
                           "Country of origin matches the statement on the label.")
    norm_exp, norm_text = normalize_text(expected), normalize_text(full_text)
    score = (fuzz.partial_ratio(norm_exp, norm_text) / 100.0) if norm_text else 0.0
    if score >= _FUZZY_THRESHOLD:
        return FieldResult(field, exp_disp, found_disp or exp_disp, MATCH_NORMALIZED, score,
                           f"Close match on the label (similarity {score:.0%}); "
                           "likely OCR noise.")
    # The label states a *different* origin -> mismatch; no origin statement at
    # all -> the mandatory field is missing.
    if found_disp and found_disp != exp_disp:
        return FieldResult(field, exp_disp, found_disp, MISMATCH, 0.0,
                           f'Application says "{exp_disp}" but the label shows '
                           f'"{found_disp}".')
    return FieldResult(field, exp_disp, found_disp, MISSING, 0.0,
                       f'Application country "{exp_disp}" was not found on the label.')
