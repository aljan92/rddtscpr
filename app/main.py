import os
import logging
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import init_db
from app.reddit_scraper.queue_manager import scrape_queue
from app.reddit_scraper.router import router as reddit_router
from app.reddit_scraper.admin_router import router as reddit_admin_router

from app.web_scraper.queue_manager import web_scrape_queue
from app.web_scraper.router import router as web_router
from app.web_scraper.admin_router import router as web_admin_router

# Logging einrichten
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rddtscpr")

app = FastAPI(title="Reddit & Web Data Extraction API", version="1.0.0")

# Vorbereitung für Templates
os.makedirs("./app/templates", exist_ok=True)
templates = Jinja2Templates(directory="app/templates")

@app.on_event("startup")
def startup_event():
    # DB Tabellen initialisieren
    init_db()
    # Reddit Queue Manager starten
    scrape_queue.start()
    # Web Scraper Queue Manager starten
    web_scrape_queue.start()
    logger.info("Datenbank initialisiert, Reddit Scrape-Queue und Web Scrape-Queue gestartet.")

@app.on_event("shutdown")
async def shutdown_event():
    await scrape_queue.stop()
    await web_scrape_queue.stop()
    logger.info("Reddit und Web Scrape-Queues gestoppt.")

# =====================================================================
# HEALTH CHECK & ROOT
# =====================================================================

@app.get("/ping", tags=["Health"])
async def health_check():
    """Public health check endpoint – used by RapidAPI and monitoring tools."""
    return {"status": "ok", "service": "Reddit & Web Scraper API", "version": "1.0.0"}

@app.get("/", include_in_schema=False)
async def root_redirect():
    return RedirectResponse(url="/admin/dashboard")

# Mount routes
app.include_router(reddit_router)
app.include_router(reddit_admin_router)
app.include_router(web_router)
app.include_router(web_admin_router)
