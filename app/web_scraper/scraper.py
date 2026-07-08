import os
from typing import Optional, Any


import re
import time
import logging
import asyncio
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import markdownify
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from playwright_stealth import Stealth

from app.web_scraper.models import ScrapeRequest, ScrapeResponse, Metadata, ExtractedData, ResponseFilters

logger = logging.getLogger("rddtscpr.web_scraper_engine")

# Typical user agents and default headers
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def get_proxy_urls(rotating_proxy_url: str) -> tuple[Optional[str], Optional[str]]:
    """
    Leitet aus der konfigurierten rotierenden Proxy-URL (Evomi)
    die Datacenter und die Residential URLs ab.
    """
    if not rotating_proxy_url or not rotating_proxy_url.strip():
        return None, None
        
    dc_url = rotating_proxy_url
    res_url = rotating_proxy_url
    
    if "core-residential" in rotating_proxy_url:
        dc_url = rotating_proxy_url.replace("core-residential", "core-datacenter")
    elif "core-datacenter" in rotating_proxy_url:
        res_url = rotating_proxy_url.replace("core-datacenter", "core-residential")
        
    return dc_url, res_url

def inject_proxy_country(proxy_url: str, country_code: str) -> str:
    """
    Injektiert das Country-Targeting in die Proxy-URL für Evomi.
    Format: http://username_country-US:password@host:port
    """
    if not proxy_url or not country_code:
        return proxy_url
        
    parsed = urlparse(proxy_url)
    if not parsed.username or not parsed.password:
        return proxy_url
        
    # Wir fügen _country-XX an den Username an (Standard für Evomi)
    # Falls bereits ein Country-Code im Usernamen steht, ersetzen wir ihn
    username = parsed.username
    if "_country-" in username:
        username = re.sub(r"_country-[A-Z]{2}", f"_country-{country_code.upper()}", username)
    else:
        username = f"{username}_country-{country_code.upper()}"
        
    netloc = f"{username}:{parsed.password}@{parsed.hostname}:{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()

def inject_proxy_session(proxy_url: str, session_id: str) -> str:
    """
    Fügt eine Session-ID hinzu, um die IP bei jedem Request zu rotieren.
    """
    if not proxy_url or not session_id:
        return proxy_url
        
    parsed = urlparse(proxy_url)
    if not parsed.username or not parsed.password:
        return proxy_url
        
    username = parsed.username
    if "_session-" in username:
        username = re.sub(r"_session-\w+", f"_session-{session_id}", username)
    else:
        username = f"{username}_session-{session_id}"
        
    netloc = f"{username}:{parsed.password}@{parsed.hostname}:{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()

def parse_playwright_proxy(proxy_url: str) -> Optional[dict]:
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    playwright_proxy = {
        "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    }
    if parsed.username and parsed.password:
        playwright_proxy["username"] = parsed.username
        playwright_proxy["password"] = parsed.password
    return playwright_proxy

async def handle_resource_blocking(route):
    if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
        await route.abort()
    else:
        await route.continue_()

async def launch_stealth_browser(
    p, 
    proxy_url: Optional[str] = None, 
    use_stealth: bool = False,
    custom_headers: Optional[dict] = None,
    custom_cookies: Optional[list] = None,
    block_media: bool = True,
    include_screenshot: bool = False,
    proxy_country: Optional[str] = None,
    proxy_session: Optional[str] = None
) -> tuple[Browser, BrowserContext, Page]:
    
    if proxy_url:
        if proxy_country:
            proxy_url = inject_proxy_country(proxy_url, proxy_country)
        if proxy_session:
            proxy_url = inject_proxy_session(proxy_url, proxy_session)
            
    playwright_proxy = parse_playwright_proxy(proxy_url)
    
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
        "user_agent": DEFAULT_USER_AGENT,
        "viewport": {"width": 1280, "height": 800},
        "locale": "de-DE",
        "timezone_id": "Europe/Berlin",
        "ignore_https_errors": True
    }
    
    if custom_headers:
        context_options["extra_http_headers"] = custom_headers
        
    context = await browser.new_context(**context_options)
    
    if custom_cookies:
        # Pydantic formats: [{'name': '...', 'value': '...', 'domain': '...'}]
        # Playwright expects absolute domains or matching urls. 
        # Falls die Domain mit . startet, bereinigen wir sie für Playwright
        cookies = []
        for cookie in custom_cookies:
            c = cookie.copy()
            if "domain" in c and c["domain"].startswith("."):
                # Playwright mag keine führenden Punkte in Domains
                pass 
            cookies.append(c)
        try:
            await context.add_cookies(cookies)
        except Exception as e:
            logger.error(f"Fehler beim Injizieren benutzerdefinierter Cookies: {e}")
            
    page = await context.new_page()
    
    if block_media:
        # Wenn ein Screenshot benötigt wird, blockieren wir Stylesheets nicht,
        # da sonst das Layout der Seite komplett zerstört wird.
        blocked_resources = ["image", "media", "font"]
        if not include_screenshot:
            blocked_resources.append("stylesheet")
            
        async def handle_resource_blocking_local(route):
            if route.request.resource_type in blocked_resources:
                await route.abort()
            else:
                await route.continue_()
                
        await page.route("**/*", handle_resource_blocking_local)
        
    if use_stealth:
        await Stealth().apply_stealth_async(page)
        
    return browser, context, page

def is_bot_blocked(status_code: int, title: str, html_content: str) -> bool:
    """
    Prüft, ob die Seite uns als Bot erkannt und blockiert hat.
    """
    if status_code in [403, 407, 429, 503]:
        return True
        
    title_lower = title.lower()
    if any(keyword in title_lower for keyword in ["cloudflare", "ddos-guard", "just a moment", "access denied", "attention required"]):
        return True
        
    html_lower = html_content.lower()
    
    # Check for specific bot challenge markers to avoid false positives on normal article text or scripts config
    block_markers = [
        "verify you are human",
        "enable javascript and cookies",
        "class=\"h-captcha\"",
        "class=\"g-recaptcha\"",
        "class=\"cf-turnstile\"",
        "hcaptcha.com/getcaptcha",
        "challenges.cloudflare.com"
    ]
    if any(marker in html_lower for marker in block_markers):
        return True
        
    return False

async def auto_scroll_page(page: Page, max_scrolls: int = 10):
    """
    Scrollt die Seite schrittweise nach unten, um Lazy Loading auszulösen.
    """
    for _ in range(max_scrolls):
        prev_height = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1000)
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == prev_height:
            break

async def dismiss_cookie_banners(page: Page):
    """
    Versucht, Cookie-Banner auf der Seite automatisch zu schließen,
    indem nach bekannten Buttons gesucht wird.
    """
    selectors = [
        # IDs/Klassen mit Cookie-Bezug (z.B. OneTrust, TrustArc, Cookiebot, Nike etc.)
        "button.modal-actions-accept-btn:visible",
        "#onetrust-accept-btn-handler:visible",
        "#consent-accept:visible",
        ".cookie-box-accept:visible",
        "[id*='cookie-accept']:visible",
        "[class*='cookie-accept']:visible",
        "button[id*='accept']:visible",
        "button[class*='accept']:visible",
        "button[id*='cookie']:visible",
        "button[class*='cookie']:visible",
        "button[id*='consent']:visible",
        "button[class*='consent']:visible",
        "[role='button'][id*='accept']:visible",
        "[role='button'][class*='accept']:visible",
        
        # Text-basierte Selektoren
        "button:has-text('Alle akzeptieren'):visible",
        "button:has-text('akzeptieren'):visible",
        "button:has-text('Zustimmen'):visible",
        "button:has-text('Alle zulassen'):visible",
        "button:has-text('zulassen'):visible",
        "button:has-text('Accept All'):visible",
        "button:has-text('accept'):visible",
        "button:has-text('Allow All'):visible",
        "button:has-text('allow'):visible",
        "button:has-text('Agree'):visible",
        "button:has-text('I agree'):visible",
        "button:has-text('Einverstanden'):visible",
        "a:has-text('Alle akzeptieren'):visible",
        "a:has-text('akzeptieren'):visible",
        "a:has-text('Zustimmen'):visible",
        "a:has-text('Accept All'):visible",
        "a:has-text('accept'):visible",
        "[role='button']:has-text('Alle akzeptieren'):visible",
        "[role='button']:has-text('akzeptieren'):visible",
        "[role='button']:has-text('Accept All'):visible",
        "[role='button']:has-text('accept'):visible"
    ]
    
    logger.info("Versuche Cookie-Banner zu schließen...")
    for selector in selectors:
        try:
            # Playwright sucht auch im Shadow DOM nach diesen Selektoren
            locator = page.locator(selector).first
            if await locator.count() > 0:
                logger.info(f"Cookie-Banner-Button gefunden: '{selector}'. Klicke darauf...")
                await locator.click(timeout=3000)
                # Kurzes Warten, bis der Banner verschwindet
                await page.wait_for_timeout(1000)
        except Exception as e:
            pass

def check_login_wall(url: str, html_content: str) -> Optional[str]:
    """
    Checks if the page is gated behind a login wall.
    Returns a clear warning message if detected, otherwise None.
    """
    url_lower = url.lower()
    html_lower = html_content.lower()
    
    # 1. X.com / Twitter
    if "x.com" in url_lower or "twitter.com" in url_lower:
        if "onboarding" in html_lower or "/i/flow/login" in html_lower or "signup" in html_lower:
            return "Login wall detected on X.com. Content is restricted. Please provide session cookies."
            
    # 2. Instagram
    elif "instagram.com" in url_lower:
        if "/accounts/login" in html_lower or "login" in html_lower:
            return "Login wall detected on Instagram. Please provide session cookies."
            
    # 3. Facebook
    elif "facebook.com" in url_lower:
        if "/login" in html_lower or "facebook.com/login" in html_lower:
            return "Login wall detected on Facebook. Please provide session cookies."
            
    # 4. LinkedIn
    elif "linkedin.com" in url_lower:
        if "/login" in html_lower or "linkedin.com/signup" in html_lower:
            return "Login wall detected on LinkedIn. Please provide session cookies."
            
    return None

async def pierce_shadow_dom_js(page: Page):
    """
    Führt JS auf der Seite aus, um Inhalte aus dem Shadow DOM in den normalen DOM zu kopieren,
    damit BeautifulSoup sie parsen kann.
    """
    js_code = """
    (() => {
        function pierceShadowDOM(node) {
            if (!node) return;
            if (node.shadowRoot) {
                const shadowHTML = node.shadowRoot.innerHTML;
                const container = document.createElement('div');
                container.setAttribute('data-shadow-pierced', 'true');
                container.style.display = 'contents';
                container.innerHTML = shadowHTML;
                node.appendChild(container);
                pierceShadowDOM(container);
            }
            for (let i = 0; i < node.children.length; i++) {
                pierceShadowDOM(node.children[i]);
            }
        }
        pierceShadowDOM(document.body);
    })()
    """
    try:
        await page.evaluate(js_code)
    except Exception as e:
        logger.warning(f"Shadow DOM Piercing JS fehlgeschlagen: {e}")

def extract_tables_from_soup(soup: BeautifulSoup) -> list[list[dict[str, Any]]]:
    """
    Findet alle Tabellen und strukturiert sie als JSON Arrays.
    """
    tables_data = []
    tables = soup.find_all("table")
    
    for table in tables:
        rows_data = []
        headers = []
        
        # Header ermitteln
        header_row = table.find("tr")
        if not header_row:
            continue
            
        th_tags = header_row.find_all(["th", "td"])
        headers = [th.get_text(strip=True) for th in th_tags]
        
        # Wenn der Header leer ist, benenne Spalten nach Spaltenindex
        if not any(headers):
            headers = [f"Spalte_{i}" for i in range(len(th_tags))]
            
        rows = table.find_all("tr")[1:]
        for row in rows:
            cells = row.find_all("td")
            if not cells:
                continue
            row_dict = {}
            for idx, cell in enumerate(cells):
                header = headers[idx] if idx < len(headers) else f"Spalte_{idx}"
                row_dict[header] = cell.get_text(strip=True)
            rows_data.append(row_dict)
            
        if rows_data:
            tables_data.append(rows_data)
            
    return tables_data

def clean_html_boilerplate(soup: BeautifulSoup) -> BeautifulSoup:
    """
    Entfernt Navigation, Footer, Cookie-Banner und störende Elemente.
    """
    # 1. Entferne typische Boilerplate Tags
    boilerplate_tags = ["nav", "footer", "header", "aside", "noscript", "iframe", "script", "style", "svg"]
    for tag in soup.find_all(boilerplate_tags):
        tag.decompose()
        
    # 2. Entferne Cookie-Banner anhand von ID/Klassen-Mustern
    cookie_patterns = re.compile(r"cookie|consent|banner|gdpr|privacy|modal|popup|ads|werbung", re.IGNORECASE)
    
    for element in soup.find_all(attrs={"class": cookie_patterns}):
        element.decompose()
    for element in soup.find_all(attrs={"id": cookie_patterns}):
        element.decompose()
        
    return soup

def chunk_markdown_content(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """
    Zerschneidet Markdown-Text in überlappende Chunks.
    """
    chunks = []
    start = 0
    text_len = len(text)
    while start < text_len:
        end = min(start + chunk_size, text_len)
        chunks.append(text[start:end])
        start += chunk_size - chunk_overlap
    return chunks

async def scrape_single_page(
    url: str, 
    request: ScrapeRequest, 
    rotating_proxy_url: Optional[str] = None,
    job_id: Optional[str] = None
) -> dict:
    """
    Führt den Scraping-Vorgang für eine einzelne URL aus.
    Implementiert das Smart Stealth Routing (DC -> Residential Fallback).
    """
    start_time = time.time()
    dc_proxy, res_proxy = get_proxy_urls(rotating_proxy_url)
    
    # Standardauswahl für response filters
    filters = request.response_filters
    if not filters:
        filters = ResponseFilters()
        
    logger.info(f"Starte Scraping für URL: {url} (Job-ID: {job_id})")
    
    proxy_used = "Evomi Datacenter"
    stealth_active = False
    status_detail = None

    import uuid
    def make_sess():
        return uuid.uuid4().hex[:8]

    # Build proxy attempts chain
    attempts = []
    if request.proxy_country:
        attempts.append({
            "name": f"Evomi Residential ({request.proxy_country.upper()})",
            "proxy_url": res_proxy,
            "country": request.proxy_country,
            "session": make_sess(),
            "use_stealth": True
        })
        attempts.append({
            "name": f"Evomi Residential ({request.proxy_country.upper()} - Retry)",
            "proxy_url": res_proxy,
            "country": request.proxy_country,
            "session": make_sess(),
            "use_stealth": True
        })
    else:
        # Auto failover chain
        from app.web_scraper.queue_manager import web_scrape_queue
        proxy_mode = getattr(web_scrape_queue, "proxy_mode", "auto")
        
        if proxy_mode != "stealth":
            attempts.append({
                "name": "Evomi Datacenter",
                "proxy_url": dc_proxy,
                "country": None,
                "session": None,
                "use_stealth": False
            })
            
        attempts.append({
            "name": "Evomi Residential (Default)",
            "proxy_url": res_proxy,
            "country": None,
            "session": make_sess(),
            "use_stealth": True
        })
        attempts.append({
            "name": "Evomi Residential (US)",
            "proxy_url": res_proxy,
            "country": "US",
            "session": make_sess(),
            "use_stealth": True
        })
        attempts.append({
            "name": "Evomi Residential (DE)",
            "proxy_url": res_proxy,
            "country": "DE",
            "session": make_sess(),
            "use_stealth": True
        })
        attempts.append({
            "name": "Evomi Residential (GB)",
            "proxy_url": res_proxy,
            "country": "GB",
            "session": make_sess(),
            "use_stealth": True
        })

    success = False
    html_content = ""
    page_title = ""
    status_code = 200
    screenshot_url = None
    last_exception = None

    for idx, attempt in enumerate(attempts):
        logger.info(f"Scraping attempt {idx+1}/{len(attempts)} using {attempt['name']}...")
        proxy_used = attempt["name"]
        stealth_active = attempt["use_stealth"]
        
        # Check soft timeout (70s) to avoid exceeding 90s queue limit
        elapsed_time = time.time() - start_time
        if elapsed_time > 70.0 and idx > 0:
            logger.warning(f"Soft timeout reached after {elapsed_time:.2f}s. Stopping retry loop.")
            break
            
        browser = None
        context = None
        page = None
        
        try:
            async with async_playwright() as p:
                browser, context, page = await launch_stealth_browser(
                    p, 
                    proxy_url=attempt["proxy_url"], 
                    use_stealth=attempt["use_stealth"],
                    custom_headers=request.custom_headers,
                    custom_cookies=request.custom_cookies,
                    block_media=request.block_media,
                    include_screenshot=filters.include_screenshot,
                    proxy_country=attempt["country"],
                    proxy_session=attempt["session"]
                )
                
                # Navigation
                wait_until_option = request.wait_until
                if wait_until_option == "auto":
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=3000)
                    except Exception:
                        pass
                else:
                    playwright_wait = "networkidle" if wait_until_option == "networkidle" else ("load" if wait_until_option == "load" else "domcontentloaded")
                    response = await page.goto(url, wait_until=playwright_wait, timeout=30000)
                status_code = response.status if response else 200
                
                if request.wait_for_selector:
                    try:
                        await page.wait_for_selector(request.wait_for_selector, timeout=10000)
                    except Exception as e:
                        logger.warning(f"Warten auf Selector '{request.wait_for_selector}' lief in ein Timeout: {e}")
                        
                await pierce_shadow_dom_js(page)
                
                page_title = await page.title()
                html_content = await page.content()
                
                # Check if blocked
                is_blocked = is_bot_blocked(status_code, page_title, html_content)
                
                if is_blocked and idx < len(attempts) - 1:
                    # Trigger retry via exception
                    raise Exception(f"Bot block or rate limit (status {status_code}) on {attempt['name']}. Retrying next fallback...")
                
                # We either succeeded, or we are on the final attempt (where we have to return the block page)
                if is_blocked:
                    status_detail = f"Access denied (status {status_code}) on {attempt['name']}. Gated by bot detection or rate limit."
                    logger.warning(f"Access denied on final attempt (status {status_code}) using {attempt['name']} for {url}")
                else:
                    status_detail = None
                    success = True
                    logger.info(f"Scraping successful using {attempt['name']} (status {status_code})")
                    
                # Post-navigation processing
                await dismiss_cookie_banners(page)
                await auto_scroll_page(page, max_scrolls=5)
                
                # Grab final content after scroll
                html_content = await page.content()
                page_title = await page.title()
                
                # Check for login wall
                final_url = page.url
                login_wall_platform = check_login_wall(final_url, html_content)
                if login_wall_platform:
                    if status_detail:
                        status_detail += f" | {login_wall_platform}"
                    else:
                        status_detail = login_wall_platform
                        
                # Take screenshot
                if filters.include_screenshot and job_id:
                    screenshot_dir = "./app/data/screenshots"
                    os.makedirs(screenshot_dir, exist_ok=True)
                    screenshot_path = f"{screenshot_dir}/{job_id}.png"
                    try:
                        await page.screenshot(path=screenshot_path, full_page=True, timeout=15000)
                        screenshot_url = f"/v1/web/screenshots/{job_id}.png"
                    except Exception as e:
                        logger.warning(f"Vollseiten-Screenshot fehlgeschlagen: {e}. Nutze Viewport-Screenshot.")
                        try:
                            await page.screenshot(path=screenshot_path, full_page=False)
                            screenshot_url = f"/v1/web/screenshots/{job_id}.png"
                        except Exception as err_sc:
                            logger.error(f"Screenshot-Generierung gänzlich fehlgeschlagen: {err_sc}")
                            
                # Close page/browser
                await page.close()
                await context.close()
                await browser.close()
                browser = None
                context = None
                page = None
                
                break
                
        except Exception as attempt_err:
            logger.warning(f"Scrape attempt {idx+1} ({attempt['name']}) failed: {attempt_err}")
            last_exception = attempt_err
            if page:
                try: await page.close()
                except Exception: pass
            if context:
                try: await context.close()
                except Exception: pass
            if browser:
                try: await browser.close()
                except Exception: pass
            browser = None
            context = None
            page = None
            
    if not success and not status_detail:
        if last_exception:
            setattr(last_exception, "proxy_used", proxy_used)
            setattr(last_exception, "stealth_active", stealth_active)
            raise last_exception
        else:
            raise Exception("All scraping attempts failed due to network or browser errors.")
        
    # --- Post-Processing mit BeautifulSoup ---
    soup = BeautifulSoup(html_content, "html.parser")
    
    # Meta-Daten extrahieren vor der Bereinigung
    meta_desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    meta_description = meta_desc_tag.get("content", "").strip() if meta_desc_tag else ""
    
    # 1. Extrahierte Daten (Links, Bilder, Tabellen)
    extracted_links = []
    for link in soup.find_all("a", href=True):
        absolute_href = urljoin(url, link["href"])
        # Nur HTTP Links berücksichtigen
        if absolute_href.startswith("http"):
            extracted_links.append(absolute_href)
    # Eindeutige Links behalten
    extracted_links = list(dict.fromkeys(extracted_links))
    
    extracted_images = []
    for img in soup.find_all("img", src=True):
        absolute_src = urljoin(url, img["src"])
        if absolute_src.startswith("http"):
            extracted_images.append(absolute_src)
    extracted_images = list(dict.fromkeys(extracted_images))
    
    extracted_tables = extract_tables_from_soup(soup)
    
    # 2. Boilerplate entfernen
    cleaned_soup = clean_html_boilerplate(soup)
    cleaned_html = str(cleaned_soup)
    
    # 3. HTML in Markdown konvertieren
    markdown_text = markdownify.markdownify(cleaned_html, heading_style="ATX").strip()
    
    # Markdown säubern (mehrfache Zeilenumbrüche entfernen)
    markdown_text = re.sub(r"\n{3,}", "\n\n", markdown_text)
    
    execution_time = int((time.time() - start_time) * 1000)
    
    # Pydantic-Response aufbauen
    meta = Metadata(
        url=url,
        title=page_title,
        description=meta_description,
        status=status_code,
        execution_time_ms=execution_time,
        status_detail=status_detail
    )
    
    response_data = {
        "meta": meta.model_dump(),
        "proxy_used": proxy_used,
        "stealth_active": stealth_active
    }
    
    if filters.include_markdown:
        response_data["content_md"] = markdown_text
        if request.chunk_size:
            overlap = request.chunk_overlap or 0
            response_data["chunks"] = chunk_markdown_content(markdown_text, request.chunk_size, overlap)
            
    if filters.include_html:
        response_data["html"] = html_content
        
    if filters.include_extracted_data:
        response_data["extracted_data"] = ExtractedData(
            links=extracted_links,
            tables=extracted_tables,
            images=extracted_images
        ).model_dump()
        
    if screenshot_url:
        response_data["screenshot_url"] = screenshot_url
        
    return response_data

async def run_crawler_pipeline(
    request: ScrapeRequest, 
    rotating_proxy_url: Optional[str] = None,
    job_id: Optional[str] = None
) -> dict:
    """
    Wrapper für den Scraper-Vorgang. Führt entweder ein Single-Page Scraping aus,
    oder startet das Sub-Page Crawling mit Domain-Locking.
    """
    # 1. Hauptergebnis holen
    main_result = await scrape_single_page(request.url, request, rotating_proxy_url, job_id)
    
    # Falls Crawling deaktiviert ist oder das Scraping fehlgeschlagen ist
    if not request.page_crawling or main_result["meta"]["status"] != 200:
        return main_result
        
    # 2. Crawler starten (Domain-Locking)
    parsed_main_url = urlparse(request.url)
    main_domain = parsed_main_url.netloc.lower()
    
    # Extrahiere Links aus dem Hauptergebnis
    extracted_links = main_result.get("extracted_data", {}).get("links", [])
    if not extracted_links:
        # Falls extracted_data im filter deaktiviert war, parsen wir sie temporär aus der HTML oder nutzen sie falls vorhanden
        # Falls nicht vorhanden, können wir nicht crawlen
        return main_result
        
    # Filtere nach gleicher Domain (Domain-Lock) und schließe Anker/Sprungmarken aus
    candidate_links = []
    for link in extracted_links:
        parsed_link = urlparse(link)
        if parsed_link.netloc.lower() == main_domain:
            clean_link = link.split("#")[0].rstrip("/")
            if clean_link != request.url.rstrip("/") and clean_link not in candidate_links:
                candidate_links.append(clean_link)
                
    # Crawler Queue (nur bis max_crawl_pages begrenzen)
    pages_to_crawl = candidate_links[:request.max_crawl_pages]
    crawled_pages_results = []
    
    # Lokale Kopie der Anfrage für Unterseiten (kein weiteres rekursives Crawling auf Unterseiten!)
    sub_request = request.model_copy(update={"page_crawling": False})
    
    logger.info(f"Crawler startet für {len(pages_to_crawl)} Unterseiten von Domain '{main_domain}'")
    
    for idx, sub_url in enumerate(pages_to_crawl):
        sub_job_id = f"{job_id}_sub_{idx}" if job_id else None
        try:
            logger.info(f"Crawl [{idx+1}/{len(pages_to_crawl)}]: {sub_url}")
            sub_res = await scrape_single_page(sub_url, sub_request, rotating_proxy_url, sub_job_id)
            crawled_pages_results.append(sub_res)
        except Exception as e:
            logger.error(f"Fehler beim Crawlen der Unterseite {sub_url}: {e}")
            crawled_pages_results.append({
                "meta": {
                    "url": sub_url,
                    "title": "Error loading page",
                    "description": "",
                    "status": 500,
                    "execution_time_ms": 0
                },
                "error": str(e)
            })
            
    main_result["crawled_pages"] = crawled_pages_results
    return main_result
