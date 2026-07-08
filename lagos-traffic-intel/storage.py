import hashlib
import json
import os
from datetime import datetime, timezone

from config import TODAY_JSON, HISTORY_JSON, SEEN_POSTS_JSON, DATA_DIR

MAX_HISTORY = 30

# Covers the ~10h15m gap between the two daily runs plus slack for a late or
# missed run, so a post stays "seen" across one full day/night cycle but
# doesn't linger forever.
SEEN_POSTS_RETENTION_HOURS = 30


def _ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def write_today(data: dict) -> None:
    """Atomic write — writes to .tmp then renames so the live file is never partial."""
    _ensure_dirs()
    tmp = TODAY_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TODAY_JSON)


def read_today() -> dict | None:
    try:
        with open(TODAY_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def append_history(data: dict) -> None:
    """Keeps last MAX_HISTORY entries. Writes daily even if today already exists."""
    _ensure_dirs()
    history: list[dict] = []
    try:
        with open(HISTORY_JSON, encoding="utf-8") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Replace today's entry if already present, else append
    today_label = data.get("date_label", "")
    history = [e for e in history if e.get("date_label") != today_label]
    history.append({
        "date_label":   data.get("date_label"),
        "generated_at": data.get("generated_at"),
        "score":        data.get("traffic", {}).get("score"),
        "level":        data.get("traffic", {}).get("level"),
        "hex_color":    data.get("traffic", {}).get("hex_color"),
        "precip_mm":    data.get("weather", {}).get("precip_sum_mm"),
    })
    history = history[-MAX_HISTORY:]

    tmp = HISTORY_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HISTORY_JSON)


def read_history() -> list[dict]:
    try:
        with open(HISTORY_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _post_key(post: dict) -> str:
    """Stable identity for a fetched post: its URL, or a hash of author+text
    when no URL is present, so posts without a URL still dedupe correctly."""
    url = (post.get("url") or "").strip()
    if url:
        return url
    raw = f"{post.get('author', '')}|{post.get('text', '')[:200]}"
    return "hash:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_seen_posts() -> dict:
    try:
        with open(SEEN_POSTS_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_seen_posts(seen: dict) -> None:
    _ensure_dirs()
    tmp = SEEN_POSTS_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SEEN_POSTS_JSON)


def filter_new_posts(posts: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Splits fetched posts into (new, already_seen) against a persisted seen-post
    registry, so a post already used in a previous run (e.g. a 4pm accident
    report that's still the newest thing in the feed at 5:45am the next
    morning because nothing fresh came in overnight) doesn't get parsed into a
    corridor report or signal a second time. Every non-error post passed in is
    recorded as seen for SEEN_POSTS_RETENTION_HOURS, and stale entries beyond
    that window are pruned on each call so the registry stays small.
    """
    seen = read_seen_posts()
    now = datetime.now(timezone.utc)

    fresh_seen: dict = {}
    for key, seen_at in seen.items():
        try:
            ts = datetime.fromisoformat(seen_at)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (now - ts).total_seconds() / 3600 <= SEEN_POSTS_RETENTION_HOURS:
                fresh_seen[key] = seen_at
        except (ValueError, TypeError):
            continue

    new_posts: list[dict] = []
    dup_posts: list[dict] = []
    for p in posts:
        if p.get("error"):
            new_posts.append(p)
            continue
        key = _post_key(p)
        if key in fresh_seen:
            dup_posts.append(p)
        else:
            new_posts.append(p)
            fresh_seen[key] = now.isoformat()

    write_seen_posts(fresh_seen)
    return new_posts, dup_posts


STALE_THRESHOLD_HOURS = 15  # pipeline runs at 5:45 and 16:00 WAT — max normal gap is ~13h45m


def mark_stale(today_data: dict | None) -> bool:
    """True if last run is older than the longest normal gap between the two daily runs."""
    if not today_data:
        return True
    gen = today_data.get("generated_at", "")
    try:
        ts = datetime.fromisoformat(gen)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        return age_hours > STALE_THRESHOLD_HOURS
    except (ValueError, TypeError):
        return True
