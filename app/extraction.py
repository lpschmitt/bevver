"""
Field extraction from OCR output.

OCR gives us a list of text lines, each with a bounding box and a confidence.
Generic "search the blob" extraction is brittle, so each field has its own
strategy keyed to how that field actually appears on a label:

  brand        -> tallest text block(s) (brands are the biggest print)
  abv          -> regex for "% alc/vol" and "proof"
  net_contents -> regex for a volume + unit
  class_type   -> handled in matching against the full text (may span lines)
  warning      -> handled in app/warning.py via the "GOVERNMENT WARNING" anchor
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class OcrLine:
    text: str
    confidence: float
    # Bounding box height in pixels (used as a proxy for print size).
    height: float
    # Top y-coordinate, for ordering top-to-bottom.
    top: float


@dataclass
class OcrResult:
    lines: list[OcrLine]

    @property
    def full_text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def mean_confidence(self) -> float:
        if not self.lines:
            return 0.0
        return sum(l.confidence for l in self.lines) / len(self.lines)


# --------------------------------------------------------------------------- #
# Brand name: largest text blocks
# --------------------------------------------------------------------------- #

# Lines that are obviously not a brand even if they are large.
_BRAND_STOPWORDS = re.compile(
    r"government warning|alc\.?\s*/?\s*vol|alcohol|proof|ml\b|liter|"
    r"fl\.?\s*oz|net\s+contents|contents|bottled|distilled|produced|imported|brewed",
    re.IGNORECASE,
)


def brand_candidates(ocr: OcrResult, top_n: int = 5) -> list[str]:
    """Return brand-name candidates ranked by print size (bbox height)."""
    scored = [
        l for l in ocr.lines
        if l.text.strip() and not _BRAND_STOPWORDS.search(l.text)
        and any(c.isalpha() for c in l.text)
    ]
    scored.sort(key=lambda l: l.height, reverse=True)
    return [l.text.strip() for l in scored[:top_n]]


def best_brand_for(expected: str, ocr: OcrResult) -> str:
    """
    Pick the brand candidate that best matches the expected value (so the
    matcher can then explain the normalization), falling back to the largest
    text block when nothing is close.

    When an expected value is given we search *all* brand-eligible lines, not
    just the few tallest: on some labels (e.g. keg collars with a large date
    wheel) the biggest print isn't the brand, so restricting to the tallest
    blocks would miss a brand the OCR actually read. The matcher still applies
    its own similarity threshold, so a wrong expected value won't be coerced
    into a false match.
    """
    from rapidfuzz import fuzz

    from app.matching import normalize_text

    candidates = brand_candidates(ocr)
    if not candidates:
        return ""
    if not expected:
        return candidates[0]
    norm_exp = normalize_text(expected)
    pool = brand_candidates(ocr, top_n=len(ocr.lines))
    best, best_score = candidates[0], -1.0
    for cand in pool:
        score = fuzz.ratio(norm_exp, normalize_text(cand))
        if score > best_score:
            best, best_score = cand, score
    return best


# --------------------------------------------------------------------------- #
# ABV / proof
# --------------------------------------------------------------------------- #

# Number-first: "13.5% alc/vol", "13.5% ABV", "13.5% alcohol by volume",
# "13.5% by volume", "13.5 % vol", "ALC. 13.5% BY VOL." — the percentage is
# followed by an alcohol/volume cue (alc, abv, alcohol, [by] vol[ume]).
_ABV_RE = re.compile(
    r"(\d{1,2}(?:\.\d+)?)\s*%\s*"
    r"(?:alc\.?\s*/?\s*vol\.?|abv|alcohol|(?:by\s+)?vol(?:\.|ume)?)",
    re.IGNORECASE,
)
# Keyword-first: the cue precedes the number — "Alcohol 13.5%",
# "ALC. 13.5%", "Alc. by Vol. 5.2%", "Alcohol content: 13.5%".
_ABV_RE_ALT = re.compile(
    r"(?:alc(?:ohol)?|abv)\.?\s*(?:content)?\s*[:\-]?\s*"
    r"(?:by\s*vol(?:ume)?\.?)?[^0-9%\n]{0,12}(\d{1,2}(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
_PROOF_RE = re.compile(r"(\d{2,3}(?:\.\d+)?)\s*proof", re.IGNORECASE)


def extract_abv(text: str) -> float | None:
    for rx in (_ABV_RE, _ABV_RE_ALT):
        m = rx.search(text)
        if m:
            return float(m.group(1))
    return None


def extract_proof(text: str) -> float | None:
    m = _PROOF_RE.search(text)
    return float(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# Net contents
# --------------------------------------------------------------------------- #

_NET_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(ml|milliliters?|millilitres?|cl|l|liters?|litres?|fl\.?\s*oz|fluid\s+ounces?)\b",
    re.IGNORECASE,
)


def extract_net_contents(text: str) -> str:
    """Return the raw '750 mL'-style string found on the label, or ''."""
    m = _NET_RE.search(text)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(0)).strip()


# --------------------------------------------------------------------------- #
# Country of origin
#
# Detection, in priority order:
#   1. An explicit origin cue — "Product of France", "Made in Scotland".
#   2. A US state or country name sitting at the END of a sentence or phrase,
#      e.g. "...Bottled by Otium Cellars, Waterford, Virginia" — that trailing
#      place name is where producers state where the product comes from.
# A place name that is part of the brand name is ignored (a "Texas Whiskey Co."
# brand is not an origin statement). US states are normalised to "USA (State)";
# countries are returned as written, to line up with how the application records
# origin.
# --------------------------------------------------------------------------- #

_US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia",
}

# Postal abbreviations are only trusted when UPPERCASE in the source, so the
# English words "in", "or", "me", "hi", "pa"… don't masquerade as states.
_STATE_ABBR = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware",
    "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
    "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
    "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah",
    "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia",
    "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}

_COUNTRIES = {
    "france", "argentina", "mexico", "canada", "italy", "spain", "portugal",
    "germany", "ireland", "scotland", "england", "wales", "united kingdom",
    "great britain", "japan", "china", "australia", "new zealand",
    "south africa", "chile", "brazil", "netherlands", "belgium", "austria",
    "switzerland", "poland", "russia", "sweden", "norway", "denmark", "finland",
    "greece", "peru", "cuba", "jamaica", "barbados", "puerto rico",
    "dominican republic", "guatemala", "colombia", "venezuela", "india",
    "thailand", "vietnam", "philippines", "korea", "south korea",
    "united states", "america", "usa",
}

_COUNTRY_CANON = {
    "usa": "USA", "united states": "USA", "america": "USA",
    "uk": "United Kingdom", "united kingdom": "United Kingdom",
    "great britain": "United Kingdom",
}

_ORIGIN_CUE_RE = re.compile(
    r"(?:product of|made in|produced in|imported from|country of origin[:\s])\s*"
    r"([A-Za-z][A-Za-z .'\-]{1,30})",
    re.IGNORECASE,
)
# A trailing US ZIP ("...Waterford, VA 20197") shouldn't hide the state that
# precedes it — the state is still effectively the end of the address phrase.
_ZIP_TAIL_RE = re.compile(r"[\s,]+\d{5}(?:-\d{4})?\s*$")
_PHRASE_SPLIT_RE = re.compile(r"[.\n\r;]+")


def _canon_origin(name_lower: str, kind: str) -> str:
    if kind == "state":
        return "USA (" + name_lower.title() + ")"
    return _COUNTRY_CANON.get(name_lower, name_lower.title())


def _origin_in_brand(name_lower: str, brand_norm: str) -> bool:
    """True if the place name is a whole word within the (normalised) brand."""
    if not brand_norm:
        return False
    return re.search(rf"\b{re.escape(name_lower)}\b", brand_norm) is not None


def _trailing_geo(segment: str):
    """If a sentence/phrase ends in a state or country, return (kind, name_lower)."""
    seg = _ZIP_TAIL_RE.sub("", segment).strip().strip(",")
    if not seg:
        return None
    tokens = seg.split()
    for n in (3, 2, 1):                      # longest place name wins ("new york")
        if len(tokens) < n:
            continue
        tail = tokens[-n:]
        phrase = " ".join(t.strip(".,'\"()").lower() for t in tail)
        if phrase in _COUNTRIES:
            return ("country", phrase)
        if phrase in _US_STATES:
            return ("state", phrase)
        if n == 1:
            raw = tail[-1].strip(".,'\"()")
            if raw.isupper() and raw.lower() in _STATE_ABBR:
                return ("state", _STATE_ABBR[raw.lower()])
    return None


def extract_country_of_origin(text: str, brand_name: str = "") -> str:
    """Best-effort country of origin read off the label, normalised.

    US states come back as ``USA (State)``; countries as written. Returns ``""``
    when nothing on the label looks like an origin statement.
    """
    if not text:
        return ""
    from app.matching import normalize_text
    brand_norm = normalize_text(brand_name)

    # 1. Explicit cue ("Product of France") — strongest signal.
    m = _ORIGIN_CUE_RE.search(text)
    if m:
        words = m.group(1).strip(" .").lower().split()
        for n in (3, 2, 1):
            phrase = " ".join(words[:n])
            if phrase in _COUNTRIES and not _origin_in_brand(phrase, brand_norm):
                return _canon_origin(phrase, "country")
            if phrase in _US_STATES and not _origin_in_brand(phrase, brand_norm):
                return _canon_origin(phrase, "state")
        cue = m.group(1).strip(" .")
        if not _origin_in_brand(cue.lower(), brand_norm):
            return cue.title()        # cue is explicit; trust it even off-gazetteer

    # 2. A place name at the end of a sentence/phrase. The origin/bottler line
    #    usually sits low on the label, so the last such match wins.
    found = ""
    for segment in _PHRASE_SPLIT_RE.split(text):
        hit = _trailing_geo(segment)
        if hit and not _origin_in_brand(hit[1], brand_norm):
            found = _canon_origin(hit[1], hit[0])
    return found
