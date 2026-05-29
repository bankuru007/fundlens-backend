"""
FundLens — FastAPI Backend (self-contained, no database)

Serves the dashboard at / and a JSON API at /api/*. Fetches real MF holdings
from mfdata.in itself, caches them on disk, and auto-refreshes on the 12th of
every month. No Railway/Supabase required — just `uvicorn main:app`.
"""

import os, logging
from datetime import datetime

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from apscheduler.schedulers.background import BackgroundScheduler

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import data_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(__file__)
INDEX_HTML = os.path.join(BASE_DIR, "index.html")

app = FastAPI(title="FundLens API", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def _startup():
    # Load any real cached data instantly, then refresh live in the background.
    data_store.load()
    data_store.refresh_async()
    logger.info("Startup complete — loaded cache + kicked background refresh")


# Auto-refresh on the 12th of every month at 10am IST (AMFI publishes by ~12th)
scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
scheduler.add_job(data_store.refresh, "cron", day=12, hour=10, minute=0)
scheduler.start()
logger.info("Scheduler started — refresh runs on the 12th of every month at 10am IST")


# ============================================================
# Dashboard (served same-origin so the UI just hits /api)
# ============================================================
@app.get("/", response_class=HTMLResponse)
def dashboard():
    if os.path.exists(INDEX_HTML):
        return FileResponse(INDEX_HTML)
    return HTMLResponse("<h1>FundLens API</h1><p>Dashboard file missing. API is at /api</p>")


@app.get("/api")
def api_root():
    return {"status": "FundLens API v3.0 running", "data_source": "AMFI India via mfdata.in",
            **data_store.get_meta()}


@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# ============================================================
# Data endpoints (read from the in-memory store)
# ============================================================
@app.get("/api/stocks")
def get_stocks(signal: str = None, sector: str = None, limit: int = 500):
    rows = data_store.get_stocks(signal, sector, limit)
    return {"data": rows, "count": len(rows)}


@app.get("/api/stocks/{isin}")
def get_stock_detail(isin: str):
    stock, amcs = data_store.get_stock(isin)
    if not stock:
        raise HTTPException(404, f"Stock {isin} not found")
    return {"stock": stock, "amc_details": amcs}


@app.get("/api/stats")
def get_stats():
    return data_store.get_stats()


@app.get("/api/sectors")
def get_sectors():
    return {"sectors": data_store.get_sectors()}


# ============================================================
# Manual refresh triggers
# ============================================================
@app.post("/api/scrape")
def trigger_scrape(background_tasks: BackgroundTasks):
    """Force a fresh fetch from mfdata.in (runs in background)."""
    background_tasks.add_task(data_store.refresh)
    return {"message": "Refresh triggered", "timestamp": datetime.utcnow().isoformat(),
            **data_store.get_meta()}


@app.post("/api/scrape/{year}/{month}")
def scrape_specific_month(year: int, month: int, background_tasks: BackgroundTasks):
    """Fetch a specific month."""
    background_tasks.add_task(data_store.refresh, year, month)
    return {"message": f"Refreshing {year}-{month:02d}", "timestamp": datetime.utcnow().isoformat()}
