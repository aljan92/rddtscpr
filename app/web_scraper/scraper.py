import os
from typing import Optional, Any


import re
import time
import uuid
import logging
import asyncio
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import markdownify
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from playwright_stealth import Stealth
from curl_cffi import requests as cffi_requests

from app.web_scraper.models import ScrapeRequest, ScrapeResponse, Metadata, ExtractedData, ResponseFilters

logger = logging.getLogger("rddtscpr.web_scraper_engine")

class BotBlockException(Exception):
    def __init__(self, message: str, status_code: int, html_content: str, page_title: str, proxy_used: str):
        super().__init__(message)
        self.status_code = status_code
        self.html_content = html_content
        self.page_title = page_title
        self.proxy_used = proxy_used

# Typical user agents and default headers
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.26 Safari/537.36"

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
    Format: http://username:password_country-us@host:port
    """
    if not proxy_url or not country_code:
        return proxy_url
        
    parsed = urlparse(proxy_url)
    if not parsed.username or not parsed.password:
        return proxy_url
        
    # Wir fügen _country-xx an das Passwort an (Standard für Evomi)
    password = parsed.password
    if "_country-" in password:
        password = re.sub(r"_country-[a-zA-Z]{2}", f"_country-{country_code.lower()}", password)
    else:
        password = f"{password}_country-{country_code.lower()}"
        
    netloc = f"{parsed.username}:{password}@{parsed.hostname}:{parsed.port}"
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
        
    password = parsed.password
    if "_session-" in password:
        password = re.sub(r"_session-\w+", f"_session-{session_id}", password)
    else:
        password = f"{password}_session-{session_id}"
        
    netloc = f"{parsed.username}:{password}@{parsed.hostname}:{parsed.port}"
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
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled"
    ]
    
    browser = await p.chromium.launch(
        headless=True,
        proxy=playwright_proxy,
        args=browser_args
    )
    
    # Dynamic locale & timezone based on targeted proxy country to avoid bot detection signature mismatch
    country_metadata = {
        "US": {"locale": "en-US", "timezone": "America/New_York"},
        "DE": {"locale": "de-DE", "timezone": "Europe/Berlin"},
        "GB": {"locale": "en-GB", "timezone": "Europe/London"},
        "AU": {"locale": "en-AU", "timezone": "Australia/Sydney"},
        "BR": {"locale": "pt-BR", "timezone": "America/Sao_Paulo"},
        "CN": {"locale": "zh-CN", "timezone": "Asia/Shanghai"},
        "RU": {"locale": "ru-RU", "timezone": "Europe/Moscow"},
        "FR": {"locale": "fr-FR", "timezone": "Europe/Paris"},
        "ES": {"locale": "es-ES", "timezone": "Europe/Madrid"},
        "IT": {"locale": "it-IT", "timezone": "Europe/Rome"},
        "CA": {"locale": "en-CA", "timezone": "America/Toronto"},
        "IN": {"locale": "en-IN", "timezone": "Asia/Kolkata"},
        "JP": {"locale": "ja-JP", "timezone": "Asia/Tokyo"}
    }
    
    selected_locale = "de-DE"
    selected_timezone = "Europe/Berlin"
    
    if proxy_country:
        meta = country_metadata.get(proxy_country.upper())
        if meta:
            selected_locale = meta["locale"]
            selected_timezone = meta["timezone"]
        else:
            selected_locale = "en-US"
            selected_timezone = "UTC"
            
    context_options = {
        "user_agent": DEFAULT_USER_AGENT,
        "viewport": {"width": 1280, "height": 800},
        "locale": selected_locale,
        "timezone_id": selected_timezone,
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
        "challenges.cloudflare.com",
        "unusual activity",
        "automated traffic",
        "unidentified, automated",
        "don't have permission to access",
        "access denied"
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

# ---------------------------------------------------------------------------
#  curl_cffi Fast-Path Engine
# ---------------------------------------------------------------------------

def _build_cffi_proxy_url(proxy_url: str, country: Optional[str] = None) -> Optional[str]:
    """
    Formats an Evomi proxy URL for curl_cffi (with optional country targeting
    and a fresh session ID to rotate the IP).
    """
    if not proxy_url:
        return None
    url = proxy_url
    if country:
        url = inject_proxy_country(url, country)
    url = inject_proxy_session(url, uuid.uuid4().hex[:8])
    return url


def scrape_with_curl_cffi(
    url: str,
    proxy_url: Optional[str] = None,
    proxy_country: Optional[str] = None,
    timeout: int = 15
) -> tuple[int, str, str, str]:
    """
    Lightweight TLS-impersonated GET request using curl_cffi.
    Returns (status_code, html_content, page_title, final_url).
    """
    formatted_proxy = _build_cffi_proxy_url(proxy_url, proxy_country)
    proxies = None
    if formatted_proxy:
        proxies = {"http": formatted_proxy, "https": formatted_proxy}

    # Match locale to country for Accept-Language header
    locale_map = {
        "US": "en-US,en;q=0.9",
        "DE": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "GB": "en-GB,en;q=0.9",
        "AU": "en-AU,en;q=0.9",
        "BR": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "FR": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "ES": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
        "IT": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "JP": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
        "CA": "en-CA,en;q=0.9",
        "IN": "en-IN,en;q=0.9",
        "CN": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "RU": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    accept_lang = locale_map.get((proxy_country or "").upper(), "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7")

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": accept_lang,
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "max-age=0",
        "Sec-Ch-Ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    response = cffi_requests.get(
        url,
        headers=headers,
        proxies=proxies,
        impersonate="chrome136",
        timeout=timeout,
        allow_redirects=True,
    )

    html_content = response.text
    status_code = response.status_code
    final_url = str(response.url)

    # Extract title from raw HTML
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_content, re.IGNORECASE | re.DOTALL)
    page_title = title_match.group(1).strip() if title_match else ""

    return status_code, html_content, page_title, final_url


def is_meaningful_html(html: str) -> bool:
    """
    Checks whether the HTML contains meaningful visible text content.
    Returns False for empty SPA skeletons (React/Vue/Angular apps that
    require JavaScript to render) and known bot-block pages.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove script and style tags before measuring text
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()

    visible_text = soup.get_text(separator=" ", strip=True)

    # Typical SPA skeletons have very little visible text
    if len(visible_text) < 100:
        return False

    # Check for well-known SPA root-only patterns
    body = soup.find("body")
    if body:
        children = [c for c in body.children if getattr(c, "name", None)]
        if len(children) <= 2:
            # Only 1-2 div children (e.g. <div id="root"></div> or <div id="app"></div>)
            inner_text = body.get_text(strip=True)
            if len(inner_text) < 80:
                return False

    return True


def build_result_from_html(
    html_content: str,
    url: str,
    page_title: str,
    status_code: int,
    request: ScrapeRequest,
    filters: ResponseFilters,
    execution_time_ms: int,
    status_detail: Optional[str] = None,
    screenshot_url: Optional[str] = None,
    proxy_used: str = "curl_cffi",
    stealth_active: bool = False,
    scrape_engine: str = "curl_cffi",
) -> dict:
    """
    Shared post-processing: converts raw HTML into the standard API response
    (markdown, extracted data, chunks, etc.). Used by both curl_cffi and
    Playwright code paths so the response format is always identical.
    """
    soup = BeautifulSoup(html_content, "html.parser")

    # Meta description
    meta_desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    meta_description = meta_desc_tag.get("content", "").strip() if meta_desc_tag else ""

    # Extracted data (links, images, tables)
    extracted_links = []
    for link in soup.find_all("a", href=True):
        absolute_href = urljoin(url, link["href"])
        if absolute_href.startswith("http"):
            extracted_links.append(absolute_href)
    extracted_links = list(dict.fromkeys(extracted_links))

    extracted_images = []
    for img in soup.find_all("img", src=True):
        absolute_src = urljoin(url, img["src"])
        if absolute_src.startswith("http"):
            extracted_images.append(absolute_src)
    extracted_images = list(dict.fromkeys(extracted_images))

    extracted_tables = extract_tables_from_soup(soup)

    # Boilerplate removal + markdown conversion
    cleaned_soup = clean_html_boilerplate(soup)
    cleaned_html = str(cleaned_soup)
    markdown_text = markdownify.markdownify(cleaned_html, heading_style="ATX").strip()
    markdown_text = re.sub(r"\n{3,}", "\n\n", markdown_text)

    # Login wall detection
    login_wall_msg = check_login_wall(url, html_content)
    if login_wall_msg:
        status_detail = f"{status_detail} | {login_wall_msg}" if status_detail else login_wall_msg

    meta = Metadata(
        url=url,
        title=page_title,
        description=meta_description,
        status=status_code,
        execution_time_ms=execution_time_ms,
        status_detail=status_detail,
    )

    response_data = {
        "meta": meta.model_dump(),
        "proxy_used": proxy_used,
        "stealth_active": stealth_active,
        "scrape_engine": scrape_engine,
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
            images=extracted_images,
        ).model_dump()

    if screenshot_url:
        response_data["screenshot_url"] = screenshot_url

    return response_data


# ---------------------------------------------------------------------------
#  Playwright Browser Engine (Slow-Path)
# ---------------------------------------------------------------------------

async def _run_playwright_attempt(
    url: str,
    request: ScrapeRequest,
    filters: ResponseFilters,
    attempt: dict,
    job_id: Optional[str] = None,
) -> tuple[bool, int, str, str, Optional[str], Optional[str]]:
    """
    Runs a single Playwright scraping attempt.
    Returns (success, status_code, html_content, page_title, screenshot_url, status_detail).
    Raises Exception if blocked (to signal retry to the caller).
    """
    screenshot_url = None
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
            proxy_session=attempt["session"],
        )

        try:
            # Navigation
            wait_until_option = request.wait_until
            if wait_until_option == "auto":
                response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass
            else:
                pw_wait = "networkidle" if wait_until_option == "networkidle" else ("load" if wait_until_option == "load" else "domcontentloaded")
                response = await page.goto(url, wait_until=pw_wait, timeout=30000)

            status_code = response.status if response else 200

            if request.wait_for_selector:
                try:
                    await page.wait_for_selector(request.wait_for_selector, timeout=10000)
                except Exception as e:
                    logger.warning(f"Wait for selector '{request.wait_for_selector}' timed out: {e}")

            await pierce_shadow_dom_js(page)

            page_title = await page.title()
            html_content = await page.content()

            # Bot-block check
            blocked = is_bot_blocked(status_code, page_title, html_content)
            if blocked:
                raise BotBlockException(
                    f"Bot block (status {status_code}) on {attempt['name']}.",
                    status_code=status_code,
                    html_content=html_content,
                    page_title=page_title,
                    proxy_used=attempt["name"]
                )

            # Post-navigation processing
            await dismiss_cookie_banners(page)
            await auto_scroll_page(page, max_scrolls=5)

            html_content = await page.content()
            page_title = await page.title()

            # Screenshot
            if filters.include_screenshot and job_id:
                screenshot_dir = "./app/data/screenshots"
                os.makedirs(screenshot_dir, exist_ok=True)
                screenshot_path = f"{screenshot_dir}/{job_id}.png"
                try:
                    await page.screenshot(path=screenshot_path, full_page=True, timeout=15000)
                    screenshot_url = f"/v1/web/screenshots/{job_id}.png"
                except Exception as e:
                    logger.warning(f"Full-page screenshot failed: {e}. Trying viewport screenshot.")
                    try:
                        await page.screenshot(path=screenshot_path, full_page=False)
                        screenshot_url = f"/v1/web/screenshots/{job_id}.png"
                    except Exception as err_sc:
                        logger.error(f"Screenshot generation completely failed: {err_sc}")

            return True, status_code, html_content, page_title, screenshot_url, None
        finally:
            await page.close()
            await context.close()
            await browser.close()


def _build_playwright_attempts(
    request: ScrapeRequest,
    dc_proxy: Optional[str],
    res_proxy: Optional[str],
    skip_datacenter: bool = False,
) -> list[dict]:
    """
    Builds the ordered list of Playwright proxy attempts.
    """
    def make_sess():
        return uuid.uuid4().hex[:8]

    attempts = []
    if request.proxy_country:
        attempts.append({
            "name": f"Evomi Residential ({request.proxy_country.upper()})",
            "proxy_url": res_proxy,
            "country": request.proxy_country,
            "session": make_sess(),
            "use_stealth": True,
        })
        attempts.append({
            "name": f"Evomi Residential ({request.proxy_country.upper()} - Retry)",
            "proxy_url": res_proxy,
            "country": request.proxy_country,
            "session": make_sess(),
            "use_stealth": True,
        })
    else:
        from app.web_scraper.queue_manager import web_scrape_queue
        proxy_mode = getattr(web_scrape_queue, "proxy_mode", "auto")

        if skip_datacenter:
            # If Cloudflare was detected, we skip datacenter and untargeted residential attempts
            # to ensure strict alignment between IP location and browser locale/timezone.
            logger.info("Cloudflare detected: starting directly with country-targeted residential proxies.")
            attempts.append({"name": "Evomi Residential (DE)", "proxy_url": res_proxy, "country": "DE", "session": make_sess(), "use_stealth": True})
            attempts.append({"name": "Evomi Residential (US)", "proxy_url": res_proxy, "country": "US", "session": make_sess(), "use_stealth": True})
            attempts.append({"name": "Evomi Residential (GB)", "proxy_url": res_proxy, "country": "GB", "session": make_sess(), "use_stealth": True})
        else:
            if proxy_mode != "stealth" and not skip_datacenter:
                attempts.append({
                    "name": "Evomi Datacenter",
                    "proxy_url": dc_proxy,
                    "country": None,
                    "session": None,
                    "use_stealth": False,
                })

            attempts.append({"name": "Evomi Residential (Default)", "proxy_url": res_proxy, "country": None, "session": make_sess(), "use_stealth": True})
            attempts.append({"name": "Evomi Residential (US)", "proxy_url": res_proxy, "country": "US", "session": make_sess(), "use_stealth": True})
            attempts.append({"name": "Evomi Residential (DE)", "proxy_url": res_proxy, "country": "DE", "session": make_sess(), "use_stealth": True})
            attempts.append({"name": "Evomi Residential (GB)", "proxy_url": res_proxy, "country": "GB", "session": make_sess(), "use_stealth": True})

    return attempts


# ---------------------------------------------------------------------------
#  curl_cffi Fast-Path Retry Chain
# ---------------------------------------------------------------------------

def _build_cffi_country_chain(request: ScrapeRequest) -> list[Optional[str]]:
    """
    Returns a list of countries to try with curl_cffi.
    If the user specified a country, we try that one (with one retry).
    Otherwise we try: no-country -> US -> DE -> GB.
    """
    if request.proxy_country:
        return [request.proxy_country, request.proxy_country]
    return [None, "US", "DE", "GB"]


def _try_curl_cffi_chain(
    url: str,
    request: ScrapeRequest,
    res_proxy: Optional[str],
    start_time: float,
) -> tuple[Optional[tuple[int, str, str, str, str, Optional[str]]], Optional[dict]]:
    """
    Runs the curl_cffi retry chain.
    Returns (success_tuple, last_block_dict).
    """
    countries = _build_cffi_country_chain(request)
    last_block = None
    for country in countries:
        # Soft timeout check
        if time.time() - start_time > 70.0:
            logger.warning("curl_cffi: soft timeout reached, stopping retry chain.")
            break
        country_label = country or "Default"
        logger.info(f"curl_cffi attempt with country={country_label}...")
        try:
            status_code, html, title, final_url = scrape_with_curl_cffi(
                url, proxy_url=res_proxy, proxy_country=country, timeout=15
            )
            blocked = is_bot_blocked(status_code, title, html)
            if blocked:
                logger.info(f"curl_cffi blocked (status {status_code}) with country={country_label}.")
                last_block = {
                    "html_content": html,
                    "url": final_url,
                    "page_title": title,
                    "status_code": status_code,
                    "status_detail": f"Access denied (status {status_code}) on curl_cffi.",
                    "proxy_used": f"curl_cffi (Residential {country_label})",
                    "stealth_active": False,
                    "scrape_engine": "curl_cffi"
                }
                
                # If a Cloudflare challenge or bot shield is detected, curl_cffi won't be able
                # to bypass it anyway. Abort early to avoid flagging the TLS fingerprint and wasting time.
                title_lower = title.lower()
                html_lower = html.lower()
                is_cf = any(k in title_lower for k in ["cloudflare", "ddos-guard", "just a moment"]) or \
                        any(k in html_lower for k in ["verify you are human", "cf-turnstile", "hcaptcha", "recaptcha"])
                if is_cf:
                    logger.info("Cloudflare/Bot shield detected. Aborting curl_cffi chain for Playwright fallback.")
                    return None, last_block
                
                continue
            if not is_meaningful_html(html):
                logger.info(f"curl_cffi returned SPA skeleton with country={country_label}. Needs browser rendering.")
                return None, last_block  # SPA detected — no point retrying curl_cffi
            proxy_name = f"curl_cffi (Residential {country_label})"
            return (status_code, html, title, final_url, proxy_name, country), None
        except Exception as e:
            logger.warning(f"curl_cffi attempt country={country_label} failed: {e}")
            continue
    return None, last_block


# ---------------------------------------------------------------------------
#  Main Entry Point: Hybrid scrape_single_page
# ---------------------------------------------------------------------------

async def scrape_single_page(
    url: str,
    request: ScrapeRequest,
    rotating_proxy_url: Optional[str] = None,
    job_id: Optional[str] = None,
) -> dict:
    """
    Hybrid scraping pipeline:
    - Fast Path (curl_cffi first):  When no browser features are requested.
    - Browser Path (Playwright first): When screenshot, wait_for_selector,
      or page_crawling are requested.
    Both paths fall back to the other engine on failure.
    """
    start_time = time.time()
    dc_proxy, res_proxy = get_proxy_urls(rotating_proxy_url)

    filters = request.response_filters or ResponseFilters()

    logger.info(f"Starting hybrid scrape for URL: {url} (Job-ID: {job_id})")

    # Determine if we need browser-only features
    needs_browser = (
        filters.include_screenshot
        or request.wait_for_selector
        or request.page_crawling
    )

    last_block_page_data = None

    # ===================================================================
    #  FAST PATH: curl_cffi first, Playwright fallback
    # ===================================================================
    if not needs_browser:
        logger.info("Routing: Fast Path (curl_cffi first)")

        cffi_success, cffi_block = _try_curl_cffi_chain(url, request, res_proxy, start_time)
        if cffi_block:
            last_block_page_data = cffi_block

        if cffi_success is not None:
            status_code, html, title, final_url, proxy_used, country = cffi_success
            execution_time_ms = int((time.time() - start_time) * 1000)
            logger.info(f"curl_cffi succeeded in {execution_time_ms}ms using {proxy_used}")
            return build_result_from_html(
                html_content=html,
                url=final_url,
                page_title=title,
                status_code=status_code,
                request=request,
                filters=filters,
                execution_time_ms=execution_time_ms,
                proxy_used=proxy_used,
                stealth_active=False,
                scrape_engine="curl_cffi",
            )

        # curl_cffi failed or SPA detected — fall through to Playwright
        logger.info("curl_cffi fast path failed. Falling back to Playwright...")

    # ===================================================================
    #  BROWSER PATH: Playwright (with retry chain)
    # ===================================================================
    logger.info("Routing: Browser Path (Playwright)")
    
    # Check if Cloudflare was detected in the fast path to skip datacenter proxy
    skip_dc = False
    if last_block_page_data:
        title_lower = last_block_page_data.get("page_title", "").lower()
        html_lower = last_block_page_data.get("html_content", "").lower()
        if any(k in title_lower for k in ["cloudflare", "ddos-guard", "just a moment"]) or \
           any(k in html_lower for k in ["verify you are human", "cf-turnstile", "hcaptcha", "recaptcha"]):
            skip_dc = True
            logger.info("Skipping datacenter proxy for Playwright fallback because Cloudflare was detected.")

    attempts = _build_playwright_attempts(request, dc_proxy, res_proxy, skip_datacenter=skip_dc)

    pw_success = False
    pw_html = ""
    pw_title = ""
    pw_status = 200
    pw_screenshot = None
    pw_status_detail = None
    pw_proxy_used = "Playwright"
    pw_last_exception = None

    for idx, attempt in enumerate(attempts):
        logger.info(f"Playwright attempt {idx+1}/{len(attempts)} using {attempt['name']}...")
        pw_proxy_used = attempt["name"]

        # Soft timeout (70s) to stay within queue limit
        if time.time() - start_time > 70.0 and idx > 0:
            logger.warning("Playwright: soft timeout reached. Stopping retry loop.")
            break

        try:
            ok, status, html, title, screenshot, detail = await _run_playwright_attempt(
                url, request, filters, attempt, job_id
            )
            pw_success = True
            pw_status = status
            pw_html = html
            pw_title = title
            pw_screenshot = screenshot
            pw_status_detail = detail
            logger.info(f"Playwright succeeded using {attempt['name']} (status {status})")
            break
        except BotBlockException as bbe:
            logger.warning(f"Playwright attempt {idx+1} ({attempt['name']}) failed with bot block: {bbe}")
            last_block_page_data = {
                "html_content": bbe.html_content,
                "url": url,
                "page_title": bbe.page_title,
                "status_code": bbe.status_code,
                "status_detail": f"Access denied (status {bbe.status_code}) on {attempt['name']}. Gated by bot detection.",
                "proxy_used": bbe.proxy_used,
                "stealth_active": True,
                "scrape_engine": "playwright"
            }
            pw_last_exception = bbe
        except Exception as err:
            logger.warning(f"Playwright attempt {idx+1} ({attempt['name']}) failed: {err}")
            pw_last_exception = err

    if pw_success:
        execution_time_ms = int((time.time() - start_time) * 1000)
        return build_result_from_html(
            html_content=pw_html,
            url=url,
            page_title=pw_title,
            status_code=pw_status,
            request=request,
            filters=filters,
            execution_time_ms=execution_time_ms,
            screenshot_url=pw_screenshot,
            status_detail=pw_status_detail,
            proxy_used=pw_proxy_used,
            stealth_active=True,
            scrape_engine="playwright",
        )

    # ===================================================================
    #  CROSS-FALLBACK: Playwright failed → try curl_cffi as last resort
    # ===================================================================
    if needs_browser:
        logger.info("Playwright failed on all proxies. Attempting curl_cffi cross-fallback...")
        cffi_success, cffi_block = _try_curl_cffi_chain(url, request, res_proxy, start_time)
        if cffi_block:
            last_block_page_data = cffi_block

        if cffi_success is not None:
            status_code, html, title, final_url, proxy_used, country = cffi_success
            execution_time_ms = int((time.time() - start_time) * 1000)
            fallback_detail = "Browser engine blocked by all proxies. Falling back to lightweight scraper. Screenshot unavailable for this target."
            logger.info(f"curl_cffi cross-fallback succeeded in {execution_time_ms}ms")
            return build_result_from_html(
                html_content=html,
                url=final_url,
                page_title=title,
                status_code=status_code,
                request=request,
                filters=filters,
                execution_time_ms=execution_time_ms,
                status_detail=fallback_detail,
                proxy_used=proxy_used,
                stealth_active=False,
                scrape_engine="curl_cffi",
            )

    # ===================================================================
    #  TOTAL FAILURE: Both engines failed (return last block page if any)
    # ===================================================================
    if last_block_page_data:
        execution_time_ms = int((time.time() - start_time) * 1000)
        logger.warning(f"All scraper engines failed. Returning last block page from {last_block_page_data['scrape_engine']}")
        return build_result_from_html(
            html_content=last_block_page_data["html_content"],
            url=last_block_page_data["url"],
            page_title=last_block_page_data["page_title"],
            status_code=last_block_page_data["status_code"],
            request=request,
            filters=filters,
            execution_time_ms=execution_time_ms,
            status_detail=last_block_page_data["status_detail"],
            proxy_used=last_block_page_data["proxy_used"],
            stealth_active=last_block_page_data["stealth_active"],
            scrape_engine=last_block_page_data["scrape_engine"]
        )

    if pw_last_exception:
        setattr(pw_last_exception, "proxy_used", pw_proxy_used)
        setattr(pw_last_exception, "stealth_active", True)
        raise pw_last_exception
    raise Exception("All scraping attempts failed (both curl_cffi and Playwright).")

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
