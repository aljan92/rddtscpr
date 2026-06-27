import os
import time
import logging
from fastapi import FastAPI, Depends, HTTPException, status, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import init_db, get_db, APIRequestLog
from app.auth import get_session_info, login_to_reddit, STATE_FILE_PATH
from app.scraper import get_subreddit_posts, get_post_comments, build_subreddit_url, clean_url

# Logging einrichten
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rddtscpr")

app = FastAPI(title="Reddit Data Extraction API", version="1.0.0")

# Vorbereitung für Templates
# Wir erstellen den templates Ordner, falls er nicht existiert
os.makedirs("./app/templates", exist_ok=True)
templates = Jinja2Templates(directory="app/templates")

# Basic Auth Security
security = HTTPBasic()

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    admin_user = os.getenv("ADMIN_USERNAME", "admin")
    admin_pass = os.getenv("ADMIN_PASSWORD", "admin123")
    
    if credentials.username != admin_user or credentials.password != admin_pass:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ungültige Admin-Zugangsdaten.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

@app.on_event("startup")
def startup_event():
    # DB Tabellen initialisieren
    init_db()
    logger.info("Datenbank initialisiert.")

# =====================================================================
# PUBLIC API ENDPOINTS
# =====================================================================

@app.get("/v1/subreddit-posts")
async def api_subreddit_posts(
    target: str,
    sort: str = "hot",
    timeframe: str = "day",
    limit: int = 10,
    db: Session = Depends(get_db)
):
    """
    Extrahiert Posts aus einem bestimmten Subreddit.
    """
    if sort not in ["hot", "new", "top", "rising"]:
        raise HTTPException(status_code=400, detail="Ungültiger 'sort'-Wert. Erlaubt: hot, new, top, rising")
    if timeframe not in ["hour", "day", "week", "month", "year", "all"]:
        raise HTTPException(status_code=400, detail="Ungültiger 'timeframe'-Wert. Erlaubt: hour, day, week, month, year, all")
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="Limit muss zwischen 1 und 100 liegen.")

    start_time = time.time()
    proxy = os.getenv("PROXY_URL")
    method_used = "json"
    
    try:
        posts, method_used = await get_subreddit_posts(
            target=target,
            sort=sort,
            timeframe=timeframe,
            limit=limit,
            proxy=proxy
        )
        
        duration = int((time.time() - start_time) * 1000)
        
        # In DB loggen
        log_entry = APIRequestLog(
            endpoint="/v1/subreddit-posts",
            target=target,
            status_code=200,
            response_time_ms=duration,
            method_used=method_used,
            proxy_used=proxy or "Kein Proxy"
        )
        db.add(log_entry)
        db.commit()
        
        scraped_url = build_subreddit_url(target, sort, timeframe)
        if sort == "top" and timeframe:
            scraped_url = f"{scraped_url}?t={timeframe}"
            
        return {
            "meta": {
                "target_subreddit": target,
                "scraped_url": scraped_url,
                "post_count": len(posts),
                "method_used": method_used,
                "execution_time_ms": duration
            },
            "data": posts
        }
        
    except Exception as e:
        duration = int((time.time() - start_time) * 1000)
        error_msg = str(e)
        logger.error(f"Fehler bei Subreddit-Scraping ({target}): {error_msg}")
        
        log_entry = APIRequestLog(
            endpoint="/v1/subreddit-posts",
            target=target,
            status_code=500,
            response_time_ms=duration,
            method_used=method_used,
            proxy_used=proxy or "Kein Proxy",
            error_message=error_msg
        )
        db.add(log_entry)
        db.commit()
        
        raise HTTPException(
            status_code=500,
            detail={"error": "Scraping-Fehler", "message": error_msg}
        )

@app.get("/v1/post-comments")
async def api_post_comments(
    post_url: str,
    sort: str = "confidence",
    limit: int = 10,
    db: Session = Depends(get_db)
):
    """
    Extrahiert Kommentare aus einem bestimmten Reddit-Post.
    """
    if sort not in ["confidence", "top", "new", "controversial", "old", "qa"]:
        raise HTTPException(status_code=400, detail="Ungültiger 'sort'-Wert. Erlaubt: confidence, top, new, controversial, old, qa")
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="Limit muss zwischen 1 und 100 liegen.")
    if not post_url.startswith("http"):
        raise HTTPException(status_code=400, detail="Ungültige Post-URL. URL muss mit http/https beginnen.")

    start_time = time.time()
    proxy = os.getenv("PROXY_URL")
    method_used = "json"
    
    try:
        comments, method_used = await get_post_comments(
            post_url=post_url,
            sort=sort,
            limit=limit,
            proxy=proxy
        )
        
        duration = int((time.time() - start_time) * 1000)
        
        log_entry = APIRequestLog(
            endpoint="/v1/post-comments",
            target=post_url,
            status_code=200,
            response_time_ms=duration,
            method_used=method_used,
            proxy_used=proxy or "Kein Proxy"
        )
        db.add(log_entry)
        db.commit()
        
        return {
            "meta": {
                "scraped_url": f"{clean_url(post_url)}?sort={sort}",
                "comment_count": len(comments),
                "method_used": method_used,
                "execution_time_ms": duration
            },
            "data": comments
        }
        
    except Exception as e:
        duration = int((time.time() - start_time) * 1000)
        error_msg = str(e)
        logger.error(f"Fehler bei Kommentar-Scraping: {error_msg}")
        
        log_entry = APIRequestLog(
            endpoint="/v1/post-comments",
            target=post_url,
            status_code=500,
            response_time_ms=duration,
            method_used=method_used,
            proxy_used=proxy or "Kein Proxy",
            error_message=error_msg
        )
        db.add(log_entry)
        db.commit()
        
        raise HTTPException(
            status_code=500,
            detail={"error": "Scraping-Fehler", "message": error_msg}
        )

# =====================================================================
# ADMIN PANEL ENDPOINTS (Basic Auth Protected)
# =====================================================================

@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    username: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    # Logs laden
    logs = db.query(APIRequestLog).order_by(APIRequestLog.timestamp.desc()).limit(50).all()
    
    # Berechne Statistiken
    total_requests = db.query(APIRequestLog).count()
    success_requests = db.query(APIRequestLog).filter(APIRequestLog.status_code == 200).count()
    error_requests = db.query(APIRequestLog).filter(APIRequestLog.status_code != 200).count()
    
    success_rate = (success_requests / total_requests * 100) if total_requests > 0 else 0
    
    # Durchschnittliche Antwortzeit (nur erfolgreiche)
    avg_duration = db.query(APIRequestLog).filter(APIRequestLog.status_code == 200)
    avg_duration_ms = 0
    if success_requests > 0:
        durations = [log.response_time_ms for log in avg_duration.all()]
        avg_duration_ms = int(sum(durations) / len(durations))
        
    # Verteilung der Scraping-Methoden
    json_count = db.query(APIRequestLog).filter(APIRequestLog.method_used == "json").count()
    playwright_count = db.query(APIRequestLog).filter(APIRequestLog.method_used == "playwright").count()
    
    # Session info holen
    session_info = get_session_info()
    
    stats = {
        "total": total_requests,
        "success": success_requests,
        "error": error_requests,
        "success_rate": f"{success_rate:.1f}%",
        "avg_duration_ms": avg_duration_ms,
        "json_count": json_count,
        "playwright_count": playwright_count
    }
    
    return templates.TemplateResponse(
        "dashboard.html", 
        {
            "request": request, 
            "stats": stats, 
            "logs": logs, 
            "session_info": session_info,
            "reddit_username": os.getenv("REDDIT_USERNAME")
        }
    )

@app.post("/admin/login-reddit")
async def admin_login_reddit(
    username: str = Depends(verify_admin)
):
    reddit_user = os.getenv("REDDIT_USERNAME")
    reddit_pass = os.getenv("REDDIT_PASSWORD")
    proxy = os.getenv("PROXY_URL")
    
    if not reddit_user or not reddit_pass:
        return RedirectResponse(
            url="/admin/dashboard?error=Username+oder+Passwort+nicht+konfiguriert+in+.env",
            status_code=303
        )
        
    try:
        success = await login_to_reddit(reddit_user, reddit_pass, proxy)
        if success:
            return RedirectResponse(url="/admin/dashboard?success=Reddit-Login+erfolgreich+durchgefuehrt!", status_code=303)
        else:
            return RedirectResponse(url="/admin/dashboard?error=Reddit-Login+fehlgeschlagen.", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/admin/dashboard?error=Login-Fehler:+{str(e)}", status_code=303)

@app.get("/admin/playground", response_class=HTMLResponse)
async def admin_playground(
    request: Request,
    username: str = Depends(verify_admin)
):
    return templates.TemplateResponse("playground.html", {"request": request})

@app.get("/admin/logs/clear")
async def admin_clear_logs(
    username: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    db.query(APIRequestLog).delete()
    db.commit()
    return RedirectResponse(url="/admin/dashboard?success=Logs+erfolgreich+geleert", status_code=303)
