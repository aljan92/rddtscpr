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
    include_screenshot: bool = False
) -> tuple[Browser, BrowserContext, Page]:
    
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

    try:
        async with async_playwright() as p:
            # Check global proxy_mode setting dynamically
            from app.web_scraper.queue_manager import web_scrape_queue
            proxy_mode = getattr(web_scrape_queue, "proxy_mode", "auto")
            
            if proxy_mode == "stealth":
                proxy_used = "Evomi Residential"
                stealth_active = True
                
            browser = None
            context = None
            page = None
            success = False
            
            if proxy_mode != "stealth":
                logger.info(f"Attempt 1: DC-Proxy für {url}...")
                try:
                    browser, context, page = await launch_stealth_browser(
                        p, 
                        proxy_url=dc_proxy, 
                        use_stealth=False,
                        custom_headers=request.custom_headers,
                        custom_cookies=request.custom_cookies,
                        block_media=request.block_media,
                        include_screenshot=filters.include_screenshot
                    )
                    
                    # Navigation
                    wait_until_option = request.wait_until
                    if wait_until_option == "auto":
                        response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                        try:
                            # Intelligentes Kurz-Warten auf Netzwerkruhe (max. 3 Sekunden)
                            await page.wait_for_load_state("networkidle", timeout=3000)
                        except Exception:
                            pass
                    else:
                        playwright_wait = "networkidle" if wait_until_option == "networkidle" else ("load" if wait_until_option == "load" else "domcontentloaded")
                        response = await page.goto(url, wait_until=playwright_wait, timeout=30000)
                    status_code = response.status if response else 200
                    
                    # Warten auf Selector falls spezifiziert
                    if request.wait_for_selector:
                        try:
                            await page.wait_for_selector(request.wait_for_selector, timeout=10000)
                        except Exception as e:
                            logger.warning(f"Warten auf Selector '{request.wait_for_selector}' lief in ein Timeout: {e}")
                    
                    # Schatten-DOM aufdecken
                    await pierce_shadow_dom_js(page)
                    
                    # DOM extrahieren
                    page_title = await page.title()
                    html_content = await page.content()
                    
                    # Prüfen, ob wir geblockt wurden
                    if is_bot_blocked(status_code, page_title, html_content):
                        logger.warning(f"Bot-Block auf Datacenter-Proxy erkannt für {url}. Starte Retry...")
                        raise Exception("Bot block detected on Datacenter proxy.")
                        
                    # Erfolgreich gescraped über Datacenter!
                    logger.info(f"DC-Scraping erfolgreich für {url} (Status: {status_code})")
                    success = True
                    
                except Exception as attempt_err:
                    logger.info(f"Datacenter-Proxy fehlgeschlagen für {url}: {attempt_err}")
                    # Schließe den DC Browser falls offen
                    if page: await page.close()
                    if context: await context.close()
                    if browser: await browser.close()
                    browser = None
                    context = None
                    page = None
                    
            if not success:
                # --- ATTEMPT 2: Fallback auf Residential + Stealth ---
                logger.info(f"Attempt 2: Residential Proxy + Stealth für {url}...")
                proxy_used = "Evomi Residential"
                stealth_active = True
                
                browser, context, page = await launch_stealth_browser(
                    p, 
                    proxy_url=res_proxy, 
                    use_stealth=True,
                    custom_headers=request.custom_headers,
                    custom_cookies=request.custom_cookies,
                    block_media=request.block_media,
                    include_screenshot=filters.include_screenshot
                )
                
                wait_until_option = request.wait_until
                if wait_until_option == "auto":
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    try:
                        # Intelligentes Kurz-Warten auf Netzwerkruhe (max. 3 Sekunden)
                        await page.wait_for_load_state("networkidle", timeout=3000)
                    except Exception:
                        pass
                else:
                    playwright_wait = "networkidle" if wait_until_option == "networkidle" else ("load" if wait_until_option == "load" else "domcontentloaded")
                    response = await page.goto(url, wait_until=playwright_wait, timeout=35000)
                status_code = response.status if response else 200
                
                if request.wait_for_selector:
                    try:
                        await page.wait_for_selector(request.wait_for_selector, timeout=10000)
                    except Exception as e:
                        logger.warning(f"Warten auf Selector '{request.wait_for_selector}' lief in ein Timeout: {e}")
                        
                await pierce_shadow_dom_js(page)
                
                page_title = await page.title()
                html_content = await page.content()
                
                if is_bot_blocked(status_code, page_title, html_content):
                    raise Exception(f"Zugriff verweigert (Status {status_code}). Trotz Residential Proxy blockiert.")
                    
                logger.info(f"Residential-Scraping erfolgreich für {url}")
                
            # Ab hier haben wir die geladene Seite in 'page' und 'html_content'
            
            # Cookie Banner schließen
            await dismiss_cookie_banners(page)
            
            # Auto-Scroll ausführen falls gewünscht (nur, wenn Medien nicht geblockt wurden, sonst macht scrollen wenig Sinn, oder falls der User es explizit will)
            # Wir scrollen standardmäßig maximal 5-mal
            await auto_scroll_page(page, max_scrolls=5)
            
            # Aktualisierten HTML-Inhalt nach Scroll holen
            html_content = await page.content()
            page_title = await page.title()
            
            # Screenshot generieren falls gewünscht
            screenshot_url = None
            if filters.include_screenshot and job_id:
                screenshot_dir = "./app/data/screenshots"
                os.makedirs(screenshot_dir, exist_ok=True)
                screenshot_path = f"{screenshot_dir}/{job_id}.png"
                try:
                    # Versuche Vollseiten-Screenshot
                    await page.screenshot(path=screenshot_path, full_page=True, timeout=15000)
                    screenshot_url = f"/v1/web/screenshots/{job_id}.png"
                except Exception as e:
                    logger.warning(f"Vollseiten-Screenshot fehlgeschlagen: {e}. Nutze Viewport-Screenshot.")
                    try:
                        await page.screenshot(path=screenshot_path, full_page=False)
                        screenshot_url = f"/v1/web/screenshots/{job_id}.png"
                    except Exception as err_sc:
                        logger.error(f"Screenshot-Generierung gänzlich fehlgeschlagen: {err_sc}")
                        
            # Browser schließen
            await page.close()
            await context.close()
            await browser.close()
    except Exception as e:
        setattr(e, "proxy_used", proxy_used)
        setattr(e, "stealth_active", stealth_active)
        raise e
        
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
        execution_time_ms=execution_time
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
                    "title": "Fehler beim Laden",
                    "description": "",
                    "status": 500,
                    "execution_time_ms": 0
                },
                "error": str(e)
            })
            
    main_result["crawled_pages"] = crawled_pages_results
    return main_result
