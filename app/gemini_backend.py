"""
Gemini vision backend (optional, cloud).

This is an *opt-in alternative* to the local OCR pipeline, selected with
``OCR_BACKEND=gemini``. It deliberately breaks the "everything runs locally"
constraint documented in app.ocr, so it is off by default and the tradeoff
(image bytes leave the container; latency depends on the network) is the
caller's explicit choice.

Where the local path is OCR-lines -> regex extraction -> matching, a vision
model reads the fields directly and far more robustly across label layouts. So
instead of faking bounding boxes, this backend asks Gemini for two things:

  1. the application fields it can read (brand, class/type, ABV, proof, net
     contents), and
  2. a *verbatim* transcription of all label text, preserving capitalization.

Both feed the SAME downstream code: the structured fields go through the
existing ``app.matching`` rules, and the verbatim transcription goes through
``app.warning.verify_warning`` (whose prefix-case check needs the original
casing). The verdict/normalization logic stays the single source of truth — the
only thing that changes is how the values are read off the image.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("gemini")

# Model + auth. gemini-2.5-flash is fast and cheap and handles label reading
# well; override for quality/cost tradeoffs on the deploy target.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"

# Hard wall-clock cap on the API call (seconds) so a hung request can't blow the
# 5-second budget silently. google-genai takes the timeout in milliseconds.
GEMINI_TIMEOUT_S = float(os.environ.get("GEMINI_TIMEOUT_S", "20"))

# JSON shape we force the model to return. Numeric fields are strings ("" when
# absent) — more robust across model outputs than nullable numbers, and parsed
# to float|None below.
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "brand_name": {"type": "string"},
        "class_type": {"type": "string"},
        "abv_percent": {"type": "string"},
        "proof": {"type": "string"},
        "net_contents": {"type": "string"},
        "full_text": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": [
        "brand_name", "class_type", "abv_percent", "proof",
        "net_contents", "full_text", "confidence",
    ],
}

_PROMPT = (
    "You are reading a US alcohol beverage label (TTB/COLA). Extract the fields "
    "below and return JSON only.\n"
    "- brand_name: the brand/marque, exactly as printed.\n"
    "- class_type: the class or type designation (e.g. 'Kentucky Straight "
    "Bourbon Whiskey', 'Cabernet Sauvignon', 'India Pale Ale'). Empty string if "
    "absent.\n"
    "- abv_percent: the alcohol-by-volume number only, no '%' (e.g. '40', "
    "'13.5'). Empty string if not shown.\n"
    "- proof: the proof number only, if printed. Empty string otherwise.\n"
    "- net_contents: the net contents exactly as printed, including unit "
    "(e.g. '750 mL', '12 FL OZ'). Empty string if absent.\n"
    "- full_text: a VERBATIM transcription of ALL text on the label. Preserve "
    "the original capitalization exactly (this matters for the Government "
    "Warning). Put each distinct line of text on its own line.\n"
    "- confidence: your overall confidence in this reading, 0.0 to 1.0.\n"
    "Do not infer or normalize values; transcribe what is actually printed."
)

_client_singleton = None

# Cache of Gemini readings keyed by image fingerprint, so an identical image
# (byte-for-byte) isn't re-sent to the API — handy for the batch flow and repeated
# single-label checks. It is two-tier: an in-process dict in front of a persistent
# on-disk store (one JSON file per fingerprint), so cached readings survive across
# requests, workers and process restarts. reset_cache() (called at program start)
# clears BOTH tiers, so each run starts empty. Override the location with
# GEMINI_CACHE_DIR.
_RESULT_CACHE: dict[str, "GeminiExtraction"] = {}
_CACHE_DIR = Path(os.environ.get(
    "GEMINI_CACHE_DIR", str(Path(tempfile.gettempdir()) / "ttb_gemini_cache")))


def reset_cache() -> None:
    """Clear the Gemini cache — both the in-memory tier and the on-disk store.
    Called at program start so a run never serves readings from a previous one."""
    _RESULT_CACHE.clear()
    try:
        for f in _CACHE_DIR.glob("*.json*"):
            f.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not clear Gemini cache dir %s: %s", _CACHE_DIR, exc)


def _cache_key(image_bytes: bytes, mime_type: str) -> str:
    """SHA-256 fingerprint of the image, plus the mime type and model, so a
    config change can't return a stale reading for the same bytes."""
    h = hashlib.sha256()
    h.update(image_bytes)
    h.update(b"\x00" + mime_type.encode("utf-8"))
    h.update(b"\x00" + GEMINI_MODEL.encode("utf-8"))
    return h.hexdigest()


def _cache_load(key: str) -> "GeminiExtraction | None":
    """Look up a reading: memory first, then the on-disk store (promoting a disk
    hit into memory). Returns None on a miss or any read/parse problem."""
    hit = _RESULT_CACHE.get(key)
    if hit is not None:
        return hit
    path = _CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        result = GeminiExtraction(**data)
    except (OSError, ValueError, TypeError) as exc:
        log.warning("Ignoring unreadable Gemini cache file %s: %s", path.name, exc)
        return None
    _RESULT_CACHE[key] = result
    return result


def _cache_store(key: str, result: "GeminiExtraction") -> None:
    """Write a reading to both tiers. Disk write is atomic (temp file + replace)
    and best-effort — a cache-write failure never blocks returning the result."""
    _RESULT_CACHE[key] = result
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _CACHE_DIR / f"{key}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(result)), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("Could not write Gemini cache file for %s…: %s", key[:12], exc)


@dataclass
class GeminiExtraction:
    """Structured label reading from Gemini, mirroring what the local OCR +
    extraction path produces, so the pipeline can feed it to app.matching."""
    brand_name: str
    class_type: str
    abv: float | None
    proof: float | None
    net_contents: str
    full_text: str
    confidence: float


def is_selected() -> bool:
    """True when the Gemini backend is the configured reader (the default)."""
    return os.environ.get("OCR_BACKEND", "gemini").lower() == "gemini"


def _get_client():
    global _client_singleton
    if _client_singleton is None:
        from google import genai
        from google.genai import types

        api_key = os.environ.get(GEMINI_API_KEY_ENV)
        if not api_key:
            raise RuntimeError(
                f"OCR_BACKEND=gemini but {GEMINI_API_KEY_ENV} is not set."
            )
        log.info("Initializing Gemini client (model=%s)…", GEMINI_MODEL)
        _client_singleton = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(GEMINI_TIMEOUT_S * 1000)),
        )
    return _client_singleton


def _to_float(value: str) -> float | None:
    try:
        s = str(value).strip()
        return float(s) if s else None
    except (TypeError, ValueError):
        return None


def extract_fields(image_bytes: bytes, mime_type: str = "image/jpeg") -> GeminiExtraction:
    """Send one label image to Gemini and return the structured reading.

    Identical image bytes are served from the cache (keyed by a SHA-256
    fingerprint; in-memory in front of an on-disk store), so the same label isn't
    re-sent to the API.

    Raises RuntimeError on auth/config problems and re-raises transport errors
    so the pipeline can map them to a friendly message.
    """
    key = _cache_key(image_bytes, mime_type)
    cached = _cache_load(key)
    if cached is not None:
        log.info("Gemini cache hit (%s…)", key[:12])
        return cached

    from google.genai import types

    client = _get_client()
    last_exc: Exception | None = None
    for attempt in range(4):
        if attempt:
            delay = 2 ** attempt
            log.warning("Gemini transient error, retry %d/3 in %ds…", attempt, delay)
            time.sleep(delay)
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    _PROMPT,
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_RESPONSE_SCHEMA,
                    temperature=0.0,
                ),
            )
            result = _parse(resp.text)
            break
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
            if status in (429, 503):
                last_exc = exc
                continue
            raise
    else:
        raise last_exc  # type: ignore[misc]
    _cache_store(key, result)
    return result


def _parse(raw_text: str | None) -> GeminiExtraction:
    """Parse Gemini's JSON response into a GeminiExtraction."""
    if not raw_text:
        raise ValueError("Gemini returned an empty response.")
    data = json.loads(raw_text)
    conf = data.get("confidence", 0.0)
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = 0.0
    return GeminiExtraction(
        brand_name=str(data.get("brand_name", "")).strip(),
        class_type=str(data.get("class_type", "")).strip(),
        abv=_to_float(data.get("abv_percent", "")),
        proof=_to_float(data.get("proof", "")),
        net_contents=str(data.get("net_contents", "")).strip(),
        full_text=str(data.get("full_text", "")),
        confidence=max(0.0, min(1.0, conf)),
    )
