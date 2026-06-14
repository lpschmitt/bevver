"""
Government Health Warning verification (27 CFR 16.21).

This field is special. Two things are checked and reported *separately*:

  (a) content : does the warning text match the statutory text, allowing for OCR
                noise *and* the word-order scrambling that multi-column label
                layouts produce (OCR reads column-by-column, so the two numbered
                sentences can come back out of order or interleaved)?
  (b) case    : is the "GOVERNMENT WARNING:" prefix in EXACT uppercase as it was
                read off the label?

Title-case ("Government Warning:") is a real, documented rejection reason, so the
case check is strict and never folded into the fuzzy content check.

Why not a single linear edit-distance? Real labels print the warning in two
columns, so OCR returns the sentences reordered/interleaved against arbitrary
other label text. A linear diff against the statutory string then reports a huge
distance even when every word is present and correct. Instead we check that each
statutory *unit* (the prefix and the two numbered sentences) is present somewhere
in the OCR text with high fuzzy similarity — order-independent, but a removed or
reworded sentence still drops below threshold and is caught.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

# Statutory text, 27 CFR 16.21.
STATUTORY_WARNING = (
    "GOVERNMENT WARNING: (1) According to the Surgeon General, women should not "
    "drink alcoholic beverages during pregnancy because of the risk of birth "
    "defects. (2) Consumption of alcoholic beverages impairs your ability to "
    "drive a car or operate machinery, and may cause health problems."
)

# The exact prefix that must appear in all caps.
WARNING_PREFIX = "GOVERNMENT WARNING:"

# Per-unit fuzzy similarity required for a statutory unit to count as "present".
# High enough that a reworded/removed sentence fails, low enough to absorb the
# single-character OCR slips real scans contain.
UNIT_PRESENT_THRESHOLD = 88

# Anchor used to locate the warning inside arbitrary OCR text. Case-insensitive
# and whitespace-*optional* (``\s*``) so it still matches when OCR mangles the
# casing or merges the two words into "GOVERNMENTWARNING".
_ANCHOR_RE = re.compile(r"government\s*warning", re.IGNORECASE)
# Same span, case-SENSITIVE, for the prefix-case check.
_ANCHOR_UPPER_RE = re.compile(r"GOVERNMENT\s*WARNING")


def _normalize_for_content(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace — so the content compare
    ignores case, the "(1)/(2)" markers, and OCR spacing."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _statutory_units() -> list[str]:
    """The statutory text split into its prefix + two numbered sentences, each
    normalized. Derived from STATUTORY_WARNING so the two never drift apart."""
    body = STATUTORY_WARNING
    prefix = body.split("(1)")[0]                       # "GOVERNMENT WARNING: "
    s1 = body.split("(1)")[1].split("(2)")[0]           # sentence 1
    s2 = body.split("(2)")[1]                           # sentence 2
    return [_normalize_for_content(p) for p in (prefix, s1, s2)]


_UNITS = _statutory_units()


def locate_warning(ocr_text: str) -> str | None:
    """Return the warning substring from full OCR text, starting at the
    'GOVERNMENT WARNING' anchor. Returns None if the anchor is absent."""
    m = _ANCHOR_RE.search(ocr_text)
    if not m:
        return None
    start = m.start()
    return ocr_text[start:start + len(STATUTORY_WARNING) + 80].strip()


@dataclass
class WarningResult:
    found: bool
    content_ok: bool
    content_distance: int          # number of statutory units NOT found (0 = all)
    case_ok: bool
    found_prefix: str | None       # the prefix exactly as OCR'd (for display)
    found_text: str | None
    note: str

    @property
    def verdict(self) -> str:
        if not self.found:
            # The statutory warning is absent from the label -> missing, not a
            # value mismatch (which is reserved for a warning that is present but
            # has the wrong content or casing).
            return "missing"
        if self.content_ok and self.case_ok:
            return "match"
        return "mismatch"


def verify_warning(ocr_text: str) -> WarningResult:
    norm_text = _normalize_for_content(ocr_text)

    # Presence of each statutory unit (prefix, sentence 1, sentence 2), order-
    # independent. partial_ratio finds the best-matching window anywhere in the
    # OCR text, so column scrambling between units doesn't matter.
    scores = [fuzz.partial_ratio(unit, norm_text) for unit in _UNITS]
    present = [s >= UNIT_PRESENT_THRESHOLD for s in scores]
    prefix_present, s1_present, s2_present = present
    missing = sum(1 for p in present if not p)

    anchor = _ANCHOR_RE.search(ocr_text)

    # "Found" = the warning is on the label at all: either the prefix anchor is
    # readable, or at least one full statutory sentence is present.
    found = bool(anchor) or s1_present or s2_present
    if not found:
        return WarningResult(
            found=False,
            content_ok=False,
            content_distance=-1,
            case_ok=False,
            found_prefix=None,
            found_text=None,
            note="No Government Health Warning statement found on the label.",
        )

    # (a) Content: every statutory unit must be present (order-independent).
    content_ok = prefix_present and s1_present and s2_present

    # (b) Case: the prefix as actually printed must be EXACT uppercase. A
    # case-sensitive match for "GOVERNMENT WARNING" succeeds only for all-caps;
    # title- or lower-case prints fail it. The statutory colon is treated as OCR-
    # droppable punctuation, so its absence is not a case violation.
    upper = _ANCHOR_UPPER_RE.search(ocr_text)
    case_ok = bool(upper)

    found_prefix = None
    if anchor:
        found_prefix = re.sub(r"\s+", " ", anchor.group()).strip()

    if content_ok and case_ok:
        note = "Warning text and ALL-CAPS prefix match the statutory requirement."
    elif content_ok and not case_ok:
        note = (
            f'Prefix case is wrong: read as "{found_prefix}" but '
            f'"{WARNING_PREFIX}" must be in exact uppercase. This is a rejection.'
        )
    elif not content_ok and case_ok:
        note = (
            "Prefix casing is correct but the warning wording is incomplete or "
            f"differs from the statutory text ({missing} of {len(_UNITS)} required "
            "parts not matched)."
        )
    else:
        note = (
            f"Warning wording differs ({missing} of {len(_UNITS)} required parts "
            f'not matched) and the prefix case is wrong (read "{found_prefix}").'
        )

    return WarningResult(
        found=True,
        content_ok=content_ok,
        content_distance=missing,
        case_ok=case_ok,
        found_prefix=found_prefix,
        found_text=locate_warning(ocr_text),
        note=note,
    )
