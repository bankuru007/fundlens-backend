"""
FundLens Scraper v4 — FinAPI (finapi.upvaly.com)
=================================================
Data source: https://finapi.upvaly.com/api/mf/scheme-code/{schemeCode}
Strategy:
  1. Discover scheme codes (probe list endpoints, else crawl via morefundsfromamc)
  2. Fetch holdings for each equity scheme
  3. Map scheme -> AMC, aggregate holdings per stock per AMC
  4. Snapshot to amc_holdings_raw, compare vs previous month -> signals
  5. Upsert stock_intelligence / amc_holdings / scrape_meta in Supabase

Diagnostics print the raw structure of the first holding so field-name
mismatches are visible immediately in the GitHub Actions log.
"""

import os, sys, json, time, logging, re
from datetime import datetime, timezone
from collections import defaultdict

import requests
from supabase import create_client

# ---------------------------------------------------------------- config
BASE = "https://finapi.upvaly.com"
SEED_CODES = ["120503", "152135"]           # confirmed working by user
MAX_SCHEMES = 800                            # safety cap
SLEEP = 0.25                                 # politeness delay (seconds)
TIMEOUT = 25
RETRIES = 2

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fundlens")

ERRORS, NOTES = [], []
def err(msg):  ERRORS.append(msg); log.error("✗ " + msg)
def note(msg): NOTES.append(msg);  log.info("→ " + msg)

# 44 AMC keyword map (schemeName prefix -> canonical AMC name)
AMC_KEYWORDS = {
    "axis":"Axis MF","hdfc":"HDFC MF","icici":"ICICI Prudential MF","sbi":"SBI MF",
    "kotak":"Kotak MF","nippon":"Nippon India MF","aditya":"Aditya Birla SL MF",
    "birla":"Aditya Birla SL MF","uti":"UTI MF","dsp":"DSP MF","tata":"Tata MF",
    "mirae":"Mirae Asset MF","invesco":"Invesco MF","franklin":"Franklin Templeton MF",
    "motilal":"Motilal Oswal MF","canara":"Canara Robeco MF","edelweiss":"Edelweiss MF",
    "lic":"LIC MF","sundaram":"Sundaram MF","quant ":"Quant MF","quantum":"Quantum MF",
    "ppfas":"PPFAS MF","parag":"PPFAS MF","bandhan":"Bandhan MF","idfc":"Bandhan MF",
    "hsbc":"HSBC MF","baroda":"Baroda BNP Paribas MF","bnp":"Baroda BNP Paribas MF",
    "union":"Union MF","mahindra":"Mahindra Manulife MF","pgim":"PGIM India MF",
    "jm ":"JM Financial MF","iti ":"ITI MF","navi":"Navi MF","whiteoak":"WhiteOak MF",
    "white oak":"WhiteOak MF","samco":"Samco MF","trust":"Trust MF","nj ":"NJ MF",
    "shriram":"Shriram MF","bajaj":"Bajaj Finserv MF","helios":"Helios MF",
    "zerodha":"Zerodha MF","groww":"Groww MF","360":"360 ONE MF","bank of india":"Bank of India MF",
    "boi":"Bank of India MF","taurus":"Taurus MF","old bridge":"Old Bridge MF",
}

EQUITY_HINTS = ("equity","elss","flexi","large","mid","small","multi","value",
                "focused","contra","dividend yield","sectoral","thematic","index")
DEBT_HINTS = ("debt","liquid","overnight","gilt","money market","bond","credit",
              "duration","treasury","banking & psu","floater","fmp","arbitrage")

# ---------------------------------------------------------------- http
def get(url, params=None):
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT,
                             headers={"User-Agent":"FundLens/1.0"})
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            log.warning(f"HTTP {r.status_code} {url} (attempt {attempt+1})")
        except Exception as e:
            log.warning(f"{type(e).__name__} {url} (attempt {attempt+1})")
        time.sleep(1 + attempt)
    return None

def unwrap(payload):
    """FinAPI wraps responses in {status,statusCode,message,data}."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload

# ---------------------------------------------------------------- field detection
def pick(d, *candidates, contains=None):
    """Return first matching key's value from dict d (case-insensitive)."""
    if not isinstance(d, dict):
        return None
    lower = {k.lower(): v for k, v in d.items()}
    for c in candidates:
        if c.lower() in lower:
            return lower[c.lower()]
    if contains:
        for k, v in lower.items():
            if contains in k:
                return v
    return None

def to_float(x):
    if x is None: return None
    try:
        return float(str(x).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None

def amc_from_scheme(name):
    n = (name or "").lower()
    for kw, amc in AMC_KEYWORDS.items():
        if n.startswith(kw) or f" {kw}" in n[:30]:
            return amc
    return None

def looks_equity(category, name):
    blob = f"{category or ''} {name or ''}".lower()
    if any(h in blob for h in DEBT_HINTS):   return False
    if any(h in blob for h in EQUITY_HINTS): return True
    return None  # unknown -> decide by holdings content

# ---------------------------------------------------------------- discovery
def discover_schemes():
    """Try list endpoints first; fall back to crawling morefundsfromamc."""
    note("Probing list endpoints...")
    for path in ("/api/mf/schemes", "/api/mf/all", "/api/mf/list", "/api/mf"):
        data = unwrap(get(BASE + path))
        if isinstance(data, list) and len(data) > 50:
            codes = []
            for item in data:
                c = pick(item, "schemeCode", "scheme_code", "code")
                if c: codes.append(str(c))
            if codes:
                note(f"List endpoint {path} -> {len(codes)} schemes")
                return codes[:MAX_SCHEMES]
    note("No list endpoint. Crawling via morefundsfromamc from seeds...")
    seen, queue, order = set(), list(SEED_CODES), []
    while queue and len(seen) < MAX_SCHEMES:
        code = queue.pop(0)
        if code in seen: continue
        seen.add(code); order.append(code)
        data = unwrap(get(f"{BASE}/api/mf/scheme-code/{code}",
                          params={"fields":"morefundsfromamc"}))
        more = pick(data or {}, "morefundsfromamc", contains="morefunds") or []
        if isinstance(more, dict): more = more.get("funds") or more.get("schemes") or []
        for f in more if isinstance(more, list) else []:
            c = pick(f, "schemeCode", "scheme_code", "code") if isinstance(f, dict) else f
            if c and str(c) not in seen:
                queue.append(str(c))
        time.sleep(SLEEP)
    note(f"Crawl discovered {len(order)} schemes")
    return order

# ---------------------------------------------------------------- holdings fetch
def fetch_holdings(code, debug_first=[True]):
    data = unwrap(get(f"{BASE}/api/mf/scheme-code/{code}",
                      params={"fields":"holdings"}))
    if not data: return None
    name = pick(data, "schemeName", "scheme_name", "name") or ""
    category = pick(data, "schemeCategory", "category") or ""
    holdings = pick(data, "holdings", contains="holding") or []
    if isinstance(holdings, dict):
        holdings = holdings.get("holdings") or holdings.get("data") or []
    if debug_first[0] and holdings:
        debug_first[0] = False
        note("RAW first holding structure: " + json.dumps(holdings[0])[:400])
    rows = []
    for h in holdings if isinstance(holdings, list) else []:
        stock = pick(h, "companyName","company","name","stockName","security","instrument")
        isin  = pick(h, "isin","isinCode","isin_code")
        pct   = to_float(pick(h, "percentage","weight","pct","holdingPercent",
                              "corpusPer","weightage", contains="per"))
        qty   = to_float(pick(h, "quantity","qty","shares","noOfShares", contains="quant"))
        sector= pick(h, "sector","industry","sectorName") or "Unknown"
        if not stock: continue
        if isin and not str(isin).upper().startswith(("INE","INF9","IN9")):
            pass  # keep — some valid ISINs differ
        rows.append({"stock":str(stock).strip(),"isin":(str(isin).strip().upper() if isin else None),
                     "pct":pct,"qty":qty,"sector":str(sector).strip()})
    return {"scheme":name,"category":category,"holdings":rows}

# ---------------------------------------------------------------- main pipeline
def run():
    note("=== FundLens FinAPI Scraper v4 ===")
    now = datetime.now(timezone.utc)
    data_month = now.strftime("%Y-%m")
    note(f"Run date {now.isoformat()}  data_month {data_month}")

    if not SUPABASE_URL or not SUPABASE_KEY:
        err("SUPABASE_URL / SUPABASE_SERVICE_KEY env vars missing"); return finish(0, data_month)
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # connectivity check
    probe = unwrap(get(f"{BASE}/api/mf/scheme-code/{SEED_CODES[0]}",
                       params={"fields":"holdings"}))
    if not probe:
        err(f"FinAPI unreachable from this runner ({BASE}). "
            "Check if the host blocks GitHub IPs."); return finish(0, data_month)
    note("FinAPI reachable ✓")

    codes = discover_schemes()
    if not codes:
        err("No schemes discovered"); return finish(0, data_month)

    # stock -> { isin, sector, amcs: {amc: weight} }
    stocks = {}
    amc_stock_pct = defaultdict(dict)     # (amc) -> {stock_key: pct}
    schemes_done, skipped_nonequity, skipped_noamc = 0, 0, 0

    for i, code in enumerate(codes):
        res = fetch_holdings(code)
        time.sleep(SLEEP)
        if not res or not res["holdings"]:
            continue
        eq = looks_equity(res["category"], res["scheme"])
        ine_share = sum(1 for h in res["holdings"] if h["isin"] and h["isin"].startswith("INE"))
        if eq is False or (eq is None and ine_share < max(3, len(res["holdings"])//4)):
            skipped_nonequity += 1; continue
        amc = amc_from_scheme(res["scheme"])
        if not amc:
            skipped_noamc += 1; continue
        schemes_done += 1
        for h in res["holdings"]:
            key = h["isin"] or ("NAME:" + h["stock"].upper())
            s = stocks.setdefault(key, {"name":h["stock"],"isin":h["isin"],
                                        "sector":h["sector"],"amcs":set()})
            if h["sector"] != "Unknown": s["sector"] = h["sector"]
            s["amcs"].add(amc)
            prev = amc_stock_pct[amc].get(key, 0.0)
            amc_stock_pct[amc][key] = max(prev, h["pct"] or 0.0)
        if (i+1) % 50 == 0:
            note(f"...{i+1}/{len(codes)} schemes, {len(stocks)} stocks so far")

    note(f"Equity schemes processed {schemes_done} | non-equity skipped {skipped_nonequity} "
         f"| unknown AMC skipped {skipped_noamc} | unique stocks {len(stocks)}")
    if not stocks:
        err("0 stocks parsed — check RAW structure log above for field names")
        return finish(0, data_month)

    # ---------------- previous month snapshot from DB
    prev = {}
    try:
        res = sb.table("amc_holdings_raw").select("isin,amc_name,quantity,data_month")\
                .neq("data_month", data_month).order("data_month", desc=True).limit(50000).execute()
        rows = res.data or []
        prev_month = rows[0]["data_month"] if rows else None
        for r in rows:
            if r["data_month"] == prev_month:
                prev[(r["isin"], r["amc_name"])] = r["quantity"] or 0
        note(f"Previous snapshot: {prev_month} ({len(prev)} rows)" if prev_month
             else "No previous snapshot — first run baseline")
    except Exception as e:
        err(f"Reading previous snapshot failed: {e}")

    # ---------------- build rows
    raw_rows, amc_rows, intel_rows = [], [], []
    for key, s in stocks.items():
        isin = s["isin"] or key
        bought=sold=new_entry=exited=holding=0
        for amc in s["amcs"]:
            cur = int(round((amc_stock_pct[amc].get(key) or 0)*100))  # pct*100 as int qty proxy
            p = prev.get((isin, amc))
            if p is None and prev:        action,new_entry = "new_entry",new_entry+1
            elif p is None:               action,holding   = "hold",holding+1
            elif cur > p*1.05:            action,bought    = "buy",bought+1
            elif cur < p*0.95:            action,sold      = "sell",sold+1
            else:                         action,holding   = "hold",holding+1
            raw_rows.append({"isin":isin,"amc_name":amc,"quantity":cur,"data_month":data_month})
            amc_rows.append({"isin":isin,"amc_name":amc,"action":action,
                             "curr_qty":cur,"prev_qty":p or 0,
                             "change_pct":round(((cur-(p or 0))/p*100),2) if p else None})
        # exits: AMCs present last month, absent now
        for (pisin, pamc), pq in prev.items():
            if pisin == isin and pamc not in s["amcs"]:
                exited += 1
                amc_rows.append({"isin":isin,"amc_name":pamc,"action":"exit",
                                 "curr_qty":0,"prev_qty":pq,"change_pct":-100.0})
        total = len(s["amcs"])
        if   exited >= 3 and exited > bought:        signal="exit"
        elif new_entry >= 3 and new_entry >= bought: signal="new"
        elif bought > sold*1.5 and bought >= 3:      signal="buy"
        elif sold > bought*1.5 and sold >= 3:        signal="sell"
        else:                                        signal="hold"
        intel_rows.append({"isin":isin,"name":s["name"],"sector":s["sector"],
                           "signal":signal,"total_amcs":total,"bought":bought,"sold":sold,
                           "holding":holding,"new_entry":new_entry,"exited":exited,
                           "data_month":data_month})

    # ---------------- upserts
    def chunked(rows, table, conflict):
        for j in range(0, len(rows), 500):
            sb.table(table).upsert(rows[j:j+500], on_conflict=conflict).execute()
    try:
        chunked(intel_rows, "stock_intelligence", "isin")
        chunked(raw_rows,   "amc_holdings_raw",   "isin,amc_name,data_month")
        chunked(amc_rows,   "amc_holdings",       "isin,amc_name")
        note(f"Upserted: {len(intel_rows)} stocks, {len(amc_rows)} AMC rows, {len(raw_rows)} raw rows")
    except Exception as e:
        err(f"Supabase upsert failed: {e}")
        return finish(0, data_month)
    return finish(len(intel_rows), data_month, sb)

def finish(count, data_month, sb=None):
    summary = {"stocks":count}
    if ERRORS: summary["error"] = ERRORS
    if sb:
        try:
            sb.table("scrape_meta").insert({
                "scraped_at":datetime.now(timezone.utc).isoformat(),
                "data_month":data_month,"stocks_processed":count,
                "notes":"finapi v4 | " + ("; ".join(ERRORS) if ERRORS else "ok")}).execute()
        except Exception as e:
            log.warning(f"scrape_meta insert failed: {e}")
    print("\n" + "="*60 + "\nDIAGNOSTIC SUMMARY\n" + "="*60)
    for n in NOTES:  print("→ " + n)
    if ERRORS:
        print("\nERRORS:")
        for e in ERRORS: print("✗ " + e)
    print("="*60)
    print(("✅" if count and not ERRORS else "❌") + f" Result: {summary}")
    return summary

if __name__ == "__main__":
    run()
