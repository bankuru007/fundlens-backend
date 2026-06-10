"""
FundLens Scraper — FinAPI (finapi.upvaly.com)
=============================================
DATABASE-FREE. Exposes run_scrape() and debug_probe() for data_store / main.

Signals come from FinAPI's per-holding `change1M` field (month-over-month % change
in weightage), so real BUY/SELL/NEW/EXIT signals work on the FIRST run — no need
to store snapshots or wait a month.

Per AMC holding:
    change1M >= +5   -> buy
    change1M <= -99  -> exit (fully out)
    change1M <= -5   -> sell
    else             -> hold
Stock-level signal aggregates across all AMCs holding that stock.

Only dependency: requests.
"""

import os, json, time, logging
from datetime import datetime, timezone
from collections import defaultdict

import requests

log = logging.getLogger("fundlens.scraper")

BASE = "https://finapi.upvaly.com"
SEED_CODES = ["120503", "152135"]
MAX_SCHEMES = int(os.environ.get("FUNDLENS_MAX_SCHEMES", "600"))
SLEEP = float(os.environ.get("FUNDLENS_SLEEP", "0.25"))
TIMEOUT = 25
RETRIES = 2

# signal thresholds (percent change in weightage, month-over-month)
BUY_TH = 5.0
SELL_TH = -5.0
EXIT_TH = -99.0

# non-stock instruments to exclude from intelligence
SKIP_NAMES = ("net receivable", "net payable", "clearing corporation", "triparty",
              "treps", "reverse repo", "cash", "cblo", "margin", "t-bill",
              "treasury bill", "364 days", "182 days", "91 days")

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
    "old bridge":"Old Bridge MF","franklin templeton":"Franklin Templeton MF",
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
            if r.status_code == 429:
                time.sleep(2 + attempt * 2)  # back off on rate limit
                continue
            log.warning("HTTP %s %s (try %s)", r.status_code, url, attempt + 1)
        except Exception as e:
            log.warning("%s %s (try %s)", type(e).__name__, url, attempt + 1)
        time.sleep(1 + attempt)
    return None


def _unwrap(p):
    return p["data"] if isinstance(p, dict) and "data" in p else p


def _pick(d, *cands, contains=None):
    if not isinstance(d, dict):
        return None
    low = {k.lower(): v for k, v in d.items()}
    for c in cands:
        if c.lower() in low:
            return low[c.lower()]
    if contains:
        for k, v in low.items():
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


def _amc_from_scheme(name, fund_house=None):
    for src in (fund_house, name):
        n = (src or "").lower()
        for kw, amc in AMC_KEYWORDS.items():
            if n.startswith(kw) or f" {kw}" in n[:40]:
                return amc
    return None


def _looks_equity(category, name):
    blob = f"{category or ''} {name or ''}".lower()
    if any(h in blob for h in DEBT_HINTS):
        return False
    if any(h in blob for h in EQUITY_HINTS):
        return True
    return None


def _is_stock(name):
    n = (name or "").lower()
    return not any(s in n for s in SKIP_NAMES)


def _discover_schemes(diag):
    for path in ("/api/mf/schemes", "/api/mf/all", "/api/mf/list", "/api/mf"):
        data = _unwrap(_get(BASE + path))
        if isinstance(data, list) and len(data) > 50:
            codes = [str(_pick(it, "schemeCode", "scheme_code", "code"))
                     for it in data if _pick(it, "schemeCode", "scheme_code", "code")]
            if codes:
                diag.append(f"List endpoint {path} -> {len(codes)} schemes")
                return codes[:MAX_SCHEMES]
    diag.append("No list endpoint; crawling morefundsfromamc from seeds")
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


def _fetch(code, diag):
    data = _unwrap(_get(f"{BASE}/api/mf/scheme-code/{code}",
                        params={"fields": "holdings"}))
    if not data:
        return None
    name = _pick(data, "schemeName", "scheme_name", "name") or ""
    category = _pick(data, "schemeCategory", "schemeCategoryLabel", "category") or ""
    fund_house = _pick(data, "fundHouse", "companyName") or ""
    holdings = _pick(data, "holdings", contains="holding") or []
    if isinstance(holdings, dict):
        holdings = holdings.get("holdings") or holdings.get("data") or list(holdings.values())
    if not _DEBUG_FIRST["done"] and holdings:
        _DEBUG_FIRST["done"] = True
        diag.append("RAW first holding: " + json.dumps(holdings[0])[:300])
    rows = []
    for h in holdings if isinstance(holdings, list) else []:
        stock = _pick(h, "name", "companyName", "company", "stockName", "security")
        if not stock or not _is_stock(stock):
            continue
        pct = _to_float(_pick(h, "weightage", "percentage", "weight", "holdingPercent",
                              contains="weight"))
        chg = _to_float(_pick(h, "change1M", contains="change1m"))
        sector = _pick(h, "sector", "industry", "sectorName") or "Unknown"
        rows.append({"stock": str(stock).strip(), "pct": pct,
                     "change1m": chg, "sector": str(sector).strip()})
    return {"scheme": name, "category": category, "fund_house": fund_house, "holdings": rows}


def _norm(name):
    """Normalize stock names so 'X Ltd' and 'X Limited' merge."""
    n = name.lower().strip()
    for suf in (" ltd.", " ltd", " limited", " ordinary shares", " (india)", " india"):
        if n.endswith(suf):
            n = n[: -len(suf)].strip()
    return n


def run_scrape(year=None, month=None):
    diag = []
    fetched_at = datetime.now(timezone.utc).isoformat()
    now = datetime.now(timezone.utc)
    data_month = f"{year}-{int(month):02d}" if year and month else now.strftime("%Y-%m")
    diag.append(f"FinAPI scrape @ {fetched_at}  month={data_month}")

    if not _unwrap(_get(f"{BASE}/api/mf/scheme-code/{SEED_CODES[0]}", params={"fields": "holdings"})):
        diag.append(f"FinAPI unreachable at {BASE}")
        return _empty(diag, data_month, fetched_at)
    diag.append("FinAPI reachable")

    codes = _discover_schemes(diag)
    if not codes:
        diag.append("No schemes discovered")
        return _empty(diag, data_month, fetched_at)

    # aggregate per normalized stock
    agg = {}   # norm -> {name,sector, amc_actions:{amc:action}, buy,sell,new,exit,hold}
    done = skip_debt = skip_noamc = 0

    for i, code in enumerate(codes):
        res = _fetch(code, diag)
        time.sleep(SLEEP)
        if not res or not res["holdings"]:
            continue
        eq = _looks_equity(res["category"], res["scheme"])
        if eq is False:
            skip_debt += 1
            continue
        amc = _amc_from_scheme(res["scheme"], res["fund_house"])
        if not amc:
            skip_noamc += 1
            continue
        done += 1
        for h in res["holdings"]:
            chg = h["change1m"]
            if chg is None:        # no month-over-month data -> treat as steady hold
                action = "hold"
            elif chg <= EXIT_TH:   action = "exit"
            elif chg >= BUY_TH:    action = "buy"
            elif chg <= SELL_TH:   action = "sell"
            else:                  action = "hold"
            key = _norm(h["stock"])
            s = agg.setdefault(key, {"name": h["stock"], "sector": h["sector"],
                                     "amcs": {}, "weights": []})
            if h["sector"] != "Unknown":
                s["sector"] = h["sector"]
            # keep the strongest signal per AMC for this stock
            prev = s["amcs"].get(amc)
            rank = {"exit": 4, "buy": 3, "sell": 2, "hold": 1}
            if prev is None or rank[action] > rank[prev]:
                s["amcs"][amc] = action
            if h["pct"]:
                s["weights"].append(h["pct"])
        if (i + 1) % 50 == 0:
            diag.append(f"...{i+1}/{len(codes)} schemes, {len(agg)} stocks")

    diag.append(f"equity schemes={done} | skipped debt={skip_debt} | "
                f"skipped unknown-AMC={skip_noamc} | unique stocks={len(agg)}")
    if not agg:
        diag.append("0 stocks parsed — inspect RAW line above")
        return _empty(diag, data_month, fetched_at)

    stock_rows, amc_details = [], {}
    for key, s in agg.items():
        actions = s["amcs"]
        bought = sum(1 for a in actions.values() if a == "buy")
        sold = sum(1 for a in actions.values() if a == "sell")
        exited = sum(1 for a in actions.values() if a == "exit")
        holding = sum(1 for a in actions.values() if a == "hold")
        total = len(actions)
        # stock-level signal
        if exited >= 2 and exited >= bought:        signal = "exit"
        elif bought >= 2 and bought > sold + exited: signal = "buy"
        elif sold + exited >= 2 and sold + exited > bought: signal = "sell"
        elif bought > 0 and bought >= sold:          signal = "buy"
        elif sold > 0:                               signal = "sell"
        else:                                        signal = "hold"
        isin = "NAME:" + key.upper()
        stock_rows.append({"isin": isin, "name": s["name"], "sector": s["sector"],
                           "signal": signal, "total_amcs": total, "bought": bought,
                           "sold": sold, "holding": holding, "new_entry": 0,
                           "exited": exited, "data_month": data_month})
        amc_details[isin] = [{"amc_name": a, "action": act, "curr_qty": 0,
                              "prev_qty": 0, "change_pct": None}
                             for a, act in sorted(actions.items())]

    stock_rows.sort(key=lambda r: r["total_amcs"], reverse=True)
    buy = sum(1 for s in stock_rows if s["signal"] == "buy")
    sell = sum(1 for s in stock_rows if s["signal"] in ("sell", "exit"))
    diag.append(f"built {len(stock_rows)} stocks | BUY={buy} SELL/EXIT={sell}")
    return {"status": "ok", "stocks": stock_rows, "amc_details": amc_details,
            "data_month": data_month, "source": "finapi.upvaly.com",
            "fetched_at": fetched_at, "diagnostics": diag}


def _empty(diag, data_month, fetched_at):
    return {"status": "unavailable", "stocks": [], "amc_details": {},
            "data_month": data_month, "source": "finapi.upvaly.com",
            "fetched_at": fetched_at, "diagnostics": diag}


def debug_probe(family_id=None, month=None):
    code = str(family_id) if family_id and str(family_id).isdigit() and int(family_id) > 1000 else SEED_CODES[0]
    raw = _get(f"{BASE}/api/mf/scheme-code/{code}", params={"fields": "holdings"})
    data = _unwrap(raw)
    holdings = _pick(data or {}, "holdings", contains="holding") or []
    if isinstance(holdings, dict):
        holdings = holdings.get("holdings") or holdings.get("data") or []
    sample = holdings[0] if isinstance(holdings, list) and holdings else None
    # count how many holdings carry change1M
    with_chg = sum(1 for h in holdings if isinstance(h, dict) and _pick(h, "change1M", contains="change1m") is not None) if isinstance(holdings, list) else 0
    return {"source": "finapi.upvaly.com", "probe_scheme_code": code,
            "scheme_name": _pick(data or {}, "schemeName", "name"),
            "fund_house": _pick(data or {}, "fundHouse", "companyName"),
            "reachable": raw is not None,
            "holdings_count": len(holdings) if isinstance(holdings, list) else 0,
            "holdings_with_change1M": with_chg,
            "raw_first_holding": sample,
            "all_keys_in_holding": list(sample.keys()) if isinstance(sample, dict) else []}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = run_scrape()
    print("\n" + "=" * 60 + "\nDIAGNOSTIC SUMMARY\n" + "=" * 60)
    for d in out["diagnostics"]:
        print("->", d)
    print("=" * 60)
    print(f"status={out['status']}  stocks={len(out['stocks'])}  month={out['data_month']}")
    for s in out["stocks"][:10]:
        print(f"  {s['name'][:30]:30} {s['signal']:5} AMCs={s['total_amcs']:3} "
              f"B={s['bought']} S={s['sold']} X={s['exited']}")
