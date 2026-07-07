import os
import json
import logging
import time
import uuid
import asyncio
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
import httpx

from app.database import SessionLocal, WebScraperJob, WebScraperRequestLog, SystemSetting
from app.web_scraper.models import ScrapeRequest
from app.web_scraper.scraper import run_crawler_pipeline

logger = logging.getLogger("rddtscpr.web_queue")

class WebScrapeRequest:
    def __init__(self, url: str, request_params: ScrapeRequest, future: asyncio.Future, is_playground: bool = False):
        self.id = str(uuid.uuid4())
        self.url = url
        self.request_params = request_params
        self.future = future
        self.status = "Wartend"  # "Wartend", "Scraping", "Erfolgreich", "Fehlgeschlagen"
        self.created_at = datetime.utcnow()
        self.completed_at = None
        self.is_playground = is_playground
        self.task = None

    def __lt__(self, other):
        # Fallback für PriorityQueue (ältere Anfragen zuerst)
        return self.created_at < other.created_at

class WebScrapeQueueManager:
    def __init__(self):
        self.queue = asyncio.PriorityQueue()
        self.worker_tasks = []
        self.cleanup_task = None
        self.load_monitor_task = None
        self._running = False
        self.max_workers = 5  # Maximale parallele Worker
        self.active_requests = {}  # id -> WebScrapeRequest
        self.load_history = []  # [(timestamp, load_pct)] for stats
        self.sparkline_history = []  # last 60 load values
        self.wait_times = []  # delay in seconds for last 100 requests

    def start(self):
        if not self._running:
            self._running = True
            
            # Max workers aus Datenbank laden
            try:
                with SessionLocal() as db:
                    setting = db.query(SystemSetting).filter(SystemSetting.key == "web_scraper_max_workers").first()
                    if setting:
                        self.max_workers = int(setting.value)
                        logger.info(f"Web Scraper Max Workers aus DB geladen: {self.max_workers}")
                    else:
                        setting = SystemSetting(key="web_scraper_max_workers", value=str(self.max_workers))
                        db.add(setting)
                        db.commit()
                        logger.info(f"Standard-Max-Workers in DB angelegt: {self.max_workers}")
            except Exception as e:
                logger.error(f"Fehler beim Laden von web_scraper_max_workers aus DB: {e}")

            # Worker Pool starten
            self.worker_tasks = [asyncio.create_task(self._worker_loop(i)) for i in range(self.max_workers)]
            self.cleanup_task = asyncio.create_task(self._cleanup_loop())
            self.load_monitor_task = asyncio.create_task(self._load_monitor_loop())
            logger.info("WebScrapeQueueManager erfolgreich gestartet (inkl. Worker Pool & Cleanup Task).")

    async def stop(self):
        self._running = False
        for t in self.worker_tasks:
            t.cancel()
        if self.cleanup_task:
            self.cleanup_task.cancel()
        if self.load_monitor_task:
            self.load_monitor_task.cancel()
        logger.info("WebScrapeQueueManager beendet.")

    async def enqueue(self, url: str, request_params: ScrapeRequest, is_playground: bool = False) -> tuple[dict, str, bool]:
        """
        Reiht einen Web-Scraping-Request in die Queue ein.
        Bei direct-Mode wird auf das Ergebnis gewartet.
        """
        if not self._running:
            self.start()
            
        future = asyncio.get_running_loop().create_future()
        request = WebScrapeRequest(url, request_params, future, is_playground)
        self.active_requests[request.id] = request
        
        # In DB anlegen falls nicht Playground
        if not is_playground:
            try:
                with SessionLocal() as db:
                    job = WebScraperJob(
                        id=request.id,
                        url=url,
                        delivery_mode=request_params.delivery_mode,
                        status="Wartend"
                    )
                    db.add(job)
                    db.commit()
            except Exception as db_err:
                logger.error(f"Fehler beim Speichern des Jobs in der DB: {db_err}")

        # Priorität: Playground-Anfragen erhalten Prio 5, reguläre Prio 10
        prio = 5 if is_playground else 10
        await self.queue.put((prio, time.time(), request))
        
        # Wenn direct mode, warte auf Beendigung
        if request_params.delivery_mode == "direct":
            try:
                # Timeout von 90 Sekunden
                return await asyncio.wait_for(future, timeout=90.0)
            except asyncio.TimeoutError as e:
                future.cancel()
                if request.task and not request.task.done():
                    request.task.cancel()
                raise e
        else:
            # Bei Webhook / Both sofort die Job-ID zurückgeben
            return {"job_id": request.id, "status": "Wartend"}, "Queued", False

    async def _worker_loop(self, worker_id: int):
        logger.info(f"Web Scraper Queue Worker {worker_id} gestartet.")
        while self._running:
            try:
                prio, t_queued, request = await self.queue.get()
                request.status = "Scraping"
                
                # In DB aktualisieren
                if not request.is_playground:
                    try:
                        with SessionLocal() as db:
                            db_job = db.query(WebScraperJob).filter(WebScraperJob.id == request.id).first()
                            if db_job:
                                db_job.status = "Scraping"
                                db.commit()
                    except Exception as e:
                        logger.error(f"Fehler beim Aktualisieren des Job-Status in DB: {e}")

                wait_time = time.time() - t_queued
                self.wait_times.append(wait_time)
                if len(self.wait_times) > 100:
                    self.wait_times.pop(0)

                # Starte Scraping Pipeline
                # Speichere Task-Referenz für eventuellen Abbruch
                request.task = asyncio.create_task(self._process_scrape(request))
                await request.task
                
                self.queue.task_done()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Unerwarteter Fehler im Worker {worker_id}: {e}")
                await asyncio.sleep(2)

    async def _process_scrape(self, request: WebScrapeRequest):
        from app.utils import load_settings
        settings = load_settings()
        rotating_proxy = settings.get("rotating_proxy_url", "")
        
        start_time = time.time()
        status_code = 200
        error_msg = None
        result = None
        proxy_used = "Dynamisch"
        stealth_active = False
        
        try:
            # Starte Scraper
            result = await run_crawler_pipeline(request.request_params, rotating_proxy, request.id)
            status_code = result["meta"]["status"]
            proxy_used = result.pop("proxy_used", "Dynamisch")
            stealth_active = result.pop("stealth_active", False)
            request.status = "Erfolgreich"
            
            # Resolve future für direct mode
            if not request.future.done():
                request.future.set_result((result, proxy_used, stealth_active))
                
        except Exception as e:
            status_code = 500
            error_msg = str(e)
            request.status = "Fehlgeschlagen"
            logger.error(f"Fehler beim Scraping von {request.url}: {e}")
            
            if not request.future.done():
                request.future.set_exception(e)
                
        duration = int((time.time() - start_time) * 1000)
        request.completed_at = datetime.utcnow()
        
        # Aus active_requests entfernen
        self.active_requests.pop(request.id, None)

        # In DB speichern und Webhook feuern falls nicht Playground
        try:
            with SessionLocal() as db:
                # Job-Ergebnis nur für Nicht-Playground-Requests speichern
                if not request.is_playground:
                    db_job = db.query(WebScraperJob).filter(WebScraperJob.id == request.id).first()
                    if db_job:
                        db_job.status = request.status
                        db_job.completed_at = request.completed_at
                        if request.status == "Erfolgreich":
                            db_job.result = json.dumps(result)
                        else:
                            db_job.error_message = error_msg
                        db.commit()
                    
                # Request Log IMMER schreiben (auch für Playground)
                log_entry = WebScraperRequestLog(
                    url=request.url,
                    status_code=status_code,
                    response_time_ms=duration,
                    proxy_used=proxy_used,
                    stealth_mode_active=stealth_active,
                    error_message=error_msg
                )
                db.add(log_entry)
                db.commit()
        except Exception as db_err:
            logger.error(f"Fehler beim Schreiben des Job-Ergebnisses in DB: {db_err}")

        # Webhook POST ausführen (nur für Nicht-Playground-Requests)
        if not request.is_playground and request.request_params.delivery_mode in ["webhook", "both"] and request.request_params.webhook_url:
            webhook_payload = {
                "job_id": request.id,
                "url": request.url,
                "status": request.status,
                "completed_at": request.completed_at.isoformat() if request.completed_at else None,
                "error": error_msg,
                "data": result if request.status == "Erfolgreich" else None
            }
            # Im Hintergrund senden, um den Worker nicht zu blockieren
            asyncio.create_task(self._send_webhook(request.request_params.webhook_url, webhook_payload))

    async def _send_webhook(self, url: str, payload: dict):
        headers = {"Content-Type": "application/json"}
        # Weiterleiten von eventuell vom User gesetzten Custom Headers für Webhooks?
        # Zur Einfachheit schicken wir einen sauberen POST Request
        logger.info(f"Sende Webhook für Job {payload['job_id']} an {url}...")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(url, json=payload, headers=headers)
                if res.status_code == 200:
                    logger.info(f"Webhook für Job {payload['job_id']} erfolgreich zugestellt.")
                else:
                    logger.warning(f"Webhook-Zustellung fehlgeschlagen für Job {payload['job_id']}. Status Code: {res.status_code}")
        except Exception as e:
            logger.error(f"Fehler bei Webhook-Zustellung an {url}: {e}")

    async def _cleanup_loop(self):
        """
        Löscht stündlich Screenshots und DB-Jobs, die älter als 24 Stunden sind.
        """
        while self._running:
            try:
                await asyncio.sleep(3600)  # Alle 60 Minuten ausführen
                logger.info("Starte stündlichen Web-Scraper-Cleanup...")
                
                cutoff = datetime.utcnow() - timedelta(hours=24)
                
                with SessionLocal() as db:
                    # Finde alte Jobs
                    old_jobs = db.query(WebScraperJob).filter(WebScraperJob.created_at < cutoff).all()
                    job_ids_to_delete = [job.id for job in old_jobs]
                    
                    if job_ids_to_delete:
                        # 1. Screenshots löschen
                        screenshot_dir = "./app/data/screenshots"
                        for jid in job_ids_to_delete:
                            screenshot_path = f"{screenshot_dir}/{jid}.png"
                            if os.path.exists(screenshot_path):
                                try:
                                    os.remove(screenshot_path)
                                    logger.info(f"Screenshot für Job {jid} gelöscht.")
                                except Exception as es:
                                    logger.error(f"Fehler beim Löschen des Screenshots für Job {jid}: {es}")
                                    
                            # Auch eventuelle Subpage Screenshots löschen
                            # z.B. jid_sub_0.png
                            for sub_idx in range(20):
                                sub_path = f"{screenshot_dir}/{jid}_sub_{sub_idx}.png"
                                if os.path.exists(sub_path):
                                    try:
                                        os.remove(sub_path)
                                    except:
                                        pass
                                    
                        # 2. DB Einträge löschen
                        db.query(WebScraperJob).filter(WebScraperJob.created_at < cutoff).delete()
                        db.commit()
                        logger.info(f"{len(job_ids_to_delete)} abgelaufene Jobs aus DB gelöscht.")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fehler im Cleanup-Loop: {e}")

    async def _load_monitor_loop(self):
        """
        Berechnet minütlich die Systemlast der Web-Scraper-Queue.
        """
        while self._running:
            try:
                active_count = len([r for r in self.active_requests.values() if r.status == "Scraping"])
                load_pct = (active_count / self.max_workers * 100) if self.max_workers > 0 else 0
                
                self.load_history.append((datetime.utcnow(), load_pct))
                if len(self.load_history) > 1440:  # 24 Stunden Historie behalten
                    self.load_history.pop(0)
                    
                self.sparkline_history.append(load_pct)
                if len(self.sparkline_history) > 60:
                    self.sparkline_history.pop(0)
                    
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fehler im Load-Monitor: {e}")
                await asyncio.sleep(60)

    def calculate_system_load(self) -> tuple[float, float]:
        """
        Gibt die aktuelle Systemlast und die Spitzenlast der letzten 24 Stunden zurück.
        """
        active_count = len([r for r in self.active_requests.values() if r.status == "Scraping"])
        current_load = (active_count / self.max_workers * 100) if self.max_workers > 0 else 0
        
        max_24h_load = current_load
        if self.load_history:
            max_24h_load = max(load_pct for _, load_pct in self.load_history)
            
        return current_load, max_24h_load

    def get_queue_status(self) -> dict:
        """
        Gibt detaillierte Statistiken über die Queue zurück analog zur Reddit Queue.
        """
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
                "action": "Playground" if r.is_playground else "Scrape",
                "target": r.url,
                "status": r.status,
                "attempts": 1,
                "age_seconds": int((datetime.utcnow() - r.created_at).total_seconds())
            })
            
        current_load, max_24h_load = self.calculate_system_load()
        avg_wait = sum(self.wait_times) / len(self.wait_times) if self.wait_times else 0.0
        
        # Latest 50 logs laden
        logs_json = []
        total_requests = 0
        success_requests = 0
        client_error_requests = 0
        api_error_requests = 0
        timeout_requests = 0
        success_rate = 100.0
        avg_duration_ms = 0
        
        try:
            with SessionLocal() as db:
                logs = db.query(WebScraperRequestLog).order_by(WebScraperRequestLog.timestamp.desc()).limit(50).all()
                for log in logs:
                    logs_json.append({
                        "timestamp": log.timestamp.strftime('%d.%m.%Y %H:%M:%S') if log.timestamp else '-',
                        "endpoint": "/v1/web/scrape",
                        "target": log.url,
                        "method_used": "STEALTH" if log.stealth_mode_active else "NORMAL",
                        "response_time_ms": log.response_time_ms,
                        "status_code": log.status_code,
                        "error_message": log.error_message or ""
                    })
                    
                total_requests = db.query(WebScraperRequestLog).count()
                success_requests = db.query(WebScraperRequestLog).filter(WebScraperRequestLog.status_code == 200).count()
                client_error_requests = db.query(WebScraperRequestLog).filter(WebScraperRequestLog.status_code.between(400, 498)).count()
                timeout_requests = db.query(WebScraperRequestLog).filter(WebScraperRequestLog.status_code == 499).count()
                api_error_requests = db.query(WebScraperRequestLog).filter(WebScraperRequestLog.status_code >= 500).count()
                
                denom = success_requests + api_error_requests + timeout_requests
                success_rate = (success_requests / denom * 100) if denom > 0 else 100.0
                
                avg_duration = db.query(WebScraperRequestLog).filter(WebScraperRequestLog.status_code == 200)
                if success_requests > 0:
                    durations = [l.response_time_ms for l in avg_duration.all()]
                    avg_duration_ms = int(sum(durations) / len(durations)) if durations else 0
        except Exception as e:
            logger.error(f"Fehler beim Laden der API-Statistiken für Web-Queue: {e}")
            
        return {
            "stats": {
                "total": len(items),
                "pending": pending_count,
                "active": active_count,
                "current_load": round(current_load, 1),
                "max_24h_load": round(max_24h_load, 1),
                "avg_wait_seconds": round(avg_wait, 1),
                "global_total": total_requests,
                "global_success_rate": f"{success_rate:.1f}%",
                "global_api_errors": api_error_requests,
                "global_client_errors": client_error_requests,
                "global_timeouts": timeout_requests,
                "global_avg_duration_ms": avg_duration_ms,
                "max_workers": self.max_workers
            },
            "requests": items,
            "logs": logs_json,
            "sparkline": list(self.sparkline_history)
        }

    def resize_worker_pool(self, new_size: int):
        if new_size == self.max_workers:
            return
        logger.info(f"Passe Web Scraper Worker Pool Größe von {self.max_workers} auf {new_size} an...")
        
        # Worker-Tasks stoppen
        for t in self.worker_tasks:
            t.cancel()
        self.worker_tasks.clear()
        
        # Neue Worker-Tasks starten
        self.max_workers = new_size
        if self._running:
            self.worker_tasks = [asyncio.create_task(self._worker_loop(i)) for i in range(self.max_workers)]

# Globaler Queue Manager für Web Scraper
web_scrape_queue = WebScrapeQueueManager()

