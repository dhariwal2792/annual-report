#!/usr/bin/env python3
"""
Download annual accounts PDFs for top 500 UK companies from Companies House.

Features:
  - Progress saved to index.json after every company
  - Stops automatically on HTTP 429 rate limit
  - Run again to resume exactly where it stopped

Setup:
    pip install requests
    export CH_API_KEY=your_key
    python download_accounts.py
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests

API_KEY = os.environ.get("CH_API_KEY", "")
if not API_KEY:
    print("Error: CH_API_KEY not set.")
    print("  Get a free key at: https://developer.company-information.service.gov.uk/")
    sys.exit(1)

BASE    = "https://api.company-information.service.gov.uk"
DOC_BASE = "https://document-api.company-information.service.gov.uk"
OUT      = Path("annual_accounts")
INDEX    = Path("index.json")
TARGET           = 700   # pool size — larger than needed to ensure 500 with accounts
MAX_DOWNLOADS    = 500   # stop once this many PDFs are successfully downloaded
DELAY    = 0.3  # seconds between requests

SEED_COMPANIES = [
    ("00102498", "BP PLC"),
    ("00617987", "HSBC Holdings PLC"),
    ("00041424", "Unilever PLC"),
    ("02723534", "AstraZeneca PLC"),
    ("03888792", "GSK PLC"),
    ("01833679", "Vodafone Group PLC"),
    ("02216611", "BT Group PLC"),
    ("00048839", "Barclays PLC"),
    ("00095072", "Lloyds Banking Group PLC"),
    ("00966425", "Standard Chartered PLC"),
    ("00003196", "Rio Tinto PLC"),
    ("03564138", "Anglo American PLC"),
    ("07989820", "Glencore PLC"),
    ("01003142", "Rolls-Royce Holdings PLC"),
    ("01470151", "BAE Systems PLC"),
    ("04083867", "Compass Group PLC"),
    ("00111714", "WPP PLC"),
    ("02294399", "Diageo PLC"),
    ("03407643", "British American Tobacco PLC"),
    ("03236483", "Imperial Brands PLC"),
    ("00445790", "Tesco PLC"),
    ("00185141", "J Sainsbury PLC"),
    ("00214436", "Marks and Spencer Group PLC"),
    ("01664007", "Kingfisher PLC"),
    ("03458224", "Burberry Group PLC"),
    ("03033654", "Centrica PLC"),
    ("04584664", "National Grid PLC"),
    ("00085920", "Next PLC"),
    ("00293262", "Associated British Foods PLC"),
    ("01714415", "Prudential PLC"),
    ("02557590", "Legal & General Group PLC"),
    ("02800986", "Aviva PLC"),
    ("03814692", "RELX PLC"),
    ("01818840", "Pearson PLC"),
    ("04191786", "Persimmon PLC"),
    ("00316887", "British Land Company PLC"),
    ("00045919", "Land Securities Group PLC"),
    ("01126978", "Segro PLC"),
    ("02366616", "United Utilities Group PLC"),
    ("03162837", "Severn Trent PLC"),
    ("04267576", "Intertek Group PLC"),
    ("03676088", "Rentokil Initial PLC"),
    ("05534340", "Ferguson PLC"),
    ("02382899", "Smiths Group PLC"),
    ("01533123", "ITV PLC"),
    ("02733583", "Rightmove PLC"),
    ("00216309", "Barratt Developments PLC"),
    ("00229294", "Taylor Wimpey PLC"),
    ("00084869", "Balfour Beatty PLC"),
    ("00081132", "Serco Group PLC"),
    ("02364873", "JD Sports Fashion PLC"),
    ("00036633", "WH Smith PLC"),
    ("00120816", "Greggs PLC"),
    ("02408895", "Frasers Group PLC"),
    ("01116613", "Admiral Group PLC"),
    ("03103683", "Hargreaves Lansdown PLC"),
    ("00247473", "Schroders PLC"),
    ("00026059", "Man Group PLC"),
    ("00756502", "3i Group PLC"),
    ("00885535", "Informa PLC"),
    ("00229002", "Halma PLC"),
    ("00055921", "Spirax-Sarco Engineering PLC"),
    ("00214825", "IMI PLC"),
    ("00029838", "Senior PLC"),
    ("00226227", "Bodycote PLC"),
    ("01679033", "Diploma PLC"),
    ("04030891", "Hikma Pharmaceuticals PLC"),
    ("00049551", "Reckitt Benckiser Group PLC"),
    ("01996364", "Ocado Group PLC"),
    ("02050955", "Auto Trader Group PLC"),
    ("04190316", "Virgin Money UK PLC"),
    ("02669874", "Close Brothers Group PLC"),
]


class RateLimitError(Exception):
    pass


def make_session() -> requests.Session:
    s = requests.Session()
    s.auth = (API_KEY, "")
    s.headers["User-Agent"] = "UK-Annual-Accounts-Downloader/1.0"
    return s


def api_get(s: requests.Session, url: str, params: dict = None) -> requests.Response:
    resp = s.get(url, params=params or {}, timeout=30)
    if resp.status_code == 429:
        raise RateLimitError()
    if resp.status_code == 401:
        print("\nError: Invalid API key (HTTP 401). Check CH_API_KEY.")
        sys.exit(1)
    return resp


# ---------------------------------------------------------------------------
# Index helpers
# ---------------------------------------------------------------------------

def load_index():
    if INDEX.exists():
        with open(INDEX) as f:
            return json.load(f)
    return []


def save_index(index):
    with open(INDEX, "w") as f:
        json.dump(index, f, indent=2)


COMPANY_LIST = Path("company_list.json")

def build_index(s: requests.Session):
    """
    Build company list in priority order:
      1. company_list.json (built by build_company_list.py — FTSE 100/250 + seed + CH API)
      2. Seed list fallback
      3. CH API PLC discovery top-up
    """
    companies = []
    seen = set()

    # ---- 1. Use company_list.json if available ----
    if COMPANY_LIST.exists():
        with open(COMPANY_LIST) as f:
            raw = json.load(f)
        for c in raw:
            cn = c.get("number", "").strip()
            name = c.get("name", cn)
            if cn and cn not in seen:
                companies.append({"number": cn, "name": name, "status": "pending"})
                seen.add(cn)
        print(f"Loaded {len(companies)} companies from {COMPANY_LIST}")
    else:
        print(f"company_list.json not found — using seed list.")
        print(f"Run: python build_company_list.py  to build a ranked list.\n")
        for cn, name in SEED_COMPANIES:
            companies.append({"number": cn, "name": name, "status": "pending"})
            seen.add(cn)
        print(f"Seed: {len(companies)} companies")

    # ---- 2. CH API discovery to top up to TARGET ----
    if len(companies) < TARGET:
        print(f"Topping up to {TARGET} via CH API discovery...")
        start = 0
        per_page = 100
        while len(companies) < TARGET:
            try:
                resp = api_get(s, f"{BASE}/advanced-search/companies", {
                    "company_status": "active",
                    "company_type": "plc",
                    "items_per_page": per_page,
                    "start_index": start,
                })
            except RateLimitError:
                print("Rate limit hit during discovery – using what we have.")
                break

            if resp.status_code != 200:
                print(f"Advanced search HTTP {resp.status_code} – stopping discovery.")
                break

            data = resp.json()
            items = data.get("items", [])
            if not items:
                break

            for item in items:
                cn = item.get("company_number", "").strip()
                name = item.get("company_name", cn)
                if cn and cn not in seen:
                    companies.append({"number": cn, "name": name, "status": "pending"})
                    seen.add(cn)

            start += per_page
            if start >= data.get("hits", 0):
                break
            time.sleep(DELAY)

        print(f"Total after top-up: {len(companies)}")

    companies = companies[:TARGET]
    save_index(companies)
    print(f"Index built: {len(companies)} companies → {INDEX}\n")
    return companies


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

SKIP_TYPES = {
    "accounts-with-accounts-type-interim",
    "accounts-with-accounts-type-micro-entity",
    "accounts-with-accounts-type-dormant",
    "accounts-with-accounts-type-abbreviated",
    "accounts-with-accounts-type-unaudited-abridged",
    "accounts-with-accounts-type-small",
}

PREFERRED_TYPES = [
    "accounts-with-accounts-type-group",
    "accounts-with-accounts-type-full",
    "accounts-with-accounts-type-small-full",
]

def get_latest_accounts_filing(s: requests.Session, company_number: str) -> Optional[dict]:
    resp = api_get(s, f"{BASE}/company/{company_number}/filing-history",
                   {"category": "accounts", "items_per_page": 20})
    if resp.status_code != 200:
        return None

    items = [
        item for item in resp.json().get("items", [])
        if "document_metadata" in item.get("links", {})
        and item.get("description", "") not in SKIP_TYPES
        and item.get("pages", 0) >= 20
    ]

    # Prefer group > full > small-full, then fall back to whatever is left
    for preferred in PREFERRED_TYPES:
        for item in items:
            if item.get("description") == preferred:
                return item

    return items[0] if items else None


def download_pdf(s: requests.Session, filing: dict,
                 company_name: str, company_number: str) -> Optional[Path]:
    doc_meta_url = filing["links"]["document_metadata"]

    meta_resp = api_get(s, doc_meta_url)
    if meta_resp.status_code != 200:
        return None

    try:
        meta = meta_resp.json()
    except ValueError:
        meta = {}

    content_url = meta.get("links", {}).get("document", "")
    if not content_url:
        doc_id = doc_meta_url.rstrip("/").split("/")[-1]
        content_url = f"{DOC_BASE}/document/{doc_id}/content"

    time.sleep(DELAY)

    pdf_resp = s.get(content_url, headers={"Accept": "application/pdf"},
                     timeout=120, stream=True, allow_redirects=True)
    if pdf_resp.status_code == 429:
        raise RateLimitError()
    if pdf_resp.status_code != 200:
        return None

    date_str = filing.get("date", "unknown")
    ftype    = filing.get("type", "accounts")
    safe     = re.sub(r"[^\w\-]", "_", company_name)[:40]
    filepath = OUT / f"{safe}_{company_number}_{date_str}_{ftype}.pdf"

    with open(filepath, "wb") as fh:
        for chunk in pdf_resp.iter_content(8192):
            fh.write(chunk)

    if filepath.stat().st_size < 2048:
        filepath.unlink(missing_ok=True)
        return None

    return filepath


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    OUT.mkdir(exist_ok=True)
    s = make_session()

    index = load_index()
    if not index:
        index = build_index(s)
    else:
        done    = sum(1 for c in index if c["status"] == "downloaded")
        skipped = sum(1 for c in index if c["status"] == "skipped")
        pending = sum(1 for c in index if c["status"] == "pending")
        print(f"Resuming — downloaded: {done}  skipped: {skipped}  pending: {pending}\n")

    total_downloaded = sum(1 for c in index if c["status"] == "downloaded")

    for i, company in enumerate(index):
        if total_downloaded >= MAX_DOWNLOADS:
            print(f"Reached {MAX_DOWNLOADS} downloads — done.")
            break

        if company["status"] not in ("pending", "error"):
            continue

        cn   = company["number"]
        name = company["name"]
        print(f"[{i+1}/{len(index)}] {name} ({cn})", end=" ... ", flush=True)

        try:
            time.sleep(DELAY)
            filing = get_latest_accounts_filing(s, cn)

            if not filing:
                company["status"] = "skipped"
                save_index(index)
                print("no accounts")
                continue

            time.sleep(DELAY)
            filepath = download_pdf(s, filing, name, cn)

            if filepath:
                total_downloaded += 1
                company["status"] = "downloaded"
                company["file"]   = filepath.name
                size_kb = filepath.stat().st_size / 1024
                print(f"saved {size_kb:,.0f} KB  [{total_downloaded} downloaded]")
            else:
                company["status"] = "skipped"
                print("no PDF")

            save_index(index)

        except RateLimitError:
            print("\n⚠  Rate limit hit. Progress saved.")
            print(f"   Wait 5 minutes, then run again to resume from this point.")
            save_index(index)
            sys.exit(0)

        except Exception as exc:
            company["status"] = "error"
            company["error"]  = str(exc)
            save_index(index)
            print(f"error: {exc}")

    done = sum(1 for c in index if c["status"] == "downloaded")
    print(f"\n{'='*60}")
    print(f"Complete. {done} PDFs in '{OUT}/'")


if __name__ == "__main__":
    main()
