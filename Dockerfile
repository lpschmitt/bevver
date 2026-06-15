# Single container: FastAPI app + local OCR (PaddleOCR, CPU). No external ML.
FROM python:3.11-slim

# OpenCV / PaddleOCR runtime libs (headless: libgl + glib).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/tmp/hf \
    OCR_BACKEND=gemini

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code + bundled sample data (so the deployed demo works out of the box).
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY test_images/ ./test_images/
COPY Reference/ ./Reference/

# HuggingFace Spaces routes to port 7860; default uvicorn elsewhere can override.
ENV PORT=7860
EXPOSE 7860

# Pre-download PaddleOCR models at build time so the first request isn't slow
# (and so a restarted container never re-downloads at request time). This MUST
# succeed — a silent failure here ships an image with no model, which only
# surfaces as a "hang" on the first real request. Retry the flaky mirror a few
# times, then run one tiny inference to force det+rec+cls models to cache.
RUN python - <<'PY'
import time
import numpy as np
from paddleocr import PaddleOCR

last = None
for attempt in range(1, 4):
    try:
        ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False, use_gpu=False)
        ocr.ocr(np.full((64, 192, 3), 255, dtype=np.uint8), cls=True)
        print(f"PaddleOCR models cached at build (attempt {attempt}).")
        break
    except Exception as exc:  # noqa: BLE001 - surface and retry
        last = exc
        print(f"Model pre-pull attempt {attempt} failed: {exc}")
        time.sleep(5)
else:
    raise SystemExit(f"Could not pre-download PaddleOCR models: {last}")
PY

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
