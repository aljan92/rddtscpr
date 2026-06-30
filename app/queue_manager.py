import asyncio
import logging
import time
import uuid
from datetime import datetime
from sqlalchemy.orm import Session
from app.database import SessionLocal, RedditAccount
from app.scraper import get_subreddit_posts, get_post_comments

logger = logging.getLogger("rddtscpr.queue")

def is_temporary_network_issue(err: Exception) -> bool:
    """Prüft, ob es sich um einen temporären Proxy- oder Netzwerkfehler handelt."""
    err_msg_lower = str(err).lower()
    return any(
        x in err_msg_lower 
        for x in ["429", "too many requests", "timeout", "connect", "503", "502", "network security", "blocked", "leere antwort"]
    )

class ScrapeRequest:
    def __init__(self, action: str, params: dict, future: asyncio.Future, is_playground: bool = False):
        self.id = str(uuid.uuid4())
        self.action = action  # "subreddit" or "comments"
        self.params = params
        self.future = future
        self.attempts = 0
        self.failed_account_ids = set()
        self.status = "Wartend"  # "Wartend", "Cooldown", "Scraping"
        self.account_username = None
        self.created_at = datetime.utcnow()
        self.is_playground = is_playground

    def __lt__(self, other):
        # Fallback-Vergleich für die PriorityQueue
        return self.created_at < other.created_at

class ScrapeQueueManager:
    def __init__(self):
        self.queue = asyncio.PriorityQueue()
        self.worker_task = None
        self.refresh_task = None
        self._running = False
        self.cooldown_seconds = 3.0  # Mindestabstand zwischen Zugriffen desselben Accounts
        self.active_requests = {}  # id -> ScrapeRequest
        self.busy_account_ids = set()  # In-Memory Sperre für Accounts, die gerade arbeiten

    def start(self):
        if not self._running:
            self._running = True
            self.worker_task = asyncio.create_task(self._worker_loop())
            self.refresh_task = asyncio.create_task(self._session_refresh_loop())
            logger.info("ScrapeQueueManager erfolgreich gestartet (inkl. Session-Refresh Task).")

    async def stop(self):
        self._running = False
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
        if self.refresh_task:
            self.refresh_task.cancel()
            try:
                await self.refresh_task
            except asyncio.CancelledError:
                pass
        logger.info("ScrapeQueueManager beendet.")

    async def enqueue(self, action: str, params: dict, is_playground: bool = False) -> tuple[list, str, str]:
        """Reiht einen Request in die PriorityQueue ein und wartet asynchron auf das Ergebnis."""
        if not self._running:
            self.start()
            
        future = asyncio.get_running_loop().create_future()
        request = ScrapeRequest(action, params, future, is_playground)
        self.active_requests[request.id] = request
        
        # Neue Requests bekommen standardmäßig Priorität 10
        # Format in PriorityQueue: (priority, timestamp, request)
        await self.queue.put((10, time.time(), request))
        try:
            return await future
        finally:
            self.active_requests.pop(request.id, None)

    async def _worker_loop(self):
        logger.info("Queue Worker-Loop gestartet.")
        while self._running:
            try:
                # Element holen: (priority, timestamp, request)
                priority, timestamp, request = await self.queue.get()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fehler im Queue Worker-Loop beim Holen: {e}")
                await asyncio.sleep(1)
                continue
            
            # 1. Check: Wurde das Request/Future bereits abgebrochen oder abgeschlossen?
            if request.future.done():
                logger.info(f"Request {request.id} wurde bereits abgebrochen/beendet. Überspringe Verarbeitung.")
                self.queue.task_done()
                continue
            
            db = SessionLocal()
            try:
                # Einen Account suchen, der aktiv, nicht beschäftigt und nicht für diese Anfrage ausgeschlossen ist
                account = self._select_best_account(db, request.failed_account_ids, self.busy_account_ids)
                
                if not account:
                    # Kein freier Account vorhanden. Request zurücklegen (mit gleicher Priorität).
                    # Um eine CPU-intensive Endlosschleife zu verhindern, schlafen wir kurz verzögert.
                    db.close()
                    async def put_back():
                        await asyncio.sleep(0.5)
                        if self._running:
                            # 2. Check: Bevor wir es wieder einreihen, prüfen wir, ob es mittlerweile abgebrochen wurde
                            if not request.future.done():
                                await self.queue.put((priority, timestamp, request))
                            else:
                                logger.info(f"Request {request.id} wurde während des Wartens auf einen freien Account abgebrochen.")
                    asyncio.create_task(put_back())
                    self.queue.task_done()
                    continue
                
                # Account sperren
                self.busy_account_ids.add(account.id)
                request.account_username = account.username
                
                # Request asynchron in Hintergrund-Task ausführen, damit der Worker blockierungsfrei bleibt
                asyncio.create_task(self._process_request_concurrent(request, account.id, priority))
                
            except Exception as e:
                logger.error(f"Fehler bei der Account-Auswahl im Worker-Loop: {e}")
            finally:
                db.close()

    async def _process_request_concurrent(self, request: ScrapeRequest, account_id: int, current_priority: int):
        db = SessionLocal()
        try:
            # 3. Check: Direkt vor Beginn der Verarbeitung prüfen
            if request.future.done():
                logger.info(f"Request {request.id} ist vor Verarbeitungsbeginn abgebrochen worden. Stoppe Worker.")
                return

            account = db.query(RedditAccount).filter(RedditAccount.id == account_id).first()
            if not account or not account.is_active:
                logger.warning(f"Gewählter Account ID {account_id} ist nicht mehr aktiv oder vorhanden.")
                # Nur wieder einreihen, wenn das Future noch aktiv ist
                if not request.future.done():
                    await self.queue.put((current_priority, time.time(), request))
                return

            request.attempts += 1
            request.status = "Cooldown"
            
            # Cooldown für diesen Account einhalten (schläft nur in diesem Task!)
            await self._enforce_cooldown(account)

            # 4. Check: Nach dem Cooldown-Schlaf prüfen, ob das Future noch aktiv ist
            if request.future.done():
                logger.info(f"Request {request.id} wurde während des Account-Cooldowns abgebrochen. Stoppe Scraping.")
                return

            # Account als verwendet markieren
            account.last_used_at = datetime.utcnow()
            db.commit()

            session_state = account.session_state
            proxy_url = account.proxy_url

            logger.info(f"Verarbeite Request '{request.action}' (Versuch {request.attempts}) mit Account '{account.username}'")

            try:
                request.status = "Scraping"
                data, method_used, new_session = await self._execute_scrape(request.action, request.params, session_state, proxy_url)
                
                # 5. Check: Nach dem Scraping prüfen, ob der Client noch da ist
                if request.future.done():
                    logger.info(f"Request {request.id} wurde während des Scrapings abgebrochen. Verwerfe Ergebnis.")
                    return

                # Erfolg: Zähler zurücksetzen
                account.failure_count = 0
                if new_session:
                    account.session_state = new_session
                if not request.is_playground:
                    account.request_count = (account.request_count or 0) + 1
                db.commit()
                
                if not request.future.done():
                    request.future.set_result((data, method_used, account.username))
                
            except ValueError as val_error:
                # Client-Fehler (z.B. Subreddit existiert nicht). Kein Failover/Sperren!
                logger.warning(f"Client-Fehler beim Scraping: {val_error}")
                if not request.future.done():
                    request.future.set_exception(val_error)
                
            except Exception as scrape_error:
                logger.warning(f"Fehler beim Scraping mit Haupt-Proxy für Account '{account.username}': {scrape_error}")
                
                is_temp = is_temporary_network_issue(scrape_error)
                fallback_success = False
                
                # Fallback-Proxy verwenden, falls definiert
                if account.fallback_proxy_url:
                    # 6. Check: Vor dem Fallback-Proxy-Versuch prüfen
                    if request.future.done():
                        logger.info(f"Request {request.id} wurde vor Fallback-Versuch abgebrochen. Stoppe.")
                        return

                    logger.info(f"Probiere Fallback-Proxy für Account '{account.username}'...")
                    try:
                        request.status = "Scraping"
                        data, method_used, new_session = await self._execute_scrape(request.action, request.params, session_state, account.fallback_proxy_url)
                        
                        # 7. Check: Nach dem Fallback-Scraping prüfen
                        if request.future.done():
                            logger.info(f"Request {request.id} wurde während des Fallback-Scrapings abgebrochen. Verwerfe Ergebnis.")
                            return

                        account.failure_count = 0
                        if new_session:
                            account.session_state = new_session
                        if not request.is_playground:
                            account.request_count = (account.request_count or 0) + 1
                        db.commit()
                        
                        if not request.future.done():
                            request.future.set_result((data, method_used, account.username))
                        fallback_success = True
                    except ValueError as val_error:
                        logger.warning(f"Client-Fehler beim Scraping über Fallback-Proxy: {val_error}")
                        if not request.future.done():
                            request.future.set_exception(val_error)
                        return
                    except Exception as fallback_error:
                        logger.error(f"Fallback-Proxy für Account '{account.username}' ebenfalls fehlgeschlagen: {fallback_error}")
                        # Der letzte Fehler gilt
                        scrape_error = fallback_error
                        is_temp = is_temporary_network_issue(fallback_error)
                
                if not fallback_success:
                    if is_temp:
                        # Temporärer Fehler: Account nicht bestrafen, nur Zeitstempel erneuern, damit er im Cooldown liegt
                        logger.warning(f"Temporäres Problem bei '{account.username}'. Keine Deaktivierung. Temporärer Cooldown...")
                        account.last_used_at = datetime.utcnow()
                        db.commit()
                    else:
                        # Kritischer Fehler: Fehlerpunkte erhöhen
                        account.failure_count = (account.failure_count or 0) + 1
                        if account.failure_count >= 3:
                            account.is_active = False
                            logger.error(f"Account '{account.username}' wurde nach {account.failure_count} kritischen Fehlern DEAKTIVIERT.")
                        db.commit()
                    
                    # 8. Check: Vor dem Re-enqueuing prüfen
                    if request.future.done():
                        logger.info(f"Request {request.id} wurde vor Re-enqueuing abgebrochen. Keine Wiederholung.")
                        return

                    # Request mit anderem Account wiederholen
                    request.failed_account_ids.add(account.id)
                    if request.attempts < 4:
                        wait_time = 5 if request.attempts == 1 else (10 if request.attempts == 2 else 30)
                        logger.info(f"Versuch {request.attempts} fehlgeschlagen. Re-enqueuing in {wait_time}s mit Priorität 0...")
                        request.status = "Wartend"
                        request.account_username = None
                        
                        # Verzögertes Re-enqueuing in einem separaten async Task, um den Worker-Loop nicht zu blockieren
                        async def delayed_requeue(req, delay):
                            await asyncio.sleep(delay)
                            if self._running:
                                # 9. Check: Unmittelbar vor dem eigentlichen Re-queueing prüfen
                                if not req.future.done():
                                    logger.info(f"Delayed Re-enqueuing: Lege Request {req.id} (nach {delay}s) wieder in die Queue (Priorität 0)...")
                                    req.failed_account_ids.clear()
                                    await self.queue.put((0, time.time(), req))
                                else:
                                    logger.info(f"Request {req.id} wurde während der Re-queue-Verzögerung abgebrochen.")
                                
                        asyncio.create_task(delayed_requeue(request, wait_time))
                    else:
                        # Maximale Versuche erreicht
                        raise Exception(f"Fehlgeschlagen nach {request.attempts} Versuchen. Letzter Fehler: {scrape_error}")
                        
        except Exception as final_exception:
            logger.error(f"Request endgültig fehlgeschlagen: {final_exception}")
            if not request.future.done():
                request.future.set_exception(final_exception)
        finally:
            # Account entsperren und Task abschließen
            self.busy_account_ids.discard(account_id)
            self.queue.task_done()
            db.close()

    def _select_best_account(self, db: Session, excluded_ids: set, busy_ids: set = None) -> RedditAccount:
        """Wählt das am längsten unbenutzte aktive Konto aus, das weder in excluded_ids noch in busy_ids liegt."""
        query = db.query(RedditAccount).filter(RedditAccount.is_active == True)
        if excluded_ids:
            query = query.filter(~RedditAccount.id.in_(excluded_ids))
        if busy_ids:
            query = query.filter(~RedditAccount.id.in_(busy_ids))
            
        accounts = query.all()
        if not accounts:
            return None
            
        # Sortieren: Accounts ohne Benutzung zuerst, danach nach Zeitstempel aufsteigend
        accounts.sort(key=lambda a: a.last_used_at or datetime.min)
        return accounts[0]

    async def _enforce_cooldown(self, account: RedditAccount):
        """Erzwingt das Cooldown-Limit für das gewählte Konto."""
        if account.last_used_at:
            elapsed = (datetime.utcnow() - account.last_used_at).total_seconds()
            wait_time = max(0.0, self.cooldown_seconds - elapsed)
            if wait_time > 0:
                logger.info(f"Cooldown für Account '{account.username}': Warte {wait_time:.2f}s...")
                await asyncio.sleep(wait_time)

    async def _execute_scrape(self, action: str, params: dict, session_state: str, proxy_url: str) -> tuple[list, str, str]:
        """Führt das eigentliche Scraping-Modul aus."""
        if action == "subreddit":
            return await get_subreddit_posts(
                target=params["target"],
                sort=params["sort"],
                timeframe=params["timeframe"],
                limit=params["limit"],
                session_state=session_state,
                proxy_url=proxy_url
            )
        elif action == "comments":
            return await get_post_comments(
                post_url=params["post_url"],
                sort=params["sort"],
                limit=params["limit"],
                include_replies=params["include_replies"],
                load_more=params["load_more"],
                session_state=session_state,
                proxy_url=proxy_url
            )
        else:
            raise ValueError(f"Unbekannte Aktion: {action}")

    def get_queue_status(self) -> dict:
        """Gibt den aktuellen Status der Warteschlange und aktive Anfragen zurück."""
        sorted_requests = sorted(self.active_requests.values(), key=lambda r: r.created_at)
        items = []
        pending_count = 0
        active_count = 0
        
        for r in sorted_requests:
            if r.status == "Wartend":
                pending_count += 1
            else:
                active_count += 1
                
            items.append({
                "id": r.id,
                "action": "Subreddit" if r.action == "subreddit" else "Kommentare",
                "target": r.params.get("target") or r.params.get("post_url", ""),
                "status": r.status,
                "account": r.account_username or "-",
                "attempts": r.attempts,
                "age_seconds": int((datetime.utcnow() - r.created_at).total_seconds())
            })
            
        return {
            "stats": {
                "total": len(items),
                "pending": pending_count,
                "active": active_count,
                "cooldown_seconds": self.cooldown_seconds
            },
            "requests": items
        }

    async def _session_refresh_loop(self):
        logger.info("Session-Refresh-Loop gestartet.")
        await asyncio.sleep(10)
        
        while self._running:
            try:
                db = SessionLocal()
                # Alle Accounts laden, um auch inaktive bei wiedererlangter Verbindung zu reaktivieren
                accounts = db.query(RedditAccount).all()
                logger.info(f"Session-Refresh: Prüfe {len(accounts)} Accounts (aktiv und inaktiv)...")
                
                for account in accounts:
                    if not self._running:
                        break
                    try:
                        was_active = account.is_active
                        if not was_active:
                            logger.info(f"Session-Refresh: Account '{account.username}' ist inaktiv. Versuche Reaktivierungs-Check...")
                        
                        success = await self._refresh_account_session(db, account)
                        
                        if success and not was_active:
                            account.is_active = True
                            account.failure_count = 0
                            db.commit()
                            logger.info(f"Session-Refresh: Account '{account.username}' wurde erfolgreich REAKTIVIERT und ist wieder einsatzbereit.")
                    except Exception as e:
                        logger.error(f"Fehler beim Refresh des Accounts '{account.username}': {e}")
                    await asyncio.sleep(5)
                    
                db.close()
            except Exception as e:
                logger.error(f"Fehler im Session-Refresh-Loop: {e}")
                
            # Alle 1 Minute ausführen (6 * 10 Sekunden)
            for _ in range(6):
                if not self._running:
                    break
                await asyncio.sleep(10)

    async def _refresh_account_session(self, db: Session, account: RedditAccount) -> bool:
        if not account.session_state:
            logger.info(f"Session-Refresh: Keine Cookies für {account.username} vorhanden. Starte Auto-Login...")
            return await self._auto_login_account(db, account)
            
        from playwright.async_api import async_playwright
        from app.scraper import launch_browser
        import json
        
        logger.info(f"Session-Refresh: Prüfe Session für '{account.username}' via Playwright...")
        
        try:
            async with async_playwright() as p:
                browser, context, page = await launch_browser(p, account.session_state, account.proxy_url)
                try:
                    # Rufe die Test-URL über den echten Browser auf
                    await page.goto("https://www.reddit.com/r/popular.json?limit=1", wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(2000)
                    
                    content = ""
                    pre_elem = await page.query_selector("pre")
                    if pre_elem:
                        content = await pre_elem.inner_text()
                    else:
                        content = await page.content()
                        
                    # Wenn wir echte Reddit-Daten sehen, ist die Session (Cookies) gültig!
                    if "data" in content or "children" in content:
                        logger.info(f"Session-Refresh: Session für '{account.username}' ist via Playwright GÜLTIG. Cookies werden aktualisiert...")
                        new_state = await context.storage_state()
                        account.session_state = json.dumps(new_state)
                        account.failure_count = 0
                        account.is_active = True
                        db.commit()
                        return True
                    else:
                        logger.warning(f"Session-Refresh: Keine JSON-Struktur im Browser-Inhalt gefunden. Vermute ungültige Session für '{account.username}'.")
                finally:
                    await page.close()
                    await context.close()
                    await browser.close()
        except Exception as e:
            logger.error(f"Session-Refresh: Fehler beim Playwright-Verbindungstest für '{account.username}': {e}")
            
        # Wenn der Verbindungstest oder Cookie-Test fehlgeschlagen ist, starten wir Auto-Login als Fallback
        logger.info(f"Session-Refresh: Verbindungstest fehlgeschlagen oder Session abgelaufen. Starte Auto-Login...")
        return await self._auto_login_account(db, account)

    async def _auto_login_account(self, db: Session, account: RedditAccount) -> bool:
        from app.auth import login_to_reddit
        try:
            logger.info(f"Auto-Login: Führe Login-Refresh für Account '{account.username}' durch...")
            session_state_json = await login_to_reddit(
                username=account.username,
                password=account.password,
                proxy_url=account.proxy_url
            )
            account.session_state = session_state_json
            account.failure_count = 0
            account.is_active = True
            account.screenshot_viewed = True
            db.commit()
            logger.info(f"Auto-Login: Login für '{account.username}' erfolgreich abgeschlossen.")
            return True
        except Exception as e:
            logger.error(f"Auto-Login: Fehler bei Login für '{account.username}': {e}")
            # Cookies bei Login-Fehlern nicht löschen, um temporäre Fehler zu tolerieren
            account.failure_count += 1
            account.screenshot_viewed = False
            if account.failure_count >= 3:
                account.is_active = False
                logger.error(f"Auto-Login: Account '{account.username}' wurde nach {account.failure_count} aufeinanderfolgenden Fehlern DEAKTIVIERT.")
            db.commit()
            return False

# Globaler Singleton-Manager
scrape_queue = ScrapeQueueManager()
