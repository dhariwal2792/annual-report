# UK Annual Accounts Downloader

Downloads the latest annual accounts PDFs for the top 500 UK companies from Companies House.

---

## What It Does

1. Builds a ranked list of 700 UK companies (buffer to guarantee 500 with accounts):
   - **FTSE 100** — top 100 companies by market cap (via Wikipedia)
   - **FTSE 250** — next 250 companies by market cap (via Wikipedia)
   - **FTSE SmallCap** — next 150 sorted by live market cap (via yfinance)
   - **CH API top-up** — active PLCs to fill remaining slots
2. For each company, finds the latest full annual accounts filing on Companies House
3. Downloads the PDF and saves to `annual_accounts/`
4. Stops at exactly **500 downloaded PDFs**
5. Progress is saved after every company — safe to stop and resume

---

## Requirements

- Python 3.9+
- Free Companies House API key
- Dependencies:

```bash
pip3 install requests beautifulsoup4 yfinance
```

---

## Setup

### 1. Get a Companies House API Key

- Go to: https://developer.company-information.service.gov.uk/
- Sign up and create an application (select **Live** environment)
- Copy your API key

### 2. Set the API Key

```bash
export CH_API_KEY=your_api_key_here
```

To make it permanent, add the line above to your `~/.zshrc` or `~/.bashrc`.

### 3. Verify the Key Works

```bash
curl -u "$CH_API_KEY:" "https://api.company-information.service.gov.uk/company/00102498"
```

Should return JSON with `"company_name":"BP P.L.C."`. If you see `Invalid Authorization`, the key is wrong.

---

## Usage

### Step 1 — Build the Company List (run once)

```bash
python build_company_list.py
```

This creates `company_list.json` — a ranked list of 700 UK companies resolved to their Companies House registration numbers. Takes a few minutes due to API rate limits.

### Step 2 — Download Annual Accounts

```bash
python download_accounts.py
```

Downloads PDFs to `annual_accounts/`. Progress is saved to `index.json` after every company.

**If interrupted**, just run the same command again — it resumes from where it stopped.

---

## Output Files

| File | Description |
|------|-------------|
| `annual_accounts/` | Downloaded PDF annual accounts |
| `company_list.json` | Ranked list of 700 candidate companies |
| `index.json` | Progress tracker — downloaded / skipped / pending |
| `no_accounts.json` | Companies where no downloadable accounts were found |

---

## PDF Naming Convention

```
CompanyName_RegistrationNumber_FilingDate_Type.pdf
```

Example:
```
HSBC_HOLDINGS_PLC_00617987_2025-05-12_AA.pdf
```

---

## Account Types Downloaded

The script prioritises in this order and skips low-quality filings:

| Priority | Type | Description |
|----------|------|-------------|
| 1 | `group` | Full group accounts (largest companies) |
| 2 | `full` | Full statutory accounts |
| 3 | `small-full` | Small company full accounts |

Skipped automatically: interim reports, dormant accounts, micro-entity, abbreviated.

---

## Rate Limits

The Companies House API allows **600 requests per 5 minutes**. The script uses 0.3s delays between requests. If the limit is hit (HTTP 429), the script saves progress and exits cleanly. Wait 5 minutes and rerun.

---

## Utilities

### Fix companies with no accounts found

```bash
python fix_no_accounts.py
```

Re-searches Companies House by name for skipped companies, tries relaxed filters, and retries downloads.

### Reconcile downloaded files with index

```bash
python reconcile.py
```

Checks `annual_accounts/` against `index.json` and fixes any mismatches.

---

## Data Source

Annual accounts are **statutory filings** submitted to Companies House — the UK's official company register. For large PLCs, these are typically the same as the full annual report. They are public documents available at:

https://find-and-update.company-information.service.gov.uk/
