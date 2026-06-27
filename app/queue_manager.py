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
    def __init__(self, action: str, params: dict, future: asyncio.Future):
        self.id = str(uuid.uuid4())
        self.action = action  # "subreddit" or "comments"
        self.params = params
        self.future = future
        self.attempts = 0
        self.failed_account_ids = set()
        self.status = "Wartend"  # "Wartend", "Cooldown", "Scraping"
        self.account_username = None
        self.created_at = datetime.utcnow()

class ScrapeQueueManager:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.worker_task = None
        self._running = False
        self.cooldown_seconds = 3.0  # Mindestabstand zwischen Zugriffen desselben Accounts
        self.active_requests = {}  # id -> ScrapeRequest

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

    async def enqueue(self, action: str, params: dict) -> tuple[list, str, str]:
        """Reiht einen Request ein und wartet asynchron auf das Ergebnis."""
        if not self._running:
            self.start()
            
        future = asyncio.get_running_loop().create_future()
        request = ScrapeRequest(action, params, future)
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
                result = await self._execute_scrape(request.action, request.params, session_state, proxy_url)
                
                # Erfolg: Counter zurücksetzen, Request-Zähler erhöhen und Ergebnis zurückgeben
                account.failure_count = 0
                account.request_count = (account.request_count or 0) + 1
                db.commit()
                request.future.set_result((result[0], result[1], account.username))
                
            except ValueError as val_error:
                # Client-Fehler (z.B. Subreddit existiert nicht). Kein Failover/Sperren des Accounts!
                logger.warning(f"Client-Fehler beim Scraping (z.B. 404/Private/Gesperrt): {val_error}")
                request.future.set_exception(val_error)
                return
            except Exception as scrape_error:
                logger.warning(f"Fehler beim Scraping mit Haupt-Proxy für Account '{account.username}': {scrape_error}")
                
                # Fallback-Proxy versuchen, falls definiert
                if account.fallback_proxy_url:
                    logger.info(f"Probiere Fallback-Proxy für Account '{account.username}'...")
                    try:
                        request.status = "Scraping"
                        result = await self._execute_scrape(request.action, request.params, session_state, account.fallback_proxy_url)
                        account.failure_count = 0
                        account.request_count = (account.request_count or 0) + 1
                        db.commit()
                        request.future.set_result((result[0], result[1], account.username))
                        return
                    except ValueError as val_error:
                        logger.warning(f"Client-Fehler beim Scraping über Fallback-Proxy: {val_error}")
                        request.future.set_exception(val_error)
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

# Globaler Singleton-Manager
scrape_queue = ScrapeQueueManager()
