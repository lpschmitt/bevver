"""Shared pytest fixtures and helpers."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from app.extraction import OcrLine, OcrResult
from app.warning import STATUTORY_WARNING

ROOT = Path(__file__).resolve().parent.parent
APPLICATIONS_DATA = ROOT / "test_images" / "ApplicationsData.csv"


def make_ocr(lines: list[tuple[str, float]] | list[str]) -> OcrResult:
    """
    Build an OcrResult from text lines for deterministic tests (no ML model).

    Each item is either a bare string or a (text, height) tuple. Height is the
    bbox-height proxy used by brand extraction; default 20, with a tall first
    line so brand-by-size logic has something to pick.
    """
    out: list[OcrLine] = []
    for i, item in enumerate(lines):
        if isinstance(item, tuple):
            text, height = item
        else:
            text, height = item, 20.0
        out.append(OcrLine(text=text, confidence=0.95, height=height, top=float(i * 30)))
    return OcrResult(lines=out)


def synthetic_label_text(brand: str, class_type: str, abv: str | None,
                         net: str | None, include_warning: bool = True,
                         warning_text: str | None = None,
                         include_sulfites: bool = False) -> list[tuple[str, float]]:
    """A plausible label's OCR lines, brand rendered tallest."""
    lines: list[tuple[str, float]] = [(brand, 60.0)]
    if class_type:
        lines.append((class_type, 24.0))
    if abv:
        lines.append((f"{abv}% ALC/VOL", 18.0))
    if net:
        lines.append((net, 18.0))
    if include_sulfites:
        lines.append(("CONTAINS SULFITES", 12.0))
    if include_warning:
        lines.append((warning_text or STATUTORY_WARNING, 12.0))
    return lines


@pytest.fixture(scope="session")
def applications_data_rows() -> list[dict]:
    if not APPLICATIONS_DATA.exists():
        return []
    with APPLICATIONS_DATA.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def image_path_for(row: dict) -> Path | None:
    """Path to the row's front label image, or None if absent/missing."""
    fn = row.get("front")
    if not fn:
        return None
    p = APPLICATIONS_DATA.parent / fn
    return p if p.exists() else None


def back_image_path_for(row: dict) -> Path | None:
    """Path to the row's back label image (optional), or None."""
    fn = row.get("back")
    if not fn:
        return None
    p = APPLICATIONS_DATA.parent / fn
    return p if p.exists() else None
