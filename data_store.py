"""
FundLens data store — self-contained, no database.

Holds the latest real MF-holdings intelligence in memory, backed by an on-disk
JSON cache (`data_cache.json`). Data only ever comes from a real fetch via
scraper.run_scrape(); on failure we keep the last real cache (clearly dated) or
report an honest "unavailable" status. Nothing is fabricated.
"""

import os, json, logging, threading
from datetime import datetime

from scraper import run_scrape

logger = logging.getLogger(__name__)

CACHE_FILE = os.path.join(os.path.dirname(__file__), "data_cache.json")

# In-memory snapshot. `status` is "empty" until a real fetch (or cache load) populates it.
_lock = threading.Lock()
_state = {
    "status": "empty",          # empty | ok | unavailable
    "stocks": [],               # list of stock_intelligence rows
    "amc_details": {},          # isin -> [amc rows]
    "data_month": None,
    "source": None,             # e.g. "mfdata.in" or "cache"
    "fetched_at": None,
    "diagnostics": [],
}
_refreshing = False


def _set_state(new):
    with _lock:
        _state.update(new)


def load():
    """Load the last real fetch from disk (if any). Never invents data."""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                cached = json.load(f)
            if cached.get("stocks"):
                _set_state({
                    "status": "ok",
                    "stocks": cached.get("stocks", []),
                    "amc_details": cached.get("amc_details", {}),
                    "data_month": cached.get("data_month"),
                    # mark provenance as cache so the UI can label it truthfully
                    "source": cached.get("source", "mfdata.in"),
                    "fetched_at": cached.get("fetched_at"),
                    "diagnostics": ["Loaded from on-disk cache (real prior fetch)"],
                })
                logger.info(f"Loaded {len(cached.get('stocks', []))} stocks from cache "
                            f"({cached.get('data_month')}, fetched {cached.get('fetched_at')})")
                return True
        except Exception as e:
            logger.warning(f"Could not read cache: {e}")
    logger.info("No usable cache — store is empty until first refresh")
    return False


def _save_cache(payload):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(payload, f)
        logger.info(f"Wrote cache: {len(payload.get('stocks', []))} stocks")
    except Exception as e:
        logger.warning(f"Could not write cache: {e}")


def refresh(year=None, month=None):
    """
    Fetch fresh real data via the scraper and replace the store on success.
    On failure, keep whatever real data we already have and record the error.
    """
    global _refreshing
    with _lock:
        if _refreshing:
            logger.info("Refresh already in progress — skipping duplicate")
            return {"status": "in_progress"}
        _refreshing = True
    try:
        logger.info("Refresh starting — fetching live data from mfdata.in ...")
        result = run_scrape(year, month)

        if result.get("status") == "ok" and result.get("stocks"):
            payload = {
                "status": "ok",
                "stocks": result["stocks"],
                "amc_details": result.get("amc_details", {}),
                "data_month": result.get("data_month"),
                "source": result.get("source", "mfdata.in"),
                "fetched_at": result.get("fetched_at"),
                "diagnostics": result.get("diagnostics", []),
            }
            _set_state(payload)
            _save_cache(payload)
            logger.info(f"Refresh OK — {len(result['stocks'])} stocks for {result.get('data_month')}")
            return {"status": "ok", "stocks": len(result["stocks"]),
                    "data_month": result.get("data_month"), "source": result.get("source")}

        # Fetch failed. Do NOT fabricate. Keep existing real data if we have it.
        with _lock:
            had_data = bool(_state["stocks"])
        if not had_data:
            _set_state({
                "status": "unavailable",
                "diagnostics": result.get("diagnostics", []),
                "fetched_at": result.get("fetched_at"),
            })
        else:
            logger.warning("Refresh failed — retaining previously cached real data")
            with _lock:
                _state["diagnostics"] = (["Refresh failed; showing previously cached data"]
                                         + result.get("diagnostics", []))
        return {"status": "unavailable", "diagnostics": result.get("diagnostics", [])}
    except Exception as e:
        logger.exception("Refresh crashed")
        with _lock:
            if not _state["stocks"]:
                _state["status"] = "unavailable"
                _state["diagnostics"] = [f"Refresh error: {e}"]
        return {"status": "error", "error": str(e)}
    finally:
        with _lock:
            _refreshing = False


def refresh_async(year=None, month=None):
    """Kick a refresh on a background thread (non-blocking)."""
    t = threading.Thread(target=refresh, kwargs={"year": year, "month": month}, daemon=True)
    t.start()
    return t


# ============================================================
# Read accessors used by the API
# ============================================================
def get_stocks(signal=None, sector=None, limit=500):
    with _lock:
        rows = list(_state["stocks"])
    if signal and signal != "all":
        rows = [s for s in rows if s.get("signal") == signal]
    if sector and sector != "all":
        rows = [s for s in rows if s.get("sector") == sector]
    rows.sort(key=lambda s: s.get("total_amcs", 0), reverse=True)
    return rows[:limit]


def get_stock(isin):
    with _lock:
        stock = next((s for s in _state["stocks"] if s.get("isin") == isin), None)
        amcs = list(_state["amc_details"].get(isin, []))
    return stock, amcs


def get_stats():
    with _lock:
        data = list(_state["stocks"])
        amc_details = _state["amc_details"]
        meta = {k: _state[k] for k in ("data_month", "source", "fetched_at", "status")}
    unique_amcs = {a["amc_name"] for rows in amc_details.values() for a in rows}
    return {
        "total_stocks": len(data),
        "buying": len([s for s in data if s.get("signal") in ("buy", "new")]),
        "selling": len([s for s in data if s.get("signal") in ("sell", "exit")]),
        "new_entries": len([s for s in data if s.get("signal") == "new"]),
        "total_amcs": len(unique_amcs),
        "data_month": meta["data_month"],
        "source": meta["source"],
        "fetched_at": meta["fetched_at"],
        "status": meta["status"],
        "last_updated": meta["fetched_at"],
    }


def get_sectors():
    with _lock:
        return sorted({s.get("sector") for s in _state["stocks"] if s.get("sector")})


def get_meta():
    with _lock:
        return {
            "status": _state["status"],
            "data_month": _state["data_month"],
            "source": _state["source"],
            "fetched_at": _state["fetched_at"],
            "stocks": len(_state["stocks"]),
            "diagnostics": _state["diagnostics"][-12:],
        }
