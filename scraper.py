"""
FundLens Scraper v3 — Correct Bottom-Up Approach
==================================================
Fetches by AMC FAMILY → aggregates all stocks automatically.
No manual stock list needed. Covers everything.

Flow:
1. GET /api/v1/families → all equity fund families
2. For each family: GET /api/v1/families/{id}/holdings
3. Aggregate by stock → buy/sell signals
4. Save to Supabase

~200 families × 0.5s = ~2 minutes. Works fine in GitHub Actions.
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

def api_get(path, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(API + path, headers=HEADERS)
            r = urllib.request.urlopen(req, timeout=20)
            data = json.loads(r.read())
            return data.get('data', []) if data.get('status') == 'success' else []
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                logger.warning(f"Failed {path}: {e}")
                return []

def get_data_month():
    today = date.today()
    # Use 2 months ago if before 15th, else previous month
    m = today.month - (2 if today.day < 15 else 1)
    y = today.year
    while m <= 0:
        m += 12; y -= 1
    return f"{y}-{m:02d}"

def get_prev_month(dm):
    y, m = int(dm[:4]), int(dm[5:])
    m -= 1
    if m == 0: m, y = 12, y - 1
    return f"{y}-{m:02d}"

def extract_amc(family_name, amc_field=''):
    if amc_field:
        amc = amc_field.strip()
        for s in [' Mutual Fund',' Asset Management',' AMC Limited',' AMC Ltd',' AMC']:
            amc = amc.replace(s, '')
        return amc.strip()
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

EQUITY_KEYWORDS = [
    'flexi cap','large cap','mid cap','small cap','multi cap',
    'large & mid','focused','value fund','contra','dividend yield',
    'bluechip','opportunities','equity fund','hybrid equity',
]

def get_equity_families():
    logger.info("Fetching fund families from mfdata.in...")
    raw = api_get("/families?limit=1000")

    if not raw:
        logger.error("No families returned")
        return []

    # Handle both list and dict
    if isinstance(raw, dict):
        raw = raw.get('families', raw.get('items', []))

    logger.info(f"Total families returned: {len(raw)}")

    equity = []
    for f in raw:
        name = (f.get('name', '') or '').lower()
        cat = (f.get('category', '') or '').lower()
        stype = (f.get('scheme_type', '') or '').lower()

        is_equity = (
            'equity' in cat or 'equity' in stype or
            any(kw in name for kw in EQUITY_KEYWORDS)
        )

        if is_equity and (f.get('id') or f.get('family_id')):
            equity.append({
                'id': f.get('id') or f.get('family_id'),
                'name': f.get('name', ''),
                'amc': f.get('amc', '') or f.get('amc_name', ''),
            })

    logger.info(f"Equity families: {len(equity)}")
    return equity

def fetch_holdings(family_id, amc_name, data_month):
    raw = api_get(f"/families/{family_id}/holdings?month={data_month}")

    if not raw:
        return []

    if isinstance(raw, dict):
        raw = raw.get('holdings', raw.get('stocks', []))

    results = []
    for h in (raw or []):
        isin = (h.get('isin') or h.get('stock_isin') or '').strip()
        name = (h.get('stock_name') or h.get('name') or h.get('company') or '').strip()
        sector = (h.get('sector') or h.get('industry') or 'Other').strip()
        qty = float(h.get('quantity') or h.get('units') or h.get('shares') or 0)
        val = float(h.get('market_value') or h.get('value') or 0)

        if name and (qty > 0 or val > 0):
            results.append({
                'isin': isin or name,
                'name': name, 'sector': sector or 'Other',
                'amc': amc_name,
                'quantity': qty if qty > 0 else val,
            })
    return results

def compute_signal(b, s, n, e, t):
    if n >= 3: return 'new'
    if e >= 2 and s > b: return 'exit'
    if b > s*1.5 and b >= 3: return 'buy'
    if s > b*1.5 and s >= 3: return 'sell'
    return 'hold'

def aggregate(curr, prev, meta, dm):
    stock_rows, amc_rows, raw_rows = [], [], []

    for isin in set(list(curr.keys()) + list(prev.keys())):
        c = curr.get(isin, {})
        p = prev.get(isin, {})
        if not c: continue

        bought=sold=holding=new_entry=exited=0
        details = []

        for amc in set(list(c.keys()) + list(p.keys())):
            cq = c.get(amc, 0); pq = p.get(amc, 0)
            if cq>0 and pq==0:   action='new_entry'; new_entry+=1; bought+=1
            elif cq==0 and pq>0: action='exit'; exited+=1; sold+=1
            elif cq>pq*1.02:     action='buy'; bought+=1
            elif cq<pq*0.98:     action='sell'; sold+=1
            else:                action='hold'; holding+=1

            chg = round(((cq-pq)/pq*100),1) if pq>0 else (100.0 if cq>0 else 0.0)
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
    logger.info(f"Aggregated: {len(stock_rows)} stocks, {len(amc_rows)} AMC records")
    return stock_rows, amc_rows, raw_rows

def save(sb, stock_rows, amc_rows, raw_rows, dm, source, n_families):
    B=50
    for i in range(0,len(stock_rows),B):
        sb.table("stock_intelligence").upsert(stock_rows[i:i+B],on_conflict="isin").execute()
    logger.info(f"✓ {len(stock_rows)} stocks saved")
    for i in range(0,len(amc_rows),B):
        sb.table("amc_holdings").upsert(amc_rows[i:i+B],on_conflict="isin,amc_name").execute()
    logger.info(f"✓ {len(amc_rows)} AMC records saved")
    for i in range(0,len(raw_rows),B):
        sb.table("amc_holdings_raw").upsert(raw_rows[i:i+B],on_conflict="isin,amc_name,data_month").execute()
    logger.info(f"✓ {len(raw_rows)} raw records saved")
    sb.table("scrape_meta").insert({
        "scraped_at":datetime.utcnow().isoformat(),"data_month":dm,
        "stocks_processed":len(stock_rows),"amcs_scraped":n_families,
        "notes":f"Source: {source}"
    }).execute()
    logger.info("✓ Meta saved")

def load_prev(sb, prev_month):
    try:
        r=sb.table("amc_holdings_raw").select("isin,amc_name,quantity").eq("data_month",prev_month).execute()
        prev=defaultdict(dict)
        for row in r.data: prev[row['isin']][row['amc_name']]=row['quantity']
        logger.info(f"Loaded {len(r.data)} prev month records")
        return dict(prev)
    except Exception as e:
        logger.warning(f"No prev month data: {e}")
        return {}

def run_scrape(year=None, month=None):
    dm = get_data_month()
    if year and month:
        dm = f"{year}-{month:02d}"
    pm = get_prev_month(dm)

    logger.info(f"=== FundLens Scrape: {dm} (prev: {pm}) ===")
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Step 1: Get families
    families = get_equity_families()
    if not families:
        logger.error("No families found — aborting")
        sb.table("scrape_meta").insert({"scraped_at":datetime.utcnow().isoformat(),
            "data_month":dm,"stocks_processed":0,"amcs_scraped":0,
            "notes":"FAILED: No families from mfdata.in"}).execute()
        return {"stocks":0,"month":dm}

    # Step 2: Fetch holdings
    curr = defaultdict(lambda: defaultdict(float))
    meta = {}
    n_fetched = 0

    for i, f in enumerate(families):
        amc = extract_amc(f['name'], f.get('amc',''))
        logger.info(f"[{i+1}/{len(families)}] {f['name'][:50]}")
        holdings = fetch_holdings(f['id'], amc, dm)

        if holdings:
            n_fetched += 1
            for h in holdings:
                curr[h['isin']][amc] += h['quantity']
                if h['isin'] not in meta:
                    meta[h['isin']] = {'name':h['name'],'sector':h['sector']}

        time.sleep(0.5)

    logger.info(f"Fetched {n_fetched} families, {len(curr)} unique stocks")

    # If no data, try previous month
    if len(curr) == 0:
        logger.warning(f"No data for {dm}, trying {pm}")
        for f in families[:50]:
            amc = extract_amc(f['name'], f.get('amc',''))
            holdings = fetch_holdings(f['id'], amc, pm)
            if holdings:
                n_fetched += 1
                for h in holdings:
                    curr[h['isin']][amc] += h['quantity']
                    if h['isin'] not in meta:
                        meta[h['isin']] = {'name':h['name'],'sector':h['sector']}
            time.sleep(0.5)
        if curr:
            dm = pm; pm = get_prev_month(dm)
            logger.info(f"Using {dm}: {len(curr)} stocks")

    # Step 3: Load prev from DB
    prev = load_prev(sb, pm)

    # Step 4: Aggregate
    stock_rows, amc_rows, raw_rows = aggregate(dict(curr), prev, meta, dm)

    # Step 5: Save
    save(sb, stock_rows, amc_rows, raw_rows, dm, "mfdata.in/families", n_fetched)

    result = {"stocks":len(stock_rows),"amc_records":len(amc_rows),
              "month":dm,"families_fetched":n_fetched}
    logger.info(f"=== Done: {result} ===")
    return result

if __name__ == "__main__":
    result = run_scrape()
    print(f"\n✅ Scrape complete: {result}")
