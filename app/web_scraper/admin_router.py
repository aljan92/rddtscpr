import os
import json
import logging
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db, WebScraperRequestLog, SystemSetting
from app.web_scraper.queue_manager import web_scrape_queue
from app.utils import verify_admin, load_settings, save_settings, get_admin_token

logger = logging.getLogger("rddtscpr.web_admin_router")

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")

@router.get("/admin/web-scraper/dashboard", response_class=HTMLResponse)
async def web_admin_dashboard(
    request: Request,
    username: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    # Logs laden
    logs = db.query(WebScraperRequestLog).order_by(WebScraperRequestLog.timestamp.desc()).limit(100).all()
    
    # Berechne Statistiken
    total_requests = db.query(WebScraperRequestLog).count()
    success_requests = db.query(WebScraperRequestLog).filter(WebScraperRequestLog.status_code == 200).count()
    client_error_requests = db.query(WebScraperRequestLog).filter(WebScraperRequestLog.status_code.between(400, 499)).count()
    api_error_requests = db.query(WebScraperRequestLog).filter(WebScraperRequestLog.status_code >= 500).count()
    
    success_rate = (success_requests / total_requests * 100) if total_requests > 0 else 100.0
    
    # Durchschnittliche Antwortzeit (nur erfolgreiche)
    avg_duration_ms = 0
    if success_requests > 0:
        avg_duration = db.query(WebScraperRequestLog).filter(WebScraperRequestLog.status_code == 200)
        durations = [log.response_time_ms for log in avg_duration.all()]
        avg_duration_ms = int(sum(durations) / len(durations)) if durations else 0
        
    # DC vs Residential Counts
    dc_count = db.query(WebScraperRequestLog).filter(WebScraperRequestLog.stealth_mode_active == False).count()
    res_count = db.query(WebScraperRequestLog).filter(WebScraperRequestLog.stealth_mode_active == True).count()
    
    # Auslastung berechnen
    current_load, max_24h_load = web_scrape_queue.calculate_system_load()
    avg_wait = sum(web_scrape_queue.wait_times) / len(web_scrape_queue.wait_times) if web_scrape_queue.wait_times else 0.0
    
    stats = {
        "total": total_requests,
        "success": success_requests,
        "client_errors": client_error_requests,
        "api_errors": api_error_requests,
        "success_rate": f"{success_rate:.1f}%",
        "avg_duration_ms": avg_duration_ms,
        "dc_count": dc_count,
        "res_count": res_count,
        "current_load": round(current_load, 1),
        "max_24h_load": round(max_24h_load, 1),
        "avg_wait_seconds": round(avg_wait, 1)
    }
    
    return templates.TemplateResponse(
        "web/dashboard.html", 
        {
            "request": request, 
            "stats": stats, 
            "logs": logs
        }
    )

@router.get("/admin/web-scraper/playground", response_class=HTMLResponse)
async def web_admin_playground(
    request: Request,
    username: str = Depends(verify_admin)
):
    settings = load_settings()
    return templates.TemplateResponse("web/playground.html", {
        "request": request,
        "settings": settings,
        "admin_token": get_admin_token()
    })

@router.get("/admin/web-scraper/settings")
async def web_admin_settings_get(
    username: str = Depends(verify_admin)
):
    return RedirectResponse(url="/admin/settings?api=web", status_code=307)

@router.post("/admin/web-scraper/settings")
async def web_admin_settings_post(
    username: str = Depends(verify_admin)
):
    return RedirectResponse(url="/admin/settings?api=web", status_code=307)

@router.get("/admin/web-scraper/queue", response_class=HTMLResponse)
async def web_admin_queue(
    request: Request,
    username: str = Depends(verify_admin)
):
    status = web_scrape_queue.get_queue_status()
    return templates.TemplateResponse("web/queue.html", {
        "request": request,
        "max_workers": web_scrape_queue.min_workers,
        "max_capacity": web_scrape_queue.max_capacity,
        "proxy_mode": web_scrape_queue.proxy_mode,
        "stats": status["stats"],
        "requests": status["requests"]
    })

@router.post("/admin/web-scraper/queue/settings")
async def web_admin_queue_settings(
    max_workers: int = Form(5),
    max_capacity: int = Form(20),
    proxy_mode: str = Form("auto"),
    username: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    if max_workers < 1 or max_workers > 100:
        return RedirectResponse(url="/admin/web-scraper/queue?error=Basis-Workers+muss+zwischen+1+und+100+liegen.", status_code=303)
    if max_capacity < max_workers or max_capacity > 100:
        return RedirectResponse(url=f"/admin/web-scraper/queue?error=Max-Kapazität+muss+mindestens+so+groß+wie+Basis-Sessions+({max_workers})+und+maximal+100+sein.", status_code=303)
    if proxy_mode not in ["auto", "stealth"]:
        proxy_mode = "auto"
    try:
        web_scrape_queue.max_capacity = max_capacity
        web_scrape_queue.proxy_mode = proxy_mode
        web_scrape_queue.resize_worker_pool(max_workers)
        
        # Save Basis-Workers
        setting = db.query(SystemSetting).filter(SystemSetting.key == "web_scraper_max_workers").first()
        if setting:
            setting.value = str(max_workers)
        else:
            setting = SystemSetting(key="web_scraper_max_workers", value=str(max_workers))
            db.add(setting)
            
        # Save Max-Capacity
        cap_setting = db.query(SystemSetting).filter(SystemSetting.key == "web_scraper_max_capacity").first()
        if cap_setting:
            cap_setting.value = str(max_capacity)
        else:
            cap_setting = SystemSetting(key="web_scraper_max_capacity", value=str(max_capacity))
            db.add(cap_setting)

        # Save Proxy-Mode
        pm_setting = db.query(SystemSetting).filter(SystemSetting.key == "web_scraper_proxy_mode").first()
        if pm_setting:
            pm_setting.value = proxy_mode
        else:
            pm_setting = SystemSetting(key="web_scraper_proxy_mode", value=proxy_mode)
            db.add(pm_setting)
            
        db.commit()
        return RedirectResponse(url="/admin/web-scraper/queue?success=Einstellungen+erfolgreich+gespeichert!", status_code=303)
    except Exception as e:
        logger.error(f"Fehler beim Speichern der Queue-Einstellungen: {e}")
        return RedirectResponse(url=f"/admin/web-scraper/queue?error=Interner+Fehler:+{str(e)}", status_code=303)

@router.get("/admin/web-scraper/queue/api")
async def web_admin_queue_api(
    username: str = Depends(verify_admin)
):
    return web_scrape_queue.get_queue_status()

@router.get("/admin/web-scraper/rapidapi-playground", response_class=HTMLResponse)
async def web_admin_rapidapi_playground(
    request: Request,
    username: str = Depends(verify_admin)
):
    settings = load_settings()
    return templates.TemplateResponse("web/rapidapi_playground.html", {"request": request, "settings": settings})

@router.post("/admin/web-scraper/settings/reset-stats")
async def web_admin_settings_reset_stats(
    username: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    db.query(WebScraperRequestLog).delete()
    db.commit()
    logger.info("Web-Scraper-Statistiken über Admin-Settings zurückgesetzt.")
    return RedirectResponse(url="/admin/web-scraper/settings?success=Statistiken+erfolgreich+zurueckgesetzt!", status_code=303)

@router.get("/admin/web-scraper/logs/clear")
async def web_admin_clear_logs(
    username: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    db.query(WebScraperRequestLog).delete()
    db.commit()
    return RedirectResponse(url="/admin/web-scraper/dashboard?success=Logs+erfolgreich+geleert", status_code=303)

@router.get("/admin/web-scraper/evomi-balance")
async def web_get_evomi_balance_endpoint(
    username: str = Depends(verify_admin)
):
    # Reuse existing global endpoint implementation
    from app.reddit_scraper.admin_router import get_evomi_balance_endpoint
    return await get_evomi_balance_endpoint(username)

@router.get("/admin/web-scraper/logs/{log_id}/json")
async def get_log_json(
    log_id: int,
    username: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    log = db.query(WebScraperRequestLog).filter(WebScraperRequestLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log nicht gefunden")
    try:
        data = json.loads(log.response_json) if log.response_json else {"error": log.error_message or "Keine Antwortdaten vorhanden"}
    except Exception:
        data = {"raw": log.response_json}
    return data

