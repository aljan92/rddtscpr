import asyncio
import logging
import time
import uuid
from datetime import datetime
from sqlalchemy.orm import Session
from app.database import SessionLocal, RedditAccount
from app.scraper import get_subreddit_posts, get_post_comments

logger = logging.getLogger("rddtscpr.queue")

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

class ScrapeQueueManager:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.worker_task = None
        self.refresh_task = None
        self._running = False
        self.cooldown_seconds = 3.0  # Mindestabstand zwischen Zugriffen desselben Accounts
        self.active_requests = {}  # id -> ScrapeRequest

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
        """Reiht einen Request ein und wartet asynchron auf das Ergebnis."""
        if not self._running:
            self.start()
            
        future = asyncio.get_running_loop().create_future()
        request = ScrapeRequest(action, params, future, is_playground)
        self.active_requests[request.id] = request
        await self.queue.put(request)
        try:
            return await future
        finally:
            self.active_requests.pop(request.id, None)

    async def _worker_loop(self):
        while self._running:
            try:
                request = await self.queue.get()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fehler im Queue Worker-Loop: {e}")
                await asyncio.sleep(1)
                continue
            
            try:
                await self._process_request(request)
            except Exception as e:
                logger.error(f"Fehler bei der Verarbeitung in der Queue: {e}")
            finally:
                self.queue.task_done()

    async def _process_request(self, request: ScrapeRequest):
        request.attempts += 1
        db = SessionLocal()
        
        try:
            # 1. Besten Account finden
            account = self._select_best_account(db, request.failed_account_ids)
            if not account:
                # Kein Account verfügbar (alle gesperrt oder keiner angelegt)
                error_msg = "Kein aktiver Reddit-Account in der Datenbank vorhanden."
                logger.error(error_msg)
                request.future.set_exception(Exception(error_msg))
                return

            request.account_username = account.username

            # 2. Cooldown einhalten
            request.status = "Cooldown"
            await self._enforce_cooldown(account)

            # 3. Account als verwendet markieren
            account.last_used_at = datetime.utcnow()
            db.commit()

            # Parameter vorbereiten
            session_state = account.session_state
            # Haupt-Proxy verwenden
            proxy_url = account.proxy_url

            logger.info(f"Verarbeite Request '{request.action}' (Versuch {request.attempts}) mit Account '{account.username}'")

            try:
                # 4. Request ausführen
                request.status = "Scraping"
                data, method_used, new_session = await self._execute_scrape(request.action, request.params, session_state, proxy_url)
                
                # Erfolg: Counter zurücksetzen, Request-Zähler erhöhen und Ergebnis zurückgeben
                account.failure_count = 0
                if new_session:
                    account.session_state = new_session
                if not request.is_playground:
                    account.request_count = (account.request_count or 0) + 1
                db.commit()
                request.future.set_result((data, method_used, account.username))
                
            except ValueError as val_error:
                # Client-Fehler (z.B. Subreddit existiert nicht). Kein Failover/Sperren des Accounts!
                logger.warning(f"Client-Fehler beim Scraping (z.B. 404/Private/Gesperrt): {val_error}")
                request.future.set_exception(val_error)
                return
            except Exception as scrape_error:
                logger.warning(f"Fehler beim Scraping mit Haupt-Proxy für Account '{account.username}': {scrape_error}")
                
                # Prüfen, ob es sich um ein temporäres Netzwerk-, Proxy- oder Rate-Limit-Problem handelt
                err_msg_lower = str(scrape_error).lower()
                is_temporary_network_issue = any(
                    x in err_msg_lower 
                    for x in ["429", "too many requests", "timeout", "connect", "503", "502", "network security", "blocked"]
                )
                
                if is_temporary_network_issue:
                    logger.warning(f"Temporäres Netzwerk/Proxy/Rate-Limit-Problem beim Scraping: {scrape_error}. Account '{account.username}' wird nicht bestraft.")
                else:
                    account.failure_count = (account.failure_count or 0) + 1
                    db.commit()
                
                # Fallback-Proxy versuchen, falls definiert
                if account.fallback_proxy_url:
                    logger.info(f"Probiere Fallback-Proxy für Account '{account.username}'...")
                    try:
                        request.status = "Scraping"
                        data, method_used, new_session = await self._execute_scrape(request.action, request.params, session_state, account.fallback_proxy_url)
                        account.failure_count = 0
                        if new_session:
                            account.session_state = new_session
                        if not request.is_playground:
                            account.request_count = (account.request_count or 0) + 1
                        db.commit()
                        request.future.set_result((data, method_used, account.username))
                        return
                    except ValueError as val_error:
                        logger.warning(f"Client-Fehler beim Scraping über Fallback-Proxy: {val_error}")
                        request.future.set_exception(val_error)
                        return
                    except Exception as fallback_error:
                        logger.error(f"Fallback-Proxy für Account '{account.username}' ebenfalls fehlgeschlagen: {fallback_error}")
                        # Session-Cookies werden nicht automatisch gelöscht, um manuelle Cookies zu schonen.
                
                # Wenn auch der Fallback-Proxy fehlschlägt (oder kein Fallback definiert war):
                # failure_count wurde bereits oben (falls kein temporärer Fehler) erhöht, hier nur prüfen ob Schwelle erreicht
                if account.failure_count >= 3:
                    account.is_active = False
                    logger.error(f"Account '{account.username}' wurde nach {account.failure_count} aufeinanderfolgenden Fehlern DEAKTIVIERT.")
                db.commit()
                
                # Request mit anderem Account wiederholen
                request.failed_account_ids.add(account.id)
                if request.attempts < 3:
                    logger.info(f"Re-enqueuing Request für einen weiteren Versuch mit anderem Account...")
                    request.status = "Wartend"
                    request.account_username = None
                    # Zurück in die Queue legen
                    await self.queue.put(request)
                else:
                    # Maximale Versuche erreicht
                    raise Exception(f"Fehlgeschlagen nach 3 Versuchen. Letzter Fehler: {scrape_error}")
                    
        except Exception as final_exception:
            logger.error(f"Request endgültig fehlgeschlagen: {final_exception}")
            request.future.set_exception(final_exception)
        finally:
            db.close()

    def _select_best_account(self, db: Session, excluded_ids: set) -> RedditAccount:
        """Wählt das am längsten unbenutzte aktive Konto aus, das nicht in excluded_ids liegt."""
        query = db.query(RedditAccount).filter(RedditAccount.is_active == True)
        if excluded_ids:
            query = query.filter(~RedditAccount.id.in_(excluded_ids))
            
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
                accounts = db.query(RedditAccount).filter(RedditAccount.is_active == True).all()
                logger.info(f"Session-Refresh: Prüfe {len(accounts)} aktive Accounts...")
                
                for account in accounts:
                    if not self._running:
                        break
                    try:
                        await self._refresh_account_session(db, account)
                    except Exception as e:
                        logger.error(f"Fehler beim Refresh des Accounts '{account.username}': {e}")
                    await asyncio.sleep(5)
                    
                db.close()
            except Exception as e:
                logger.error(f"Fehler im Session-Refresh-Loop: {e}")
                
            for _ in range(180):
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
