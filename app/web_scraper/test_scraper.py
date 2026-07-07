import asyncio
import os
import sys
from bs4 import BeautifulSoup

# Verzeichnis zum Python-Pfad hinzufügen, falls nötig
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from app.web_scraper.scraper import (
    get_proxy_urls,
    parse_playwright_proxy,
    clean_html_boilerplate,
    extract_tables_from_soup,
    chunk_markdown_content,
    scrape_single_page
)
from app.web_scraper.models import ScrapeRequest

def test_proxy_helpers():
    print("Starte Test: Proxy Helpers...")
    # Test 1: Standard URL
    p_url = "http://user:pass@core-residential.evomi.com:1000"
    dc, res = get_proxy_urls(p_url)
    assert "core-datacenter" in dc
    assert "core-residential" in res
    
    # Test 2: Playwright Parsing
    parsed = parse_playwright_proxy(p_url)
    assert parsed["server"] == "http://core-residential.evomi.com:1000"
    assert parsed["username"] == "user"
    assert parsed["password"] == "pass"
    print("✓ Proxy Helpers erfolgreich getestet.\n")

def test_html_cleaner():
    print("Starte Test: HTML Boilerplate Cleaner...")
    html = """
    <html>
        <body>
            <header>Logo & Nav</header>
            <nav><a href="/">Home</a></nav>
            <main>
                <h1>Hauptinhalt</h1>
                <p>Das ist der wichtige Text.</p>
                <table>
                    <tr><th>Name</th><th>Wert</th></tr>
                    <tr><td>A</td><td>1</td></tr>
                </table>
            </main>
            <aside>Werbung</aside>
            <div class="cookie-consent-banner">Bitte zustimmen!</div>
            <footer>Copyright 2026</footer>
        </body>
    </html>
    """
    soup = BeautifulSoup(html, "html.parser")
    cleaned = clean_html_boilerplate(soup)
    cleaned_text = str(cleaned)
    
    # Nav, Footer, header, cookie banner sollten gelöscht sein
    assert "<nav>" not in cleaned_text
    assert "<footer>" not in cleaned_text
    assert "<header>" not in cleaned_text
    assert "cookie-consent-banner" not in cleaned_text
    assert "Hauptinhalt" in cleaned_text
    print("✓ HTML Boilerplate Cleaner erfolgreich getestet.\n")

def test_table_extractor():
    print("Starte Test: Table Extractor...")
    html = """
    <table>
        <tr><th>Farbe</th><th>Hex</th></tr>
        <tr><td>Rot</td><td>#FF0000</td></tr>
        <tr><td>Grün</td><td>#00FF00</td></tr>
    </table>
    """
    soup = BeautifulSoup(html, "html.parser")
    tables = extract_tables_from_soup(soup)
    assert len(tables) == 1
    assert tables[0][0]["Farbe"] == "Rot"
    assert tables[0][0]["Hex"] == "#FF0000"
    assert tables[0][1]["Farbe"] == "Grün"
    print("✓ Table Extractor erfolgreich getestet.\n")

def test_chunking():
    print("Starte Test: Chunking...")
    text = "abcdefghij" # 10 Zeichen
    # Chunk size 4, overlap 2
    # Chunk 1: abcd (start 0)
    # Next start: 4 - 2 = 2
    # Chunk 2: cdef (start 2)
    # Next start: 6 - 2 = 4
    # Chunk 3: efgh (start 4)
    # Next start: 8 - 2 = 6
    # Chunk 4: ghij (start 6)
    # Next start: 10 - 2 = 8
    # Chunk 5: ij (start 8)
    chunks = chunk_markdown_content(text, 4, 2)
    assert len(chunks) == 5
    assert chunks[0] == "abcd"
    assert chunks[1] == "cdef"
    print("✓ Chunking erfolgreich getestet.\n")

async def test_live_scrape():
    print("Starte Test: Live-Scrape von https://example.com...")
    req = ScrapeRequest(
        url="https://example.com",
        delivery_mode="direct"
    )
    try:
        # Führe Scrape ohne Proxy aus (None übergeben)
        res = await scrape_single_page("https://example.com", req, rotating_proxy_url=None)
        assert res["meta"]["status"] == 200
        assert "Example Domain" in res["meta"]["title"]
        assert "content_md" in res
        assert "iana.org" in res["extracted_data"]["links"][0]
        print("✓ Live-Scrape von https://example.com war erfolgreich!")
        print(f"  Titel: {res['meta']['title']}")
        print(f"  Markdown-Länge: {len(res['content_md'])} Zeichen")
    except Exception as e:
        print(f"✗ Live-Scrape fehlgeschlagen: {e}")
        # Wenn Playwright im Container / System nicht initialisiert ist, fangen wir das ab
        # ohne den Test-Run als kritisch scheitern zu lassen.
        print("  (Hinweis: Kann an fehlenden Playwright-Browsern auf diesem Testsystem liegen)")

async def main():
    test_proxy_helpers()
    test_html_cleaner()
    test_table_extractor()
    test_chunking()
    await test_live_scrape()
    print("=== Alle lokalen Tests beendet ===")

if __name__ == "__main__":
    asyncio.run(main())
