import os
import json
import logging
import uuid
import httpx
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db, APIRequestLog, RedditAccount, SystemSetting
from app.reddit_scraper.auth import get_session_info_from_state, login_to_reddit
from app.reddit_scraper.queue_manager import scrape_queue
from app.utils import verify_admin, load_settings, save_settings, get_admin_token

logger = logging.getLogger("rddtscpr.reddit_admin_router")

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")

@router.get("/admin/dashboard", response_class=HTMLResponse)
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
        "reddit/dashboard.html", 
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

def format_proxy_string(proxy_str: str) -> Optional[str]:
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
    parts = proxy_str.split(":")
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
    
    if len(clean_parts) == 4:
        if clean_parts[1].isdigit():
            host, port, user, password = clean_parts
            return f"{scheme}://{user}:{password}@{host}:{port}"
        elif clean_parts[3].isdigit():
            user, password, host, port = clean_parts
            return f"{scheme}://{user}:{password}@{host}:{port}"
        else:
            host, port, user, password = clean_parts
            return f"{scheme}://{user}:{password}@{host}:{port}"
        
    if not has_scheme:
        if "@" in proxy_str:
            return f"http://{proxy_str}"
        return f"http://{proxy_str}"
        
    return f"{scheme}://{proxy_str_clean}"

def make_session_state_from_fields(
    reddit_session: str = None,
    loid: str = None,
    session_tracker: str = None,
    csrf_token: str = None,
    token_v2: str = None
) -> Optional[str]:
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

@router.post("/admin/accounts/add")
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

        if session_state and background_tasks:
            async def run_warmup():
                from app.database import SessionLocal
                with SessionLocal() as s:
                    from app.database import RedditAccount as RA
                    db_acc = s.query(RA).filter(RA.username == new_acc.username).first()
                    if db_acc:
                        await scrape_queue._refresh_account_session(s, db_acc)
            background_tasks.add_task(run_warmup)

        return RedirectResponse(url="/admin/dashboard?success=Reddit-Account+erfolgreich+hinzugefuegt!+Warmlauf-Prozess+wurde+gestartet.", status_code=303)
    except Exception as e:
        db.rollback()
        return RedirectResponse(url=f"/admin/dashboard?error=Fehler+beim+Hinzufuegen:+{str(e)}", status_code=303)

@router.post("/admin/accounts/{account_id}/delete")
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

@router.post("/admin/accounts/{account_id}/toggle")
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

@router.post("/admin/accounts/{account_id}/refresh")
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
        session_state_json = await login_to_reddit(
            username=acc.username,
            password=acc.password,
            proxy_url=acc.proxy_url
        )
        
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

@router.post("/admin/accounts/{account_id}/rotate-session")
async def rotate_account_proxy_session(
    account_id: int,
    admin_user: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    acc = db.query(RedditAccount).filter(RedditAccount.id == account_id).first()
    if not acc:
        return RedirectResponse(url="/admin/dashboard?error=Account+nicht+gefunden", status_code=303)
    
    import random
    import string
    import re
    
    def generate_session_id():
        return "".join(random.choices(string.ascii_uppercase + string.digits, k=9))
    
    new_session_id = generate_session_id()
    
    def replace_session(url, new_id):
        if not url:
            return url
        if "hardsession-" in url:
            return re.sub(r"hardsession-[A-Za-z0-9]+", f"hardsession-{new_id}", url)
        return url

    old_proxy = acc.proxy_url
    old_fallback = acc.fallback_proxy_url
    
    acc.proxy_url = replace_session(acc.proxy_url, new_session_id)
    acc.fallback_proxy_url = replace_session(acc.fallback_proxy_url, new_session_id)
    
    if acc.proxy_url != old_proxy or acc.fallback_proxy_url != old_fallback:
        db.commit()
        return RedirectResponse(url=f"/admin/dashboard?success=Proxy-Session+fuer+{acc.username}+erfolgreich+rotiert!", status_code=303)
    else:
        return RedirectResponse(url="/admin/dashboard?error=Keine+hardsession-Spezifikation+im+Proxy+gefunden", status_code=303)

@router.post("/admin/accounts/{account_id}/check")
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
        acc.is_active = True
        db.commit()
        
        success = await scrape_queue._refresh_account_session(db, acc)
        
        if success:
            return RedirectResponse(url=f"/admin/dashboard?success=Session-Check+fuer+Konto+{acc.username}+erfolgreich!+Session+ist+aktiv+und+Cookies+wurden+aktualisiert.", status_code=303)
        else:
            acc.is_active = False
            db.commit()
            return RedirectResponse(url=f"/admin/dashboard?error=Session-Check+fuer+Konto+{acc.username}+fehlgeschlagen.+Konto+ist+inaktiv+oder+nicht+eingeloggt.", status_code=303)
    except Exception as e:
        logger.error(f"Fehler bei Session-Check für {acc.username}: {e}")
        acc.is_active = False
        db.commit()
        return RedirectResponse(url=f"/admin/dashboard?error=Fehler+beim+Session-Check+fuer+{acc.username}:+{str(e)}", status_code=303)

@router.post("/admin/accounts/{account_id}/check_proxy")
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
        logger.info(f"Prüfe Proxy-Verbindung für Account '{acc.username}' ({acc.proxy_url})...")
        
        proxies = {
            "http://": acc.proxy_url,
            "https://": acc.proxy_url
        }
        
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
                except Exception as service_err:
                    last_error = f"{service_url}: {service_err}"
            
            return RedirectResponse(url=f"/admin/dashboard?error=Proxy-Check+fehlgeschlagen.+Alle+IP-Dienste+nicht+erreichbar.+Letzter+Fehler:+{last_error}", status_code=303)
                
    except Exception as e:
        logger.error(f"Fehler beim Prüfen des Proxys für {acc.username}: {e}")
        fallback_info = " (Ein Fallback-Proxy ist konfiguriert, wurde aber nicht getestet)" if acc.fallback_proxy_url else ""
        return RedirectResponse(url=f"/admin/dashboard?error=Proxy-Verbindung+fehlgeschlagen:+{str(e)}{fallback_info}", status_code=303)

@router.get("/admin/playground", response_class=HTMLResponse)
async def admin_playground(
    request: Request,
    username: str = Depends(verify_admin)
):
    settings = load_settings()
    return templates.TemplateResponse("reddit/playground.html", {
        "request": request,
        "settings": settings,
        "admin_token": get_admin_token()
    })

@router.get("/admin/rapidapi-playground", response_class=HTMLResponse)
async def admin_rapidapi_playground(
    request: Request,
    username: str = Depends(verify_admin)
):
    settings = load_settings()
    return templates.TemplateResponse("reddit/rapidapi_playground.html", {"request": request, "settings": settings})

@router.get("/admin/queue", response_class=HTMLResponse)
async def admin_queue(
    request: Request,
    username: str = Depends(verify_admin)
):
    status = scrape_queue.get_queue_status()
    return templates.TemplateResponse("reddit/queue.html", {
        "request": request,
        "cooldown_seconds": scrape_queue.cooldown_seconds,
        "cooldown_mode": scrape_queue.cooldown_mode,
        "max_accountless_sessions": scrape_queue.max_accountless_sessions,
        "rotating_proxy_url": scrape_queue.rotating_proxy_url,
        "stats": status["stats"],
        "requests": status["requests"]
    })

@router.get("/admin/queue/api")
async def admin_queue_api(
    username: str = Depends(verify_admin)
):
    return scrape_queue.get_queue_status()

@router.post("/admin/queue/settings")
async def admin_queue_settings(
    cooldown_seconds: float = Form(0),
    cooldown_mode: str = Form("fixed"),
    max_accountless_sessions: int = Form(5),
    rotating_proxy_url: str = Form(""),
    username: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    if cooldown_mode not in ("fixed", "auto"):
        return RedirectResponse(url="/admin/queue?error=Ungueltiger+Modus.", status_code=303)
    
    if cooldown_mode == "fixed" and cooldown_seconds < 0.0:
        return RedirectResponse(url="/admin/queue?error=Ungueltiger+Wert+fuer+Pause.", status_code=303)
        
    if max_accountless_sessions < 1:
        return RedirectResponse(url="/admin/queue?error=Die+Anzahl+der+Sessions+muss+mindestens+1+sein.", status_code=303)
    
    scrape_queue.cooldown_mode = cooldown_mode
    if cooldown_mode == "fixed":
        scrape_queue.cooldown_seconds = cooldown_seconds
    scrape_queue.max_accountless_sessions = max_accountless_sessions
    scrape_queue.rotating_proxy_url = rotating_proxy_url.strip()
    
    try:
        setting = db.query(SystemSetting).filter(SystemSetting.key == "cooldown_seconds").first()
        if setting:
            setting.value = str(cooldown_seconds)
        else:
            setting = SystemSetting(key="cooldown_seconds", value=str(cooldown_seconds))
            db.add(setting)
        
        mode_setting = db.query(SystemSetting).filter(SystemSetting.key == "cooldown_mode").first()
        if mode_setting:
            mode_setting.value = cooldown_mode
        else:
            mode_setting = SystemSetting(key="cooldown_mode", value=cooldown_mode)
            db.add(mode_setting)
            
        sessions_setting = db.query(SystemSetting).filter(SystemSetting.key == "max_accountless_sessions").first()
        if sessions_setting:
            sessions_setting.value = str(max_accountless_sessions)
        else:
            sessions_setting = SystemSetting(key="max_accountless_sessions", value=str(max_accountless_sessions))
            db.add(sessions_setting)
            
        proxy_setting = db.query(SystemSetting).filter(SystemSetting.key == "rotating_proxy_url").first()
        if proxy_setting:
            proxy_setting.value = rotating_proxy_url.strip()
        else:
            proxy_setting = SystemSetting(key="rotating_proxy_url", value=rotating_proxy_url.strip())
            db.add(proxy_setting)
        
        db.commit()
        logger.info(f"Queue-Einstellungen aktualisiert: Modus={cooldown_mode}, Sekunden={cooldown_seconds}s, Sessions={max_accountless_sessions}")
    except Exception as e:
        logger.error(f"Fehler beim Speichern der Queue-Einstellungen in DB: {e}")
    
    return RedirectResponse(url="/admin/queue?success=Einstellungen+erfolgreich+gespeichert!", status_code=303)

@router.get("/admin/diagnose-queue")
async def diagnose_queue(
    username: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    try:
        reqs = []
        for rid, r in list(scrape_queue.active_requests.items()):
            reqs.append({
                "id": r.id,
                "action": r.action,
                "status": r.status,
                "attempts": r.attempts,
                "requires_nsfw_account": r.requires_nsfw_account,
                "failed_accounts": list(r.failed_account_ids),
                "account_username": r.account_username
            })
        logs = db.query(APIRequestLog).order_by(APIRequestLog.timestamp.desc()).limit(20).all()
        log_list = []
        for l in logs:
            log_list.append({
                "timestamp": l.timestamp.isoformat() if l.timestamp else None,
                "endpoint": l.endpoint,
                "target": l.target,
                "reddit_username": l.reddit_username,
                "status_code": l.status_code,
                "error_message": l.error_message
            })
        
        accounts = db.query(RedditAccount).filter(RedditAccount.is_active == True).all()
        acc_list = []
        for a in accounts:
            acc_list.append({
                "id": a.id,
                "username": a.username,
                "proxy_url": a.proxy_url,
                "fallback_proxy_url": a.fallback_proxy_url,
                "failure_count": a.failure_count,
                "session_info": get_session_info_from_state(a.session_state)
            })
            
        warmup_status = {}
        for slot, cache in scrape_queue.session_cache.items():
            warmup_status[f"Session {slot}"] = {
                "status": cache.get("status", "unknown"),
                "last_warmed": cache.get("last_warmed").isoformat() if cache.get("last_warmed") else None,
                "last_used": cache.get("last_used").isoformat() if cache.get("last_used") else None,
                "cookies_count": len(json.loads(cache.get("cookies", "{}")).get("cookies", [])) if cache.get("cookies") else 0,
                "proxy_url": cache.get("proxy_url", "")[-30:] if cache.get("proxy_url") else None
            }
        return {
            "max_accountless_sessions": scrape_queue.max_accountless_sessions,
            "rotating_proxy_url_configured": bool(scrape_queue.rotating_proxy_url),
            "rotating_proxy_url": scrape_queue.rotating_proxy_url,
            "busy_session_ids": list(scrape_queue.busy_session_ids),
            "busy_account_ids": list(scrape_queue.busy_account_ids),
            "active_requests_count": len(scrape_queue.active_requests),
            "active_requests": reqs,
            "queue_empty": scrape_queue.queue.empty(),
            "running": scrape_queue._running,
            "session_warmup_cache": warmup_status,
            "recent_logs": log_list,
            "active_accounts": acc_list
        }
    except Exception as e:
        return {"error": str(e)}

@router.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings_get(
    request: Request,
    api: str = "reddit",
    username: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    settings = load_settings()
    
    # Load max workers from DB
    web_scraper_max_workers = 5
    setting = db.query(SystemSetting).filter(SystemSetting.key == "web_scraper_max_workers").first()
    if setting:
        web_scraper_max_workers = int(setting.value)
        
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "settings": settings,
        "rotating_proxy_url": scrape_queue.rotating_proxy_url,
        "cooldown_seconds": scrape_queue.cooldown_seconds,
        "cooldown_mode": scrape_queue.cooldown_mode,
        "max_accountless_sessions": scrape_queue.max_accountless_sessions,
        "web_scraper_max_workers": web_scraper_max_workers,
        "api": api
    })

@router.post("/admin/settings")
async def admin_settings_post(
    api: str = "reddit",
    rapidapi_proxy_secret: str = Form(None),
    rapidapi_key: str = Form(None),
    rapidapi_host: str = Form(None),
    rotating_proxy_url: str = Form(None),
    evomi_api_key: str = Form(None),
    # Web Scraper specific
    web_rapidapi_proxy_secret: str = Form(None),
    web_rapidapi_key: str = Form(None),
    web_rapidapi_host: str = Form(None),
    username: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    # 1. Update JSON settings file
    settings = {
        "rapidapi_proxy_secret": (rapidapi_proxy_secret or "").strip(),
        "rapidapi_key": (rapidapi_key or "").strip(),
        "rapidapi_host": (rapidapi_host or "").strip(),
        "evomi_api_key": (evomi_api_key or "").strip(),
        "web_rapidapi_proxy_secret": (web_rapidapi_proxy_secret or "").strip(),
        "web_rapidapi_key": (web_rapidapi_key or "").strip(),
        "web_rapidapi_host": (web_rapidapi_host or "").strip(),
    }
    save_settings(settings)
    
    # 2. Update rotating proxy in DB & scrape_queue (Reddit)
    if rotating_proxy_url is not None:
        formatted_proxy = format_proxy_string(rotating_proxy_url) or ""
        scrape_queue.rotating_proxy_url = formatted_proxy
        db_setting = db.query(SystemSetting).filter(SystemSetting.key == "rotating_proxy_url").first()
        if db_setting:
            db_setting.value = formatted_proxy
        else:
            db_setting = SystemSetting(key="rotating_proxy_url", value=formatted_proxy)
            db.add(db_setting)
            
    db.commit()
    logger.info(f"System settings updated via unified form. API context: {api}")
    return RedirectResponse(url=f"/admin/settings?api={api}&success=Einstellungen+erfolgreich+gespeichert!", status_code=303)

@router.post("/admin/settings/test-rotating-proxy")
async def test_rotating_proxy(
    rotating_proxy_url: str = Form(...),
    username: str = Depends(verify_admin)
):
    proxy_url = format_proxy_string(rotating_proxy_url)
    if not proxy_url:
        return {"success": False, "error": "Keine Proxy-URL angegeben."}
        
    try:
        from urllib.parse import urlparse
        test_proxy_url = proxy_url
        parsed = urlparse(proxy_url)
        if parsed.username:
            session_suffix = f"_hardsession-test-{uuid.uuid4().hex[:4].upper()}"
            netloc = parsed.netloc
            if '@' in netloc:
                parts = netloc.split('@', 1)
                credentials = parts[0]
                host_port = parts[1]
                if ':' in credentials:
                    user, pw = credentials.split(':', 1)
                    import re
                    clean_pw = re.sub(r"_(?:hard)?session-[A-Za-z0-9]+", "", pw)
                    new_pw = f"{clean_pw}{session_suffix}"
                    netloc = f"{user}:{new_pw}@{host_port}"
                else:
                    new_user = f"{credentials}{session_suffix}"
                    netloc = f"{new_user}@{host_port}"
            test_proxy_url = f"{parsed.scheme}://{netloc}{parsed.path}"
            
        proxies = {
            "http://": test_proxy_url,
            "https://": test_proxy_url
        }
        
        ip_check_services = [
            ("https://api.ipify.org?format=json", lambda r: r.json().get("ip", "Unbekannt")),
            ("https://ifconfig.me/ip", lambda r: r.text.strip()),
            ("https://icanhazip.com", lambda r: r.text.strip()),
        ]
        
        last_error = None
        async with httpx.AsyncClient(proxies=proxies, timeout=10.0) as client:
            for service_url, extract_ip in ip_check_services:
                try:
                    res = await client.get(service_url)
                    if res.status_code == 200:
                        returned_ip = extract_ip(res)
                        return {
                            "success": True, 
                            "message": f"Proxy-Verbindung erfolgreich! Erkannte IP: {returned_ip}"
                        }
                    else:
                        last_error = f"{service_url} antwortete mit Status {res.status_code}"
                except Exception as service_err:
                    last_error = f"{service_url}: {str(service_err)}"
                    
        return {
            "success": False, 
            "error": f"Proxy-Verbindung fehlgeschlagen. Alle Dienste nicht erreichbar. Letzter Fehler: {last_error}"
        }
    except Exception as e:
        logger.error(f"Fehler beim Testen des rotierenden Proxys: {e}")
        return {"success": False, "error": f"Interner Fehler: {str(e)}"}

@router.post("/admin/settings/reset-stats")
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

@router.get("/admin/logs/clear")
async def admin_clear_logs(
    username: str = Depends(verify_admin),
    db: Session = Depends(get_db)
):
    db.query(APIRequestLog).delete()
    db.commit()
    return RedirectResponse(url="/admin/dashboard?success=Logs+erfolgreich+geleert", status_code=303)

@router.get("/admin/accounts/{account_id}/screenshot")
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

@router.post("/admin/accounts/{account_id}/edit")
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
                screenshot_path = f"./app/data/last_error_{acc.username}.png"
                if os.path.exists(screenshot_path):
                    os.remove(screenshot_path)
            except Exception:
                pass
                
        db.commit()

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

@router.get("/admin/evomi-balance")
async def get_evomi_balance_endpoint(
    username: str = Depends(verify_admin)
):
    settings = load_settings()
    api_key = settings.get("evomi_api_key", "").strip()
    if not api_key:
        return {"status": "not_configured", "remaining": None}
        
    try:
        url = "https://api.evomi.com/public/proxy"
        headers = {"x-apikey": api_key}
        async with httpx.AsyncClient(timeout=4.0) as client:
            res = await client.get(url, headers=headers)
            
        if res.status_code == 401:
            return {"status": "unauthorized", "remaining": None}
        elif res.status_code != 200:
            return {"status": "error", "remaining": None}
            
        data = res.json()
        traffic_data = data.get("data", [])
        remaining_gb = None
        
        if isinstance(traffic_data, list):
            for item in traffic_data:
                if not isinstance(item, dict):
                     continue
                prod = str(item.get("product", "")).lower()
                if prod in ["rpc", "rp", "core_residential", "residential"]:
                    traffic = item.get("traffic", {})
                    if isinstance(traffic, dict):
                        rem_bytes = traffic.get("remaining") or traffic.get("left")
                    else:
                        rem_bytes = item.get("remaining") or item.get("traffic_remaining")
                    
                    if rem_bytes is not None:
                        try:
                            remaining_gb = round(float(rem_bytes) / (1024**3), 2)
                        except:
                            pass
                    break
        elif isinstance(traffic_data, dict):
            for prod_key in ["rpc", "rp", "core_residential", "residential"]:
                if prod_key in traffic_data:
                    item = traffic_data[prod_key]
                    if isinstance(item, dict):
                        rem_bytes = item.get("remaining") or item.get("traffic_remaining") or item.get("left")
                        if rem_bytes is not None:
                            try:
                                remaining_gb = round(float(rem_bytes) / (1024**3), 2)
                            except:
                                pass
                        break
                        
        if remaining_gb is not None:
            return {"status": "success", "remaining": remaining_gb}
        return {"status": "success", "remaining": None}
    except Exception as e:
        logger.error(f"Fehler bei Evomi Bandbreiten-Abfrage: {e}")
        return {"status": "error", "remaining": None}
