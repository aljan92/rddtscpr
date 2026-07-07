import os
import logging
from fastapi import APIRouter, Request, HTTPException, Depends, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_db, WebScraperJob
from app.web_scraper.models import ScrapeRequest
from app.web_scraper.queue_manager import web_scrape_queue
from app.utils import check_rapidapi_access

logger = logging.getLogger("rddtscpr.web_router")

router = APIRouter()

@router.post("/v1/web/scrape")
async def api_web_scrape(
    request: Request,
    payload: ScrapeRequest,
    db: Session = Depends(get_db)
):
    """
    Startet ein Web-Scraping oder Sub-Page Crawling.
    
    Unterstützt synchrone Lieferung ('direct') und asynchrone Lieferung ('webhook' oder 'both').
    Gibt bei asynchroner Verarbeitung sofort 202 Accepted mit der Job-ID zurück.
    """
    check_rapidapi_access(request)

    if payload.delivery_mode not in ["direct", "webhook", "both"]:
        raise HTTPException(
            status_code=400,
            detail="delivery_mode muss 'direct', 'webhook' oder 'both' sein."
        )

    if payload.delivery_mode in ["webhook", "both"] and not payload.webhook_url:
        raise HTTPException(
            status_code=400,
            detail="webhook_url ist erforderlich, wenn delivery_mode 'webhook' oder 'both' ist."
        )

    # Check subscription for premium features (Crawling, Chunking, Screenshots)
    has_crawling = payload.page_crawling
    has_chunking = payload.chunk_size is not None
    has_screenshot = payload.response_filters.include_screenshot if payload.response_filters else False

    if has_crawling or has_chunking or has_screenshot:
        subscription = request.headers.get("x-rapidapi-subscription") or ""
        sub_str = subscription.lower()
        is_premium = any(p in sub_str for p in ["pro", "ultra", "mega"])
        
        if not is_premium:
            if has_crawling:
                raise HTTPException(
                    status_code=403,
                    detail="The 'page_crawling' feature is restricted to Pro, Mega, and Ultra plans. Please upgrade your subscription."
                )
            if has_chunking:
                raise HTTPException(
                    status_code=403,
                    detail="The 'chunking' feature is restricted to Pro, Mega, and Ultra plans. Please upgrade your subscription."
                )
            if has_screenshot:
                raise HTTPException(
                    status_code=403,
                    detail="The 'include_screenshot' feature is restricted to Pro, Mega, and Ultra plans. Please upgrade your subscription."
                )

    is_playground = request.query_params.get("playground") == "true"
    
    try:
        # Enqueue the request
        result, status_msg, is_failed = await web_scrape_queue.enqueue(
            url=payload.url,
            request_params=payload,
            is_playground=is_playground
        )
        
        # Falls direct mode, liefert result direkt das ScrapeResponse JSON
        if payload.delivery_mode == "direct":
            return result
            
        # Falls webhook / both, liefern wir 202 Accepted
        return RedirectResponse(url="", status_code=202) if False else result
        
    except Exception as e:
        logger.error(f"Fehler im Web-Scraper Router bei {payload.url}: {e}")
        raise HTTPException(
            status_code=500,
            detail={"error": "Internal scraper error", "message": str(e)}
        )

@router.get("/v1/web/job-status/{job_id}")
async def api_web_job_status(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Ruft den Status und das Ergebnis eines asynchronen Web-Scraping Jobs ab.
    Ergebnisse sind für 24 Stunden zwischengespeichert.
    """
    check_rapidapi_access(request)

    job = db.query(WebScraperJob).filter(WebScraperJob.id == job_id).first()
    if not job:
        raise HTTPException(
            status_code=404,
            detail="Job-ID nicht gefunden oder bereits abgelaufen (älter als 24 Stunden)."
        )

    response_data = {
        "job_id": job.id,
        "url": job.url,
        "status": job.status,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error": job.error_message,
        "data": None
    }

    if job.status == "Erfolgreich" and job.result:
        try:
            import json
            response_data["data"] = json.loads(job.result)
        except Exception as e:
            logger.error(f"Fehler beim Laden des JSON-Ergebnisses für Job {job_id}: {e}")
            
    return response_data

@router.get("/v1/web/screenshots/{filename}")
async def api_web_screenshot(
    filename: str
):
    """
    Öffentlicher Endpunkt zum Abrufen eines temporären Screenshots.
    """
    # Pfadbereinigung, um Path Traversal zu verhindern
    clean_filename = os.path.basename(filename)
    screenshot_path = f"./app/data/screenshots/{clean_filename}"
    
    if os.path.exists(screenshot_path):
        return FileResponse(screenshot_path)
        
    raise HTTPException(
        status_code=404,
        detail="Screenshot nicht gefunden oder bereits abgelaufen."
    )
