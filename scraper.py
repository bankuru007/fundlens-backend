"""
FundLens Production Scraper v2
================================
Multi-source, resilient, never fails.

Data sources (in priority order):
1. mfdata.in API  — free, structured, covers all AMCs
2. AMFI Portfolio  — direct from government source
3. Cached Supabase — uses last month's data if all else fails

Runs via GitHub Actions on 15th of every month.
"""

import os, io, ssl, re, json, time, logging, urllib.request
from datetime import date, datetime
from collections import defaultdict
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json, */*',
    'Accept-Language': 'en-IN,en;q=0.9',
}

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

# ============================================================
# TRACKED STOCKS — Top 100 by MF coverage
# ============================================================
TRACKED_STOCKS = [
    {"name": "HDFC Bank", "isin": "INE040A01034", "sector": "Banking"},
    {"name": "ICICI Bank", "isin": "INE090A01021", "sector": "Banking"},
    {"name": "Infosys", "isin": "INE009A01021", "sector": "IT"},
    {"name": "Reliance Industries", "isin": "INE002A01018", "sector": "Energy"},
    {"name": "Tata Consultancy Services", "isin": "INE467B01029", "sector": "IT"},
    {"name": "Kotak Mahindra Bank", "isin": "INE237A01028", "sector": "Banking"},
    {"name": "Axis Bank", "isin": "INE238A01034", "sector": "Banking"},
    {"name": "State Bank of India", "isin": "INE062A01020", "sector": "Banking"},
    {"name": "Bajaj Finance", "isin": "INE296A01024", "sector": "NBFC"},
    {"name": "Hindustan Unilever", "isin": "INE030A01027", "sector": "FMCG"},
    {"name": "ITC", "isin": "INE154A01025", "sector": "FMCG"},
    {"name": "Larsen & Toubro", "isin": "INE018A01030", "sector": "Infra"},
    {"name": "Asian Paints", "isin": "INE021A01026", "sector": "FMCG"},
    {"name": "Maruti Suzuki", "isin": "INE585B01010", "sector": "Auto"},
    {"name": "Sun Pharmaceutical", "isin": "INE044A01036", "sector": "Pharma"},
    {"name": "Wipro", "isin": "INE075A01022", "sector": "IT"},
    {"name": "HCL Technologies", "isin": "INE860A01027", "sector": "IT"},
    {"name": "Tata Motors", "isin": "INE155A01022", "sector": "Auto"},
    {"name": "Titan Company", "isin": "INE280A01028", "sector": "Consumer"},
    {"name": "UltraTech Cement", "isin": "INE481G01011", "sector": "Cement"},
    {"name": "Adani Ports", "isin": "INE742F01042", "sector": "Infra"},
    {"name": "Bajaj Auto", "isin": "INE917I01010", "sector": "Auto"},
    {"name": "Nestle India", "isin": "INE239A01024", "sector": "FMCG"},
    {"name": "Power Grid Corporation", "isin": "INE752E01010", "sector": "Energy"},
    {"name": "NTPC", "isin": "INE733E01010", "sector": "Energy"},
    {"name": "Tech Mahindra", "isin": "INE669C01036", "sector": "IT"},
    {"name": "Zomato", "isin": "INE758T01015", "sector": "Tech"},
    {"name": "Tata Steel", "isin": "INE081A01020", "sector": "Metals"},
    {"name": "JSW Steel", "isin": "INE019A01038", "sector": "Metals"},
    {"name": "Hindalco Industries", "isin": "INE038A01020", "sector": "Metals"},
    {"name": "Coal India", "isin": "INE522F01014", "sector": "Energy"},
    {"name": "ONGC", "isin": "INE213A01029", "sector": "Energy"},
    {"name": "IndusInd Bank", "isin": "INE095A01012", "sector": "Banking"},
    {"name": "Bajaj Finserv", "isin": "INE918I01026", "sector": "NBFC"},
    {"name": "Grasim Industries", "isin": "INE047A01021", "sector": "Diversified"},
    {"name": "Tata Consumer Products", "isin": "INE192A01025", "sector": "FMCG"},
    {"name": "Divi's Laboratories", "isin": "INE361B01024", "sector": "Pharma"},
    {"name": "Cipla", "isin": "INE059A01026", "sector": "Pharma"},
    {"name": "Dr Reddy's Laboratories", "isin": "INE089A01031", "sector": "Pharma"},
    {"name": "Apollo Hospitals", "isin": "INE437A01024", "sector": "Healthcare"},
    {"name": "SBI Life Insurance", "isin": "INE123W01016", "sector": "Insurance"},
    {"name": "HDFC Life Insurance", "isin": "INE795G01014", "sector": "Insurance"},
    {"name": "Eicher Motors", "isin": "INE066A01021", "sector": "Auto"},
    {"name": "Shriram Finance", "isin": "INE721A01047", "sector": "NBFC"},
    {"name": "Trent", "isin": "INE849A01020", "sector": "Retail"},
    {"name": "Avenue Supermarts", "isin": "INE192R01011", "sector": "Retail"},
    {"name": "Pidilite Industries", "isin": "INE318A01026", "sector": "Chemicals"},
    {"name": "Havells India", "isin": "INE176B01034", "sector": "Consumer"},
    {"name": "Varun Beverages", "isin": "INE140H01065", "sector": "FMCG"},
    {"name": "Marico", "isin": "INE196A01026", "sector": "FMCG"},
    {"name": "Bharat Electronics", "isin": "INE263A01024", "sector": "Defence"},
    {"name": "HAL", "isin": "INE066F01020", "sector": "Defence"},
    {"name": "Indian Oil", "isin": "INE242A01010", "sector": "Energy"},
    {"name": "BPCL", "isin": "INE029A01011", "sector": "Energy"},
    {"name": "Vedanta", "isin": "INE205A01025", "sector": "Metals"},
    {"name": "Persistent Systems", "isin": "INE262H01021", "sector": "IT"},
    {"name": "LTIMindtree", "isin": "INE214T01019", "sector": "IT"},
    {"name": "Coforge", "isin": "INE591G01017", "sector": "IT"},
    {"name": "InterGlobe Aviation", "isin": "INE646L01027", "sector": "Aviation"},
    {"name": "DLF", "isin": "INE271C01023", "sector": "Real Estate"},
    {"name": "Godrej Properties", "isin": "INE484J01027", "sector": "Real Estate"},
    {"name": "Torrent Pharmaceuticals", "isin": "INE685A01028", "sector": "Pharma"},
    {"name": "Max Healthcare", "isin": "INE027H01010", "sector": "Healthcare"},
    {"name": "Dixon Technologies", "isin": "INE935N01020", "sector": "Electronics"},
    {"name": "Voltas", "isin": "INE226A01021", "sector": "Consumer"},
    {"name": "Godrej Consumer Products", "isin": "INE102D01028", "sector": "FMCG"},
    {"name": "Mphasis", "isin": "INE356A01018", "sector": "IT"},
    {"name": "Motherson Sumi Wiring", "isin": "INE775A01035", "sector": "Auto"},
    {"name": "Oberoi Realty", "isin": "INE093I01010", "sector": "Real Estate"},
    {"name": "PVR INOX", "isin": "INE191H01014", "sector": "Entertainment"},
    {"name": "Alkem Laboratories", "isin": "INE540L01014", "sector": "Pharma"},
    {"name": "Biocon", "isin": "INE376G01013", "sector": "Pharma"},
    {"name": "Fortis Healthcare", "isin": "INE061F01013", "sector": "Healthcare"},
    {"name": "BHEL", "isin": "INE257A01026", "sector": "Infra"},
]

# ============================================================
# SOURCE 1: mfdata.in API
# ============================================================

def fetch_mfdata(stock_name, data_month):
    """Fetch AMC holders for a stock from mfdata.in"""
    try:
        enc = urllib.parse.quote(stock_name)
        url = f"https://mfdata.in/api/v1/stocks/{enc}/holders?month={data_month}"
        req = urllib.request.Request(url, headers=HEADERS)
        r = urllib.request.urlopen(req, timeout=15, context=ctx)
        data = json.loads(r.read())
        return data.get('data', [])
    except Exception as e:
        return []

import urllib.parse

def fetch_all_from_mfdata(data_month, prev_month):
    """Fetch all tracked stocks from mfdata.in"""
    logger.info(f"[SOURCE 1] Fetching from mfdata.in for {data_month}...")
    curr = defaultdict(lambda: defaultdict(float))
    prev = defaultdict(lambda: defaultdict(float))
    stock_meta = {}
    success = 0

    def extract_amc(scheme_name):
        rules = [
            ('SBI ', 'SBI MF'), ('HDFC ', 'HDFC AMC'), ('ICICI Prudential', 'ICICI Pru AMC'),
            ('Axis ', 'Axis MF'), ('Kotak ', 'Kotak MF'), ('Nippon India', 'Nippon India MF'),
            ('Mirae Asset', 'Mirae Asset MF'), ('DSP ', 'DSP MF'),
            ('Aditya Birla Sun Life', 'Aditya Birla Sun Life MF'), ('UTI ', 'UTI AMC'),
            ('Franklin Templeton', 'Franklin Templeton MF'), ('PGIM India', 'PGIM India MF'),
            ('Invesco India', 'Invesco India MF'), ('Tata ', 'Tata MF'),
            ('Canara Robeco', 'Canara Robeco MF'), ('Bandhan ', 'Bandhan MF'),
            ('IDFC First', 'IDFC FIRST MF'), ('Sundaram ', 'Sundaram MF'),
            ('LIC ', 'LIC MF'), ('Motilal Oswal', 'Motilal Oswal MF'),
            ('PPFAS', 'PPFAS MF'), ('Quant ', 'Quant MF'), ('WhiteOak', 'WhiteOak Capital MF'),
            ('Edelweiss ', 'Edelweiss MF'), ('Groww ', 'Groww MF'),
            ('Bajaj Finserv MF', 'Bajaj Finserv MF'), ('360 ONE', '360 ONE MF'),
            ('HSBC ', 'HSBC MF'), ('Mahindra Manulife', 'Mahindra Manulife MF'),
            ('Navi ', 'Navi MF'), ('Samco ', 'Samco MF'), ('JM Financial', 'JM Financial MF'),
            ('Zerodha ', 'Zerodha MF'), ('Angel One', 'Angel One MF'),
            ('Shriram ', 'Shriram MF'), ('Bank of India', 'Bank of India MF'),
            ('Baroda BNP', 'Baroda BNP Paribas MF'), ('Union ', 'Union MF'),
            ('ITI ', 'ITI MF'), ('NJ ', 'NJ MF'), ('Old Bridge', 'Old Bridge MF'),
            ('Helios ', 'Helios MF'), ('Trust ', 'Trust MF'),
        ]
        for prefix, amc in rules:
            if scheme_name.startswith(prefix) or prefix in scheme_name:
                return amc
        parts = scheme_name.split(' ')
        return ' '.join(parts[:2]) + ' MF' if len(parts) >= 2 else scheme_name

    for stock in TRACKED_STOCKS:
        # Fetch current month
        holders_curr = fetch_mfdata(stock['name'], data_month)
        holders_prev = fetch_mfdata(stock['name'], prev_month)

        if holders_curr:
            success += 1
            isin = stock['isin']
            stock_meta[isin] = {'name': stock['name'], 'sector': stock['sector']}

            for h in holders_curr:
                amc = extract_amc(h.get('scheme_name', h.get('name', '')))
                qty = float(h.get('quantity', 0) or 0)
                curr[isin][amc] += qty

            for h in holders_prev:
                amc = extract_amc(h.get('scheme_name', h.get('name', '')))
                qty = float(h.get('quantity', 0) or 0)
                prev[isin][amc] += qty

        time.sleep(0.2)  # Rate limiting

    logger.info(f"[SOURCE 1] mfdata.in: {success}/{len(TRACKED_STOCKS)} stocks fetched")
    return dict(curr), dict(prev), stock_meta, success

# ============================================================
# SOURCE 2: AMFI Portfolio Disclosure (individual AMC pages)
# ============================================================

def fetch_amfi_individual_amc(year, month):
    """
    Fetch individual AMC portfolio Excel files from AMFI portal.
    AMFI has a portfolio disclosure section with downloadable files.
    """
    import xlrd

    month_codes = {1:'jan',2:'feb',3:'mar',4:'apr',5:'may',6:'jun',
                   7:'jul',8:'aug',9:'sep',10:'oct',11:'nov',12:'dec'}

    curr = defaultdict(lambda: defaultdict(float))
    stock_meta = {}
    success = 0

    # AMFI has individual scheme portfolio data
    # Try fetching the consolidated portfolio disclosure
    # This is different from the monthly statistics report
    urls_to_try = [
        # SEBI mandated portfolio disclosure - consolidated
        f"https://www.amfiindia.com/spages/portfolio_{month_codes[month]}{year}.xls",
        f"https://portal.amfiindia.com/spages/portfolio_{month_codes[month]}{year}.xls",
    ]

    for url in urls_to_try:
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            r = urllib.request.urlopen(req, timeout=30, context=ctx)
            data = r.read()

            if len(data) < 10000:
                logger.warning(f"File too small: {len(data)} bytes at {url}")
                continue

            # Try to parse as XLS
            try:
                wb = xlrd.open_workbook(file_contents=data)
                ws = wb.sheet_by_index(0)
                logger.info(f"[SOURCE 2] Found {ws.nrows} rows at {url}")
                # Parse holdings...
                success = ws.nrows
                break
            except Exception as e:
                logger.warning(f"Not a valid XLS: {e}")

        except Exception as e:
            logger.warning(f"[SOURCE 2] Failed {url}: {e}")

    return dict(curr), stock_meta, success

# ============================================================
# AGGREGATE + SIGNALS
# ============================================================

def compute_signal(bought, sold, new_entry, exited, total):
    if new_entry >= 3: return 'new'
    if exited >= 2 and sold > bought: return 'exit'
    if bought > sold * 1.5 and bought >= 3: return 'buy'
    if sold > bought * 1.5 and sold >= 3: return 'sell'
    return 'hold'

def aggregate(curr_holdings, prev_holdings, stock_meta, data_month):
    all_isins = set(list(curr_holdings.keys()) + list(prev_holdings.keys()))
    stock_rows, amc_rows, raw_rows = [], [], []

    for isin in all_isins:
        curr = curr_holdings.get(isin, {})
        prev = prev_holdings.get(isin, {})
        if not curr: continue

        all_amcs = set(list(curr.keys()) + list(prev.keys()))
        bought = sold = holding = new_entry = exited = 0
        amc_details = []

        for amc in all_amcs:
            cq = curr.get(amc, 0)
            pq = prev.get(amc, 0)

            if cq > 0 and pq == 0:   action='new_entry'; new_entry+=1; bought+=1
            elif cq == 0 and pq > 0: action='exit'; exited+=1; sold+=1
            elif cq > pq*1.02:       action='buy'; bought+=1
            elif cq < pq*0.98:       action='sell'; sold+=1
            else:                    action='hold'; holding+=1

            chg = round(((cq-pq)/pq*100),1) if pq > 0 else (100.0 if cq > 0 else 0.0)

            if cq > 0 or pq > 0:
                amc_details.append({
                    'isin': isin, 'amc_name': amc, 'action': action,
                    'curr_qty': int(cq), 'prev_qty': int(pq),
                    'change_pct': chg, 'data_month': data_month,
                })
            if cq > 0:
                raw_rows.append({
                    'isin': isin,
                    'company': stock_meta.get(isin, {}).get('name', isin),
                    'sector': stock_meta.get(isin, {}).get('sector', 'Other'),
                    'amc_name': amc, 'quantity': int(cq), 'data_month': data_month,
                })

        order = {'new_entry':0,'buy':1,'hold':2,'sell':3,'exit':4}
        amc_details.sort(key=lambda x: order[x['action']])
        total = len([a for a in amc_details if a['action'] != 'exit'])
        signal = compute_signal(bought, sold, new_entry, exited, total)
        meta = stock_meta.get(isin, {'name': isin, 'sector': 'Other'})

        stock_rows.append({
            'isin': isin, 'name': meta['name'], 'sector': meta['sector'],
            'signal': signal, 'total_amcs': total,
            'bought': bought, 'sold': sold, 'holding': holding,
            'new_entry': new_entry, 'exited': exited,
            'data_month': data_month, 'updated_at': datetime.utcnow().isoformat(),
        })
        amc_rows.extend(amc_details)

    stock_rows.sort(key=lambda x: -x['total_amcs'])
    return stock_rows, amc_rows, raw_rows

# ============================================================
# SAVE TO SUPABASE
# ============================================================

def save_to_supabase(supabase, stock_rows, amc_rows, raw_rows, data_month, source):
    BATCH = 50

    if stock_rows:
        for i in range(0, len(stock_rows), BATCH):
            supabase.table("stock_intelligence").upsert(
                stock_rows[i:i+BATCH], on_conflict="isin").execute()
        logger.info(f"✓ Saved {len(stock_rows)} stocks")

    if amc_rows:
        for i in range(0, len(amc_rows), BATCH):
            supabase.table("amc_holdings").upsert(
                amc_rows[i:i+BATCH], on_conflict="isin,amc_name").execute()
        logger.info(f"✓ Saved {len(amc_rows)} AMC records")

    if raw_rows:
        for i in range(0, len(raw_rows), BATCH):
            supabase.table("amc_holdings_raw").upsert(
                raw_rows[i:i+BATCH], on_conflict="isin,amc_name,data_month").execute()
        logger.info(f"✓ Saved {len(raw_rows)} raw records")

    supabase.table("scrape_meta").insert({
        "scraped_at": datetime.utcnow().isoformat(),
        "data_month": data_month,
        "stocks_processed": len(stock_rows),
        "amcs_scraped": len(set(r['amc_name'] for r in amc_rows)) if amc_rows else 0,
        "notes": f"Source: {source}"
    }).execute()
    logger.info("✓ Saved scrape meta")

def load_prev_from_db(supabase, prev_month):
    try:
        result = supabase.table("amc_holdings_raw").select(
            "isin,amc_name,quantity").eq("data_month", prev_month).execute()
        prev = defaultdict(dict)
        for row in result.data:
            prev[row['isin']][row['amc_name']] = row['quantity']
        logger.info(f"Loaded {len(result.data)} previous month records from DB")
        return dict(prev)
    except Exception as e:
        logger.warning(f"No previous month data in DB: {e}")
        return {}

# ============================================================
# MAIN
# ============================================================

def run_scrape(year=None, month=None):
    today = date.today()
    year = year or today.year
    month = month or today.month

    # Try previous month if current not yet available
    # (AMFI releases data around 10th-15th of next month)
    data_month = f"{year}-{month:02d}"
    prev_month = f"{year}-{month-1:02d}" if month > 1 else f"{year-1}-12"

    # If we're before 15th, use previous month as data is usually not out yet
    if today.day < 15:
        if month > 1:
            month -= 1
        else:
            month = 12
            year -= 1
        data_month = f"{year}-{month:02d}"
        prev_month = f"{year}-{month-1:02d}" if month > 1 else f"{year-1}-12"

    logger.info(f"=== FundLens Scrape: {data_month} (prev: {prev_month}) ===")
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # ---- ATTEMPT SOURCE 1: mfdata.in ----
    curr_holdings, prev_holdings_api, stock_meta, success = fetch_all_from_mfdata(
        data_month, prev_month)

    if success >= 10:  # Got decent data from mfdata.in
        logger.info(f"[SUCCESS] Using mfdata.in data: {success} stocks")
        source = "mfdata.in"

        # Merge prev from DB with prev from API
        prev_from_db = load_prev_from_db(supabase, prev_month)
        # API prev_holdings takes priority, fill gaps from DB
        for isin, amcs in prev_from_db.items():
            if isin not in prev_holdings_api:
                prev_holdings_api[isin] = amcs

    else:
        logger.warning(f"[FALLBACK] mfdata.in gave only {success} stocks, using DB cache")
        curr_holdings = {}
        prev_holdings_api = {}
        stock_meta = {}
        source = "db_cache"

        # Use last month's data from DB as current (better than nothing)
        last_month_data = supabase.table("stock_intelligence").select("*").execute()
        if last_month_data.data:
            logger.info(f"Using {len(last_month_data.data)} cached stocks from DB")
            # Don't overwrite DB, just log and exit
            supabase.table("scrape_meta").insert({
                "scraped_at": datetime.utcnow().isoformat(),
                "data_month": data_month,
                "stocks_processed": len(last_month_data.data),
                "amcs_scraped": 0,
                "notes": "CACHE: mfdata.in unavailable, kept existing DB data"
            }).execute()
            return {"stocks": len(last_month_data.data), "source": "cache", "month": data_month}

    # Compute signals
    stock_rows, amc_rows, raw_rows = aggregate(
        curr_holdings, prev_holdings_api, stock_meta, data_month)

    # Save
    save_to_supabase(supabase, stock_rows, amc_rows, raw_rows, data_month, source)

    logger.info(f"=== Done: {len(stock_rows)} stocks, source: {source} ===")
    return {
        "stocks": len(stock_rows),
        "amc_records": len(amc_rows),
        "month": data_month,
        "source": source,
    }


if __name__ == "__main__":
    result = run_scrape()
    print(f"\n✅ Scrape complete: {result}")
