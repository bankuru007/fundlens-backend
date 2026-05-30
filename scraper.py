"""
FundLens Scraper — Dynamic + Diagnostic (self-contained, no database)
=====================================================================
Fully dynamic: auto-discovers families, auto-finds latest data month.
Full diagnostics: tells you exactly WHY if data is 0.

run_scrape() RETURNS the aggregated data (real data only, no fabrication).
The previous month is fetched live too, so buy/sell/new/exit signals are
computed entirely from authentic mfdata.in holdings — no DB required.
"""

import json, time, logging, urllib.request, urllib.error
from datetime import date, datetime
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

API = "https://mfdata.in/api/v1"
# Real browser User-Agent — generic UAs get 403'd by Cloudflare bot protection.
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
}

# ============================================================
# DIAGNOSTIC TRACKER
# Tracks every step so we know exactly what happened
# ============================================================
class Diagnostic:
    def __init__(self):
        self.steps = []
        self.errors = []
        self.warnings = []

    def ok(self, msg):
        self.steps.append(f"✓ {msg}")
        logger.info(f"✓ {msg}")

    def fail(self, msg):
        self.errors.append(f"✗ {msg}")
        logger.error(f"✗ {msg}")

    def warn(self, msg):
        self.warnings.append(f"⚠ {msg}")
        logger.warning(f"⚠ {msg}")

    def info(self, msg):
        self.steps.append(f"→ {msg}")
        logger.info(f"→ {msg}")

    def summary(self):
        lines = ["", "=" * 60, "DIAGNOSTIC SUMMARY", "=" * 60]
        lines += self.steps
        if self.warnings:
            lines += ["", "WARNINGS:"] + self.warnings
        if self.errors:
            lines += ["", "ERRORS:"] + self.errors
        lines.append("=" * 60)
        return "\n".join(lines)

    def why_zero(self):
        """Explain exactly why stocks = 0"""
        reasons = []
        for e in self.errors:
            reasons.append(e)
        for w in self.warnings:
            reasons.append(w)
        if not reasons:
            reasons.append("⚠ Unknown reason — check logs above")
        return reasons

diag = Diagnostic()

# ============================================================
# HTTP HELPER
# ============================================================
def api_get(path, retries=3):
    url = API + path
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            r = urllib.request.urlopen(req, timeout=20)
            raw = r.read()
            data = json.loads(raw)
            if data.get('status') == 'success':
                return data.get('data', []), None
            else:
                err = data.get('message', 'API returned non-success status')
                return [], err
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}: {e.reason} for {url}"
            if attempt == retries - 1:
                return [], err
            time.sleep(2 ** attempt)
        except urllib.error.URLError as e:
            err = f"Network error: {e.reason} for {url}"
            if attempt == retries - 1:
                return [], err
            time.sleep(2 ** attempt)
        except json.JSONDecodeError as e:
            return [], f"Invalid JSON response from {url}: {e}"
        except Exception as e:
            if attempt == retries - 1:
                return [], str(e)
            time.sleep(2 ** attempt)
    return [], "Max retries exceeded"

# ============================================================
# STEP 1: VERIFY API IS REACHABLE
# ============================================================
def verify_api():
    diag.info("Testing mfdata.in API connectivity...")
    data, err = api_get("/families?limit=1")
    if err:
        diag.fail(f"mfdata.in API unreachable: {err}")
        diag.fail("Possible causes: API down, network issue, rate limited")
        return False
    if not data:
        diag.fail("mfdata.in API reachable but returned empty data")
        return False
    diag.ok(f"mfdata.in API is reachable")
    return True

# ============================================================
# STEP 2: AUTO-DISCOVER LATEST DATA MONTH
# ============================================================
def find_latest_month():
    """
    Test a known family to find which months have data.
    Tries from current month going back 18 months.
    """
    diag.info("Auto-discovering latest available data month...")

    # Use family 87 (ABSL Large Cap) as probe — stable, always exists
    probe_family = 87

    today = date.today()
    months_tried = []

    for i in range(18):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12; y -= 1
        month_str = f"{y}-{m:02d}"
        months_tried.append(month_str)

        data, err = api_get(f"/families/{probe_family}/holdings?month={month_str}")

        if err:
            diag.warn(f"  {month_str}: error — {err}")
            continue

        if data and len(data) > 0:
            diag.ok(f"Latest available month: {month_str} ({len(data)} holdings in probe fund)")
            return month_str

        diag.info(f"  {month_str}: no data")
        time.sleep(0.3)

    diag.fail(f"No data found in last 18 months. Tried: {', '.join(months_tried[:6])}...")
    diag.fail("mfdata.in may not have portfolio holdings data currently")
    return None

# ============================================================
# STEP 3: AUTO-DISCOVER EQUITY FAMILIES
# ============================================================
def get_equity_families():
    diag.info("Fetching all fund families from mfdata.in...")
    data, err = api_get("/families?limit=2000")

    if err:
        diag.fail(f"Could not fetch families: {err}")
        return []

    if not data:
        diag.fail("Families endpoint returned empty list")
        diag.fail("Try: curl https://mfdata.in/api/v1/families?limit=5 to check manually")
        return []

    # Handle list or dict
    if isinstance(data, dict):
        data = data.get('families', data.get('items', data.get('data', [])))

    diag.ok(f"Total families from API: {len(data)}")

    # Filter equity funds dynamically
    equity_keywords = [
        'flexi cap', 'large cap', 'mid cap', 'small cap', 'multi cap',
        'large & mid', 'focused', 'value fund', 'contra', 'dividend yield',
        'bluechip', 'opportunities', 'equity', 'elss', 'tax saver',
        'multicap', 'largecap', 'midcap', 'smallcap',
    ]
    debt_keywords = [
        'liquid', 'overnight', 'debt', 'bond', 'gilt', 'money market',
        'credit risk', 'banking and psu', 'corporate bond', 'duration',
        'fixed maturity', 'arbitrage', 'fmp',
    ]

    equity = []
    skipped_debt = 0
    skipped_no_id = 0

    for f in data:
        fid = f.get('id') or f.get('family_id')
        if not fid:
            skipped_no_id += 1
            continue

        name = (f.get('name', '') or '').lower()
        cat = (f.get('category', '') or '').lower()
        stype = (f.get('scheme_type', '') or '').lower()

        combined = f"{name} {cat} {stype}"

        # Skip obvious debt funds
        if any(kw in combined for kw in debt_keywords):
            skipped_debt += 1
            continue

        # Include if equity-related
        if any(kw in combined for kw in equity_keywords):
            equity.append({
                'id': fid,
                'name': f.get('name', ''),
                'amc': f.get('amc', '') or f.get('amc_name', ''),
            })

    diag.ok(f"Equity families found: {len(equity)}")
    diag.info(f"Skipped: {skipped_debt} debt funds, {skipped_no_id} without ID")

    if len(equity) == 0:
        diag.fail("Zero equity families found after filtering")
        diag.fail(f"Sample of raw family data: {json.dumps(data[:2], indent=2)[:500]}")
        diag.warn("The API response format may have changed — check field names")

    return equity

# ============================================================
# STEP 4: FETCH HOLDINGS PER FAMILY
# ============================================================
def extract_amc(family_name, amc_field=''):
    if amc_field:
        amc = amc_field.strip()
        for s in [' Mutual Fund',' Asset Management',' AMC Limited',' AMC Ltd',' AMC']:
            amc = amc.replace(s, '')
        return amc.strip() or family_name.split()[0]

    rules = [
        ('SBI ','SBI MF'),('HDFC ','HDFC AMC'),('ICICI Prudential','ICICI Pru AMC'),
        ('Axis ','Axis MF'),('Kotak ','Kotak MF'),('Nippon India','Nippon India MF'),
        ('Mirae Asset','Mirae Asset MF'),('DSP ','DSP MF'),
        ('Aditya Birla Sun Life','Aditya Birla Sun Life MF'),('ABSL ','Aditya Birla Sun Life MF'),
        ('UTI ','UTI AMC'),('Franklin Templeton','Franklin Templeton MF'),
        ('PGIM India','PGIM India MF'),('Invesco India','Invesco India MF'),
        ('Tata ','Tata MF'),('Canara Robeco','Canara Robeco MF'),
        ('Bandhan ','Bandhan MF'),('IDFC First','IDFC FIRST MF'),
        ('Sundaram ','Sundaram MF'),('LIC ','LIC MF'),
        ('Motilal Oswal','Motilal Oswal MF'),('PPFAS','PPFAS MF'),
        ('Quant ','Quant MF'),('WhiteOak','WhiteOak Capital MF'),
        ('Edelweiss ','Edelweiss MF'),('Groww ','Groww MF'),
        ('Bajaj Finserv','Bajaj Finserv MF'),('360 ONE','360 ONE MF'),
        ('HSBC ','HSBC MF'),('Navi ','Navi MF'),('Zerodha ','Zerodha MF'),
        ('Shriram ','Shriram MF'),('Bank of India','Bank of India MF'),
        ('Baroda BNP','Baroda BNP Paribas MF'),('Union ','Union MF'),
        ('Mahindra Manulife','Mahindra Manulife MF'),
    ]
    for prefix, amc in rules:
        if family_name.startswith(prefix) or prefix in family_name:
            return amc
    parts = family_name.split()
    return ' '.join(parts[:2]) + ' MF' if len(parts) >= 2 else family_name

def fetch_family_holdings(fid, fname, amc, data_month):
    data, err = api_get(f"/families/{fid}/holdings?month={data_month}")

    if err:
        return [], err

    if isinstance(data, dict):
        data = data.get('holdings', data.get('stocks', []))

    results = []
    for h in (data or []):
        isin = (h.get('isin') or h.get('stock_isin') or '').strip()
        name = (h.get('stock_name') or h.get('name') or h.get('company') or '').strip()
        sector = (h.get('sector') or h.get('industry') or 'Other').strip()
        qty = float(h.get('quantity') or h.get('units') or h.get('shares') or 0)
        val = float(h.get('market_value') or h.get('value') or 0)

        if name and (qty > 0 or val > 0):
            results.append({
                'isin': isin or name,
                'name': name,
                'sector': sector or 'Other',
                'amc': amc,
                'quantity': qty if qty > 0 else val,
            })

    return results, None

# ============================================================
# STEP 5: AGGREGATE
# ============================================================
def compute_signal(b,s,n,e,t):
    if n>=3: return 'new'
    if e>=2 and s>b: return 'exit'
    if b>s*1.5 and b>=3: return 'buy'
    if s>b*1.5 and s>=3: return 'sell'
    return 'hold'

def get_prev_month(dm):
    y,m=int(dm[:4]),int(dm[5:])
    m-=1
    if m==0: m,y=12,y-1
    return f"{y}-{m:02d}"

def aggregate(curr, prev, meta, dm):
    stock_rows, amc_rows, raw_rows = [], [], []
    for isin in set(list(curr.keys())+list(prev.keys())):
        c=curr.get(isin,{}); p=prev.get(isin,{})
        if not c: continue
        all_amcs=set(list(c.keys())+list(p.keys()))
        bought=sold=holding=new_entry=exited=0
        details=[]
        for amc in all_amcs:
            cq=c.get(amc,0); pq=p.get(amc,0)
            if cq>0 and pq==0:   action='new_entry'; new_entry+=1; bought+=1
            elif cq==0 and pq>0: action='exit'; exited+=1; sold+=1
            elif cq>pq*1.02:     action='buy'; bought+=1
            elif cq<pq*0.98:     action='sell'; sold+=1
            else:                action='hold'; holding+=1
            chg=round(((cq-pq)/pq*100),1) if pq>0 else (100.0 if cq>0 else 0.0)
            if cq>0 or pq>0:
                details.append({'isin':isin,'amc_name':amc,'action':action,
                    'curr_qty':int(cq),'prev_qty':int(pq),'change_pct':chg,'data_month':dm})
            if cq>0:
                raw_rows.append({'isin':isin,'company':meta.get(isin,{}).get('name',isin),
                    'sector':meta.get(isin,{}).get('sector','Other'),
                    'amc_name':amc,'quantity':int(cq),'data_month':dm})
        order={'new_entry':0,'buy':1,'hold':2,'sell':3,'exit':4}
        details.sort(key=lambda x: order[x['action']])
        total=len([a for a in details if a['action']!='exit'])
        m=meta.get(isin,{'name':isin,'sector':'Other'})
        stock_rows.append({'isin':isin,'name':m['name'],'sector':m['sector'],
            'signal':compute_signal(bought,sold,new_entry,exited,total),
            'total_amcs':total,'bought':bought,'sold':sold,'holding':holding,
            'new_entry':new_entry,'exited':exited,
            'data_month':dm,'updated_at':datetime.utcnow().isoformat()})
        amc_rows.extend(details)
    stock_rows.sort(key=lambda x: -x['total_amcs'])
    return stock_rows, amc_rows, raw_rows

def fetch_month(families, dm):
    """Fetch holdings for every family for a given month. Returns (curr, meta, stats)."""
    curr = defaultdict(lambda: defaultdict(float))
    meta = {}
    n_ok = n_empty = n_error = 0
    sample_errors = []

    for i, f in enumerate(families):
        amc = extract_amc(f['name'], f.get('amc', ''))
        holdings, err = fetch_family_holdings(f['id'], f['name'], amc, dm)

        if err:
            n_error += 1
            if len(sample_errors) < 3:
                sample_errors.append(f"{f['name'][:30]}: {err}")
        elif len(holdings) == 0:
            n_empty += 1
        else:
            n_ok += 1
            for h in holdings:
                curr[h['isin']][amc] += h['quantity']
                if h['isin'] not in meta:
                    meta[h['isin']] = {'name': h['name'], 'sector': h['sector']}

        if (i + 1) % 20 == 0:
            diag.info(f"  [{dm}] progress {i+1}/{len(families)} | {len(curr)} stocks so far")

        time.sleep(0.4)

    return curr, meta, {"ok": n_ok, "empty": n_empty, "error": n_error, "sample_errors": sample_errors}

# ============================================================
# MAIN — fetches real data and RETURNS it (no database)
# ============================================================
def run_scrape(year=None, month=None):
    """
    Fetch real MF holdings from mfdata.in and return aggregated intelligence.

    Returns a dict:
      {
        "status": "ok" | "unavailable",
        "stocks": [ ...stock_intelligence rows... ],
        "amc_details": { isin: [ ...amc rows... ] },
        "data_month": "YYYY-MM" | None,
        "source": "mfdata.in",
        "fetched_at": ISO timestamp,
        "diagnostics": [ ...human-readable steps/errors... ],
        "families_with_data": int,
      }
    No fabricated data is ever returned — on failure status is "unavailable".
    """
    global diag
    diag = Diagnostic()

    diag.info("=== FundLens Dynamic Scraper Started ===")
    diag.info(f"Date: {datetime.utcnow().isoformat()}")

    def fail(dm_val):
        print(diag.summary())
        return {
            "status": "unavailable", "stocks": [], "amc_details": {},
            "data_month": dm_val, "source": "mfdata.in",
            "fetched_at": datetime.utcnow().isoformat(),
            "diagnostics": diag.errors + diag.warnings, "families_with_data": 0,
        }

    # STEP 1: Verify API
    if not verify_api():
        return fail(None)

    # STEP 2: Find latest data month (or use explicitly requested one)
    if year and month:
        dm = f"{year}-{month:02d}"
        diag.info(f"Using requested month: {dm}")
    else:
        dm = find_latest_month()
    if not dm:
        return fail(None)

    pm = get_prev_month(dm)
    diag.info(f"Data month: {dm}, Previous month: {pm}")

    # STEP 3: Get equity families
    families = get_equity_families()
    if not families:
        return fail(dm)

    # STEP 4: Fetch current month holdings (real data)
    diag.info(f"Fetching CURRENT month holdings ({dm})...")
    curr_d, meta, cstats = fetch_month(families, dm)
    diag.info(f"Current month: {cstats['ok']} with data, {cstats['empty']} empty, {cstats['error']} errors")
    if cstats["sample_errors"]:
        diag.warn(f"Sample errors: {'; '.join(cstats['sample_errors'])}")

    if len(curr_d) == 0:
        diag.fail(f"ZERO stocks found for {dm} after fetching {len(families)} families")
        if cstats["ok"] == 0 and cstats["error"] == 0:
            diag.fail(f"All families returned empty for {dm} — month may not be published yet")
        elif cstats["error"] > len(families) * 0.5:
            diag.fail("Over 50% of families errored — API may be rate limiting or blocking")
        return fail(dm)

    diag.ok(f"Found {len(curr_d)} unique stocks across {cstats['ok']} fund families ({dm})")

    # STEP 5: Fetch previous month holdings (real data) to compute buy/sell signals
    diag.info(f"Fetching PREVIOUS month holdings ({pm}) for signal computation...")
    prev_d, _, pstats = fetch_month(families, pm)
    prev = {isin: dict(amcs) for isin, amcs in prev_d.items()}
    if len(prev) == 0:
        diag.warn(f"No previous-month ({pm}) data — signals will treat all positions as new/held")
    else:
        diag.ok(f"Previous month: {len(prev)} stocks ({pm}) for comparison")

    # STEP 6: Aggregate
    stock_rows, amc_rows, _raw = aggregate(
        {isin: dict(amcs) for isin, amcs in curr_d.items()}, prev, meta, dm
    )
    diag.ok(f"Computed signals: {len(stock_rows)} stocks, {len(amc_rows)} AMC records")
    print(diag.summary())

    # Group amc rows by isin for fast detail lookup
    amc_by_isin = defaultdict(list)
    for r in amc_rows:
        amc_by_isin[r["isin"]].append(r)

    return {
        "status": "ok",
        "stocks": stock_rows,
        "amc_details": dict(amc_by_isin),
        "data_month": dm,
        "source": "mfdata.in",
        "fetched_at": datetime.utcnow().isoformat(),
        "diagnostics": diag.steps + diag.warnings,
        "families_with_data": cstats["ok"],
    }


if __name__ == "__main__":
    result = run_scrape()
    print(f"\n{'✅' if result.get('status') == 'ok' else '❌'} "
          f"Result: {len(result.get('stocks', []))} stocks, month={result.get('data_month')}")
