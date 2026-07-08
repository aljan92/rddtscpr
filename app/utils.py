import os
import json
import logging
import base64
from typing import Optional
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

logger = logging.getLogger("rddtscpr.utils")

security = HTTPBasic()

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    admin_user = os.getenv("ADMIN_USERNAME", "admin")
    admin_pass = os.getenv("ADMIN_PASSWORD", "admin123")
    
    if credentials.username != admin_user or credentials.password != admin_pass:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

SETTINGS_FILE = "./app/data/settings.json"

def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
                if "evomi_api_key" not in data:
                    data["evomi_api_key"] = ""
                return data
        except Exception:
            pass
    return {"rapidapi_proxy_secret": "", "evomi_api_key": ""}

def save_settings(settings: dict):
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    current = load_settings()
    current.update(settings)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(current, f, indent=2)

def get_admin_token() -> str:
    import hashlib
    admin_pass = os.getenv("ADMIN_PASSWORD", "admin123")
    return hashlib.sha256(admin_pass.encode()).hexdigest()

def is_admin_request(request: Request) -> bool:
    # First check X-Admin-Token header to bypass browser basic auth caching limits
    admin_token = request.headers.get("x-admin-token") or request.headers.get("X-Admin-Token")
    if admin_token and admin_token == get_admin_token():
        return True

    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not auth_header:
        return False
    try:
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
    
    # Path-basiertes Secret auswählen (Web vs Reddit)
    path = request.url.path
    if path.startswith("/v1/web/"):
        secret = settings.get("web_rapidapi_proxy_secret", "")
    else:
        secret = settings.get("rapidapi_proxy_secret", "")
        
    # Falls kein Proxy-Secret konfiguriert ist, lassen wir alle Anfragen durchgehen
    if not secret or not secret.strip():
        return

    request_secret = request.headers.get("x-rapidapi-proxy-secret")
    if request_secret != secret:
        raise HTTPException(
            status_code=403,
            detail="Invalid X-RapidAPI-Proxy-Secret header."
        )

