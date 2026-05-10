"""
FundLens — AMFI Excel Scraper
==============================
Downloads real AMFI Excel monthly → parses holdings →
computes buy/sell signals → stores in Supabase

Run automatically on 12th of every month via Railway cron.
Can also be triggered manually anytime.
"""

import os
import io
import ssl
import json
import logging
import urllib.request
from datetime import date, datetime
from collections import defaultdict

import openpyxl
from supabase import create_client, Client

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

# AMFI Excel URL pattern — works every month without fail
# Confirmed from: amfiindia.com/research-information/amfi-monthly
MONTH_CODES = {
    1:'jan',2:'feb',3:'mar',4:'apr',5:'may',6:'jun',
    7:'jul',8:'aug',9:'sep',10:'oct',11:'nov',12:'dec'
}

def get_amfi_url(year: int, month: int) -> str:
    mo = MONTH_CODES[month]
    return f"https://portal.amfiindia.com/spages/am{mo}{year}repo.xls"

def get_current_month() -> str:
    d = date.today()
    return f"{d.year}-{d.month:02d}"

def get_prev_month() -> str:
    d = date.today()
    if d.month == 1:
        return f"{d.year-1}-12"
    return f"{d.year}-{d.month-1:02d}"


# ============================================================
# STEP 1: DOWNLOAD AMFI EXCEL
# ============================================================
def download_amfi_excel(year: int, month: int) -> tuple[bytes, int, int]:
    """
    Download AMFI Excel. If current month not published yet,
    automatically falls back to previous month.
    Returns (bytes, actual_year, actual_month)
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/vnd.ms-excel,*/*',
        'Accept-Language': 'en-IN,en;q=0.9',
        'Referer': 'https://www.amfiindia.com/',
        'Connection': 'keep-alive',
    }

    # Try current month first, then fallback to previous 3 months
    attempts = []
    y, m = year, month
    for _ in range(4):
        attempts.append((y, m))
        if m == 1:
            m, y = 12, y - 1
        else:
            m -= 1

    for (ay, am) in attempts:
        url = get_amfi_url(ay, am)
        logger.info(f"Trying AMFI Excel: {url}")
        try:
            req = urllib.request.Request(url, headers=headers)
            response = urllib.request.urlopen(req, timeout=30, context=ctx)
            data = response.read()
            if len(data) > 10000:  # valid file check
                logger.info(f"✓ Downloaded {len(data):,} bytes for {ay}-{am:02d}")
                return data, ay, am
            else:
                logger.warning(f"File too small ({len(data)} bytes), skipping")
        except Exception as e:
            logger.warning(f"Failed {ay}-{am:02d}: {e}")
            continue

    raise Exception("Could not download AMFI Excel for any recent month")


# ============================================================
# STEP 2: PARSE EXCEL → HOLDINGS
# ============================================================
def extract_amc_name(full_amc: str) -> str:
    """Normalize AMC names from AMFI Excel"""
    amc = full_amc.strip()
    # Remove common suffixes
    for suffix in [' Mutual Fund', ' Asset Management', ' AMC Limited', ' AMC Ltd']:
        amc = amc.replace(suffix, '')
    return amc.strip()

def parse_holdings(excel_bytes: bytes) -> tuple[dict, dict]:
    """
    Parse AMFI Excel file.
    Returns:
        holdings: {isin: {amc: total_quantity}}
        stock_meta: {isin: {name, sector}}
    """
    wb = openpyxl.load_workbook(io.BytesIO(excel_bytes), read_only=True)
    ws = wb.active

    holdings = defaultdict(lambda: defaultdict(float))
    stock_meta = {}
    rows_parsed = 0
    rows_skipped = 0

    for i, row in enumerate(ws.iter_rows(values_only=True)):
        # Skip header
        if i == 0:
            continue

        try:
            if not row[0]:
                rows_skipped += 1
                continue

            amc_raw = str(row[0]).strip()
            # scheme = str(row[1]).strip() if row[1] else ''
            isin = str(row[2]).strip() if row[2] else ''
            company = str(row[3]).strip() if row[3] else ''
            sector = str(row[4]).strip() if row[4] else 'Other'
            qty_raw = row[6] if len(row) > 6 else 0

            # Validate ISIN format (INE + 10 chars)
            import re
            if not re.match(r'^IN[A-Z0-9]{10}$', isin):
                rows_skipped += 1
                continue

            # Parse quantity
            try:
                qty = float(str(qty_raw).replace(',', '').strip()) if qty_raw else 0
            except (ValueError, AttributeError):
                qty = 0

            if not company or qty < 0:
                rows_skipped += 1
                continue

            amc = extract_amc_name(amc_raw)
            holdings[isin][amc] += qty

            if isin not in stock_meta:
                stock_meta[isin] = {
                    'name': company,
                    'sector': sector or 'Other'
                }
            rows_parsed += 1

        except Exception as e:
            rows_skipped += 1
            continue

    wb.close()
    logger.info(f"Parsed {rows_parsed} rows, skipped {rows_skipped}")
    logger.info(f"Found {len(holdings)} unique stocks, {len(set(a for h in holdings.values() for a in h))} AMCs")
    return dict(holdings), stock_meta


# ============================================================
# STEP 3: LOAD PREVIOUS MONTH FROM SUPABASE
# ============================================================
def load_prev_month_from_db(supabase: Client, prev_month: str) -> dict:
    """Load previous month's raw holdings from Supabase"""
    logger.info(f"Loading previous month ({prev_month}) from Supabase...")
    try:
        result = supabase.table("amc_holdings_raw")\
            .select("isin, amc_name, quantity")\
            .eq("data_month", prev_month)\
            .execute()

        prev = defaultdict(dict)
        for row in result.data:
            prev[row['isin']][row['amc_name']] = row['quantity']

        logger.info(f"Loaded {len(result.data)} previous month records")
        return dict(prev)
    except Exception as e:
        logger.warning(f"No previous month data: {e}")
        return {}


# ============================================================
# STEP 4: COMPUTE BUY/SELL SIGNALS
# ============================================================
def fmt_qty(n: float) -> str:
    if n >= 10000000: return f"{n/1000000:.1f}Cr"
    if n >= 100000: return f"{n/100000:.1f}L"
    if n >= 1000: return f"{n/1000:.0f}K"
    return str(int(n))

def compute_signal(bought, sold, new_entry, exited, total) -> str:
    if new_entry >= 3: return 'new'
    if exited >= 2 and sold > bought: return 'exit'
    if bought > sold * 1.5 and bought >= 3: return 'buy'
    if sold > bought * 1.5 and sold >= 3: return 'sell'
    return 'hold'

def aggregate_intelligence(
    curr_holdings: dict,
    prev_holdings: dict,
    stock_meta: dict,
    data_month: str
) -> tuple[list, list]:
    """
    Compare current vs previous month.
    Returns (stock_intelligence_rows, amc_holdings_rows)
    """
    all_isins = set(list(curr_holdings.keys()) + list(prev_holdings.keys()))
    stock_rows = []
    amc_rows = []
    raw_rows = []

    for isin in all_isins:
        curr = curr_holdings.get(isin, {})
        prev = prev_holdings.get(isin, {})

        # Skip if not held this month at all
        if not curr:
            continue

        all_amcs = set(list(curr.keys()) + list(prev.keys()))
        bought = sold = holding = new_entry = exited = 0
        amc_details = []

        for amc in all_amcs:
            cq = curr.get(amc, 0)
            pq = prev.get(amc, 0)

            if cq > 0 and pq == 0:
                action = 'new_entry'; new_entry += 1; bought += 1
            elif cq == 0 and pq > 0:
                action = 'exit'; exited += 1; sold += 1
            elif cq > pq * 1.02:
                action = 'buy'; bought += 1
            elif cq < pq * 0.98:
                action = 'sell'; sold += 1
            else:
                action = 'hold'; holding += 1

            chg = round(((cq - pq) / pq * 100), 1) if pq > 0 else (100.0 if cq > 0 else 0.0)

            if cq > 0 or pq > 0:
                amc_details.append({
                    'isin': isin,
                    'amc_name': amc,
                    'action': action,
                    'curr_qty': int(cq),
                    'prev_qty': int(pq),
                    'change_pct': chg,
                    'data_month': data_month,
                })

            # Also save raw for next month comparison
            if cq > 0:
                raw_rows.append({
                    'isin': isin,
                    'company': stock_meta.get(isin, {}).get('name', isin),
                    'sector': stock_meta.get(isin, {}).get('sector', 'Other'),
                    'amc_name': amc,
                    'quantity': int(cq),
                    'data_month': data_month,
                })

        order = {'new_entry':0,'buy':1,'hold':2,'sell':3,'exit':4}
        amc_details.sort(key=lambda x: order[x['action']])

        total = len([a for a in amc_details if a['action'] != 'exit'])
        signal = compute_signal(bought, sold, new_entry, exited, total)
        meta = stock_meta.get(isin, {'name': isin, 'sector': 'Other'})

        stock_rows.append({
            'isin': isin,
            'name': meta['name'],
            'sector': meta['sector'],
            'signal': signal,
            'total_amcs': total,
            'bought': bought,
            'sold': sold,
            'holding': holding,
            'new_entry': new_entry,
            'exited': exited,
            'data_month': data_month,
            'updated_at': datetime.utcnow().isoformat(),
        })
        amc_rows.extend(amc_details)

    stock_rows.sort(key=lambda x: -x['total_amcs'])
    logger.info(f"Computed signals: {len(stock_rows)} stocks, {len(amc_rows)} AMC records")
    return stock_rows, amc_rows, raw_rows


# ============================================================
# STEP 5: SAVE TO SUPABASE
# ============================================================
def save_to_supabase(
    supabase: Client,
    stock_rows: list,
    amc_rows: list,
    raw_rows: list,
    data_month: str
):
    logger.info("Saving to Supabase...")
    BATCH = 50

    # Save stock intelligence
    for i in range(0, len(stock_rows), BATCH):
        supabase.table("stock_intelligence")\
            .upsert(stock_rows[i:i+BATCH], on_conflict="isin")\
            .execute()
    logger.info(f"✓ Saved {len(stock_rows)} stocks")

    # Save AMC holdings
    for i in range(0, len(amc_rows), BATCH):
        supabase.table("amc_holdings")\
            .upsert(amc_rows[i:i+BATCH], on_conflict="isin,amc_name")\
            .execute()
    logger.info(f"✓ Saved {len(amc_rows)} AMC records")

    # Save raw holdings (for next month comparison)
    for i in range(0, len(raw_rows), BATCH):
        supabase.table("amc_holdings_raw")\
            .upsert(raw_rows[i:i+BATCH], on_conflict="isin,amc_name,data_month")\
            .execute()
    logger.info(f"✓ Saved {len(raw_rows)} raw records")

    # Save scrape meta
    supabase.table("scrape_meta").insert({
        "scraped_at": datetime.utcnow().isoformat(),
        "data_month": data_month,
        "stocks_processed": len(stock_rows),
        "amcs_scraped": len(set(r['amc_name'] for r in amc_rows)),
        "notes": "Auto-scraped from AMFI Excel"
    }).execute()
    logger.info("✓ Saved scrape meta")


# ============================================================
# MAIN ENTRY POINT
# ============================================================
def run_scrape(year: int = None, month: int = None):
    """
    Main scrape function. Runs monthly automatically.
    Args:
        year, month: override current month (for backfilling)
    """
    today = date.today()
    year = year or today.year
    month = month or today.month
    data_month = f"{year}-{month:02d}"
    prev_month = f"{year}-{month-1:02d}" if month > 1 else f"{year-1}-12"

    logger.info(f"=== FundLens Scrape: {data_month} ===")

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # 1. Download — auto-fallback to previous month if current not published
    excel_bytes, actual_year, actual_month = download_amfi_excel(year, month)
    data_month = f"{actual_year}-{actual_month:02d}"
    prev_month = f"{actual_year}-{actual_month-1:02d}" if actual_month > 1 else f"{actual_year-1}-12"
    logger.info(f"Using data month: {data_month}")

    # 2. Parse
    curr_holdings, stock_meta = parse_holdings(excel_bytes)

    # 3. Load previous month
    prev_holdings = load_prev_month_from_db(supabase, prev_month)

    # 4. Compute signals
    stock_rows, amc_rows, raw_rows = aggregate_intelligence(
        curr_holdings, prev_holdings, stock_meta, data_month
    )

    # 5. Save to Supabase
    save_to_supabase(supabase, stock_rows, amc_rows, raw_rows, data_month)

    logger.info(f"=== Done: {len(stock_rows)} stocks processed ===")
    return {
        "stocks": len(stock_rows),
        "amc_records": len(amc_rows),
        "month": data_month
    }


if __name__ == "__main__":
    result = run_scrape()
    print(f"\n✅ Scrape complete: {result}")
