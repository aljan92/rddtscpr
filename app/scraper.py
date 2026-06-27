import os
import json
import logging
import httpx
import asyncio
import re
from urllib.parse import urlparse
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from app.auth import get_account_cookies

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
    target_clean = target.strip()
    
    # Falls das Target eine komplette Reddit-URL ist oder mit r/ startet
    if "reddit.com" in target_clean or "r/" in target_clean:
        # Extrahiere Subreddit-Name (alles nach /r/ oder r/)
        match = re.search(r"r/([^/?#]+)", target_clean)
        if match:
            subreddit_name = match.group(1)
            base_url = f"https://www.reddit.com/r/{subreddit_name}/"
        else:
            base_url = clean_url(target_clean)
            if not base_url.endswith("/"):
                base_url += "/"
    else:
        # Target ist nur der Subreddit-Name
        base_url = f"https://www.reddit.com/r/{target_clean.strip('/')}/"
        
    url = f"{base_url}{sort}/"
    return url

def detect_media(data: dict) -> tuple[str, str]:
    """Erkennt Bilder und Videos im Post-Payload von Reddit."""
    image_url = None
    video_url = None
    
    # 1. Video-Erkennung (natives Reddit Video)
    if data.get("is_video"):
        video_url = data.get("media", {}).get("reddit_video", {}).get("fallback_url")
        if video_url:
            video_url = video_url.split("?")[0]
            
    # 2. Externe Video-Erkennung (z.B. redgifs, youtube) über secure_media / iframe
    secure_media = data.get("secure_media")
    if not video_url and isinstance(secure_media, dict):
        oembed = secure_media.get("oembed", {})
        html_content = oembed.get("html", "")
        if html_content:
            src_match = re.search(r'src=["\'](https?://[^"\']+)["\']', html_content)
            if src_match:
                video_url = src_match.group(1)
                
    # 3. Falls immer noch kein Video, url prüfen (z.B. redgifs, youtube Links)
    url_dest = data.get("url_overridden_by_dest") or data.get("url", "")
    if not video_url and url_dest:
        if any(domain in url_dest for domain in ["youtube.com", "youtu.be", "redgifs.com", "v.redd.it"]):
            video_url = url_dest
            
    # 4. Bild-Erkennung
    if url_dest and not video_url:
        if any(url_dest.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"]):
            image_url = url_dest
        elif "i.redd.it" in url_dest or "imgur.com" in url_dest:
            image_url = url_dest
            
    return image_url, video_url

# =====================================================================
# METOHDE 1: Der .json-Trick (Schnell & Ressourcenschonend)
# =====================================================================

async def scrape_subreddit_posts_json(target: str, sort: str, timeframe: str, limit: int, session_state: str = None, proxy_url: str = None) -> list:
    """
    Holt Posts eines Subreddits über den .json-Trick.
    """
    base_url = build_subreddit_url(target, sort, timeframe)
    json_url = f"{base_url.rstrip('/')}.json"
    
    # Query Parameter
    params = {"limit": limit}
    if sort == "top" and timeframe:
        params["t"] = timeframe
        
    cookies = get_account_cookies(session_state)
    
    # Proxy-Konfiguration
    proxies = {"all://": proxy_url} if proxy_url else None
    
    logger.info(f"JSON-Trick: Rufe URL auf: {json_url} mit Params {params}")
    
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, cookies=cookies, proxies=proxies, timeout=15.0, follow_redirects=True) as client:
        response = await client.get(json_url, params=params)
        
        if response.status_code == 404:
            raise ValueError("Subreddit oder Post existiert nicht (HTTP 404).")
        elif response.status_code != 200:
            raise Exception(f"Reddit-API lieferte Status Code {response.status_code}")
            
        payload = response.json()
        posts = []
        
        children = payload.get("data", {}).get("children", [])
        for child in children[:limit]:
            data = child.get("data", {})
            # Nur echte Posts berücksichten (t3)
            if child.get("kind") != "t3":
                continue
                
            image_url, video_url = detect_media(data)
            
            posts.append({
                "title": data.get("title"),
                "description": data.get("selftext", ""),
                "image_url": image_url,
                "video_url": video_url,
                "post_url": f"https://www.reddit.com{data.get('permalink')}",
                "upvotes": data.get("ups", 0),
                "comment_count": data.get("num_comments", 0),
                "author": data.get("author", "[deleted]")
            })
            
        return posts

async def fetch_more_children(link_id: str, children_ids: list[str], sort: str, session_state: str = None, proxy_url: str = None) -> list:
    """Ruft nachgelagerte Kommentare über den /api/morechildren.json Endpunkt von Reddit ab."""
    url = "https://www.reddit.com/api/morechildren.json"
    params = {
        "api_type": "json",
        "link_id": link_id,
        "children": ",".join(children_ids),
        "sort": sort
    }
    cookies = get_account_cookies(session_state)
    proxies = {"all://": proxy_url} if proxy_url else None
    
    logger.info(f"MoreChildren: Rufe {len(children_ids)} IDs ab...")
    try:
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, cookies=cookies, proxies=proxies, timeout=15.0) as client:
            response = await client.get(url, params=params)
            if response.status_code == 200:
                payload = response.json()
                return payload.get("json", {}).get("data", {}).get("things", [])
            else:
                logger.warning(f"MoreChildren API lieferte Status Code {response.status_code}")
    except Exception as e:
        logger.error(f"Fehler bei fetch_more_children: {e}")
    return []

def extract_comments_recursive(children: list, include_replies: bool, is_root: bool = True, limit: int = None, more_nodes: list = None) -> list:
    """Extrahiert Kommentare und ggf. deren Replies rekursiv und flacht sie ab. Das Limit gilt nur für Hauptkommentare."""
    comments = []
    root_count = 0
    
    for child in children:
        if is_root and limit is not None and root_count >= limit:
            break
            
        kind = child.get("kind")
        data = child.get("data", {})
        
        # Falls es ein Platzhalter für "weitere Antworten" ist
        if kind == "more" and include_replies and more_nodes is not None:
            children_ids = data.get("children", [])
            parent_id = data.get("parent_id", "")
            if children_ids:
                more_nodes.append({
                    "parent_id": parent_id,
                    "children": children_ids
                })
            continue
            
        if kind != "t1":
            continue
            
        parent_id = data.get("parent_id", "")
        is_reply = not parent_id.startswith("t3_")
        
        comments.append({
            "comment_text": data.get("body", ""),
            "upvotes": data.get("ups", 0),
            "author": data.get("author", "[deleted]"),
            "is_reply": is_reply
        })
        
        if is_root:
            root_count += 1
        
        if include_replies:
            replies_payload = data.get("replies")
            if isinstance(replies_payload, dict):
                reply_children = replies_payload.get("data", {}).get("children", [])
                sub_comments = extract_comments_recursive(reply_children, include_replies, is_root=False, limit=None, more_nodes=more_nodes)
                comments.extend(sub_comments)
                
    return comments

async def scrape_post_comments_json(post_url: str, sort: str, limit: int, include_replies: bool = False, load_more: bool = False, session_state: str = None, proxy_url: str = None) -> list:
    """
    Holt Kommentare eines Posts über den .json-Trick.
    """
    clean_post_url = clean_url(post_url)
    json_url = f"{clean_post_url.rstrip('/')}.json"
    
    params = {"sort": sort}
    cookies = get_account_cookies(session_state)
    proxies = {"all://": proxy_url} if proxy_url else None
    
    logger.info(f"JSON-Trick: Rufe URL auf: {json_url} mit Params {params}")
    
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, cookies=cookies, proxies=proxies, timeout=15.0, follow_redirects=True) as client:
        response = await client.get(json_url, params=params)
        
        if response.status_code == 404:
            raise ValueError("Post existiert nicht (HTTP 404).")
        elif response.status_code != 200:
            raise Exception(f"Reddit-API lieferte Status Code {response.status_code}")
            
        payload = response.json()
        
        # Die Response bei Kommentaren besteht aus einer Liste: [Post-Daten, Kommentar-Daten]
        if not isinstance(payload, list) or len(payload) < 2:
            raise Exception("Unerwartetes JSON-Format von Reddit für Kommentare.")
            
        comments_payload = payload[1]
        children = comments_payload.get("data", {}).get("children", [])
        
        more_nodes = []
        comments = extract_comments_recursive(children, include_replies, is_root=True, limit=limit, more_nodes=more_nodes)
        
        # Falls load_more aktiv ist, laden wir diese nach
        if include_replies and load_more and more_nodes:
            # Post-ID aus URL extrahieren (z. B. t3_1u8hnzu)
            post_id_match = re.search(r'/comments/([a-z0-9]+)/', post_url)
            link_id = f"t3_{post_id_match.group(1)}" if post_id_match else ""
            
            if link_id:
                max_requests = 10  # Schutzlimit vor unendlichen Requests und Bans
                request_count = 0
                queue = list(more_nodes)
                
                logger.info(f"LoadMore: Starte Nachladen von {len(queue)} Platzhaltern (Max {max_requests} Requests, 1.5s Delay)...")
                
                while queue and request_count < max_requests:
                    node = queue.pop(0)
                    children_ids = node["children"]
                    
                    # 1.5 Sekunden Wartezeit einhalten, um Rate-Limits zu schonen
                    await asyncio.sleep(1.5)
                    
                    things = await fetch_more_children(link_id, children_ids, sort, session_state, proxy_url)
                    request_count += 1
                    
                    if things:
                        new_more_nodes = []
                        # Die nachgeladenen things können wiederum more-Knoten enthalten
                        new_comments = extract_comments_recursive(things, include_replies, is_root=False, limit=None, more_nodes=new_more_nodes)
                        comments.extend(new_comments)
                        
                        if new_more_nodes:
                            queue.extend(new_more_nodes)
                            
        return comments

# =====================================================================
# METODE 2: Playwright-Stealth (Robuster Browser-Fallback)
# =====================================================================

async def launch_browser(p, session_state_str: str = None, proxy_url: str = None):
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
    
    context_options = {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "viewport": {"width": 1280, "height": 800},
        "locale": "de-DE",
        "timezone_id": "Europe/Berlin"
    }
    
    if session_state_str:
        try:
            state_dict = json.loads(session_state_str)
            context_options["storage_state"] = state_dict
            logger.info("Playwright: Nutze übergebene Session-Cookies aus der DB.")
        except Exception as e:
            logger.error(f"Fehler beim Laden des Session-States in Playwright: {e}")
        
    context = await browser.new_context(**context_options)
    page = await context.new_page()
    await Stealth().apply_stealth_async(page)
    
    return browser, context, page

async def get_page_json(page) -> dict:
    """Extrahiert und parst das JSON aus dem geladenen Browser-Dokument."""
    content = ""
    try:
        # Browser stellen JSON oft in einem <pre>-Tag dar
        pre_elem = await page.query_selector("pre")
        if pre_elem:
            content = await pre_elem.inner_text()
    except Exception:
        pass
        
    if not content:
        content = await page.evaluate("() => document.body.innerText")
        
    # Überprüfen, ob wir die Sicherheitsblock-Seite sehen
    if "blocked by network security" in content.lower() or "deine anfrage wurde von der netzwerksicherheit blockiert" in content.lower():
        raise Exception("Reddit blockiert die Verbindung (Netzwerksicherheits-Sperre). Proxy erforderlich.")
        
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Wenn kein valides JSON da ist, Screenshot zur Diagnose machen
        os.makedirs("./app/data", exist_ok=True)
        await page.screenshot(path="./app/data/last_error.png")
        
        content_lower = content.lower()
        # Spezifische Client-Fehler erkennen, die keinen Account-Failover auslösen sollten
        if "forbidden" in content_lower or "privat" in content_lower or "gesperrt" in content_lower or "banned" in content_lower:
            raise ValueError("Subreddit ist privat, gesperrt oder nicht zugänglich.")
        if "page not found" in content_lower or "nicht gefunden" in content_lower or "404" in content_lower or "page_not_found" in content_lower:
            raise ValueError("Subreddit oder Post existiert nicht (HTTP 404).")
            
        # Teilauszug des Texts für die Fehlermeldung
        preview = content[:200].replace('\n', ' ')
        raise Exception(f"Ungültige JSON-Antwort von Reddit erhalten. Inhalt startet mit: '{preview}'")

async def scrape_subreddit_posts_playwright(target: str, sort: str, timeframe: str, limit: int, session_state: str = None, proxy_url: str = None) -> list:
    """
    Holt Posts eines Subreddits über Playwright, indem die .json-URL im Browser geladen wird.
    """
    base_url = build_subreddit_url(target, sort, timeframe)
    url_json = f"{base_url.rstrip('/')}.json"
    
    # Query Parameter
    params = f"limit={limit}"
    if sort == "top" and timeframe:
        params += f"&t={timeframe}"
    url = f"{url_json}?{params}"
        
    logger.info(f"Playwright: Öffne JSON-URL {url}")
    
    async with async_playwright() as p:
        browser, context, page = await launch_browser(p, session_state, proxy_url)
        try:
            # Zu Reddit navigieren
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2000)
            
            payload = await get_page_json(page)
            posts = []
            
            children = payload.get("data", {}).get("children", [])
            for child in children[:limit]:
                data = child.get("data", {})
                if child.get("kind") != "t3":
                    continue
                    
                image_url, video_url = detect_media(data)
                
                posts.append({
                    "title": data.get("title"),
                    "description": data.get("selftext", ""),
                    "image_url": image_url,
                    "video_url": video_url,
                    "post_url": f"https://www.reddit.com{data.get('permalink')}",
                    "upvotes": data.get("ups", 0),
                    "comment_count": data.get("num_comments", 0),
                    "author": data.get("author", "[deleted]")
                })
                
            if not posts:
                # Prüfen, ob das Listing absichtlich leer ist oder blockiert wurde
                if "data" not in payload:
                    os.makedirs("./app/data", exist_ok=True)
                    await page.screenshot(path="./app/data/last_error.png")
                    raise Exception("Keine Datenstruktur in der Reddit-Antwort gefunden.")
                    
            return posts
        finally:
            await page.close()
            await context.close()
            await browser.close()

async def scrape_post_comments_playwright(post_url: str, sort: str, limit: int, include_replies: bool = False, load_more: bool = False, session_state: str = None, proxy_url: str = None) -> list:
    """
    Holt Kommentare eines Posts über Playwright, indem die .json-URL im Browser geladen wird.
    """
    clean_post_url = clean_url(post_url)
    params = f"sort={sort}"
    url = f"{clean_post_url.rstrip('/')}.json?{params}"
    
    logger.info(f"Playwright: Öffne JSON-URL {url}")
    
    async with async_playwright() as p:
        browser, context, page = await launch_browser(p, session_state, proxy_url)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2000)
            
            payload = await get_page_json(page)
            
            if not isinstance(payload, list) or len(payload) < 2:
                os.makedirs("./app/data", exist_ok=True)
                await page.screenshot(path="./app/data/last_error.png")
                raise Exception("Unerwartetes JSON-Format von Reddit für Kommentare.")
                
            comments_payload = payload[1]
            children = comments_payload.get("data", {}).get("children", [])
            
            more_nodes = []
            comments = extract_comments_recursive(children, include_replies, is_root=True, limit=limit, more_nodes=more_nodes)
            
            if include_replies and load_more and more_nodes:
                post_id_match = re.search(r'/comments/([a-z0-9]+)/', post_url)
                link_id = f"t3_{post_id_match.group(1)}" if post_id_match else ""
                
                if link_id:
                    max_requests = 10
                    request_count = 0
                    queue = list(more_nodes)
                    
                    logger.info(f"Playwright LoadMore: Starte Nachladen von {len(queue)} Platzhaltern...")
                    
                    while queue and request_count < max_requests:
                        node = queue.pop(0)
                        children_ids = node["children"]
                        
                        await asyncio.sleep(1.5)
                        
                        things = await fetch_more_children(link_id, children_ids, sort, session_state, proxy_url)
                        request_count += 1
                        
                        if things:
                            new_more_nodes = []
                            new_comments = extract_comments_recursive(things, include_replies, is_root=False, limit=None, more_nodes=new_more_nodes)
                            comments.extend(new_comments)
                            
                            if new_more_nodes:
                                queue.extend(new_more_nodes)
                                
            return comments
        finally:
            await page.close()
            await context.close()
            await browser.close()

# =====================================================================
# INTEGRATION & FAILLBACK ROUTING
# =====================================================================

async def get_subreddit_posts(target: str, sort: str, timeframe: str, limit: int, session_state: str = None, proxy_url: str = None) -> tuple[list, str]:
    """
    Versucht zuerst die JSON-Methode. Schlägt diese fehl, wird Playwright aufgerufen.
    Gibt (posts_list, "json"|"playwright") zurück.
    """
    try:
        posts = await scrape_subreddit_posts_json(target, sort, timeframe, limit, session_state, proxy_url)
        logger.info("Subreddit-Posts erfolgreich via JSON-Trick geladen.")
        return posts, "json"
    except Exception as e:
        logger.warning(f"JSON-Trick für Subreddit fehlgeschlagen: {e}. Starte Playwright Fallback...")
        try:
            posts = await scrape_subreddit_playwright_fallback(target, sort, timeframe, limit, session_state, proxy_url)
            return posts, "playwright"
        except Exception as pe:
            logger.error(f"Playwright Fallback ebenfalls fehlgeschlagen: {pe}")
            raise pe

async def scrape_subreddit_playwright_fallback(target: str, sort: str, timeframe: str, limit: int, session_state: str = None, proxy_url: str = None) -> list:
    # Hilfsfunktion zur Entkopplung
    return await scrape_subreddit_posts_playwright(target, sort, timeframe, limit, session_state, proxy_url)

async def get_post_comments(post_url: str, sort: str, limit: int, include_replies: bool = False, load_more: bool = False, session_state: str = None, proxy_url: str = None) -> tuple[list, str]:
    """
    Versucht zuerst die JSON-Methode. Schlägt diese fehl, wird Playwright aufgerufen.
    Gibt (comments_list, "json"|"playwright") zurück.
    """
    try:
        comments = await scrape_post_comments_json(post_url, sort, limit, include_replies, load_more, session_state, proxy_url)
        logger.info("Kommentare erfolgreich via JSON-Trick geladen.")
        return comments, "json"
    except Exception as e:
        logger.warning(f"JSON-Trick für Kommentare fehlgeschlagen: {e}. Starte Playwright Fallback...")
        try:
            comments = await scrape_post_comments_playwright(post_url, sort, limit, include_replies, load_more, session_state, proxy_url)
            return comments, "playwright"
        except Exception as pe:
            logger.error(f"Playwright Fallback ebenfalls fehlgeschlagen: {pe}")
            raise pe
