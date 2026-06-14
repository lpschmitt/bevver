"""
Class/type -> superclass lookup, backed by ``Reference/ClassLookUp.csv``.

The reference table maps a specific TTB class/type *designation* (e.g.
"Pinot Noir", "Kentucky Straight Bourbon Whiskey", "Hazy IPA") to its broad
*superclass* / Category — one of "Distilled Spirits", "Wine", "Beer/Malt".

We use it to display the superclass of the application's class/type in the
results table's "Expected" column. The lookup is normalization- and
token-aware so the dataset's free-text values ("STRAIGHT WHISKEY",
"TABLE RED WINE") resolve to the right Category even when they aren't an exact
designation in the table. It is display-only: the class/type verdict still
compares the application value against the label.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

from app.matching import normalize_text

log = logging.getLogger("app")

LOOKUP_CSV = Path(__file__).resolve().parent.parent / "Reference" / "ClassLookUp.csv"


def _load() -> tuple[dict[str, str], list[tuple[str, str]], dict[str, str]]:
    """Load the CSV into (exact-map, designations-by-length-desc, originals).

    ``exact``      : normalized designation -> Category, for O(1) exact hits.
    ``by_length``  : (normalized designation, Category) sorted longest-first, so
                     the most specific designation wins a token-substring match.
    ``originals``  : normalized designation -> original-cased designation, so a
                     match can be surfaced as it is written in the table ("Malbec").
    Missing/unreadable file degrades to empty tables (lookups return None).
    """
    exact: dict[str, str] = {}
    pairs: list[tuple[str, str]] = []
    originals: dict[str, str] = {}
    if not LOOKUP_CSV.exists():
        log.warning("Class lookup table not found at %s; superclass display disabled.",
                    LOOKUP_CSV)
        return exact, pairs, originals
    # utf-8-sig drops the BOM the file is saved with.
    with LOOKUP_CSV.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            category = (row.get("Category") or "").strip()
            designation = (row.get("Designation") or "").strip()
            norm = normalize_text(designation)
            if not category or not norm:
                continue
            exact.setdefault(norm, category)
            originals.setdefault(norm, designation)
            pairs.append((norm, category))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return exact, pairs, originals


_EXACT, _BY_LENGTH, _ORIG = _load()


def superclass_for(class_type: str) -> str | None:
    """Return the superclass (Category) for an application class/type, or None.

    Matching, in order: exact normalized designation; then the longest table
    designation that shares a whole-token run with the input (either direction),
    so "TABLE RED WINE" -> "Wine" via "Red Wine" and "STRAIGHT WHISKEY" ->
    "Distilled Spirits" via "Whiskey".
    """
    key = normalize_text(class_type or "")
    if not key:
        return None
    if key in _EXACT:
        return _EXACT[key]
    padded_key = f" {key} "
    for des_norm, category in _BY_LENGTH:
        if f" {des_norm} " in padded_key or padded_key in f" {des_norm} ":
            return category
    return None


def label_designation(text: str) -> tuple[str, str] | None:
    """Find a class/type designation stated in label text, with its Category.

    Scans the table longest-designation-first so the most specific designation
    present wins, and returns ``(designation, category)`` in the table's original
    casing — e.g. a label reading "Malbec" returns ``("Malbec", "Wine")``. Used to
    verify the label's stated class against the application's by *superclass*
    (a "Malbec" label is consistent with a "TABLE RED WINE" application — both are
    Wine). Returns None when no designation is found.
    """
    norm = normalize_text(text or "")
    if not norm:
        return None
    padded = f" {norm} "
    for des_norm, category in _BY_LENGTH:
        if f" {des_norm} " in padded:
            return (_ORIG.get(des_norm, des_norm), category)
    return None
