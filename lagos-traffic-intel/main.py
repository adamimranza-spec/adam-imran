"""
Pipeline orchestrator. Run directly for manual/one-off execution:
  python main.py              # real run, posts to the live Telegram channel
  python main.py --dry-run    # same pipeline, skips the Telegram send (use this for testing)

Called by APScheduler in server.py at 6:30 AM, 4:00 PM, and 8:00 PM WAT daily.
"""

import asyncio
import logging
import os
from datetime import datetime

import pytz

from config import LOG_DIR
from flood import assess_flood_zones
from narrator import generate_narrative
from scorer import full_score
from signals import (
    corridor_reports_to_text,
    extract_signals,
    parse_corridor_reports,
    signals_to_text,
)
from storage import append_history, filter_new_posts, write_today
from telegram import send_alert
from weather import fetch_weather
from x_poll import fetch_x_posts

os.makedirs(LOG_DIR, exist_ok=True)

LAGOS_TZ = pytz.timezone("Africa/Lagos")


def _setup_logger() -> logging.Logger:
    now = datetime.now(LAGOS_TZ)
    log_file = os.path.join(LOG_DIR, f"pipeline_{now.strftime('%Y%m%d')}.log")
    logger = logging.getLogger("lti_pipeline")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(log_file)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)
        logger.addHandler(sh)
    return logger


async def run_pipeline(send_telegram: bool = True) -> dict | None:
    logger = _setup_logger()
    now    = datetime.now(LAGOS_TZ)
    logger.info("Pipeline starting — %s", now.isoformat())

    # ── 1. Collect all data concurrently ──────────────────────────────────────
    # News scraping (Pulse Nigeria, Vanguard) was dropped 2026-07-06: both were
    # unreliable (403-blocked / matching the wrong page elements). Trigify
    # (searches + real-time webhook) was dropped 2026-07-10: its saved
    # searches were capped at DAILY re-crawl frequency, so posts could sit
    # unseen for up to ~24h, and the real-time webhook workaround built to
    # compensate never once fired. Replaced with a direct TwitterAPI.io poll
    # of @lagostraffic961 and @followlastma (x_poll.py), fresh within minutes.
    weather_data, posts = await asyncio.gather(
        _safe(fetch_weather, logger, "weather"),
        _safe(fetch_x_posts, logger, "twitterapi_io"),
    )

    weather_data = weather_data or {
        "precip_sum_mm": 0.0, "precip_hours": 0, "weather_code": 0,
        "condition_label": "Unknown", "season": "dry", "season_label": "Unknown", "month": now.month,
    }
    posts = posts or []

    # Drop posts already used in a previous run (e.g. a 4pm accident report
    # that's still the newest thing in the feed at 5:45am the next morning)
    # so the same report doesn't get re-surfaced as if it just happened.
    posts, dup_posts = filter_new_posts(posts)

    logger.info(
        "Collected: weather=%s, posts=%d new (%d already seen, skipped)",
        weather_data.get("condition_label"), len(posts), len(dup_posts),
    )

    # ── 2. Extract signals ────────────────────────────────────────────────────
    signals = extract_signals([], posts)
    corridor_reports = parse_corridor_reports(posts)
    logger.info("Signals found: %d | corridor reports parsed: %d", len(signals), len(corridor_reports))

    # ── 3. Score ──────────────────────────────────────────────────────────────
    day_name = now.strftime("%A").lower()
    score_result = full_score(
        day              = day_name,
        hour             = now.hour,
        precip_hours     = weather_data["precip_hours"],
        precip_sum_mm    = weather_data["precip_sum_mm"],
        month            = weather_data["month"],
        signals          = signals,
        corridor_reports = corridor_reports,
    )
    logger.info(
        "Score: %d (%s) | base=%d weather_mod=%d signal_mod=%d corridor_mod=%d",
        score_result["final_score"], score_result["level"],
        score_result["base_score"], score_result["weather_modifier"],
        score_result["signal_modifier"], score_result["corridor_modifier"],
    )

    # ── 4. Flood zones ────────────────────────────────────────────────────────
    flood_zones = assess_flood_zones(
        precip_sum_mm = weather_data["precip_sum_mm"],
        precip_hours  = weather_data["precip_hours"],
        month         = weather_data["month"],
    )
    logger.info("Flood zones at risk: %d", len(flood_zones))

    # ── 5. Generate narrative ─────────────────────────────────────────────────
    date_label = now.strftime("%A, %B %-d")
    period     = "morning" if now.hour < 12 else "evening"
    narrator_ctx = {
        "day_of_week":      day_name,
        "date_label":       date_label,
        "period":           period,
        "run_time_label":   now.strftime("%-I:%M %p WAT"),
        "level":            score_result["level"],
        "score":            score_result["final_score"],
        "base_score":       score_result["base_score"],
        "weather_modifier": score_result["weather_modifier"],
        "signal_modifier":  score_result["signal_modifier"],
        "corridor_modifier": score_result["corridor_modifier"],
        "signal_count":     len([s for s in signals if not s.get("error")]),
        "precip_sum_mm":    weather_data["precip_sum_mm"],
        "precip_hours":     weather_data["precip_hours"],
        "condition_label":  weather_data["condition_label"],
        "season_label":     weather_data["season_label"],
        "flood_zones":      flood_zones,
        "signal_lines":     signals_to_text(signals),
        "corridor_reports_text": corridor_reports_to_text(corridor_reports),
        "corridor_reports": corridor_reports,
    }

    narrative, used_fallback = await _safe(
        lambda: generate_narrative(narrator_ctx), logger, "narrator"
    ) or (None, True)

    if narrative is None:
        from narrator import get_fallback_narrative
        narrative    = get_fallback_narrative(score_result["level"], period, corridor_reports)
        used_fallback = True

    logger.info("Narrative ready (fallback=%s): %.80s...", used_fallback, narrative)

    # ── 6. Build areas_to_watch ───────────────────────────────────────────────
    areas = _build_areas(score_result["level"], signals, flood_zones, corridor_reports)

    # ── 7. Assemble today.json ────────────────────────────────────────────────
    sources_attempted = ["open_meteo", "twitterapi_io"]
    sources_succeeded = []
    if weather_data.get("condition_label") != "Unknown":
        sources_succeeded.append("open_meteo")
    if any(not p.get("error") for p in posts):
        sources_succeeded.append("twitterapi_io")

    today = {
        "schema_version": "1.0",
        "generated_at":   now.isoformat(),
        "date_label":     date_label,
        "day_of_week":    day_name,
        "traffic": {
            "score":     score_result["final_score"],
            "level":     score_result["level"],
            "hex_color": score_result["hex_color"],
            "narrative": narrative,
        },
        "scoring_breakdown": {
            "base_score":        score_result["base_score"],
            "weather_modifier":  score_result["weather_modifier"],
            "signal_modifier":   score_result["signal_modifier"],
            "corridor_modifier": score_result["corridor_modifier"],
            "final_score":       score_result["final_score"],
            "capped_at_10":      score_result.get("capped_at_10", False),
            "capped_at_1":       score_result.get("capped_at_1", False),
        },
        "areas_to_watch": areas,
        "weather": {
            "precip_sum_mm":   weather_data["precip_sum_mm"],
            "precip_hours":    weather_data["precip_hours"],
            "weather_code":    weather_data.get("weather_code", 0),
            "condition_label": weather_data["condition_label"],
            "season":          weather_data["season"],
            "season_label":    weather_data["season_label"],
        },
        "flood_zones": flood_zones,
        "signals":     [s for s in signals if not s.get("error")][:10],
        "corridor_reports": corridor_reports[:14],
        "meta": {
            "pipeline_version":    "1.0",
            "sources_attempted":   sources_attempted,
            "sources_succeeded":   sources_succeeded,
            "narrator_used_fallback": used_fallback,
            "is_stale":            False,
            "last_successful_run": now.isoformat(),
        },
    }

    # ── 8. Persist ────────────────────────────────────────────────────────────
    write_today(today)
    append_history(today)
    logger.info("today.json written")

    # ── 9. Telegram ───────────────────────────────────────────────────────────
    if send_telegram:
        tg_ok = await _safe(lambda: send_alert(today), logger, "telegram") or False
        logger.info("Telegram delivery: %s", "OK" if tg_ok else "FAILED")
    else:
        logger.info("Telegram delivery: skipped (dry run)")

    logger.info("Pipeline complete")
    return today


def _build_areas(level: str, signals: list[dict], flood_zones: list[dict],
                  corridor_reports: list[dict] | None = None) -> list[str]:
    """
    Build areas_to_watch list, real reports first: confirmed incidents from
    today's corridor reports, then busy corridors from the same reports, then
    signal-derived routes, then flood-zone areas. Hardcoded per-level defaults
    are the last resort, used only when nothing real was actually observed
    this run — they describe what's *typical*, not what's happening now, and
    should not be presented as if they were.
    """
    areas: list[str] = []
    seen: set[str]   = set()

    def _add(name: str) -> None:
        if name not in seen and len(areas) < 5:
            areas.append(name)
            seen.add(name)

    corridor_reports = corridor_reports or []

    # Real incidents first
    for r in corridor_reports:
        if r.get("status") == "incident":
            _add(r["area"])
    # Then real busy corridors
    for r in corridor_reports:
        if r.get("status") == "busy":
            _add(r["area"])

    # From confirmed signals (news/keyword-search pattern matches)
    for s in signals:
        if s.get("severity") == "HIGH" and s.get("affected_route") and not s.get("error"):
            route = s["affected_route"]
            if "multiple" not in route:
                _add(route)

    # From flood zones (tier 1 first)
    for z in flood_zones:
        _add(z["name"].split("(")[0].strip())

    # Last resort: hardcoded typical corridors for this score level, only if
    # nothing real filled the list yet.
    if not areas:
        defaults = {
            "Gridlock": ["Third Mainland Bridge", "Lekki-Epe Expressway (Sangotedo-Ajah)", "Oshodi-Apapa Expressway", "Ikorodu Road"],
            "Severe":   ["Third Mainland Bridge", "Lekki-Epe Expressway", "Oshodi-Apapa Expressway"],
            "Heavy":    ["Third Mainland Bridge", "Lekki-Epe Expressway", "Ikorodu Road"],
            "Moderate": ["Third Mainland Bridge", "Lagos Island corridors"],
            "Light":    [],
        }
        for route in defaults.get(level, []):
            _add(route)

    return areas[:5]


async def _safe(coro_fn, logger: logging.Logger, name: str):
    try:
        result = coro_fn()
        if asyncio.iscoroutine(result):
            return await result
        return result
    except Exception as exc:
        logger.warning("%s failed: %s", name, exc)
        return None


if __name__ == "__main__":
    import sys
    dry_run = "--dry-run" in sys.argv or "--no-telegram" in sys.argv
    asyncio.run(run_pipeline(send_telegram=not dry_run))
