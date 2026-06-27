import json
import logging
from datetime import datetime
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

logger = logging.getLogger("rddtscpr.auth")

def get_session_info_from_state(session_state_str: str) -> dict:
    """
    Gibt Informationen über den in der DB gespeicherten Session-State zurück.
    """
    if not session_state_str:
        return {"active": False, "message": "Keine Session aktiv."}
    
    try:
        state = json.loads(session_state_str)
        cookies = state.get("cookies", [])
        reddit_session_cookie = next((c for c in cookies if c["name"] == "reddit_session"), None)
        
        if reddit_session_cookie:
            expires = reddit_session_cookie.get("expires", "Unbekannt")
            # Falls expires ein UNIX-Timestamp ist, lesbar machen
            if isinstance(expires, (int, float)):
                expires = datetime.fromtimestamp(expires).isoformat()
                
            return {
                "active": True,
                "expires": expires,
                "message": "Session ist aktiv (reddit_session Cookie vorhanden)."
            }
        else:
            return {
                "active": False,
                "message": "Kein reddit_session Cookie im State gefunden."
            }
    except Exception as e:
        return {"active": False, "message": f"Fehler beim Lesen der Session: {str(e)}"}

def get_account_cookies(session_state_str: str) -> dict:
    """
    Konvertiert den JSON-Session-State aus der DB in ein httpx-kompatibles Cookie-Format.
    """
    if not session_state_str:
        return {}
    
    try:
        state = json.loads(session_state_str)
        cookies_dict = {}
        for cookie in state.get("cookies", []):
            if "reddit.com" in cookie["domain"]:
                cookies_dict[cookie["name"]] = cookie["value"]
        return cookies_dict
    except Exception as e:
        logger.error(f"Fehler beim Laden der gespeicherten Cookies: {e}")
        return {}

async def login_to_reddit(username, password, proxy_url=None) -> str:
    """
    Startet Playwright, loggt sich bei Reddit ein und gibt den Session-State als JSON-String zurück.
    """
    if not username or not password:
        raise ValueError("Benutzername und Passwort müssen konfiguriert sein.")
    
    playwright_proxy = None
    if proxy_url:
        from urllib.parse import urlparse
        parsed = urlparse(proxy_url)
        playwright_proxy = {
            "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        }
        if parsed.username and parsed.password:
            playwright_proxy["username"] = parsed.username
            playwright_proxy["password"] = parsed.password

    async with async_playwright() as p:
        browser_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage"
        ]
        
        browser = await p.chromium.launch(
            headless=True,
            proxy=playwright_proxy,
            args=browser_args
        )
        
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="de-DE",
            timezone_id="Europe/Berlin"
        )
        
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)
        
        try:
            logger.info("Rufe Reddit Login-Seite auf...")
            await page.goto("https://www.reddit.com/login", wait_until="domcontentloaded", timeout=45000)
            
            # Cookie-Banner schließen, falls vorhanden
            logger.info("Prüfe auf Cookie-Banner...")
            cookie_selectors = [
                "#onetrust-accept-btn-handler",
                "button:has-text('Alle akzeptieren')",
                "button:has-text('Alle Akzeptieren')",
                "button:has-text('Accept all')",
                "button:has-text('Accept All')",
                "button[aria-label='Close']",
                ".ot-sdk-row button",
                "#accept-recommendations"
            ]
            for cookie_selector in cookie_selectors:
                try:
                    if await page.is_visible(cookie_selector, timeout=2000):
                        await page.click(cookie_selector, force=True)
                        logger.info(f"Cookie-Banner geschlossen via: {cookie_selector}")
                        await page.wait_for_timeout(1000)
                        break
                except Exception as ce:
                    logger.debug(f"Cookie-Selector {cookie_selector} fehlgeschlagen: {ce}")
                    continue

            # Fallback: In allen iframes nach dem Cookie-Banner suchen
            for frame in page.frames:
                if frame == page:
                    continue
                for cookie_selector in ["#onetrust-accept-btn-handler", "button:has-text('Alle akzeptieren')", "button:has-text('Accept all')", "button[aria-label='Close']"]:
                    try:
                        if await frame.is_visible(cookie_selector, timeout=500):
                            await frame.click(cookie_selector, force=True)
                            logger.info(f"Cookie-Banner im iframe geschlossen via: {cookie_selector}")
                            await page.wait_for_timeout(1000)
                            break
                    except Exception:
                        continue

            # Explizit auf das Erscheinen eines Eingabefeldes warten
            try:
                await page.wait_for_selector("input[name='username']", timeout=15000)
            except Exception as e:
                logger.warning(f"Username-Feld nicht gefunden, versuche fortzufahren. Details: {e}")
            
            # Eingabefelder ausfüllen
            username_filled = False
            for selector in ["input[name='username']", "#loginUsername", "#login-username"]:
                try:
                    if await page.is_visible(selector, timeout=2000):
                        await page.fill(selector, username)
                        username_filled = True
                        break
                except Exception:
                    continue
            
            if not username_filled:
                await page.fill("input[placeholder*='Username']", username)
                
            password_filled = False
            for selector in ["input[name='password']", "#loginPassword", "#login-password"]:
                try:
                    if await page.is_visible(selector, timeout=2000):
                        await page.fill(selector, password)
                        password_filled = True
                        break
                except Exception:
                    continue
            
            if not password_filled:
                await page.fill("input[placeholder*='Password']", password)
                
            # Submit Button klicken
            submit_clicked = False
            for selector in ["button[type='submit']", "button:has-text('Log In')", "button:has-text('Anmelden')"]:
                try:
                    await page.click(selector, force=True, timeout=2000)
                    submit_clicked = True
                    logger.info(f"Submit-Button geklickt via: {selector}")
                    break
                except Exception:
                    continue
            
            if not submit_clicked:
                logger.info("Submit-Button nicht direkt klickbar, drücke Enter im Passwortfeld...")
                await page.press("input[name='password']", "Enter")
            
            logger.info("Login-Daten abgeschickt. Warte auf Navigation...")
            await page.wait_for_timeout(8000)
            
            # Überprüfen, ob wir eingeloggt sind
            cookies = await context.cookies()
            reddit_session = any(c["name"] == "reddit_session" for c in cookies)
            
            if reddit_session:
                logger.info("Login erfolgreich! Extrahiere storage state...")
                state = await context.storage_state()
                return json.dumps(state)
            else:
                current_url = page.url
                if "login" not in current_url:
                    logger.info("URL hat sich geändert, vermute erfolgreichen Login. Extrahiere state...")
                    state = await context.storage_state()
                    return json.dumps(state)
                
                # Screenshot für Debugging-Zwecke speichern
                import os
                os.makedirs("./app/data", exist_ok=True)
                debug_screenshot_path = "./app/data/last_error.png"
                await page.screenshot(path=debug_screenshot_path)
                logger.error(f"Login fehlgeschlagen. Screenshot unter {debug_screenshot_path} gespeichert.")
                raise Exception("Anmeldung fehlgeschlagen: Kein Session-Cookie erhalten und keine Umleitung festgestellt.")
                
        finally:
            await page.close()
            await context.close()
            await browser.close()
