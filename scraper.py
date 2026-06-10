"""
FundLens Scraper — FinAPI (finapi.upvaly.com)
=============================================
Self-contained, DATABASE-FREE. Exposes:

    run_scrape(year=None, month=None) -> dict
    debug_probe(family_id=None, month=None) -> dict   (diagnostics for /api/debug/source)

Returns the exact shape data_store.refresh() expects:
    {
      "status": "ok" | "unavailable",
      "stocks": [ {isin,name,sector,signal,total_amcs,bought,sold,holding,new_entry,exited,data_month}, ... ],
      "amc_details": { isin: [ {amc_name,action,curr_qty,prev_qty,change_pct}, ... ] },
      "data_month": "YYYY-MM",
      "source": "finapi.upvaly.com",
      "fetched_at": ISO8601,
      "diagnostics": [ ... ],
    }

Only dependency: requests. No Supabase, no DB.
"""

import os, json, time, logging
from datetime import datetime, timezone
from collections import defaultdict

import requests

log = logging.getLogger("fundlens.scraper")

BASE = "https://finapi.upvaly.com"
SEED_CODES = ["120503", "152135"]
MAX_SCHEMES = int(os.environ.get("FUNDLENS_MAX_SCHEMES", "600"))
SLEEP = float(os.environ.get("FUNDLENS_SLEEP", "0.2"))
TIMEOUT = 25
RETRIES = 2

PREVIOUS_SNAPSHOT = {}

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
    "zerodha":"Zerodha MF","groww":"Groww MF","360":"360 ONE MF",
    "bank of india":"Bank of India MF","boi":"Bank of India MF","taurus":"Taurus MF",
    "old bridge":"Old Bridge MF",
}

EQUITY_HINTS = ("equity","elss","flexi","large","mid","small","multi","value",
                "focused","contra","dividend yield","sectoral","thematic","index",
                "bluechip","opportunities","special")
DEBT_HINTS = ("debt","liquid","overnight","gilt","money market","bond","credit",
              "duration","treasury","banking & psu","floater","fmp","corporate bond")


def _get(url, params=None):
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT,
                             headers={"User-Agent": "FundLens/1.0"})
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            log.warning("HTTP %s %s (try %s)", r.status_code, url, attempt + 1)
        except Exception as e:
            log.warning("%s %s (try %s)", type(e).__name__, url, attempt + 1)
        time.sleep(1 + attempt)
    return None


def _unwrap(payload):
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _pick(d, *candidates, contains=None):
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


def _to_float(x):
    if x is None:
        return None
    try:
        return float(str(x).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def _amc_from_scheme(name):
    n = (name or "").lower()
    for kw, amc in AMC_KEYWORDS.items():
        if n.startswith(kw) or f" {kw}" in n[:32]:
            return amc
    return None


def _looks_equity(category, name):
    blob = f"{category or ''} {name or ''}".lower()
    if any(h in blob for h in DEBT_HINTS):
        return False
    if any(h in blob for h in EQUITY_HINTS):
        return True
    return None


def _discover_schemes(diag):
    for path in ("/api/mf/schemes", "/api/mf/all", "/api/mf/list", "/api/mf"):
        data = _unwrap(_get(BASE + path))
        if isinstance(data, list) and len(data) > 50:
            codes = [str(_pick(it, "schemeCode", "scheme_code", "code"))
                     for it in data if _pick(it, "schemeCode", "scheme_code", "code")]
            if codes:
                diag.append(f"List endpoint {path} -> {len(codes)} schemes")
                return codes[:MAX_SCHEMES]
    diag.append("No list endpoint; crawling via morefundsfromamc from seeds")
    seen, queue, order = set(), list(SEED_CODES), []
    while queue and len(seen) < MAX_SCHEMES:
        code = queue.pop(0)
        if code in seen:
            continue
        seen.add(code); order.append(code)
        data = _unwrap(_get(f"{BASE}/api/mf/scheme-code/{code}",
                            params={"fields": "morefundsfromamc"}))
        more = _pick(data or {}, "morefundsfromamc", contains="morefunds") or []
        if isinstance(more, dict):
            more = more.get("funds") or more.get("schemes") or list(more.values())
        for f in more if isinstance(more, list) else []:
            c = _pick(f, "schemeCode", "scheme_code", "code") if isinstance(f, dict) else f
            if c and str(c) not in seen:
                queue.append(str(c))
        time.sleep(SLEEP)
    diag.append(f"Crawl discovered {len(order)} schemes")
    return order


_DEBUG_FIRST = {"done": False}


def _fetch_holdings(code, diag):
    data = _unwrap(_get(f"{BASE}/api/mf/scheme-code/{code}",
                        params={"fields": "holdings"}))
    if not data:
        return None
    name = _pick(data, "schemeName", "scheme_name", "name") or ""
    category = _pick(data, "schemeCategory", "category") or ""
    holdings = _pick(data, "holdings", contains="holding") or []
    if isinstance(holdings, dict):
        holdings = holdings.get("holdings") or holdings.get("data") or list(holdings.values())
    if not _DEBUG_FIRST["done"] and holdings:
        _DEBUG_FIRST["done"] = True
        diag.append("RAW first holding: " + json.dumps(holdings[0])[:400])
    rows = []
    for h in holdings if isinstance(holdings, list) else []:
        stock = _pick(h, "companyName", "company", "name", "stockName", "security", "instrument")
        isin = _pick(h, "isin", "isinCode", "isin_code")
        pct = _to_float(_pick(h, "percentage", "weight", "pct", "holdingPercent",
                              "corpusPer", "weightage", "assetPercentage", contains="per"))
        sector = _pick(h, "sector", "industry", "sectorName") or "Unknown"
        if not stock:
            continue
        rows.append({"stock": str(stock).strip(),
                     "isin": str(isin).strip().upper() if isin else None,
                     "pct": pct, "sector": str(sector).strip()})
    return {"scheme": name, "category": category, "holdings": rows}


def run_scrape(year=None, month=None):
    diag = []
    fetched_at = datetime.now(timezone.utc).isoformat()
    now = datetime.now(timezone.utc)
    data_month = f"{year}-{int(month):02d}" if year and month else now.strftime("%Y-%m")
    diag.append(f"FundLens FinAPI scrape @ {fetched_at}  data_month={data_month}")

    probe = _unwrap(_get(f"{BASE}/api/mf/scheme-code/{SEED_CODES[0]}",
                         params={"fields": "holdings"}))
    if not probe:
        diag.append(f"FinAPI unreachable at {BASE} — host may block this server IP")
        return {"status": "unavailable", "stocks": [], "amc_details": {},
                "data_month": data_month, "source": "finapi.upvaly.com",
                "fetched_at": fetched_at, "diagnostics": diag}
    diag.append("FinAPI reachable")

    codes = _discover_schemes(diag)
    if not codes:
        diag.append("No schemes discovered")
        return {"status": "unavailable", "stocks": [], "amc_details": {},
                "data_month": data_month, "source": "finapi.upvaly.com",
                "fetched_at": fetched_at, "diagnostics": diag}

    stocks = {}
    amc_pct = defaultdict(dict)
    done = skip_debt = skip_noamc = 0

    for i, code in enumerate(codes):
        res = _fetch_holdings(code, diag)
        time.sleep(SLEEP)
        if not res or not res["holdings"]:
            continue
        eq = _looks_equity(res["category"], res["scheme"])
        ine = sum(1 for h in res["holdings"] if h["isin"] and h["isin"].startswith("INE"))
        if eq is False or (eq is None and ine < max(3, len(res["holdings"]) // 4)):
            skip_debt += 1
            continue
        amc = _amc_from_scheme(res["scheme"])
        if not amc:
            skip_noamc += 1
            continue
        done += 1
        for h in res["holdings"]:
            key = h["isin"] or ("NAME:" + h["stock"].upper())
            s = stocks.setdefault(key, {"name": h["stock"], "isin": h["isin"],
                                        "sector": h["sector"], "amcs": set()})
            if h["sector"] != "Unknown":
                s["sector"] = h["sector"]
            s["amcs"].add(amc)
            amc_pct[amc][key] = max(amc_pct[amc].get(key, 0.0), h["pct"] or 0.0)
        if (i + 1) % 50 == 0:
            diag.append(f"...{i+1}/{len(codes)} schemes, {len(stocks)} stocks")

    diag.append(f"equity schemes={done} | skipped debt={skip_debt} | "
                f"skipped unknown-AMC={skip_noamc} | unique stocks={len(stocks)}")
    if not stocks:
        diag.append("0 stocks parsed — inspect RAW first holding line above")
        return {"status": "unavailable", "stocks": [], "amc_details": {},
                "data_month": data_month, "source": "finapi.upvaly.com",
                "fetched_at": fetched_at, "diagnostics": diag}

    prev = PREVIOUS_SNAPSHOT or {}
    diag.append(f"previous snapshot rows={len(prev)} "
                f"({'month-over-month' if prev else 'first run baseline'})")

    stock_rows, amc_details = [], {}
    for key, s in stocks.items():
        isin = s["isin"] or key
        bought = sold = new_entry = exited = holding = 0
        details = []
        for amc in sorted(s["amcs"]):
            cur = int(round((amc_pct[amc].get(key) or 0) * 100))
            p = prev.get((isin, amc))
            if p is None and prev:
                action = "new_entry"; new_entry += 1
            elif p is None:
                action = "hold"; holding += 1
            elif cur > p * 1.05:
                action = "buy"; bought += 1
            elif cur < p * 0.95:
                action = "sell"; sold += 1
            else:
                action = "hold"; holding += 1
            details.append({"amc_name": amc, "action": action, "curr_qty": cur,
                            "prev_qty": p or 0,
                            "change_pct": round((cur - (p or 0)) / p * 100, 2) if p else None})
        for (pi, pa), pq in prev.items():
            if pi == isin and pa not in s["amcs"]:
                exited += 1
                details.append({"amc_name": pa, "action": "exit", "curr_qty": 0,
                                "prev_qty": pq, "change_pct": -100.0})
        total = len(s["amcs"])
        if exited >= 3 and exited > bought:
            signal = "exit"
        elif new_entry >= 3 and new_entry >= bought:
            signal = "new"
        elif bought > sold * 1.5 and bought >= 3:
            signal = "buy"
        elif sold > bought * 1.5 and sold >= 3:
            signal = "sell"
        else:
            signal = "hold"
        stock_rows.append({"isin": isin, "name": s["name"], "sector": s["sector"],
                           "signal": signal, "total_amcs": total, "bought": bought,
                           "sold": sold, "holding": holding, "new_entry": new_entry,
                           "exited": exited, "data_month": data_month})
        amc_details[isin] = details

    stock_rows.sort(key=lambda r: r["total_amcs"], reverse=True)
    diag.append(f"built {len(stock_rows)} stock rows")
    return {"status": "ok", "stocks": stock_rows, "amc_details": amc_details,
            "data_month": data_month, "source": "finapi.upvaly.com",
            "fetched_at": fetched_at, "diagnostics": diag}


def debug_probe(family_id=None, month=None):
    code = str(family_id) if family_id and str(family_id).isdigit() and int(family_id) > 1000 else SEED_CODES[0]
    raw = _get(f"{BASE}/api/mf/scheme-code/{code}", params={"fields": "holdings"})
    data = _unwrap(raw)
    name = _pick(data or {}, "schemeName", "scheme_name", "name")
    holdings = _pick(data or {}, "holdings", contains="holding") or []
    if isinstance(holdings, dict):
        holdings = holdings.get("holdings") or holdings.get("data") or []
    sample = holdings[0] if isinstance(holdings, list) and holdings else None
    parsed = None
    if sample:
        parsed = {"stock": _pick(sample, "companyName", "company", "name", "stockName", "security"),
                  "isin": _pick(sample, "isin", "isinCode", "isin_code"),
                  "pct": _pick(sample, "percentage", "weight", "holdingPercent", "corpusPer",
                               "weightage", contains="per"),
                  "sector": _pick(sample, "sector", "industry", "sectorName")}
    return {"source": "finapi.upvaly.com", "probe_scheme_code": code,
            "scheme_name": name, "reachable": raw is not None,
            "holdings_count": len(holdings) if isinstance(holdings, list) else 0,
            "raw_first_holding": sample, "parsed_first_holding": parsed,
            "all_keys_in_holding": list(sample.keys()) if isinstance(sample, dict) else []}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = run_scrape()
    print("\n" + "=" * 60 + "\nDIAGNOSTIC SUMMARY\n" + "=" * 60)
    for d in out["diagnostics"]:
        print("->", d)
    print("=" * 60)
    print(f"status={out['status']}  stocks={len(out['stocks'])}  month={out['data_month']}")
    for s in out["stocks"][:5]:
        print(f"  {s['name'][:34]:34} {s['signal']:5} AMCs={s['total_amcs']}")
