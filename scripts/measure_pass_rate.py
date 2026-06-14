#!/usr/bin/env python3
"""Run the real-OCR pipeline over every downloaded label and report timing +
per-field verdicts. Used to produce the honest pass-rate figures in the README."""
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.pipeline import Application, run_pipeline  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP_DATA = ROOT / "test_images" / "ApplicationsData.csv"

rows = list(csv.DictReader(APP_DATA.open(encoding="utf-8")))
brand_ok = brand_total = 0
times = []

for row in rows:
    fn = row.get("front")
    if not fn:
        continue
    path = APP_DATA.parent / fn
    if not path.exists():
        continue
    label = row.get("ttb_id") or fn
    back_fn = row.get("back") or ""
    back_path = APP_DATA.parent / back_fn if back_fn else None
    back_data = back_path.read_bytes() if back_path and back_path.exists() else None
    app = Application(
        brand_name=row.get("brand_name", ""),
        class_type=row.get("class_type", ""),
        abv=row.get("abv", ""),
        net_contents=row.get("net_contents", ""),
        country_of_origin=row.get("country_of_origin", ""),
    )
    t0 = time.perf_counter()
    try:
        res = run_pipeline(path.read_bytes(), app, filename=path.name,
                           back_data=back_data, back_filename=back_fn)
    except Exception as exc:
        print(f"{label:>16}  ERROR {exc}", flush=True)
        continue
    dt = time.perf_counter() - t0
    times.append(dt)
    verdicts = {f.field: f.verdict for f in res.fields}
    brand_v = verdicts["Brand name"]
    cls_v = verdicts["Class/type"]
    warn_v = res.warning["verdict"]
    brand_total += 1
    if brand_v in ("match", "match_normalized"):
        brand_ok += 1
    print(f"{label:>16}  {dt:5.2f}s  brand={brand_v:<16} "
          f"class={cls_v:<16} warn={warn_v}", flush=True)

n = len(times)
if n:
    print("\n--- summary ---", flush=True)
    print(f"images processed : {n}", flush=True)
    print(f"brand verified   : {brand_ok}/{brand_total} "
          f"({brand_ok/brand_total:.0%})", flush=True)
    print(f"latency  avg/min/max : {sum(times)/n:.2f}s / "
          f"{min(times):.2f}s / {max(times):.2f}s", flush=True)
    print(f"within 5s budget : {sum(1 for t in times if t < 5)}/{n}", flush=True)
