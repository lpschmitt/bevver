#!/usr/bin/env python3
"""
Back-fill the `abv` column of ApplicationsData.csv by OCR-reading each label.

Alcohol content isn't in the public COLA structured data (see the README), so
it's read off the label artwork. This posts each row's front (and back, when
present) to a running verifier server's /verify endpoint and parses the ABV the
OCR found, then writes it into the CSV.

A configurable fraction of rows (default 10%) is deliberately LEFT UNTOUCHED, so
the dataset keeps some blank-ABV entries (these exercise the "Assumed" verdict
when the form ABV is empty but the label states it).

Usage:
    python scripts/fill_abv_from_labels.py [--server URL] [--leave-fraction 0.10]

Requires a verifier server already running (any OCR backend; Gemini reads ABV
most reliably). Run it first, e.g. OCR_BACKEND=gemini uvicorn app.main:app.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DATA = ROOT / "test_images" / "ApplicationsData.csv"
IMAGES = APP_DATA.parent


def _clean_num(x) -> str:
    f = float(x)
    return str(int(f)) if f == int(f) else str(f)


def parse_abv(found: str) -> str | None:
    """Pull an ABV value out of the verifier's 'found on label' string."""
    if not found:
        return None
    m = re.search(r"([\d.]+)\s*%", found)
    if m:
        return _clean_num(m.group(1))
    m = re.search(r"([\d.]+)\s*proof", found, re.IGNORECASE)   # proof = 2x ABV
    if m:
        return _clean_num(float(m.group(1)) / 2.0)
    return None


def read_abv(server: str, front: str, back: str) -> str | None:
    cmd = ["curl", "-s", "-m", "90", "-X", "POST",
           "-F", f"file=@{IMAGES / front}"]
    if back:
        cmd += ["-F", f"back_file=@{IMAGES / back}"]
    cmd.append(f"{server}/verify")
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    found = next((f["found"] for f in data.get("fields", [])
                  if f["field"].startswith("Alcohol")), "")
    return parse_abv(found)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--leave-fraction", type=float, default=0.10)
    args = ap.parse_args()

    with APP_DATA.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames
        rows = list(reader)
    if "abv" not in (fields or []):
        print("ApplicationsData.csv has no 'abv' column", file=sys.stderr)
        return 1

    n = len(rows)
    k = max(1, round(n * args.leave_fraction))
    # Evenly spaced rows left untouched.
    leave_idx = {round(i * n / k) for i in range(k)}

    left, filled, missed = [], [], []
    for i, row in enumerate(rows):
        label = row.get("ttb_id") or row["front"]
        if i in leave_idx:
            left.append(label)
            print(f"  [{i:>2}] {label}: LEFT AS-IS (abv={row['abv'] or 'blank'!r})", flush=True)
            continue
        val = read_abv(args.server, row["front"], row.get("back", ""))
        if val is not None:
            row["abv"] = val
            filled.append((label, val))
        else:
            missed.append(label)
        print(f"  [{i:>2}] {label}: {row['abv'] or '(no ABV read)'}", flush=True)

    with APP_DATA.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print("\n" + "=" * 60)
    print(f"filled: {len(filled)}  ·  left untouched (10%): {len(left)} {left}")
    if missed:
        print(f"no ABV readable (left blank): {missed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
