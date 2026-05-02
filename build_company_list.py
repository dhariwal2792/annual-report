#!/usr/bin/env python3
"""
Build company_list.json — 500 top UK companies ranked by market cap.

Sources:
  1. FTSE 100    — Wikipedia  (100 companies, largest by market cap)
  2. FTSE 250    — Wikipedia  (250 companies, next largest)
  3. FTSE SmallCap — Wikipedia → yfinance market cap sort → top 150

Each company name/ticker is resolved to a Companies House registration
number via the CH search API.

Run once:
    python build_company_list.py

Output: company_list.json
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests
import yfinance as yf
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("CH_API_KEY", "")
if not API_KEY:
    print("Error: CH_API_KEY not set.")
    print("  Add CH_API_KEY=your_key to a .env file in this directory.")
    sys.exit(1)

BASE   = "https://api.company-information.service.gov.uk"
OUTPUT = Path("company_list.json")
DELAY  = 0.4   # between CH API calls

WIKIPEDIA_INDICES = [
    ("FTSE 100",      "https://en.wikipedia.org/wiki/FTSE_100_Index",      100),
    ("FTSE 250",      "https://en.wikipedia.org/wiki/FTSE_250_Index",      250),
    ("FTSE SmallCap", "https://en.wikipedia.org/wiki/FTSE_SmallCap_Index", None),  # take top 150 by mcap
]

SMALLCAP_TAKE = 150   # how many SmallCap companies to add after sorting by market cap
POOL_SIZE     = 700   # build a larger pool so we always find 500 with accounts


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.auth = (API_KEY, "")
    s.headers["User-Agent"] = "UK-Company-List-Builder/1.0"
    return s


# ---------------------------------------------------------------------------
# Wikipedia scraping  →  [(company_name, epic_ticker), ...]
# ---------------------------------------------------------------------------
def scrape_ftse_wiki(url: str, label: str) -> list:
    print(f"  Fetching {label} from Wikipedia...")
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as exc:
        print(f"  ERROR fetching {url}: {exc}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for table in soup.find_all("table", class_="wikitable"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]

        # Need at least a company column
        if not any("company" in h for h in headers):
            continue

        company_col = next((i for i, h in enumerate(headers) if "company" in h), 0)
        # EPIC / ticker column
        epic_col = next(
            (i for i, h in enumerate(headers) if "epic" in h or "ticker" in h or "symbol" in h),
            None,
        )

        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= company_col:
                continue

            name = re.sub(r"\[.*?\]", "", cells[company_col].get_text(strip=True)).strip()
            if not name:
                continue

            ticker = ""
            if epic_col is not None and len(cells) > epic_col:
                ticker = re.sub(r"\[.*?\]", "", cells[epic_col].get_text(strip=True)).strip()

            results.append((name, ticker))

    print(f"  Got {len(results)} companies from {label}")
    return results


# ---------------------------------------------------------------------------
# yfinance market cap fetch
# ---------------------------------------------------------------------------
def get_market_cap(ticker: str) -> int:
    """Return market cap in GBP (or 0 on failure). Retries on throttle."""
    yf_ticker = ticker.upper() + ".L"
    for attempt in range(3):
        try:
            info = yf.Ticker(yf_ticker).info
            return info.get("marketCap") or 0
        except Exception:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))  # 5s, 10s backoff
    return 0


# ---------------------------------------------------------------------------
# Companies House name → (number, official_name)
# ---------------------------------------------------------------------------
def ch_search(s: requests.Session, name: str) -> tuple:
    try:
        resp = s.get(
            f"{BASE}/search/companies",
            params={"q": name, "items_per_page": 10},
            timeout=30,
        )
    except requests.RequestException:
        return None, None

    if resp.status_code == 429:
        print("  Rate limit — waiting 60s...")
        time.sleep(60)
        return None, None

    if resp.status_code != 200:
        return None, None

    items = resp.json().get("items", [])

    # Prefer active PLC
    for item in items:
        if item.get("company_status") == "active" and item.get("company_type") == "plc":
            return item["company_number"], item["title"]

    # Fall back to any active company
    for item in items:
        if item.get("company_status") == "active":
            return item["company_number"], item["title"]

    return None, None


# ---------------------------------------------------------------------------
# CH API PLC discovery top-up
# ---------------------------------------------------------------------------
def discover_plcs(s: requests.Session, seen: set, target: int) -> list:
    found = []
    start = 0
    per_page = 100
    need = target - len(seen)
    print(f"\n[CH API top-up] Need {need} more companies...")

    while len(found) < need:
        try:
            resp = s.get(
                f"{BASE}/advanced-search/companies",
                params={
                    "company_status": "active",
                    "company_type": "plc",
                    "items_per_page": per_page,
                    "start_index": start,
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            print(f"  Network error: {exc}")
            break

        if resp.status_code == 429:
            print("  Rate limit — waiting 60s...")
            time.sleep(60)
            continue

        if resp.status_code != 200:
            print(f"  HTTP {resp.status_code} — stopping discovery")
            break

        data = resp.json()
        items = data.get("items", [])
        if not items:
            break

        for item in items:
            cn   = item.get("company_number", "").strip()
            name = item.get("company_name", cn)
            if cn and cn not in seen:
                found.append({"number": cn, "name": name, "source": "ch-api"})
                seen.add(cn)

        start += per_page
        if start >= data.get("hits", 0):
            break
        time.sleep(DELAY)

    print(f"  Added {len(found)} via CH API")
    return found


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    s = make_session()
    companies = []   # final list of dicts
    seen_numbers = set()
    seen_names   = set()

    # Load existing company_list.json so we don't duplicate already-found companies
    if OUTPUT.exists():
        existing = json.load(open(OUTPUT))
        for c in existing:
            cn = c.get("number", "")
            if cn:
                companies.append(c)
                seen_numbers.add(cn)
                seen_names.add(c.get("name", "").lower())
        print(f"Loaded {len(companies)} existing companies from {OUTPUT}")

    # ================================================================
    # PASS 1 — FTSE 100 and FTSE 250 (all companies, no market cap needed)
    # ================================================================
    for label, url, _ in WIKIPEDIA_INDICES[:2]:
        print(f"\n[{label}]")
        wiki_entries = scrape_ftse_wiki(url, label)

        resolved = 0
        for wiki_name, ticker in wiki_entries:
            if wiki_name.lower() in seen_names:
                continue
            time.sleep(DELAY)
            cn, official_name = ch_search(s, wiki_name)
            if cn and cn not in seen_numbers:
                companies.append({
                    "number": cn,
                    "name":   official_name,
                    "source": label,
                    "ticker": ticker,
                })
                seen_numbers.add(cn)
                seen_names.add(wiki_name.lower())
                resolved += 1
                print(f"  {official_name} ({cn})  [{ticker}]")
            else:
                seen_names.add(wiki_name.lower())
                print(f"  [skipped/not found] {wiki_name}")

        print(f"  Resolved {resolved}/{len(wiki_entries)}")
        time.sleep(2)  # polite delay between Wikipedia pages

    # ================================================================
    # PASS 2 — FTSE SmallCap: fetch market cap, sort, take top 150
    # ================================================================
    print(f"\n[FTSE SmallCap — fetching market caps via yfinance]")
    label, url, _ = WIKIPEDIA_INDICES[2]
    wiki_entries = scrape_ftse_wiki(url, label)

    # Filter out companies already added from FTSE 100/250
    smallcap_entries = [
        (name, ticker) for name, ticker in wiki_entries
        if name.lower() not in seen_names and ticker
    ]
    print(f"  {len(smallcap_entries)} new SmallCap companies to check")

    # Fetch market caps
    smallcap_with_mcap = []
    for i, (name, ticker) in enumerate(smallcap_entries, 1):
        mcap = get_market_cap(ticker)
        smallcap_with_mcap.append((name, ticker, mcap))
        status = f"£{mcap/1e6:.0f}M" if mcap else "n/a"
        print(f"  [{i}/{len(smallcap_entries)}] {name} ({ticker}.L)  mcap={status}")
        time.sleep(1)  # 1s between yfinance calls to avoid Yahoo throttling

    # Sort by market cap descending, take top SMALLCAP_TAKE
    smallcap_with_mcap.sort(key=lambda x: x[2], reverse=True)
    top_smallcap = smallcap_with_mcap[:SMALLCAP_TAKE]

    print(f"\n  Top {SMALLCAP_TAKE} SmallCap by market cap — resolving CH numbers...")
    for name, ticker, mcap in top_smallcap:
        if name.lower() in seen_names:
            continue
        time.sleep(DELAY)
        cn, official_name = ch_search(s, name)
        mcap_str = f"£{mcap/1e6:.0f}M" if mcap else "n/a"
        if cn and cn not in seen_numbers:
            companies.append({
                "number": cn,
                "name":   official_name,
                "source": "FTSE SmallCap",
                "ticker": ticker,
                "market_cap": mcap,
            })
            seen_numbers.add(cn)
            seen_names.add(name.lower())
            print(f"  {official_name} ({cn})  mcap={mcap_str}")
        else:
            seen_names.add(name.lower())
            print(f"  [skipped] {name}  mcap={mcap_str}")

    # ================================================================
    # PASS 3 — CH API top-up if still under 500
    # ================================================================
    if len(companies) < POOL_SIZE:
        extra = discover_plcs(s, seen_numbers, target=POOL_SIZE)
        companies.extend(extra)

    companies = companies[:POOL_SIZE]

    # ================================================================
    # Save
    # ================================================================
    with open(OUTPUT, "w") as f:
        json.dump(companies, f, indent=2)

    by_source = {}
    for c in companies:
        src = c.get("source", "unknown")
        by_source[src] = by_source.get(src, 0) + 1

    print(f"\n{'='*60}")
    print(f"Saved {len(companies)} companies to {OUTPUT}")
    for src, count in sorted(by_source.items()):
        print(f"  {src:20s}: {count}")


if __name__ == "__main__":
    main()
