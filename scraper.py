"""
FundLens Scraper — Dynamic + Diagnostic
=========================================
Fully dynamic: auto-discovers families, auto-finds latest data month.
Full diagnostics: tells you exactly WHY if data is 0.
"""

import os, json, time, logging, urllib.request
from datetime import date, datetime
from collections import defaultdict
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
API = "https://mfdata.in/api/v1"
HEADERS = {'User-Agent': 'FundLens/1.0', 'Accept': 'application/json'}

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

def save(sb, stock_rows, amc_rows, raw_rows, dm, notes):
    B=50
    for i in range(0,len(stock_rows),B):
        sb.table("stock_intelligence").upsert(stock_rows[i:i+B],on_conflict="isin").execute()
    for i in range(0,len(amc_rows),B):
        sb.table("amc_holdings").upsert(amc_rows[i:i+B],on_conflict="isin,amc_name").execute()
    for i in range(0,len(raw_rows),B):
        sb.table("amc_holdings_raw").upsert(raw_rows[i:i+B],on_conflict="isin,amc_name,data_month").execute()
    sb.table("scrape_meta").insert({
        "scraped_at":datetime.utcnow().isoformat(),"data_month":dm,
        "stocks_processed":len(stock_rows),
        "amcs_scraped":len(set(r['amc_name'] for r in amc_rows)) if amc_rows else 0,
        "notes":notes
    }).execute()
    logger.info(f"✓ Saved {len(stock_rows)} stocks, {len(amc_rows)} AMC records")

def load_prev(sb, pm):
    try:
        r=sb.table("amc_holdings_raw").select("isin,amc_name,quantity").eq("data_month",pm).execute()
        prev=defaultdict(dict)
        for row in r.data: prev[row['isin']][row['amc_name']]=row['quantity']
        logger.info(f"Loaded {len(r.data)} prev month records from DB ({pm})")
        return dict(prev)
    except Exception as e:
        diag.warn(f"No previous month data in DB: {e}")
        return {}

# ============================================================
# MAIN
# ============================================================
def run_scrape(year=None, month=None):
    global diag
    diag = Diagnostic()

    diag.info("=== FundLens Dynamic Scraper Started ===")
    diag.info(f"Date: {datetime.utcnow().isoformat()}")

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # STEP 1: Verify API
    if not verify_api():
        print(diag.summary())
        save(sb, [], [], [], "unknown",
             f"FAILED: {'; '.join(diag.errors)}")
        return {"stocks": 0, "error": diag.errors}

    # STEP 2: Find latest data month
    dm = find_latest_month()
    if not dm:
        print(diag.summary())
        save(sb, [], [], [], "unknown",
             f"FAILED: {'; '.join(diag.errors)}")
        return {"stocks": 0, "error": diag.errors}

    pm = get_prev_month(dm)
    diag.info(f"Data month: {dm}, Previous month: {pm}")

    # STEP 3: Get equity families
    families = get_equity_families()
    if not families:
        print(diag.summary())
        save(sb, [], [], [], dm,
             f"FAILED: {'; '.join(diag.errors)}")
        return {"stocks": 0, "error": diag.errors}

    # STEP 4: Fetch holdings
    curr = defaultdict(lambda: defaultdict(float))
    meta = {}
    n_ok = 0
    n_empty = 0
    n_error = 0
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

        if (i+1) % 20 == 0:
            diag.info(f"Progress: {i+1}/{len(families)} families | {len(curr)} stocks so far")

        time.sleep(0.4)

    diag.info(f"Holdings fetch complete: {n_ok} with data, {n_empty} empty, {n_error} errors")

    if n_error > 0:
        diag.warn(f"Sample errors: {'; '.join(sample_errors)}")

    if len(curr) == 0:
        diag.fail("ZERO stocks found after fetching all families")
        diag.fail(f"Families fetched: {len(families)}")
        diag.fail(f"Families with data: {n_ok}")
        diag.fail(f"Families empty for {dm}: {n_empty}")
        diag.fail(f"Families with errors: {n_error}")

        # Try to diagnose why
        if n_ok == 0 and n_error == 0:
            diag.fail(f"All families returned empty for {dm}")
            diag.fail("This month may not have portfolio data yet in mfdata.in")
            diag.warn("Check manually: curl https://mfdata.in/api/v1/families/87/holdings?month=" + dm)
        elif n_error > len(families) * 0.5:
            diag.fail("More than 50% of families returned errors — API may be rate limiting")

        print(diag.summary())
        save(sb, [], [], [], dm, f"FAILED: {'; '.join(diag.errors[:3])}")
        return {"stocks": 0, "month": dm, "diagnostic": diag.errors}

    diag.ok(f"Found {len(curr)} unique stocks across {n_ok} fund families")

    # STEP 5: Load prev month
    prev = load_prev(sb, pm)

    # STEP 6: Aggregate
    stock_rows, amc_rows, raw_rows = aggregate(dict(curr), prev, meta, dm)
    diag.ok(f"Computed signals: {len(stock_rows)} stocks, {len(amc_rows)} AMC records")

    # STEP 7: Save
    save(sb, stock_rows, amc_rows, raw_rows, dm,
         f"OK: {len(stock_rows)} stocks from {n_ok} families via mfdata.in/families")
    diag.ok(f"Saved to Supabase successfully")

    print(diag.summary())

    result = {
        "stocks": len(stock_rows),
        "amc_records": len(amc_rows),
        "month": dm,
        "families_with_data": n_ok,
        "families_empty": n_empty,
        "families_errors": n_error,
    }
    logger.info(f"=== Done: {result} ===")
    return result


if __name__ == "__main__":
    result = run_scrape()
    print(f"\n{'✅' if result.get('stocks',0) > 0 else '❌'} Result: {result}")
