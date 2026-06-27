import asyncio
import logging
import time
from datetime import datetime
from sqlalchemy.orm import Session
from app.database import SessionLocal, RedditAccount
from app.scraper import get_subreddit_posts, get_post_comments

logger = logging.getLogger("rddtscpr.queue")

class ScrapeRequest:
    def __init__(self, action: str, params: dict, future: asyncio.Future):
        self.action = action  # "subreddit" or "comments"
        self.params = params
        self.future = future
        self.attempts = 0
        self.failed_account_ids = set()

class ScrapeQueueManager:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.worker_task = None
        self._running = False
        self.cooldown_seconds = 3.0  # Mindestabstand zwischen Zugriffen desselben Accounts

    def start(self):
        if not self._running:
            self._running = True
            self.worker_task = asyncio.create_task(self._worker_loop())
            logger.info("ScrapeQueueManager erfolgreich gestartet.")

    async def stop(self):
        self._running = False
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
            logger.info("ScrapeQueueManager beendet.")

    async def enqueue(self, action: str, params: dict) -> tuple[list, str]:
        """Reiht einen Request ein und wartet asynchron auf das Ergebnis."""
        if not self._running:
            self.start()
            
        future = asyncio.get_running_loop().create_future()
        request = ScrapeRequest(action, params, future)
        await self.queue.put(request)
        return await future

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
            
            # Startet die Verarbeitung als eigenständigen Task, um Parallelität zu ermöglichen
            asyncio.create_task(self._process_request(request))
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

            # 2. Cooldown einhalten
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
                result = await self._execute_scrape(request.action, request.params, session_state, proxy_url)
                
                # Erfolg: Counter zurücksetzen und Ergebnis zurückgeben
                account.failure_count = 0
                db.commit()
                request.future.set_result(result)
                
            except Exception as scrape_error:
                logger.warning(f"Fehler beim Scraping mit Haupt-Proxy für Account '{account.username}': {scrape_error}")
                
                # Fallback-Proxy versuchen, falls definiert
                if account.fallback_proxy_url:
                    logger.info(f"Probiere Fallback-Proxy für Account '{account.username}'...")
                    try:
                        result = await self._execute_scrape(request.action, request.params, session_state, account.fallback_proxy_url)
                        account.failure_count = 0
                        db.commit()
                        request.future.set_result(result)
                        return
                    except Exception as fallback_error:
                        logger.error(f"Fallback-Proxy für Account '{account.username}' ebenfalls fehlgeschlagen: {fallback_error}")
                
                # Wenn auch der Fallback-Proxy fehlschlägt (oder kein Fallback definiert war):
                # Account Fehler zählen
                account.failure_count += 1
                if account.failure_count >= 3:
                    account.is_active = False
                    logger.error(f"Account '{account.username}' wurde nach 3 aufeinanderfolgenden Fehlern DEAKTIVIERT.")
                db.commit()
                
                # Request mit anderem Account wiederholen
                request.failed_account_ids.add(account.id)
                if request.attempts < 3:
                    logger.info(f"Re-enqueuing Request für einen weiteren Versuch mit anderem Account...")
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

    async def _execute_scrape(self, action: str, params: dict, session_state: str, proxy_url: str) -> tuple[list, str]:
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

# Globaler Singleton-Manager
scrape_queue = ScrapeQueueManager()
