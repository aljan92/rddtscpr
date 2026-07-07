import asyncio
import httpx
import time
import logging
import json
import os
import sys

# --- CONFIGURATION ---
BASE_URL = "https://api.angermann.work"
SETTINGS_FILE = "./app/data/settings.json"
LOG_FILE = "web_scraper_stresstest.log"

# Setup logging
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Load local settings for rapidapi secrets if available
def load_local_secrets():
    proxy_secret = ""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
                proxy_secret = data.get("web_rapidapi_proxy_secret") or data.get("rapidapi_proxy_secret") or ""
        except Exception as e:
            print(f"⚠️ Konnte settings.json nicht laden ({e}). Nutze Standardwerte.")
    return proxy_secret

PROXY_SECRET = load_local_secrets()

HEADERS = {
    "Content-Type": "application/json"
}
if PROXY_SECRET:
    HEADERS["X-RapidAPI-Proxy-Secret"] = PROXY_SECRET

# Test target sites
SITE_SIMPLE = "https://example.com"
SITE_DYNAMIC_JS = "https://quotes.toscrape.com/js/"
SITE_TABLES = "https://en.wikipedia.org/wiki/List_of_countries_by_GDP_(nominal)"
SITE_STEALTH = "https://news.ycombinator.com"

# Metrics collection
test_results = {
    "functional": [],
    "stress": {
        "total_requests": 0,
        "success": 0,
        "failed": 0,
        "forbidden": 0,
        "timeout": 0,
        "network_error": 0,
        "durations": []
    }
}

# Helper to print colored console messages
def print_status(icon, message, status="INFO"):
    color_map = {
        "SUCCESS": "\033[92m", # Green
        "ERROR": "\033[91m",   # Red
        "WARNING": "\033[93m", # Yellow
        "INFO": "\033[94m",    # Blue
        "RESET": "\033[0m"
    }
    color = color_map.get(status, color_map["RESET"])
    print(f"{icon} {color}{message}{color_map['RESET']}")
    logging.info(f"[{status}] {message}")

# --- PART 1: SEQUENTIAL FUNCTIONAL TESTS ---
async def run_functional_tests(client):
    print_status("⚙️", "Starte Phase 1: Feature- & Plan-Validierung...", "INFO")
    
    # 1. Basic Markdown test
    start = time.time()
    payload = {
        "url": SITE_SIMPLE,
        "delivery_mode": "direct",
        "response_filters": {
            "include_markdown": True,
            "include_html": False,
            "include_extracted_data": False,
            "include_screenshot": False
        }
    }
    try:
        res = await client.post(f"{BASE_URL}/v1/web/scrape", json=payload, headers={**HEADERS, "X-RapidAPI-Subscription": "BASIC"})
        dur = time.time() - start
        if res.status_code == 200 and "content_md" in res.json() and "Example Domain" in res.json()["content_md"]:
            print_status("✅", f"Test 1: Basic Markdown Scrape erfolgreich ({dur:.2f}s)", "SUCCESS")
            test_results["functional"].append({"name": "Basic Markdown Scrape", "status": "PASSED"})
        else:
            print_status("❌", f"Test 1: Basic Markdown Scrape fehlgeschlagen ({res.status_code})", "ERROR")
            test_results["functional"].append({"name": "Basic Markdown Scrape", "status": "FAILED", "response": res.text})
    except Exception as e:
        print_status("❌", f"Test 1: Fehler bei Basic Markdown Scrape: {e}", "ERROR")

    # 2. Raw HTML test
    start = time.time()
    payload["response_filters"] = {
        "include_markdown": False,
        "include_html": True,
        "include_extracted_data": False,
        "include_screenshot": False
    }
    try:
        res = await client.post(f"{BASE_URL}/v1/web/scrape", json=payload, headers={**HEADERS, "X-RapidAPI-Subscription": "BASIC"})
        dur = time.time() - start
        if res.status_code == 200 and "html" in res.json() and "<html" in res.json()["html"].lower():
            print_status("✅", f"Test 2: Raw HTML Extraktion erfolgreich ({dur:.2f}s)", "SUCCESS")
            test_results["functional"].append({"name": "Raw HTML Extraktion", "status": "PASSED"})
        else:
            print_status("❌", f"Test 2: Raw HTML Extraktion fehlgeschlagen", "ERROR")
            test_results["functional"].append({"name": "Raw HTML Extraktion", "status": "FAILED"})
    except Exception as e:
        print_status("❌", f"Test 2: Fehler: {e}", "ERROR")

    # 3. Extracted Data & Tables test
    start = time.time()
    payload = {
        "url": SITE_TABLES,
        "delivery_mode": "direct",
        "response_filters": {
            "include_markdown": False,
            "include_html": False,
            "include_extracted_data": True,
            "include_screenshot": False
        }
    }
    try:
        res = await client.post(f"{BASE_URL}/v1/web/scrape", json=payload, headers={**HEADERS, "X-RapidAPI-Subscription": "BASIC"})
        dur = time.time() - start
        data = res.json()
        if res.status_code == 200 and "extracted_data" in data and len(data["extracted_data"]["tables"]) > 0:
            print_status("✅", f"Test 3: Tabellen- & Linkextraktion (Wikipedia) erfolgreich ({dur:.2f}s)", "SUCCESS")
            test_results["functional"].append({"name": "Tabellenextraktion", "status": "PASSED"})
        else:
            print_status("❌", f"Test 3: Tabellenextraktion fehlgeschlagen ({res.status_code})", "ERROR")
            test_results["functional"].append({"name": "Tabellenextraktion", "status": "FAILED"})
    except Exception as e:
        print_status("❌", f"Test 3: Fehler: {e}", "ERROR")

    # 4. Premium Screenshot Allowed (PRO)
    start = time.time()
    payload = {
        "url": SITE_SIMPLE,
        "delivery_mode": "direct",
        "response_filters": {
            "include_markdown": False,
            "include_html": False,
            "include_extracted_data": False,
            "include_screenshot": True
        }
    }
    try:
        res = await client.post(f"{BASE_URL}/v1/web/scrape", json=payload, headers={**HEADERS, "X-RapidAPI-Subscription": "PRO"})
        dur = time.time() - start
        if res.status_code == 200 and "screenshot_url" in res.json():
            print_status("✅", f"Test 4: Screenshot erlaubt für PRO-Plan ({dur:.2f}s)", "SUCCESS")
            test_results["functional"].append({"name": "Screenshot PRO Allowed", "status": "PASSED"})
        else:
            print_status("❌", f"Test 4: Screenshot fehlgeschlagen für PRO ({res.status_code})", "ERROR")
            test_results["functional"].append({"name": "Screenshot PRO Allowed", "status": "FAILED"})
    except Exception as e:
        print_status("❌", f"Test 4: Fehler: {e}", "ERROR")

    # 5. Premium Screenshot Blocked (BASIC)
    try:
        res = await client.post(f"{BASE_URL}/v1/web/scrape", json=payload, headers={**HEADERS, "X-RapidAPI-Subscription": "BASIC"})
        if res.status_code == 403 and "restricted to Pro" in res.text:
            print_status("✅", "Test 5: Screenshot erfolgreich für BASIC-Plan geblockt (403)", "SUCCESS")
            test_results["functional"].append({"name": "Screenshot BASIC Blocked", "status": "PASSED"})
        else:
            print_status("❌", f"Test 5: Screenshot BASIC wurde NICHT geblockt ({res.status_code})", "ERROR")
            test_results["functional"].append({"name": "Screenshot BASIC Blocked", "status": "FAILED"})
    except Exception as e:
        print_status("❌", f"Test 5: Fehler: {e}", "ERROR")

    # 6. Premium Chunking Allowed (PRO)
    payload = {
        "url": SITE_SIMPLE,
        "delivery_mode": "direct",
        "chunk_size": 200,
        "chunk_overlap": 50
    }
    try:
        res = await client.post(f"{BASE_URL}/v1/web/scrape", json=payload, headers={**HEADERS, "X-RapidAPI-Subscription": "PRO"})
        if res.status_code == 200 and "chunks" in res.json() and len(res.json()["chunks"]) > 0:
            print_status("✅", "Test 6: LLM-Chunking erlaubt für PRO-Plan", "SUCCESS")
            test_results["functional"].append({"name": "Chunking PRO Allowed", "status": "PASSED"})
        else:
            print_status("❌", f"Test 6: Chunking fehlgeschlagen für PRO ({res.status_code})", "ERROR")
            test_results["functional"].append({"name": "Chunking PRO Allowed", "status": "FAILED"})
    except Exception as e:
        print_status("❌", f"Test 6: Fehler: {e}", "ERROR")

    # 7. Premium Chunking Blocked (BASIC)
    try:
        res = await client.post(f"{BASE_URL}/v1/web/scrape", json=payload, headers={**HEADERS, "X-RapidAPI-Subscription": "BASIC"})
        if res.status_code == 403 and "restricted to Pro" in res.text:
            print_status("✅", "Test 7: LLM-Chunking erfolgreich für BASIC-Plan geblockt (403)", "SUCCESS")
            test_results["functional"].append({"name": "Chunking BASIC Blocked", "status": "PASSED"})
        else:
            print_status("❌", f"Test 7: Chunking BASIC wurde NICHT geblockt ({res.status_code})", "ERROR")
            test_results["functional"].append({"name": "Chunking BASIC Blocked", "status": "FAILED"})
    except Exception as e:
        print_status("❌", f"Test 7: Fehler: {e}", "ERROR")

    # 8. Premium Crawling Allowed (PRO)
    start = time.time()
    payload = {
        "url": "https://quotes.toscrape.com/",
        "delivery_mode": "direct",
        "page_crawling": True,
        "max_crawl_depth": 1,
        "max_crawl_pages": 2
    }
    try:
        res = await client.post(f"{BASE_URL}/v1/web/scrape", json=payload, headers={**HEADERS, "X-RapidAPI-Subscription": "PRO"})
        dur = time.time() - start
        if res.status_code == 200 and "crawled_pages" in res.json() and len(res.json()["crawled_pages"]) > 0:
            print_status("✅", f"Test 8: Sub-Page Crawling erlaubt für PRO-Plan ({dur:.2f}s)", "SUCCESS")
            test_results["functional"].append({"name": "Crawling PRO Allowed", "status": "PASSED"})
        else:
            print_status("❌", f"Test 8: Sub-Page Crawling fehlgeschlagen für PRO ({res.status_code})", "ERROR")
            test_results["functional"].append({"name": "Crawling PRO Allowed", "status": "FAILED"})
    except Exception as e:
        print_status("❌", f"Test 8: Fehler: {e}", "ERROR")

    # 9. Premium Crawling Blocked (BASIC)
    try:
        res = await client.post(f"{BASE_URL}/v1/web/scrape", json=payload, headers={**HEADERS, "X-RapidAPI-Subscription": "BASIC"})
        if res.status_code == 403 and "restricted to Pro" in res.text:
            print_status("✅", "Test 9: Sub-Page Crawling erfolgreich für BASIC-Plan geblockt (403)", "SUCCESS")
            test_results["functional"].append({"name": "Crawling BASIC Blocked", "status": "PASSED"})
        else:
            print_status("❌", f"Test 9: Sub-Page Crawling BASIC wurde NICHT geblockt ({res.status_code})", "ERROR")
            test_results["functional"].append({"name": "Crawling BASIC Blocked", "status": "FAILED"})
    except Exception as e:
        print_status("❌", f"Test 9: Fehler: {e}", "ERROR")

    # 10. Dynamic Javascript Rendering
    start = time.time()
    payload = {
        "url": SITE_DYNAMIC_JS,
        "delivery_mode": "direct",
        "response_filters": {
            "include_markdown": True,
            "include_html": False,
            "include_extracted_data": False,
            "include_screenshot": False
        }
    }
    try:
        res = await client.post(f"{BASE_URL}/v1/web/scrape", json=payload, headers={**HEADERS, "X-RapidAPI-Subscription": "BASIC"})
        dur = time.time() - start
        data = res.json()
        if res.status_code == 200 and "content_md" in data and "Albert Einstein" in data["content_md"]:
            print_status("✅", f"Test 10: Dynamisches Javascript erfolgreich ausgeführt ({dur:.2f}s)", "SUCCESS")
            test_results["functional"].append({"name": "JS Rendering", "status": "PASSED"})
        else:
            print_status("❌", f"Test 10: JS-Rendering fehlgeschlagen (Albert Einstein wurde im Markdown nicht gefunden)", "ERROR")
            test_results["functional"].append({"name": "JS Rendering", "status": "FAILED"})
    except Exception as e:
        print_status("❌", f"Test 10: Fehler: {e}", "ERROR")

# --- PART 2: STRESS TEST ---
async def send_stress_request(client, url, filters, plan, req_id):
    payload = {
        "url": url,
        "delivery_mode": "direct",
        "response_filters": filters
    }
    
    headers = HEADERS.copy()
    headers["X-RapidAPI-Subscription"] = plan
    
    start = time.time()
    test_results["stress"]["total_requests"] += 1
    
    try:
        res = await client.post(f"{BASE_URL}/v1/web/scrape", json=payload, headers=headers)
        dur = time.time() - start
        test_results["stress"]["durations"].append(dur)
        
        if res.status_code == 200:
            test_results["stress"]["success"] += 1
            print("🟢", end="", flush=True)
        elif res.status_code == 403:
            test_results["stress"]["forbidden"] += 1
            print("🟡", end="", flush=True)
            logging.warning(f"Req {req_id} | 403 Forbidden | URL: {url} | Tier: {plan} | Response: {res.text}")
        else:
            test_results["stress"]["failed"] += 1
            print("🔴", end="", flush=True)
            logging.warning(f"Req {req_id} | Code: {res.status_code} | URL: {url} | Response: {res.text}")
            
    except httpx.TimeoutException:
        dur = time.time() - start
        test_results["stress"]["timeout"] += 1
        print("⏰", end="", flush=True)
        logging.error(f"Req {req_id} | Timeout nach {dur:.2f}s | URL: {url}")
        
    except Exception as e:
        test_results["stress"]["network_error"] += 1
        print("💥", end="", flush=True)
        logging.error(f"Req {req_id} | Netzwerkfehler: {e} | URL: {url}")

async def run_stress_test(client):
    print("\n")
    print_status("🚀", "Starte Phase 2: Stresstest (Last stetig steigend)...", "INFO")
    print("🟢 = 200 OK | 🟡 = 403 Blocked (Erwartet) | 🔴 = Fehler | ⏰ = Timeout | 💥 = Netz-Fehler\n")
    
    # Test payloads mix
    targets = [
        (SITE_SIMPLE, {"include_markdown": True, "include_html": False, "include_extracted_data": False, "include_screenshot": False}, "BASIC"),
        (SITE_DYNAMIC_JS, {"include_markdown": True, "include_html": False, "include_extracted_data": False, "include_screenshot": False}, "BASIC"),
        (SITE_STEALTH, {"include_markdown": True, "include_html": False, "include_extracted_data": False, "include_screenshot": False}, "PRO"),
        (SITE_SIMPLE, {"include_markdown": True, "include_html": False, "include_extracted_data": False, "include_screenshot": True}, "BASIC"), # Erwartet 403
        (SITE_TABLES, {"include_markdown": False, "include_html": False, "include_extracted_data": True, "include_screenshot": False}, "PRO")
    ]
    
    req_id = 0
    duration_minutes = 2
    end_time = time.time() + (duration_minutes * 60)
    
    # We gradually decrease sleep time to increase load (concurrency)
    # Start: sleep 3.0s between requests
    # Mid (1 min): sleep 1.2s between requests
    # End (1.5 min): sleep 0.4s between requests (High load)
    
    start_time = time.time()
    tasks = []
    
    while time.time() < end_time:
        elapsed = time.time() - start_time
        
        # Calculate adaptive sleep interval to increase load
        if elapsed < 40:
            sleep_time = 3.0
        elif elapsed < 80:
            sleep_time = 1.2
        else:
            sleep_time = 0.4
            
        req_id += 1
        url, filters, plan = targets[req_id % len(targets)]
        
        task = asyncio.create_task(send_stress_request(client, url, filters, plan, req_id))
        tasks.append(task)
        
        await asyncio.sleep(sleep_time)
        
    print("\n\n>>> Sendephase beendet. Warte auf ausstehende Worker-Antworten...")
    await asyncio.gather(*tasks, return_exceptions=True)
    print_status("✓", "Stresstest-Phase vollständig abgeschlossen.", "SUCCESS")

# --- MAIN RUNNER ---
async def main():
    print("====================================================")
    print("     AGENTIC WEB SCRAPER API TEST & STRESSTEST")
    print("====================================================")
    print(f"Target Server: {BASE_URL}")
    print(f"X-RapidAPI-Proxy-Secret: {'Ja (aus settings.json geladen)' if PROXY_SECRET else 'Nein (kein Secret geladen, teste direkt)'}")
    print(f"Logs werden geschrieben in: {LOG_FILE}\n")

    limits = httpx.Limits(max_keepalive_connections=30, max_connections=60)
    async with httpx.AsyncClient(limits=limits, timeout=60.0) as client:
        # Part 1: Features
        await run_functional_tests(client)
        
        # Part 2: Stress
        await run_stress_test(client)

    # --- PRINT FINAL REPORT ---
    print("\n====================================================")
    print("                ABSCHLUSS-BERICHT")
    print("====================================================")
    
    print("\n[PHASE 1] Feature- & Plan-Prüfungen:")
    passed_func = len([r for r in test_results["functional"] if r["status"] == "PASSED"])
    total_func = len(test_results["functional"])
    for res in test_results["functional"]:
        status_symbol = "✅" if res["status"] == "PASSED" else "❌"
        print(f" {status_symbol} {res['name']}: {res['status']}")
    print(f"-> Funktionstests bestanden: {passed_func}/{total_func}")

    stress_data = test_results["stress"]
    avg_duration = sum(stress_data["durations"]) / len(stress_data["durations"]) if stress_data["durations"] else 0
    print("\n[PHASE 2] Stresstest-Statistik:")
    print(f"  Gesendete Requests: {stress_data['total_requests']}")
    print(f"  🟢 200 OK Erfolge : {stress_data['success']}")
    print(f"  🟡 403 Forbidden  : {stress_data['forbidden']} (Gewollte Plan-Abweisungen)")
    print(f"  🔴 Fehlercodes    : {stress_data['failed']}")
    print(f"  ⏰ Timeout-Errors : {stress_data['timeout']}")
    print(f"  💥 Netzwerkfehler : {stress_data['network_error']}")
    print(f"  Mittlere Antwortzeit: {avg_duration:.2f} Sekunden")
    
    success_rate = (stress_data['success'] + stress_data['forbidden']) / stress_data['total_requests'] * 100 if stress_data['total_requests'] else 0
    print(f"  -> Erfolgsquote (inkl. 403s): {success_rate:.1f}%")
    print("====================================================")
    print(f"Detaillierte Fehlerlogs befinden sich in '{LOG_FILE}'.")
    print("====================================================")

if __name__ == "__main__":
    asyncio.run(main())
