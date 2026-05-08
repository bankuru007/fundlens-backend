"""
FundLens — FastAPI Backend
Auto-runs AMFI scraper on 12th of every month
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
import os, logging
from datetime import datetime, date
from scraper import run_scrape

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

app = FastAPI(title="FundLens API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Auto-scrape on 12th of every month at 10am IST
scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
scheduler.add_job(run_scrape, "cron", day=12, hour=10, minute=0)
scheduler.start()
logger.info("Scheduler started — scrape runs on 12th of every month at 10am IST")


@app.get("/")
def root():
    return {"status": "FundLens API v2.0 running", "data_source": "AMFI India (portal.amfiindia.com)"}

@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}

@app.get("/api/stocks")
def get_stocks(signal: str = None, sector: str = None, limit: int = 500):
    try:
        q = supabase.table("stock_intelligence").select("*")
        if signal and signal != "all":
            q = q.eq("signal", signal)
        if sector and sector != "all":
            q = q.eq("sector", sector)
        result = q.order("total_amcs", desc=True).limit(limit).execute()
        return {"data": result.data, "count": len(result.data)}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/stocks/{isin}")
def get_stock_detail(isin: str):
    try:
        stock = supabase.table("stock_intelligence").select("*").eq("isin", isin).single().execute()
        amcs = supabase.table("amc_holdings").select("*").eq("isin", isin).execute()
        return {"stock": stock.data, "amc_details": amcs.data}
    except Exception as e:
        raise HTTPException(404, str(e))

@app.get("/api/stats")
def get_stats():
    try:
        stocks = supabase.table("stock_intelligence").select("signal, sector").execute()
        data = stocks.data
        meta = supabase.table("scrape_meta").select("*").order("scraped_at", desc=True).limit(1).execute()
        amcs = supabase.table("amc_holdings").select("amc_name").execute()
        unique_amcs = len(set(r['amc_name'] for r in amcs.data))
        return {
            "total_stocks": len(data),
            "buying": len([s for s in data if s["signal"] in ("buy","new")]),
            "selling": len([s for s in data if s["signal"] in ("sell","exit")]),
            "new_entries": len([s for s in data if s["signal"] == "new"]),
            "total_amcs": unique_amcs,
            "last_updated": meta.data[0]["scraped_at"] if meta.data else None,
            "data_month": meta.data[0]["data_month"] if meta.data else None,
        }
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/sectors")
def get_sectors():
    try:
        result = supabase.table("stock_intelligence").select("sector").execute()
        sectors = sorted(set(r["sector"] for r in result.data if r["sector"]))
        return {"sectors": sectors}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/api/scrape")
def trigger_scrape(background_tasks: BackgroundTasks):
    """Manually trigger scrape — use this if auto-scrape fails"""
    background_tasks.add_task(run_scrape)
    return {"message": "Scrape triggered", "timestamp": datetime.utcnow().isoformat()}

@app.post("/api/scrape/{year}/{month}")
def scrape_specific_month(year: int, month: int, background_tasks: BackgroundTasks):
    """Backfill a specific month"""
    background_tasks.add_task(run_scrape, year, month)
    return {"message": f"Scraping {year}-{month:02d}", "timestamp": datetime.utcnow().isoformat()}
