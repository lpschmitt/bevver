#!/usr/bin/env python3
"""
Back-fill back labels into an existing ApplicationsData.csv.

The original fetch (scripts/fetch_test_images.py) only saved the FIRST label
attachment per COLA record, so the `back` column was always empty even when the
registry held a back/neck label. This script revisits each record by its
existing `ttb_id`, parses ALL `filetype=l` attachments from the Printable
Version page, and — when a record has more than one — downloads the second as
the back label (`test_images/{TTB_ID}_back.{ext}`) and records it in `back`.

Rows without a `ttb_id` (hand-added own-photo pairs) are left untouched, as are
rows that already have a `back`. Same politeness as the original fetch:
sequential, a delay between records, identifiable User-Agent, bounded retries.

Transport note: this runs the HTTP through the system `curl` rather than
`requests`. On some environments (old LibreSSL + urllib3 v2) the Python TLS
stack is reset by ttbonline.gov mid-handshake, while the system TLS used by curl
connects fine. curl also carries the JSESSIONID via a cookie jar, which the
attachment servlet requires alongside a Referer header.

Usage:
    python scripts/fetch_back_labels.py
"""
from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_test_images import (  # noqa: E402  (reuse the existing HTML parsers)
    ATTACH_URL, DETAIL_URL, PRINT_URL, REQUEST_DELAY_S, TIMEOUT_S, USER_AGENT,
    ext_for, filename_from_href, log, parse_label_attachments,
)

APP_DATA_PATH = Path(__file__).resolve().parent.parent / "test_images" / "ApplicationsData.csv"

MAX_RETRIES = 4
BACKOFF_BASE_S = 2.0


def pick_back_href(hrefs: list[str]) -> str | None:
    """Choose the back-label attachment from a record's label hrefs.

    Filenames are the most reliable signal: prefer one that literally says
    "back". Otherwise take a secondary attachment that does NOT say "front"
    (so a front/back pair with generic names still works, but we never mislabel
    a second *front* image as the back). Returns None when there's no back.
    """
    named = [(h, filename_from_href(h).lower()) for h in hrefs]
    for h, name in named:
        if "back" in name:
            return h
    for h, name in named[1:]:
        if "front" not in name:
            return h
    return None


def attach_url(href: str) -> str:
    """Rebuild the attachment URL with the filename percent-encoded (names contain
    spaces/special chars that curl won't accept raw)."""
    fn = filename_from_href(href)
    return ATTACH_URL.format(href=f"publicViewAttachment.do?filename={quote(fn)}&filetype=l")


def curl_get(url: str, jar: str, referer: str | None = None) -> tuple[int, str, bytes]:
    """One curl GET sharing the cookie jar. Returns (http_code, content_type, body)."""
    out = tempfile.mktemp()
    cmd = ["curl", "-s", "-m", str(TIMEOUT_S), "-c", jar, "-b", jar,
           "-A", USER_AGENT, "-o", out, "-w", "%{http_code}\t%{content_type}"]
    if referer:
        cmd += ["-e", referer]
    cmd.append(url)
    res = subprocess.run(cmd, capture_output=True, text=True)
    meta = (res.stdout or "").strip().split("\t")
    code = int(meta[0]) if meta and meta[0].isdigit() else 0
    ctype = meta[1] if len(meta) > 1 else ""
    body = Path(out).read_bytes() if Path(out).exists() else b""
    Path(out).unlink(missing_ok=True)
    return code, ctype, body


def curl_get_retries(url: str, jar: str, referer: str | None = None
                     ) -> tuple[int, str, bytes]:
    """curl_get with bounded retries + backoff (the host resets connections under burst)."""
    code = 0
    ctype = ""
    body = b""
    for attempt in range(1, MAX_RETRIES + 1):
        code, ctype, body = curl_get(url, jar, referer)
        if code == 200 and body:
            return code, ctype, body
        log.warning("  HTTP %s for %s (attempt %d)", code, url, attempt)
        if attempt < MAX_RETRIES:
            time.sleep(BACKOFF_BASE_S * attempt)
    return code, ctype, body


def fetch_back(ttbid: str) -> str | None:
    """Return the saved back-label filename for a record, or None if it has none."""
    jar = tempfile.mktemp()
    try:
        # Detail page mints the JSESSIONID cookie into the jar.
        code, _, _ = curl_get_retries(DETAIL_URL.format(ttbid=ttbid), jar)
        if code != 200:
            log.error("[%s] detail page failed", ttbid)
            return None

        print_url = PRINT_URL.format(ttbid=ttbid)
        code, _, body = curl_get_retries(print_url, jar,
                                         referer=DETAIL_URL.format(ttbid=ttbid))
        if code != 200:
            log.error("[%s] printable page failed", ttbid)
            return None

        hrefs = parse_label_attachments(body.decode("utf-8", "replace"))
        href = pick_back_href(hrefs)
        if href is None:
            log.info("[%s] %d label attachment(s); no back label", ttbid, len(hrefs))
            return None

        code, ctype, content = curl_get_retries(attach_url(href), jar, referer=print_url)
        if code != 200 or not content:
            log.error("[%s] back attachment download failed", ttbid)
            return None
        if "text/html" in ctype.lower():
            log.error("[%s] back attachment returned HTML (session/referer rejected)", ttbid)
            return None

        ext = ext_for(filename_from_href(href), ctype)
        out_path = APP_DATA_PATH.parent / f"{ttbid}_back.{ext}"
        out_path.write_bytes(content)
        log.info("[%s] saved back label %s (%d bytes)", ttbid, out_path.name, len(content))
        return out_path.name
    finally:
        Path(jar).unlink(missing_ok=True)


def main() -> int:
    if not APP_DATA_PATH.exists():
        log.error("No ApplicationsData.csv at %s; run fetch_test_images.py first.", APP_DATA_PATH)
        return 1

    with APP_DATA_PATH.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        rows = list(reader)
    if "back" not in fieldnames:
        log.error("ApplicationsData.csv has no 'back' column; expected the front/back schema.")
        return 1

    # Only revisit rows that have a ttb_id, a front, and no back yet.
    todo = [r for r in rows if r.get("ttb_id") and r.get("front") and not r.get("back")]
    log.info("%d row(s) to check for a back label.", len(todo))

    added = 0
    for i, row in enumerate(todo):
        ttbid = row["ttb_id"]
        log.info("(%d/%d) %s", i + 1, len(todo), ttbid)
        try:
            back = fetch_back(ttbid)
        except Exception as exc:  # never let one record kill the batch
            log.error("[%s] unexpected error: %s", ttbid, exc)
            back = None
        if back:
            row["back"] = back
            added += 1
        if i < len(todo) - 1:
            time.sleep(REQUEST_DELAY_S)

    with APP_DATA_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    log.info("=" * 60)
    log.info("Back labels added: %d of %d checked. Applications data -> %s",
             added, len(todo), APP_DATA_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
