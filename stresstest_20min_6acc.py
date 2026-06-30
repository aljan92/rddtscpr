import asyncio
import httpx
import time
import logging

# --- LOGGING SETUP ---
logging.basicConfig(
    filename="api_direct_stresstest_20min_6acc.log",
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

BASE_URL = "https://api.angermann.work"

COMMON_HEADERS = {
    "X-RapidAPI-Proxy-Secret": "e1a29790-7252-11f1-b46b-8dfda4a1b8ae",
    "Content-Type": "application/json"
}

TEST_POST_URL = "https://www.reddit.com/r/beziehungen/comments/1u8hnzu/bida_wenn_ich_meiner_partnerin_und_ihren_kindern/"

async def send_request(client, endpoint, params, subscription_tier, request_id):
    headers = COMMON_HEADERS.copy()
    headers["X-RapidAPI-Subscription"] = subscription_tier

    start = time.time()
    try:
        response = await client.get(f"{BASE_URL}{endpoint}", params=params, headers=headers)
        duration = time.time() - start
        
        if response.status_code == 200:
            print(".", end="", flush=True)
        else:
            print(f"\n⚠️ [Req {request_id}] Status {response.status_code} bei {subscription_tier}-Anfrage ({duration:.2f}s)")
            logging.warning(
                f"Req {request_id} | Tier: {subscription_tier} | Endpoint: {endpoint} | Params: {params} | "
                f"Status: {response.status_code} | Zeit: {duration:.2f}s | Response: {response.text}"
            )
            
    except httpx.TimeoutException:
        duration = time.time() - start
        print(f"\n🛑 [Req {request_id}] TIMEOUT nach {duration:.2f}s bei {subscription_tier}")
        logging.error(f"Req {request_id} TIMEOUT | Tier: {subscription_tier} | Endpoint: {endpoint} nach {duration:.2f}s")
        
    except Exception as e:
        duration = time.time() - start
        print(f"\n💥 [Req {request_id}] NETZWERK-FEHLER ({duration:.2f}s)")
        logging.error(f"Req {request_id} NETZWERK-FEHLER | Endpoint: {endpoint} | Error: {e}")

async def main():
    req_counter = 0
    print("Starte 20-Minuten Stresstest (optimiert für 6 Accounts & 20s Cooldown) auf api.angermann.work.")
    print("Erfolgreiche 200er-Anfragen werden als Punkte (....) dargestellt.")
    print("Fehler werden in 'api_direct_stresstest_20min_6acc.log' protokolliert.\n")
    
    limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
    
    async with httpx.AsyncClient(limits=limits, timeout=90.0) as client:
        
        # --- PHASE 1: Normaler Rundlauf (10 Min = 600 Sek) ---
        # Frequenz: 1 Request alle 6 Sekunden.
        # Erwartete Last: 0.166 Req/Sek. (6 Accounts bewältigen das spielend ohne Queue-Stau).
        print("\n>>> Phase 1 gestartet (Normaler Traffic - 1 Request alle 6 Sekunden)...")
        p1_tasks = []
        end_time = time.time() + 600
        while time.time() < end_time:
            req_counter += 1
            task = asyncio.create_task(send_request(client, "/v1/subreddit-posts", {"target": "technology", "limit": 5}, "BASIC", req_counter))
            p1_tasks.append(task)
            await asyncio.sleep(6)
            
        print("\n>>> Warte auf Fertigstellung aller Phase 1 Requests...")
        await asyncio.gather(*p1_tasks, return_exceptions=True)
        print("✓ Phase 1 abgeschlossen.")
        
        # --- PHASE 2: Peak Traffic (5 Min = 300 Sek) ---
        # Frequenz: 1 Request alle 3 Sekunden.
        # Erwartete Last: 0.333 Req/Sek. (Liegt leicht über der Kapazität von 0.279 Req/Sek.
        # Die Queue wird sich leicht füllen, max. Wartezeit am Ende der Phase ca. 58 Sek., keine Timeouts!).
        print("\n>>> Phase 2 gestartet (Peak Traffic - 1 Request alle 3 Sekunden)...")
        p2_tasks = []
        end_time = time.time() + 300
        while time.time() < end_time:
            req_counter += 1
            task = asyncio.create_task(send_request(client, "/v1/post-comments", {"post_url": TEST_POST_URL, "load_more": "true"}, "PRO", req_counter))
            p2_tasks.append(task)
            await asyncio.sleep(3)
            
        print("\n>>> Phase 2 Sende-Intervall beendet. Warte auf die Abarbeitung der Queue auf dem Server...")
        await asyncio.gather(*p2_tasks, return_exceptions=True)
        print("✓ Phase 2 komplett abgeschlossen und Queue leer.")
        
        # --- PHASE 3: Fehler- & Validierungstest (5 Min = 300 Sek) ---
        # Frequenz: Alle 10 Sekunden je 1 BASIC load_more (403) und 1 ungültiges Subreddit (404).
        print("\n>>> Phase 3 gestartet (Validierungstest - 403/404 Fehler-Checks)...")
        p3_tasks = []
        end_time = time.time() + 300
        while time.time() < end_time:
            # Test 1: BASIC-User schickt load_more=true -> Erwartet: 403 Forbidden
            req_counter += 1
            t1 = asyncio.create_task(send_request(client, "/v1/post-comments", {"post_url": TEST_POST_URL, "load_more": "true"}, "BASIC", req_counter))
            p3_tasks.append(t1)
            
            # Test 2: Ungültiges Subreddit -> Erwartet: Sauberes 404
            req_counter += 1
            t2 = asyncio.create_task(send_request(client, "/v1/subreddit-posts", {"target": "dieses_subreddit_gibt_es_nicht_12345"}, "PRO", req_counter))
            p3_tasks.append(t2)
            
            await asyncio.sleep(10)

        print("\n>>> Warte auf Fertigstellung aller Phase 3 Requests...")
        await asyncio.gather(*p3_tasks, return_exceptions=True)

    print("\n\nTest erfolgreich durchgelaufen! Überprüfe jetzt die Datei 'api_direct_stresstest_20min_6acc.log'.")

if __name__ == "__main__":
    asyncio.run(main())
