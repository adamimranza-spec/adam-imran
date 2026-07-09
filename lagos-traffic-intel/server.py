"""
FastAPI web server. Entry point for Railway.
APScheduler runs the pipeline at 5:45 AM and 4:00 PM WAT daily inside the server process.
"""

import asyncio
import os
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import DELAY_MULTIPLIER, STATIC_DIR, TRIGIFY_WEBHOOK_SECRET
from main import run_pipeline
from storage import append_live_post, mark_stale, read_history, read_today
from telegram import get_subscriber_count

LAGOS_TZ = pytz.timezone("Africa/Lagos")


def _detect_deploy_version() -> str:
    """
    Short commit SHA for whatever code this process is actually running.
    Lets /health be checked after a push to confirm the new deploy is live
    without ever touching /run — that's what should have been used instead
    of polling the pipeline-triggering endpoint (see 2026-07-08 incident:
    polling /run?dry_run=true before the deploy landed silently ran the real
    pipeline against the live Telegram channel 17 times, since the old code
    didn't recognize that query param).
    """
    railway_sha = os.getenv("RAILWAY_GIT_COMMIT_SHA", "")
    if railway_sha:
        return railway_sha[:8]
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


DEPLOY_VERSION = _detect_deploy_version()

# Cached subscriber count — refreshed every 6 hours to avoid hitting Telegram on every request
_subscriber_cache: dict = {"total": None, "new_today": None, "fetched_at": None}


async def _refresh_subscribers():
    result = await get_subscriber_count()
    if result:
        _subscriber_cache["total"] = result["total"]
        _subscriber_cache["new_today"] = result["new_today"]
    _subscriber_cache["fetched_at"] = datetime.now(LAGOS_TZ).isoformat()


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = AsyncIOScheduler(timezone=LAGOS_TZ)
    scheduler.add_job(run_pipeline, "cron", hour=5, minute=45, id="morning_pipeline")
    scheduler.add_job(run_pipeline, "cron", hour=16, minute=0, id="evening_pipeline")
    scheduler.add_job(_refresh_subscribers, "interval", hours=6, id="subscriber_refresh")
    scheduler.start()

    # Warm the subscriber cache on startup
    asyncio.create_task(_refresh_subscribers())

    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Lagos Traffic Intel", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    today = read_today()
    return {
        "ok": True,
        "version": DEPLOY_VERSION,
        "last_run": today.get("meta", {}).get("last_successful_run") if today else None,
        "is_stale": mark_stale(today),
    }


@app.post("/run")
async def trigger_run(dry_run: bool = False):
    """
    Manually trigger the pipeline. Used for first-run and debugging.
    Pass ?dry_run=true to run the full pipeline (writes today.json as usual)
    without posting to the live Telegram channel.
    """
    asyncio.create_task(run_pipeline(send_telegram=not dry_run))
    return {"ok": True, "dry_run": dry_run, "message": "Pipeline triggered. Check /api/today in ~30 seconds."}


WEBHOOK_FIELD_SEP = "\x01"  # a control char, cannot appear in real tweet text


@app.post("/webhook/trigify-post")
async def trigify_webhook(request: Request):
    """
    Receives real-time posts from the Trigify workflow (New Post trigger on
    each saved search -> HTTP Request action). Exists because both searches
    are capped at Trigify's own DAILY re-crawl frequency (confirmed
    2026-07-09, there is no hourly tier), so polling GET /searches/{id}/results
    at pipeline run time alone can be up to a day stale; this captures each
    new matching post the moment Trigify's engine detects it.

    Body is NOT JSON: Trigify's `{{ !ref(...) }}` templating does raw string
    substitution with no JSON-escaping, so a real tweet's text (which almost
    always contains literal newlines, e.g. "TRAFFIC UPDATE FROM X\n\nBody...")
    breaks a hand-built JSON body the moment it's substituted in (confirmed
    2026-07-09: a static/no-templating body worked fine, but the same body
    with a real multi-line post failed with an opaque platform error).
    Instead the workflow sends postUrl, authorUrl, datePosted, source, then
    text (in that order) joined by WEBHOOK_FIELD_SEP, with text last and
    split off with maxsplit so its content, including any literal newlines,
    can't break the parsing.
    """
    if not TRIGIFY_WEBHOOK_SECRET or request.headers.get("X-Webhook-Secret") != TRIGIFY_WEBHOOK_SECRET:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    raw = (await request.body()).decode("utf-8", errors="replace")
    parts = raw.split(WEBHOOK_FIELD_SEP, 4)
    if len(parts) != 5:
        return JSONResponse({"error": "malformed body"}, status_code=400)

    post_url, author_url, date_posted, source, text = parts
    append_live_post({
        "text":      text[:600],
        "author":    author_url,
        "posted_at": date_posted,
        "url":       post_url,
    })
    return {"ok": True}


@app.get("/api/today")
async def api_today():
    today = read_today()
    if not today:
        return JSONResponse({"error": "No data yet. Run the pipeline first."}, status_code=503)
    today["meta"]["is_stale"] = mark_stale(today)
    return today


@app.get("/api/history")
async def api_history():
    return read_history()


@app.get("/api/subscribers")
async def api_subscribers():
    total     = _subscriber_cache.get("total")
    new_today = _subscriber_cache.get("new_today")
    return {
        "total": total,
        "new_today": new_today,
        "fetched_at": _subscriber_cache.get("fetched_at"),
        "display": f"{new_today:,} people subscribed today" if new_today is not None else "Join our community",
    }


@app.get("/api/embed")
async def api_embed():
    """Lightweight response for Shuttlers / ride-hailing integrations."""
    today = read_today()
    if not today:
        return JSONResponse({"error": "No data yet."}, status_code=503)

    traffic = today.get("traffic", {})
    level   = traffic.get("level", "Moderate")
    areas   = today.get("areas_to_watch", [])
    flood   = today.get("flood_zones", [])

    corridors = []
    for area in areas:
        corridors.append({
            "name":       area,
            "severity":   level.lower(),
            "flood_risk": any(
                z["name"].lower() in area.lower() or area.lower() in z["name"].lower()
                for z in flood
            ),
        })

    high_zones = [z["name"] for z in flood if z["confidence"] == "HIGH"]

    return {
        "provider":         "Lagos Traffic Intel",
        "updated_at":       today.get("generated_at"),
        "date_label":       today.get("date_label"),
        "status": {
            "level":    level,
            "hex_color": traffic.get("hex_color"),
            "summary":  traffic.get("narrative", "")[:200],
        },
        "affected_corridors":  corridors,
        "delay_multiplier":    DELAY_MULTIPLIER.get(level, 1.0),
        "flood_advisory": {
            "active":               len(flood) > 0,
            "zone_count":           len(flood),
            "high_confidence_zones": high_zones,
        },
        "subscribe": {
            "telegram":     "https://t.me/+BEpdDOqEm1IxYjI8",
            "channel_name": "Lagos Traffic Intel",
        },
        "is_stale": mark_stale(today),
    }


# ── Static files / SPA ────────────────────────────────────────────────────────

os.makedirs(STATIC_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    html_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return JSONResponse({"error": "Landing page not found."}, status_code=404)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
