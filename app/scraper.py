import os
import json
import logging
import httpx
from urllib.parse import urlparse
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from app.auth import get_stored_cookies, STATE_FILE_PATH

logger = logging.getLogger("rddtscpr.scraper")

# HTTP Header, um wie ein echter Browser zu wirken
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "de,en-US;q=0.7,en;q=0.3"
}

def clean_url(url: str) -> str:
    """Schneidet Query-Parameter von einer URL ab."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

def build_subreddit_url(target: str, sort: str, timeframe: str) -> str:
    """Konstruiert die Reddit-Subreddit-URL."""
    # Bereinigung des targets
    if "reddit.com" in target:
        # Target ist bereits eine URL, base URL extrahieren und aufräumen
        base_url = clean_url(target)
        if not base_url.endswith("/"):
            base_url += "/"
    else:
        # Target ist nur der Subreddit-Name
        base_url = f"https://www.reddit.com/r/{target.strip('/')}/"
    
    # Sortierung anhängen
    url = f"{base_url}{sort}/"
    return url

# =====================================================================
# METOHDE 1: Der .json-Trick (Schnell & Ressourcenschonend)
# =====================================================================

async def scrape_subreddit_posts_json(target: str, sort: str, timeframe: str, limit: int, proxy: str = None) -> list:
    """
    Holt Posts eines Subreddits über den .json-Trick.
    """
    base_url = build_subreddit_url(target, sort, timeframe)
    json_url = f"{base_url.rstrip('/')}.json"
    
    # Query Parameter
    params = {"limit": limit}
    if sort == "top" and timeframe:
        params["t"] = timeframe
        
    cookies = get_stored_cookies()
    
    # Proxy-Konfiguration
    proxies = {"all://": proxy} if proxy else None
    
    logger.info(f"JSON-Trick: Rufe URL auf: {json_url} mit Params {params}")
    
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, cookies=cookies, proxies=proxies, timeout=15.0, follow_redirects=True) as client:
        response = await client.get(json_url, params=params)
        
        if response.status_code != 200:
            raise Exception(f"Reddit-API lieferte Status Code {response.status_code}")
            
        payload = response.json()
        posts = []
        
        children = payload.get("data", {}).get("children", [])
        for child in children[:limit]:
            data = child.get("data", {})
            # Nur echte Posts berücksichten (t3)
            if child.get("kind") != "t3":
                continue
                
            posts.append({
                "title": data.get("title"),
                "description": data.get("selftext", ""),
                "post_url": f"https://www.reddit.com{data.get('permalink')}",
                "upvotes": data.get("ups", 0),
                "comment_count": data.get("num_comments", 0),
                "author": data.get("author", "[deleted]")
            })
            
        return posts

async def scrape_post_comments_json(post_url: str, sort: str, limit: int, proxy: str = None) -> list:
    """
    Holt Kommentare eines Posts über den .json-Trick.
    """
    clean_post_url = clean_url(post_url)
    json_url = f"{clean_post_url.rstrip('/')}.json"
    
    params = {"sort": sort, "limit": limit}
    cookies = get_stored_cookies()
    proxies = {"all://": proxy} if proxy else None
    
    logger.info(f"JSON-Trick: Rufe URL auf: {json_url} mit Params {params}")
    
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, cookies=cookies, proxies=proxies, timeout=15.0, follow_redirects=True) as client:
        response = await client.get(json_url, params=params)
        
        if response.status_code != 200:
            raise Exception(f"Reddit-API lieferte Status Code {response.status_code}")
            
        payload = response.json()
        
        # Die Response bei Kommentaren besteht aus einer Liste: [Post-Daten, Kommentar-Daten]
        if not isinstance(payload, list) or len(payload) < 2:
            raise Exception("Unerwartetes JSON-Format von Reddit für Kommentare.")
            
        comments_payload = payload[1]
        children = comments_payload.get("data", {}).get("children", [])
        
        comments = []
        for child in children[:limit]:
            data = child.get("data", {})
            if child.get("kind") != "t1":  # t1 = Kommentar
                continue
                
            parent_id = data.get("parent_id", "")
            is_reply = not parent_id.startswith("t3_")  # Wenn parent_id nicht mit t3_ (Post) anfängt, ist es ein Reply
            
            comments.append({
                "comment_text": data.get("body", ""),
                "upvotes": data.get("ups", 0),
                "author": data.get("author", "[deleted]"),
                "is_reply": is_reply
            })
            
        return comments

# =====================================================================
# METODE 2: Playwright-Stealth (Robuster Browser-Fallback)
# =====================================================================

async def launch_browser(p, proxy_url=None):
    """Hilfsfunktion zum Starten des Playwright-Browsers mit Stealth-Profil."""
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
    
    # Falls Session existiert, laden
    context_options = {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "viewport": {"width": 1280, "height": 800},
        "locale": "de-DE",
        "timezone_id": "Europe/Berlin"
    }
    
    if os.path.exists(STATE_FILE_PATH):
        context_options["storage_state"] = STATE_FILE_PATH
        logger.info("Playwright: Nutze gespeicherte Session-Cookies.")
        
    context = await browser.new_context(**context_options)
    page = await context.new_page()
    await Stealth().apply_stealth_async(page)
    
    return browser, context, page

async def scrape_subreddit_posts_playwright(target: str, sort: str, timeframe: str, limit: int, proxy: str = None) -> list:
    """
    Holt Posts eines Subreddits über Playwright (Browser rendering).
    """
    url = build_subreddit_url(target, sort, timeframe)
    if sort == "top" and timeframe:
        url = f"{url}?t={timeframe}"
        
    logger.info(f"Playwright: Öffne URL {url}")
    
    async with async_playwright() as p:
        browser, context, page = await launch_browser(p, proxy)
        try:
            # Zu Reddit navigieren
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)  # Kurz warten, um JS-Rendering zu erlauben
            
            # Subreddit-Posts über shreddit-post Komponenten holen
            posts = []
            post_elements = await page.query_selector_all("shreddit-post")
            
            for elem in post_elements[:limit]:
                try:
                    title = await elem.get_attribute("post-title") or ""
                    permalink = await elem.get_attribute("permalink") or ""
                    author = await elem.get_attribute("author") or "[deleted]"
                    score = await elem.get_attribute("score") or "0"
                    comment_count = await elem.get_attribute("comment-count") or "0"
                    
                    # Beschreibung (Teaser) extrahieren
                    # Meistens in einem Div mit slot="text-body" oder im inneren Text
                    description = ""
                    text_elem = await elem.query_selector("[slot='text-body']")
                    if text_elem:
                        description = await text_elem.inner_text()
                    
                    posts.append({
                        "title": title.strip(),
                        "description": description.strip(),
                        "post_url": f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink,
                        "upvotes": int(score) if score.isdigit() else 0,
                        "comment_count": int(comment_count) if comment_count.isdigit() else 0,
                        "author": author
                    })
                except Exception as inner_e:
                    logger.warning(f"Fehler beim Parsen eines Posts: {inner_e}")
                    continue
                    
            return posts
        finally:
            await page.close()
            await context.close()
            await browser.close()

async def scrape_post_comments_playwright(post_url: str, sort: str, limit: int, proxy: str = None) -> list:
    """
    Holt Kommentare eines Posts über Playwright.
    """
    clean_post_url = clean_url(post_url)
    url = f"{clean_post_url}?sort={sort}"
    
    logger.info(f"Playwright: Öffne Kommentar-URL {url}")
    
    async with async_playwright() as p:
        browser, context, page = await launch_browser(p, proxy)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)
            
            comments = []
            comment_elements = await page.query_selector_all("shreddit-comment")
            
            for elem in comment_elements[:limit]:
                try:
                    author = await elem.get_attribute("author") or "[deleted]"
                    score = await elem.get_attribute("score") or "0"
                    depth = await elem.get_attribute("depth") or "0"
                    
                    # Kommentartext extrahieren (liegt meistens im div mit id="*-post-rtjson-content")
                    text_elem = await elem.query_selector("[id$='-post-rtjson-content']")
                    comment_text = ""
                    if text_elem:
                        comment_text = await text_elem.inner_text()
                    else:
                        # Fallback: Alles auslesen außer dem Header-Bereich
                        comment_text = await elem.inner_text()
                    
                    comments.append({
                        "comment_text": comment_text.strip(),
                        "upvotes": int(score) if score.isdigit() else 0,
                        "author": author,
                        "is_reply": int(depth) > 0 if depth.isdigit() else False
                    })
                except Exception as inner_e:
                    logger.warning(f"Fehler beim Parsen eines Kommentars: {inner_e}")
                    continue
                    
            return comments
        finally:
            await page.close()
            await context.close()
            await browser.close()

# =====================================================================
# INTEGRATION & FAILLBACK ROUTING
# =====================================================================

async def get_subreddit_posts(target: str, sort: str, timeframe: str, limit: int, proxy: str = None) -> tuple[list, str]:
    """
    Versucht zuerst die JSON-Methode. Schlägt diese fehl, wird Playwright aufgerufen.
    Gibt (posts_list, "json"|"playwright") zurück.
    """
    try:
        posts = await scrape_subreddit_posts_json(target, sort, timeframe, limit, proxy)
        logger.info("Subreddit-Posts erfolgreich via JSON-Trick geladen.")
        return posts, "json"
    except Exception as e:
        logger.warning(f"JSON-Trick für Subreddit fehlgeschlagen: {e}. Starte Playwright Fallback...")
        try:
            posts = await scrape_subreddit_playwright_fallback(target, sort, timeframe, limit, proxy)
            return posts, "playwright"
        except Exception as pe:
            logger.error(f"Playwright Fallback ebenfalls fehlgeschlagen: {pe}")
            raise pe

async def scrape_subreddit_playwright_fallback(target: str, sort: str, timeframe: str, limit: int, proxy: str = None) -> list:
    # Hilfsfunktion zur Entkopplung
    return await scrape_subreddit_posts_playwright(target, sort, timeframe, limit, proxy)

async def get_post_comments(post_url: str, sort: str, limit: int, proxy: str = None) -> tuple[list, str]:
    """
    Versucht zuerst die JSON-Methode. Schlägt diese fehl, wird Playwright aufgerufen.
    Gibt (comments_list, "json"|"playwright") zurück.
    """
    try:
        comments = await scrape_post_comments_json(post_url, sort, limit, proxy)
        logger.info("Kommentare erfolgreich via JSON-Trick geladen.")
        return comments, "json"
    except Exception as e:
        logger.warning(f"JSON-Trick für Kommentare fehlgeschlagen: {e}. Starte Playwright Fallback...")
        try:
            comments = await scrape_post_comments_playwright(post_url, sort, limit, proxy)
            return comments, "playwright"
        except Exception as pe:
            logger.error(f"Playwright Fallback ebenfalls fehlgeschlagen: {pe}")
            raise pe
