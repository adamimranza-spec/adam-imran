import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

# ── API credentials ────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
TRIGIFY_API_KEY    = os.getenv("TRIGIFY_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8782744685:AAF5Rw4KeggrZCWsTRYJjDLdsHzooDpQ1ZU")
TELEGRAM_CHANNEL   = os.getenv("TELEGRAM_CHANNEL", "-1004333163645")

# Shared secret the Trigify workflow's HTTP Request action sends back so
# /webhook/trigify-post can reject calls that aren't actually from Trigify.
# No hardcoded fallback on purpose (unlike TELEGRAM_BOT_TOKEN above, which
# already shouldn't do that either): set TRIGIFY_WEBHOOK_SECRET in .env
# locally and as a Railway environment variable in production, never commit
# the actual value to source.
TRIGIFY_WEBHOOK_SECRET = os.getenv("TRIGIFY_WEBHOOK_SECRET", "")

# Direct poll of @lagostraffic961 and @followlastma via TwitterAPI.io — added
# 2026-07-10 after confirming Trigify's own DAILY re-crawl cap left both
# accounts up to ~24h stale (a real accident post sat unseen for hours; Adam
# spotted a fresh one on the live Twitter page that Trigify hadn't picked up
# at all). No hardcoded fallback, same convention as TRIGIFY_WEBHOOK_SECRET.
TWITTERAPI_IO_KEY = os.getenv("TWITTERAPI_IO_KEY", "")

# ── Trigify saved searches ─────────────────────────────────────────────────────
TRIGIFY_SEARCH_KEYWORDS     = "d1da38c4-36ab-4448-94a8-c694245893de"
TRIGIFY_SEARCH_LAGOSTRAFFIC = "e07ed569-1bed-4174-9864-8c4dc51043e1"

# ── Open-Meteo (Lagos coords) ──────────────────────────────────────────────────
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=6.5244&longitude=3.3792"
    "&hourly=precipitation,weathercode"
    "&timezone=Africa%2FLagos"
    "&past_hours=6"
    "&forecast_days=1"
)

# ── File paths ─────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
# On Railway, RAILWAY_VOLUME_MOUNT_PATH points at the persistent volume
# attached to this service (added 2026-07-09), so today.json/history.json/
# seen_posts.json/live_posts.json survive redeploys instead of resetting to
# empty every time (Railway's container filesystem itself is ephemeral).
# Locally, that env var isn't set, so this falls back to the same relative
# ./data path as before.
DATA_DIR    = os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.path.join(BASE_DIR, "data")
STATIC_DIR  = os.path.join(BASE_DIR, "static")
LOG_DIR     = os.path.join(BASE_DIR, "logs")
TODAY_JSON  = os.path.join(DATA_DIR, "today.json")
HISTORY_JSON = os.path.join(DATA_DIR, "history.json")
SEEN_POSTS_JSON = os.path.join(DATA_DIR, "seen_posts.json")

# Real-time posts pushed by the Trigify workflow (see /webhook/trigify-post
# in server.py) are kept here, separate from seen_posts.json above. Both
# saved searches are capped at Trigify's own DAILY re-crawl frequency
# (confirmed 2026-07-09, there is no hourly tier), so polling
# GET /searches/{id}/results at pipeline run time can be up to a day stale.
# The workflow instead forwards each new matching post the moment Trigify
# detects it; the pipeline merges this feed with the daily poll as a
# fallback/backstop.
LIVE_POSTS_JSON = os.path.join(DATA_DIR, "live_posts.json")
LIVE_POSTS_RETENTION_HOURS = 48
LIVE_POSTS_MAX = 300

# A post older than this is dropped before it can become a corridor report or
# signal, regardless of whether it's been "seen" before. Found live on
# 2026-07-09: the 5:45am run reported a tanker breakdown and flash flooding
# as current when the underlying posts were from the previous morning
# (~20h old), and a Mile 2 congestion claim sourced from a ~35h old post.
# Dedup alone doesn't catch this, it only stops reusing a post that's
# already been seen; a post that's simply old and never seen before still
# needs an explicit freshness check.
#
# 20h, not the ~10h15m gap between pipeline runs, because both Trigify
# searches are configured at `frequency: DAILY` (confirmed via `trigify
# search get`), which is the most frequent option the platform offers for a
# saved search's own re-crawl cycle, there is no hourly tier. So one of the
# two daily pipeline runs will structurally see data that's many hours old
# no matter what. 20h still excludes genuinely dead data (the 35h Mile 2
# report) without zeroing out every run just because the daily crawl hasn't
# landed yet. The real fix is switching to a Trigify workflow with a
# real-time "New Post" trigger instead of polling the search's own results,
# see conversation 2026-07-09 for the tradeoffs on that.
MAX_POST_AGE_HOURS = 20

# ── Scoring matrix: (day_of_week, hour_start, hour_end_exclusive) → base_score ─
# Covers all hours; morning pipeline uses the 6–9 slot.
BASE_SCORE_MATRIX: dict[tuple[str, int, int], int] = {
    # MONDAY — pent-up weekend movement, full working week begins
    ("monday",    0,  6): 2,
    ("monday",    6,  9): 8,
    ("monday",    9, 12): 5,
    ("monday",   12, 16): 4,
    ("monday",   16, 20): 7,
    ("monday",   20, 24): 3,
    # TUESDAY
    ("tuesday",   0,  6): 2,
    ("tuesday",   6,  9): 7,
    ("tuesday",   9, 12): 4,
    ("tuesday",  12, 16): 4,
    ("tuesday",  16, 20): 7,
    ("tuesday",  20, 24): 3,
    # WEDNESDAY
    ("wednesday", 0,  6): 2,
    ("wednesday", 6,  9): 7,
    ("wednesday", 9, 12): 4,
    ("wednesday",12, 16): 4,
    ("wednesday",16, 20): 7,
    ("wednesday",20, 24): 3,
    # THURSDAY — Thursday PM is Lagos's second-worst evening
    ("thursday",  0,  6): 2,
    ("thursday",  6,  9): 7,
    ("thursday",  9, 12): 4,
    ("thursday", 12, 16): 5,
    ("thursday", 16, 20): 8,
    ("thursday", 20, 24): 3,
    # FRIDAY — worst AM after Monday; Friday PM is the ceiling
    ("friday",    0,  6): 3,
    ("friday",    6,  9): 9,
    ("friday",    9, 12): 6,
    ("friday",   12, 16): 7,
    ("friday",   16, 20): 10,
    ("friday",   20, 24): 6,
    # SATURDAY — market/leisure traffic midday
    ("saturday",  0,  6): 2,
    ("saturday",  6,  9): 3,
    ("saturday",  9, 12): 5,
    ("saturday", 12, 16): 5,
    ("saturday", 16, 20): 4,
    ("saturday", 20, 24): 3,
    # SUNDAY — church traffic 9–12 only notable window
    ("sunday",    0,  6): 1,
    ("sunday",    6,  9): 2,
    ("sunday",    9, 12): 3,
    ("sunday",   12, 16): 3,
    ("sunday",   16, 20): 3,
    ("sunday",   20, 24): 2,
}

DEFAULT_BASE_SCORE = 3  # fallback for any unlisted slot

# ── Score → level ──────────────────────────────────────────────────────────────
SCORE_LEVELS: list[tuple[int, int, str, str]] = [
    (1,  3,  "Light",    "#16A34A"),
    (4,  5,  "Moderate", "#CA8A04"),
    (6,  7,  "Heavy",    "#EA580C"),
    (8,  9,  "Severe",   "#DC2626"),
    (10, 10, "Gridlock", "#7F1D1D"),
]

# ── Delay multipliers (for /api/embed — Shuttlers integration) ─────────────────
DELAY_MULTIPLIER: dict[str, float] = {
    "Light": 1.0, "Moderate": 1.3, "Heavy": 1.7, "Severe": 2.2, "Gridlock": 2.5,
}

# ── Flood zones (calibrated from news archives 2019–2026) ─────────────────────
# mm_threshold: daily precipitation (mm) that triggers this zone
# hours_threshold: active rain hours overnight that triggers this zone
# Either threshold met → zone at risk; both met OR extreme event → HIGH confidence
FLOOD_ZONES: list[dict] = [
    # Tier 1 — chronic traps, flood at low thresholds
    {
        "id": "oshodi_apapa_apakun",
        "name": "Oshodi-Apapa Expressway / Apakun Bridge underpass",
        "area": "Oshodi/Apapa",
        "tier": 1, "mm_threshold": 8, "hours_threshold": 2,
    },
    {
        "id": "marina_lagos_island",
        "name": "Marina / Lagos Island (Adeniji Adele, Idumagbo)",
        "area": "Lagos Island",
        "tier": 1, "mm_threshold": 10, "hours_threshold": 2,
    },
    {
        "id": "trade_fair_mile2",
        "name": "Trade Fair / Mile 2 / Apapa",
        "area": "Mile 2/Apapa",
        "tier": 1, "mm_threshold": 10, "hours_threshold": 3,
    },
    {
        "id": "iyana_oworo_tmb",
        "name": "Iyana-Oworo / Third Mainland Bridge approach",
        "area": "Iyana-Oworo",
        "tier": 1, "mm_threshold": 12, "hours_threshold": 3,
    },
    # Tier 2 — flood at moderate thresholds
    {
        "id": "lekki_epe",
        "name": "Lekki-Epe Expressway (Awoyaya, Sangotedo, Agungi)",
        "area": "Lekki/Ajah",
        "tier": 2, "mm_threshold": 15, "hours_threshold": 4,
    },
    {
        "id": "ikorodu_road",
        "name": "Ikorodu Road (Anthony-Odo-Iyalaro Bridge)",
        "area": "Ikorodu Road",
        "tier": 2, "mm_threshold": 15, "hours_threshold": 4,
    },
    {
        "id": "gbagada",
        "name": "Gbagada Expressway",
        "area": "Gbagada",
        "tier": 2, "mm_threshold": 20, "hours_threshold": 4,
    },
    {
        "id": "orile_agege",
        "name": "Orile-Agege / Pen Cinema corridor",
        "area": "Agege/Pen Cinema",
        "tier": 2, "mm_threshold": 18, "hours_threshold": 4,
    },
    {
        "id": "ilupeju_mushin",
        "name": "Ilupeju Road / Mushin",
        "area": "Ilupeju/Mushin",
        "tier": 2, "mm_threshold": 18, "hours_threshold": 4,
    },
    {
        "id": "vi_ahmadu_bello",
        "name": "Victoria Island (Ahmadu Bello Way)",
        "area": "Victoria Island",
        "tier": 2, "mm_threshold": 20, "hours_threshold": 5,
    },
]

# ── Day context strings (injected into narrator prompt) ────────────────────────
DAY_CONTEXT: dict[str, str] = {
    "monday":    "Monday rush: compressed weekend movement, full working week begins",
    "tuesday":   "Midweek: standard Lagos rush hour density",
    "wednesday": "Midweek: standard Lagos rush hour density",
    "thursday":  "Late-week build: Thursday PM is Lagos's second-worst evening",
    "friday":    "Friday: Lagos's heaviest traffic day, morning and evening both severe",
    "saturday":  "Saturday: lighter commute but market and leisure traffic builds midday",
    "sunday":    "Sunday: lightest day, church traffic the only notable window 9-12",
}
