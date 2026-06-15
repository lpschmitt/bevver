"""
Image preprocessing + OCR backend.

Design constraints from the stakeholders:
  - Everything runs locally in the container. No cloud OCR (firewalled, latency).
  - 5-second end-to-end budget, so preprocessing stays cheap and OCR runs on CPU.

PaddleOCR (PP-OCRv3 detection + PP-OCRv4 recognition) is the primary backend. It
is heavy and is imported lazily
and cached as a singleton so the model loads once per process, not per request.
Tesseract is a documented fallback if PaddleOCR won't fit the deploy target.

The OCR backend is deliberately decoupled from the pipeline: it returns a plain
`OcrResult` (see app.extraction), so the test-suite can feed canned OCR output
through the same matching code without loading any ML model.
"""
from __future__ import annotations

import io
import logging
import os
import threading

import cv2
import numpy as np

from app.extraction import OcrLine, OcrResult

log = logging.getLogger("ocr")

# Local OCR backup (used when OCR_BACKEND isn't "gemini"): "paddle" or
# "tesseract". Defaults to the Gemini reader; the local path falls back to paddle.
OCR_BACKEND = os.environ.get("OCR_BACKEND", "gemini").lower()

# Cap the longest image side before OCR. Registry label artwork is often
# 1300–2500 px; full-resolution CPU OCR blows the 5-second budget, while ~1280 px
# keeps text legible to the detector. Tunable via env for the deploy target.
MAX_OCR_SIDE = int(os.environ.get("MAX_OCR_SIDE", "1280"))

# Angle classification corrects rotated text but roughly doubles OCR time. On by
# default for accuracy; can be disabled on the deploy target to tighten latency.
USE_ANGLE_CLS = os.environ.get("OCR_USE_ANGLE_CLS", "true").lower() != "false"

_paddle_singleton = None
# A single PaddleOCR predictor is NOT safe for concurrent inference. When the
# pipeline reads front+back in parallel, this lock serializes the paddle call
# (load/preprocess still overlap). Tesseract (subprocess) and the Gemini backend
# (independent HTTP calls) need no such guard.
_paddle_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Image loading (image bytes or PDF first page) -> BGR numpy array
# --------------------------------------------------------------------------- #

def load_image(data: bytes, content_type: str = "", filename: str = "") -> np.ndarray:
    """
    Decode uploaded bytes to a BGR image. PDFs are rendered (first page) to an
    image via PyMuPDF before OCR; everything else is decoded with OpenCV.
    """
    is_pdf = (
        "pdf" in (content_type or "").lower()
        or filename.lower().endswith(".pdf")
        or data[:5] == b"%PDF-"
    )
    if is_pdf:
        return _pdf_first_page_to_image(data)

    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image bytes.")
    return img


def _pdf_first_page_to_image(data: bytes, zoom: float = 2.0) -> np.ndarray:
    import fitz  # PyMuPDF

    doc = fitz.open(stream=data, filetype="pdf")
    if doc.page_count == 0:
        raise ValueError("PDF has no pages.")
    page = doc.load_page(0)
    matrix = fitz.Matrix(zoom, zoom)            # render at 2x for OCR legibility
    pix = page.get_pixmap(matrix=matrix)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
    if pix.n == 4:      # RGBA -> BGR
        img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    elif pix.n == 3:    # RGB -> BGR
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    else:               # grayscale -> BGR
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return img


# --------------------------------------------------------------------------- #
# Preprocessing: grayscale, contrast normalize, deskew
# --------------------------------------------------------------------------- #

def preprocess(img: np.ndarray) -> np.ndarray:
    """
    Grayscale -> CLAHE contrast normalization -> deskew. Returns a 3-channel BGR
    image (PaddleOCR expects colour input) so the detector sees normalized
    contrast while keeping its own colour pipeline happy.
    """
    img = _downscale(img)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Contrast Limited Adaptive Histogram Equalization handles uneven lighting,
    # which is exactly the "photo taken by an agent at a desk" failure mode.
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)

    gray = _deskew(gray)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def _downscale(img: np.ndarray, max_side: int | None = None) -> np.ndarray:
    """Shrink so the longest side is <= max_side; keeps OCR within the latency
    budget without hurting legibility. Never upscales."""
    cap = max_side or MAX_OCR_SIDE
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= cap:
        return img
    scale = cap / float(longest)
    return cv2.resize(img, (int(w * scale), int(h * scale)),
                      interpolation=cv2.INTER_AREA)


def _deskew(gray: np.ndarray, max_angle: float = 15.0) -> np.ndarray:
    """
    Estimate small text skew and rotate it flat. Only correct modest angles; a
    label photographed sideways is a user error we surface, not silently rotate.
    """
    inverted = cv2.bitwise_not(gray)
    _, thresh = cv2.threshold(inverted, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    coords = np.column_stack(np.where(thresh > 0))
    if coords.shape[0] < 50:
        return gray
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = 90 + angle
    if abs(angle) > max_angle:
        return gray
    (h, w) = gray.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, matrix, (w, h),
                          flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


# --------------------------------------------------------------------------- #
# OCR backends
# --------------------------------------------------------------------------- #

def _get_paddle():
    global _paddle_singleton
    if _paddle_singleton is None:
        # Double-checked under the lock so concurrent first requests build once.
        with _paddle_lock:
            if _paddle_singleton is None:
                from paddleocr import PaddleOCR
                log.info("Loading PaddleOCR (CPU; PP-OCRv3 detection + PP-OCRv4 recognition)…")
                _paddle_singleton = PaddleOCR(
                    use_angle_cls=USE_ANGLE_CLS, lang="en", show_log=False, use_gpu=False,
                )
    return _paddle_singleton


def _run_paddle(img: np.ndarray) -> OcrResult:
    ocr = _get_paddle()
    # Serialize inference: one predictor instance can't be called concurrently.
    with _paddle_lock:
        raw = ocr.ocr(img, cls=USE_ANGLE_CLS)
    lines: list[OcrLine] = []
    # PaddleOCR returns [[ [box, (text, conf)], ... ]] (one entry per image).
    page = raw[0] if raw and raw[0] is not None else []
    for entry in page:
        box, (text, conf) = entry[0], entry[1]
        ys = [pt[1] for pt in box]
        height = max(ys) - min(ys)
        lines.append(OcrLine(text=text, confidence=float(conf),
                             height=float(height), top=float(min(ys))))
    return OcrResult(lines=lines)


def _run_tesseract(img: np.ndarray) -> OcrResult:
    import pytesseract
    from pytesseract import Output

    data = pytesseract.image_to_data(img, output_type=Output.DICT)
    lines: list[OcrLine] = []
    n = len(data["text"])
    for i in range(n):
        text = data["text"][i].strip()
        conf = float(data["conf"][i])
        if not text or conf < 0:
            continue
        lines.append(OcrLine(
            text=text, confidence=conf / 100.0,
            height=float(data["height"][i]), top=float(data["top"][i]),
        ))
    return OcrResult(lines=lines)


def run_ocr(img: np.ndarray) -> OcrResult:
    """Run the configured OCR backend on a preprocessed image."""
    if OCR_BACKEND == "tesseract":
        return _run_tesseract(img)
    return _run_paddle(img)
