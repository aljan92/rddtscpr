import asyncio
import logging
import time
import re
from fastapi import APIRouter, Request, Query, HTTPException, Depends
from sqlalchemy.orm import Session
from app.database import get_db, APIRequestLog
from app.reddit_scraper.scraper import clean_url, build_subreddit_url
from app.reddit_scraper.queue_manager import scrape_queue

logger = logging.getLogger("rddtscpr.reddit_router")

router = APIRouter()

@router.get("/v1/subreddit-posts")
async def api_subreddit_posts(
    request: Request,
    target: str = Query(..., description="The subreddit name (e.g. 'beziehungen') or full URL", example="beziehungen"),
    sort: str = Query("hot", description="Sort order: hot, new, top, rising", example="hot"),
    timeframe: str = Query("day", description="Timeframe for top sort: hour, day, week, month, year, all", example="day"),
    limit: int = Query(5, description="Number of posts to return (1-100)", example=5),
    include_nsfw: bool = Query(True, description="Whether to include NSFW posts", example=True),
    filter_pinned: bool = Query(True, description="Whether to filter out pinned/stickied posts", example=True),
    db: Session = Depends(get_db)
):
    """
    Extrahiert Posts aus einem bestimmten Subreddit.
    """
    # Circular import prevention: import check functions here
    from app.utils import check_rapidapi_access, is_admin_request

    check_rapidapi_access(request)

    # Sortierung aus URL extrahieren und überschreiben, falls vorhanden (z.B. r/NudeGermans/rising -> rising)
    if "reddit.com" in target or "r/" in target:
        match = re.search(r"r/[^/?#]+/([a-zA-Z]+)", target)
        if match:
            url_sort = match.group(1)
            if url_sort in ["hot", "new", "top", "rising"]:
                sort = url_sort
                logger.info(f"Sortierung aus URL extrahiert und überschrieben: {sort}")

    if sort not in ["hot", "new", "top", "rising"]:
        raise HTTPException(status_code=400, detail="Invalid 'sort' value. Allowed: hot, new, top, rising")
    if timeframe not in ["hour", "day", "week", "month", "year", "all"]:
        raise HTTPException(status_code=400, detail="Invalid 'timeframe' value. Allowed: hour, day, week, month, year, all")
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="Limit must be between 1 and 100.")

    is_playground = request.query_params.get("playground") == "true" and is_admin_request(request)
    start_time = time.time()
    method_used = "json"
    proxy_used = "Dynamisch"
    
    try:
        posts, method_used, username_used = await scrape_queue.enqueue(
            action="subreddit",
            params={
                "target": target,
                "sort": sort,
                "timeframe": timeframe,
                "limit": limit,
                "include_nsfw": include_nsfw,
                "filter_pinned": filter_pinned
            },
            is_playground=is_playground
        )
        
        duration = int((time.time() - start_time) * 1000)
        
        # In DB loggen
        log_entry = APIRequestLog(
            endpoint="/v1/subreddit-posts",
            target=target,
            status_code=200,
            response_time_ms=duration,
            method_used=method_used,
            proxy_used=proxy_used,
            reddit_username=username_used
        )
        db.add(log_entry)
        db.commit()
        
        scraped_url = build_subreddit_url(target, sort, timeframe)
        if sort == "top" and timeframe:
            scraped_url = f"{scraped_url}?t={timeframe}"
            
        return {
            "meta": {
                "target_subreddit": target,
                "scraped_url": scraped_url,
                "post_count": len(posts),
                "method_used": method_used,
                "execution_time_ms": duration
            },
            "data": posts
        }
    except asyncio.TimeoutError as e:
        duration = int((time.time() - start_time) * 1000)
        username_used = getattr(e, "reddit_username", None) or "-"
        logger.warning(f"Queue-Timeout bei Subreddit-Scraping ({target}) nach {duration}ms (Account: {username_used})")
        log_entry = APIRequestLog(
            endpoint="/v1/subreddit-posts",
            target=target,
            status_code=499,
            response_time_ms=duration,
            method_used=method_used,
            proxy_used=proxy_used,
            reddit_username=username_used,
            error_message="Queue-Timeout nach 90 Sekunden."
        )
        db.add(log_entry)
        db.commit()
        raise HTTPException(
            status_code=504,
            detail={"error": "Queue timeout", "message": "Request could not be processed within 90 seconds."}
        )
    except asyncio.CancelledError as e:
        duration = int((time.time() - start_time) * 1000)
        username_used = getattr(e, "reddit_username", None) or "-"
        logger.warning(f"Request abgebrochen/Timeout bei Subreddit-Scraping ({target}) nach {duration}ms (Account: {username_used})")
        log_entry = APIRequestLog(
            endpoint="/v1/subreddit-posts",
            target=target,
            status_code=499,
            response_time_ms=duration,
            method_used=method_used,
            proxy_used=proxy_used,
            reddit_username=username_used,
            error_message="Request wurde vom Client abgebrochen oder lief in ein Timeout."
        )
        db.add(log_entry)
        db.commit()
        raise
    except ValueError as ve:
        duration = int((time.time() - start_time) * 1000)
        error_msg = str(ve)
        logger.warning(f"Client-Fehler bei Subreddit-Scraping ({target}): {error_msg}")
        
        log_entry = APIRequestLog(
            endpoint="/v1/subreddit-posts",
            target=target,
            status_code=404,
            response_time_ms=duration,
            method_used=method_used,
            proxy_used=proxy_used,
            reddit_username="-",
            error_message=error_msg
        )
        db.add(log_entry)
        db.commit()
        
        raise HTTPException(
            status_code=404,
            detail={"error": "Client error", "message": error_msg}
        )
    except Exception as e:
        duration = int((time.time() - start_time) * 1000)
        error_msg = str(e)
        username_used = getattr(e, "reddit_username", None) or "-"
        logger.error(f"Fehler bei Subreddit-Scraping ({target}) mit Account {username_used}: {error_msg}")
        
        log_entry = APIRequestLog(
            endpoint="/v1/subreddit-posts",
            target=target,
            status_code=500,
            response_time_ms=duration,
            method_used=method_used,
            proxy_used=proxy_used,
            reddit_username=username_used,
            error_message=error_msg
        )
        db.add(log_entry)
        db.commit()
        
        raise HTTPException(
            status_code=500,
            detail={"error": "Scraping error", "message": error_msg}
        )

@router.get("/v1/post-comments")
async def api_post_comments(
    request: Request,
    post_url: str = Query(..., description="The full URL of the Reddit post", example="https://www.reddit.com/r/beziehungen/comments/1u8hnzu/bida_wenn_ich_meiner_partnerin_und_ihren_kindern/"),
    sort: str = Query("confidence", description="Sort order: confidence, top, new, controversial, old, qa", example="confidence"),
    limit: int = Query(10, description="Number of root comments to return (1-100)", example=10),
    include_replies: bool = Query(False, description="Whether to include comment replies recursively", example=False),
    load_more: bool = Query(False, description="Whether to fetch deeper replies (Pro/Ultra plans)", example=False),
    filter_bots: bool = Query(True, description="Whether to filter out automated bot comments", example=True),
    db: Session = Depends(get_db)
):
    """
    Extrahiert Kommentare aus einem bestimmten Reddit-Post.
    """
    from app.utils import check_rapidapi_access, load_settings, is_admin_request

    check_rapidapi_access(request)

    if load_more:
        settings = load_settings()
        subscription = request.headers.get("x-rapidapi-subscription")
        if settings.get("sandbox_mode", True) and not subscription:
            subscription = request.headers.get("x-sandbox-subscription")
            
        sub_str = (subscription or "").lower()
        is_premium = ("pro" in sub_str) or ("ultra" in sub_str) or ("mega" in sub_str)
        
        if not is_premium:
            raise HTTPException(
                status_code=403,
                detail="The 'load_more' feature is restricted to Pro, Mega, and Ultra plans. Please upgrade your subscription."
            )

    if sort not in ["confidence", "top", "new", "controversial", "old", "qa"]:
        raise HTTPException(status_code=400, detail="Invalid 'sort' value. Allowed: confidence, top, new, controversial, old, qa")
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="Limit must be between 1 and 100.")
    if not post_url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid post URL. URL must start with http/https.")

    is_playground = request.query_params.get("playground") == "true" and is_admin_request(request)
    start_time = time.time()
    method_used = "json"
    proxy_used = "Dynamisch"
    
    try:
        comments, method_used, username_used = await scrape_queue.enqueue(
            action="comments",
            params={
                "post_url": post_url,
                "sort": sort,
                "limit": limit,
                "include_replies": include_replies,
                "load_more": load_more,
                "filter_bots": filter_bots
            },
            is_playground=is_playground
        )
        
        duration = int((time.time() - start_time) * 1000)
        
        log_entry = APIRequestLog(
            endpoint="/v1/post-comments",
            target=post_url,
            status_code=200,
            response_time_ms=duration,
            method_used=method_used,
            proxy_used=proxy_used,
            reddit_username=username_used
        )
        db.add(log_entry)
        db.commit()
        
        return {
            "meta": {
                "scraped_url": f"{clean_url(post_url)}?sort={sort}",
                "comment_count": len(comments),
                "include_replies": include_replies,
                "load_more": load_more,
                "method_used": method_used,
                "execution_time_ms": duration
            },
            "data": comments
        }
    except asyncio.TimeoutError as e:
        duration = int((time.time() - start_time) * 1000)
        username_used = getattr(e, "reddit_username", None) or "-"
        logger.warning(f"Queue-Timeout bei Kommentar-Scraping ({post_url}) nach {duration}ms (Account: {username_used})")
        log_entry = APIRequestLog(
            endpoint="/v1/post-comments",
            target=post_url,
            status_code=499,
            response_time_ms=duration,
            method_used=method_used,
            proxy_used=proxy_used,
            reddit_username=username_used,
            error_message="Queue-Timeout nach 90 Sekunden."
        )
        db.add(log_entry)
        db.commit()
        raise HTTPException(
            status_code=504,
            detail={"error": "Queue timeout", "message": "Request could not be processed within 90 seconds."}
        )
    except asyncio.CancelledError as e:
        duration = int((time.time() - start_time) * 1000)
        username_used = getattr(e, "reddit_username", None) or "-"
        logger.warning(f"Request abgebrochen/Timeout bei Kommentar-Scraping ({post_url}) nach {duration}ms (Account: {username_used})")
        log_entry = APIRequestLog(
            endpoint="/v1/post-comments",
            target=post_url,
            status_code=499,
            response_time_ms=duration,
            method_used=method_used,
            proxy_used=proxy_used,
            reddit_username=username_used,
            error_message="Request wurde vom Client abgebrochen oder lief in ein Timeout."
        )
        db.add(log_entry)
        db.commit()
        raise
    except ValueError as ve:
        duration = int((time.time() - start_time) * 1000)
        error_msg = str(ve)
        logger.warning(f"Client-Fehler bei Kommentar-Scraping: {error_msg}")
        
        log_entry = APIRequestLog(
            endpoint="/v1/post-comments",
            target=post_url,
            status_code=404,
            response_time_ms=duration,
            method_used=method_used,
            proxy_used=proxy_used,
            reddit_username="-",
            error_message=error_msg
        )
        db.add(log_entry)
        db.commit()
        
        raise HTTPException(
            status_code=404,
            detail={"error": "Client error", "message": error_msg}
        )
    except Exception as e:
        duration = int((time.time() - start_time) * 1000)
        error_msg = str(e)
        username_used = getattr(e, "reddit_username", None) or "-"
        logger.error(f"Fehler bei Kommentar-Scraping mit Account {username_used}: {error_msg}")
        
        log_entry = APIRequestLog(
            endpoint="/v1/post-comments",
            target=post_url,
            status_code=500,
            response_time_ms=duration,
            method_used=method_used,
            proxy_used=proxy_used,
            reddit_username=username_used,
            error_message=error_msg
        )
        db.add(log_entry)
        db.commit()
        
        raise HTTPException(
            status_code=500,
            detail={"error": "Scraping error", "message": error_msg}
        )
