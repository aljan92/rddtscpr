import os
import json
import time
import logging
import asyncio
from fastapi import FastAPI, Depends, HTTPException, status, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import init_db, get_db, SessionLocal, APIRequestLog, RedditAccount, SystemSetting
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

SETTINGS_FILE = "./app/data/settings.json"

def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"rapidapi_proxy_secret": "", "sandbox_mode": True}

def save_settings(settings: dict):
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)

def is_admin_request(request: Request) -> bool:
    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not auth_header:
        return False
    try:
        import base64
        auth_type, credentials = auth_header.split(" ", 1)
        if auth_type.lower() != "basic":
            return False
        decoded = base64.b64decode(credentials).decode("utf-8")
        username, password = decoded.split(":", 1)
        admin_user = os.getenv("ADMIN_USERNAME", "admin")
        admin_pass = os.getenv("ADMIN_PASSWORD", "admin123")
        return username == admin_user and password == admin_pass
    except Exception:
        return False

def check_rapidapi_access(request: Request):
    # Bypass verification for local playground requests made by admin
    if request.query_params.get("playground") == "true" and is_admin_request(request):
        return

    settings = load_settings()
    sandbox_mode = settings.get("sandbox_mode", True)
    secret = settings.get("rapidapi_proxy_secret", "")
    # Falls kein Proxy-Secret konfiguriert ist, lassen wir alle Anfragen durchgehen
    if not secret or not secret.strip():
        return

    request_secret = request.headers.get("x-rapidapi-proxy-secret")
    is_secret_valid = bool(request_secret == secret)
    
    if sandbox_mode:
        sub = request.headers.get("x-sandbox-subscription") or request.headers.get("x-rapidapi-subscription") or ""
        sub_lower = sub.lower()
        
        is_sub_valid = False
        # Prüfen, ob das Wort basic, pro oder ultra im String enthalten ist
        for plan_name in ["basic", "pro", "ultra"]:
            if plan_name in sub_lower:
                is_sub_valid = True
                break
        
        if not (is_sub_valid or is_secret_valid):
            raise HTTPException(
                status_code=403,
                detail="Invalid X-RapidAPI-Proxy-Secret or invalid X-RapidAPI-Subscription plan."
            )
    else:
        if not is_secret_valid:
            raise HTTPException(
                status_code=403,
                detail="Invalid X-RapidAPI-Proxy-Secret header."
            )

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
# HEALTH CHECK & ROOT
# =====================================================================

@app.get("/ping", tags=["Health"])
async def health_check():
    """Public health check endpoint – used by RapidAPI and monitoring tools."""
    return {"status": "ok", "service": "Reddit Scraper API", "version": "1.0.0"}

@app.get("/", include_in_schema=False)
async def root_redirect():
    return RedirectResponse(url="/admin/dashboard")

# =====================================================================
# PUBLIC API ENDPOINTS (Routet über Queue)
# =====================================================================

@app.get("/v1/subreddit-posts")
async def api_subreddit_posts(
    request: Request,
    target: str,
    sort: str = "hot",
    timeframe: str = "day",
    limit: int = 10
):
    """
    Extrahiert Posts aus einem bestimmten Subreddit.
    """
    check_rapidapi_access(request)

    # Sortierung aus URL extrahieren und überschreiben, falls vorhanden (z.B. r/NudeGermans/rising -> rising)
    if "reddit.com" in target or "r/" in target:
        import re
        match = re.search(r"r/[^/?#]+/([a-zA-Z]+)", target)
        if match:
            url_sort = match.group(1)
            if url_sort in ["hot", "new", "top", "rising"]:
                sort = url_sort
                logger.info(f"Sortierung aus URL extrahiert und überschrieben: {sort}")

    if sort not in ["hot", "new", "top", "rising"]:
        raise HTTPException(status_code=400, detail="Invalid 'sort' value. Allowed: hot, new, top, rising")
    if timeframe not in ["hour", "day", "week", "month", "year", "all"]:
        raise HTTPException(status_code=400, detail="Invalid 'timeframe' value. Allowed: hour, day, week, month, year, all")
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="Limit must be between 1 and 100.")

    is_playground = request.query_params.get("playground") == "true" and is_admin_request(request)
    start_time = time.time()
    method_used = "json"
    proxy_used = "Dynamisch"
    
    try:
        posts, method_used, username_used = await scrape_queue.enqueue(
            action="subreddit",
            params={
                "target": target,
                "sort": sort,
                "timeframe": timeframe,
                "limit": limit
            },
            is_playground=is_playground
        )
        
        duration = int((time.time() - start_time) * 1000)
        
        # In DB loggen
        if not is_playground:
            with SessionLocal() as db:
                log_entry = APIRequestLog(
                    endpoint="/v1/subreddit-posts",
                    target=target,
                    status_code=200,
                    response_time_ms=duration,
                    method_used=method_used,
                    proxy_used=proxy_used,
                    reddit_username=username_used
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
    except asyncio.CancelledError:
        duration = int((time.time() - start_time) * 1000)
        logger.warning(f"Request abgebrochen/Timeout bei Subreddit-Scraping ({target}) nach {duration}ms")
        if not is_playground:
            with SessionLocal() as db:
                log_entry = APIRequestLog(
                    endpoint="/v1/subreddit-posts",
                    target=target,
                    status_code=499,
                    response_time_ms=duration,
                    method_used=method_used,
                    proxy_used=proxy_used,
                    error_message="Request wurde vom Client abgebrochen oder lief in ein Timeout."
                )
                db.add(log_entry)
                db.commit()
        raise
    except ValueError as ve:
        duration = int((time.time() - start_time) * 1000)
        error_msg = str(ve)
        logger.warning(f"Client-Fehler bei Subreddit-Scraping ({target}): {error_msg}")
        
        if not is_playground:
            with SessionLocal() as db:
                log_entry = APIRequestLog(
                    endpoint="/v1/subreddit-posts",
                    target=target,
                    status_code=404,
                    response_time_ms=duration,
                    method_used=method_used,
                    proxy_used=proxy_used,
                    error_message=error_msg
                )
                db.add(log_entry)
                db.commit()
        
        raise HTTPException(
            status_code=404,
            detail={"error": "Client error", "message": error_msg}
        )
    except Exception as e:
        duration = int((time.time() - start_time) * 1000)
        error_msg = str(e)
        logger.error(f"Fehler bei Subreddit-Scraping ({target}): {error_msg}")
        
        if not is_playground:
            with SessionLocal() as db:
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
            detail={"error": "Scraping error", "message": error_msg}
        )

@app.get("/v1/post-comments")
async def api_post_comments(
    request: Request,
    post_url: str,
    sort: str = "confidence",
    limit: int = 10,
    include_replies: bool = False,
    load_more: bool = False
):
    """
    Extrahiert Kommentare aus einem bestimmten Reddit-Post.
    """
    check_rapidapi_access(request)

    if load_more:
        settings = load_settings()
        subscription = request.headers.get("x-rapidapi-subscription")
        if settings.get("sandbox_mode", True) and not subscription:
            subscription = request.headers.get("x-sandbox-subscription")
            
        sub_str = (subscription or "").lower()
        
        # Bestimmen, ob wir Pro oder Ultra haben
        is_premium = ("pro" in sub_str) or ("ultra" in sub_str)
        
        if not is_premium:
            raise HTTPException(
                status_code=403,
                detail="The 'load_more' feature is restricted to Pro and Ultra plans. Please upgrade your subscription."
            )

    if sort not in ["confidence", "top", "new", "controversial", "old", "qa"]:
        raise HTTPException(status_code=400, detail="Invalid 'sort' value. Allowed: confidence, top, new, controversial, old, qa")
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="Limit must be between 1 and 100.")
    if not post_url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid post URL. URL must start with http/https.")

    is_playground = request.query_params.get("playground") == "true" and is_admin_request(request)
    start_time = time.time()
    method_used = "json"
    proxy_used = "Dynamisch"
    
    try:
        comments, method_used, username_used = await scrape_queue.enqueue(
            action="comments",
            params={
                "post_url": post_url,
                "sort": sort,
                "limit": limit,
                "include_replies": include_replies,
                "load_more": load_more
            },
            is_playground=is_playground
        )
        
        duration = int((time.time() - start_time) * 1000)
        
        if not is_playground:
            with SessionLocal() as db:
                log_entry = APIRequestLog(
                    endpoint="/v1/post-comments",
                    target=post_url,
                    status_code=200,
                    response_time_ms=duration,
                    method_used=method_used,
                    proxy_used=proxy_used,
                    reddit_username=username_used
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
    except asyncio.CancelledError:
        duration = int((time.time() - start_time) * 1000)
        logger.warning(f"Request abgebrochen/Timeout bei Kommentar-Scraping ({post_url}) nach {duration}ms")
        if not is_playground:
            with SessionLocal() as db:
                log_entry = APIRequestLog(
                    endpoint="/v1/post-comments",
                    target=post_url,
                    status_code=499,
                    response_time_ms=duration,
                    method_used=method_used,
                    proxy_used=proxy_used,
                    error_message="Request wurde vom Client abgebrochen oder lief in ein Timeout."
                )
                db.add(log_entry)
                db.commit()
        raise
    except ValueError as ve:
        duration = int((time.time() - start_time) * 1000)
        error_msg = str(ve)
        logger.warning(f"Client-Fehler bei Kommentar-Scraping: {error_msg}")
        
        if not is_playground:
            with SessionLocal() as db:
                log_entry = APIRequestLog(
                    endpoint="/v1/post-comments",
                    target=post_url,
                    status_code=404,
                    response_time_ms=duration,
                    method_used=method_used,
                    proxy_used=proxy_used,
                    error_message=error_msg
                )
                db.add(log_entry)
                db.commit()
        
        raise HTTPException(
            status_code=404,
            detail={"error": "Client error", "message": error_msg}
        )
    except Exception as e:
        duration = int((time.time() - start_time) * 1000)
        error_msg = str(e)
        logger.error(f"Fehler bei Kommentar-Scraping: {error_msg}")
        
        if not is_playground:
            with SessionLocal() as db:
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
            detail={"error": "Scraping error", "message": error_msg}
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
    client_error_requests = db.query(APIRequestLog).filter(APIRequestLog.status_code.between(400, 498)).count()
    timeout_requests = db.query(APIRequestLog).filter(APIRequestLog.status_code == 499).count()
    api_error_requests = db.query(APIRequestLog).filter(APIRequestLog.status_code >= 500).count()
    
    success_rate = (success_requests / (success_requests + api_error_requests + timeout_requests) * 100) if (success_requests + api_error_requests + timeout_requests) > 0 else 100.0
    
    # Durchschnittliche Antwortzeit (nur erfolgreiche)
    avg_duration = db.query(APIRequestLog).filter(APIRequestLog.status_code == 200)
    avg_duration_ms = 0
    if success_requests > 0:
        durations = [log.response_time_ms for log in avg_duration.all()]
        avg_duration_ms = int(sum(durations) / len(durations))
        
    json_count = db.query(APIRequestLog).filter(APIRequestLog.method_used == "json").count()
    playwright_count = db.query(APIRequestLog).filter(APIRequestLog.method_used == "playwright").count()
    
    # Auslastung berechnen
    current_load, max_24h_load = scrape_queue.calculate_system_load()
    avg_wait = sum(scrape_queue.wait_times) / len(scrape_queue.wait_times) if scrape_queue.wait_times else 0.0
    
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
            "request_count": acc.request_count or 0,
            "last_used_at": acc.last_used_at.isoformat() if acc.last_used_at else "Nie",
            "session_active": session_info["active"],
            "session_message": session_info["message"],
            "session_expires": session_info.get("expires", "-"),
            "has_screenshot": has_screenshot,
            "screenshot_viewed": getattr(acc, "screenshot_viewed", True)
        })
    
    stats = {
        "total": total_requests,
        "success": success_requests,
        "client_errors": client_error_requests,
        "timeouts": timeout_requests,
        "api_errors": api_error_requests,
        "success_rate": f"{success_rate:.1f}%",
        "avg_duration_ms": avg_duration_ms,
        "json_count": json_count,
        "playwright_count": playwright_count,
        "current_load": round(current_load, 1),
        "max_24h_load": round(max_24h_load, 1),
        "avg_wait_seconds": round(avg_wait, 1)
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
    """Legacy: Aus einem einzelnen Cookie-String (name=val; ...) einen Session-State bauen."""
    cookie_val = cookie_val.strip()
    cookies_list = []
    
    if "=" in cookie_val:
        parts = cookie_val.split(";")
        for part in parts:
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, val = part.split("=", 1)
            name = name.strip()
            val = val.strip()
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            cookies_list.append({
                "name": name,
                "value": val,
                "domain": ".reddit.com",
                "path": "/",
                "expires": -1,
                "httpOnly": name in ["reddit_session", "token_v2"],
                "secure": True,
                "sameSite": "Lax"
            })
            
    if not cookies_list:
        cookies_list.append({
            "name": "reddit_session",
            "value": cookie_val,
            "domain": ".reddit.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax"
        })
        
    return json.dumps({"cookies": cookies_list})

def format_proxy_string(proxy_str: str) -> str | None:
    """
    Formatiert einen Proxy-String in das von httpx verlangte Format.
    Erkennt und konvertiert das Format host:port:user:pass in http://user:pass@host:port.
    Fügt automatisch http:// als Schema hinzu, falls es fehlt.
    """
    if not proxy_str:
        return None
        
    proxy_str = proxy_str.strip()
    if not proxy_str:
        return None
        
    # Entferne eventuelle Anführungszeichen
    if (proxy_str.startswith('"') and proxy_str.endswith('"')) or (proxy_str.startswith("'") and proxy_str.endswith("'")):
        proxy_str = proxy_str[1:-1].strip()

    # Fall: host:port:user:pass (Evomi Standard-Format aus Zwischenablage)
    # z.B. gate.evomi.com:1000:username:passwort
    parts = proxy_str.split(":")
    # Falls das Schema 'http://' oder 'https://' vorne steht, entfernen wir es temporär zum Parsen der Doppelpunkte
    has_scheme = False
    scheme = "http"
    if proxy_str.lower().startswith("http://"):
        proxy_str_clean = proxy_str[7:]
        has_scheme = True
    elif proxy_str.lower().startswith("https://"):
        proxy_str_clean = proxy_str[8:]
        has_scheme = True
        scheme = "https"
    else:
        proxy_str_clean = proxy_str
        
    clean_parts = proxy_str_clean.split(":")
    
    # host:port:user:pass hat genau 4 Teile (z.B. gate.evomi.com, 1000, user, pass)
    if len(clean_parts) == 4:
        host, port, user, password = clean_parts
        return f"http://{user}:{password}@{host}:{port}"
        
    # Falls kein Schema vorhanden ist, aber das Format ansonsten okay ist, http:// davorschalten
    if not has_scheme:
        # Falls es user:pass@host:port ist
        if "@" in proxy_str:
            return f"http://{proxy_str}"
        # Falls es nur host:port ist
        return f"http://{proxy_str}"
        
    return proxy_str

def make_session_state_from_fields(
    reddit_session: str = None,
    loid: str = None,
    session_tracker: str = None,
    csrf_token: str = None,
    token_v2: str = None
) -> str | None:
    """Aus bis zu 5 einzelnen Cookie-Feldern einen vollständigen Session-State bauen."""
    httponly_names = {"reddit_session", "token_v2"}
    field_map = {
        "reddit_session": reddit_session,
        "loid": loid,
        "session_tracker": session_tracker,
        "csrf_token": csrf_token,
        "token_v2": token_v2,
    }
    cookies_list = []
    for name, val in field_map.items():
        if val and val.strip():
            cookies_list.append({
                "name": name,
                "value": val.strip(),
                "domain": ".reddit.com",
                "path": "/",
                "expires": -1,
                "httpOnly": name in httponly_names,
                "secure": True,
                "sameSite": "Lax"
            })
    if not cookies_list:
        return None
    return json.dumps({"cookies": cookies_list})

@app.post("/admin/accounts/add")
async def admin_add_account(
    username: str = Form(...),
    password: str = Form(...),
    proxy_url: str = Form(None),
    fallback_proxy_url: str = Form(None),
    cookie_reddit_session: str = Form(None),
    cookie_loid: str = Form(None),
    cookie_session_tracker: str = Form(None),
    cookie_csrf_token: str = Form(None),
    cookie_token_v2: str = Form(None),
    cookie_combined: str = Form(None),
    admin_user: str = Depends(verify_admin),
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None
):
    try:
        session_state = None
        # Falls der Benutzer den kombinierten Cookie-String benutzt hat
        if cookie_combined and cookie_combined.strip():
            session_state = make_session_state_from_cookie(cookie_combined)
        else:
            session_state = make_session_state_from_fields(
                reddit_session=cookie_reddit_session,
                loid=cookie_loid,
                session_tracker=cookie_session_tracker,
                csrf_token=cookie_csrf_token,
                token_v2=cookie_token_v2
            )
            
        new_acc = RedditAccount(
            username=username.strip(),
            password=password.strip(),
            proxy_url=format_proxy_string(proxy_url),
            fallback_proxy_url=format_proxy_string(fallback_proxy_url),
            session_state=session_state,
            is_active=True if session_state else False
        )
        db.add(new_acc)
        db.commit()

        # Warmlauf im Hintergrund triggern, falls Cookies angegeben wurden
        if session_state and background_tasks:
            # Da die Queue ein async Refresh hat, verpacken wir das in ein Task-Wrapper
            async def run_warmup():
                from app.database import SessionLocal
                with SessionLocal() as s:
                    # Frischen Account-State laden
                    from app.database import RedditAccount as RA
                    db_acc = s.query(RA).filter(RA.username == new_acc.username).first()
                    if db_acc:
                        await scrape_queue._refresh_account_session(s, db_acc)
            background_tasks.add_task(run_warmup)

        return RedirectResponse(url="/admin/dashboard?success=Reddit-Account+erfolgreich+hinzugefuegt!+Warmlauf-Prozess+wurde+gestartet.", status_code=303)
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
        acc.screenshot_viewed = True
        db.commit()
        
        return RedirectResponse(url=f"/admin/dashboard?success=Session+fuer+Konto+{acc.username}+erfolgreich+erneuert!", status_code=303)
    except Exception as e:
        logger.error(f"Fehler bei Session-Refresh für {acc.username}: {e}")
        acc.screenshot_viewed = False
        db.commit()
        return RedirectResponse(url=f"/admin/dashboard?error=Refresh-Fehler+fuer+{acc.username}:+{str(e)}", status_code=303)

@app.post("/admin/accounts/{account_id}/check")
async def admin_check_account_session(
    account_id: int,
    admin_user: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    acc = db.query(RedditAccount).filter(RedditAccount.id == account_id).first()
    if not acc:
        return RedirectResponse(url="/admin/dashboard?error=Account+nicht+gefunden", status_code=303)
        
    try:
        logger.info(f"Führe manuellen Session-Check für Account '{acc.username}' durch...")
        from app.queue_manager import scrape_queue
        
        # Account vor dem Test aktivieren
        acc.is_active = True
        db.commit()
        
        # Session-Check ausführen (prüft r/popular.json, erneuert Cookies, und führt Auto-Login aus falls abgelaufen)
        success = await scrape_queue._refresh_account_session(db, acc)
        
        if success:
            return RedirectResponse(url=f"/admin/dashboard?success=Session-Check+fuer+Konto+{acc.username}+erfolgreich!+Session+ist+aktiv+und+Cookies+wurden+aktualisiert.", status_code=303)
        else:
            # Bei Fehlschlag Account deaktivieren
            acc.is_active = False
            db.commit()
            return RedirectResponse(url=f"/admin/dashboard?error=Session-Check+fuer+Konto+{acc.username}+fehlgeschlagen.+Konto+ist+inaktiv+oder+nicht+eingeloggt.", status_code=303)
    except Exception as e:
        logger.error(f"Fehler bei Session-Check für {acc.username}: {e}")
        acc.is_active = False
        db.commit()
        return RedirectResponse(url=f"/admin/dashboard?error=Fehler+beim+Session-Check+fuer+{acc.username}:+{str(e)}", status_code=303)

@app.post("/admin/accounts/{account_id}/check_proxy")
async def admin_check_account_proxy(
    account_id: int,
    admin_user: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    acc = db.query(RedditAccount).filter(RedditAccount.id == account_id).first()
    if not acc:
        return RedirectResponse(url="/admin/dashboard?error=Account+nicht+gefunden", status_code=303)
        
    if not acc.proxy_url:
        return RedirectResponse(url=f"/admin/dashboard?error=Konto+{acc.username}+hat+keinen+Proxy+konfiguriert.+Anfragen+laufen+direkt+ueber+den+VPS.", status_code=303)
        
    try:
        import httpx
        logger.info(f"Prüfe Proxy-Verbindung für Account '{acc.username}' ({acc.proxy_url})...")
        
        # Testen mit dem Haupt-Proxy
        proxies = {
            "http://": acc.proxy_url,
            "https://": acc.proxy_url
        }
        
        # Mehrere IP-Echo-Dienste als Fallback (httpbin.org ist notorisch unzuverlässig)
        ip_check_services = [
            ("https://api.ipify.org?format=json", lambda r: r.json().get("ip", "Unbekannt")),
            ("https://ifconfig.me/ip", lambda r: r.text.strip()),
            ("https://icanhazip.com", lambda r: r.text.strip()),
            ("https://httpbin.org/ip", lambda r: r.json().get("origin", "Unbekannt")),
        ]
        
        async with httpx.AsyncClient(proxies=proxies, timeout=15.0) as client:
            last_error = None
            for service_url, extract_ip in ip_check_services:
                try:
                    res = await client.get(service_url)
                    if res.status_code == 200:
                        returned_ip = extract_ip(res)
                        logger.info(f"Proxy-Check erfolgreich via {service_url}. Erkannte IP: {returned_ip}")
                        return RedirectResponse(url=f"/admin/dashboard?success=Proxy-Verbindung+erfolgreich!+Deine+Proxy-IP+ist:+{returned_ip}", status_code=303)
                    else:
                        last_error = f"{service_url} antwortete mit Status {res.status_code}"
                        logger.warning(f"Proxy-Check: {last_error}, versuche nächsten Dienst...")
                except Exception as service_err:
                    last_error = f"{service_url}: {service_err}"
                    logger.warning(f"Proxy-Check: {last_error}, versuche nächsten Dienst...")
            
            # Alle Dienste fehlgeschlagen
            return RedirectResponse(url=f"/admin/dashboard?error=Proxy-Check+fehlgeschlagen.+Alle+IP-Dienste+nicht+erreichbar.+Letzter+Fehler:+{last_error}", status_code=303)
                
    except Exception as e:
        logger.error(f"Fehler beim Prüfen des Proxys für {acc.username}: {e}")
        # Falls Fallback vorhanden, darauf hinweisen
        fallback_info = " (Ein Fallback-Proxy ist konfiguriert, wurde aber nicht getestet)" if acc.fallback_proxy_url else ""
        return RedirectResponse(url=f"/admin/dashboard?error=Proxy-Verbindung+fehlgeschlagen:+{str(e)}{fallback_info}", status_code=303)
@app.get("/admin/playground", response_class=HTMLResponse)
async def admin_playground(
    request: Request,
    username: str = Depends(verify_admin)
):
    settings = load_settings()
    return templates.TemplateResponse("playground.html", {"request": request, "settings": settings})

@app.get("/admin/rapidapi-playground", response_class=HTMLResponse)
async def admin_rapidapi_playground(
    request: Request,
    username: str = Depends(verify_admin)
):
    settings = load_settings()
    return templates.TemplateResponse("rapidapi_playground.html", {"request": request, "settings": settings})

@app.get("/admin/queue", response_class=HTMLResponse)
async def admin_queue(
    request: Request,
    username: str = Depends(verify_admin)
):
    status = scrape_queue.get_queue_status()
    return templates.TemplateResponse("queue.html", {
        "request": request,
        "cooldown_seconds": scrape_queue.cooldown_seconds,
        "stats": status["stats"],
        "requests": status["requests"]
    })

@app.get("/admin/queue/api")
async def admin_queue_api(
    username: str = Depends(verify_admin)
):
    return scrape_queue.get_queue_status()

@app.post("/admin/queue/settings")
async def admin_queue_settings(
    cooldown_seconds: float = Form(...),
    username: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    if cooldown_seconds < 0.0:
        return RedirectResponse(url="/admin/queue?error=Ungueltiger+Wert+fuer+Pause.", status_code=303)
    
    # In Memory aktualisieren
    scrape_queue.cooldown_seconds = cooldown_seconds
    
    # In Datenbank persistieren
    try:
        setting = db.query(SystemSetting).filter(SystemSetting.key == "cooldown_seconds").first()
        if setting:
            setting.value = str(cooldown_seconds)
        else:
            setting = SystemSetting(key="cooldown_seconds", value=str(cooldown_seconds))
            db.add(setting)
        db.commit()
        logger.info(f"Cooldown-Sekunden in DB auf {cooldown_seconds}s aktualisiert.")
    except Exception as e:
        logger.error(f"Fehler beim Speichern von cooldown_seconds in DB: {e}")
        
    return RedirectResponse(url=f"/admin/queue?success=Pause+erfolgreich+auf+{cooldown_seconds}s+geaendert!", status_code=303)

@app.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings_get(
    request: Request,
    username: str = Depends(verify_admin)
):
    settings = load_settings()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "settings": settings
    })

@app.post("/admin/settings")
async def admin_settings_post(
    rapidapi_proxy_secret: str = Form(None),
    sandbox_mode: bool = Form(False),
    rapidapi_key: str = Form(None),
    rapidapi_host: str = Form(None),
    username: str = Depends(verify_admin)
):
    current = load_settings()
    settings = {
        "rapidapi_proxy_secret": (rapidapi_proxy_secret or "").strip(),
        "sandbox_mode": sandbox_mode,
        "rapidapi_key": (rapidapi_key or "").strip(),
        "rapidapi_host": (rapidapi_host or "").strip()
    }
    save_settings(settings)
    logger.info(f"System-Settings aktualisiert: Sandbox Mode = {sandbox_mode}")
    return RedirectResponse(url="/admin/settings?success=Einstellungen+erfolgreich+gespeichert!", status_code=303)

@app.post("/admin/settings/reset-stats")
async def admin_settings_reset_stats(
    username: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    db.query(APIRequestLog).delete()
    accounts = db.query(RedditAccount).all()
    for acc in accounts:
        acc.request_count = 0
    db.commit()
    logger.info("Statistiken (Logs & Request-Counts) über Admin-Settings zurückgesetzt.")
    return RedirectResponse(url="/admin/settings?success=Statistiken+erfolgreich+zurueckgesetzt!", status_code=303)

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
        acc.screenshot_viewed = True
        db.commit()
        return FileResponse(screenshot_path)
    raise HTTPException(status_code=404, detail="Kein Fehler-Screenshot für dieses Konto vorhanden.")

@app.post("/admin/accounts/{account_id}/edit")
async def admin_edit_account(
    account_id: int,
    username: str = Form(...),
    password: str = Form(None),
    proxy_url: str = Form(None),
    fallback_proxy_url: str = Form(None),
    cookie_reddit_session: str = Form(None),
    cookie_loid: str = Form(None),
    cookie_session_tracker: str = Form(None),
    cookie_csrf_token: str = Form(None),
    cookie_token_v2: str = Form(None),
    cookie_combined: str = Form(None),
    admin_user: str = Depends(verify_admin),
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None
):
    acc = db.query(RedditAccount).filter(RedditAccount.id == account_id).first()
    if not acc:
        return RedirectResponse(url="/admin/dashboard?error=Account+nicht+gefunden", status_code=303)
        
    try:
        acc.username = username.strip()
        if password and password.strip():
            acc.password = password.strip()
        acc.proxy_url = format_proxy_string(proxy_url)
        acc.fallback_proxy_url = format_proxy_string(fallback_proxy_url)
        
        new_state = None
        if cookie_combined and cookie_combined.strip():
            new_state = make_session_state_from_cookie(cookie_combined)
        else:
            new_state = make_session_state_from_fields(
                reddit_session=cookie_reddit_session,
                loid=cookie_loid,
                session_tracker=cookie_session_tracker,
                csrf_token=cookie_csrf_token,
                token_v2=cookie_token_v2
            )
        if new_state:
            acc.session_state = new_state
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

        # Warmlauf im Hintergrund triggern, falls neue Cookies angegeben wurden
        if new_state and background_tasks:
            async def run_warmup():
                from app.database import SessionLocal
                with SessionLocal() as s:
                    from app.database import RedditAccount as RA
                    db_acc = s.query(RA).filter(RA.id == account_id).first()
                    if db_acc:
                        await scrape_queue._refresh_account_session(s, db_acc)
            background_tasks.add_task(run_warmup)

        return RedirectResponse(url=f"/admin/dashboard?success=Konto+{acc.username}+erfolgreich+aktualisiert!+Warmlauf-Prozess+wurde+gestartet.", status_code=303)
    except Exception as e:
        db.rollback()
        return RedirectResponse(url=f"/admin/dashboard?error=Fehler+beim+Aktualisieren+von+{acc.username}:+{str(e)}", status_code=303)


