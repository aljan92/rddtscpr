import os
import json
import time
import logging
from fastapi import FastAPI, Depends, HTTPException, status, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import init_db, get_db, APIRequestLog, RedditAccount
from app.auth import get_session_info_from_state, login_to_reddit
from app.scraper import build_subreddit_url, clean_url
from app.queue_manager import scrape_queue

# Logging einrichten
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rddtscpr")

app = FastAPI(title="Reddit Data Extraction API", version="1.0.0")

# Vorbereitung für Templates
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
    # Queue Manager starten
    scrape_queue.start()
    logger.info("Datenbank initialisiert und Scrape-Queue gestartet.")

@app.on_event("shutdown")
async def shutdown_event():
    await scrape_queue.stop()
    logger.info("Scrape-Queue gestoppt.")

# =====================================================================
# PUBLIC API ENDPOINTS (Routet über Queue)
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
    method_used = "json"
    proxy_used = "Dynamisch"
    
    try:
        posts, method_used = await scrape_queue.enqueue(
            action="subreddit",
            params={
                "target": target,
                "sort": sort,
                "timeframe": timeframe,
                "limit": limit
            }
        )
        
        duration = int((time.time() - start_time) * 1000)
        
        # In DB loggen
        log_entry = APIRequestLog(
            endpoint="/v1/subreddit-posts",
            target=target,
            status_code=200,
            response_time_ms=duration,
            method_used=method_used,
            proxy_used=proxy_used
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
            proxy_used=proxy_used,
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
    include_replies: bool = False,
    load_more: bool = False,
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
    method_used = "json"
    proxy_used = "Dynamisch"
    
    try:
        comments, method_used = await scrape_queue.enqueue(
            action="comments",
            params={
                "post_url": post_url,
                "sort": sort,
                "limit": limit,
                "include_replies": include_replies,
                "load_more": load_more
            }
        )
        
        duration = int((time.time() - start_time) * 1000)
        
        log_entry = APIRequestLog(
            endpoint="/v1/post-comments",
            target=post_url,
            status_code=200,
            response_time_ms=duration,
            method_used=method_used,
            proxy_used=proxy_used
        )
        db.add(log_entry)
        db.commit()
        
        return {
            "meta": {
                "scraped_url": f"{clean_url(post_url)}?sort={sort}",
                "comment_count": len(comments),
                "include_replies": include_replies,
                "load_more": load_more,
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
            proxy_used=proxy_used,
            error_message=error_msg
        )
        db.add(log_entry)
        db.commit()
        
        raise HTTPException(
            status_code=500,
            detail={"error": "Scraping-Fehler", "message": error_msg}
        )

# =====================================================================
# ADMIN PANEL ENDPOINTS & CRUD (Basic Auth Protected)
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
        
    json_count = db.query(APIRequestLog).filter(APIRequestLog.method_used == "json").count()
    playwright_count = db.query(APIRequestLog).filter(APIRequestLog.method_used == "playwright").count()
    
    # Accounts aus DB laden
    db_accounts = db.query(RedditAccount).all()
    accounts_info = []
    
    for acc in db_accounts:
        session_info = get_session_info_from_state(acc.session_state)
        has_screenshot = os.path.exists(f"./app/data/last_error_{acc.username}.png")
        accounts_info.append({
            "id": acc.id,
            "username": acc.username,
            "proxy_url": acc.proxy_url or "Kein Proxy",
            "fallback_proxy_url": acc.fallback_proxy_url or "Kein Proxy",
            "is_active": acc.is_active,
            "failure_count": acc.failure_count,
            "last_used_at": acc.last_used_at.isoformat() if acc.last_used_at else "Nie",
            "session_active": session_info["active"],
            "session_message": session_info["message"],
            "session_expires": session_info.get("expires", "-"),
            "has_screenshot": has_screenshot
        })
    
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
            "accounts": accounts_info
        }
    )

def make_session_state_from_cookie(cookie_val: str) -> str:
    cookie_val = cookie_val.strip()
    if cookie_val.startswith("reddit_session="):
        cookie_val = cookie_val.split("=", 1)[1]
    cookie_val = cookie_val.split(";")[0].strip()
    
    state = {
        "cookies": [
            {
                "name": "reddit_session",
                "value": cookie_val,
                "domain": ".reddit.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax"
            }
        ]
    }
    return json.dumps(state)

@app.post("/admin/accounts/add")
async def admin_add_account(
    username: str = Form(...),
    password: str = Form(...),
    proxy_url: str = Form(None),
    fallback_proxy_url: str = Form(None),
    reddit_session: str = Form(None),
    admin_user: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    try:
        session_state = None
        if reddit_session and reddit_session.strip():
            session_state = make_session_state_from_cookie(reddit_session)
            
        new_acc = RedditAccount(
            username=username.strip(),
            password=password.strip(),
            proxy_url=proxy_url.strip() if proxy_url else None,
            fallback_proxy_url=fallback_proxy_url.strip() if fallback_proxy_url else None,
            session_state=session_state,
            is_active=True if session_state else False
        )
        db.add(new_acc)
        db.commit()
        return RedirectResponse(url="/admin/dashboard?success=Reddit-Account+erfolgreich+hinzugefuegt!", status_code=303)
    except Exception as e:
        db.rollback()
        return RedirectResponse(url=f"/admin/dashboard?error=Fehler+beim+Hinzufuegen:+{str(e)}", status_code=303)

@app.post("/admin/accounts/{account_id}/delete")
async def admin_delete_account(
    account_id: int,
    admin_user: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    acc = db.query(RedditAccount).filter(RedditAccount.id == account_id).first()
    if acc:
        db.delete(acc)
        db.commit()
        return RedirectResponse(url="/admin/dashboard?success=Account+geloescht", status_code=303)
    return RedirectResponse(url="/admin/dashboard?error=Account+nicht+gefunden", status_code=303)

@app.post("/admin/accounts/{account_id}/toggle")
async def admin_toggle_account(
    account_id: int,
    admin_user: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    acc = db.query(RedditAccount).filter(RedditAccount.id == account_id).first()
    if acc:
        acc.is_active = not acc.is_active
        db.commit()
        return RedirectResponse(url="/admin/dashboard?success=Account-Status+geaendert", status_code=303)
    return RedirectResponse(url="/admin/dashboard?error=Account+nicht+gefunden", status_code=303)

@app.post("/admin/accounts/{account_id}/refresh")
async def admin_refresh_session(
    account_id: int,
    admin_user: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    acc = db.query(RedditAccount).filter(RedditAccount.id == account_id).first()
    if not acc:
        return RedirectResponse(url="/admin/dashboard?error=Account+nicht+gefunden", status_code=303)
        
    try:
        logger.info(f"Führe manuellen Login-Refresh für Account '{acc.username}' durch...")
        # Login über Playwright mit Proxy durchführen
        session_state_json = await login_to_reddit(
            username=acc.username,
            password=acc.password,
            proxy_url=acc.proxy_url
        )
        
        # State in der DB speichern
        acc.session_state = session_state_json
        acc.failure_count = 0
        acc.is_active = True
        db.commit()
        
        return RedirectResponse(url=f"/admin/dashboard?success=Session+fuer+Konto+{acc.username}+erfolgreich+erneuert!", status_code=303)
    except Exception as e:
        logger.error(f"Fehler bei Session-Refresh für {acc.username}: {e}")
        return RedirectResponse(url=f"/admin/dashboard?error=Refresh-Fehler+fuer+{acc.username}:+{str(e)}", status_code=303)
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

@app.get("/admin/accounts/{account_id}/screenshot")
async def admin_account_screenshot(
    account_id: int,
    admin_user: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    acc = db.query(RedditAccount).filter(RedditAccount.id == account_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account nicht gefunden.")
    screenshot_path = f"./app/data/last_error_{acc.username}.png"
    if os.path.exists(screenshot_path):
        return FileResponse(screenshot_path)
    raise HTTPException(status_code=404, detail="Kein Fehler-Screenshot für dieses Konto vorhanden.")

@app.post("/admin/accounts/{account_id}/edit")
async def admin_edit_account(
    account_id: int,
    username: str = Form(...),
    password: str = Form(None),
    proxy_url: str = Form(None),
    fallback_proxy_url: str = Form(None),
    reddit_session: str = Form(None),
    admin_user: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    acc = db.query(RedditAccount).filter(RedditAccount.id == account_id).first()
    if not acc:
        return RedirectResponse(url="/admin/dashboard?error=Account+nicht+gefunden", status_code=303)
        
    try:
        acc.username = username.strip()
        if password and password.strip():
            acc.password = password.strip()
        acc.proxy_url = proxy_url.strip() if proxy_url else None
        acc.fallback_proxy_url = fallback_proxy_url.strip() if fallback_proxy_url else None
        
        if reddit_session and reddit_session.strip():
            acc.session_state = make_session_state_from_cookie(reddit_session)
            acc.is_active = True
            acc.failure_count = 0
            try:
                import os
                screenshot_path = f"./app/data/last_error_{acc.username}.png"
                if os.path.exists(screenshot_path):
                    os.remove(screenshot_path)
            except Exception:
                pass
                
        db.commit()
        return RedirectResponse(url=f"/admin/dashboard?success=Konto+{acc.username}+erfolgreich+aktualisiert!", status_code=303)
    except Exception as e:
        db.rollback()
        return RedirectResponse(url=f"/admin/dashboard?error=Fehler+beim+Aktualisieren+von+{acc.username}:+{str(e)}", status_code=303)


