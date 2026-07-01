import asyncio
import logging
import time
import uuid
from datetime import datetime
from sqlalchemy.orm import Session
from app.database import SessionLocal, RedditAccount, APIRequestLog, SystemSetting
from app.scraper import get_subreddit_posts, get_post_comments, NSFWRequiredException

logger = logging.getLogger("rddtscpr.queue")

# Adaptive Cooldown-Stufen: Schwellwerte = maximale Requests im 60s-Fenster für diese Stufe
ADAPTIVE_TIERS = [
    {"name": "Sprint",    "cooldown": 2,  "threshold": 5},
    {"name": "Normal",    "cooldown": 5,  "threshold": 10},
    {"name": "Vorsicht",  "cooldown": 10, "threshold": 15},
    {"name": "Defensiv",  "cooldown": 15, "threshold": 20},
    {"name": "Maximum",   "cooldown": 20, "threshold": float("inf")},
]

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
        self.last_tried_username = None
        self.created_at = datetime.utcnow()
        self.is_playground = is_playground
        self.requires_nsfw_account = False
        self.task = None

    def __lt__(self, other):
        # Fallback-Vergleich für die PriorityQueue
        return self.created_at < other.created_at

class ScrapeQueueManager:
    def __init__(self):
        self.queue = asyncio.PriorityQueue()
        self.worker_task = None
        self.refresh_task = None
        self.load_monitor_task = None
        self._running = False
        self.cooldown_seconds = 3.0  # Mindestabstand zwischen Zugriffen desselben Accounts (Fester Modus)
        self.cooldown_mode = "fixed"  # "fixed" oder "auto"
        self.max_accountless_sessions = 5  # Maximale Anzahl paralleler accountloser Sessions
        self.rotating_proxy_url = ""  # Rotierende Proxy URL (z.B. Evomi)
        self.busy_session_ids = set()  # In-Memory Sperre für aktive Session-Nummern (z.B. {1, 2})
        self.account_request_timestamps = {}  # account_id -> list of timestamps (rolling 60s)
        self.account_last_request_time = {}  # account_id -> float (timestamp)
        self.account_adaptive_tier = {}      # account_id -> int (tier index)
        self.active_requests = {}  # id -> ScrapeRequest
        self.busy_account_ids = set()  # In-Memory Sperre für Accounts, die gerade arbeiten
        self.load_history = []  # [(timestamp, load_pct)] over the last 24 hours
        self.sparkline_history = []  # last 60 load values for sparkline
        self.wait_times = []  # delay in seconds for last 100 requests

    def start(self):
        if not self._running:
            self._running = True
            
            # Cooldown und Modus aus Datenbank laden (für Persistenz nach Deployments)
            try:
                with SessionLocal() as db:
                    setting = db.query(SystemSetting).filter(SystemSetting.key == "cooldown_seconds").first()
                    if setting:
                        self.cooldown_seconds = float(setting.value)
                        logger.info(f"Cooldown-Zeit aus DB geladen: {self.cooldown_seconds}s")
                    else:
                        setting = SystemSetting(key="cooldown_seconds", value=str(self.cooldown_seconds))
                        db.add(setting)
                        db.commit()
                        logger.info(f"Standard-Cooldown-Zeit in DB angelegt: {self.cooldown_seconds}s")
                    
                    # Cooldown-Modus laden
                    mode_setting = db.query(SystemSetting).filter(SystemSetting.key == "cooldown_mode").first()
                    if mode_setting:
                        self.cooldown_mode = mode_setting.value
                        logger.info(f"Cooldown-Modus aus DB geladen: {self.cooldown_mode}")
                    else:
                        mode_setting = SystemSetting(key="cooldown_mode", value=self.cooldown_mode)
                        db.add(mode_setting)
                        db.commit()
                        logger.info(f"Standard-Cooldown-Modus in DB angelegt: {self.cooldown_mode}")
                        
                    # Max accountless sessions laden
                    sessions_setting = db.query(SystemSetting).filter(SystemSetting.key == "max_accountless_sessions").first()
                    if sessions_setting:
                        self.max_accountless_sessions = int(sessions_setting.value)
                        logger.info(f"Max-Accountless-Sessions aus DB geladen: {self.max_accountless_sessions}")
                    else:
                        sessions_setting = SystemSetting(key="max_accountless_sessions", value=str(self.max_accountless_sessions))
                        db.add(sessions_setting)
                        db.commit()
                        logger.info(f"Standard-Max-Accountless-Sessions in DB angelegt: {self.max_accountless_sessions}")
                        
                    # Rotating proxy URL laden
                    proxy_setting = db.query(SystemSetting).filter(SystemSetting.key == "rotating_proxy_url").first()
                    if proxy_setting:
                        self.rotating_proxy_url = proxy_setting.value
                        logger.info(f"Rotating-Proxy-URL aus DB geladen: {self.rotating_proxy_url}")
                    else:
                        proxy_setting = SystemSetting(key="rotating_proxy_url", value=self.rotating_proxy_url)
                        db.add(proxy_setting)
                        db.commit()
                        logger.info(f"Standard-Rotating-Proxy-URL in DB angelegt: {self.rotating_proxy_url}")
            except Exception as e:
                logger.error(f"Fehler beim Laden der Einstellungen aus DB: {e}")

            self.worker_task = asyncio.create_task(self._worker_loop())
            self.refresh_task = asyncio.create_task(self._session_refresh_loop())
            self.load_monitor_task = asyncio.create_task(self._load_monitor_loop())
            logger.info("ScrapeQueueManager erfolgreich gestartet (inkl. Session-Refresh & Load-Monitor Tasks).")

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
        if self.load_monitor_task:
            self.load_monitor_task.cancel()
            try:
                await self.load_monitor_task
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
            return await asyncio.wait_for(future, timeout=90.0)
        except asyncio.TimeoutError as e:
            future.cancel()
            if request.task and not request.task.done():
                logger.info(f"Request {request.id} wegen Queue-Timeout abgebrochen. Storniere laufenden Scraper-Hintergrundtask...")
                request.task.cancel()
            logger.info(f"Request {request.id} wurde wegen Queue-Timeout (90s) abgebrochen. Future storniert.")
            # Letzten versuchten Account an Exception anhängen
            e.reddit_username = request.last_tried_username
            raise e
        except asyncio.CancelledError as e:
            future.cancel()
            if request.task and not request.task.done():
                logger.info(f"Request {request.id} abgebrochen. Storniere laufenden Scraper-Hintergrundtask...")
                request.task.cancel()
            logger.info(f"Request {request.id} wurde abgebrochen (Client-Timeout/Disconnect). Future storniert.")
            # Letzten versuchten Account an Exception anhängen
            e.reddit_username = request.last_tried_username
            raise e
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
                assigned_account_id = None
                assigned_session_slot = None
                
                # 2. Check: Erfordert der Request einen echten NSFW-Account?
                if request.requires_nsfw_account:
                    # Zwingend echten Reddit-Account suchen
                    account = self._select_best_account(db, request.failed_account_ids, self.busy_account_ids)
                    if account:
                        assigned_account_id = account.id
                        self.busy_account_ids.add(account.id)
                        request.account_username = account.username
                        request.last_tried_username = account.username
                else:
                    # Standard-Anfrage: Bevorzuge in-memory Session-Slots
                    free_slot = None
                    for slot in range(1, self.max_accountless_sessions + 1):
                        if slot not in self.busy_session_ids:
                            free_slot = slot
                            break
                    
                    if free_slot is not None:
                        assigned_session_slot = free_slot
                        self.busy_session_ids.add(free_slot)
                        request.account_username = f"Session {free_slot}"
                        request.last_tried_username = f"Session {free_slot}"
                    else:
                        # Fallback: Freien echten Account suchen
                        account = self._select_best_account(db, request.failed_account_ids, self.busy_account_ids)
                        if account:
                            assigned_account_id = account.id
                            self.busy_account_ids.add(account.id)
                            request.account_username = account.username
                            request.last_tried_username = account.username
                
                if assigned_account_id is None and assigned_session_slot is None:
                    # Keine freien Ressourcen vorhanden. Request zurücklegen (mit gleicher Priorität).
                    db.close()
                    async def put_back():
                        await asyncio.sleep(0.5)
                        if self._running:
                            if not request.future.done():
                                await self.queue.put((priority, timestamp, request))
                            else:
                                logger.info(f"Request {request.id} wurde während des Wartens auf freie Ressourcen abgebrochen.")
                    asyncio.create_task(put_back())
                    self.queue.task_done()
                    continue
                
                # Request asynchron in Hintergrund-Task ausführen
                task = asyncio.create_task(self._process_request_concurrent(
                    request, 
                    account_id=assigned_account_id, 
                    session_slot=assigned_session_slot, 
                    current_priority=priority
                ))
                request.task = task
                
            except Exception as e:
                logger.error(f"Fehler bei der Ressourcen-Zuweisung im Worker-Loop: {e}")
            finally:
                db.close()

    def _prepare_rotating_proxy(self) -> str:
        """Generiert eine frische IP für einen Job durch Anfügen einer zufälligen Session-ID an die Proxy-URL (Passwort-Suffix)."""
        if not self.rotating_proxy_url:
            return None
        from urllib.parse import urlparse
        try:
            parsed = urlparse(self.rotating_proxy_url)
            if not parsed.username:
                return self.rotating_proxy_url
            
            # Zufälligen Session-Suffix generieren
            session_suffix = f"_hardsession-{uuid.uuid4().hex[:8].upper()}"
            
            netloc = parsed.netloc
            if '@' in netloc:
                parts = netloc.split('@', 1)
                credentials = parts[0]
                host_port = parts[1]
                if ':' in credentials:
                    user, pw = credentials.split(':', 1)
                    # Vorhandene Suffixe entfernen, um Doppelungen zu vermeiden
                    import re
                    clean_pw = re.sub(r"_(?:hard)?session-[A-Za-z0-9]+", "", pw)
                    # Evomi verlangt den Session-Suffix am Passwort, nicht am Benutzernamen
                    new_pw = f"{clean_pw}{session_suffix}"
                    netloc = f"{user}:{new_pw}@{host_port}"
                else:
                    new_user = f"{credentials}{session_suffix}"
                    netloc = f"{new_user}@{host_port}"
            
            return f"{parsed.scheme}://{netloc}{parsed.path}"
        except Exception as e:
            logger.error(f"Fehler beim Erstellen des rotierenden Proxys: {e}")
            return self.rotating_proxy_url

    async def _process_request_concurrent(self, request: ScrapeRequest, account_id: int = None, session_slot: int = None, current_priority: int = 10):
        db = None
        try:
            # Warteschlangen-Wartezeit erfassen
            wait_time = (datetime.utcnow() - request.created_at).total_seconds()
            self.wait_times.append(wait_time)
            if len(self.wait_times) > 100:
                self.wait_times.pop(0)

            # 3. Check: Direkt vor Beginn der Verarbeitung prüfen
            if request.future.done():
                logger.info(f"Request {request.id} ist vor Verarbeitungsbeginn abgebrochen worden. Stoppe Worker.")
                return

            if session_slot is not None:
                # ==========================================
                # VIRTUELLE SESSION ABARBEITUNG (Accountless)
                # ==========================================
                username = f"Session {session_slot}"
                proxy_url = self._prepare_rotating_proxy()
                session_state = None
                
                request.attempts += 1
                logger.info(f"Verarbeite Request '{request.action}' (Versuch {request.attempts}) mit virtueller Session '{username}'")
                
                try:
                    request.status = "Scraping"
                    data, method_used, new_session = await self._execute_scrape(request.action, request.params, session_state, proxy_url)
                    
                    if request.future.done():
                        logger.info(f"Request {request.id} wurde während des Scrapings abgebrochen. Verwerfe Ergebnis.")
                        return
                        
                    if not request.future.done():
                        request.future.set_result((data, method_used, username))
                        
                except NSFWRequiredException as nsfw_error:
                    logger.warning(f"NSFW/Altersgate-Sperre erkannt für virtuelle '{username}': {nsfw_error}. Re-enqueuing mit NSFW Account-Zwang...")
                    request.requires_nsfw_account = True
                    request.status = "Wartend"
                    request.account_username = None
                    
                    if not request.future.done():
                        # Sofort zurück in die Queue mit Priorität 0 (höchste)
                        await self.queue.put((0, time.time(), request))
                    return
                    
                except Exception as scrape_error:
                    logger.warning(f"Fehler beim Scraping mit virtueller '{username}': {scrape_error}")
                    
                    if request.attempts < 4:
                        # Nach 3 Fehlversuchen auf echten Reddit-Account eskalieren
                        if request.attempts >= 3:
                            logger.info(f"Request {request.id} ist bereits {request.attempts} mal anonym fehlgeschlagen. Eskaliere auf echten Reddit-Account als finalen Fallback...")
                            request.requires_nsfw_account = True
                            
                        wait_time = 1 if request.attempts == 1 else (2 if request.attempts == 2 else 3)
                        logger.info(f"Versuch {request.attempts} fehlgeschlagen. Re-enqueuing in {wait_time}s...")
                        request.status = "Wartend"
                        request.account_username = None
                        
                        async def delayed_requeue(req, delay):
                            await asyncio.sleep(delay)
                            if self._running:
                                if not req.future.done():
                                    await self.queue.put((current_priority, time.time(), req))
                        asyncio.create_task(delayed_requeue(request, wait_time))
                    else:
                        raise Exception(f"Fehlgeschlagen nach {request.attempts} Versuchen. Letzter Fehler: {scrape_error}")
            
            else:
                # ==========================================
                # KLASSISCHE ABARBEITUNG MIT REDDIT-ACCOUNT
                # ==========================================
                db = SessionLocal()
                account = db.query(RedditAccount).filter(RedditAccount.id == account_id).first()
                if not account or not account.is_active:
                    logger.warning(f"Gewählter Account ID {account_id} ist nicht mehr aktiv oder vorhanden.")
                    if not request.future.done():
                        await self.queue.put((current_priority, time.time(), request))
                    return

                request.attempts += 1
                request.status = "Cooldown"
                
                await self._enforce_cooldown(account)

                if request.future.done():
                    logger.info(f"Request {request.id} wurde während des Account-Cooldowns abgebrochen. Stoppe Scraping.")
                    return

                session_state = account.session_state
                proxy_url = account.proxy_url

                logger.info(f"Verarbeite Request '{request.action}' (Versuch {request.attempts}) mit Account '{account.username}'")

                try:
                    request.status = "Scraping"
                    data, method_used, new_session = await self._execute_scrape(request.action, request.params, session_state, proxy_url)
                    
                    if request.future.done():
                        logger.info(f"Request {request.id} wurde während des Scrapings abgebrochen. Verwerfe Ergebnis.")
                        return

                    account.failure_count = 0
                    if new_session:
                        account.session_state = new_session
                    if not request.is_playground:
                        account.request_count = (account.request_count or 0) + 1
                    self._record_request(account.id)
                    db.commit()
                    
                    if not request.future.done():
                        request.future.set_result((data, method_used, account.username))
                        
                except NSFWRequiredException as nsfw_error:
                    logger.warning(f"NSFW/Altersgate-Sperre erkannt für Account '{account.username}': {nsfw_error}. Re-enqueuing mit NSFW Account-Zwang...")
                    request.requires_nsfw_account = True
                    request.status = "Wartend"
                    request.account_username = None
                    if not request.future.done():
                        await self.queue.put((0, time.time(), request))
                    return

                except Exception as scrape_error:
                    logger.warning(f"Fehler beim Scraping mit Haupt-Proxy für Account '{account.username}': {scrape_error}")
                    is_temp = is_temporary_network_issue(scrape_error)
                    fallback_success = False
                    
                    if account.fallback_proxy_url:
                        if request.future.done():
                            logger.info(f"Request {request.id} wurde vor Fallback-Versuch abgebrochen. Stoppe.")
                            return

                        logger.info(f"Probiere Fallback-Proxy für Account '{account.username}'...")
                        try:
                            request.status = "Scraping"
                            data, method_used, new_session = await self._execute_scrape(request.action, request.params, session_state, account.fallback_proxy_url)
                            
                            if request.future.done():
                                logger.info(f"Request {request.id} wurde während des Fallback-Scrapings abgebrochen. Verwerfe Ergebnis.")
                                return

                            account.failure_count = 0
                            if new_session:
                                account.session_state = new_session
                            if not request.is_playground:
                                account.request_count = (account.request_count or 0) + 1
                            self._record_request(account.id)
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
                            scrape_error = fallback_error
                            is_temp = is_temporary_network_issue(fallback_error)
                    
                    if not fallback_success:
                        if is_temp:
                            logger.warning(f"Temporäres Problem bei '{account.username}'. Keine Deaktivierung.")
                        else:
                            account.failure_count = (account.failure_count or 0) + 1
                            if account.failure_count >= 3:
                                account.is_active = False
                                logger.error(f"Account '{account.username}' wurde nach {account.failure_count} kritischen Fehlern DEAKTIVIERT.")
                            db.commit()
                        
                        if request.future.done():
                            logger.info(f"Request {request.id} wurde vor Re-enqueuing abgebrochen. Keine Wiederholung.")
                            return

                        request.failed_account_ids.add(account.id)
                        if request.attempts < 4:
                            wait_time = 1 if request.attempts == 1 else (2 if request.attempts == 2 else 3)
                            logger.info(f"Versuch {request.attempts} fehlgeschlagen. Re-enqueuing in {wait_time}s mit Priorität 0...")
                            request.status = "Wartend"
                            request.account_username = None
                            
                            async def delayed_requeue(req, delay):
                                await asyncio.sleep(delay)
                                if self._running:
                                    if not req.future.done():
                                        await self.queue.put((0, time.time(), req))
                            asyncio.create_task(delayed_requeue(request, wait_time))
                        else:
                            raise Exception(f"Fehlgeschlagen nach {request.attempts} Versuchen. Letzter Fehler: {scrape_error}")

        except Exception as final_exception:
            logger.error(f"Request endgültig fehlgeschlagen: {final_exception}")
            final_exception.reddit_username = request.last_tried_username
            if not request.future.done():
                request.future.set_exception(final_exception)
        finally:
            if session_slot is not None:
                self.busy_session_ids.discard(session_slot)
            elif account_id is not None:
                # Cooldown erst nach Abarbeitung der Anfrage starten!
                if db:
                    try:
                        account = db.query(RedditAccount).filter(RedditAccount.id == account_id).first()
                        if account:
                            account.last_used_at = datetime.utcnow()
                            db.commit()
                    except Exception as e:
                        logger.error(f"Fehler beim Aktualisieren von last_used_at im finally-Block: {e}")
                self.busy_account_ids.discard(account_id)
            self.queue.task_done()
            if db:
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

    def _record_request(self, account_id: int):
        """Erfasst den Zeitstempel einer erfolgreichen Anfrage für einen bestimmten Account."""
        now = time.time()
        if account_id not in self.account_request_timestamps:
            self.account_request_timestamps[account_id] = []
        self.account_request_timestamps[account_id].append(now)
        self.account_last_request_time[account_id] = now

    def _get_adaptive_cooldown(self, account_id: int) -> float:
        """Berechnet den adaptiven Cooldown basierend auf dem rollierenden 60s-Fenster für einen bestimmten Account."""
        now = time.time()
        
        if account_id not in self.account_request_timestamps:
            self.account_request_timestamps[account_id] = []
            
        # Rolling Window bereinigen: nur Timestamps der letzten 60s behalten
        self.account_request_timestamps[account_id] = [ts for ts in self.account_request_timestamps[account_id] if now - ts < 60]
        requests_in_window = len(self.account_request_timestamps[account_id])
        
        # Herunterskalierung: Wenn lange keine Anfrage kam, Stufe senken
        last_time = self.account_last_request_time.get(account_id, 0.0)
        current_tier = self.account_adaptive_tier.get(account_id, 0)
        
        if last_time > 0:
            idle_time = now - last_time
            if idle_time > 60:
                # Längere Stille → direkt auf Sprint
                current_tier = 0
            elif idle_time > 30 and current_tier > 0:
                # Moderate Stille → eine Stufe runter
                current_tier = max(0, current_tier - 1)
        
        # Hochskalierung: Basierend auf Anfragenzahl im Fenster
        new_tier = 0
        for i, tier in enumerate(ADAPTIVE_TIERS):
            if requests_in_window < tier["threshold"]:
                new_tier = i
                break
        else:
            new_tier = len(ADAPTIVE_TIERS) - 1
        
        # Stufe darf nur steigen (nicht fallen) basierend auf Anfragenzahl
        if new_tier > current_tier:
            current_tier = new_tier
        
        self.account_adaptive_tier[account_id] = current_tier
        tier_info = ADAPTIVE_TIERS[current_tier]
        return float(tier_info["cooldown"])

    async def _enforce_cooldown(self, account: RedditAccount):
        """Erzwingt das Cooldown-Limit für das gewählte Konto."""
        # Effektiven Cooldown bestimmen (je nach Modus)
        if self.cooldown_mode == "auto":
            effective_cooldown = self._get_adaptive_cooldown(account.id)
        else:
            effective_cooldown = self.cooldown_seconds
        
        if account.last_used_at:
            elapsed = (datetime.utcnow() - account.last_used_at).total_seconds()
            wait_time = max(0.0, effective_cooldown - elapsed)
            if wait_time > 0:
                tier_label = ""
                if self.cooldown_mode == "auto":
                    tier_index = self.account_adaptive_tier.get(account.id, 0)
                    tier_label = f" [Stufe: {ADAPTIVE_TIERS[tier_index]['name']}]"
                logger.info(f"Cooldown für Account '{account.username}': Warte {wait_time:.2f}s{tier_label}...")
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
            
        current_load, max_24h_load = self.calculate_system_load()
        avg_wait = sum(self.wait_times) / len(self.wait_times) if self.wait_times else 0.0
        
        # Latest 50 logs und Accounts laden
        logs_json = []
        accounts_list = []
        total_requests = 0
        success_requests = 0
        client_error_requests = 0
        api_error_requests = 0
        success_rate = 100.0
        avg_duration_ms = 0
        
        try:
            with SessionLocal() as db:
                logs = db.query(APIRequestLog).order_by(APIRequestLog.timestamp.desc()).limit(50).all()
                for log in logs:
                    logs_json.append({
                        "timestamp": log.timestamp.strftime('%d.%m.%Y %H:%M:%S'),
                        "endpoint": log.endpoint,
                        "target": log.target,
                        "method_used": log.method_used,
                        "reddit_username": log.reddit_username or "-",
                        "response_time_ms": log.response_time_ms,
                        "status_code": log.status_code,
                        "error_message": log.error_message or ""
                    })
                    
                # Accounts laden (alphabetisch sortiert für stabile UI)
                db_accounts = db.query(RedditAccount).order_by(RedditAccount.username).all()
                now_dt = datetime.utcnow()
                for acc in db_accounts:
                    if self.cooldown_mode == "auto":
                        cooldown = self._get_adaptive_cooldown(acc.id)
                        tier_index = self.account_adaptive_tier.get(acc.id, 0)
                        tier_name = ADAPTIVE_TIERS[tier_index]["name"]
                        cooldown_display = f"{tier_name} ({cooldown:.1f}s)"
                    else:
                        cooldown = self.cooldown_seconds
                        tier_index = 0
                        cooldown_display = f"Manuell ({cooldown:.1f}s)"
                    
                    status = "IDLE"
                    remaining_seconds = 0
                    
                    if not acc.is_active:
                        status = "DEAKTIVIERT"
                    elif acc.id in self.busy_account_ids:
                        status = "WORKING"
                    elif acc.last_used_at:
                        elapsed = (now_dt - acc.last_used_at).total_seconds()
                        if elapsed < cooldown:
                            status = "COOLDOWN"
                            remaining_seconds = int(cooldown - elapsed)
                    
                    last_used_seconds_ago = None
                    if acc.last_used_at:
                        last_used_seconds_ago = int((now_dt - acc.last_used_at).total_seconds())
                    
                    accounts_list.append({
                        "id": acc.id,
                        "username": acc.username,
                        "cooldown": cooldown,
                        "cooldown_display": cooldown_display,
                        "status": status,
                        "remaining_seconds": remaining_seconds,
                        "last_used_seconds_ago": last_used_seconds_ago,
                        "is_active": acc.is_active,
                        "tier_index": tier_index
                    })
                    
                total_requests = db.query(APIRequestLog).count()
                success_requests = db.query(APIRequestLog).filter(APIRequestLog.status_code == 200).count()
                client_error_requests = db.query(APIRequestLog).filter(APIRequestLog.status_code.between(400, 498)).count()
                timeout_requests = db.query(APIRequestLog).filter(APIRequestLog.status_code == 499).count()
                api_error_requests = db.query(APIRequestLog).filter(APIRequestLog.status_code >= 500).count()
                
                success_rate = (success_requests / (success_requests + api_error_requests + timeout_requests) * 100) if (success_requests + api_error_requests + timeout_requests) > 0 else 100.0
                
                avg_duration = db.query(APIRequestLog).filter(APIRequestLog.status_code == 200)
                if success_requests > 0:
                    durations = [log.response_time_ms for log in avg_duration.all()]
                    avg_duration_ms = int(sum(durations) / len(durations))
        except Exception as e:
            logger.error(f"Fehler beim Laden der API-Statistiken für Queue-API: {e}")
            
        # Max-Cooldown, Max-Tier und Max-Requests über alle aktiven Accounts ermitteln
        max_cooldown = 0.0
        max_tier_index = 0
        max_requests_in_window = 0
        
        now_ts = time.time()
        for acc in accounts_list:
            if not acc["is_active"]:
                continue
            
            # Clean rolling window für diesen Account
            if acc["id"] in self.account_request_timestamps:
                self.account_request_timestamps[acc["id"]] = [ts for ts in self.account_request_timestamps[acc["id"]] if now_ts - ts < 60]
            
            req_count = len(self.account_request_timestamps.get(acc["id"], []))
            
            if acc["cooldown"] > max_cooldown:
                max_cooldown = acc["cooldown"]
                max_tier_index = acc["tier_index"]
            if req_count > max_requests_in_window:
                max_requests_in_window = req_count
            
        tier_info = ADAPTIVE_TIERS[max_tier_index] if self.cooldown_mode == "auto" else None
        
        return {
            "stats": {
                "total": len(items),
                "pending": pending_count,
                "active": active_count,
                "cooldown_seconds": self.cooldown_seconds,
                "cooldown_mode": self.cooldown_mode,
                "effective_cooldown": round(max_cooldown, 1),
                "adaptive_tier_name": tier_info["name"] if tier_info else None,
                "adaptive_tier_index": max_tier_index,
                "requests_in_window": max_requests_in_window,
                "current_load": round(current_load, 1),
                "max_24h_load": round(max_24h_load, 1),
                "avg_wait_seconds": round(avg_wait, 1),
                # Global stats
                "global_total": total_requests,
                "global_success_rate": f"{success_rate:.1f}%",
                "global_api_errors": api_error_requests,
                "global_client_errors": client_error_requests,
                "global_timeouts": timeout_requests,
                "global_avg_duration_ms": avg_duration_ms
            },
            "requests": items,
            "logs": logs_json,
            "accounts": accounts_list,
            "sparkline": list(self.sparkline_history)
        }

    def calculate_system_load(self) -> tuple[float, float]:
        """Berechnet die aktuelle Auslastung (in %) und gibt (current_load, max_24h_load) zurück."""
        try:
            with SessionLocal() as db:
                active_accounts = db.query(RedditAccount).filter(RedditAccount.is_active == True).all()
        except Exception as e:
            logger.error(f"Fehler beim Laden aktiver Accounts für Load-Berechnung: {e}")
            return 0.0, self.get_max_24h_load()
            
        n_active = len(active_accounts)
        if n_active == 0:
            return 0.0, self.get_max_24h_load()
            
        w_working = 0
        now = datetime.utcnow()
        
        for acc in active_accounts:
            is_busy = acc.id in self.busy_account_ids
            in_cooldown = False
            if acc.last_used_at:
                elapsed = (now - acc.last_used_at).total_seconds()
                cooldown = self._get_adaptive_cooldown(acc.id) if self.cooldown_mode == "auto" else self.cooldown_seconds
                if elapsed < cooldown:
                    in_cooldown = True
            if is_busy or in_cooldown:
                w_working += 1
                
        # Queue-Länge (wartende Requests)
        q_len = 0
        for r in self.active_requests.values():
            if r.status == "Wartend":
                q_len += 1
                
        current_load = ((w_working + q_len) / n_active) * 100.0
        
        # Max 24h Load aktualisieren und holen
        self.update_load_history(current_load)
        max_24h = self.get_max_24h_load()
        
        return current_load, max_24h

    def update_load_history(self, current_load: float):
        now = time.time()
        self.load_history.append((now, current_load))
        # Bereinigen: älter als 24h entfernen
        cutoff = now - 24 * 3600
        self.load_history = [x for x in self.load_history if x[0] > cutoff]

    def get_max_24h_load(self) -> float:
        if not self.load_history:
            return 0.0
        return max(x[1] for x in self.load_history)

    async def _load_monitor_loop(self):
        logger.info("Load-Monitor-Loop gestartet.")
        while self._running:
            try:
                current_load, _ = self.calculate_system_load()
                # Für Sparkline: Letzten 60 Samples behalten (z.B. die letzten 5 Minuten bei 5s Intervall)
                self.sparkline_history.append(round(current_load, 1))
                if len(self.sparkline_history) > 60:
                    self.sparkline_history.pop(0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fehler im Load-Monitor-Loop: {e}")
            await asyncio.sleep(5)

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
