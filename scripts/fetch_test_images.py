#!/usr/bin/env python3
"""
Phase 0 - Test data acquisition.

Downloads 29 real alcohol labels + structured metadata from TTB's Public COLA
Registry (public data, no auth). Produces:

    test_images/{TTB_ID}.{ext}        one label artwork per record
    test_images/ApplicationsData.csv      parsed metadata, one row per record

The COLA detail page is a server-rendered Struts page. The label artwork is NOT
linked directly from the detail page; you have to:

    1. GET the detail page to mint a JSESSIONID cookie.
    2. GET the "Printable Version" page (action=publicFormDisplay) which embeds
       <img src="publicViewAttachment.do?filename=...&filetype=l"> for the label.
    3. GET each attachment URL with the same session cookie + a Referer header
       (the attachment servlet returns an HTML error page otherwise).

This is a one-time fetch of 29 public records, so we are deliberately polite:
sequential requests, a delay between requests, an identifiable User-Agent, and
bounded retries with backoff. If the site blocks us entirely we fall back to
writing a manual-download checklist and an empty applications-data template, so that
the rest of the build never depends on this script succeeding.

Usage:
    python scripts/fetch_test_images.py
"""
from __future__ import annotations

import csv
import html
import logging
import mimetypes
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote

import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

TTB_IDS = [
    "15141001000396", "16061001000513", "16130001000646", "16214001000087",
    "16257001000626", "18116001000075", "18017001000321", "18284001000715",
    "17191001000366", "05250001000138", "08212001000265", "10200001000173",
    "10210001000026", "10210001000027", "12004001000218", "12004001000217",
    "12004001000216", "11262001000162", "11215001000375", "17152001000486",
    "18061001000436", "18061001000443", "22033001000108", "24002001000182",
    "24233001000740", "24184001000420", "24184001000028", "24134001000765",
    "24119001000041",
]

BASE = "https://ttbonline.gov/colasonline"
DETAIL_URL = BASE + "/viewColaDetails.do?action=publicDisplaySearchAdvanced&ttbid={ttbid}"
PRINT_URL = BASE + "/viewColaDetails.do?action=publicFormDisplay&ttbid={ttbid}"
ATTACH_URL = BASE + "/{href}"

USER_AGENT = (
    "TTB-COLA-label-verifier/1.0 (take-home prototype; contact: applicant) "
    "python-requests"
)
REQUEST_DELAY_S = 2.0          # politeness delay between records
MAX_RETRIES = 3
BACKOFF_BASE_S = 2.0
TIMEOUT_S = 30

OUT_DIR = Path("test_images")

# Structured fields rendered on the detail page as "<strong>Label: </strong> value".
DETAIL_FIELDS = {
    "brand_name": "Brand Name",
    "fanciful_name": "Fanciful Name",
    "class_type": "Class/Type Code",
    "origin": "Origin Code",
    "net_contents": "Total Bottle Capacity",
    "approval_date": "Approval Date",
    "status": "Status",
    "vendor_code": "Vendor Code",
}

CSV_COLUMNS = [
    "ttb_id", "front", "back", "brand_name", "fanciful_name", "class_type",
    "origin", "net_contents", "approval_date", "abv", "warning_expected",
    "country_of_origin",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fetch")


# --------------------------------------------------------------------------- #
# HTML parsing helpers
# --------------------------------------------------------------------------- #

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(fragment: str) -> str:
    """Strip tags + entities + collapse whitespace from an HTML fragment."""
    text = _TAG_RE.sub(" ", fragment)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_detail_fields(page: str) -> dict[str, str]:
    """
    Parse the labelled metadata cells from a COLA detail page.

    Each field looks like:
        <strong>Brand Name: </strong><a ...>help</a> &nbsp; CALVERT BREWING COMPANY </td>

    We grab everything between the <strong>Label:</strong> and the closing </td>,
    drop the embedded help-link markup, and keep the trailing text as the value.
    """
    out: dict[str, str] = {}
    for key, label in DETAIL_FIELDS.items():
        # Match the strong label, then capture up to the end of the table cell.
        pattern = re.compile(
            r"<strong>\s*" + re.escape(label) + r"\s*:\s*</strong>(.*?)</td>",
            re.IGNORECASE | re.DOTALL,
        )
        m = pattern.search(page)
        out[key] = _clean(m.group(1)) if m else ""
    return out


def parse_label_attachments(print_page: str) -> list[str]:
    """
    Return the attachment hrefs (relative to BASE) for label artwork on the
    printable page.

    The artwork is embedded as e.g.:
        <img src="/colasonline/publicViewAttachment.do?filename=Collar_Walrus.jpg&filetype=l">

    filetype=l == label artwork. We prefer those, but fall back to any
    publicViewAttachment image if a record uses a different filetype. There may
    be several (front/back/neck); we keep document order and use the first as the
    canonical label image. The leading "/colasonline/" path is stripped so the
    caller can join the href onto BASE.
    """
    def _collect(pattern: str) -> list[str]:
        hrefs = re.findall(pattern, print_page, re.IGNORECASE)
        seen: set[str] = set()
        ordered: list[str] = []
        for h in hrefs:
            clean = html.unescape(h)
            if clean not in seen:
                seen.add(clean)
                ordered.append(clean)
        return ordered

    # Capture just the "publicViewAttachment.do?..." part, dropping any path prefix.
    label = _collect(r'src="(?:[^"]*/)?(publicViewAttachment\.do\?[^"]*filetype=l[^"]*)"')
    if label:
        return label
    return _collect(r'src="(?:[^"]*/)?(publicViewAttachment\.do\?[^"]*)"')


def filename_from_href(href: str) -> str:
    m = re.search(r"filename=([^&]+)", href)
    return unquote(m.group(1)) if m else ""


def ext_for(filename: str, content_type: str) -> str:
    """Best-effort file extension from the attachment filename or content-type."""
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix in {"jpg", "jpeg", "png", "gif", "tif", "tiff", "bmp", "pdf"}:
        return "jpg" if suffix == "jpeg" else suffix
    guessed = mimetypes.guess_extension(content_type.split(";")[0].strip() or "")
    if guessed:
        return guessed.lstrip(".")
    if "pdf" in content_type:
        return "pdf"
    return "jpg"


# --------------------------------------------------------------------------- #
# Network helpers
# --------------------------------------------------------------------------- #

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    # TTB's chain sometimes presents an incomplete cert bundle to non-browser
    # clients. Public data, read-only GETs; verify can be disabled if the
    # environment lacks the intermediate. Try verified first, caller may retry.
    return s


def get_with_retries(
    session: requests.Session, url: str, referer: str | None = None
) -> requests.Response | None:
    headers = {"Referer": referer} if referer else {}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, headers=headers, timeout=TIMEOUT_S)
            if resp.status_code == 200:
                return resp
            log.warning("  HTTP %s for %s (attempt %d)", resp.status_code, url, attempt)
        except requests.RequestException as exc:
            log.warning("  request error %s (attempt %d)", exc, attempt)
        if attempt < MAX_RETRIES:
            time.sleep(BACKOFF_BASE_S * attempt)
    return None


# --------------------------------------------------------------------------- #
# Fallback
# --------------------------------------------------------------------------- #

def write_manual_checklist(reason: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    checklist = OUT_DIR / "manual_download_checklist.md"
    lines = [
        "# Manual download checklist",
        "",
        f"Automated fetch could not proceed: {reason}",
        "",
        "Open each detail page, click **Printable Version**, and save the label "
        "image to `test_images/{TTB_ID}.jpg` (or `.pdf`). Then fill in "
        "`ApplicationsData.csv`.",
        "",
        "| TTB ID | Detail page |",
        "|---|---|",
    ]
    for ttbid in TTB_IDS:
        lines.append(f"| {ttbid} | {DETAIL_URL.format(ttbid=ttbid)} |")
    checklist.write_text("\n".join(lines) + "\n", encoding="utf-8")

    app_data = OUT_DIR / "ApplicationsData.csv"
    if not app_data.exists():
        with app_data.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for ttbid in TTB_IDS:
                writer.writerow({"ttb_id": ttbid, "warning_expected": "TRUE"})
    log.info("Wrote fallback checklist -> %s", checklist)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def fetch_one(session: requests.Session, ttbid: str) -> dict | None:
    """Fetch + parse one record. Returns a applications-data row dict, or None."""
    detail = get_with_retries(session, DETAIL_URL.format(ttbid=ttbid))
    if detail is None:
        log.error("[%s] detail page failed", ttbid)
        return None

    fields = parse_detail_fields(detail.text)
    if not fields.get("brand_name"):
        log.warning("[%s] no brand name parsed (unexpected page layout)", ttbid)

    row = {
        "ttb_id": ttbid,
        "front": "",
        "back": "",                # front only here; backs are back-filled later
                                   # by scripts/fetch_back_labels.py (by ttb_id)
        "abv": "",                 # not in public structured data; hand-filled
        "warning_expected": "TRUE",
        **fields,
    }
    row.pop("status", None)        # parsed for sanity but not a CSV column
    row.pop("vendor_code", None)

    # Printable page -> label attachment URLs.
    print_url = PRINT_URL.format(ttbid=ttbid)
    printable = get_with_retries(session, print_url, referer=DETAIL_URL.format(ttbid=ttbid))
    if printable is None:
        log.error("[%s] printable page failed; metadata kept, no image", ttbid)
        return row

    hrefs = parse_label_attachments(printable.text)
    if not hrefs:
        log.warning("[%s] no label attachment found on printable page", ttbid)
        return row

    href = hrefs[0]
    attach = get_with_retries(session, ATTACH_URL.format(href=href), referer=print_url)
    if attach is None or not attach.content:
        log.error("[%s] attachment download failed", ttbid)
        return row

    ctype = attach.headers.get("Content-Type", "")
    if "text/html" in ctype.lower():
        log.error("[%s] attachment returned HTML (session/referer rejected)", ttbid)
        return row

    ext = ext_for(filename_from_href(href), ctype)
    out_path = OUT_DIR / f"{ttbid}.{ext}"
    out_path.write_bytes(attach.content)
    row["front"] = out_path.name
    log.info("[%s] saved %s (%d bytes), brand=%r",
             ttbid, out_path.name, len(attach.content), row.get("brand_name", ""))
    return row


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    session = make_session()

    # Probe once; if TLS verification fails on this host, fall back to unverified
    # (public, read-only data). If even that fails, write the manual checklist.
    try:
        session.get(BASE + "/", timeout=TIMEOUT_S)
    except requests.exceptions.SSLError:
        log.warning("TLS verification failed; retrying without verification "
                    "(public read-only data).")
        session.verify = False
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
        try:
            session.get(BASE + "/", timeout=TIMEOUT_S)
        except requests.RequestException as exc:
            write_manual_checklist(f"site unreachable ({exc})")
            return 0
    except requests.RequestException as exc:
        write_manual_checklist(f"site unreachable ({exc})")
        return 0

    rows: list[dict] = []
    downloaded = failed = 0
    for i, ttbid in enumerate(TTB_IDS):
        log.info("(%d/%d) %s", i + 1, len(TTB_IDS), ttbid)
        row = fetch_one(session, ttbid)
        if row is None:
            failed += 1
            rows.append({"ttb_id": ttbid, "warning_expected": "TRUE"})
        else:
            rows.append(row)
            if row.get("front"):
                downloaded += 1
            else:
                failed += 1
        if i < len(TTB_IDS) - 1:
            time.sleep(REQUEST_DELAY_S)

    app_data_path = OUT_DIR / "ApplicationsData.csv"
    with app_data_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})

    log.info("=" * 60)
    log.info("Summary: %d downloaded, %d failed, %d metadata rows written",
             downloaded, failed, len(rows))
    log.info("Applications data -> %s", app_data_path)
    log.info("NOTE: fill in the blank 'abv' column by reading each label image.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
